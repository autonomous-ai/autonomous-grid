"""The shape of a running task's working directory, published on the heartbeat (ADR 0032 D-f).

Between claim and terminal the repository holds nothing new — the provider commits only at terminal
boundaries (D-e) — so this stream is the **only** live view of a running task. That is what the
snapshot is for: not a nicety on top of the agent's narration, but the one answer to "what has it
actually built so far".

Pushed, never asked for. A "show me the tree" request/response channel would make the wire
bidirectional, obliging a provider saturated with a task to keep answering control traffic and
obliging both ends to correlate replies with requests. Instead a snapshot rides the beat the provider
already sends, and lands in the same event log the client is already reading.

Two properties make that affordable, and both are load-bearing rather than tidy:

  * **An unchanged tree publishes nothing.** The snapshot is hashed and compared with the last one
    PUBLISHED, so a task that spends ten minutes in a test suite adds no traffic at all.
  * **The payload is capped, and says when it was capped.** Real workspaces grow dependency
    directories that would dwarf the event. A truncated tree that admits it is worth far more than a
    heartbeat that becomes megabytes — or, worse, one the relay refuses.
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import task_events, task_repo

# How many paths one snapshot may carry. A secondary bound: the byte budget below is what actually
# protects the wire, and this stops a workspace of ten-thousand one-character names from producing an
# event that is technically small and completely unreadable.
MAX_TREE_ENTRIES = 500

# How many bytes of path the snapshot may carry. DERIVED from the relay's own per-event ceiling
# rather than chosen, and deliberately a fraction of it, because the failure it prevents is worse
# than a missing tree: an event over `_MAX_EVENT_BYTES` is refused with a 422, and a 422 does not
# latch the publisher off — it DROPS the whole batch. So one oversized tree takes every progress line
# batched beside it down with it, on every beat, for the rest of the task. The margin covers the JSON
# envelope, the hash, and the other events sharing the flush.
MAX_TREE_BYTES = task_events.MAX_EVENT_BYTES // 2

# What one path costs beyond its own serialized form: the comma that joins it to the next.
_PATH_SEPARATOR_BYTES = 1


@dataclass(frozen=True)
class TreeSnapshot:
    """One reading of the workspace: what it holds, how much was shown, and its identity."""

    paths: tuple[str, ...]
    total: int
    truncated: bool
    digest: str

    def as_event(self) -> dict:
        """The `task.tree` payload, minus the type the publisher adds.

        `total` rides along with the paths because a truncated listing is close to useless without
        it — "500 files" and "500 of 12,431 files" are different facts, and only the second one tells
        a user their dependency install is what they are looking at.
        """
        return {"paths": list(self.paths), "total": self.total,
                "truncated": self.truncated, "hash": self.digest}


def snapshot(workspace: Path) -> TreeSnapshot:
    """Read the workspace once. Raises whatever `task_repo` raises; the caller decides."""
    paths = task_repo.list_files(workspace)
    return _capped(paths)


def _capped(paths: list[str]) -> TreeSnapshot:
    """As many paths as fit under BOTH budgets, and the honest count of how many there were.

    Bytes first, entries second. A path is filesystem-bounded at ~4 KB, so five hundred of them is a
    small-looking number and a payload the relay would refuse; the count is the readability bound
    sitting behind the one that protects the wire.
    """
    kept: list[str] = []
    budget = MAX_TREE_BYTES
    for path in paths[:MAX_TREE_ENTRIES]:
        cost = _wire_cost(path)
        if cost > budget:
            break
        budget -= cost
        kept.append(path)

    frozen = tuple(kept)
    truncated = len(frozen) < len(paths)
    return TreeSnapshot(
        paths=frozen,
        total=len(paths),
        truncated=truncated,
        digest=_digest(frozen, len(paths), truncated),
    )


def _warn(message: str) -> None:
    print(f"\n[tasks] {message}", file=sys.stderr)


class WorkspaceTree:
    """One task's workspace, published on the heartbeat when — and only when — it changed.

    Runs on the lease renewer's thread, which is the whole reason `beat()` is guarded end to end:
    anything that escaped would kill the renewal loop, the lease would lapse, and the task would be
    reclaimed and retried by another provider — a *result* lost to a *progress* feature. Publishing
    is best-effort in exactly the sense `task_events` means it: losing a snapshot costs a view of a
    directory, and no view of a directory is worth an attempt.
    """

    def __init__(self, workspace: Path, publisher: Any) -> None:
        self._workspace = workspace
        self._publisher = publisher
        # The digest of the last snapshot PUBLISHED, so a beat whose publish failed is retried
        # rather than remembered as delivered. A workspace that changes rarely is exactly the one
        # whose single tree event matters, and it would otherwise stay silent until it changed again.
        self._published: str | None = None
        # The reason last complained about, so a repeat stays quiet and a CHANGE does not. A beat
        # fires every 30s for up to an hour, so saying one line on each of them buries the provider's
        # log — but a single "have I ever complained" latch is worse in the other direction: a
        # transient hiccup early on would buy silence for the permanent breakage that followed it,
        # leaving the operator a stale message about a problem that already cleared.
        self._complaint: str | None = None

    def beat(self) -> None:
        """One reading, published if it is new. Never raises.

        `SystemExit` is named alongside `Exception` deliberately — it is this repo's clean-error
        idiom (`jsonio`, the CLI validators) and is not an `Exception`, so a guard naming only the
        latter would let it through into the renewal thread and stop it silently.
        """
        try:
            current = snapshot(self._workspace)
        except (Exception, SystemExit) as exc:
            self._complain(f"could not read task workspace {self._workspace} for a tree snapshot "
                           f"({exc}); the task is unaffected")
            return

        if current.digest == self._published:
            self._recovered()
            return
        try:
            # `blocking=False`: this runs on the lease renewer's thread, and the publisher's lock is
            # held across a POST bounded only by `relay._TASK_EVENT_TIMEOUT`. Waiting for it would
            # spend the lease's own budget on a progress event. Declining costs nothing — the digest
            # only advances on an ACCEPTED publish, so the snapshot is simply offered again next beat.
            accepted = self._publisher.publish(
                "task.tree", blocking=False, **current.as_event())
        except (Exception, SystemExit) as exc:
            # The publisher is documented never to raise. This does not rest on that: the same rule
            # `tasks._publish_safely` applies, for the same reason — a promise made by another module
            # is not a guarantee this thread can afford to inherit.
            self._complain(f"the event publisher raised on a tree snapshot ({exc!r}) — it is "
                           f"documented never to; the task is unaffected")
            return

        if not accepted:
            # Not an error and not delivered: the channel was busy, or it has latched off after a
            # verdict. Either way the digest must NOT advance — remembering a snapshot the relay
            # never saw would leave the view permanently one revision stale if the channel recovers.
            return
        self._published = current.digest
        self._recovered()

    def _complain(self, message: str) -> None:
        if message == self._complaint:
            return
        self._complaint = message
        _warn(message)

    def _recovered(self) -> None:
        """Forget the last complaint, so a failure that recurs after a good beat is news again."""
        self._complaint = None


def _wire_cost(path: str) -> int:
    """What one path costs in the bytes the RELAY counts, which are not the bytes the path weighs.

    grid-src measures an event with `len(json.dumps(event).encode("utf-8"))`, and `json.dumps`
    escapes to ASCII by default: a CJK character is three bytes in UTF-8 and six on the wire, and a
    control character is one byte and six on the wire. Estimating from the path's own length
    understates both — by 2× for an ordinary Chinese-language project and by 6× for the filenames an
    agent running under `bypassPermissions` is perfectly able to create. Overshooting the relay's cap
    is not a truncated tree, it is a 422; and a 422 drops the WHOLE batch, so the agent output
    published alongside the snapshot disappears with it, on every beat, for the rest of the task.

    So the cost is serialized rather than guessed. It is one tiny `dumps` per path, bounded by
    `MAX_TREE_ENTRIES`, on a call that happens every thirty seconds.
    """
    return len(json.dumps(path).encode("utf-8")) + _PATH_SEPARATOR_BYTES


def _digest(paths: tuple[str, ...], total: int, truncated: bool) -> str:
    """The identity of the snapshot AS PUBLISHED, not of the directory.

    Hashing the full listing instead would republish an identical-looking payload every beat while a
    dependency install churned beyond the cap. Hashing what the client actually receives makes "an
    unchanged tree publishes nothing" exactly true of what the client sees — and `total` is part of
    it, so growth past the cap still moves the hash.
    """
    material = json.dumps(
        {"paths": list(paths), "total": total, "truncated": truncated},
        sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(material).hexdigest()
