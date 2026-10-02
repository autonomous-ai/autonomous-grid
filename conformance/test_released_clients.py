"""Every `grid` in the field, against today's master behind the proxy (grid-platform ticket 13; ADR 0004).

Each test runs once per released version (0.3.47–0.3.52) and once for the harness's pinned `grid`. Behaviour that a
version was never built to have is asserted as THAT version's behaviour, so a change that breaks an old client is a
failure here, and a client that changed on purpose names the version it changed in:

* reads (`models`, `engines`, `stats`) of an awake grid answer;
* `--no-wake` on an asleep grid: from 0.3.49 a refusal whose `--json` envelope carries `grid_asleep`, never a wake;
  before it, argparse's exit 2 before anything is sent;
* a signed-in read wakes an asleep grid;
* `grid join --at` registers, heart-beats and polls, and a consumer's request through the proxy is served by it;
* a joined provider on an asleep grid outlives the sleep and serves after the wake;
* a joined provider on a deleted grid stops from 0.3.48 (`grid_deleted` ends an engine) and keeps retrying before it.
"""
from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time

import grid_protocol
import httpx
import pytest
from grid_protocol.constants import GRID_ASLEEP_CODE

from conformance.clients import Client, envelope, parse
from conformance.stack import (
    ENGINE_ANSWER,
    ENGINE_MODEL,
    INFERENCE_SCOPES,
    PROVIDER_SCOPES,
    Stack,
)

READS = ("models", "engines", "stats")
#: `grid_deleted` stops an engine, and `grid_asleep`/`grid_stopped` park one, from 0.3.48 (`idle-sleep` issues 02, 04).
PROVIDER_CODES_SINCE = (0, 3, 48)


def _as_consumer(client: Client, stack: Stack) -> None:
    client.sign_in(stack, token=stack.consumer_token(), roles=["consumer"], scopes=INFERENCE_SCOPES)


def _as_provider(client: Client, stack: Stack) -> None:
    client.sign_in(stack, token=stack.provider_token(), roles=["consumer", "provider"],
                   scopes=INFERENCE_SCOPES + PROVIDER_SCOPES)


# ── Reads ────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("read", READS)
def test_a_read_of_an_awake_grid_answers(client, stack, read):
    _as_consumer(client, stack)

    done = client.run("--remote", read, stack.network_id, "--json")

    assert done.returncode == 0, done.stderr
    json.loads(done.stdout)  # a JSON answer, whatever its shape in this version


@pytest.mark.parametrize("read", READS)
def test_no_wake_on_an_asleep_grid_never_wakes_it(client, stack, read):
    _as_consumer(client, stack)
    stack.set_state("asleep", last_known={"nodes": [], "ids": []})

    done = client.run("--remote", read, stack.network_id, "--no-wake", "--json")

    if client.has_no_wake:
        assert done.returncode != 0
        answer = envelope(done)
        assert answer is not None, done.stderr
        grid_protocol.validator("cli-error").validate(answer)
        assert answer["error"]["code"] == GRID_ASLEEP_CODE, answer
    else:
        assert done.returncode == 2 and "--no-wake" in done.stderr, "older than 0.3.49: refused at argparse"
    assert stack.state()["state"] == "asleep", "a read under --no-wake must never wake a grid"


def test_a_signed_in_read_wakes_an_asleep_grid(client, stack):
    _as_consumer(client, stack)
    stack.set_state("asleep", last_known={"nodes": [], "ids": []})

    done = client.run("--remote", "models", stack.network_id, "--json", timeout=120)

    assert done.returncode == 0, done.stderr
    assert stack.state()["state"] == "running" and stack.state().get("woken", 0) >= 1


# ── A provider ───────────────────────────────────────────────────────────────────────────────────


