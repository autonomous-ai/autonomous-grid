"""Runtime lifecycle qualification with durable, machine-readable evidence."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from shared import jsonio


@dataclass(frozen=True, slots=True)
class QualificationStep:
    name: str
    passed: bool
    duration_seconds: float
    detail: str = ""


@dataclass(frozen=True, slots=True)
class QualificationReport:
    runtime: str
    model_id: str
    passed: bool
    started_at: float
    completed_at: float
    artifact_sha256: str
    fetched_artifact: bool
    cleaned_artifact: bool
    steps: tuple[QualificationStep, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def qualify_runtime(
    *,
    runtime: str,
    backend: Any,
    model_id: str,
    port: int,
    inference: Callable[[Any], tuple[int, str]],
    artifact_source: str = "",
    artifact_sha256: str = "",
    artifact_size_mb: int = 0,
    cleanup_artifact: bool = False,
) -> QualificationReport:
    """Exercise fetch → warm → ownership/readiness → inference → drain/stop.

    Every attempted teardown runs from ``finally``. Cleanup applies only to an artifact fetched by
    this run, never to a pre-existing cache entry.
    """

    started_at = time.time()
    steps: list[QualificationStep] = []
    handle = None
    fetched = False
    cleaned = False
    observed_digest = ""

    def step(name: str, operation: Callable[[], Any], describe: Callable[[Any], str] = str) -> Any:
        step_started = time.monotonic()
        try:
            result = operation()
            detail = describe(result) if result is not None else "ok"
            steps.append(QualificationStep(name, True, time.monotonic() - step_started, detail))
            return result
        except Exception as exc:
            steps.append(
                QualificationStep(
                    name,
                    False,
                    time.monotonic() - step_started,
                    f"{type(exc).__name__}: {exc}",
                )
            )
            raise

    try:
        cached = step("inventory", backend.cached_models, lambda value: f"{len(value)} cached")
        if model_id not in cached:
            if not artifact_source or not artifact_sha256 or artifact_size_mb <= 0:
                raise RuntimeError(
                    "uncached qualification requires artifact source, digest, and size bound"
                )
            observed_digest = step(
                "fetch",
                lambda: backend.fetch_artifact(
                    model_id, artifact_source, artifact_sha256, artifact_size_mb
                ),
            )
            fetched = True
        else:
            observed_digest = step(
                "artifact-identity", lambda: backend.artifact_sha256(model_id)
            )
            if artifact_sha256 and observed_digest != artifact_sha256.lower():
                raise RuntimeError("cached artifact digest does not match the requested revision")
        handle = step("warm", lambda: backend.start(model_id, port), lambda value: str(value.runtime))
        step(
            "ownership",
            lambda: _require(backend.owns(handle, model_id), "runtime ownership was not proven"),
        )
        step(
            "readiness",
            lambda: _require(backend.ready(handle, model_id), "native readiness was not proven"),
        )
        def run_inference():
            result = inference(handle)
            _require(result[0] > 0, "real inference produced no output")
            return result

        step(
            "real-inference",
            run_inference,
            lambda value: f"{value[0]} output units · {value[1]}",
        )
        def probe_activity():
            active = backend.active_requests(handle, model_id)
            if active is not None:
                _require(isinstance(active, int) and active >= 0, "activity probe is malformed")
            return active

        step("activity-probe", probe_activity)
    except Exception as exc:
        if not steps or steps[-1].passed:
            steps.append(QualificationStep("qualification", False, 0.0, str(exc)))
    finally:
        if handle is not None:
            try:
                step("drain-stop", lambda: backend.stop(handle, model_id))
            except Exception:
                pass
        if fetched and cleanup_artifact and observed_digest:
            try:
                step("artifact-cleanup", lambda: backend.evict_artifact(model_id, observed_digest))
                cleaned = True
            except Exception:
                pass
        close = getattr(backend, "close", None)
        if callable(close):
            close()
    passed = (
        all(item.passed for item in steps)
        and any(item.name == "real-inference" for item in steps)
        and any(item.name == "drain-stop" for item in steps)
    )
    return QualificationReport(
        runtime=runtime,
        model_id=model_id,
        passed=passed,
        started_at=started_at,
        completed_at=time.time(),
        artifact_sha256=observed_digest,
        fetched_artifact=fetched,
        cleaned_artifact=cleaned,
        steps=tuple(steps),
    )


def save_report(path: Path, report: QualificationReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    jsonio.atomic_write_json(path, report.to_dict(), mode=0o600)


def _require(condition: bool, message: str) -> str:
    if not condition:
        raise RuntimeError(message)
    return "ok"
