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
.venv/bin/python -m pytest tests/e2e_cross_repo/e2e_live_agent.py  -q   # ~45 s, SPENDS A SUBSCRIPTION
```

## What it stands up

| piece | what it really is |
|---|---|
| relay | grid-src's `server:app` under a real `uvicorn`, in a subprocess, run by **grid-src's own interpreter** — two installs, as in the field |
| provider | this repo's `remote.tasks.task_loop`, in its own **process** (`provider_process.py`) so one can be `kill -9`ed |
| client | `remote.relay` and `remote.task_repo`, called directly |
| agent | `fake_claude.py` (free), or the real Claude Code (`e2e_live_agent.py`) |
| auth | a real HS256 token minted with `hmac` — nothing here can reach into the relay to patch a verifier |

The lease is 6s against a 0.5s renewal rather than the production 120s/30s. The **ratio** is what is
kept (ADR 0032 D-c: a TTL several beats wider than the interval), so what is tested is the mechanism.

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
