from __future__ import annotations

import errno
import os
import stat
import sys
from pathlib import Path

_IS_WINDOWS = sys.platform == "win32"

# The mode a directory under ``GRID_HOME`` is created with: ``0o777`` minus the shared-write bits.
# The umask still applies on top and can only make it *stricter* (``umask 077`` lands ``0o700``), so
# the operator keeps every say over the read bits while the one bit that decides deletion is not
# theirs to hand out. Passing it to ``os.mkdir`` rather than chmod-ing afterwards is deliberate:
# there is then no window in which the directory exists group-writable.
_DIR_CREATE_MODE = 0o755

# What a repair takes away from a directory that already exists — and the only thing it ever touches.
_SHARED_WRITE_BITS = 0o022


def grid_home() -> Path:
    return Path(os.getenv("GRID_HOME", "~/.grid")).expanduser()


def _note_unhardened(directory: Path, problem: str) -> None:
    """Say on stderr that one directory could not be made unshared-writable.

    Best-effort must not mean invisible. The repair is deliberately non-fatal — an unprivileged
    `grid leave` has to reap its own serve child even over a tree it does not own — but with nothing
    printed, no return value and no counter, a **partially** hardened chain is indistinguishable
    from a fully hardened one, and the topology that produces it (`sudo grid join` then an
    unprivileged `grid leave`) is named in ADR 0027 as the steady state rather than an edge case.
    Same shape and same reason as ``run_records.known_grid_ids``, which prints before it answers a
    degraded value.

    Quiet in the ordinary case by construction, so this is not a caveat nobody reads: a repair is
    only *attempted* when the directory is already group- or other-writable, so a note means an
    exposure was found **and** could not be closed — rare, and exactly what an operator needs told.

    ``problem`` carries the whole explanation, consequence included, because the three sites that
    call this establish genuinely different things — one knows the directory is exposed, one could
    not find out, and one is not talking about a mode at all.
    """
    print(f"Note: Grid did not harden {directory} — {problem}.", file=sys.stderr)


def _ensure_unshared_dir(directory: Path) -> None:
    """Create one directory unshared-writable, or take those bits off an existing one.

    Read/traverse bits are never touched in either direction. Narrowing them would take *listing*
    away from a second account on a shared ``GRID_HOME`` (``remote/CONTEXT.md``: `sudo grid join`
    then an unprivileged `grid leave`), and ``Path.glob`` swallows the resulting ``PermissionError``
    — so ``run_records.read_records`` would answer a silent ``{}`` where today the ``0o600`` record
    fails loudly through ``jsonio.load_json``. A silent ``{}`` is the record-less-orphan fingerprint
    this feature reads as an alarm, so hardening must not manufacture one.

    A failed repair is **non-fatal but never silent**: the directory may belong to another account,
    and a `grid leave` that cannot chmod somebody else's tree must still tear its own child down —
    the same best-effort shape as ``remote.credentials.save_credentials``'s
    ``grid_home().chmod(0o700)`` — so it notes the failure on stderr and carries on rather than
    swallowing it (``_note_unhardened``). A failed **mkdir** still raises: that caller genuinely
    cannot write.

    Windows is skipped entirely: ``mkdir``'s mode argument is ignored there and ``os.chmod`` only
    toggles the read-only bit, the same reason ``jsonio.atomic_write_bytes`` documents for files.
    """
    try:
        os.mkdir(directory, _DIR_CREATE_MODE)
        return  # freshly created — the umask has already had its say and nothing to repair
    except FileExistsError:
        pass
    if _IS_WINDOWS:
        return
    try:
        entry = directory.stat()
    except OSError as exc:
        # "The caller's own write will report it" is only reliably true when the directory has
        # *vanished* — then the write ENOENTs loudly. A transient that hits this one `stat` on a
        # directory that is otherwise fine (a flaky network-mounted GRID_HOME) skips the hardening
        # for this pass and the write then succeeds normally, so this is the only place it can be
        # said at all.
        _note_unhardened(
            directory,
            f"its mode could not be read ({exc}), so whether other accounts can write it — "
            "and delete the run records in it — is unknown",
        )
        return
    if not stat.S_ISDIR(entry.st_mode):
        # A FILE where a directory belongs — what `mkdir(parents=True, exist_ok=True)` raises for, so
        # re-raise rather than silently repairing a file's mode.
        #
        # Decided from the ``stat`` we already hold rather than a second ``Path.is_dir()`` call,
        # because that one's error handling is not a fixed thing to reason about: on 3.12 it re-raises
        # anything outside ENOENT/ENOTDIR/EBADF/ELOOP (so an EACCES from a concurrently-narrowed
        # parent escapes this function uncaught — precisely the "must still tear its own child down"
        # case the repair below is careful about), while 3.13+ routes it through ``posixpath.isdir``,
        # whose blanket ``except OSError`` answers *False* for the same directory. ``requires-python``
        # is ``>=3.11``, so both are live. One ``stat``, one place that decides what its failure means.
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(directory))
    mode = stat.S_IMODE(entry.st_mode)
    if mode & _SHARED_WRITE_BITS:
        try:
            os.chmod(directory, mode & ~_SHARED_WRITE_BITS)
        except OSError as exc:
            # The one case we can state with certainty: the directory IS shared-writable and we did
            # not fix it. `chmod` is atomic, so a failed attempt leaves the mode exactly as found —
            # never worse — but leaving it *unsaid* is what would make a partially hardened chain
            # indistinguishable from a fully hardened one, in the one topology this whole ADR is
            # about (a run tree owned by another account).
            _note_unhardened(
                directory,
                f"it is writable by other accounts and its mode could not be changed ({exc}); "
                "anyone who can write it can delete the run records in it",
            )


