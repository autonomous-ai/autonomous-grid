"""Installation identity for detached Grid processes, plus legacy Linux environment inspection.

``shared/orphan_sweep.py`` once identified an engine child only as ``<cli> __remote-engine
<network_id> <engine_id>``. That is the identity of a **grid**, not an **installation**. Two
accounts serving the same grid from one provider box — the shared, internally operated host ADR 0032
is written for — spawn children whose argv agrees in every token, so ``grid leave`` run from one
``GRID_HOME`` unregistered and killed the other account's provider, silently (dev-VM finding E-03,
reproduced in minutes on the first box that had two). ADR 0025 recorded the property as an accepted
residual on the reasoning that two configs sharing a grid id are one grid to this CLI; that holds for
a *local* grid id, which is an accident when it collides, and does not hold for a remote network id,
which is **shared by design** — joining one grid from several accounts is what a grid is for.

Current children therefore carry ``--grid-home-tag=<sha256(canonical GRID_HOME)>`` immediately
before the marker. It is non-secret, stable, cross-platform, and exposes no path. ``GRID_HOME`` is
the correct identity because run records, credentials, engine logs and node identity are all keyed
by it; two homes on one box are two installations by definition.

For pre-tag children, environment inspection remains a **Linux-only compatibility backstop**.
``/proc/<pid>/environ`` is readable by the
process's own uid and by root, and by nobody else — but that is exactly the case that needs it. A
match owned by a *different user* is already declined by POSIX at signal time and reported as
``foreign`` (see ``orphan_sweep``'s module docstring), so the hole left open is same-uid,
different-``GRID_HOME`` — which is precisely where the file is readable. macOS has no equivalent:
measured on 26.6, ``ps -E``/``ps eww`` print no environment at all, not even for the caller's own
child. Windows would need a remote-thread read of the PEB. On both, and for anything we cannot pin
down, the answer is ``None`` — *unknown*. Legacy unknowns retain the pre-tag behaviour so an upgrade
does not strand record-less workers; current children never need that inference because their argv
identity is definitive.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from collections.abc import Iterable
from pathlib import Path

from shared import paths

# The variable, and the default it stands in for. Both are ``shared.paths.grid_home``'s, restated
# here rather than imported as one expression because that function answers for *this* process and
# every question below is about a different one — the whole bug being fixed is an answer computed
# with the wrong process's environment.
GRID_HOME_VAR = "GRID_HOME"
_DEFAULT_HOME_LEAF = ".grid"

# How much of an environment block we will read before declining to answer. Linux bounds argv+environ
# by ``ARG_MAX`` (2 MiB on a stock kernel), so a real serve child's block is orders of magnitude
# under this; a process at the cap is either pathological or hostile. Reading it all would let any
# local user make ``grid leave`` allocate at will, and reading a *truncated* prefix is worse than not
# reading at all: ``GRID_HOME`` could sit past the cut, so the parse would fall back to ``HOME`` and
# name the wrong directory **with confidence**. Hence: at the cap ⇒ unknown.
_ENVIRON_MAX_BYTES = 1 << 20

# A pid the kernel will never assign, for the "names nothing" test. ``0`` is the caller's own process
# group and negative values are groups, so neither is a pid that is merely absent.
_UNUSED_PID_PROBE = 2**31 - 1

# Detached engine children carry this non-secret, one-token installation discriminator immediately
# before their internal dispatch marker. The digest names the canonical GRID_HOME, not its contents:
# it reveals no credential or path and remains stable across processes while separating two Grid
# installations serving the same network from one OS account.
HOME_TAG_ARG_PREFIX = "--grid-home-tag="
_HOME_TAG_HEX_LEN = 64
_HOME_TAG_RE = re.compile(rf"[0-9a-f]{{{_HOME_TAG_HEX_LEN}}}\Z")


def _environ_path(pid: int) -> Path:
    """Where this platform keeps one process's environment. A seam, so the size guard can be tested
    against a real file without a real process at the cap."""
    return Path(f"/proc/{pid}/environ")


def _parse_environ_blob(blob: bytes) -> dict[str, str]:
    """A NUL-separated ``KEY=VALUE`` block as a mapping.

    The **first** spelling of a repeated key wins, which is what libc ``getenv`` answers — a process
    can be handed a block with duplicates, and reading the last would let a planted trailing copy
    re-point the answer. Undecodable bytes are replaced rather than raising: this runs inside a
    teardown that must never crash, and both the variable name and the paths being compared are
    ASCII (a mangled value simply fails to equal ours, which spares the process — the safe side).
    """
    env: dict[str, str] = {}
    for entry in blob.split(b"\0"):
        name, sep, value = entry.partition(b"=")
        if sep:
            env.setdefault(name.decode("utf-8", "replace"), value.decode("utf-8", "replace"))
    return env


def _read_environ(pid: int) -> dict[str, str] | None:
    """One process's environment, or ``None`` when this box cannot tell us.

    ``None`` covers every reason at once and they are deliberately not distinguished, because every
    caller does the same thing with them: not Linux, no such pid, another user's process, a kernel
    thread with an empty block, a block at the read cap. "We could not find out" and "there is
    nothing to find out" both mean *do not narrow the sweep on this pid*.
    """
    if not sys.platform.startswith("linux"):
        return None
    try:
        with open(_environ_path(pid), "rb") as handle:
            blob = handle.read(_ENVIRON_MAX_BYTES + 1)
    except OSError:
        return None  # gone, or not ours to read — either way, nothing established
    if len(blob) > _ENVIRON_MAX_BYTES:
        return None  # see `_ENVIRON_MAX_BYTES`: a truncated read answers confidently and wrongly
    return _parse_environ_blob(blob) or None


def _expand(raw: str, env: dict[str, str]) -> str | None:
    """``raw`` as an absolute path, using **that process's** ``HOME``, or ``None``.

    ``os.path.expanduser`` cannot be used and the reason is the bug itself: it expands against the
    environment of whoever is running ``grid leave``, so another account's ``~/.grid`` would resolve
    to ours and be swept. Only a bare ``~`` is expanded — ``~someone/.grid`` names an account whose
    home directory we would have to look up, and a lookup that can be wrong is worse here than an
    honest unknown.
    """
    if raw.startswith("~"):
        rest = raw[1:]
        if rest and not rest.startswith("/"):
            return None  # `~user/...` — not ours to resolve
        home = env.get("HOME")
        if not home:
            return None
        raw = home + rest
    if not os.path.isabs(raw):
        return None  # resolved against a cwd we cannot know
    return _canonical(raw)


def _canonical(path: str) -> str:
    """One spelling per directory, so a symlinked or trailing-slash ``GRID_HOME`` is not read as
    somebody else's. Falls back to a lexical normalisation if the filesystem declines to answer —
    a wrong *difference* strands an orphan, so it is worth one syscall to avoid."""
    try:
        return os.path.realpath(path)
    except OSError:
        return os.path.normpath(path)


def _home_from_environ(env: dict[str, str]) -> str | None:
    """The grid home ``env`` describes, canonicalised, or ``None`` when it does not describe one.

    Mirrors ``shared.paths.grid_home``: the variable if set, else ``<HOME>/.grid``. An empty value is
    treated as unset, which is what the shell means by ``GRID_HOME=``.
    """
    raw = env.get(GRID_HOME_VAR) or ""
    if not raw:
        home = env.get("HOME")
        if not home:
            return None
        raw = os.path.join(home, _DEFAULT_HOME_LEAF)
    return _expand(raw, env)


def own_home() -> str | None:
    """This process's grid home, canonicalised the same way, or ``None`` if it cannot be resolved.

    Unresolvable is not a theoretical branch — ``Path.expanduser`` raises when the platform cannot
    name a home directory — and it must answer ``None`` rather than a guess, because a wrong "ours"
    is a kill.
    """
    try:
        return _canonical(str(paths.grid_home()))
    except (OSError, RuntimeError, ValueError):
        return None


def tag_for_home(home: str) -> str:
    """A non-secret, fixed-width identity for one canonical Grid installation path."""
    return hashlib.sha256(_canonical(home).encode("utf-8", "surrogateescape")).hexdigest()


def own_tag() -> str | None:
    """This process's installation tag, or ``None`` when its Grid home cannot be resolved."""
    home = own_home()
    return None if home is None else tag_for_home(home)


