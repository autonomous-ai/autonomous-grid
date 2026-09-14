"""The mlx-omarchy engine: MLX on the Apple GPU under Linux, served through ``mlx_lm.server``.

The third built-in engine, beside llama.cpp (text) and ComfyUI (media), and the only one with a
hardware gate: it runs on exactly one kind of machine — an M-series Mac booted into Linux
(Omarchy M), where ``shared.system.apple_linux`` says so and Asahi's Vulkan driver is in place.
Everywhere else ``install`` refuses with the reason, because the wheel exists for nothing else
(``cp314-linux_aarch64``, and it dlopens Honeykrisp at import).

**Why a second text engine on a machine llama.cpp already serves.** llama.cpp's Vulkan build runs
the same GPU, and today it is not slower — mlx-omarchy's own receipts put it at 69–71 % of native
Metal decode on an M1, with llama.cpp Vulkan as the parity bar. What llama.cpp cannot do is run a
model in MLX format (``mlx-community/*``) or, later, reach the Neural Engine, which is what the
project is working toward and what its author asked Omagrid to be ready for. This engine is that
readiness: a person on Omarchy M types ``--engine mlx-omarchy`` and the same join, leave and
relay loop carry it. Nothing here makes it the default.

**Grid owns the install, not the upstream ``install.sh``.** That script is the right thing for a
person at a terminal and the wrong thing for a CLI to run: it writes into ``~/.local``, registers a
launcher entry with the Omarchy shell, and calls ``sudo`` for the Neural Engine's tmpfiles. So the
same steps are done here under ``~/.grid/engines/mlx-omarchy`` — the pattern ``comfyui.py`` set.

**The release is resolved at install time, not pinned in this file.** Unlike llama.cpp
(``installer.LLAMA_RELEASE`` plus six digests, bumped by hand), this engine tracks upstream's
LATEST release by decision: the project moves fast, its author is the one testing on the hardware,
and an Omagrid user should get what he shipped last, not what this repo last had time to copy in.
:func:`resolve_release` asks GitHub for the latest tag, reads that release's ``SHA256SUMS`` for the
``cp314-linux_aarch64`` wheel, and the download is checked against that digest. ⚠️ **That is
weaker than a digest committed here** — the sums file travels the same TLS connection as the wheel
it vouches for — and it is the same trust the upstream installer extends. ``--version vX.Y.Z``
pins one release for a machine that needs to; :data:`FALLBACK_RELEASE` is used only when GitHub
cannot be asked at all, so an offline box (or a rate-limited one) still installs something known.

The ``mlx-lm`` / ``transformers`` pins are read from THAT release's ``install.sh`` for the same
reason — they are what its author tested with — and fall back to :data:`MLX_LM` /
:data:`MLX_LM_DEPS` when the script cannot be fetched or has changed shape.

⚠️ **``mlx-lm`` is installed ``--no-deps`` on purpose.** It declares a dependency on upstream ``mlx``,
which ships the same ``mlx`` module and would silently replace the Vulkan build with one that fails
at import (no Metal). Its real runtime dependencies are listed explicitly instead, the exact set the
upstream installer pins for the release being installed (see :func:`_pins_from_installer`).

⚠️ **The advertised name is never the name the server is asked for.** ``mlx_lm.server`` treats an
unknown ``model`` in a request as a Hugging Face repo to download — so an ``--advertise-as`` alias
reaching it would start a fetch of a repo called ``qwen7b`` and fail. :func:`start` therefore
returns the repo id as the engine's *upstream* name, and the serve loop rewrites the alias back to
it before forwarding, the same rewrite it already does for an external Ollama.
"""

from __future__ import annotations

import ctypes.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from shared import logging_setup, paths
from shared.engine import installer
from shared.engine.launcher import LlamaProcess
from shared.system import apple_linux

ENGINE = "mlx-omarchy"

_REPO = "joshuaswarren/mlx-omarchy"
_RELEASES = f"https://github.com/{_REPO}/releases/download"
_LATEST_API = f"https://api.github.com/repos/{_REPO}/releases/latest"
_RAW = f"https://raw.githubusercontent.com/{_REPO}"

