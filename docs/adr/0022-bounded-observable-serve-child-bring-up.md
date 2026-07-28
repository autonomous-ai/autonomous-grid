---
status: accepted
---

# Bring-up leaves a trail, has a deadline, and converges on its own

A detached serve child used to spend its entire startup silent and unbounded, and could not recover
from a relay that was merely *temporarily* unwell. [ADR 0020](./0020-process-identity-at-run-record-seams.md)
made a run record's pid mean something; [ADR 0021](./0021-service-truth-at-the-join-gate.md) made the
CLI report an unregistered child honestly. Both describe the same afternoon's second incident from
the outside. This one fixes it from the inside.

Field-confirmed 2026-07-27: a `grid join` issued while the grid's master was mid-respawn (relay
answering 503) produced a child that sat in bring-up for 8+ minutes — one thread parked in a socket
wait, a **0-byte log** — and never recovered. The engine endpoint and the relay both probed healthy
from that box minutes later. The operator had to leave and re-join by hand, and the first evidence
had to come from `/proc`, because nothing on disk said anything at all.

Scope: **this CLI only.** No wire value, no master or control-plane slice, nothing for the lockstep
register in `CLAUDE.md`. Bring-up means spawn → first successful registration; the steady-state
poll and heartbeat loops already survive relay outages and log their retries, and are untouched.
Evidence and design: `.scratch/grid-leave/` (PRD follow-up F9, issue 11 — Layer 3 of the approved
three-layer proposal).

Choices a future reader will otherwise re-litigate:

- **The log was empty because nothing was written, not because something was buffered.** The
  spawner already opens the file before the child exists and already sets `PYTHONUNBUFFERED=1`, so a
  0-byte log is positive evidence that the child printed nothing — and it printed nothing because the
  first unconditional `print` on the serve path sat *after* registration. So the child now names
  itself before it reads its own record (that read can fail on a corrupt *sibling* record, before any
  handler exists), and names the engines, the models and the relay it is about to register with
  before it opens a socket. Everything after that is narrated per phase with its elapsed time. A
  0-byte log from a spawned child is now structurally impossible, which is worth more than any single
  line in it: the artifact an orphan investigation starts from is the child's own log.

- **A deadline per call was not the missing thing; a deadline over the *fan-out* was.** Auditing the
  path for "a call inheriting an unbounded default" found only two, both trivial — raw
  `connect_ex` port probes with no `settimeout`, which a sibling in `local/` already sets. Every HTTP
  call passed a timeout. What no one owned was the **sum**: capability probing runs four metadata
  probes and up to four *real-inference* probes per model, sequentially, each with its own clock, on
  top of engine and media readiness waits that also have their own. Eight minutes is reachable with
  every individual call comfortably inside its own bound. So the probe fan-out gets one wall-clock
  budget for the whole of it, in the shape [ADR 0019](./0019-engine-health-in-the-heartbeat.md)'s
  health sweep already uses. Per-call deadlines are still made explicit — a bare float is applied by
  httpx to all four phases *independently*, so it never stated what the caller thought it stated —
  but the aggregate is the fix.

  It is a **soft** ceiling, deliberately: the budget is checked between models, not preempted inside
  one model's own probe sequence, so the true worst case is the budget plus one model's remaining
  calls. Interrupting a probe mid-flight would buy a tighter number at the cost of a partial answer
  that is neither a real capability nor a clean failure, and the bound that mattered was going from
  *unbounded* to *bounded at all*.

