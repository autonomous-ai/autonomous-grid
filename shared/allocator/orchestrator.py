"""Allocator lifecycle adapters for Grid's multi-engine orchestrator.

The allocator chooses desired model residency.  These adapters translate that common lifecycle
into the native control surface of each engine while keeping routing in Grid's existing provider
union.  A model is never advertised merely because an engine was discovered: ``ready`` must prove
that the selected runtime can actually serve it.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

from shared import jsonio
from shared.allocator.models import MutationAction, canonical_sha256
from shared.allocator.runtime import LlamaCppBackend, RuntimeHandle


class EngineOrchestratorBackend:
    """Dispatch the managed-runtime protocol to the runtime selected by a placement action."""

    def __init__(self, adapters: Mapping[str, Any], *, default_runtime: str = "llama.cpp") -> None:
        self.adapters = dict(adapters)
        if default_runtime not in self.adapters:
            raise ValueError("orchestrator default runtime is not configured")
        self.default_runtime = default_runtime
        default = self.adapters[default_runtime]
        self.bind_host = str(getattr(default, "bind_host", "127.0.0.1"))
        self.endpoint_host = str(getattr(default, "endpoint_host", ""))
        self.endpoint_scheme = str(getattr(default, "endpoint_scheme", "http"))
        self.tls_cert_file = str(getattr(default, "tls_cert_file", ""))
        self.tls_key_file = str(getattr(default, "tls_key_file", ""))
        self.tls_ca_file = str(getattr(default, "tls_ca_file", ""))
        self.tls_ca_pem = str(getattr(default, "tls_ca_pem", "") or "")
        self._model_runtimes: dict[str, str] = {}
        self._lock = threading.RLock()

    @property
    def supported_runtimes(self) -> tuple[str, ...]:
        return tuple(sorted(self.adapters))

    def prepare(self, action: MutationAction) -> None:
        runtime = action.runtime or self._infer_runtime(action.model_id)
        if runtime not in self.adapters:
            raise RuntimeError(f"managed runtime {runtime!r} is not installed on this node")
        with self._lock:
            current = self._model_runtimes.get(action.model_id)
            if current and current != runtime:
                raise RuntimeError(
                    f"model {action.model_id!r} is already bound to runtime {current!r}"
                )
            self._model_runtimes[action.model_id] = runtime

    def bind(self, model_id: str, runtime: str) -> None:
        """Restore a previously persisted model/runtime binding before process recovery."""

        if not runtime:
            return
        if runtime not in self.adapters:
            raise RuntimeError(
                f"persisted runtime {runtime!r} for {model_id!r} is not installed on this node"
            )
        with self._lock:
            current = self._model_runtimes.get(model_id)
            if current and current != runtime:
                raise RuntimeError(
                    f"model {model_id!r} is already bound to runtime {current!r}"
                )
            self._model_runtimes[model_id] = runtime

    def configure_api_key(self, api_key: str) -> None:
        for adapter in self.adapters.values():
            configure = getattr(adapter, "configure_api_key", None)
            if callable(configure):
                configure(api_key)

    def configure_api_key_file(self, path: str | os.PathLike[str]) -> None:
        for adapter in self.adapters.values():
            configure = getattr(adapter, "configure_api_key_file", None)
            if callable(configure):
                configure(path)

    def cached_models(self) -> tuple[str, ...]:
        models: set[str] = set()
        for runtime, adapter in self.adapters.items():
            try:
                found = adapter.cached_models()
            except Exception:  # discovery is best-effort; lifecycle calls remain strict
                continue
            models.update(found)
            with self._lock:
                for model in found:
                    self._model_runtimes.setdefault(model, runtime)
        return tuple(sorted(models))

    def artifact_sha256(self, model_id: str) -> str:
        return self._for_model(model_id).artifact_sha256(model_id)

    def fetch_artifact(
        self,
        model_id: str,
        source: str,
        expected_sha256: str,
        max_size_mb: int,
    ) -> str:
        return self._for_model(model_id).fetch_artifact(
            model_id, source, expected_sha256, max_size_mb
        )

    def evict_artifact(self, model_id: str, expected_sha256: str) -> None:
        self._for_model(model_id).evict_artifact(model_id, expected_sha256)

    def start(self, model_id: str, port: int) -> RuntimeHandle:
        handle = self._for_model(model_id).start(model_id, port)
        return _handle_with_runtime(handle, self.runtime_for(model_id, handle))

    def start_with_callback(
        self,
        model_id: str,
        port: int,
        on_spawn: Callable[[RuntimeHandle], None],
    ) -> RuntimeHandle:
        adapter = self._for_model(model_id)
        callback_start = getattr(adapter, "start_with_callback", None)
        runtime = self.runtime_for(model_id)
        if not callable(callback_start):
            handle = _handle_with_runtime(adapter.start(model_id, port), runtime)
            on_spawn(handle)
            return handle

        def publish(handle: RuntimeHandle) -> None:
            on_spawn(_handle_with_runtime(handle, runtime))

        return _handle_with_runtime(callback_start(model_id, port, publish), runtime)

    def alive(self, handle: RuntimeHandle) -> bool:
        return bool(self._for_handle(handle).alive(handle))

    def owns(self, handle: RuntimeHandle, model_id: str) -> bool:
        return bool(self._for_handle(handle, model_id).owns(handle, model_id))

    def ready(self, handle: RuntimeHandle, model_id: str) -> bool:
        return bool(self._for_handle(handle, model_id).ready(handle, model_id))

    def stop(self, handle: RuntimeHandle, model_id: str) -> None:
        self._for_handle(handle, model_id).stop(handle, model_id)

    def active_requests(self, handle: RuntimeHandle, model_id: str) -> int | None:
        query = getattr(self._for_handle(handle, model_id), "active_requests", None)
        return query(handle, model_id) if callable(query) else None

    def cancel_pending(self) -> None:
        for adapter in self.adapters.values():
            cancel = getattr(adapter, "cancel_pending", None)
            if callable(cancel):
                cancel()

    def endpoint_scheme_for(self, model_id: str) -> str:
        return str(getattr(self._for_model(model_id), "endpoint_scheme", "http"))

    def endpoint_path_for(self, model_id: str) -> str:
        return str(getattr(self._for_model(model_id), "endpoint_path", "/v1"))

    def endpoint_host_for(self, model_id: str, fallback: str) -> str:
        adapter = self._for_model(model_id)
        base_url = str(getattr(adapter, "base_url", ""))
        if base_url:
            parsed = urlsplit(base_url)
            if parsed.hostname:
                return parsed.hostname
        return str(getattr(adapter, "endpoint_host", fallback) or fallback)

    def engine_api_key_for(self, model_id: str) -> str:
        value = getattr(self._for_model(model_id), "api_key", "")
        if callable(value):
            value = value()
        return str(value or "")

    def runtime_for(self, model_id: str, handle: RuntimeHandle | None = None) -> str:
        if handle is not None and handle.runtime:
            return handle.runtime
        with self._lock:
            return self._model_runtimes.get(model_id) or self._infer_runtime(model_id)

    def _infer_runtime(self, model_id: str) -> str:
        if model_id.lower().endswith(".gguf"):
            return "llama.cpp"
        candidates = [
            runtime
            for runtime, adapter in self.adapters.items()
            if runtime != self.default_runtime and model_id in _safe_cached(adapter)
        ]
        return sorted(candidates)[0] if candidates else self.default_runtime

    def _for_model(self, model_id: str) -> Any:
        runtime = self.runtime_for(model_id)
        try:
            return self.adapters[runtime]
        except KeyError as exc:
            raise RuntimeError(f"managed runtime {runtime!r} is not configured") from exc

    def _for_handle(self, handle: RuntimeHandle, model_id: str = "") -> Any:
        runtime = handle.runtime or (self.runtime_for(model_id) if model_id else self.default_runtime)
        try:
            return self.adapters[runtime]
        except KeyError as exc:
            raise RuntimeError(f"managed runtime {runtime!r} is not configured") from exc


class OllamaBackend:
    """Manage Ollama model residency without owning or restarting the shared Ollama daemon."""

    endpoint_scheme = "http"
    bind_host = "127.0.0.1"
    api_key = ""
    endpoint_path = "/v1"

    def __init__(self, base_url: str = "http://127.0.0.1:11434", *, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        parsed = urlsplit(self.base_url)
        self.port = parsed.port or 11434
        self.client = httpx.Client(timeout=timeout, trust_env=False)

    def cached_models(self) -> tuple[str, ...]:
        payload = self._json("GET", "/api/tags")
        return tuple(
            str(item["name"])
            for item in payload.get("models", [])
            if isinstance(item, Mapping) and item.get("name")
        )

    def artifact_sha256(self, model_id: str) -> str:
        for item in self._json("GET", "/api/tags").get("models", []):
            if isinstance(item, Mapping) and item.get("name") == model_id:
                return _ollama_digest(item.get("digest"))
        raise RuntimeError(f"Ollama model is not installed: {model_id!r}")

    def fetch_artifact(
        self, model_id: str, source: str, expected_sha256: str, max_size_mb: int
    ) -> str:
        parsed = urlsplit(source)
        if parsed.scheme != "ollama":
            raise RuntimeError("Ollama artifacts require ollama://model sources")
        source_model = unquote(f"{parsed.netloc}{parsed.path}").lstrip("/")
        if source_model != model_id:
            raise RuntimeError("Ollama source model does not match the placement model")
        expected = canonical_sha256(expected_sha256)
        if model_id in self.cached_models():
            existing = self.artifact_sha256(model_id)
            if existing != expected:
                raise RuntimeError("refusing to replace an existing Ollama tag with another digest")
            return existing
        response = self.client.post(
            f"{self.base_url}/api/pull",
            json={"model": model_id, "stream": False},
        )
        _raise_engine_error(response, "Ollama pull")
        digest = self.artifact_sha256(model_id)
        if digest != expected:
            self.evict_artifact(model_id, digest)
            raise RuntimeError("pulled Ollama model digest does not match the pinned artifact")
        for item in self._json("GET", "/api/tags").get("models", []):
            if isinstance(item, Mapping) and item.get("name") == model_id:
                size = int(item.get("size") or 0)
                if size > max_size_mb * 1024 * 1024:
                    self.evict_artifact(model_id, digest)
                    raise RuntimeError("pulled Ollama model exceeds the configured size bound")
        return digest

    def evict_artifact(self, model_id: str, expected_sha256: str) -> None:
        if self.artifact_sha256(model_id) != canonical_sha256(expected_sha256):
            raise RuntimeError("refusing to delete an Ollama model with a different digest")
        response = self.client.request(
            "DELETE", f"{self.base_url}/api/delete", json={"model": model_id}
        )
        _raise_engine_error(response, "Ollama delete")

    def start(self, model_id: str, port: int) -> RuntimeHandle:
        del port
        response = self.client.post(
            f"{self.base_url}/api/generate",
            json={"model": model_id, "prompt": "", "stream": False, "keep_alive": -1},
        )
        _raise_engine_error(response, "Ollama warm")
        handle = RuntimeHandle(
            pid=os.getpid(),
            port=self.port,
            executable_path="ollama-service",
            model_path=f"ollama:{self.artifact_sha256(model_id)}",
            runtime="ollama",
        )
        if not self.ready(handle, model_id):
            raise RuntimeError(f"Ollama did not report {model_id!r} as loaded")
        return handle

    def alive(self, handle: RuntimeHandle) -> bool:
        del handle
        try:
            return self.client.get(f"{self.base_url}/api/tags").status_code == 200
        except httpx.HTTPError:
            return False

    def owns(self, handle: RuntimeHandle, model_id: str) -> bool:
        return (
            handle.runtime == "ollama"
            and handle.port == self.port
            and handle.model_path == f"ollama:{self.artifact_sha256(model_id)}"
        )

    def ready(self, handle: RuntimeHandle, model_id: str) -> bool:
        if not self.alive(handle):
            return False
        payload = self._json("GET", "/api/ps")
        return any(
            isinstance(item, Mapping)
            and str(item.get("name") or item.get("model") or "") == model_id
            for item in payload.get("models", [])
        )

    def stop(self, handle: RuntimeHandle, model_id: str) -> None:
        if not self.owns(handle, model_id):
            raise RuntimeError("refusing to unload an unproven Ollama model")
        response = self.client.post(
            f"{self.base_url}/api/generate",
            json={"model": model_id, "prompt": "", "stream": False, "keep_alive": 0},
        )
        _raise_engine_error(response, "Ollama unload")
        deadline = time.monotonic() + 30.0
        while self.ready(handle, model_id) and time.monotonic() < deadline:
            time.sleep(0.1)
        if self.ready(handle, model_id):
            raise RuntimeError(f"Ollama did not unload {model_id!r} within 30 seconds")

    def close(self) -> None:
        self.client.close()

    def active_requests(self, handle: RuntimeHandle, model_id: str) -> int | None:
        del handle, model_id
        # Ollama exposes loaded models but no active-request counter. The allocator publishes a
        # DRAINING route before this check; a dedicated node has no other ingress.
        return 0

    def _json(self, method: str, path: str) -> dict[str, Any]:
        response = self.client.request(method, f"{self.base_url}{path}")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"Ollama returned malformed JSON from {path}")
        return payload


class ComfyUIBackend:
    """Manage ComfyUI's shared model memory and route readiness by workload bundle."""

    endpoint_scheme = "http"
    bind_host = "127.0.0.1"
    api_key = ""
    endpoint_path = ""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8188",
        *,
        bundles: tuple[str, ...] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.port = urlsplit(self.base_url).port or 8188
        if bundles is None:
            from shared.models.media_bundles import present_bundles

            bundles = tuple(present_bundles())
        self.bundles = tuple(sorted({f"comfyui:{item}" for item in bundles}))
        self.client = httpx.Client(timeout=10.0, trust_env=False)

    def cached_models(self) -> tuple[str, ...]:
        return self.bundles if self._healthy() else ()

    def artifact_sha256(self, model_id: str) -> str:
        if model_id not in self.bundles:
            raise RuntimeError(f"unknown ComfyUI bundle: {model_id!r}")
        return hashlib.sha256(model_id.encode()).hexdigest()

    def fetch_artifact(self, *args: Any, **kwargs: Any) -> str:
        raise RuntimeError("ComfyUI asset downloads require an installed workflow manifest")

    def evict_artifact(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("ComfyUI workflow assets are not automatically deleted")

    def start(self, model_id: str, port: int) -> RuntimeHandle:
        del port
        if model_id not in self.bundles or not self._healthy():
            raise RuntimeError(f"ComfyUI bundle is not ready: {model_id!r}")
        return RuntimeHandle(
            pid=os.getpid(),
            port=self.port,
            executable_path="comfyui-service",
            model_path=model_id,
            runtime="comfyui",
        )

    def alive(self, handle: RuntimeHandle) -> bool:
        del handle
        return self._healthy()

    def owns(self, handle: RuntimeHandle, model_id: str) -> bool:
        return handle.runtime == "comfyui" and handle.port == self.port and model_id in self.bundles

    def ready(self, handle: RuntimeHandle, model_id: str) -> bool:
        return self.owns(handle, model_id) and self._healthy()

    def stop(self, handle: RuntimeHandle, model_id: str) -> None:
        if not self.owns(handle, model_id):
            raise RuntimeError("refusing to unload an unproven ComfyUI bundle")
        response = self.client.post(
            f"{self.base_url}/free",
            json={"unload_models": True, "free_memory": True},
        )
        response.raise_for_status()

    def active_requests(self, handle: RuntimeHandle, model_id: str) -> int | None:
        del handle, model_id
        try:
            payload = self.client.get(f"{self.base_url}/queue").json()
            running = payload.get("queue_running", []) if isinstance(payload, dict) else []
            return len(running) if isinstance(running, list) else None
        except (httpx.HTTPError, ValueError):
            return None

    def close(self) -> None:
        self.client.close()

    def _healthy(self) -> bool:
        try:
            return self.client.get(f"{self.base_url}/system_stats").status_code == 200
        except httpx.HTTPError:
            return False


class VllmBackend:
    """Own one vLLM process per model; immutable HF snapshots are fetched before launch."""

    endpoint_scheme = "http"
    bind_host = "127.0.0.1"
    api_key = ""
    endpoint_path = "/v1"

    def __init__(
        self,
        cache_dir: Path,
        *,
        tensor_parallel_size: int = 1,
        gpu_memory_utilization: float = 0.90,
        max_model_len: int = 0,
        enforce_eager: bool = False,
        readiness_timeout: float = 600.0,
        bind_host: str = "127.0.0.1",
        endpoint_host: str | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_path = self.cache_dir / "artifacts.json"
        self.tensor_parallel_size = max(1, int(tensor_parallel_size))
        if not 0.0 < gpu_memory_utilization <= 1.0:
            raise ValueError("gpu_memory_utilization must be in (0, 1]")
        self.gpu_memory_utilization = float(gpu_memory_utilization)
        if max_model_len < 0:
            raise ValueError("max_model_len must be non-negative")
        self.max_model_len = int(max_model_len)
        self.enforce_eager = bool(enforce_eager)
        self.readiness_timeout = readiness_timeout
        self.bind_host = bind_host
        self.endpoint_host = endpoint_host or bind_host
        self._metadata = jsonio.load_json(self.metadata_path)
        self._spawned: dict[int, subprocess.Popen[bytes]] = {}
        self._lock = threading.RLock()

    def cached_models(self) -> tuple[str, ...]:
        return tuple(sorted(self._metadata))

    def artifact_sha256(self, model_id: str) -> str:
        item = self._metadata.get(model_id)
        if not isinstance(item, Mapping):
            raise RuntimeError(f"vLLM model snapshot is not cached: {model_id!r}")
        return canonical_sha256(item.get("artifact_sha256"), "vLLM snapshot identity")

    def fetch_artifact(
        self, model_id: str, source: str, expected_sha256: str, max_size_mb: int
    ) -> str:
        repo_id, revision = _parse_hf_snapshot_source(source)
        identity = hashlib.sha256(f"hf://{repo_id}@{revision}".encode()).hexdigest()
        if identity != canonical_sha256(expected_sha256):
            raise RuntimeError("vLLM source/revision does not match the pinned snapshot identity")
        from huggingface_hub import HfApi, snapshot_download

        files = HfApi().list_repo_tree(repo_id, revision=revision, recursive=True, expand=True)
        total = sum(int(getattr(item, "size", 0) or 0) for item in files)
        if total > max_size_mb * 1024 * 1024:
            raise RuntimeError("vLLM snapshot exceeds the configured size bound")
        target = self.cache_dir / hashlib.sha256(model_id.encode()).hexdigest()[:20]
        path = snapshot_download(
            repo_id=repo_id,
            revision=revision,
            local_dir=target,
        )
        self._metadata[model_id] = {
            "artifact_sha256": identity,
            "repo_id": repo_id,
            "revision": revision,
            "path": str(Path(path).resolve()),
        }
        jsonio.atomic_write_json(self.metadata_path, self._metadata, mode=0o600)
        return identity

    def evict_artifact(self, model_id: str, expected_sha256: str) -> None:
        if self.artifact_sha256(model_id) != canonical_sha256(expected_sha256):
            raise RuntimeError("refusing to evict a different vLLM snapshot")
        item = self._metadata.pop(model_id)
        target = Path(str(item["path"]))
        if target.parent != self.cache_dir or target.is_symlink():
            raise RuntimeError("refusing to remove a vLLM snapshot outside its managed cache")
        shutil.rmtree(target)
        jsonio.atomic_write_json(self.metadata_path, self._metadata, mode=0o600)

    def start(self, model_id: str, port: int) -> RuntimeHandle:
        item = self._metadata.get(model_id)
        if not isinstance(item, Mapping):
            raise RuntimeError(f"vLLM snapshot is not cached: {model_id!r}")
        binary = shutil.which("vllm")
        if not binary:
            raise RuntimeError("vLLM is not installed on this node")
        model_path = str(Path(str(item["path"])).resolve())
        command = [
            binary,
            "serve",
            model_path,
            "--host",
            self.bind_host,
            "--port",
            str(port),
            "--served-model-name",
            model_id,
            "--tensor-parallel-size",
            str(self.tensor_parallel_size),
            "--gpu-memory-utilization",
            str(self.gpu_memory_utilization),
        ]
        if self.max_model_len:
            command.extend(("--max-model-len", str(self.max_model_len)))
        if self.enforce_eager:
            command.append("--enforce-eager")
        log_path = self.cache_dir / f"{hashlib.sha256(model_id.encode()).hexdigest()[:16]}.log"
        log = log_path.open("ab")
        try:
            process = subprocess.Popen(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log.close()
        with self._lock:
            self._spawned[process.pid] = process
        handle = RuntimeHandle(
            pid=process.pid,
            port=port,
            process_birth_marker=_linux_birth_marker(process.pid),
            executable_path=str(Path(binary).resolve()),
            model_path=model_path,
            runtime="vllm",
        )
        deadline = time.monotonic() + self.readiness_timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"vLLM exited during startup; see {log_path}")
            if self.ready(handle, model_id):
                return handle
            time.sleep(0.5)
        self.stop(handle, model_id)
        raise RuntimeError(f"vLLM readiness timed out; see {log_path}")

    def alive(self, handle: RuntimeHandle) -> bool:
        with self._lock:
            process = self._spawned.get(handle.pid)
        if process is not None:
            return process.poll() is None
        try:
            os.kill(handle.pid, 0)
            return True
        except OSError:
            return False

    def owns(self, handle: RuntimeHandle, model_id: str) -> bool:
        del model_id
        if handle.runtime != "vllm" or not self.alive(handle):
            return False
        if os.name == "posix" and Path(f"/proc/{handle.pid}").exists():
            return (
                _linux_birth_marker(handle.pid) == handle.process_birth_marker
                and _linux_cmdline(handle.pid)
                and handle.model_path in _linux_cmdline(handle.pid)
            )
        with self._lock:
            process = self._spawned.get(handle.pid)
        return process is not None and process.poll() is None

    def ready(self, handle: RuntimeHandle, model_id: str) -> bool:
        if not self.owns(handle, model_id):
            return False
        try:
            response = httpx.get(f"http://127.0.0.1:{handle.port}/v1/models", timeout=1.0)
            response.raise_for_status()
            payload = response.json()
            return any(item.get("id") == model_id for item in payload.get("data", []))
        except (httpx.HTTPError, ValueError, AttributeError):
            return False

    def stop(self, handle: RuntimeHandle, model_id: str) -> None:
        if not self.owns(handle, model_id):
            raise RuntimeError("refusing to stop an unproven vLLM process")
        os.kill(handle.pid, signal.SIGTERM)
        deadline = time.monotonic() + 30.0
        while self.alive(handle) and time.monotonic() < deadline:
            time.sleep(0.1)
        if self.alive(handle):
            raise RuntimeError("vLLM did not drain and stop within 30 seconds")
        with self._lock:
            process = self._spawned.pop(handle.pid, None)
        if process is not None:
            process.wait(timeout=1.0)

    def active_requests(self, handle: RuntimeHandle, model_id: str) -> int | None:
        del model_id
        try:
            text = httpx.get(f"http://127.0.0.1:{handle.port}/metrics", timeout=1.0).text
        except httpx.HTTPError:
            return None
        total = 0.0
        found = False
        for line in text.splitlines():
            if line.startswith("vllm:num_requests_running"):
                try:
                    total += float(line.rsplit(" ", 1)[-1])
                    found = True
                except ValueError:
                    return None
        return int(total) if found and total.is_integer() else None


def build_engine_orchestrator(
    *,
    state_path: Path,
    llama_backend: LlamaCppBackend,
    dedicated: bool,
    gpu_count: int = 1,
    local_proxy: bool = False,
) -> EngineOrchestratorBackend:
    """Build the installed runtime set; dedicated mode grants shared-engine lifecycle authority."""

    adapters: dict[str, Any] = {"llama.cpp": llama_backend}
    if dedicated and local_proxy:
        ollama = OllamaBackend()
        if _safe_cached(ollama):
            adapters["ollama"] = ollama
        comfyui = ComfyUIBackend()
        if _safe_cached(comfyui):
            adapters["comfyui"] = comfyui
        if shutil.which("vllm"):
            adapters["vllm"] = VllmBackend(
                state_path.parent / "vllm",
                tensor_parallel_size=max(1, gpu_count),
            )
    return EngineOrchestratorBackend(adapters)


def _safe_cached(adapter: Any) -> tuple[str, ...]:
    try:
        return tuple(adapter.cached_models())
    except Exception:
        return ()


def _ollama_digest(value: Any) -> str:
    digest = str(value or "")
    if digest.startswith("sha256:"):
        digest = digest.removeprefix("sha256:")
    return canonical_sha256(digest, "Ollama model digest")


def _raise_engine_error(response: httpx.Response, operation: str) -> None:
    """Preserve a native engine's bounded error text instead of hiding it behind status only."""

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        try:
            payload = response.json()
            detail = str(payload.get("error") or payload.get("detail") or "")
        except (ValueError, AttributeError):
            detail = response.text[:500]
        suffix = f": {detail[:500]}" if detail else ""
        raise RuntimeError(f"{operation} failed with HTTP {response.status_code}{suffix}") from exc


def _handle_with_runtime(handle: RuntimeHandle, runtime: str) -> RuntimeHandle:
    if handle.runtime == runtime:
        return handle
    return RuntimeHandle(
        handle.pid,
        handle.port,
        handle.process_birth_marker,
        handle.executable_path,
        handle.model_path,
        runtime,
    )


def _parse_hf_snapshot_source(source: str) -> tuple[str, str]:
    parsed = urlsplit(source)
    if parsed.scheme != "hf" or not parsed.netloc:
        raise RuntimeError("vLLM artifacts require hf://owner/repo@revision sources")
    value = f"{parsed.netloc}{parsed.path}"
    repo_id, separator, revision = value.rpartition("@")
    if not separator or repo_id.count("/") != 1 or not revision:
        raise RuntimeError("vLLM artifacts require hf://owner/repo@revision sources")
    if len(revision) != 40 or any(
        character not in "0123456789abcdefABCDEF" for character in revision
    ):
        raise RuntimeError("vLLM revision must be a full immutable 40-hex commit")
    return repo_id, revision.lower()


def _linux_birth_marker(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[21]
    except (OSError, IndexError):
        return ""


def _linux_cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
            errors="replace"
        )
    except OSError:
        return ""
