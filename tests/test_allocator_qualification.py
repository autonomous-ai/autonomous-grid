from __future__ import annotations

import json
import os

from cli import parser
from shared.allocator.qualification import qualify_runtime, save_report
from shared.allocator.runtime import RuntimeHandle


class FakeBackend:
    def __init__(self, *, cached=True, fail_inference=False):
        self.cached = cached
        self.fail_inference = fail_inference
        self.calls = []
        self.running = False

    def cached_models(self):
        self.calls.append("inventory")
        return ("tiny",) if self.cached else ()

    def artifact_sha256(self, model):
        assert model == "tiny"
        return "a" * 64

    def fetch_artifact(self, model, source, digest, size):
        self.calls.append(("fetch", model, source, digest, size))
        self.cached = True
        return digest

    def start(self, model, port):
        self.calls.append(("warm", model, port))
        self.running = True
        return RuntimeHandle(123, port, model_path=model, runtime="fake")

    def owns(self, handle, model):
        return self.running and handle.model_path == model

    def ready(self, handle, model):
        return self.owns(handle, model)

    def active_requests(self, handle, model):
        assert self.owns(handle, model)
        return 0

    def stop(self, handle, model):
        assert self.owns(handle, model)
        self.calls.append("stop")
        self.running = False

    def evict_artifact(self, model, digest):
        self.calls.append(("evict", model, digest))
        self.cached = False


def test_qualification_proves_full_real_lifecycle_and_keeps_preexisting_artifact():
    backend = FakeBackend()
    report = qualify_runtime(
        runtime="fake",
        backend=backend,
        model_id="tiny",
        port=18001,
        artifact_sha256="a" * 64,
        inference=lambda _handle: (4, "real response"),
        cleanup_artifact=True,
    )
    assert report.passed
    assert [item.name for item in report.steps] == [
        "inventory",
        "artifact-identity",
        "warm",
        "ownership",
        "readiness",
        "real-inference",
        "activity-probe",
        "drain-stop",
    ]
    assert not report.fetched_artifact
    assert not report.cleaned_artifact
    assert "stop" in backend.calls


def test_failed_inference_still_stops_and_cleans_only_new_artifact():
    backend = FakeBackend(cached=False)

    def fail(_handle):
        raise RuntimeError("native generation failed")

    report = qualify_runtime(
        runtime="fake",
        backend=backend,
        model_id="tiny",
        port=18001,
        artifact_source="test://tiny",
        artifact_sha256="a" * 64,
        artifact_size_mb=10,
        inference=fail,
        cleanup_artifact=True,
    )
    assert not report.passed
    assert "stop" in backend.calls
    assert ("evict", "tiny", "a" * 64) in backend.calls
    assert report.fetched_artifact and report.cleaned_artifact
    assert next(item for item in report.steps if item.name == "real-inference").passed is False


def test_missing_uncached_identity_is_reported_without_warming():
    backend = FakeBackend(cached=False)
    report = qualify_runtime(
        runtime="fake",
        backend=backend,
        model_id="tiny",
        port=18001,
        inference=lambda _handle: (1, "unused"),
    )
    assert not report.passed
    assert report.steps[-1].name == "qualification"
    assert "digest" in report.steps[-1].detail
    assert "stop" not in backend.calls


def test_report_is_owner_only_and_json_round_trips(tmp_path):
    report = qualify_runtime(
        runtime="fake",
        backend=FakeBackend(),
        model_id="tiny",
        port=1,
        inference=lambda _handle: (1, "ok"),
    )
    target = tmp_path / "report.json"
    save_report(target, report)
    assert json.loads(target.read_text())["passed"] is True
    if os.name == "posix":
        assert target.stat().st_mode & 0o077 == 0


def test_parser_exposes_physical_runtime_qualification():
    args = parser.build_parser().parse_args(
        [
            "allocator",
            "qualify",
            "vllm",
            "Qwen/tiny",
            "--artifact-source",
            "hf://Qwen/tiny@" + "a" * 40,
            "--artifact-sha256",
            "b" * 64,
            "--artifact-size-mb",
            "1000",
            "--cleanup-artifact",
        ]
    )
    assert args.handler.__name__ == "cmd_allocator_qualify"
    assert args.tensor_parallel_size == 1
    assert args.cleanup_artifact is True
