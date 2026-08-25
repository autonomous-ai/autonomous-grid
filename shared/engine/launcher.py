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
from shared.models import gguf


MIN_LLAMA_SERVER_BUILD = 9240


@dataclass
class LlamaProcess:
    proc: subprocess.Popen
    port: int
    log: Path


@dataclass(frozen=True)
class RuntimeProfile:
    """Launch policy we actually mean to impose. Deliberately carries NO ctx_size and no
    flash_attn: llama.cpp's own `--fit` sizes the context to the machine, and `--flash-attn auto`
    probes the backend. Both are on by default in llama-server, and both are disabled *for the
    dimension we pin* — `fit` "only modifies parameters that still hold their default value", so
    every value we pass is a value we take responsibility for."""

    n_predict: int
    temp: float
    reasoning_budget: int
    parallel: int = 1
    # `--n-gpu-layers all` on unified memory. When llama.cpp cannot fit a model it chooses between
    # two levers: shrink the context, or spill layers to host memory — and it picks the spill,
    # because `fit` assumes system memory is unlimited. On Apple Silicon the "host memory" it spills
    # into is the same physical pool it just left, so the spill buys nothing and swaps the whole OS.
    # Pinning the layers takes that lever away and forces the context lever instead. Left unset on
    # discrete-VRAM hosts, where spilling to real system RAM is a legitimate partial offload.
    gpu_layers: str | None = None
    min_p: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    presence_penalty: float | None = None
    spec_draft_n_max: int = 6


APPLE_SILICON_RUNTIME = RuntimeProfile(
    n_predict=64000,
    temp=1.0,
    min_p=0.0,
    top_p=0.95,
    top_k=20,
    presence_penalty=1.5,
    reasoning_budget=0,
    gpu_layers="all",
    spec_draft_n_max=2,
)
NVIDIA_RUNTIME = RuntimeProfile(
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


# A raw `connect_ex` with no `settimeout` inherits the OS TCP connect timeout — minutes against a
# filtered address, and unbounded as far as this code is concerned. This check is the FIRST thing the
# remote serve child's engine bring-up runs, where a stall is invisible (ADR 0022). `localhost` is
# expected to answer or refuse immediately; `local/media_runtime` has always bounded the identical
# check, so this is the sibling catching up rather than a new policy.
_PORT_CHECK_TIMEOUT = 1.0


def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(_PORT_CHECK_TIMEOUT)
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
    mmproj: str | None = None,
) -> LlamaProcess:
    paths.ensure_all()
    profile = runtime_profile()
    # No profile default for ctx_size or flash_attn — see RuntimeProfile. `None` here means "we did
    # not ask", which is what leaves llama.cpp free to decide; anything else is an operator override
    # arriving from `grid join --ctx-size / --flash-attn`, and is passed through untouched.
    n_predict = profile.n_predict if n_predict is None else n_predict
    parallel = profile.parallel if parallel is None else parallel
    temp = profile.temp if temp is None else temp
    reasoning_budget = profile.reasoning_budget if reasoning_budget is None else reasoning_budget
    if ctx_size == 0:
        # `-c 0` is NOT the same as omitting `-c`. llama.cpp reads an explicit 0 as "give me the
        # full trained window and do not reduce it", setting the fitter's floor to UINT32_MAX — so
        # a model that does not fit spills its weights to system memory instead of shrinking. That
        # is a slow, swap-heavy mode that looks like a typo for "unset". Make the user say it twice.
        raise SystemExit(
            "--ctx-size 0 asks llama.cpp for the model's full trained context and turns off the "
            "automatic fit-to-memory, which will spill weights into system RAM. Omit --ctx-size to "
            "let the engine size itself to this machine, or pass a real token count."
        )

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
    # Vision turns itself on. A vision GGUF is text-only until its projector is loaded, and the
    # projector is a SECOND file — so whether this model can see is a question about what is on
    # this disk, not about what the operator remembered to type. `grid pull` saves a repo's
    # projector as `<model-stem>.mmproj.gguf`, and that is what gets picked up here.
    # `--mmproj` stays as an override for a projector fetched by hand or named something else.
    if mmproj:
        mmproj_path = paths.models_dir() / mmproj
        if not mmproj_path.is_file():
            raise SystemExit(
                f"Multimodal projector not found: {mmproj_path}. Pull it next to the model, or "
                "drop the projector argument to serve this model as text-only."
            )
        cmd.extend(["--mmproj", str(mmproj_path)])
    else:
        discovered = gguf.projector_beside(model_path)
        if discovered:
            print(f"Vision: serving with projector {discovered.name}")
            cmd.extend(["--mmproj", str(discovered)])
    cmd.extend(
        [
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
            "--n-predict",
            str(n_predict),
            "--temp",
            str(temp),
        ]
    )
    # Emitted ONLY when the operator asked for one. Left unset, llama.cpp measures free device
    # memory at load and takes the largest window that fits; naming a number here turns that off,
    # which is the whole reason the old hardcoded 128000 broke small machines.
    if ctx_size is not None:
        cmd.extend(["--ctx-size", str(ctx_size)])
    if profile.gpu_layers is not None:
        cmd.extend(["--n-gpu-layers", profile.gpu_layers])
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
            "--parallel",
            str(parallel),
            # Redundant at the pinned build (context shift has defaulted to disabled since b6427),
            # but kept: it is one argv entry, and it pins a default llama.cpp has already flipped
            # once. Shifting silently discards the head of a conversation, so we never want it back.
            "--no-context-shift",
        ]
    )
    # Unset means `--flash-attn auto`, which builds a probe graph and falls back when the backend
    # cannot place the fused kernel. The old hardcoded `on` asserted instead of probing, so on a
    # backend without FA support the op was scheduled off-device layer by layer with no warning.
    if flash_attn is not None:
        cmd.extend(["--flash-attn", str(flash_attn)])
    # Filename alone can't tell: unsloth ships a `Qwen3.6-35B-A3B-Q8_0.gguf` in both an
    # MTP repo and a plain one, byte-identical name, only one with the fused draft head.
    # Read the file's own header instead of trusting the name.
    if gguf.has_mtp_head(model_path):
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
