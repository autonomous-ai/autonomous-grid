"""What this machine IS, in the shape the grid page reads.

Produces the relay's node-meta contract — ``device`` / ``chip`` / ``device_class`` / ``memory_gb`` —
so a machine on the grid shows as something a person recognises instead of a hostname and an OS
name. The relay stores these verbatim and ``build_grid_overview`` hands them to the app, which
prints ``chip`` when there is one and ``device`` otherwise.

That preference is the whole reason both fields exist, and it is not arbitrary:

- **Apple Silicon is named by its CHIP** ("Apple M4 Pro"). The GPU has no name of its own — it is
  part of the SoC — so "Apple GPU" would tell a reader nothing they can act on, while the chip name
  is exactly what they would say out loud about the machine.
- **Everything else is named by its CARD** ("NVIDIA GeForce RTX 4090 ×2"). That is what decides what
  the box can run; its CPU brand is noise beside it, and on a rented GPU box the CPU is often
  something nobody chose.

Probed **once and cached**. ``system_profiler`` takes seconds to answer and this is read on every
heartbeat; hardware does not change under a running process, and a swapped card needs a restart —
which is also when the next probe would happen.

Best-effort throughout: every field may come back empty, and the grid page is built to say less
rather than guess. A wrong "RTX 4090" is worse than a blank, because somebody would route work to it.
"""

from __future__ import annotations

import platform
import subprocess

from shared.system import apple, arch, gpu, host

# Filled by the first `describe()` and reused after — see the module docstring on why this is not
# re-probed. `None` means "not probed yet", distinct from a probe that legitimately found nothing.
_cached: dict | None = None


def _is_apple_silicon() -> bool:
    # `native_machine()` sees through Rosetta (x86_64 Python on Apple Silicon), the same check
    # `device._is_apple_silicon` makes — an Intel-looking interpreter on an M-series box must still
    # be named by its chip.
    return platform.system() == "Darwin" and arch.native_machine() == "arm64"


def _memory_gb() -> int | None:
    """Unified memory on Apple Silicon, in whole GB, or None when it can't be read."""
    mb = gpu._sysctl_memsize_mb()
    return int(round(mb / 1024)) if mb else None


def _apple() -> dict:
    model, chip = apple.describe_chip()
    return {
        # Both, though the app prints only the chip: `device` is the fallback for a machine whose
        # chip line failed to parse, and it costs nothing to carry the model name we already read.
        "device": model or "",
        "chip": chip or None,
        "memory_gb": _memory_gb(),
        "device_class": "gpu",
    }


def _nvidia() -> dict | None:
    """The card(s) this box has, or None when nothing answered — a CPU-only node, or a driver that
    isn't installed. None rather than a placeholder: see the module docstring."""
    try:
        cards = gpu.enumerate_gpus()
    except Exception:
        cards = []
    if not cards:
        return None
    primary = cards[0]
    # "×2" rather than listing both: two identical cards are the common case and the name is a
    # label, not an inventory. A mixed pair still reads as the primary — the memory figures the
    # page shows come from telemetry, which counts them properly.
    suffix = f" ×{len(cards)}" if len(cards) > 1 else ""
    vram_gb = (
        int(round(primary.memory_total_mb / 1024)) if primary.memory_total_mb else None
    )
    return {
        "device": f"{primary.name}{suffix}",
        "chip": None,
        "memory_gb": vram_gb,
        "device_class": "gpu",
    }


def _macos_gpu() -> dict | None:
    """The graphics card an **Intel** Mac has, from ``system_profiler SPDisplaysDataType``.

    `enumerate_gpus` only knows `nvidia-smi`, and no Intel Mac has an NVIDIA card — so without this
    a MacBook Pro with a Radeon Pro 560X fell through to the CPU branch and advertised itself as
    "i386", which is the machine's architecture written in a way nobody recognises.

    A machine reports several chipsets (an integrated Intel one alongside a discrete AMD one). The
    discrete card is the one that decides what the box can run, and on a Mac the integrated one is
    always the Intel-branded entry — so anything not Intel-branded wins, and a machine with only
    integrated graphics still gets named rather than dropped.
    """
    try:
        out = subprocess.check_output(
            ["system_profiler", "SPDisplaysDataType"], timeout=10.0, stderr=subprocess.DEVNULL
        ).decode("utf-8", errors="replace")
    except (subprocess.SubprocessError, OSError):
        return None
    names = [
        line.split(":", 1)[1].strip()
        for line in out.splitlines()
        if line.strip().startswith("Chipset Model:")
    ]
    names = [n for n in names if n]
    if not names:
        return None
    discrete = [n for n in names if "intel" not in n.lower()]
    vram_mb = gpu._macos_profiler_vram_mb()
    return {
        "device": (discrete or names)[0],
        "chip": None,
        "memory_gb": int(round(vram_mb / 1024)) if vram_mb else None,
        "device_class": "gpu",
    }


def _cpu_only() -> dict:
    # `host.cpu_brand()`, not `platform.processor()`: the latter answers "i386" on macOS and
    # "x86_64" on Linux — the architecture, not the processor, and useless on a page whose job is to
    # say what the machine is.
    return {
        "device": host.cpu_brand() or platform.machine() or "CPU",
        "chip": None,
        "memory_gb": None,
        "device_class": "server",
    }


def _probe() -> dict:
    if _is_apple_silicon():
        return _apple()
    if platform.system() == "Darwin":
        # An Intel Mac: no NVIDIA card exists for it, so ask macOS directly before falling back.
        return _macos_gpu() or _cpu_only()
    return _nvidia() or _cpu_only()


def describe() -> dict:
    """``{device, chip, memory_gb, device_class}`` for this machine. Never raises.

    A copy each call, so a caller merging it into a meta dict can't mutate the cache and change what
    every later heartbeat reports.
    """
    global _cached
    if _cached is None:
        try:
            _cached = _probe()
        except Exception:
            # A probe that fails must not take the node down with it: an unnamed machine still
            # serves models perfectly well, and the page is built to show less rather than guess.
            _cached = {}
    return dict(_cached)


def meta_fields() -> dict:
    """[describe] with the empty fields dropped — what actually goes on the wire.

    Empties are omitted rather than sent blank because the relay **merges** meta over what it holds:
    a probe that came back short this run would otherwise erase a name that a previous run got
    right, and the page would go blank for a reason nobody could see from either side.
    """
    return {k: v for k, v in describe().items() if v not in (None, "")}
