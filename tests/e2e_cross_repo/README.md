# Cross-repo E2E — distributed tasks (ADR 0032)

The one place this repo's provider and client meet grid-src's relay with nothing mocked in between.

Both unit suites pass with this seam broken, by construction: each mocks the other side. grid-src's
own `*_live` tests run a real relay and hand-roll the provider's HTTP; this repo's tests run the real
provider against a fake relay. Every defect in this feature that survived both suites lived exactly
here — a relay module missing from the loader tuple (both suites import it directly; the live master
does not), and a transcript symlink planted at an unresolved path while the agent wrote at the
resolved one.

## Not collected by an ordinary run

The modules are `e2e_*.py`, not `test_*.py` — the same convention as `tests/e2e_doggi.py` and
`tests/e2e_train.py`. `uv run pytest tests/` never touches them. Pass a path to run them:

```bash
.venv/bin/python -m pytest tests/e2e_cross_repo/e2e_cross_repo.py -q   # ~2 min, free
.venv/bin/python -m pytest tests/e2e_cross_repo/e2e_goal.py -q         # ~2 min, free
.venv/bin/python -m pytest tests/e2e_cross_repo/e2e_live_agent.py  -q   # ~45 s, SPENDS A SUBSCRIPTION
.venv/bin/python -m pytest tests/e2e_agent_settings.py -q               # ~45 s, SPENDS A SUBSCRIPTION
```

`tests/e2e_agent_settings.py` lives one directory up because it needs no relay — the real binary and
`run_task` are the whole seam. It borrows `_harness.sweep_transcript_links` from here, because every
module that drives the real binary writes into the operator's own `~/.claude` and they must all clean
up the same way.

## What it stands up

| piece | what it really is |
|---|---|
| relay | grid-src's `server:app` under a real `uvicorn`, in a subprocess, run by **grid-src's own interpreter** — two installs, as in the field |
| provider | this repo's `remote.tasks.task_loop`, in its own **process** (`provider_process.py`) so one can be `kill -9`ed |
| client | `remote.relay` and `remote.task_repo`, called directly |
| agent | `fake_claude.py` (free), or the real Claude Code (`e2e_live_agent.py`) |
| agent config | `fake_claude.py` REQUIRES `--setting-sources user --strict-mcp-config` (issue 22) — dropping either from `agent_argv` breaks this free run, not only the paid one |
| auth | a real HS256 token minted with `hmac` — nothing here can reach into the relay to patch a verifier |

The lease is 6s against a 0.5s renewal rather than the production 120s/30s. The **ratio** is what is
kept (ADR 0032 D-c: a TTL several beats wider than the interval), so what is tested is the mechanism.

## Goal scenario matrix

`e2e_goal.py` runs the private relay and every provider in separate OS processes with distinct
task roots. It exercises HTTP auth, claims and leases, Git fetch/push and exact commit pins, native
harness protocols, transcript checkpoints, independent evals, and relay-authored evidence. The
fake model and fake native binaries keep failures deterministic; the Grid protocol between them is
real.

The fake Codex also implements the installed-binary schema command used by provider admission. This
means every Codex process in the matrix must first prove the exact `thread/resume`,
`thread/goal/{set,get}`, event and dynamic-tool surface before it can advertise `native_goal`; the
interactive JSON-RPC fixture then proves those declared methods during execution.

| Scenario | Nodes and harnesses | Failure or constraint | Proof |
|---|---|---|---|
| Root create replay | Client -> relay twice | First acknowledgement may be lost | Same member key returns one Goal and one turn; changed body conflicts |
| Same-node claim ABA | A claim 1 -> A claim 2 | An old process retains the same reusable node credential | Missing/old generations fail on lease, events, retry, result, inference, Git, and child create/replay; claim 2 stays live with no false eval/evidence |
| Model arrival | A Codex polls; inference-only C joins | Requested route does not exist yet | Same row remains attempt 0, then A claims attempt 1 when C advertises it |
| Quota recovery | A Codex polls; inference-only C heartbeats | Route exists but its subscription seat reports `serving: false` | No attempt/evidence during withdrawal; same row wakes on C's healthy heartbeat |
| Four-feature game | A Codex -> B Codex -> C Codex | A and B are killed mid-turn | Same rows are reclaimed; commit-pinned wiring/click/score/style evals pass |
| Native crash checkpoint | A Codex -> B Codex | A's app-server fails after partial work | Same turn immediately requeues; B restores partial tree/thread and behavior evals pass |
| Claude protocol drift | A Claude (2 workers) -> B Codex | A exits zero without its native evaluator attachment while worker 2 has a stale capability poll | Worker 2 visibly declines the delivery without spending an attempt; A stays online; same turn/checkpoint moves to B and evals pass |
| Codex protocol drift | A Codex (4 workers) -> B Claude | Schema passes, then `thread/goal/get` returns method-not-found while three siblings have stale capability polls | All three deliveries are visibly declined without spending an attempt; A stays online; same turn/checkpoint moves to B and evals pass |
| Native crash after API commit | A Codex -> B Codex | A's app-server fails after a successful business mutation | Stable key yields one side effect; both attempts retain request/result evidence |
| Machine death in API result window | B Codex -> C Codex | B is SIGKILLed after the API commits but before Grid records the result | C reconciles the unmatched request with the same key; one side effect and independently verified evidence |
| Mixed game | A Codex -> B Claude -> C Codex | Two machine losses across unlike harnesses | Shared continuity plus commit-pinned behavior evals across harnesses |
| Cross-harness eval repair | A Codex -> B Claude -> C Codex -> D Claude | C nominates plausible but broken interaction | D restores B's Claude session across Codex, consumes failed eval evidence, and repairs it |
| Image artifact | B Claude polls; A Codex executes | Goal requires `image_generation` | Ineligible node spends no attempt; independent PNG eval passes |
| Support reply | A polls; B Codex -> C Codex | Origin restriction, crash after API commit, failed first eval | One business side effect, stable idempotency key, repair turn passes |
| Required child | A parent model; B Claude child specialist model; C parent | Parent waits while child runs | Child model/harness survives the tool bridge; evaluated commit fans in once; allocation becomes one actual token charge |
| Mixed child reclaim | A Codex parent -> B Codex child -> C Claude child -> D Codex parent | B's native child harness fails after partial work | C reclaims the exact child turn at attempt 2 from Git/transcript checkpoints; its accepted eval gates one fan-in before D resumes the parent |
| Parallel child fan-out | A Codex parent -> B Codex and C Claude children -> D Codex parent; inference E/F | Two required children run simultaneously on separate roots, harnesses, capabilities, and models | Children make real Responses/Messages calls through distinct Grid providers; signed evidence binds exact model/provider/executor/attempt/token usage; both evals pass before deterministic fan-in |
| Required child failure | A Codex parent -> B Codex and C Claude children | B returns a native semantic failure while C is still running a required sibling | Parent blocks; Grid cancels and lease-fences C, releases all reservations, and exports the cancellation in Goal evidence |
| Child spawn reconstruction | A Codex -> B Codex -> C Claude -> D Codex | A fails after the durable spawn; B reconstructs different optional child policy | Both attempts receive one child identity and one fan-in; action evidence carries one stable key |
| Optional child | A parent; B child; C parent | Child returns native `failed` verdict | Failure remains evidence and does not block parent completion |

