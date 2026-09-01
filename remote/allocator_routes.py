"""Publish allocator-owned llama.cpp routes through an existing remote Grid identity.

The remote provider already owns the relay credential, poll workers, and one stable node identity.
Allocator nodes therefore do not register a second provider.  They add loopback engines to the
existing remote run record and ask its serve process to atomically hot-reload the model union.

Only specs carrying ``allocator_host_id`` are owned here.  User-managed vLLM, ComfyUI, API, and
other external engines are preserved byte-for-byte.
"""

from __future__ import annotations

import os
import signal
import time
import uuid
from collections.abc import Iterable
from typing import Any, Callable

import httpx

from shared import jsonio, run_records
from shared.allocator.models import ResidencyState
from shared.allocator.runtime import ManagedResidency
from shared.filelock import file_lock


REMOTE_IDENTITY = "remote"
_OVERVIEW_PATH = "/relay/v1/grid/overview"


class RemoteProviderRoutePublisher:
    """Converge allocator routes without restarting or replacing the provider process."""

    def __init__(
        self,
        network_id: str,
        host_id: str,
        *,
        engine_id: str = REMOTE_IDENTITY,
        client: httpx.Client | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not network_id or not host_id:
            raise ValueError("remote allocator routing requires network_id and host_id")
        self.network_id = network_id
        self.host_id = host_id
        self.engine_id = engine_id
        self.client = client or httpx.Client(timeout=5.0, trust_env=True)
        self._monotonic = monotonic
        self._sleep = sleeper
        self._routable_models: tuple[str, ...] = ()

    @property
    def routable_models(self) -> tuple[str, ...]:
        return self._routable_models

    def sync(
        self,
        residencies: Iterable[ManagedResidency],
        *,
        deadline: float | None = None,
    ) -> tuple[str, ...]:
        """Publish READY residencies and confirm the relay sees the resulting node model set."""

        ready = {
            item.model_id: item
            for item in residencies
            if item.handle is not None and item.state == ResidencyState.READY
        }
        requested = tuple(sorted(ready))
        if deadline is not None and self._monotonic() >= deadline:
            return requested

        path = run_records.record_path(self.network_id, self.engine_id)
        revision = uuid.uuid4().hex
        with file_lock(path):
            record = jsonio.load_json(path)
            self._validate_live_record(record)
            existing = list(record.get("engines") or [])
            previously_managed = {
                str(model)
                for spec in existing
                if isinstance(spec, dict)
                and spec.get("allocator_host_id") == self.host_id
                for model in (spec.get("models") or [])
            }
            unmanaged = [
                dict(spec)
                for spec in existing
                if not isinstance(spec, dict)
                or spec.get("allocator_host_id") != self.host_id
            ]
            managed = [
                {
                    "endpoint_url": f"http://127.0.0.1:{ready[model].handle.port}/v1",
                    "models": [model],
                    "engine_label": "allocator:llama.cpp",
                    "allocator_host_id": self.host_id,
                }
                for model in requested
            ]
            desired = unmanaged + managed
            changed = desired != existing
            if changed:
                record["engines"] = desired
                record["models"] = list(
                    dict.fromkeys(
                        str(model)
                        for spec in desired
                        if isinstance(spec, dict)
                        for model in (spec.get("models") or [])
                    )
                )
                record["endpoint_url"] = (
                    desired[0].get("endpoint_url") if len(desired) == 1 else None
                )
                record["allocator_routing_revision"] = revision
                run_records.write_record(self.network_id, self.engine_id, record)
                self._signal(record)

        if self._relay_converged(
            record,
            set(requested),
            previously_managed.difference(requested),
            deadline=deadline,
        ):
            self._routable_models = requested
            return ()
        return requested or tuple(
            sorted(
                str(model)
                for spec in (record.get("engines") or [])
                if isinstance(spec, dict)
                and spec.get("allocator_host_id") == self.host_id
                for model in (spec.get("models") or [])
            )
        )

    def fence(self, *, deadline: float | None = None) -> tuple[str, ...]:
        """Remove every allocator-owned route and confirm the relay no longer advertises it."""

        return self.sync((), deadline=deadline)

    def _validate_live_record(self, record: dict[str, Any]) -> None:
        if not record:
            raise RuntimeError(
                f"remote provider {self.engine_id!r} is not joined to {self.network_id}"
            )
        if record.get("reload_signal") != "sighup":
            raise RuntimeError("remote provider does not support zero-drop route hot reload")
        if not run_records.record_alive(record):
            raise RuntimeError("remote provider process is not alive")
        specs = list(record.get("engines") or [])
        if any(not isinstance(spec, dict) or not spec.get("endpoint_url") for spec in specs):
            raise RuntimeError(
                "allocator routes require an external-only remote provider union"
            )

    @staticmethod
    def _signal(record: dict[str, Any]) -> None:
        pid = run_records.recorded_pid(record) or 0
        if pid <= 0 or not run_records.record_alive(record):
            raise RuntimeError("remote provider died before allocator route reload")
        os.kill(pid, signal.SIGHUP)

    def _relay_converged(
        self,
        record: dict[str, Any],
        expected_models: set[str],
        forbidden_models: set[str],
        *,
        deadline: float | None,
    ) -> bool:
        signaling_url = str(record.get("signaling_url") or "").rstrip("/")
        node_name = str(record.get("meta_name") or "")
        if not signaling_url or not node_name:
            raise RuntimeError("remote provider record is missing relay identity")
        end = deadline if deadline is not None else self._monotonic() + 30.0
        last_error = ""
        while self._monotonic() < end:
            current = jsonio.load_json(
                run_records.record_path(self.network_id, self.engine_id)
            )
            reload_error = str(current.get("last_reload_error") or "")
            if reload_error:
                raise RuntimeError(f"remote provider rejected allocator route reload: {reload_error}")
            try:
                response = self.client.get(f"{signaling_url}{_OVERVIEW_PATH}")
                response.raise_for_status()
                payload = response.json()
                nodes = payload.get("nodes") if isinstance(payload, dict) else None
                if isinstance(nodes, list):
                    matches = [
                        node
                        for node in nodes
                        if isinstance(node, dict)
                        and str(node.get("name") or "") == node_name
                    ]
                    if len(matches) > 1:
                        raise RuntimeError(
                            f"remote provider name {node_name!r} is not unique on the grid"
                        )
                    for node in matches:
                        visible = {
                            str(model).casefold() for model in (node.get("models") or [])
                        }
                        expected_folded = {model.casefold() for model in expected_models}
                        forbidden_folded = {
                            model.casefold() for model in forbidden_models
                        }
                        if expected_folded.issubset(visible) and visible.isdisjoint(
                            forbidden_folded
                        ):
                            return True
                        last_error = (
                            f"relay advertises {sorted(visible)}; managed routes require "
                            f"{sorted(expected_folded)} and forbid {sorted(forbidden_folded)}"
                        )
                        break
            except (httpx.HTTPError, TypeError, ValueError) as exc:
                last_error = str(exc)
            self._sleep(min(0.2, max(0.0, end - self._monotonic())))
        if last_error:
            raise RuntimeError(f"remote provider route convergence timed out: {last_error}")
        return False