#: Used ONLY when GitHub cannot say what the latest release is (offline, rate-limited). The digest
#: still comes from that release's own ``SHA256SUMS``, so this is a tag, not a trust anchor.
FALLBACK_RELEASE = "v0.4.2"

#: The one wheel this machine can run, as it is named in every release's ``SHA256SUMS``.
_WHEEL_TAG = "cp314-cp314-linux_aarch64.whl"

#: The wheel is built for one interpreter. There is no cp313/cp312 build to fall back to.
PYTHON = "3.14"

#: ``mlx-lm`` and the dependencies it would otherwise pull in. The two versions are the FALLBACK:
#: :func:`resolve_release` reads the release's own ``MLX_LM_VERSION`` / ``TRANSFORMERS_VERSION``
#: from its ``install.sh`` and these are what v0.4.2 pinned, used when that read fails.
MLX_LM = "mlx-lm==0.31.3"
MLX_LM_DEPS = (
    "transformers[sentencepiece]==5.16.1",
    "numpy",
    "protobuf",
    "pyyaml",
    "jinja2",
    "huggingface_hub",
)

#: System libraries the wheel's BLAS calls resolve against at import. Arch: ``lapack blas openblas``.
_SYSTEM_LIBS = ("openblas", "lapack", "blas")
_SYSTEM_LIBS_HINT = "sudo pacman -S --needed lapack blas openblas"

#: How long to wait for the server. Longer than llama-server's 120 s because the first start of a
#: model DOWNLOADS it — ``mlx_lm.server`` fetches from Hugging Face on demand — and a 7B 4-bit
#: model is 4 GB. The poll also watches the process, so a crash still surfaces immediately.
READY_TIMEOUT = 1800.0


@dataclass(frozen=True)
class Release:
    """One upstream release, resolved: the tag, the wheel this machine runs, its digest, and the
    ``mlx-lm`` / ``transformers`` pins its installer used."""

    tag: str
    wheel: str
    sha256: str
    mlx_lm: str = MLX_LM
    deps: tuple[str, ...] = MLX_LM_DEPS

    @property
    def wheel_url(self) -> str:
        return f"{_RELEASES}/{self.tag}/{self.wheel}"


def _get_text(url: str, timeout: float = 20.0) -> str | None:
    """One GET, or ``None`` on any failure — the callers all have a fallback and none of them
    should turn a flaky network into a traceback in the middle of an install."""
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True,
                         headers={"Accept": "application/vnd.github+json"})
    except httpx.HTTPError:
        return None
    return resp.text if resp.status_code == 200 else None


def _latest_tag() -> str | None:
    body = _get_text(_LATEST_API)
    if not body:
        return None
    try:
        tag = json.loads(body).get("tag_name")
    except (json.JSONDecodeError, AttributeError):
        return None
    return tag if isinstance(tag, str) and tag else None


def _pins_from_installer(tag: str) -> tuple[str, tuple[str, ...]]:
    """The ``mlx-lm`` and ``transformers`` versions the release's own ``install.sh`` pins — read
    as two assignments, never executed. Falls back to the module defaults when either is absent."""
    script = _get_text(f"{_RAW}/{tag}/install.sh")
    if not script:
        return MLX_LM, MLX_LM_DEPS
    mlx_lm = re.search(r"^MLX_LM_VERSION=([0-9][\w.+-]*)\s*$", script, re.M)
    transformers = re.search(r"^TRANSFORMERS_VERSION=([0-9][\w.+-]*)\s*$", script, re.M)
    if not (mlx_lm and transformers):
        return MLX_LM, MLX_LM_DEPS
    deps = (f"transformers[sentencepiece]=={transformers.group(1)}",) + MLX_LM_DEPS[1:]
    return f"mlx-lm=={mlx_lm.group(1)}", deps


