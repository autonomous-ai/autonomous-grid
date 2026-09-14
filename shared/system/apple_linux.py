"""An Apple Silicon Mac running Linux — Omarchy M, on the Asahi stack — seen from the hardware side.

Every other probe in this package answers "Apple Silicon?" with ``platform.system() == "Darwin"``,
and until this module that was the whole truth: the only way to run Grid on an M-series chip was
macOS. Omarchy M (announced 2026-09-11) changes that. The same chip now boots Linux natively, and
on that Linux the Mac looks like nothing Grid knows: ``platform.system()`` says ``Linux``,
``nvidia-smi`` is absent, ``system_profiler`` and ``sysctl hw.memsize`` do not exist, and
``/proc/cpuinfo`` carries no ``model name``. So ``grid engine install`` picked the CPU build, the
grid page named the node ``aarch64`` with no memory, and ``grid catalog`` could not narrow — on a
machine with 16–192 GB of unified memory and a conformant Vulkan driver.

This module is the ONE place that recognises the machine, and it reads what Linux on that hardware
actually exposes:

- **Whether it is one** — the device tree. ``/proc/device-tree/compatible`` on Asahi reads
  ``apple,j314s\\0apple,t6000\\0apple,arm-platform``: the board, the SoC, the platform. Matching
  ``apple,`` is the exact test Omarchy's own packaging uses to tell a Mac from every other
  aarch64 box (``omarchy-settings.install``, ``_apple_silicon()``), so this CLI and the
  distribution cannot disagree about which machines are Macs.

  ⚠️ **A VM on a Mac is NOT one, and that is the design.** ``try-omarchy`` — the app most people
  meet Omarchy through on a Mac — is QEMU's ``virt`` machine, whose device tree says
  ``linux,dummy-virt`` and whose GPU is ``virtio-gpu-gl`` (OpenGL through virgl, Vulkan only via
  lavapipe on the CPU). The Apple GPU is not reachable from inside it, so answering "Apple
  Silicon" there would install a Vulkan engine that runs on software rasterisation. The device
  tree gets this right for free; do not "improve" it with a ``uname -m`` check.

- **Whether its GPU is usable** — the Vulkan driver. Asahi's Vulkan driver (Honeykrisp, in Mesa)
  registers itself with the loader through an ICD manifest, ``asahi_icd.aarch64.json`` — Arch
  Linux ARM ships it as ``vulkan-asahi``, which Omarchy Mac installs by default
  (``install/hardware/vulkan.sh``). llama.cpp's Vulkan build ``dlopen``s ``libvulkan.so.1`` and
  asks the loader for devices, so BOTH the manifest and the loader must be present, and this
  checks both: a Vulkan engine on a box with either one missing does not start at all.

- **What to call it** — ``/proc/device-tree/model``, which m1n1 fills from Apple's own strings:
  ``Apple MacBook Pro (14-inch, M1 Pro, 2021)``. The chip is parsed out of the parentheses
  because the grid page names an Apple machine by its chip (see ``node_hardware``), and nothing
  else on a Linux Mac states it.

- **How much memory** — the device tree's ``memory`` node, which m1n1 fills with the physical
  size (``reg = <addr_hi addr_lo size_hi size_lo>``, big-endian, 2 address cells and 2 size
  cells). ⚠️ Not ``/proc/meminfo`` first: ``MemTotal`` there is what is left after the firmware
  and the kernel's own reservations, which on a 16 GB Mac reads about 15.5 GB and rounds to
  "15 GB" on a page where every other Mac says "16 GB". ``meminfo`` is the fallback when the
  node cannot be read.

Every probe is best-effort and never raises — a missing fact is ``False``, ``""`` or ``0``, and
the caller lands on the branch it was on before this module existed.
"""

from __future__ import annotations

import ctypes.util
import platform
import re
import struct
from pathlib import Path

from shared.system import arch

#: The device tree, as the kernel exposes it. ``/proc/device-tree`` is a symlink to
#: ``/sys/firmware/devicetree/base``; the ``/proc`` spelling is the one Omarchy's own scripts use.
_COMPATIBLE = Path("/proc/device-tree/compatible")
_MODEL = Path("/proc/device-tree/model")
_DEVICE_TREE = Path("/proc/device-tree")

_MEMINFO = Path("/proc/meminfo")

#: Honeykrisp's ICD manifest, where Mesa installs it on an aarch64 Arch (``with_vulkan_icd_dir``
#: is ``/usr/share/vulkan/icd.d``; the file is ``asahi_icd.<cpu>.json`` — ``src/asahi/vulkan/
#: meson.build``). A system path on purpose, like ``os_grid._BY_MARKER``: this decides which
#: engine build runs, and a per-user path is writable by anything the person runs.
_ASAHI_ICD = Path("/usr/share/vulkan/icd.d/asahi_icd.aarch64.json")

