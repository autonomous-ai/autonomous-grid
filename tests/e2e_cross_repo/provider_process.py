#!/usr/bin/env python3
"""A real provider process: this repo's own `remote.tasks.task_loop`, nothing simulated.

Run as a SUBPROCESS by the E2E rather than as a thread, for one reason that matters: a provider that
dies mid-task has to actually die. `kill -9` on a thread does not exist, and a renewer that is merely
asked to stop is a provider tidying up — the opposite of the failure ADR 0032 D-c exists to survive.

Everything it needs arrives through the environment, so the driver can start several of these with
different identities against one relay.
"""
from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.environ["GRID_REPO"])


class _State:
    """The five things `task_loop` and its callees read off a serve state.

    Deliberately not `remote.serve._ServeState`: constructing one drags in engine discovery and a
    registered node, none of which this plane touches. What IS real is everything below the state —
    `claim_once`, `_run_and_report`, the lease renewer, the publisher, git, the child.
    """

    def __init__(self) -> None:
        self.signaling_url = os.environ["GRID_SIGNALING_URL"]
        self.node_id = os.environ["GRID_NODE_ID"]
        self._token = os.environ["GRID_TOKEN"]
        self.stop = threading.Event()
        self.tasks_stop = threading.Event()

    def token(self) -> str:
        return self._token

    def refresh(self, stale_token: str | None = None) -> bool:
        # No control plane here. A provider that cannot refresh is a real configuration, and the
        # honest answer is "no" — never a second copy of the same token, which would make a 401 loop.
        return False


def main() -> int:
    from remote import task_lease, tasks

    # The relay under test runs a seconds-scale lease so a reclaim can be observed without waiting
    # two minutes. The RATIO is what matters (D-c: a TTL several times the renewal interval), so the
    # provider's cadence is scaled by the same factor rather than the test pretending 30s fits in 3s.
    #
    # Forced at CONSTRUCTION, not by rebinding `RENEW_INTERVAL_SECONDS`. That constant is a
    # keyword-only DEFAULT, bound when the class body executed, so assigning to the module attribute
    # afterwards changes nothing about renewers built later — the cadence stays 30s while the code
    # reads as though it were 0.5s. `tests/test_task_agent.py` records the same trap. It cost a
    # green-looking run here: every beat-driven `task.tree` silently disappeared, and the tasks that
    # were short enough to finish inside one lease TTL all still passed.
    renew_seconds = float(os.environ["GRID_RENEW_SECONDS"])

    class _ScaledRenewer(task_lease.LeaseRenewer):
        def __init__(self, state, task_id, *, interval=None, on_beat=None):
            super().__init__(state, task_id, interval=renew_seconds, on_beat=on_beat)

    task_lease.LeaseRenewer = _ScaledRenewer
    task_lease.RENEW_INTERVAL_SECONDS = renew_seconds

    state = _State()
    # How many turns this provider runs AT ONCE (ADR 0034 D-b, issue 40). **Default 1**, and that
    # default is load-bearing rather than conservative: `test_09` proves a cancel really stopped an
    # agent by observing that a second task becomes claimable, which only means anything while this
    # process has exactly one worker. Every test that does not ask for concurrency must keep it.
    #
    # One `task_loop` per worker, on its own thread, sharing one `_State` — which is what
    # `remote/serve.py` does for `GRID_MAX_TASKS`. Not a rebinding of that variable: this file
    # deliberately does not go through `serve`, so reading it here would name a knob nothing in this
    # process consults.
    workers = int(os.environ.get("GRID_E2E_TASK_WORKERS", "1"))
    print(f"provider {state.node_id} up ({workers} worker(s))", file=sys.stderr, flush=True)
    if workers == 1:
        tasks.task_loop(state)
        return 0

    threads = [threading.Thread(target=tasks.task_loop, args=(state,), daemon=True)
               for _ in range(workers)]
    for thread in threads:
        thread.start()
    # Joined rather than left to the process exiting: a daemon thread mid-task would be killed
    # without its lease being given up, which is a different failure from the one any test here is
    # about. `task_loop` returns when `stop`/`tasks_stop` is set, and the harness ends this process
    # with a signal, so in practice this blocks until then.
    for thread in threads:
        thread.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