def resolve_release(version: str | None = None) -> Release:
    """Which release to install: ``version`` when given, else upstream's latest, else the fallback.

    The wheel's name and digest always come from the chosen release's ``SHA256SUMS`` — there is no
    other place a filename that embeds a build timestamp and commit could come from.
    """
    tag = version
    if tag is None:
        tag = _latest_tag()
        if tag is None:
            print(f"Could not ask GitHub for the latest {ENGINE} release; using {FALLBACK_RELEASE}.")
            tag = FALLBACK_RELEASE
    sums = _get_text(f"{_RELEASES}/{tag}/SHA256SUMS")
    if not sums:
        raise SystemExit(
            f"Could not fetch SHA256SUMS for {ENGINE} {tag} — is that a published release? "
            f"See https://github.com/{_REPO}/releases"
        )
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].endswith(_WHEEL_TAG) and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            mlx_lm, deps = _pins_from_installer(tag)
            return Release(tag=tag, wheel=parts[1], sha256=parts[0], mlx_lm=mlx_lm, deps=deps)
    raise SystemExit(f"{ENGINE} {tag} publishes no {_WHEEL_TAG} wheel; nothing this machine can run.")


def engine_dir() -> Path:
    return paths.grid_home() / "engines" / ENGINE


def venv_dir() -> Path:
    return engine_dir() / ".venv"


def python_bin() -> Path:
    return venv_dir() / "bin" / "python"


def version_file() -> Path:
    """The tag and wheel this install came from, so a re-run can tell "already latest" from
    "there is a newer one" without reimporting the venv."""
    return engine_dir() / "VERSION"


def installed_version() -> str | None:
    try:
        return version_file().read_text().split()[0]
    except (OSError, IndexError):
        return None


def is_installed() -> bool:
    return python_bin().is_file()


#: Escapes both hardware gates below — same shape as upstream's own ``MLX_OMARCHY_ALLOW_NON_APPLE``
#: (``docs/install-omarchy.md``). For testing the CLI's own wiring (venv, download, pip order,
#: join/leave) on a box with no Apple GPU — Try Omarchy's software Vulkan, a plain aarch64 Linux
#: VM — never on a machine this engine is meant to actually serve from. `require_supported_machine`
#: still refuses anything that is not `aarch64` Linux; this only widens WHICH aarch64 Linux box, and
#: `_smoke_test` still runs the matmul and still fails on a NON-finite result. Every code path this
#: unlocks prints where it differs from the real gate so a dev run is never mistaken for one.
_DEV_ENV = "GRID_MLX_OMARCHY_DEV"


def _dev_override() -> bool:
    return os.environ.get(_DEV_ENV, "").strip().lower() in ("1", "true", "yes")


def require_supported_machine() -> str:
    """The chip this engine will run on, or ``SystemExit`` naming why it will not.

    Two gates, two different remedies, so they are two different sentences: the wrong machine has no
    fix, a missing driver has a one-line one.
    """
    if apple_linux.is_aarch64_linux() and _dev_override():
        print(
            f"⚠️  {_DEV_ENV}=1: skipping the Apple Silicon + Vulkan-driver gate. This is for testing "
            "the engine's OWN code path on a non-Apple aarch64 Linux box (a VM's software Vulkan, "
            "for instance) — never point real users at it, and expect CPU-speed inference here."
        )
        _, chip = apple_linux.describe_chip()
        return chip or "development device (non-Apple GPU)"
    if not apple_linux.is_apple_silicon_linux():
        raise SystemExit(
            f"{ENGINE} runs only on an Apple Silicon Mac booted into Linux (Omarchy M). "
            "On macOS and on every other Linux box, use `grid engine install llama.cpp`."
        )
    if not apple_linux.vulkan_ready():
        raise SystemExit(
            f"{ENGINE} needs the Apple GPU's Vulkan driver, and it is not installed.\n"
            "  Install it:  sudo pacman -S vulkan-asahi vulkan-icd-loader    (then re-run)"
        )
    _, chip = apple_linux.describe_chip()
    return chip or "Apple Silicon"


def _run(cmd: list[str], **kwargs) -> None:
    print(f"$ {' '.join(cmd)}")
    subprocess.check_call(cmd, **kwargs)


