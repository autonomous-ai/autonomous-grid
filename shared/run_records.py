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
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from shared import jsonio, paths, process_identity, win_paths
from shared.filelock import file_lock, try_file_lock
from shared.models import api_catalog

_IS_WINDOWS = sys.platform == "win32"

# The argv marker of the detached REMOTE serve subprocess: `<cli> __remote-engine <network_id>
# <engine_id>`. The join spawn (`cli/remote_provider._spawn_remote_engine`) builds it and the leave
# orphan sweep (`shared/orphan_sweep`) matches on it, so they share ONE constant and can never drift.
# A third copy is the dispatch literal in `cli/_main.py` (kept in lockstep by hand, like the sibling
# `__server` dispatch key).
REMOTE_ENGINE_MARKER = "__remote-engine"

# The same, for the detached LOCAL engine subprocess: `<cli> __engine <grid_id> <engine_id>`. Built by
# `cli/provider._spawn_engine`, matched by the same sweep, and — like its remote sibling — a hand-kept
# copy of the `cli/_main.py` dispatch literal. The two markers are distinct whole tokens, so neither
# sweep can ever match the other mode's children.
LOCAL_ENGINE_MARKER = "__engine"

# The engine id of the remote singleton — one record per grid, `engines_dir(<network_id>)/remote.json`,
# and the file every `grid join`/`grid leave`/sign-out serializes its union changes on. It lives here,
# beside the marker above, for the same reason: three modules name it (`cli/remote_provider`, the
# sign-out teardown, the leave backstop's record lookup) and a second literal is a second thing to drift.
REMOTE_IDENTITY = "remote"


# How long ``stop_engine`` waits for a SIGTERM'd child to exit before SIGKILLing its group.
# SIGTERM → SIGKILL escalation budget for a detached engine child. 25 = the serve loop's worker
# drain (5s) + a codex token exchange caught mid-flight (its 15s vendor timeout — remote/serve.py
# waits that exchange out rather than losing a journaled rotation, ADR 0015 D-d) + unregister/
# teardown margin. Costs nothing on healthy exits — the wait below polls `pid_alive` every 0.2s
# and returns the moment the child dies; only a genuinely wedged child feels the longer fuse.
_STOP_GRACE_SECONDS = 25


def record_path(grid_id: str, engine_id: str) -> Path:
    return paths.engines_dir(grid_id) / f"{engine_id}.json"


def heartbeat_path(grid_id: str, engine_id: str) -> Path:
    """The serve child's heartbeat sidecar, beside its record. Its **mtime** is the whole payload: the
    child touches it once per successful heartbeat, at a cadence (30s) that must not take the record
    lock a `grid join`/`leave` serializes on, nor rewrite the record ~2 880 times a day.

    Deliberately **not** ``.json``: ``read_records`` globs ``*.json`` and hands every hit to
    ``jsonio.load_json``, which raises ``SystemExit`` on a bad parse — so a ``.json`` sidecar would
    make one empty file break every record read in the CLI. (``file_lock`` already leaves
    ``<name>.json.lock`` siblings here, so a non-``.json`` neighbour is nothing new.)"""
    return paths.engines_dir(grid_id) / f"{engine_id}.heartbeat"


def remove_record(grid_id: str, engine_id: str) -> None:
    """Drop one engine's on-disk footprint — the record **and** its heartbeat sidecar.

    One function rather than an unlink at each site on purpose: the sidecar has to go wherever the
    record goes, and the sites are scattered (the leave teardown, the child's own died-before-
    registering reap, and two in the respawn path). Enumerating them by hand is exactly how a fifth
    one gets added later without its sidecar — which would leave a heartbeat file with nothing to
    explain it, in the directory whose stray `.log`/`.lock` leftovers were the evidence trail for the
    incident this feature came from."""
    record_path(grid_id, engine_id).unlink(missing_ok=True)
    heartbeat_path(grid_id, engine_id).unlink(missing_ok=True)


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


def own_model_case(grid_id: str, model: str) -> str:
    """``model`` as THIS machine actually advertised it, if it case-insensitively matches one of
    its own served models — never anyone else's, since this machine only ever knows its own true
    case.

    The relay's public overview lowercases every model id for display (`grid engines`/`grid
    models`), so the exact string a reader copies from `grid models`' own output — and from this
    CLI's own `Next:` hint — 404s ("No providers available for this model") when read back for a
    model THIS machine is serving under mixed case (grid-leave issue: reproduced live serving
    `Qwen3.5-2B-Q4_K_M`, listed back as `qwen3.5-2b-q4_k_m`, and rejected verbatim). Used both to
    fix the display itself (`cli/remote_overview.py`) and, as a last-resort backstop, to fix a
    model name right before a request that names it (`cli/remote_request.py`) — a local disk read,
    not a network call, so it costs nothing when there is no mismatch to fix.
    """
    for record in read_records(grid_id).values():
        # `advertise_as` first: that alias — not the record's top-level `models` (the raw filename,
        # e.g. `Qwen3.5-2B-Q4_K_M.gguf`) — is what actually got registered as the servable model id
        # (confirmed live: a filename-cased record still routed under its alias). `models` stays
        # the fallback for an engine joined with no `--advertise-as`, where it IS the real id.
        candidates = list(record.get("advertise_as") or []) + list(record.get("models") or [])
        for served in candidates:
            if served.lower() == model.lower():
                return served
    return model


