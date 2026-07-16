"""Spawn and supervise local llama-server processes."""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from shared import logging_setup, paths


MIN_LLAMA_SERVER_BUILD = 9240


@dataclass
class LlamaProcess:
    proc: subprocess.Popen
    port: int
    log: Path


@dataclass(frozen=True)
class RuntimeProfile:
    ctx_size: int
    n_predict: int
    temp: float
    reasoning_budget: int
    flash_attn: str = "on"
    parallel: int = 1
    min_p: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    presence_penalty: float | None = None
    spec_draft_n_max: int = 6


APPLE_SILICON_RUNTIME = RuntimeProfile(
    ctx_size=128000,
    n_predict=64000,
    temp=1.0,
    min_p=0.0,
    top_p=0.95,
    top_k=20,
    presence_penalty=1.5,
    reasoning_budget=0,
    spec_draft_n_max=2,
)
NVIDIA_RUNTIME = RuntimeProfile(
    ctx_size=128000,
    n_predict=64000,
    temp=0.7,
    reasoning_budget=8192,
    spec_draft_n_max=1,
)


def is_apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() in ("arm64", "aarch64")


def runtime_profile() -> RuntimeProfile:
    if is_apple_silicon():
        return APPLE_SILICON_RUNTIME
    return NVIDIA_RUNTIME


def llama_server_path() -> str:
    override = os.environ.get("LLAMA_SERVER")
    if override:
        expanded = os.path.expanduser(override)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
        raise SystemExit(f"LLAMA_SERVER is set but not an executable file: {override}")
    pinned = paths.llama_server_bin()
    if pinned.is_file():
        return str(pinned)
    on_path = shutil.which("llama-server")
    if on_path:
        return on_path
    raise SystemExit("llama-server not found. Run `grid engine install llama.cpp` first.")


def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("localhost", port)) == 0


def start_llm(
    model_file: str,
    *,
    port: int,
    ctx_size: int | None = None,
    n_predict: int | None = None,
    parallel: int | None = None,
    flash_attn: str | None = None,
    temp: float | None = None,
    reasoning_budget: int | None = None,
    alias: str | None = None,
    mmproj: str = "mmproj-BF16.gguf",
) -> LlamaProcess:
    paths.ensure_all()
    profile = runtime_profile()
    ctx_size = profile.ctx_size if ctx_size is None else ctx_size
    n_predict = profile.n_predict if n_predict is None else n_predict
    parallel = profile.parallel if parallel is None else parallel
    flash_attn = profile.flash_attn if flash_attn is None else flash_attn
    temp = profile.temp if temp is None else temp
    reasoning_budget = profile.reasoning_budget if reasoning_budget is None else reasoning_budget

    model_path = paths.models_dir() / Path(model_file).name
    if not model_path.is_file():
        raise SystemExit(
            f"Model file not found: {model_path}. Use `grid models pull` first."
        )

    log = paths.llama_log(port)
    log.parent.mkdir(parents=True, exist_ok=True)
    log_fh = logging_setup.cap_and_open_append(
        log, logging_setup.engine_log_max_bytes(), text=True, buffering=1
    )
    log_fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} grid starting llm on :{port} ===\n")

    cmd = [llama_server_path(), "-m", str(model_path)]
    if alias:
        cmd.extend(["--alias", alias])
    if mmproj:
        mmproj_path = paths.models_dir() / mmproj
        if mmproj_path.is_file():
            cmd.extend(["--mmproj", str(mmproj_path)])
        else:
            print(
                f"warning: --mmproj {mmproj} not found at {mmproj_path}; "
                "starting text-only"
            )
    cmd.extend(
        [
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
            "--jinja",
            "--ctx-size",
            str(ctx_size),
            "--n-predict",
            str(n_predict),
            "--temp",
            str(temp),
        ]
    )
    if profile.min_p is not None:
        cmd.extend(["--min-p", str(profile.min_p)])
    if profile.top_p is not None:
        cmd.extend(["--top-p", str(profile.top_p)])
    if profile.top_k is not None:
        cmd.extend(["--top-k", str(profile.top_k)])
    if profile.presence_penalty is not None:
        cmd.extend(["--presence-penalty", str(profile.presence_penalty)])
    cmd.extend(
        [
            "--reasoning-budget",
            str(reasoning_budget),
            "--flash-attn",
            str(flash_attn),
            "--parallel",
            str(parallel),
            "--no-context-shift",
        ]
    )
    if Path(model_file).name.lower().startswith("qwen3.6"):
        cmd.extend(["--spec-type", "draft-mtp", "--spec-draft-n-max", str(profile.spec_draft_n_max)])

    proc = subprocess.Popen(cmd, stdout=log_fh, stderr=log_fh)
    return LlamaProcess(proc=proc, port=port, log=log)


def wait_for_models(proc: LlamaProcess, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        rc = proc.proc.poll()
        if rc is not None:
            raise SystemExit(
                f"llama-server on port {proc.port} exited (rc={rc}) before becoming ready. "
                f"Last lines of {proc.log}:\n{_log_tail(proc.log)}"
            )
        try:
            resp = httpx.get(f"http://localhost:{proc.port}/v1/models", timeout=5.0)
            if resp.status_code == 200:
                return
        except httpx.RequestError as exc:
            last_exc = exc
        time.sleep(1.0)
    message = f"llama-server did not become ready on port {proc.port} within {timeout}s"
    if last_exc:
        message += f" (last error: {last_exc})"
    raise SystemExit(message)


def stop(proc: LlamaProcess, *, timeout: float = 10.0) -> None:
    if proc.proc.poll() is not None:
        return
    proc.proc.terminate()
    try:
        proc.proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.proc.kill()
        try:
            proc.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def parse_version(timeout: float = 5.0) -> int | None:
    try:
        out = subprocess.run(
            [llama_server_path(), "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (SystemExit, subprocess.SubprocessError, OSError):
        return None
    blob = (out.stdout or "") + (out.stderr or "")
    for line in blob.splitlines():
        stripped = line.strip()
        if "version:" not in stripped:
            continue
        try:
            return int(stripped.split("version:", 1)[1].strip().split()[0])
        except (ValueError, IndexError):
            continue
    return None


def assert_supported_build() -> None:
    build = parse_version()
    if build is not None and build > 1 and build < MIN_LLAMA_SERVER_BUILD:
        raise SystemExit(
            f"llama-server build {build} is too old; need >= {MIN_LLAMA_SERVER_BUILD}. "
            "Run `grid engine install llama.cpp`."
        )


def _log_tail(path: Path, lines: int = 30) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return "(log unavailable)"
    text = data.decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:])