def _require_system_libs() -> None:
    missing = [name for name in _SYSTEM_LIBS if ctypes.util.find_library(name) is None]
    if missing:
        raise SystemExit(
            f"{ENGINE} needs {', '.join(missing)} on this system (its BLAS calls resolve against "
            f"them at import).\n  Install them:  {_SYSTEM_LIBS_HINT}    (then re-run)"
        )


def _create_venv() -> None:
    """A private venv on Python 3.14 — the only interpreter the wheel is built for. ``uv`` first,
    because it can fetch a 3.14 the box lacks; the stdlib ``venv`` when a ``python3.14`` is on PATH."""
    venv = venv_dir()
    if venv.exists():
        return
    venv.parent.mkdir(parents=True, exist_ok=True)
    uv = shutil.which("uv")
    if uv:
        _run([uv, "venv", "--seed", "-p", PYTHON, str(venv)])
        return
    python = shutil.which(f"python{PYTHON}")
    if not python:
        raise SystemExit(
            f"{ENGINE} needs Python {PYTHON} (the wheel is built for it) — install `uv` "
            f"(recommended) or a `python{PYTHON}`.\n"
            "  Install uv:  curl -LsSf https://astral.sh/uv/install.sh | sh    (then re-run)"
        )
    _run([python, "-m", "venv", str(venv)])


def _pip(*args: str) -> None:
    _run([str(python_bin()), "-m", "pip", "install", "--quiet", *args])


def _smoke_test() -> str:
    """One matmul on the GPU, returning the device name. Refuses ``llvmpipe``: that is Mesa's
    software rasteriser answering the Vulkan call, which means the Apple driver was not the one
    loaded — the engine would "work" at CPU speed and nobody would know why."""
    script = (
        "import mlx.core as mx\n"
        "info = mx.device_info()\n"
        "a = mx.random.normal((256, 256)); b = mx.random.normal((256, 256))\n"
        "c = (a @ b).sum(); mx.eval(c)\n"
        "assert mx.isfinite(c).item(), 'matmul produced a non-finite result'\n"
        "print(info.get('device_name', info))\n"
    )
    out = subprocess.check_output([str(python_bin()), "-c", script], text=True).strip()
    if "llvmpipe" in out.lower():
        if _dev_override():
            print(f"⚠️  {_DEV_ENV}=1: accepting a software Vulkan device ({out}). Speed numbers "
                  "from this run mean nothing — this is a code-path check, not a hardware one.")
            return out
        raise SystemExit(
            f"{ENGINE} imported, but Vulkan handed it a software device ({out}) rather than the "
            "Apple GPU. Check `vulkaninfo --summary` lists Honeykrisp, then re-run."
        )
    return out


def install(version: str | None = None) -> str:
    """Install (or upgrade to) the resolved release under ``~/.grid/engines/mlx-omarchy``.
    Returns the GPU device name. A second run at the same release re-runs only the smoke test."""
    chip = require_supported_machine()
    _require_system_libs()
    paths.ensure_all()
    release = resolve_release(version)
    if is_installed() and installed_version() == release.tag:
        print(f"{ENGINE} {release.tag} is already installed for {chip}; checking it still runs ...")
        return _smoke_test()
    print(f"Installing {ENGINE} {release.tag} for {chip} ...")
    _create_venv()
    with tempfile.TemporaryDirectory(prefix="grid-engine-") as tmpdir:
        wheel = Path(tmpdir) / release.wheel
        print(f"Downloading {release.wheel} ...")
        installer._download(release.wheel_url, wheel)
        got = installer._sha256(wheel)
        if got != release.sha256:
            raise SystemExit(f"SHA-256 mismatch for {release.wheel}: expected {release.sha256}, got {got}")
        _pip("--upgrade", "pip")
        _pip("--upgrade", str(wheel))
    _pip("--upgrade", "--no-deps", release.mlx_lm)
    _pip("--upgrade", *release.deps)
    device = _smoke_test()
    version_file().write_text(f"{release.tag} {release.wheel}\n")
    return device


