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

``omarchy`` is the fourth token (ADR 0039 D-c, issue 04) and the only one ``platform.system()`` cannot
answer: Omarchy reports ``Linux`` like every other distribution, so it is resolved from ``ID=`` in
``/etc/os-release``, one step further down and only for a machine that already resolved to ``linux``.

⚠️ **``ID=``, and never ``ID_LIKE=``.** Omarchy writes ``ID=omarchy`` *and* ``ID_LIKE=arch``, and so
does every other Arch derivative — EndeavourOS, CachyOS, Manjaro. ``shared.engine.installer
._detect_distro`` is the read this docstring used to point at as "the shape to follow", and it reads
the two fields into ONE list and matches either; copied here it would put every Arch derivative on the
Omarchy grid, which is the exact mis-sorting the closed set exists to prevent. ``ID=`` is an identity,
``ID_LIKE=`` is a lineage, and only the first one names a community.

⚠️ **Omarchy is NOT also on the Linux grid, and that is the decision rather than a gap.** ``os=`` is a
single value and the far end's gate is an equality test against one grid's own token, so claiming
``omarchy`` is precisely what takes the machine off the general Linux grid. A reading that has it join
both sounds generous — Omarchy *is* Linux — and the wire cannot express it.
"""

from __future__ import annotations

import platform
from pathlib import Path

OS_MACOS = "macos"
OS_WINDOWS = "windows"
OS_LINUX = "linux"
OS_OMARCHY = "omarchy"

#: Every token this CLI can emit. Closed by decision (ADR 0039 D-c) — see the module docstring.
OS_TOKENS: tuple[str, ...] = (OS_MACOS, OS_WINDOWS, OS_LINUX, OS_OMARCHY)

# `platform.system()` → OS token. Only the systems that HAVE a grid appear; everything else — a BSD, a
# Java runtime, the empty string a frozen build can report — is absent and resolves to None.
_BY_SYSTEM = {
    "Darwin": OS_MACOS,
    "Windows": OS_WINDOWS,
    "Linux": OS_LINUX,
}

#: Where a Linux machine names its distribution. Read only when `_BY_SYSTEM` already said `linux`.
#:
#: ⚠️ `/usr/lib/os-release` — the freedesktop fallback — is deliberately NOT consulted. Omarchy's
#: `omarchy-settings` package `rm -f`s `/etc/os-release` and copies its own over it on every install
#: AND upgrade, so this path is where its claim lands; and on a machine that has only the `/usr/lib`
#: copy the answer would be some other distribution's `ID`, which resolves to `linux` either way.
_OS_RELEASE = Path("/etc/os-release")

#: How much of `/etc/os-release` is read. It is a root-owned file of a few hundred bytes everywhere,
#: so this is a bound and not a threat model: it keeps an unbounded read off a path this CLI does not
#: own. Truncation can only LOSE the `ID=` line, and a lost signal lands on `linux`.
_MAX_OS_RELEASE_BYTES = 64 * 1024

#: Distribution ``ID`` → OS token, for the distributions that have a grid of their own. A second
#: closed set, and everything absent from it is an ordinary Linux machine (see `os_token`).
_BY_DISTRO_ID = {
    "omarchy": OS_OMARCHY,
}


def _distro_id() -> str:
    """The lowercased ``ID=`` of ``/etc/os-release``, or ``""`` when there is nothing to read.

    Every failure is the empty string, never an exception: this runs inside ``grid login`` on a
    machine whose only fault might be a mis-encoded system file, and a traceback there would be about
    the OS grid — the one part of a sign-in nobody asked for. ``errors="replace"`` rather than a bare
    ``read_text`` because ``UnicodeDecodeError`` is a ``ValueError`` that no ``except OSError`` sees.

    ⚠️ Reads ``ID=`` alone. See the module docstring for why ``ID_LIKE=`` must stay out of it.
    """
    try:
        with _OS_RELEASE.open("rb") as handle:
            text = handle.read(_MAX_OS_RELEASE_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        if line.startswith("ID="):
            return line.split("=", 1)[1].strip().strip("\"'").strip().lower()
    return ""


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

    Every Linux resolves to ``linux`` **except** one that names itself ``omarchy`` — a distribution
    nobody has heard of still lands on the general Linux grid, and so does a machine with no
    ``/etc/os-release`` to read. The distro lookup narrows nothing: it is an exception TO the ``linux``
    answer, taken after it, so a new entry in ``_BY_DISTRO_ID`` can only ever move machines that
    already matched that entry's own ``ID``.

    Resolved from :func:`system_name` rather than from its own ``platform.system()`` call, so the two
    answers this module gives are one reading. The trim that comes with it is free: a padded system
    name matched nothing before and matches its grid now.

    ⚠️ The distro read hangs off the ``linux`` answer and nothing else. ``/etc/os-release`` is not
    Linux's alone — a container image or a hand-rolled script can leave one on a Mac — and consulting
    it before the system is known would move that machine's grid.
    """
    token = _BY_SYSTEM.get(system_name())
    if token != OS_LINUX:
        return token
    return _BY_DISTRO_ID.get(_distro_id(), OS_LINUX)
