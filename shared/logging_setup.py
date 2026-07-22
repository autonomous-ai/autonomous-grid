"""In-process log rotation + capping for Grid's local-mode child processes.

Ported from the master CLI's ADR 0004 (commit e84704d). The problem is identical here: every
managed subprocess redirects its stdout/stderr into an append-mode ``.log`` that nothing ever
bounds, so a long-running local grid grows ``server.log`` (one line per HTTP request — heartbeats,
health checks) and the engine/media logs without limit until they fill the disk.

Two mechanisms, applied per the process we own:

* The **signaling server** is a uvicorn app we launch ourselves, so it owns ``server.log`` through
  an in-process size-based ``GzipRotatingFileHandler`` (:func:`build_uvicorn_log_config`). True
  rotation, gzipped segments, no external logrotate/cron — ships everywhere the CLI runs.
* **External engines** (llama-server, ComfyUI, the media/remote-engine subprocesses) log on their
  own stdout, which we can't reconfigure. There we bound the raw file with :func:`truncate_if_oversized`
  on every (re)start — it caps growth across restarts, preserving a ~2MB tail to ``<path>.oversized``.
  A single process that runs for weeks can still grow its active file; only in-process handlers can
  rotate live, and these processes aren't ours to instrument.
"""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import sys
from logging.handlers import RotatingFileHandler

from uvicorn.logging import AccessFormatter, DefaultFormatter

# Rotated segments are gzipped at level 1 on purpose: compression runs synchronously on the
# emitting thread (uvicorn's event loop). Level 1 (~0.8s/100MB) stays well under the health-check
# window; level 9 (~4.5s) would risk stalling a rollover past wait_for_health.
_GZIP_COMPRESSLEVEL = 1

# Logs hold request lines / client IPs; create them owner-only.
_LOG_FILE_MODE = 0o600


def _chmod_log(path) -> None:
    try:
        os.chmod(path, _LOG_FILE_MODE)
    except OSError:
        pass


def _gzip_namer(name: str) -> str:
    return name + ".gz"


def _gzip_rotator(source: str, dest: str) -> None:
    """Compress ``source`` into ``dest`` (already carrying ``.gz``).

    Must never raise out of a logging call: on any failure, signal to stderr (never re-enter
    ``logging`` — the handler lock is held) and fall back to a plain rename so the active file is
    still rotated out.
    """
    if not os.path.exists(source):
        return
    tmp = dest + ".tmp"
    try:
        with open(source, "rb") as f_in, gzip.GzipFile(
            filename=tmp, mode="wb", compresslevel=_GZIP_COMPRESSLEVEL
        ) as f_out:
            shutil.copyfileobj(f_in, f_out, length=1024 * 1024)
        _chmod_log(tmp)
        os.replace(tmp, dest)
    except Exception as exc:
        sys.stderr.write(
            f"grid: log compression failed for {source} -> {dest}: {exc!r}; kept plain rename\n"
        )
        try:
            os.remove(tmp)
        except OSError:
            pass
        try:
            os.replace(source, dest)
        except OSError:
            pass
        return
    # Removing the compressed-away source is best-effort and deliberately OUTSIDE the try above:
    # a racing os.remove failure must not fall into the fallback and clobber the good .gz.
    try:
        os.remove(source)
    except OSError:
        pass


class GzipRotatingFileHandler(RotatingFileHandler):
    """A size-based rotating handler whose rotated segments are gzip-compressed.

    Subclassing is required because ``logging.config.dictConfig`` builds a handler from its class
    plus constructor kwargs and has no way to set the ``rotator``/``namer`` attributes.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.namer = _gzip_namer
        self.rotator = _gzip_rotator

    def _open(self):
        stream = super()._open()
        _chmod_log(self.baseFilename)
        return stream


_DEFAULT_PRESERVE_TAIL_BYTES = 2 * 1024 * 1024


def _snapshot_tail_best_effort(path: str, size: int, tail_bytes: int) -> None:
    """Copy the last ``tail_bytes`` of ``path`` to ``<path>.oversized``. Best-effort: the
    disk-freeing truncation is what matters, so a failed snapshot must not block it."""
    if tail_bytes <= 0:
        return
    dest = os.fspath(path) + ".oversized"
    try:
        with open(path, "rb") as f_in:
            if size > tail_bytes:
                f_in.seek(size - tail_bytes)
            with open(dest, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out, length=1024 * 1024)
        _chmod_log(dest)
    except OSError:
        pass


def truncate_if_oversized(
    path: str | os.PathLike[str],
    max_bytes: int,
    *,
    preserve_tail_bytes: int = _DEFAULT_PRESERVE_TAIL_BYTES,
) -> int | None:
    """Truncate ``path`` to 0 if it already exceeds ``max_bytes``; return the old size (so the
    caller can WARN) or ``None`` if nothing was done.

    Snapshots the last ``preserve_tail_bytes`` to ``<path>.oversized`` first — the tail that
    explains the runaway. For the rotating server handler this MUST be called BEFORE the handler
    opens the file: an empty active file makes ``shouldRollover`` return False, so a multi-GB
    legacy file never triggers a synchronous gzip that would stall boot past ``wait_for_health``.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size <= max_bytes:
        return None
    _snapshot_tail_best_effort(os.fspath(path), size, preserve_tail_bytes)
    try:
        with open(path, "w"):
            pass  # truncate in place, keep the inode/path
        _chmod_log(path)
    except OSError as exc:
        # Oversized but un-truncatable (read-only mount, perms) is exactly the disk-fill risk this
        # guard exists to prevent — do NOT stay silent.
        sys.stderr.write(
            f"grid: could not truncate oversized log {os.fspath(path)} ({size} bytes): {exc!r}\n"
        )
        return None
    return size


