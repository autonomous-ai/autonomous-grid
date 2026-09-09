"""Apple-silicon hardware recognised from **Linux** — the Asahi Firmwares side of the same silicon.

`platform.system() == "Darwin"` is how this codebase normally says "Apple hardware", and on Linux it
is false. That is correct for what the operating system can DO (there is no Metal, no MLX and no
MPS in PyTorch here, so every backend selector must keep asking for Darwin) and wrong for what the
machine IS: an M1/M2 under Asahi Linux has the same SoC, the same marketing chip name, and the same
single unified pool of memory the GPU and the CPU share. The consequence of conflating the two was a
real omarchy-mac node (Asahi + Arch + Hyprland) joining the grid as an anonymous "server": no chip,
no memory, and its 16 GB of unified RAM labelled VRAM.

This module answers the hardware question only. It reads the ARM *devicetree* the Asahi boot chain
hands the kernel and derives from it:

- `apple,tXXXX` in the root `compatible` list — the SoC identity. Upstream orders the list
  board-first ("apple,j293", "apple,t8103", "apple,arm-platform"), but the SoC is looked for anywhere
  in it: the SoC entry is the one present on every machine of a generation, while a vendor tree is
  free to reorder the rest.
- `model` — the Apple marketing string, verbatim ("Apple MacBook Pro (14-inch, M2 Pro, 2023)"). It
  says the *product*, not the silicon ("Apple MacBook Air"), so it names the machine but never the
  chip.

Reads are NUL-separated property blobs (device-tree convention), bounded, and every path ends in
``None`` / ``False`` rather than an exception — a machine without a flattened devicetree is a normal
machine, and one wrong "Apple M2 Pro" is worse than a blank because somebody would route work to it.
"""

from __future__ import annotations

import platform

from shared.system import arch

# Property blobs are small (< 200 bytes upstream); the bound is the usual defensive read against a
# proc/sysfs entry that reports a huge size.
_MAX_DT_BYTES = 4096

_DT_PATHS = ("/proc/device-tree", "/sys/firmware/devicetree/base")

# SoC part number → the chip name a person uses for the machine, matching the naming rule in
# `node_hardware` ("Apple M4 Pro"). Part numbers are the Asahi/upstream device-tree identifiers;
# Asahi's supported list tops out at the M2 generation today, so the newer rows are forward
# compatibility, not verified support — the chip name is still the right thing to print when a
# future tree grows support, and never a claim that a backend exists.
SOC_CHIPS: dict[str, str] = {
    "t8103": "Apple M1",
    "t6000": "Apple M1 Pro",
    "t6001": "Apple M1 Max",
    "t6002": "Apple M1 Ultra",
    "t8112": "Apple M2",
    "t6020": "Apple M2 Pro",
    "t6021": "Apple M2 Max",
    "t6022": "Apple M2 Ultra",
    "t8122": "Apple M3",
    "t6030": "Apple M3 Pro",
    "t6031": "Apple M3 Max",
    "t6032": "Apple M3 Ultra",
    "t6034": "Apple M3 Max",  # the binned variant upstream gives its own part number
    "t8132": "Apple M4",
    "t6040": "Apple M4 Pro",
    "t6041": "Apple M4 Max",
}

# Memoised once: the devicetree does not change under a running process, and this is read on the
# heartbeat path. `None` means "not probed yet", distinct from a probe that found no Apple tree.
_probed: dict[str, str | None] | None = None


def _read_property(name: str) -> bytes:
    """The raw value of a root devicetree property, or ``b""`` when there is no devicetree.

    Tries every known mount point; the first one that answers wins. String properties are
    NUL-terminated, and multi-string ones (`compatible`) are NUL-separated — the caller splits."""
    for base in _DT_PATHS:
        try:
            with open(f"{base}/{name}", "rb") as handle:
                return handle.read(_MAX_DT_BYTES)
        except OSError:
            continue
    return b""


def _probe() -> dict[str, str | None]:
    """The Apple device-tree reading: ``{"soc", "board", "model"}``, any of which may be None.

    Keys are always present — that is what distinguishes "this is an Apple tree, but the property was
    unreadable" from "this is not an Apple tree" (`None`)."""
    out: dict[str, str | None] = {"soc": None, "board": None, "model": None}
    compatible = _read_property("compatible")
    if not compatible:
        return out
    # Every entry must decode as ASCII to count: a device-tree we cannot decode is a device-tree we
    # must not guess about, not a licence to pattern-match on mojibake.
    try:
        entries = [
            entry.decode("ascii")
            for entry in compatible.split(b"\x00")
            if entry
        ]
    except UnicodeDecodeError:
        return out
    if not any(entry == "apple,arm-platform" or entry.startswith(("apple,t", "apple,j"))
               for entry in entries):
        return out
    for entry in entries:
        if out["soc"] is None and entry.startswith("apple,t"):
            out["soc"] = entry[len("apple,"):]
        elif out["board"] is None and entry.startswith("apple,j"):
            out["board"] = entry[len("apple,"):]
    model = _read_property("model").split(b"\x00", 1)[0]
    try:
        text = model.decode("utf-8").strip()
    except UnicodeDecodeError:
        text = ""
    if text:
        out["model"] = text
    return out


def info() -> dict[str, str | None]:
    """Memoised `apple,` devicetree reading for this machine; all-None off Apple-on-Linux."""
    global _probed
    if _probed is None:
        try:
            _probed = _probe()
        except Exception:
            _probed = {"soc": None, "board": None, "model": None}
    return _probed


def is_apple_linux() -> bool:
    """True on Apple silicon running Linux (Asahi), False on macOS and on any other Linux box.

    The ARM check comes first so an x86 machine never opens the devicetree. It is the release-artifact
    spelling `arch.normalized_machine` produces, which is what an Asahi userland reports."""
    if platform.system() != "Linux":
        return False
    if arch.normalized_machine() != "aarch64":
        return False
    return info()["soc"] is not None


def chip_name() -> str:
    """The marketing chip name ("Apple M2 Pro") or ``""`` when the SoC is unknown.

    The devicetree `model` names the *product* ("Apple MacBook Pro (14-inch, M2 Pro, 2023)"), which is
    not the vocabulary the grid page uses for Apple boxes, so it is never parsed here. A part number
    outside `SOC_CHIPS` — a generation newer than this table — yields empty rather than a guess: an
    unpublished chip reads as a blank, while a wrong one routes work to the wrong machine."""
    soc = info()["soc"]
    if soc is None:
        return ""
    return SOC_CHIPS.get(soc, "")


def board_identifier() -> str:
    """The Apple board revision ("j414s") or ``""``; the model string when there is no board entry.

    This is the hardware identifier, so unlike `chip_name` it passes through what the tree actually
    says. Upstream puts the board before the SoC in `compatible`, so a tree that has a SoC but no board
    is a non-mainline one; the model string is the only other thing in the tree that can tie such a
    machine to a device."""
    found = info()
    return found["board"] or found["model"] or ""