def ensure_dir(path: Path) -> Path:
    """Create ``path`` and every missing ancestor, guaranteeing that **no directory under
    ``grid_home()`` is group- or other-writable** — at creation or afterwards.

    The directory-creation primitive for the **run tree** and for every file the hardened atomic
    writer produces, replacing a bare ``mkdir(parents=True, exist_ok=True)`` at those sites.
    ``~/.grid/services/`` and the install trees still use a bare ``mkdir`` — a different threat with
    a larger blast radius (imported Python, launched binaries), tracked as follow-up F21. See
    ADR 0027 for exactly what is covered.

    ``grid_home()`` is re-read on every call, so the tree this belongs to is decided **at call
    time**: a ``Path`` built while ``GRID_HOME`` pointed somewhere else is, correctly, not our tree.

    Why a directory's mode is worth a rule at all (`.scratch/grid-leave/` issue 19): POSIX ``unlink``
    checks write permission on the **containing directory** and consults nothing about the target
    file, so a run record's ``0o600`` does not protect it from deletion. Since the argv sweep, a
    record-less serve child is exactly what a bare `grid leave` reaps — so deleting a record
    escalates from "lose tracking" to "the owner's own leave kills a healthy engine", and because the
    victim issues the kill, the EPERM boundary that stops a cross-uid kill never applies.

    The whole chain is walked root-first rather than handed to ``mkdir(parents=True, mode=…)``,
    which cannot express this: CPython creates the *parents* with the default mode, so only the leaf
    would be covered and ``run``/``run/engines`` would keep whatever the umask gave them.

    A ``path`` **outside** ``grid_home()`` gets today's plain ``mkdir(parents=True, exist_ok=True)``
    and no mode work at all, so this is inert for any caller that is not writing into Grid's own
    tree. The prefix test is a literal one (``relative_to``, never ``resolve``) — every real caller
    composes its path from ``grid_home()``, and resolving would drag symlink semantics into a
    permissions decision.
    """
    home = grid_home()
    try:
        parts = path.relative_to(home).parts
    except ValueError:
        path.mkdir(parents=True, exist_ok=True)  # not our tree — unchanged behaviour
        return path
    if os.pardir in parts:
        # ``relative_to`` is lexical, so ``<home>/a/../../etc`` succeeds and yields a ``..`` part —
        # the walk below would then create and chmod directories OUTSIDE the tree, which is the exact
        # opposite of the paragraph above. No caller can reach this today (both id sources are
        # sanitised: `cli/remote_grid._NETWORK_ID_RE` and `local/runtime.slug_name`), and this is a
        # hardening primitive, so it declines rather than trusts a caller it cannot see.
        #
        # Noted, not silent, for the same reason the two branches below are: "unreachable today" is
        # the kind of claim that ages badly, and this one's blast radius is a superset of theirs —
        # not "still shared-writable inside ~/.grid" but "created outside it entirely, at the ambient
        # umask". Reaching it also means an upstream id sanitiser has broken, which is worth hearing
        # about on its own.
        _note_unhardened(
            path,
            "the path escapes GRID_HOME, which should not be reachable; it was created with the "
            "ambient umask instead",
        )
        path.mkdir(parents=True, exist_ok=True)
        return path
    # Anything ABOVE GRID_HOME is not ours to set a mode on, but it does have to exist before the
    # walk can start (a `GRID_HOME` pointing somewhere several levels deep is legal).
    home.parent.mkdir(parents=True, exist_ok=True)
    current = home
    _ensure_unshared_dir(current)
    for part in parts:
        current = current / part
        _ensure_unshared_dir(current)
    return path