# --- scoped size limits (env-overridable) ------------------------------------

_SERVER_DEFAULT_MAX_BYTES = 100 * 1024 * 1024
_SERVER_DEFAULT_BACKUP_COUNT = 5
# Engine/media/comfyui logs are truncate-capped (not rotated), so one knob — the cap — is enough.
_ENGINE_DEFAULT_MAX_BYTES = 50 * 1024 * 1024
# Bounds on env overrides. A tiny maxBytes would rotate+gzip on nearly every write (a
# synchronous-rotation storm); an absurd backupCount storms the filesystem. Out-of-range → fallback.
_MIN_LOG_MAX_BYTES = 64 * 1024
_MAX_LOG_BACKUP_COUNT = 100

# Bootstrap/crash channel (server.err): normally near-empty, but a crash-restart loop — or an
# uncaught traceback that writes past `logging` — can grow it. It's a raw FD redirect (not a
# rotating handler), so bound it with the truncate guard on each (re)start.
ERR_LOG_MAX_BYTES = 4 * 1024 * 1024


def _env_int(
    name: str, default: int, *, minimum: int = 1, maximum: int | None = None
) -> int:
    """Read an int env override, falling back to ``default`` on missing/blank/non-int or when
    outside ``[minimum, maximum]``. The clamp matters: a value that would disable rotation
    (``maxBytes`` near 0) or storm the FS (huge ``backupCount``) falls back rather than take effect."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    if value < minimum:
        return default
    if maximum is not None and value > maximum:
        return default
    return value


def server_log_limits() -> tuple[int, int]:
    return (
        _env_int("GRID_SERVER_LOG_MAX_BYTES", _SERVER_DEFAULT_MAX_BYTES, minimum=_MIN_LOG_MAX_BYTES),
        _env_int(
            "GRID_SERVER_LOG_BACKUP_COUNT", _SERVER_DEFAULT_BACKUP_COUNT,
            minimum=1, maximum=_MAX_LOG_BACKUP_COUNT,
        ),
    )


def engine_log_max_bytes() -> int:
    return _env_int("GRID_ENGINE_LOG_MAX_BYTES", _ENGINE_DEFAULT_MAX_BYTES, minimum=_MIN_LOG_MAX_BYTES)


def cap_and_open_append(path, max_bytes: int, *, text: bool = False, buffering: int = -1):
    """Bound an external-process log to ``max_bytes`` (truncate the oversized carry-over, keeping a
    tail in ``<path>.oversized``), then open it in append mode with owner-only perms — the drop-in
    for ``path.open("ab")`` at every subprocess launch site. Returns the open file handle.
    """
    truncate_if_oversized(path, max_bytes)
    fh = open(path, "a" if text else "ab", buffering=buffering)
    _chmod_log(path)
    return fh


# --- uvicorn log formatting/config -------------------------------------------


class UvicornFileFormatter(logging.Formatter):
    """One formatter for the single shared file handler: renders ``uvicorn.access`` records through
    uvicorn's ``AccessFormatter`` and everything else through ``DefaultFormatter``.

    A handler has exactly one formatter, so dispatching here keeps uvicorn's access rendering while
    still writing every logger to one file (single writer = safe rotation). ``%(asctime)s`` is
    prepended (uvicorn's own format carries none) and colours are forced off so no ANSI lands on disk.
    """

    def __init__(self, use_colors: bool = False) -> None:
        super().__init__()
        self._default = DefaultFormatter(
            fmt="%(asctime)s %(levelprefix)s %(message)s", use_colors=use_colors
        )
        self._access = AccessFormatter(
            fmt='%(asctime)s %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
            use_colors=use_colors,
        )

    def format(self, record: logging.LogRecord) -> str:
        try:
            if record.name == "uvicorn.access":
                return self._access.format(record)
        except Exception:
            pass  # non-standard access record -> fall through to the default renderer
        return self._default.format(record)


def build_uvicorn_log_config(
    path: str | os.PathLike[str], *, max_bytes: int, backup_count: int, level: str = "INFO"
) -> dict:
    """dictConfig for ``uvicorn.run(log_config=...)`` — one shared rotating file handler.

    A single ``grid_file`` handler instance is shared across ``uvicorn``/``uvicorn.access``/``root``
    (single writer ⇒ safe rotation); ``propagate: False`` on the uvicorn loggers and a handler-less
    ``uvicorn.error`` (bubbles to ``uvicorn``) mean every record is emitted exactly once. Do NOT also
    pass ``log_level=``/``use_colors=`` to ``uvicorn.run`` — either overrides this config
    (``use_colors`` also KeyErrors on the non-``default`` formatter name).
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"default": {"()": "shared.logging_setup.UvicornFileFormatter"}},
        "handlers": {
            "grid_file": {
                "class": "shared.logging_setup.GzipRotatingFileHandler",
                "filename": str(path),
                "maxBytes": int(max_bytes),
                "backupCount": int(backup_count),
                "encoding": "utf-8",
                "formatter": "default",
            }
        },
        "loggers": {
            "uvicorn": {"handlers": ["grid_file"], "level": level, "propagate": False},
            "uvicorn.error": {"level": level, "propagate": True},
            "uvicorn.access": {"handlers": ["grid_file"], "level": level, "propagate": False},
        },
        "root": {"handlers": ["grid_file"], "level": level},
    }
