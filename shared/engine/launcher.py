"""Spawn and supervise local llama-server processes."""

from __future__ import annotations

import ipaddress
import os
import platform
import shutil
import socket
import ssl
import subprocess
import time
from collections.abc import Callable
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
    host: str = "0.0.0.0"
    scheme: str = "http"
    probe_host: str = ""
    tls_ca_file: str = ""


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
# A box with no GPU backend at all. Both caps below are the SAME settings as the GPU profiles
# measured in wall-clock rather than tokens: a CPU build of this engine generates at roughly
# 20 tok/s (measured on an Intel Mac, `Qwen3.5-2B-Q4_K_M`), where the GPU numbers become absurd —
# `reasoning_budget=8192` is seven minutes of thinking before the answer starts, and
# `n_predict=64000` lets one model that fails to stop run for the better part of an hour. There is
# no third lever: the operator cannot make the box faster, so the budget is what has to give.
CPU_RUNTIME = RuntimeProfile(
    n_predict=4096,       # ~3 min at 20 tok/s; far above a real answer, far below a runaway
    temp=0.7,
    reasoning_budget=0,   # thinking is a GPU luxury; here it is dead time before the first word
    spec_draft_n_max=1,
)

# Every GPU backend llama.cpp ships as a separate `libggml-<name>` next to the binary. Their
# ABSENCE is what makes a build CPU-only, whatever hardware the box happens to contain.
_GPU_BACKENDS = ("metal", "cuda", "vulkan", "hip", "sycl", "opencl", "musa", "cann")


def is_apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() in ("arm64", "aarch64")


def has_gpu_backend() -> bool:
    """Whether the INSTALLED llama.cpp build can reach a GPU at all.

    Asks the build, not the box. A machine can hold a perfectly good GPU that this engine cannot
    use — an Intel Mac with a Radeon, running the CPU tarball, ships only `libggml-cpu`,
    `libggml-blas` and `libggml-rpc`, and every token comes off the CPU no matter what
    `grid device-info` reports. Guessing from hardware is what put such a box on the NVIDIA
    profile; reading the backends it actually loaded cannot.

    Unknown (no install directory, unreadable) is treated as GPU-present: the conservative error
    is to leave the generous GPU budgets alone, never to silently cap a real GPU node at 4096.
    """
    # `.resolve()`: the pinned path is a SYMLINK into the install directory (`~/.grid/bin/
    # llama-server` -> `~/.grid/engines/llama.cpp/llama-server`), and the backends sit beside the
    # real file, not beside the link. Reading the link's own parent finds an empty `bin/`, which
    # this function would then report as "layout not recognised" — i.e. as a GPU build.
    engine_dir = paths.llama_server_bin().resolve().parent
    try:
        names = [entry.name.lower() for entry in engine_dir.iterdir()]
    except OSError:
        return True
    if not any(name.startswith("libggml-cpu") for name in names):
        return True  # not a layout we recognise — do not infer anything from it
    return any(
        name.startswith(f"libggml-{backend}") for name in names for backend in _GPU_BACKENDS
    )


def runtime_profile() -> RuntimeProfile:
    if is_apple_silicon():
        return APPLE_SILICON_RUNTIME
    # Before NVIDIA, because that profile's name is a claim about the hardware and this is the
    # check that can falsify it: everything-not-Apple used to mean "assume a discrete GPU".
    if not has_gpu_backend():
        return CPU_RUNTIME
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
    raise SystemExit(
        "llama-server not found. Run `grid engine install llama.cpp` first."
    )