#: The substring every Apple Silicon device tree carries, and no other aarch64 machine's does.
_APPLE_COMPATIBLE = "apple,"

#: ``M1``, ``M2 Pro``, ``M1 Ultra`` inside the model string's parentheses. Word-bounded on both
#: sides so ``M1`` cannot match inside ``M10`` or a future ``M1X``.
_CHIP = re.compile(r"\bM(\d+)(?: (Pro|Max|Ultra))?\b")

#: A bound on the device-tree reads. They are a few dozen bytes each; this keeps an unbounded
#: read off paths this CLI does not own.
_MAX_READ = 4096


def _read(path: Path, limit: int = _MAX_READ) -> str:
    """A device-tree string property, or ``""`` when it cannot be read.

    Device-tree strings are NUL-terminated and a ``stringlist`` is NUL-separated, so the NULs are
    turned into spaces: ``compatible`` becomes ``"apple,j314s apple,t6000 apple,arm-platform"``,
    which is searchable, and ``model`` loses only its terminator.
    """
    try:
        with path.open("rb") as handle:
            raw = handle.read(limit)
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace").replace("\x00", " ").strip()


def is_aarch64_linux() -> bool:
    """Linux on ARM64 — the shape :func:`is_apple_silicon_linux` narrows with the device tree.
    Exposed on its own for the mlx-omarchy engine's dev override (``GRID_MLX_OMARCHY_DEV``),
    which needs "any aarch64 Linux box" without also accepting an x86 machine."""
    return platform.system() == "Linux" and arch.normalized_machine() == "aarch64"


def is_apple_silicon_linux() -> bool:
    """Whether this is an M-series Mac booted into Linux — bare metal, not a VM (see module doc).

    The system and architecture are checked FIRST, so the device tree is never read on a Mac
    running macOS (where the path does not exist) or on an x86 box (where a stray file could not
    make it one).
    """
    if platform.system() != "Linux" or arch.normalized_machine() != "aarch64":
        return False
    return _APPLE_COMPATIBLE in _read(_COMPATIBLE)


def vulkan_ready() -> bool:
    """Whether the Apple GPU is reachable through Vulkan — the ICD manifest AND the loader.

    Meaningful only after :func:`is_apple_silicon_linux`; on any other machine the manifest is
    simply absent. ``find_library`` is the same lookup ``dlopen`` performs (``ldconfig -p``), so
    a ``True`` here is a loader llama.cpp will actually find.
    """
    if not _ASAHI_ICD.is_file():
        return False
    try:
        return ctypes.util.find_library("vulkan") is not None
    except Exception:
        # `find_library` shells out; a broken `ldconfig` must read as "no loader", not a traceback.
        return False


def describe_chip() -> tuple[str, str]:
    """Best-effort ``(model, chip)`` — ``("Apple MacBook Pro (14-inch, M1 Pro, 2021)", "Apple M1
    Pro")``. The chip keeps its ``Apple `` prefix, matching what ``apple.describe_chip`` returns on
    macOS, so the grid page names the same machine the same way whichever OS it booted.
    ``("", "")`` when the model cannot be read; ``(model, "")`` when it names no M-series chip."""
    model = _read(_MODEL)
    if not model:
        return "", ""
    match = _CHIP.search(model)
    if not match:
        return model, ""
    chip = f"Apple M{match.group(1)}"
    if match.group(2):
        chip = f"{chip} {match.group(2)}"
    return model, chip


def _device_tree_memory_mb() -> float:
    """Physical memory from the device tree's ``memory`` node(s), in MB, or 0 when unreadable.

    Each ``reg`` entry is ``<address size>`` with two 32-bit cells each (Apple's trees declare
    ``#address-cells = <2>`` and ``#size-cells = <2>``), big-endian as all device-tree data is.
    A ``reg`` whose length is not a whole number of 16-byte entries is not one this parser
    understands, and answers 0 rather than a guess.
    """
    total = 0
    try:
        nodes = sorted(_DEVICE_TREE.glob("memory*"))
    except OSError:
        return 0.0
    for node in nodes:
        try:
            raw = (node / "reg").read_bytes()
        except OSError:
            continue
        if not raw or len(raw) % 16:
            return 0.0
        for offset in range(0, len(raw), 16):
            _addr, size = struct.unpack(">QQ", raw[offset:offset + 16])
            total += size
    return total / (1024 * 1024)


def _meminfo_total_mb() -> float:
    """``MemTotal`` from ``/proc/meminfo``, in MB, or 0 when unreadable."""
    try:
        with _MEMINFO.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return float(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def memory_total_mb() -> float:
    """Unified memory in MB — the physical size, which on Apple Silicon is also the GPU's pool.
    Device tree first, ``/proc/meminfo`` as the fallback (see the module docstring for why that
    order). 0 when neither can be read."""
    return _device_tree_memory_mb() or _meminfo_total_mb()