def home() -> Path:
    return grid_home()


def credentials_file() -> Path:
    """Remote-mode credential store (TOML, 0o600). Absent ⇒ signed out."""
    return grid_home() / "credentials.toml"


def device_file() -> Path:
    """Stable per-machine device id (TOML). Survives logout."""
    return grid_home() / "device.toml"


def api_keys_file() -> Path:
    """Machine-local API-engine key store (TOML, 0o600), keyed by service kind. Survives logout —
    deliberately separate from the sign-in credential store (like device.toml)."""
    return grid_home() / "api_keys.toml"


def codex_models_cache_file() -> Path:
    """Grid-side cache of the codex seat's last `GET /models` probe (JSON, 0o600). Written after every
    successful probe (join + `grid catalog --api codex`), read back when offline. The seat's own
    entitlement — carries no token/account id — and survives logout, like api_keys.toml (issue 10b)."""
    return grid_home() / "codex_models_cache.json"


def seat_home(kind: str) -> Path:
    """A CLI seat's own home for the tool it drives (e.g. CODEX_HOME).

    Isolated on purpose: the operator's real home carries their hooks, skills and project trust,
    and a seat serving strangers must run none of them. Sign-in happens INTO this directory, so the
    seat holds its own credential and the operator's own login is never copied or disturbed.
    """
    return grid_home() / "seats" / kind


def grids_dir() -> Path:
    return grid_home() / "grids"


def grid_dir(grid_id: str) -> Path:
    return grids_dir() / grid_id


def ensure_base() -> None:
    ensure_dir(grids_dir())


def bin_dir() -> Path:
    return grid_home() / "bin"


def llama_server_bin() -> Path:
    return bin_dir() / "llama-server"


def llama_prefix_dir() -> Path:
    """Where a prebuilt llama.cpp is unpacked. The macOS binaries link their shared libraries
    through `@loader_path`, so `llama-server` only runs with its `.dylib`s beside it — they get a
    directory of their own, and `bin/llama-server` is a symlink into it."""
    return grid_home() / "engines" / "llama.cpp"


def tools_dir() -> Path:
    """Where `uv` keeps the agent tools it installs for Grid (one venv per tool)."""
    return grid_home() / "tools"


def python_dir() -> Path:
    """Where `uv` downloads the private CPython those tools run on. It lives under ~/.grid so Grid
    owns what Grid installed — and removing ~/.grid removes it too."""
    return grid_home() / "python"


def models_dir() -> Path:
    return grid_home() / "models"


def logs_dir() -> Path:
    return grid_home() / "logs"


def run_dir() -> Path:
    return grid_home() / "run"


def engines_dir(grid_id: str) -> Path:
    return run_dir() / "engines" / grid_id


def llama_log(port: int) -> Path:
    return logs_dir() / f"llama_llm_{port}.log"


def ensure_all() -> None:
    for directory in (grid_home(), grids_dir(), bin_dir(), models_dir(), logs_dir(), run_dir()):
        ensure_dir(directory)