class _Joined:
    """`grid join --at` with this client, left again at the end whatever happened."""

    def __init__(self, client: Client, stack: Stack) -> None:
        self.client, self.stack = client, stack
        self.name = f"conformance-{client.version.replace('.', '-')}"

    def __enter__(self) -> _Joined:
        _as_provider(self.client, self.stack)
        done = self.client.run("join", self.stack.network_id, "--at", self.stack.engine_url, "-m", ENGINE_MODEL,
                               "--name", self.name, timeout=120)
        try:
            assert done.returncode == 0, done.stdout + done.stderr
            # The engine is a real, live process: otherwise every "it stopped" below would pass by never having run.
            _until(self.engine_alive, 30, f"{self.name}: no live engine process after `grid join`")
            _until(lambda: self.name in _node_names(self.stack), 60, f"{self.name} never appeared in the overview")
        except BaseException:
            self.__exit__()  # a detached engine outlives the test unless it is left here
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        self.stack.set_state("running")
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.client.run("leave", self.stack.network_id, timeout=60)
        for pid in self.engine_pids():  # whatever `grid leave` did not end, ended here
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)

    def engine_pids(self) -> list[int]:
        records = (self.client.home / ".grid" / "run" / "engines" / self.stack.network_id)
        pids = []
        for record in records.glob("*.json") if records.exists() else []:
            try:
                pids.append(int(json.loads(record.read_text())["pid"]))
            except (ValueError, KeyError, TypeError):
                continue
        return pids

    def engine_alive(self) -> bool:
        for pid in self.engine_pids():
            try:
                os.kill(pid, 0)
                return True
            except ProcessLookupError:
                continue
            except PermissionError:
                return True
        return False


def _node_names(stack: Stack) -> list[str]:
    try:
        answer = httpx.get(f"{stack.grid_url}/relay/v1/grid/overview", timeout=10)
    except httpx.HTTPError:
        return []
    return [node.get("name") for node in answer.json().get("nodes", [])] if answer.status_code == 200 else []


def _chat(stack: Stack, *, model: str = ENGINE_MODEL, stream: bool = False) -> httpx.Response:
    return httpx.post(f"{stack.grid_url}/relay/v1/chat/completions", timeout=120,
                      headers={"Authorization": f"Bearer {stack.consumer_token()}"},
                      json={"model": model, "stream": stream, "messages": [{"role": "user", "content": "say it"}]})


def _served(answer: httpx.Response) -> bool:
    return answer.status_code == 200 and answer.json()["choices"][0]["message"]["content"] == ENGINE_ANSWER


def _streamed(answer: httpx.Response) -> bool:
    """The provider's streamed upload, relayed as server-sent events, carries the engine's answer."""
    if answer.status_code != 200:
        return False
    text = "".join(
        (json.loads(line[6:])["choices"][0].get("delta") or {}).get("content") or ""
        for line in answer.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]" and json.loads(line[6:]).get("choices")
    )
    return ENGINE_ANSWER in text


def _until(condition, seconds: float, message: str) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.5)
    raise AssertionError(message)


def test_a_joined_provider_serves_a_request_through_the_proxy(client, stack):
    with _Joined(client, stack):
        answer = _chat(stack)
        streamed = _chat(stack, stream=True)

        assert _served(answer), answer.text
        assert _streamed(streamed), streamed.text


def test_a_provider_outlives_a_sleep_and_serves_after_the_wake(client, stack):
    with _Joined(client, stack) as joined:
        stack.set_state("asleep", last_known={"nodes": [], "ids": []})
        time.sleep(12)  # past a parked provider's 10 s probe
        assert joined.engine_alive(), "a sleeping grid must never end an engine (it parks from 0.3.48)"

        stack.set_state("running")

        _until(lambda: _served(_chat(stack)), 90, "the provider never served again after the wake")


def test_a_provider_on_a_deleted_grid(client, stack):
    with _Joined(client, stack) as joined:
        stack.set_state("deleted")

        if parse(client.version) >= PROVIDER_CODES_SINCE:
            _until(lambda: not joined.engine_alive(), 60, "grid_deleted must end the engine from 0.3.48")
        else:
            time.sleep(15)
            assert joined.engine_alive(), "0.3.47 never parsed grid_deleted: it keeps retrying"


def test_a_provider_whose_node_row_is_gone_registers_again_and_serves(client, stack):
    """grid-platform ticket 23: a node whose row was pruned, lost in a restore or left behind by a move. Every released
    CLI registers again when its heartbeat is answered 404 (`grid-protocol` node-heartbeat). Before the master's half
    of that rule it re-created the row with no models and answered 200, so the provider stayed connected, its polls
    answered 204, and it was never given work again — which is what this fails on."""
    with _Joined(client, stack) as joined:
        _until(lambda: _served(_chat(stack)), 60, f"{joined.name} never served before its row was deleted")

        assert stack.delete_node(joined.name) == 1

        # One heartbeat (HEARTBEAT_INTERVAL_SECONDS, 30) to be answered 404, the registration, and a poll.
        _until(lambda: _served(_chat(stack)), 90, f"{joined.name} never served again after its node row was deleted")
