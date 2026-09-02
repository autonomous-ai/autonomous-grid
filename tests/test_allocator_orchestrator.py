from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from shared.allocator.models import ActionKind, MutationAction
from shared.allocator.orchestrator import EngineOrchestratorBackend, OllamaBackend
from shared.allocator.runtime import ManagedModelRuntime, RuntimeHandle


class StubBackend:
    endpoint_scheme = "http"
    endpoint_path = "/native"
    api_key = ""

    def __init__(self, runtime: str, cached: tuple[str, ...]) -> None:
        self.runtime = runtime
        self.cached = cached
        self.started: list[tuple[str, int]] = []

    def cached_models(self) -> tuple[str, ...]:
        return self.cached

    def artifact_sha256(self, model_id: str) -> str:
        assert model_id in self.cached
        return "a" * 64

    def start(self, model_id: str, port: int) -> RuntimeHandle:
        self.started.append((model_id, port))
        return RuntimeHandle(123, port, model_path=model_id, runtime=self.runtime)

    def alive(self, handle: RuntimeHandle) -> bool:
        return handle.runtime == self.runtime

    def owns(self, handle: RuntimeHandle, model_id: str) -> bool:
        return self.alive(handle) and handle.model_path == model_id

    def ready(self, handle: RuntimeHandle, model_id: str) -> bool:
        return self.owns(handle, model_id)

    def stop(self, handle: RuntimeHandle, model_id: str) -> None:
        assert self.owns(handle, model_id)

    def active_requests(self, handle: RuntimeHandle, model_id: str) -> int:
        assert self.owns(handle, model_id)
        return 0


def action(
    model_id: str,
    runtime: str,
    kind: ActionKind = ActionKind.LOAD,
) -> MutationAction:
    return MutationAction(
        action_id=f"{kind.value}-{model_id}",
        kind=kind,
        node_id="host-a",
        model_id=model_id,
        memory_mb=100,
        reason="test",
        plan_generation="plan-1",
        created_at=1,
        executable=True,
        runtime=runtime,
    )


def test_orchestrator_dispatches_to_selected_runtime_and_restores_binding() -> None:
    llama = StubBackend("llama.cpp", ("tiny.gguf",))
    vllm = StubBackend("vllm", ("coder",))
    backend = EngineOrchestratorBackend({"llama.cpp": llama, "vllm": vllm})

    backend.prepare(action("coder", "vllm"))
    handle = backend.start("coder", 18_100)

    assert handle.runtime == "vllm"
    assert vllm.started == [("coder", 18_100)]
    assert llama.started == []
    assert backend.endpoint_path_for("coder") == "/native"

    restored = EngineOrchestratorBackend({"llama.cpp": llama, "vllm": vllm})
    restored.bind("coder", "vllm")
    assert restored.runtime_for("coder") == "vllm"
    with pytest.raises(RuntimeError, match="already bound"):
        restored.bind("coder", "llama.cpp")


def test_node_persists_inferred_runtime_from_an_older_controller(tmp_path) -> None:
    llama = StubBackend("llama.cpp", ("tiny.gguf",))
    vllm = StubBackend("vllm", ("coder",))
    backend = EngineOrchestratorBackend({"llama.cpp": llama, "vllm": vllm})
    managed = ManagedModelRuntime(
        tmp_path / "runtime.json",
        host_id="host-a",
        backend=backend,
        port_available=lambda _port: True,
    )

    managed.begin(action("coder", ""))
    assert managed.wait_idle(2)
    managed.begin(action("coder", "", ActionKind.WARM))
    assert managed.wait_idle(2)

    residency = next(item for item in managed.residencies if item.model_id == "coder")
    assert residency.runtime == "vllm"
    assert residency.handle is not None and residency.handle.runtime == "vllm"


def test_ollama_lifecycle_requires_native_readiness() -> None:
    digest = hashlib.sha256(b"tiny").hexdigest()
    loaded = False
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal loaded
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={"models": [{"name": "tiny:latest", "digest": f"sha256:{digest}", "size": 4}]},
            )
        if request.url.path == "/api/ps":
            return httpx.Response(
                200,
                json={"models": [{"name": "tiny:latest"}] if loaded else []},
            )
        if request.url.path == "/api/generate":
            payload = json.loads(request.content)
            calls.append(payload)
            loaded = payload["keep_alive"] == -1
            return httpx.Response(200, json={"done": True})
        raise AssertionError(request.url)

    backend = OllamaBackend()
    backend.client.close()
    backend.client = httpx.Client(transport=httpx.MockTransport(handler))

    assert backend.cached_models() == ("tiny:latest",)
    assert backend.artifact_sha256("tiny:latest") == digest
    handle = backend.start("tiny:latest", 19_999)
    assert handle.runtime == "ollama"
    assert handle.port == 11_434
    assert backend.ready(handle, "tiny:latest")
    backend.stop(handle, "tiny:latest")
    assert not backend.ready(handle, "tiny:latest")
    assert [call["keep_alive"] for call in calls] == [-1, 0]


def test_ollama_start_failure_never_returns_a_routable_handle() -> None:
    digest = hashlib.sha256(b"broken").hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={"models": [{"name": "broken", "digest": digest}]},
            )
        if request.url.path == "/api/generate":
            return httpx.Response(500, json={"error": "model load failed"})
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        raise AssertionError(request.url)

    backend = OllamaBackend()
    backend.client.close()
    backend.client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(httpx.HTTPStatusError):
        backend.start("broken", 18_100)