- **The budget's policy belongs to the caller, not to the probe.** The probe site is deliberately
  shared by startup and hot-reload so the two can never drift ([ADR 0009](./0009-remote-provider-concurrency.md) C2),
  and it stays shared — but the two callers are in genuinely different situations, so it raises and
  they decide. At **startup** there is no previous verdict to keep, so the remaining models take the
  fail-closed all-False envelope the probe already degrades to, and the log names exactly which ones.

  That envelope must contain a **key for every advertised model**, claiming nothing — not an absent
  key. The relay validates that `capabilities.models`' keys equal the advertised model list exactly
  and answers 400 otherwise, and 400 is terminal to the retry loop above: a degrade that omitted the
  skipped models would therefore *kill* the slow engine it exists to keep serving, on every join,
  permanently. The mismatch was unreachable before this budget existed, because a failed probe
  empties a model's entry but never removes it — which is exactly why the shape is easy to get wrong
  here and why a test asserts the invariant on the registration payload rather than on the function
  that builds it.
  On **reload** the node is already registered and serving, and the only path that probes is a
  *newly appended* engine — the case most likely to be legitimately slow, because it is still
  loading. Writing all-False there would poison a live registration until the next reload, after the
  CLI had already printed "hot-reloaded", and would contradict ADR 0019's own rule for the identical
  trade-off (*the engines it did not reach keep their previous verdict*). So a budget bite there
  **refuses the reload**: the existing reload guard keeps the old routing and records
  `last_reload_error`, which the join gate already surfaces.

- **Registration retries until it is told to stop, and the terminal set is the small one.** The
  regime this exists for is a relay that is *temporarily* wrong, so the classification is written as
  an explicit list of what will never self-heal — `400`, `403`, `422`, where the relay understood the
  request and refused it — and everything else retries: transport failures, `429`, every 5xx, and
  **`404`**. That last one is the reason the list is written this way round: a master mid-respawn can
  answer 404 as easily as 503 (routes not yet mounted, or a proxy in front of an app that has not
  started), and an "explicit retryable set" would have missed the incident this ADR is named after.
  The wait is a capped exponential backoff on the stop event the whole process already uses, so a
  SIGTERM during a backoff still exits well inside the parent's kill grace.

  There is a **third** fatal category that no status can express, and it needs its own marker: a
  relay address that is not a usable URL fails while the request is being *built*, so nothing was
  ever asked. That carries no status — which is otherwise the exact signature of a plain transport
  failure, the case that must retry — so without an explicit flag a broken address on the run record
  would spin at the backoff floor forever. It is as unfixable-by-waiting as a 403 and is treated the
  same way, naming the offending address.

- **An exhausted token retries too, on its own floor, and says `grid login`.** This is the one
  retryable class that cannot fix itself, so treating it like a 503 would be wrong — but killing the
  child is also wrong, because the refresh path re-reads the credential file and adopts a token
  **another process** stored. That makes `grid login` on the same box a real remedy for a child that
  is already running, with no re-join. It gets a much longer floor (each attempt is otherwise a
  control-plane round trip), and both the log line and the recorded error name the command, which is
  strictly better than what it replaces: this path used to die printing `Remote engine stopped: `
  with nothing after the colon, because the exception carries no message.

- **What the operator sees costs a `grid join` its fast failure, and that is the trade being made.**
  A relay that refuses fast used to kill the child inside the join's short start-wait, and the join
  then removed the run record and failed with the log tail. That path is now unreachable for a relay
  that is down or refusing: the child survives, retries, and the join reports "starting". The child
  is not lost — it holds a record with no `registered_at` and a live heartbeat sidecar, which is
  exactly the state ADR 0021's gate was built to describe, so the next `grid join` names the pid, the
  uptime, the last register error and the log path, and `grid leave` reaps it like any other. The
  alternative — keeping the fast failure — is the same choice that produced the incident, because a
  relay mid-respawn refuses fast too.

- **The attempt count is log-only.** The recorded `last_register_error` must stay byte-identical
  while the failure is unchanged, or the record writer's no-op-write skip stops working and a relay
  that has been down for a day costs one locked write per attempt instead of one. Attempt numbers and
  elapsed times are narration; the record holds the reason.

Consequences. A child that cannot reach its relay is now visible from disk from its first
millisecond, converges on its own when the relay comes back, and cannot spend minutes in a socket
without saying so. Two things are knowingly left open. The child's log is bounded **per line** and
re-capped when the next child is spawned, but has no in-process rotation, so a child retrying for
months grows its log slowly — the same property every other engine log here has, and the reason the
cap exists at spawn. And a `grid join` that appends to a child still retrying reports "hot-reloaded"
while its signal stays queued — bring-up blocks that signal for its whole duration and this makes
that duration longer — so the append takes effect when registration finally succeeds rather than
when the CLI says it did. The outcome is correct and the message is optimistic; making the append
wait on service truth is a larger change than this one.