The native-crash case starts each provider in one-claim mode so A withdraws after handing off its
checkpoint and cannot race B for its own immediate retry. This changes only the test process
lifecycle; production claim, checkpoint, retry, and settlement code remains untouched.

The two protocol-drift cases do the opposite: A runs multiple real task-loop threads. The fake
native harness clears a counted synchronization file and waits until every sibling enters a fresh
claim long-poll with the pre-quarantine profile, then triggers drift. Claude uses one stale sibling;
Codex uses three, enough to exhaust the ordinary retry cap if even one delivery were charged. The
test does not start B until A's log proves every stale delivery was declined and the relay again
shows the same turn queued at attempt 1. B must therefore receive attempt 2; a lucky race by B or a
hidden retry-budget burn cannot make either case pass.
The full matrix also leaves providers long-polling immediately before fixture teardown. The relay
checks socket disconnects on both sides of assignment and returns any response that cannot be
delivered to the queue without consuming an attempt; otherwise an orphan request from one scenario
can steal the next scenario's first Goal turn.
The four-node repair case proves that Grid keeps Codex and Claude histories side by side rather than
overwriting or translating either: D's fresh disk contains both opaque namespaces, resumes B's
Claude session after C's intervening Codex turn, and receives the failed deterministic score as
relay-authored guidance. Evidence retains C's accepted failing score and D's accepted passing score
against their distinct result commits. Retry evidence also retains the relay-selected harness for
the lost attempt, so a row later claimed by Claude cannot rewrite an earlier Codex attempt (or vice
versa) in the training trajectory.
The crash-after-action case covers the stronger handoff path while A still owns the lease. Grid
flushes the action request/result trajectory before the retry endpoint revokes event authority,
accepts exact worktree and transcript pins, and requeues the same logical turn immediately. B's
replay carries the identical Goal-wide idempotency key, so the business API performs no duplicate
mutation.
The child-spawn reconstruction case applies the same rule to Grid's own dependency table. A and B
use different native sessions and deliberately restate the child eval and routing hints differently;
the normalized objective still produces one parent-turn action key, so the relay returns the first
reserved child and the parent later fans in exactly one branch.

## Prerequisites

- grid-src's matching worktree at `/Users/macbookpro/Projects/grid-src-feats/distributed-tasks`, with
  its own `.venv`. Override with `GRID_SRC_REPO`. Missing ⇒ **skip**, never fail — same rule as the
  lockstep constants `tests/test_task_lease.py` parses out of that repo.
- `git` on PATH.
- For `e2e_live_agent.py` only: a logged-in Claude Code. `GRID_E2E_MODEL` picks the model
  (default `claude-haiku-4-5-20251001` — the seam is what is under test, not the reasoning).

## Two things to know before editing

**`RENEW_INTERVAL_SECONDS` cannot be scaled by assignment.** It is a keyword-only *default*, bound
when the class body ran, so rebinding the module attribute changes nothing about renewers built
later — the cadence stays 30s while the code reads as though it were 0.5s. `provider_process.py`
forces it at construction instead. Getting this wrong does not look like a failure: every
`task.tree` disappears silently and the tasks short enough to finish inside one lease TTL all pass.

**`e2e_live_agent.py` writes into the operator's real `~/.claude`.** It has to: issue 01's spike
measured a custom `CLAUDE_CONFIG_DIR` failing to authenticate at all. It removes the symlinks it
planted in teardown, checking both the written and the resolved spelling of each workspace path.
