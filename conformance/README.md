# conformance — every client in the field, against the server, on every change

grid-platform ticket 13 (ADR 0004). Built on ticket 12's `grid-protocol`: every answer is checked against its schemas.

## What it runs

| Client | Installed from | Driven as |
|---|---|---|
| public `grid` 0.3.47 – 0.3.57 | each version's **release wheel** on GitHub Releases | the CLI a user runs |
| the harness's pinned `grid` | its version, read from the manifest the harness daemon follows | the same |
| the harness daemon's own reads | `test_harness_reads.py`, checked against `grid-protocol` | credential-less HTTP, as `gridReader.ts` |

**The server side.**
- grid-src's master, started in **grid mode**. It verifies RS256 tokens against a JWKS the run writes.
- grid-apis' **real proxy app** in front of it. `build_app`'s control-plane reads come from a state file the run flips
  (`proxy_launcher.py`), and every routing, wake and refusal decision is the proxy's own.
- A sleeping grid keeps its port and loses its process, as on the fleet. A supervisor brings the master back when the
  proxy's wake flips the state.
- A stub stands in for the one control-plane route old CLIs call (a grid's status), and a fake OpenAI-compatible
  engine for what a provider serves.

**What each client is checked for.**
- reads (`models`, `engines`, `stats`) of an awake grid;
- `--no-wake` on an asleep grid: from 0.3.49, a `cli-error` envelope with `grid_asleep` and no wake; before 0.3.49,
  argparse's exit 2;
- a signed-in read wakes an asleep grid;
- `grid join --at`: register, heartbeat, poll and upload, proven by a consumer's request through the proxy being served;
- a provider outlives a sleep and serves after the wake;
- on a deleted grid, a provider stops from 0.3.48 and keeps retrying before it.

**The harness reads** are the overview and discovery, awake and asleep (with `last_known`), and every refusal code
(`grid_asleep`, `grid_stopped`, `grid_deleted`, `grid_master_down`, `feature_retired`) with its status.

**The relay's own refusals** for a model no engine serves now, in both envelopes: `no_providers_available` (503,
retryable) for a model the grid has served whose engine has left, and `model_not_found` (404, at once) for a model no
engine on the grid ever served, once it has had one. Each test makes the history it needs, so neither depends on the
order the tests run in.

## What it does NOT cover

The stand-ins are what make it run in CI without the fleet. A green run therefore says nothing about these:
- **The control plane** beyond a grid's status: login, token refresh and rotation, session revoke, the snapshot push.
  Tokens are minted here and expire in 2100.
- **The wake's cost:** the stand-in flips the state at once. The real wake's rate limits, queue and memory floor are
  not exercised, and neither is the slow-wake timing pair (the boot hold against the parked probe). The proxy is told
  memory is plentiful.
- **The admin revive:** a master that is down while `running` stays down here. Only its refusal
  (`grid_master_down`) is checked.
- **The master's database:** it runs on SQLite, with billing off and no sync snapshot, so Postgres, RLS, billing's
  402 and the allowlist are out.
- **`relay_restarting`:** it needs a SIGTERM in the middle of a request, and no client may parse it (the register).
  Only its schema exists.
- **Anything a schema leaves optional:** a renamed optional field passes the schema. The tests name what the harness
  reads (the overview's `models[].id`, discovery's `raw_model_id`), but that list is only as complete as they are.

## Running it

```bash
GRID_SRC_REPO=~/Projects/grid-src GRID_APIS_REPO=~/Projects/grid-apis \
  ~/Projects/grid-src/.venv/bin/python -m pytest conformance -o addopts=""
```

- It runs under **grid-src's interpreter**, which has every dependency it needs.
- A plain `pytest` here leaves it alone (`norecursedirs`).
- Without the siblings it skips. With `GRID_CONFORMANCE_REQUIRED=1`, which CI sets, a missing sibling FAILS: pytest
  exits 0 on an all-skipped run.
- `GRID_HARNESS_PIN` overrides the manifest's pin. `GRID_CONFORMANCE_CACHE` is where the venvs live.
- About 20 minutes warm for 11 releases and the harness pin (136 tests, measured 2026-10-08 on an M-series Mac);
  it grows by about 1.6 minutes per release added. A cold run adds the per-version venvs.
- **Every answer is checked against THIS checkout's `protocol/`**, not grid-src's pinned `grid-protocol`, so a schema
  changed here first is what the server is held to.

## CI

`.github/workflows/conformance.yml` in **grid-src** and **grid-apis** runs it on every pull request. Each repository
checks out this one with no token, and the OTHER private sibling with a read-only deploy key: `GRID_APIS_DEPLOY_KEY`
in grid-src, `GRID_SRC_DEPLOY_KEY` in grid-apis. A last step reads the JUnit report and fails on zero tests or any
skip.

**Paired changes.** The suite and the other sibling are checked out at the PR's own branch name when their repository
has one, and otherwise at `main`, or at `CONFORMANCE_SUITE_REF`, `CONFORMANCE_GRID_SRC_REF` or
`CONFORMANCE_GRID_APIS_REF`. A wire change made in two repositories, on two branches of one name, tests each half
against the other.

## Changing it

- **A new public release:** add it to `clients.RELEASED` when the harness pins it, or when anything in the field
  can run it. Drop a version only when nothing can.
- **A client that changed behaviour on purpose:** gate the assertion on the version it changed in (`parse(version) >=
  …`), so the old behaviour stays pinned for the old clients.