def own_tag_arg() -> str:
    """The argv token placed on a detached engine child.

    Spawning without a discriminator would reopen cross-installation teardown on platforms that
    cannot inspect another process's environment, so an unresolvable home is a hard refusal rather
    than a legacy-shaped child.
    """
    tag = own_tag()
    if tag is None:
        raise RuntimeError("could not resolve GRID_HOME for detached engine process isolation")
    return HOME_TAG_ARG_PREFIX + tag


def tag_from_arg(token: str) -> str | None:
    """Parse a well-formed installation argv token; malformed/non-tag tokens answer ``None``."""
    if not token.startswith(HOME_TAG_ARG_PREFIX):
        return None
    value = token[len(HOME_TAG_ARG_PREFIX):]
    return value if _HOME_TAG_RE.fullmatch(value) else None


def home_of(pid: int) -> str | None:
    """``pid``'s grid home, or ``None`` when we could not establish one. The single seam the suite
    patches, so no test decides anything from a real process on the developer's box."""
    env = _read_environ(pid)
    return None if env is None else _home_from_environ(env)


def other_home_pids(pids: Iterable[int]) -> dict[int, str]:
    """Of ``pids``, the ones we can **prove** belong to a different ``GRID_HOME``, mapped to it.

    Proof, never suspicion: a pid whose home is unknown, or whose home matches ours, is absent — so
    a caller that sweeps everything not listed here behaves exactly as it did before this module
    existed on every platform and every pid where nothing could be established. The home is returned
    with the pid because the caller has to *say* what it spared and why; a bare set would make the
    fix as silent as the bug.
    """
    mine = own_home()
    if mine is None:
        return {}  # cannot say where we live ⇒ cannot prove anyone lives elsewhere
    others: dict[int, str] = {}
    for pid in pids:
        theirs = home_of(pid)
        if theirs is not None and theirs != mine:
            others[pid] = theirs
    return others
