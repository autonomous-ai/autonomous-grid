"""Which OS grid this machine claims to belong to — the **OS token** (ADR 0039 D-b, D-c).

An *OS grid* is a grid keyed on an operating system rather than on an email domain: sign in on a Mac
and you are already a member of the macOS grid. The **OS token** is the value a machine and a grid are
matched on, and this module is the ONLY place in this CLI that decides what this machine's is. It
travels as the ``os=`` query parameter on the control-plane token fetch (``remote.control_plane
.fetch_tokens``) — the only channel there is, because the device-login start and poll calls carry
nothing about the machine and the browser that approves a sign-in may be a different device entirely.

⚠️ **This is not `host.platform_kind()`, and the two must not be merged.** That one answers *"which
binaries run here"* and splits macOS by CPU generation (``macos-arm64`` / ``macos-x86_64``); reusing it
would put Apple Silicon and Intel Mac users in two different communities. Two questions, two
vocabularies, even where today's values overlap (ADR 0039 D-c).

⚠️ **The taxonomy is CLOSED, and that is what makes auto-provisioning safe.** An unrecognised system
resolves to ``None`` and no grid is provisioned for it; on an open value space every unrecognised
string would become a permanent empty grid. `platform_kind()` carries an ``other`` bucket for this
reason — ``other`` must never reach this path, which is why nothing here has a fallback token.

⚠️ **A machine's OS is a CLAIM, never a fact.** The control plane cannot verify it, exactly as it
cannot verify ``device_id`` (a ``uuid4`` this CLI writes to ``~/.grid/device.toml``). The gate it feeds
stops *mistakes*, not *intent*, and nothing downstream may be justified by "only macOS machines are on
the macOS grid" (ADR 0039 D-b).

``omarchy`` is the fourth token in ADR 0039 D-c and is deliberately **absent here**: it is Arch-based
and whether ``/etc/os-release`` distinguishes it from stock Arch is UNMEASURED. Adding it on an
unverified signal would silently sort every Arch user into the Omarchy grid — the exact mis-sorting the
closed set exists to prevent. `.scratch/os-grid-type/issues/04-omarchy-gets-its-own-grid.md` owns it,
and owns taking the measurement first; that is also where the ``/etc/os-release`` read arrives (see
``shared.engine.installer._detect_distro`` for the shape it will follow).
"""

from __future__ import annotations

import platform

OS_MACOS = "macos"
OS_WINDOWS = "windows"
OS_LINUX = "linux"

#: Every token this CLI can emit. Closed by decision (ADR 0039 D-c) — see the module docstring.
OS_TOKENS: tuple[str, ...] = (OS_MACOS, OS_WINDOWS, OS_LINUX)

# `platform.system()` → OS token. Only the systems that HAVE a grid appear; everything else — a BSD, a
# Java runtime, the empty string a frozen build can report — is absent and resolves to None.
_BY_SYSTEM = {
    "Darwin": OS_MACOS,
    "Windows": OS_WINDOWS,
    "Linux": OS_LINUX,
}


def os_token() -> str | None:
    """This machine's OS token, or ``None`` when it has no OS grid.

    ``None`` is an ordinary answer, not a failure: a machine outside the closed set simply has no OS
    grid, keeps every other grid it belongs to, and sends no ``os`` parameter at all.

    Every Linux resolves to ``linux``, including a distribution nobody has heard of — an unusual
    choice of distribution must not exclude somebody from the general Linux grid.
    """
    return _BY_SYSTEM.get(platform.system())
