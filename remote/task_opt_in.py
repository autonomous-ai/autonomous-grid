"""Whether this provider claims distributed tasks, and how many at once (ADR 0032, issue 09).

Its own module because **two processes ask**. The serve child asks at startup, which is where these
answers began (`remote/serve.py`); the CLI parent asks before it spawns that child, so `grid join`
can tell an operator their task configuration is broken while they are still looking at the terminal
instead of after a member's task has died on it.

The parent cannot get them from `serve`: that module is the provider runtime and pulls httpx and the
whole relay client with it. But the import weight is the smaller reason. The real one is that the
alternative — the parent reading `GRID_TASKS` for itself — is a **second reading of one rule**, and
two readings of one rule get edited apart while every test stays green. `tests/test_task_opt_in.py`
enforces that there is exactly one of each, by scanning the source rather than by asking nicely.
"""
from __future__ import annotations

import os
import sys

SERVING_ENV = "GRID_TASKS"
WORKERS_ENV = "GRID_MAX_TASKS"

# The count that changes nothing. Turning task serving on may not also change how much of the
# operator's subscription it spends, so the pool starts where it has always been and only the
# operator moves it. Deliberately NOT a benchmarked ceiling — the ceiling that matters is the
# subscription's own, read at runtime by `remote/task_capacity.py` (ADR 0032 issue 09).
DEFAULT_WORKERS = 1


def serving_enabled() -> bool:
    """Whether this provider claims distributed tasks (ADR 0032). Opt-in, and off by default.

    Read from the environment at serve time rather than baked into the run record: the detached
    serve child inherits the parent's environment (`cli/remote_provider._spawn_remote_engine` passes
    ``{**os.environ}``), so ``GRID_TASKS=1 grid join …`` reaches the child that way. Opt-in is not a
    convenience — a task loop spends the operator's own agent subscription, so it may never turn
    itself on.
    """
    return os.getenv(SERVING_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def worker_count() -> int:
    """How many tasks this provider runs at once (ADR 0032 issue 09).

    Misconfiguration falls back rather than failing, the same rule `tasks.task_timeout()` states: a
    provider that refused to serve tasks because an operator typed `three` would take task serving
    down for the life of the process, which is a far worse answer than running with the default and
    saying so.

    There is **no upper clamp**, and that is deliberate. A number picked here would be exactly the
    guessed constant this issue exists to remove, and it would be guessed about the wrong thing —
    what binds is the operator's own subscription, not this process's opinion of their machine. The
    machine's real limit is discovered instead: a thread that cannot start is reported, and the
    workers that did start keep serving.
    """
    raw = (os.getenv(WORKERS_ENV) or "").strip()
    if not raw:
        return DEFAULT_WORKERS
    try:
        count = int(raw)
    except ValueError:
        count = 0
    if count < 1:
        print(f"\n[tasks] {WORKERS_ENV}={raw!r} is not a positive whole number of tasks; "
              f"using {DEFAULT_WORKERS}", file=sys.stderr)
        return DEFAULT_WORKERS
    return count