# A raw `connect_ex` with no `settimeout` inherits the OS TCP connect timeout — minutes against a
# filtered address, and unbounded as far as this code is concerned. This check is the FIRST thing the
# remote serve child's engine bring-up runs, where a stall is invisible (ADR 0022). `localhost` is
# expected to answer or refuse immediately; `local/media_runtime` has always bounded the identical
# check, so this is the sibling catching up rather than a new policy.
_PORT_CHECK_TIMEOUT = 1.0


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Probe a listener through the address family used by its bind host."""

    value = str(host).strip().strip("[]")
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError(
            "port probe host must be a valid IPv4 or IPv6 address"
        ) from exc
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    if address.is_unspecified:
        value = "::1" if address.version == 6 else "127.0.0.1"
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(_PORT_CHECK_TIMEOUT)
        return sock.connect_ex((value, port)) == 0


def start_llm(
    model_file: str,
    *,
    port: int,
    host: str = "0.0.0.0",
    ctx_size: int | None = None,
    n_predict: int | None = None,
    parallel: int | None = None,
    flash_attn: str | None = None,
    temp: float | None = None,
    reasoning_budget: int | None = None,
    alias: str | None = None,
    mmproj: str | None = None,
    api_key_file: str | os.PathLike[str] | None = None,
    tls_cert_file: str | os.PathLike[str] | None = None,
    tls_key_file: str | os.PathLike[str] | None = None,
    tls_ca_file: str | os.PathLike[str] | None = None,
    probe_host: str | None = None,
    on_spawn: Callable[[LlamaProcess], None] | None = None,
) -> LlamaProcess:
    host = str(host).strip()
    if not host:
        raise ValueError("llama-server bind host must not be empty")
    api_key_path = _readable_file(
        api_key_file,
        "llama-server API key file",
        owner_only=True,
    )
    cert_path = _readable_file(tls_cert_file, "llama-server TLS certificate")
    key_path = _readable_file(
        tls_key_file,
        "llama-server TLS private key",
        owner_only=True,
    )
    ca_path = _readable_file(tls_ca_file, "llama-server TLS CA file")
    if bool(cert_path) != bool(key_path):
        raise ValueError("llama-server TLS certificate and private key must be provided together")
    if ca_path and not cert_path:
        raise ValueError("llama-server TLS CA file requires TLS certificate and private key")
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
            # `grid models pull` is not a command — `grid models` lists what the grid serves and
            # takes no subcommand. Naming a command that does not exist sends the reader looking
            # for it, which is worse than the missing file they came here for.
            f"No model file at {model_path}.\n"
            f"  Download it:   grid pull {Path(model_file).stem}\n"
            "  Or see what is already here:   grid catalog"
        )

    log = paths.llama_log(port)
    log.parent.mkdir(parents=True, exist_ok=True)
    log_fh = logging_setup.cap_and_open_append(
        log, logging_setup.engine_log_max_bytes(), text=True, buffering=1
    )
    log_fh.write(
        f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} grid starting llm on :{port} ===\n"
    )

    cmd = [llama_server_path(), "-m", str(model_path)]
    if alias:
        cmd.extend(["--alias", alias])
    if api_key_path:
        # The key itself must never appear in argv or the inherited environment. llama.cpp reads
        # one key per line from this owner-only file instead.
        cmd.extend(["--api-key-file", api_key_path])
    if cert_path:
        cmd.extend(["--ssl-cert-file", cert_path, "--ssl-key-file", key_path])
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
            host,
            "--port",
            str(port),
            # Keep llama.cpp's loopback slot-introspection endpoint enabled. The allocator uses
            # GET /slots to observe requests that bypass Grid's central proxy before unloading a
            # model process.
            "--slots",
            "--jinja",
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
    launched = LlamaProcess(
        proc=proc,
        port=port,
        log=log,
        host=host,
        scheme="https" if cert_path else "http",
        probe_host=str(probe_host or ""),
        tls_ca_file=ca_path,
    )
    try:
        # This hook deliberately runs in the first instructions after Popen. Allocator callers use
        # it to durably record the PID and port before doing OS identity probes or readiness work.
        # If durable publication fails, the new child must not escape as an untracked process.
        if on_spawn is not None:
            on_spawn(launched)
    except BaseException:
        stop(launched)
        raise
    finally:
        # The child inherited/duplicated the descriptor passed to Popen; the parent does not need
        # to retain a second handle for the lifetime of the server.
        log_fh.close()
    return launched


def wait_for_models(
    proc: LlamaProcess,
    timeout: float = 120.0,
    *,
    api_key: str | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    bind_host = str(getattr(proc, "host", "0.0.0.0"))
    probe_host = str(getattr(proc, "probe_host", "") or "")
    if not probe_host:
        try:
            probe_host = (
                "[::1]"
                if ipaddress.ip_address(bind_host).version == 6
                else "127.0.0.1"
            )
        except ValueError:
            probe_host = "localhost"
    scheme = str(getattr(proc, "scheme", "http") or "http")
    ca_file = str(getattr(proc, "tls_ca_file", "") or "")
    verify: ssl.SSLContext | bool = (
        ssl.create_default_context(cafile=ca_file) if ca_file else True
    )
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    while time.monotonic() < deadline:
        rc = proc.proc.poll()
        if rc is not None:
            raise SystemExit(
                f"llama-server on port {proc.port} exited (rc={rc}) before becoming ready. "
                f"Last lines of {proc.log}:\n{_log_tail(proc.log)}"
            )
        try:
            with httpx.Client(timeout=5.0, trust_env=False, verify=verify) as client:
                resp = client.get(
                    f"{scheme}://{_url_host(probe_host)}:{proc.port}/v1/models",
                    headers=headers,
                )
            if resp.status_code == 200:
                return
        except httpx.RequestError as exc:
            last_exc = exc
        time.sleep(1.0)
    message = f"llama-server did not become ready on port {proc.port} within {timeout}s"
    if last_exc:
        message += f" (last error: {last_exc})"
    raise SystemExit(message)


def _readable_file(
    value: str | os.PathLike[str] | None,
    label: str,
    *,
    owner_only: bool = False,
) -> str:
    if value is None:
        return ""
    raw = os.fspath(value)
    if not raw or any(character in "\r\n\0" for character in raw):
        raise ValueError(f"{label} path must be non-empty and single-line")
    path = Path(raw).expanduser().resolve()
    if not path.is_file() or not os.access(path, os.R_OK):
        raise ValueError(f"{label} is not a readable regular file: {path}")
    if owner_only and os.name != "nt":
        metadata = path.stat()
        if metadata.st_mode & 0o077:
            raise ValueError(f"{label} must be owner-only (chmod 600): {path}")
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise ValueError(f"{label} must be owned by the current user: {path}")
    return str(path)


def _url_host(value: str) -> str:
    """Return a URL authority host without changing DNS names or scoped IPv6."""

    host = str(value).strip()
    if host.startswith("[") and host.endswith("]"):
        return host
    decoded_host = host.replace("%25", "%")
    candidate = decoded_host.split("%", 1)[0]
    try:
        if ipaddress.ip_address(candidate).version == 6:
            return f"[{decoded_host.replace('%', '%25')}]"
    except ValueError:
        pass
    return host


def stop(
    proc: LlamaProcess,
    *,
    timeout: float = 10.0,
    kill_timeout: float = 5.0,
) -> None:
    if proc.proc.poll() is not None:
        return
    try:
        proc.proc.terminate()
    except OSError:
        # The child can race with the signal. Recheck through kill/wait below instead of masking a
        # durability error raised by an immediate post-spawn callback.
        pass
    else:
        try:
            proc.proc.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            pass
    try:
        proc.proc.kill()
    except OSError as exc:
        if proc.proc.poll() is None:
            raise RuntimeError(
                f"llama-server on port {proc.port} survived stop: {exc}"
            ) from exc
        return
    try:
        proc.proc.wait(timeout=kill_timeout)
    except subprocess.TimeoutExpired as exc:
        if proc.proc.poll() is None:
            raise RuntimeError(
                f"llama-server on port {proc.port} survived forced stop"
            ) from exc
    if proc.proc.poll() is None:
        raise RuntimeError(f"llama-server on port {proc.port} did not exit")


def parse_version(timeout: float = 5.0) -> int | None:
    try:
        out = subprocess.run(
            [llama_server_path(), "--version"],
            capture_output=True,
            check=False,
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
