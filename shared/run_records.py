"""Detached-engine run records — the on-disk handle that ties a `grid join` to its
detached subprocess, shared by both modes.

`grid join` writes one record per engine under
``~/.grid/run/engines/<grid_id>/<engine_id>.json`` (`shared.paths.engines_dir`) and spawns a
detached child; `grid leave` reads it back to SIGTERM that child and remove the file. The record
holds only **non-secret routing** — never a token (remote tokens live in ``credentials.toml``,
``0o600``).

Extracted from ``cli/provider.py`` so the remote serve loop (`remote/serve.py`) and the remote
join/leave handlers (`cli/remote_provider.py`) reuse the exact same record format and teardown
without an ``remote → cli`` back-dependency (DECISIONS D17). Writes go through ``shared.jsonio`` —
the same atomic, ``0o600`` writer ``local/config`` re-exports — so local behaviour is byte-identical.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from shared import jsonio, paths
from shared.filelock import try_file_lock
from shared.models import api_catalog

_IS_WINDOWS = sys.platform == "win32"

# The argv marker of the detached REMOTE serve subprocess: `<cli> __remote-engine <network_id>
# <engine_id>`. The join spawn (`cli/remote_provider._spawn_remote_engine`) builds it and the leave
# orphan sweep (`remote/orphan_sweep`) matches on it, so they share ONE constant and can never drift.
# A third copy is the dispatch literal in `cli/_main.py` (kept in lockstep by hand, like the sibling
# `__engine`/`__server` dispatch keys).
REMOTE_ENGINE_MARKER = "__remote-engine"


# How long ``stop_engine`` waits for a SIGTERM'd child to exit before SIGKILLing its group.
# SIGTERM → SIGKILL escalation budget for a detached engine child. 25 = the serve loop's worker
# drain (5s) + a codex token exchange caught mid-flight (its 15s vendor timeout — remote/serve.py
# waits that exchange out rather than losing a journaled rotation, ADR 0015 D-d) + unregister/
# teardown margin. Costs nothing on healthy exits — the wait below polls `pid_alive` every 0.2s
# and returns the moment the child dies; only a genuinely wedged child feels the longer fuse.
_STOP_GRACE_SECONDS = 25


def record_path(grid_id: str, engine_id: str) -> Path:
    return paths.engines_dir(grid_id) / f"{engine_id}.json"


def write_record(grid_id: str, engine_id: str, record: dict[str, Any]) -> None:
    jsonio.atomic_write_json(record_path(grid_id, engine_id), record)


def read_records(grid_id: str) -> dict[str, dict[str, Any]]:
    root = paths.engines_dir(grid_id)
    if not root.exists():
        return {}
    records: dict[str, dict[str, Any]] = {}
    for path in sorted(root.glob("*.json")):
        data = jsonio.load_json(path)
        if data.get("engine_id"):
            records[data["engine_id"]] = data
    return records


def read_record(grid_id: str, engine_id: str) -> dict[str, Any] | None:
    """One engine's record, or ``None`` — the detached child's lookup of its own routing."""
    return read_records(grid_id).get(engine_id)


def update_record(grid_id: str, engine_id: str, **fields: Any) -> None:
    """Merge fields into an existing engine record; no-op if the record is already gone."""
    record = read_records(grid_id).get(engine_id)
    if record is None:
        return
    record.update(fields)
    write_record(grid_id, engine_id, record)


def match_engine(
    specs: list[dict[str, Any]],
    selector: str,
    *,
    label: str,
    summary: str,
    hint: str = "pass the exact endpoint URL instead",
) -> list[dict[str, Any]]:
    """Engine spec(s) a `grid leave --engine <selector>` picks out of ``specs``, tried in order: exact
    ``endpoint_url`` → exact ``engine_label`` → a served model → an ``endpoint_url`` substring. Each match
    must resolve to exactly ONE engine or it raises ``SystemExit`` (naming ``summary`` and ``hint``);
    returns ``[]`` on no match so the caller raises its own not-found. Returned dicts are the SAME objects
    passed in — identity is preserved for an ``id()``-based drop filter. An exact engine-*id* match is the
    caller's job BEFORE this (remote keys engines by URL/label; local by record id). ``hint`` is the
    disambiguation instruction, per mode: remote points at the endpoint URL, local at the engine id."""
    if not selector:  # defensive: an empty selector is a substring of every URL — never "match all"
        return []

    def unique(matches: list[dict[str, Any]], how: str) -> list[dict[str, Any]]:
        if len(matches) > 1:
            raise SystemExit(
                f"{how} {selector!r} matches several engines on {label}; {hint}. Engines: {summary}."
            )
        return matches

    by_url = unique([s for s in specs if s.get("endpoint_url") == selector], "URL")
    if by_url:
        return by_url
    by_label = unique([s for s in specs if s.get("engine_label") == selector], "Label")
    if by_label:
        return by_label
    by_model = unique([s for s in specs if selector in (s.get("models") or [])], "Model")
    if by_model:
        return by_model
    return unique([s for s in specs if selector in (s.get("endpoint_url") or "")], "URL fragment")


def media_signature(record: dict[str, Any]) -> tuple[bool, tuple[str, ...], int, int]:
    """A comparable fingerprint of an identity's media config (on/off, bundles, ports). A SIGHUP
    hot-reload can't bring media up/down or swap bundles, so ``grid join``/``leave`` (CLI) and the serve
    loop's reload both compare this to choose hot-reload vs respawn — ONE definition so the two decisions
    can never desync (ADR 0010 C3)."""
    return (
        bool(record.get("media")),
        tuple(sorted(record.get("media_bundles") or [])),
        int(record.get("comfyui_port") or 8188),
        int(record.get("media_port") or 8190),
    )


# Poll-worker default for an identity that serves ONLY API engines: the upstream is a hosted API,
# so several consumers must not queue behind one worker while it sits idle (ADR 0012). Any hardware
# engine (or media) in the union keeps the conservative default of 1.
API_ONLY_DEFAULT_CONCURRENCY = 8


def effective_max_concurrency(record: dict[str, Any]) -> int:
    """The poll-worker count a record's identity runs and advertises.

    An explicit ``--max-concurrency`` (stored truthy on the record) always wins; otherwise the
    default is derived from the union: 8 when every engine spec is an API engine and the identity
    serves no media — **unless the union contains a codex engine, which pins the default to 1**
    (ADR 0015 D-f: a codex seat is a flat-rate subscription; eight workers would drain the
    operator's personal monthly allowance eight-wide by default) — else 1. Like
    ``media_signature``, this is the ONE definition shared by the CLI's hot-reload-vs-respawn
    choice and the serve loop's startup, so the two can never desync (ADR 0010 C3). A legacy flat
    record (no ``engines`` field) keeps the default of 1.
    """
    explicit = record.get("max_concurrency")
    if explicit:
        return int(explicit)
    engines = record.get("engines") or []
    if any(spec.get("api_kind") == api_catalog.CODEX_KIND for spec in engines):
        return 1  # a flat-rate seat is never hammered eight-wide by default (ADR 0015)
    api_only = bool(engines) and all(spec.get("api_kind") for spec in engines)
    return API_ONLY_DEFAULT_CONCURRENCY if api_only and not record.get("media") else 1


def _win_pid_alive(pid: int) -> bool:
    """Windows liveness probe. POSIX's ``os.kill(pid, 0)`` is unusable here: on Windows signal 0 is
    ``CTRL_C_EVENT``, so ``os.kill(pid, 0)`` tries to signal a console group rather than test for
    existence — it never reports "alive", so ``terminate_pid`` would skip the kill and orphan the
    child. Query the process's exit code instead: ``STILL_ACTIVE`` (259) means it's running."""
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False  # no such pid (OpenProcess fails with ERROR_INVALID_PARAMETER)
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


# The largest value any OS hands out as a pid fits in a C int. Two things go wrong when a record's
# drifted/corrupt pid escapes that range, and both are worse than "dead": a pid above it makes
# ``os.kill`` raise **OverflowError** — an ArithmeticError, not an OSError, so no caller's handler
# catches it and every pid-reading path (the leave terminator, the join live-record filter, the
# orphan sweep) crashes instead of reading "not alive"; a **negative** pid is not a process at all —
# ``os.kill(-N, sig)`` addresses process GROUP N, so a drifted negative pid would make
# ``terminate_pid`` SIGTERM an unrelated group. Neither can name a live engine child, so both answer
# "not alive" here, before any syscall, and `terminate_pid`'s dead-probe short-circuit then makes it
# a no-op rather than a stray signal.
_PID_MAX = 2**31 - 1


def pid_alive(pid: int) -> bool:
    if not pid or pid < 0 or pid > _PID_MAX:
        return False
    if _IS_WINDOWS:
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False  # ESRCH: no such process
    except PermissionError:
        return True  # EPERM: the process exists, it's just owned by another uid — reporting it dead would
        # let a join spawn a second engine under the same token node_id and clobber it.
    except OSError:
        return False


def kill_group(pid: int) -> None:
    if _IS_WINDOWS:
        # No process groups or SIGKILL on Windows. `taskkill /T` tears down the whole child tree
        # (the uv launcher shim → the real interpreter that holds the callback port), which is what
        # `killpg` achieves on POSIX. `/F` forces; a dead pid just yields a non-zero exit we ignore.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            check=False,
        )
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def terminate_pid(pid: int) -> bool:
    """SIGTERM a detached engine child and wait for it to exit, escalating to SIGKILL of its process
    group after the grace window. Does **not** touch any run record — the caller decides whether to
    remove it (a `grid leave` teardown) or keep it (a respawn that rewrites the record in place, so the
    engine child unregisters + stops what it launched, but the merged record survives). A ``0``/dead pid
    is a no-op.

    Returns whether the process is confirmed gone. ``False`` means it survived even SIGKILL — the caller
    must NOT spawn a replacement, because two live children on one token-pinned relay node_id clobber
    each other (the exact bug this whole flow exists to prevent).
    """
    if not (pid and pid_alive(pid)):
        return True
    if _IS_WINDOWS:
        # No process groups and no deliverable SIGTERM to a detached, console-less child on Windows,
        # so a graceful term-then-escalate has nothing to escalate from — and killing only `pid`
        # would orphan the interpreter child that actually holds the callback port. `taskkill /T`
        # tears the whole tree down at once, the outcome the POSIX path reaches via SIGTERM→killpg.
        kill_group(pid)
        return not pid_alive(pid)
    # SIGTERM the detached engine so it unregisters and stops anything it started.
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.time() + _STOP_GRACE_SECONDS
    while time.time() < deadline and pid_alive(pid):
        time.sleep(0.2)
    if pid_alive(pid):
        kill_group(pid)
    return not pid_alive(pid)


def discard_own_record(grid_id: str, engine_id: str) -> bool:
    """Drop the run record of an engine child that is exiting **before it ever registered** — unless
    that record now points at a *different, live* process. Returns whether it removed the record.

    The cleanup itself is old (a media engine whose ComfyUI never became ready must not leave a ghost
    record behind that only `grid leave --all` can clear); the ownership check is the fix from the
    record-deletion audit (`.scratch/grid-leave/` issue 05). The record is keyed by
    ``(grid_id, engine_id)``, not by process, so a child that unlinks it blindly on the way out can
    delete a record that a NEWER child now owns: a spawner acting on a stale-dead recorded pid writes
    a fresh record and starts child B while the pre-registration child A is still alive (wedged in
    bring-up, which is measured in minutes — see the PRD's field confirmation #2); when A finally
    dies, its exit reap unlinks B's record. That is precisely the orphan this feature exists to
    prevent — a live serve child with no record, untracked and (before issue 02's argv sweep)
    unkillable via the CLI.

    So: delete only what is provably ours (``os.getpid()`` — or our **parent's** pid, because the
    Linux distribution is a Nuitka ``--onefile`` binary whose bootstrap unpacks the real executable
    and stays its parent, so ``proc.pid`` — what the spawner writes into the record — is the
    bootstrap's, not ours; same shape as a Windows launcher shim, and the reason the remote child
    self-stamps at all) or provably dead (a drifted pid nobody is behind, the join write-race ``0``
    included). A record whose pid is alive and not ours is left alone: at
    worst a ghost record survives, which the next join rewrites and `grid leave` unlinks — recoverable,
    unlike stranding a live child. ``pid_alive`` reports a zombie as alive (grid-leave follow-up F6),
    which biases this the safe way: keep the record.

    Takes the record's ``file_lock`` **non-blockingly**, so this never waits for a holder. In remote
    mode that holder is a `grid join`/`leave` which is authoritative over the record anyway (leave
    unlinks it once our death is confirmed; join rewrites it), and blocking would stall leave's
    teardown for its whole 25s stop grace before SIGKILLing us mid-cleanup. Local mode takes no
    record lock at all, so contention there means something unexpected — hence the note on stderr.

    **Never raises** (bar ``KeyboardInterrupt``, which is how a SIGTERM'd serve child exits cleanly).
    It runs inside the ``finally`` of an engine that is already dying of something else, and an
    exception raised there *replaces* the operator's real failure reason — Python demotes the
    original to ``__context__``, which nothing prints. Reading the record alone can raise
    ``SystemExit`` (``jsonio`` turns a corrupt file into one, and ``SystemExit`` is not even an
    ``Exception``), so every failure is reported on stderr — the child's own log — and swallowed.
    Each decision that keeps a record says why there, too: this cleanup is otherwise invisible, and
    "a newer live child owned my record" is exactly the event a future orphan investigation needs.
    """
    try:
        return _discard_own_record(grid_id, engine_id)
    except (Exception, SystemExit) as exc:  # never mask the real exit error; KeyboardInterrupt flows on
        print(f"Could not reap the run record for {engine_id}@{grid_id} (ignoring): {exc}", file=sys.stderr)
        return False


def _recorded_pid(record: dict[str, Any]) -> int | None:
    """A record's ``pid`` as something safe to reason about: ``0`` when it was never stamped (absent
    or null — the value the join write-race leaves behind), the pid itself when it is a plain
    in-range process id, and ``None`` when the field is any other shape.

    ``None`` means *we can prove nothing about it*, which for a deletion decision must fail closed.
    ``int(record.get("pid") or 0)`` cannot express that: it coerces ``[]``/``{}``/``0.0``/``false``
    to the "never stamped" ``0`` — the **delete** branch — and passes an out-of-range int straight
    through to ``os.kill``.
    """
    pid = record.get("pid")
    if pid is None:
        return 0
    if isinstance(pid, bool) or not isinstance(pid, int):
        return None
    return pid if 0 <= pid <= _PID_MAX else None


def _discard_own_record(grid_id: str, engine_id: str) -> bool:
    path = record_path(grid_id, engine_id)
    with try_file_lock(path) as acquired:
        if not acquired:
            print(
                f"Left the run record for {engine_id}@{grid_id} alone: another process holds its lock.",
                file=sys.stderr,
            )
            return False
        # Load OUR file by path rather than `read_record`, which globs and parses the whole grid
        # directory — one corrupt *sibling* record (a legacy `engine-<uuid>.json`) would otherwise
        # decide the fate of ours.
        record = jsonio.load_json(path)
        if not record:
            return False  # already gone (a concurrent leave), or empty — nothing to reap, nothing to say
        pid = _recorded_pid(record)
        if pid is None:
            print(
                f"Kept the run record for {engine_id}@{grid_id}: its pid field ({record.get('pid')!r}) "
                "is not a process id, so it can't be proven stale.",
                file=sys.stderr,
            )
            return False
        if pid and pid not in (os.getpid(), os.getppid()) and pid_alive(pid):
            # A newer child owns this record now — unlinking it would strand that live child
            # untracked. Say so: this line is the only trace that two children existed.
            print(
                f"Kept the run record for {engine_id}@{grid_id}: it points at live pid {pid}, "
                "not this process.",
                file=sys.stderr,
            )
            return False
        path.unlink(missing_ok=True)
        return True


def stop_engine(grid_id: str, engine_id: str, record: dict[str, Any]) -> int:
    """SIGTERM the detached engine child so it unregisters + tears down, then drop its record — but
    only when the child is **confirmed gone**. Escalates to SIGKILL of the process group if it does
    not exit within the grace window.

    Returns the surviving pid, or ``0`` when the child is confirmed gone. A child that survives even
    SIGKILL keeps its record — so a retried ``grid leave`` still has a handle and the caller can fail
    loudly naming the pid — the honest teardown that stops leave printing "Left …" over a live child.
    """
    pid = int(record.get("pid") or 0)
    if terminate_pid(pid):
        record_path(grid_id, engine_id).unlink(missing_ok=True)
        return 0
    return pid
