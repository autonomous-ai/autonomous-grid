"""Which OS grid this machine claims to belong to — the **OS token** (ADR 0039 D-b, D-c).

An *OS grid* is a grid keyed on an operating system rather than on an email domain: sign in on a Mac
and you are already a member of the macOS grid. The **OS token** is the value a machine and a grid are
matched on, and this module is the ONLY place in this CLI that decides what this machine's is. It
travels as the ``os=`` query parameter on the control-plane token fetch (``remote.control_plane
.fetch_tokens``) — the only channel there is, because the device-login start and poll calls carry
nothing about the machine and the browser that approves a sign-in may be a different device entirely.

It also **names** this machine's system for a person (:func:`system_name`), which is a different job
from deciding its token and is here for one reason: both answers come from a single reading of
``platform.system()``, so the sentence somebody is shown and the value the gate compares can never be
talking about different machines. Do not "tidy" that function out on the grounds that it decides no
token — it is what stops a second read appearing somewhere else.

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


def system_name() -> str:
    """What this machine calls its own operating system — ``platform.system()``, trimmed.

    ⚠️ **The single read.** :func:`os_token` resolves its answer FROM this one, so the name shown to a
    person and the token sent on the wire cannot come from two different readings of the same call —
    which is what the module docstring promises and what a second bare ``platform.system()`` four
    lines away quietly broke.

    The name a PERSON is shown when there is no OS grid for their machine (ADR 0039 D-k), which is why
    it is the raw value and not a prettied-up one: on a system outside the closed set there is no token
    to name and no label to look one up by, so the only honest thing to print is what the machine
    itself answers. It can be the empty string on a frozen build — the same systems that resolve to no
    token — so a caller must have something to say when it is.
    """
    return platform.system().strip()


def os_token() -> str | None:
    """This machine's OS token, or ``None`` when it has no OS grid.

    ``None`` is an ordinary answer, not a failure: a machine outside the closed set simply has no OS
    grid, keeps every other grid it belongs to, and sends no ``os`` parameter at all.

    Every Linux resolves to ``linux``, including a distribution nobody has heard of — an unusual
    choice of distribution must not exclude somebody from the general Linux grid.

    Resolved from :func:`system_name` rather than from its own ``platform.system()`` call, so the two
    answers this module gives are one reading. The trim that comes with it is free: a padded system
    name matched nothing before and matches its grid now.
    """
    return _BY_SYSTEM.get(system_name())