def known_grid_ids() -> tuple[str, ...]:
    """Every grid id with a run-record **directory** on this box, sorted.

    The only index of what this machine has joined that survives a ``grid logout``: the remote grid
    list lives solely in ``credentials.toml``, which logout deletes. Scoped to directories rather
    than records on purpose — ``read_records`` answers ``{}`` both for a grid never joined and for one
    whose record was unlinked out from under a **live** child, and only the directory tells them
    apart (grid-leave issue 05 found 9 record-less directories, each still holding the child's log).

    A name that is not a directory is skipped rather than raising: this runs inside a sign-out that
    must not fail over a stray file someone dropped in the run tree.

    A missing tree is the ordinary never-joined case and answers ``()`` silently. Any **other**
    ``OSError`` answers ``()`` too — a sign-out must not die of a permissions change — but says so on
    stderr first, because every caller reads "no ids" as "nothing to stop". This is the one primitive in
    this feature with no ``scanned`` flag to carry the difference (``SweepResult.scanned``,
    ``find_orphans``, ``LiveScan.scanned`` all have one), so the note is what stops an unreadable tree
    from being reported as a clean box.
    """
    root = paths.run_dir() / "engines"
    try:
        return tuple(sorted(entry.name for entry in root.iterdir() if entry.is_dir()))
    except FileNotFoundError:
        return ()  # never joined anything — the ordinary case, and not a fault
    except OSError as exc:
        print(f"Note: couldn't list {root} ({exc}); this box's joined grids can't be enumerated.",
              file=sys.stderr)
        return ()


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


def mutate_record(grid_id: str, engine_id: str, mutate: Callable[[dict[str, Any]], None]) -> bool:
    """Read-modify-write one engine's record under its lock, or do nothing if it is gone. Returns
    whether anything was written.

    Three properties, each of which every caller previously had to remember on its own:

    * **Locked.** The CLI merges a join's union under this same lock, so an unlocked write from the
      serve child could lose it (ADR 0010 F3).
    * **Never resurrects.** A record a concurrent `grid leave` just deleted stays deleted. A live
      serve child with no record is the untracked orphan this whole feature exists to prevent, and
      re-creating one from a stale in-memory copy is the other way to manufacture it (issue 05).
    * **Reads OUR file by path**, not through ``read_record``'s whole-directory glob, so one corrupt
      sibling (a legacy ``engine-<uuid>.json``) can't decide the fate of ours.

    It **raises** — on I/O, or on a corrupt record, which ``jsonio`` turns into ``SystemExit``. Every
    caller is best-effort bookkeeping that must not take a serving engine down with it, so each keeps
    its own never-raise wrapper: the warning text belongs where the caller knows what it was doing.
    """
    path = record_path(grid_id, engine_id)
    with file_lock(path):
        record = jsonio.load_json(path)
        if not record:
            return False
        updated = dict(record)
        mutate(updated)
        if updated == record:
            return False  # nothing changed — don't rewrite the file on every healthy tick
        write_record(grid_id, engine_id, updated)
        return True