def start(model: str, *, port: int) -> LlamaProcess:
    """Spawn ``mlx_lm.server`` for ``model`` (a Hugging Face repo id such as
    ``mlx-community/Qwen2.5-7B-Instruct-4bit``, or a local directory) on loopback ``port``.

    Returns the same :class:`LlamaProcess` shape llama-server does, so the serve loops' teardown
    (``launcher.stop``) needs no second code path. Bound to ``127.0.0.1``: the relay reaches this
    engine through the serve loop on the same box, never directly.

    ⚠️ **``python -m mlx_lm server``, never ``python -m mlx_lm.server``.** The latter still runs —
    ``mlx_lm/server.py``'s own ``if __name__ == "__main__"`` calls ``main()`` — but as of mlx-lm
    0.31.3 it opens by printing "Calling `python -m mlx_lm.server...` directly is deprecated. Use
    `mlx_lm.server...` or `python -m mlx_lm server ...` instead." into THIS engine's log on every
    single start. Verified against ``mlx_lm/__main__.py`` (``cli.main()``) and ``mlx_lm/cli.py``
    (``server`` is a listed subcommand, dispatched by ``importlib.import_module("mlx_lm.server")``)
    at tag ``v0.31.3`` — the exact release :data:`MLX_LM` pins.
    """
    if not is_installed():
        raise SystemExit(f"{ENGINE} is not installed. Run: grid engine install {ENGINE}")
    paths.ensure_all()
    log_path = paths.run_dir() / f"mlx-omarchy-{port}.log"
    log_fh = logging_setup.cap_and_open_append(log_path, logging_setup.engine_log_max_bytes())
    cmd = [
        str(python_bin()), "-m", "mlx_lm", "server",
        "--model", model,
        "--host", "127.0.0.1",
        "--port", str(port),
    ]
    proc = subprocess.Popen(cmd, stdout=log_fh, stderr=log_fh)
    return LlamaProcess(proc=proc, port=port, log=log_path)


def wait_ready(proc: LlamaProcess, timeout: float = READY_TIMEOUT) -> None:
    """Block until the model is actually loaded and answering, the process exits, or ``timeout``
    passes.

    ⚠️ **A real generation, never ``GET /v1/models``.** Traced against ``server.py`` at v0.31.3:
    the HTTP server binds and ``/v1/models`` answers 200 within a second of the process starting —
    it only lists what ``scan_cache_dir()`` already finds on disk, and says nothing about the
    ``--model`` this process was launched with. That model loads on a SEPARATE background thread
    started at the same moment (``ResponseGenerator.__init__`` → ``Thread(target=self._generate)``,
    whose first line is ``self.model_provider.load_default()``) — downloading it from Hugging Face
    where needed. Polling ``/v1/models`` would report "ready" while a multi-gigabyte download is
    still in flight, and `grid join` would hand the relay a node that then stalls on its first real
    request instead of during this wait. A trivial ``POST /v1/chat/completions`` is queued behind
    that same load and returns only once it has produced a token — the only honest ready signal
    this server exposes. ``max_tokens: 1`` keeps the warm-up itself cheap once the model IS loaded.
    """
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    probe = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 1, "stream": False}
    while time.monotonic() < deadline:
        rc = proc.proc.poll()
        if rc is not None:
            raise SystemExit(
                f"{ENGINE} on port {proc.port} exited (rc={rc}) before becoming ready. "
                f"Last lines of {proc.log}:\n{_log_tail(proc.log)}"
            )
        try:
            resp = httpx.post(
                f"http://127.0.0.1:{proc.port}/v1/chat/completions", json=probe, timeout=10.0,
            )
            if resp.status_code == 200:
                return
        except httpx.RequestError as exc:
            last_exc = exc
        time.sleep(1.0)
    message = f"{ENGINE} did not become ready on port {proc.port} within {timeout:.0f}s"
    if last_exc:
        message += f" (last error: {last_exc})"
    raise SystemExit(message + f". Log: {proc.log}")


def stop(proc: LlamaProcess, *, timeout: float = 10.0) -> None:
    from shared.engine import launcher

    launcher.stop(proc, timeout=timeout)


def _log_tail(path: Path, lines: int = 30) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return "(log unreadable)"