def match_engine(
    specs: list[dict[str, Any]],
    selector: str,
    *,
    label: str,
    summary: str,
    hint: str = "pass the exact endpoint URL instead",
) -> list[dict[str, Any]]:
    """Engine spec(s) a `grid leave --engine <selector>` picks out of ``specs``, tried in order: exact
    ``endpoint_url`` → exact ``engine_label`` → the ``--name`` given at join (``meta_name``, remote only —
    what `grid engines` prints under NODE) → a served model → an ``endpoint_url`` substring. Each match
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
    # `--name` at join is what `grid engines` shows as NODE — the only thing a member typed
    # themselves, so it is the first thing they try for `--engine` too (grid-leave issue: it
    # silently never matched anything, since a remote identity keys engines by URL/label, not
    # its own display name). Matching every spec under that name here is right, not partial:
    # one identity's engines all share one `--name`, so a name selector always means "all of
    # them" — a multi-engine identity surfaces that honestly via the `unique()` ambiguity error
    # below, pointing at `--all` instead of silently guessing which one was meant.
    by_node = unique([s for s in specs if s.get("meta_name") == selector], "Node")
    if by_node:
        return by_node
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
API_ONLY_DEFAULT_CONCURRENCY = 4


def effective_max_concurrency(record: dict[str, Any]) -> int:
    """The poll-worker count a record's identity runs and advertises.

    An explicit ``--max-concurrency`` (stored truthy on the record) always wins; otherwise the
    default is derived from the union: 8 when every engine spec is an API engine and the identity
    serves no media — **unless the union contains a FLAT-RATE seat (codex, or a CLI seat), which pins it to 1**
    (ADR 0015 D-f: a codex seat is a flat-rate subscription; eight workers would drain the
    operator's personal monthly allowance four-wide by default) — else 1. Like
    ``media_signature``, this is the ONE definition shared by the CLI's hot-reload-vs-respawn
    choice and the serve loop's startup, so the two can never desync (ADR 0010 C3). A legacy flat
    record (no ``engines`` field) keeps the default of 1.
    """
    explicit = record.get("max_concurrency")
    if explicit:
        return int(explicit)
    engines = record.get("engines") or []
    if any(api_catalog.kind_is_flat_rate(str(spec.get("api_kind") or "")) for spec in engines):
        return 1  # a flat-rate seat is never hammered four-wide by default (ADR 0015 D-f)
    api_only = bool(engines) and all(spec.get("api_kind") for spec in engines)
    return API_ONLY_DEFAULT_CONCURRENCY if api_only and not record.get("media") else 1


def _win_pid_alive(pid: int) -> bool:
    """Windows liveness probe. POSIX's ``os.kill(pid, 0)`` is unusable here: on Windows signal 0 is
    ``CTRL_C_EVENT``, so ``os.kill(pid, 0)`` tries to signal a console group rather than test for
    existence — it never reports "alive", so ``terminate_pid`` would skip the kill and orphan the
    child. Query the process's exit code instead: ``STILL_ACTIVE`` (259) means it's running."""
    import ctypes
    from ctypes import wintypes

    still_active = 259
    error_invalid_parameter = 87
    # Shared with the identity probe, which asks the same library for overlapping calls; the helper
    # is also where their signatures are declared, so an undeclared `OpenProcess` cannot truncate a
    # HANDLE differently in one of them than the other (grid-leave issue 08).
    kernel32 = process_identity.win_kernel32()
    handle = kernel32.OpenProcess(process_identity.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Only ERROR_INVALID_PARAMETER means "no such pid". Every other failure — ERROR_ACCESS_DENIED
        # for another user's or a protected process — means the process EXISTS and we are simply not
        # allowed to look, so reporting it dead would let `terminate_pid` short-circuit and claim it
        # reaped something still running. That is the false success this whole feature exists to end,
        # and it is the direction the POSIX branch already chose (EPERM → alive). The access right
        # asked for is *grantable* across users, which is not the same as granted.
        return ctypes.get_last_error() != error_invalid_parameter
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True  # opened it but couldn't read it — uncertain, so not "dead"
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
# a no-op rather than a stray signal. ONE definition, shared with the process-state probes that
# guard the same syscalls from the other side (``shared.process_identity``).
_PID_MAX = process_identity.PID_MAX


def pid_alive(pid: int) -> bool:
    # Negative values have process-group semantics on POSIX (``kill(-1, ...)`` is especially
    # dangerous), so they are never valid detached-child identities.
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or pid > _PID_MAX
    ):
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


def process_command_args(pid: int) -> tuple[str, ...] | None:
    """Return a live process's argv, or ``None`` when it cannot be proved safely.

    This is an identity check, not a best-effort display helper: callers use it before signaling a
    detached process.  An unavailable command line therefore fails closed instead of guessing that
    a reused PID still belongs to Grid.
    """

    if not pid_alive(pid):
        return None
    if not _IS_WINDOWS:
        proc_cmdline = Path(f"/proc/{pid}/cmdline")
        try:
            raw = proc_cmdline.read_bytes()
        except OSError:
            raw = b""
        if raw:
            return tuple(os.fsdecode(item) for item in raw.split(b"\0") if item)
        try:
            result = subprocess.run(
                ["ps", "-ww", "-p", str(pid), "-o", "command="],
                capture_output=True,
                check=False,
                text=True,
                timeout=2.0,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        command = result.stdout.strip() if result.returncode == 0 else ""
        if not command:
            return None
        try:
            return tuple(shlex.split(command))
        except ValueError:
            return None

    # Get-CimInstance is present on supported Windows PowerShell installations and returns the
    # original command line, unlike tasklist.  ConvertTo-Json gives Python a dependable argv parser
    # for quoting and spaces.
    script = (
        f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}'; "
        "if ($null -ne $p) { $p.CommandLine | ConvertTo-Json -Compress }"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            check=False,
            text=True,
            timeout=3.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        import json

        command = json.loads(result.stdout)
    except (TypeError, ValueError):
        return None
    if not isinstance(command, str) or not command.strip():
        return None
    # ``CommandLineToArgvW`` has no stdlib wrapper. The allocator identity values are deliberately
    # URL-safe and contain no whitespace, so shlex's non-POSIX tokenizer preserves exact tokens.
    try:
        return tuple(token.strip('"') for token in shlex.split(command, posix=False))
    except ValueError:
        return None


def process_start_marker(pid: int) -> str | None:
    """Stable OS process-birth marker used to reject PID reuse."""

    if not pid_alive(pid):
        return None
    if not _IS_WINDOWS:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            # comm (field 2) is parenthesized and may itself contain spaces or parentheses. The
            # final ')' is the only safe split point; starttime is field 22, index 19 after field 2.
            tail = stat[stat.rfind(")") + 2 :].split()
            if len(tail) > 19:
                return f"proc:{tail[19]}"
        except (OSError, ValueError):
            pass
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "lstart="],
                capture_output=True,
                check=False,
                text=True,
                timeout=2.0,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        value = " ".join(result.stdout.split()) if result.returncode == 0 else ""
        return f"ps:{value}" if value else None

    script = (
        f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}'; "
        "if ($null -ne $p) { [string]$p.CreationDate }"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            check=False,
            text=True,
            timeout=3.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip() if result.returncode == 0 else ""
    return f"win:{value}" if value else None


def process_matches(
    pid: int,
    *,
    required_args: tuple[str, ...],
    start_marker: str | None = None,
) -> bool:
    """Whether ``pid`` is exactly the recorded Grid child, with PID-reuse fencing."""

    argv = process_command_args(pid)
    if argv is None or any(argument not in argv for argument in required_args):
        return False
    if start_marker is None:
        return True
    return process_start_marker(pid) == start_marker


def kill_group(pid: int) -> None:
    if _IS_WINDOWS:
        # No process groups or SIGKILL on Windows. `taskkill /T` tears down the whole child tree
        # (the uv launcher shim → the real interpreter that holds the callback port), which is what
        # `killpg` achieves on POSIX. `/F` forces; a dead pid just yields a non-zero exit we ignore.
        #
        # By ABSOLUTE path: `CreateProcess` searches the current directory too, and a teardown runs
        # wherever the operator is standing, so a bare name lets a planted `taskkill.exe` run as us
        # (grid-leave issue 15, Notes). Every Windows caller of this function is in that blast radius.
        subprocess.run(
            [win_paths.system32_tool("taskkill.exe"), "/F", "/T", "/PID", str(pid)],
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


# How often ``terminate_pid``'s grace loop asks whether the pid has become a *corpse* rather than
# merely whether it still exists. The liveness predicate is ``pid_alive`` — an ``os.kill(pid, 0)``,
# free — but the zombie discrimination costs a ``ps`` fork on macOS (it is a ``/proc`` read on Linux),
# and the loop wakes five times a second. Once per second bounds that at ~25 forks across a full
# 25s grace and ~1 on the measured 0.6s healthy exit, while still ending the wait a second after a
# SIGTERM'd child exits into a container that never reaps it — the case this exists for.
_ZOMBIE_PROBE_INTERVAL_SECONDS = 1.0


def stopped_running(pid: int) -> bool:
    """Whether ``pid`` is no longer a **running** process: gone, or a zombie — exited, but still
    holding a process-table slot because nobody reaped it.

    The question every teardown actually means when it asks ``pid_alive``. Kept separate from
    ``pid_alive`` rather than folded into it: that one answers "does this pid exist", which is what
    the orphan sweep's exclusion logic and 55 monkeypatching tests depend on, and widening it in
    place would change all of them silently (`.scratch/grid-leave/` issue 08).
    """
    return not pid_alive(pid) or process_identity.is_zombie(pid)


def terminate_pid(pid: int, *, identity_check: Callable[[], bool] | None = None) -> bool:
    """SIGTERM a detached engine child and wait for it to exit, escalating to SIGKILL of its process
    group after the grace window. Does **not** touch any run record — the caller decides whether to
    remove it (a `grid leave` teardown) or keep it (a respawn that rewrites the record in place, so the
    engine child unregisters + stops what it launched, but the merged record survives). A ``0``/dead pid
    is a no-op.

    Returns whether the process is confirmed gone. ``False`` means it survived even SIGKILL — the caller
    must NOT spawn a replacement, because two live children on one token-pinned relay node_id clobber
    each other (the exact bug this whole flow exists to prevent).

    A **zombie is already gone**: there is nothing to signal, nothing to wait for, and it is not ours
    to reap (its parent is init, or a container PID 1 that never will). Believing ``pid_alive`` here
    used to cost the full grace, a pointless SIGKILL, and *still* an unconfirmed answer — which aborts
    a union-changing join with "Could not stop the engine(s) …" and, with the honest-teardown gate,
    made a bare `grid leave` fail on every retry forever (the argv sweep can never match a zombie —
    its command line is empty). Measured at 25.3s before this check, milliseconds after.

    Deliberately **token-free**: the orphan sweep hands this pids it matched by argv, which carry no
    identity token by construction. Requiring one here would silently stop the sweep killing anything
    (see the note at ``shared/orphan_sweep.terminate``). Identity is the record-aware caller's job —
    ``terminate_recorded``.
    """
    if not (pid and pid_alive(pid)):
        return True
    if process_identity.is_zombie(pid):
        return True
    if identity_check is not None and not identity_check():
        return False
    if _IS_WINDOWS:
        # No process groups and no deliverable SIGTERM to a detached, console-less child on Windows,
        # so a graceful term-then-escalate has nothing to escalate from — and killing only `pid`
        # would orphan the interpreter child that actually holds the callback port. `taskkill /T`
        # tears the whole tree down at once, the outcome the POSIX path reaches via SIGTERM→killpg.
        if identity_check is not None and not identity_check():
            return False
        kill_group(pid)
        return not pid_alive(pid)
    # SIGTERM the detached engine so it unregisters and stops anything it started.
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + _STOP_GRACE_SECONDS
    next_corpse_probe = 0.0
    while time.monotonic() < deadline and pid_alive(pid):
        if identity_check is not None and not identity_check():
            # The numeric PID either changed owners or can no longer be proven. Never escalate a
            # stale PID into an unrelated process group.
            return False
        now = time.monotonic()
        if now >= next_corpse_probe:
            # Rate-limited, not per-iteration: see `_ZOMBIE_PROBE_INTERVAL_SECONDS`. A child that
            # took our SIGTERM and exited into a container with no reaper stays "alive" to
            # `pid_alive` forever, so without this the wait always runs to the deadline.
            if process_identity.is_zombie(pid):
                return True
            next_corpse_probe = now + _ZOMBIE_PROBE_INTERVAL_SECONDS
        time.sleep(0.2)
    if not stopped_running(pid):
        if identity_check is not None and not identity_check():
            return False
        kill_group(pid)
    return stopped_running(pid)


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
    unlike stranding a live child. A **zombie** owner counts as dead (grid-leave issue 08 closed the
    follow-up this line used to defer): a corpse owns nothing, and in the container regime where this
    cleanup matters most, every dead child is one — so keeping their records would accumulate ghosts.

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


def recorded_pid(record: dict[str, Any]) -> int | None:
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


IDENTITY_FIELDS = ("pid", "pid_start_time", "pgid")
"""The record keys that together name one process verifiably. ``identity_stamp`` is built from this
tuple, so it is enforced rather than decorative.

It exists for the **readers**, not the writer. A writer can derive the key list from
``identity_stamp``'s returned dict and gets a fourth field for free; a reader assembling that dict
from somewhere else (``local.runtime`` reads the grid config's ``server_``-prefixed copy) cannot —
and the obvious substitute, a prefix sweep, would quietly swallow any future unrelated key into a
record the teardown then signals on.
"""


def identity_stamp(pid: int) -> dict[str, Any]:
    """The identity fields a run record carries for its serve child: ``pid`` plus the
    ``(start_time, pgid)`` that make it verifiable. Merge into the record — never write one without
    the others.

    ONE definition for all three writers (the remote spawner, the child's own startup self-stamp, and
    the local join), so they cannot drift; and one atomic record write, which is what lets a verified
    ``pid`` vouch for the ``pgid`` beside it.

    A fact that cannot be read is stored as ``None``, **never** a placeholder. A sentinel would
    compare equal across unrelated processes — turning "we cannot tell" into a confident match at the
    one seam where being wrong means signalling somebody else's process — and would make every record
    written before this existed read as a *mismatch* rather than as unverifiable, which is the
    verdict that withdraws trust.
    """
    # `strict=True` is the enforcement: a field added to IDENTITY_FIELDS without a value here (or the
    # reverse) raises at the first stamp instead of writing a record that is silently short one fact.
    return dict(zip(
        IDENTITY_FIELDS,
        (pid, process_identity.process_start_time(pid), process_identity.process_group_of(pid)),
        strict=True,
    ))


class RecordVerdict(StrEnum):
    """What a run record's ``pid`` field actually names, right now.

    Every seam that used to ask ``pid_alive(record["pid"])`` was asking the wrong question twice over:
    a **zombie** answered "alive", and a **recycled** pid answered "alive" about somebody else's
    process (`.scratch/grid-leave/` issue 08). These five answers are the difference, and the two
    that matter most are the ones that used to be indistinguishable from ``LIVE_OURS``.
    """

    LIVE_OURS = "live-ours"
    """Running, and its start time matches the token stamped beside it. The **only** verdict that may
    be signalled — the identity is proven."""

    LIVE_UNVERIFIED = "live-unverified"
    """Running, but we cannot prove whose it is: the record carries no token (written by an older
    build) or the process could not be read (another user's, no ``ps``). Treated exactly as the CLI
    treated every record before this existed — an upgrade must never be a new failure — so it stays
    "alive" and stays signallable."""

    NOT_OURS = "not-ours"
    """The pid exists — running **or** a corpse — and its start time does not match: it was recycled
    onto an unrelated process. Not our engine, so not alive, and never signalled. `SIGHUP`'s default
    disposition is *terminate*, so signalling here kills one of the operator's own processes, and
    granting this record provenance would also unlock signalling the stale process **group** stamped
    beside the pid. Named for ownership rather than for process state on purpose: the first version
    of this enum sorted by state, and the zombie branch was written without a token check because
    "zombie" did not look like it was answering a question about ownership."""

    ZOMBIE = "zombie"
    """Exited, but still in the process table because nobody reaped it — the container regime this
    issue was filed about. Not alive: the join gate must respawn rather than print "Already serving
    …", and the terminator must confirm death rather than burn its stop grace."""

    DEAD = "dead"
    """No such process — including a record whose ``pid`` was never stamped or is unreadable."""


def record_verdict(record: dict[str, Any]) -> RecordVerdict:
    """Classify a run record's pid. See ``RecordVerdict`` for what each answer means and costs.

    Asks ``pid_alive`` **first** and only then the richer probe. That order is load-bearing twice: it
    keeps ``pid_alive`` the single seam this suite monkeypatches to fabricate liveness, and it keeps
    the expensive question (a ``ps`` fork on macOS) off the path for a pid that plainly does not
    exist.
    """
    pid = recorded_pid(record) or 0
    if not pid or not pid_alive(pid):
        return RecordVerdict.DEAD
    facts = process_identity.process_facts(pid)
    token = record.get("pid_start_time")
    stamped = token if isinstance(token, str) and token else None
    if facts is None or stamped is None:
        # Unreadable process, or a record from before tokens existed. Neither a match nor a
        # mismatch — and a mismatch is the only thing that may withdraw trust. A corpse we cannot
        # identify is still a corpse, so the zombie answer survives the missing token.
        return RecordVerdict.ZOMBIE if facts is not None and facts.is_zombie else RecordVerdict.LIVE_UNVERIFIED
    if facts.start_time != stamped:
        # Checked BEFORE the zombie branch, deliberately. A recycled pid whose new owner has since
        # died is still not ours, and calling it "our zombie" would launder provenance onto the
        # ``pgid`` stamped beside it — a group id that, our own process being long gone, may since
        # have been reused by an unrelated session. Every state has to check the token, not most.
        return RecordVerdict.NOT_OURS
    return RecordVerdict.ZOMBIE if facts.is_zombie else RecordVerdict.LIVE_OURS


def record_alive(record: dict[str, Any]) -> bool:
    """Whether a run record still names a **running serve child of ours** — the replacement for
    ``pid_alive(recorded_pid(record))`` at every seam that trusts a record.

    A zombie is not alive (it serves nothing), and neither is a recycled pid (it is not ours). Both
    used to read as alive, which is how `grid join` came to print "Already serving …; nothing to
    append." over an engine that had been dead for hours.
    """
    return record_verdict(record) in (RecordVerdict.LIVE_OURS, RecordVerdict.LIVE_UNVERIFIED)


def recorded_pgid(record: dict[str, Any]) -> int | None:
    """A record's ``pgid`` as something safe to signal, or ``None`` when it cannot name a real POSIX
    process group — absent (an older build's record), the join write-race's ``0``, negative,
    out of range, or any non-``int`` shape.

    Stricter than ``recorded_pid``, which is allowed to return ``0`` for "never stamped", because the
    consequence of getting this wrong is categorically worse. A pid of ``0`` reaches ``terminate_pid``
    and is a documented no-op; a **pgid** of ``0`` reaches ``os.killpg``, which addresses **the caller's
    own process group** — so `grid leave` would SIGTERM and then SIGKILL itself and the operator's
    shell job. There is no "never stamped" branch here: unusable is unusable, and the group backstop
    simply does not run.

    Why a *stamped* pgid at all, rather than ``os.getpgid(pid)`` at kill time: on macOS that raises
    ``ProcessLookupError`` for a **zombie** (measured), which is exactly the regime the backstop exists
    for. The spawner and the child's self-stamp write ``pgid`` beside ``pid`` in the same atomic record
    write, which is also what lets a verified pid vouch for the group id next to it.
    """
    pgid = record.get("pgid")
    if isinstance(pgid, bool) or not isinstance(pgid, int):
        return None
    return pgid if 0 < pgid <= _PID_MAX else None


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
        pid = recorded_pid(record)
        if pid is None:
            print(
                f"Kept the run record for {engine_id}@{grid_id}: its pid field ({record.get('pid')!r}) "
                "is not a process id, so it can't be proven stale.",
                file=sys.stderr,
            )
            return False
        # `stopped_running`, not `pid_alive`: a **zombie** owner is not a live newer child, it is a
        # corpse, and keeping a record for it strands a ghost that only `grid leave` can clear. In the
        # container regime this cleanup runs in, zombies are the common case (grid-leave issue 08).
        if pid and pid not in (os.getpid(), os.getppid()) and not stopped_running(pid):
            # A newer child owns this record now — unlinking it would strand that live child
            # untracked. Say so: this line is the only trace that two children existed.
            print(
                f"Kept the run record for {engine_id}@{grid_id}: it points at live pid {pid}, "
                "not this process.",
                file=sys.stderr,
            )
            return False
        remove_record(grid_id, engine_id)  # the sidecar goes with it — this child wrote both
        return True


@dataclass(frozen=True)
class Teardown:
    """The outcome of stopping the serve child a run record names.

    ``survivor``/``verified`` answer two different questions, and collapsing them into one integer is
    what made an earlier design unable to express the case this feature cares most about: ``0`` meant
    both *proven clean* and *we could prove nothing*.
    """

    survivor: int = 0
    """A pid — or a **process group id** when ``is_group`` — that is still running and that we failed
    to stop. ``0`` means nothing of ours survived. The caller fails loud and names it."""

    is_group: bool = False
    """``survivor`` names a process group, so the operator's remedy is ``kill -9 -<pgid>``. Printing a
    bare pid there would name the group's leader, which by then does not exist."""

    verified: bool = True
    """We either stopped the recorded child *as ours*, or positively proved nothing of ours is left.
    ``False`` means neither — the record's pid could not be tied to our engine and no process group
    vouched for it. Harmless on its own (the argv sweep is the other net), but a leave that is
    unverified **and** could not read the process table has checked nothing and must not report
    success."""


def describe_survivor(outcome: Teardown) -> str:
    """Name what survived a teardown for what it actually **is** — a pid, or a process *group*.

    Shared by both modes' leave messages, because both now tear down through ``terminate_recorded``
    and either can be handed a group id. Printing one as a "pid" tells the operator to
    ``kill -9 <group leader>``, and the group leader is precisely the process that is already gone:
    the command they are given does nothing and the engine keeps serving.
    """
    return describe_target(-outcome.survivor if outcome.is_group else outcome.survivor)


def describe_target(value: int) -> str:
    """One signalling target named for what it is, from the sign-convention the callers already use: a
    **negative** value is a process *group* (``kill(1)``'s own convention), a positive one a pid.

    The flattened form exists because the remote full leave and the sign-out both carry survivors as a
    plain ``list[int]`` — several targets, each possibly a group — where ``Teardown``'s two fields would
    need a parallel list to survive. One implementation for both shapes: ``describe_survivor`` above is
    this function with the sign applied, so a wording change cannot land in one message and not the other.
    """
    return f"process group {-value}" if value < 0 else f"pid {value}"


def terminate_recorded(record: dict[str, Any]) -> Teardown:
    """Stop the serve child ``record`` names, proving identity before signalling anything.

    Three things this does that ``terminate_pid(record["pid"])`` cannot (`.scratch/grid-leave/`
    issue 08):

    * **A recycled pid is never signalled.** A stale record whose pid now belongs to an unrelated
      process would otherwise take SIGTERM and then a SIGKILL of its whole process group.
    * **A zombie confirms immediately** instead of burning the 25s stop grace and still answering
      "unconfirmed" — the container regime that made bare `grid leave` fail on every retry.
    * **The process group is the backstop when the recorded pid can no longer reach the child.** A
      `grid leave` that wins the record lock before a just-spawned child self-stamps holds the
      *launcher shim*'s pid; the real serve process is a member of that shim's group. A group id is
      not recycled while the group has members, so the stamped ``pgid`` still names our own
      descendants — a reap that needs no readable process table, and therefore works in the one
      regime where the argv sweep cannot.

    The group is **probed** whenever the record carries a usable one (signal 0 delivers nothing, so
    reading is always safe) but only **signalled** when the recorded pid was verifiable — proven ours
    while alive, or a zombie whose token still matches. Once a pid has been reaped there is no way to
    verify a group id at all: ``killpg(pgid, 0)`` proves a group exists, not that it is ours. So an
    ancient record whose pid is long gone is left to the argv sweep and reported ``verified=False``,
    rather than signalling a session we cannot prove we own.
    """
    verdict = record_verdict(record)
    pid = recorded_pid(record) or 0
    pgid = recorded_pgid(record)
    # Which verdicts vouch for the `pgid` stamped beside the pid. `NOT_OURS` and `DEAD` do not: the
    # first names somebody else's process, and after the second nothing can authenticate a group id
    # at all (`killpg(pgid, 0)` proves a group exists, not whose it is, and group ids are reused once
    # empty).
    provenance = verdict in (RecordVerdict.LIVE_OURS, RecordVerdict.LIVE_UNVERIFIED, RecordVerdict.ZOMBIE)

    stopped_as_ours = False
    if verdict in (RecordVerdict.LIVE_OURS, RecordVerdict.LIVE_UNVERIFIED):
        if not terminate_pid(pid):
            return Teardown(survivor=pid, verified=True)  # survived SIGKILL — loud, and it IS ours
        stopped_as_ours = True
    elif verdict is RecordVerdict.ZOMBIE:
        stopped_as_ours = True  # a corpse of ours: already stopped, nothing to signal

    if pgid is None:
        return Teardown(verified=stopped_as_ours)
    if not _left_the_process_table(pid):
        # The group probe is informative only once the recorded pid has left the process table
        # entirely. A **zombie** still occupies its process group, so `group_alive` would keep
        # answering "alive" about our own corpse — and escalating on that signals a group with nothing
        # running in it, burns the full stop grace waiting for a corpse that cannot be reaped, and then
        # reports it as a survivor. That is the very dead-end this issue closes, moved one level up;
        # measured against a real zombie in `tests/test_remote_leave_real_child.py`.
        #
        # So a live sibling hiding behind our corpse is left to the argv sweep, which matches by argv
        # content and can see it — and this is reported **unverified**, because we hold a group id and
        # chose not to read it. Saying otherwise would be the feature's own failure mode: in the
        # container regime it was built for, PID 1 never reaps, so the corpse is permanent and this
        # branch is not a delay but the *only* outcome. Claiming proof there would let `grid leave`
        # print "Left …" with a live `llama-server` still running and the argv sweep — which matches
        # only the `__remote-engine` marker — structurally unable to see it.
        return Teardown(verified=False)
    if not process_identity.group_alive(pgid):
        # Positive evidence, and the only kind available without a process table: our whole session
        # group is empty, so nothing we spawned is still running.
        return Teardown(verified=True)
    if not provenance:
        return Teardown(verified=False)  # a live group we cannot prove is ours — do not signal it
    if terminate_group(pgid):
        return Teardown(verified=True)
    print(
        f"Could not stop every process in serve group {pgid} (try `kill -9 -{pgid}`).",
        file=sys.stderr,
    )
    return Teardown(survivor=pgid, is_group=True, verified=True)


# How long to let a just-stopped pid be cleared out of the process table before judging whether its
# process group tells us anything. A child we terminate is not ours to reap — its parent is init, or
# the container's PID 1 — so there is a short window where it is a zombie and still occupying the
# group. Without this settle the group backstop would decline exactly when it is needed: right after
# stopping the launcher shim, which is when the real serve child is still running.
_REAP_SETTLE_POLLS = 20
_REAP_SETTLE_INTERVAL_SECONDS = 0.05


def _left_the_process_table(pid: int) -> bool:
    """Whether ``pid`` has been reaped — gone entirely, not merely stopped. Allows a bounded settle,
    because whoever reaps it is not us."""
    for _ in range(_REAP_SETTLE_POLLS):
        if not pid_alive(pid):
            return True
        time.sleep(_REAP_SETTLE_INTERVAL_SECONDS)
    return not pid_alive(pid)


def terminate_group(pgid: int) -> bool:
    """SIGTERM a serve child's whole process group, escalating to SIGKILL, and report whether the
    group is empty afterwards. POSIX-only — ``group_alive`` is ``False`` on Windows, where
    ``taskkill /T`` already tears down the tree, so this is never reached there.

    Reached only when the recorded pid is gone but its group still has members: the launcher shim
    exited without taking the serve process with it.

    Guards ``pgid`` itself rather than trusting its caller. ``recorded_pgid`` already refuses every
    value that would make ``killpg`` address something other than a real group — ``0`` above all,
    which is the **caller's own** process group — but this is a module-level function whose entire
    purpose is signalling a group, and a future second caller reaching it without that filter is
    exactly how `grid leave` would come to SIGKILL the operator's shell job. ``group_alive`` carries
    the same belt for the same reason.
    """
    if not process_identity.is_addressable(pgid):
        return False  # not a group id at all — nothing was signalled, and nothing is confirmed
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return not process_identity.group_alive(pgid)
    deadline = time.time() + _STOP_GRACE_SECONDS
    while time.time() < deadline and process_identity.group_alive(pgid):
        time.sleep(0.2)
    if process_identity.group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass  # EPERM (another user's group) or already gone — the probe below is the answer
    return not process_identity.group_alive(pgid)


def stop_engine(grid_id: str, engine_id: str, record: dict[str, Any]) -> Teardown:
    """SIGTERM the detached engine child so it unregisters + tears down, then drop its record — but
    only when the child is **confirmed gone**. Escalates to SIGKILL of the process group if it does
    not exit within the grace window.

    Returns the ``Teardown``: ``survivor`` is ``0`` when the child is confirmed gone, and a child
    that survives even SIGKILL keeps its record — so a retried ``grid leave`` still has a handle and
    the caller can fail loudly naming the pid — the honest teardown that stops leave printing
    "Left …" over a live child. ``verified`` says whether the teardown was *proven*, which is a
    different question from whether anything survived: an unreadable or recycled pid leaves nothing
    to stop and nothing to conclude.

    The identity work lives in ``terminate_recorded``. Its tolerant pid read matters here: this runs
    inside a full leave, ahead of the relay deregister that is the mechanism of record, and a
    hand-edited or corrupt ``pid`` field that raised would take that deregister with it — the model
    would then stay advertised for the whole node TTL, behind a traceback.
    """
    outcome = terminate_recorded(record)
    if not outcome.survivor:
        remove_record(grid_id, engine_id)
    return outcome
