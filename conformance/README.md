# conformance — every client in the field, against what serves every grid, on every change

grid-platform ticket 13 (ADR 0004); on relay pools since M5 ticket 07. Built on ticket 12's `grid-protocol`: every answer
is checked against its schemas.

## What it runs

| Client | Installed from | Driven as |
|---|---|---|
| public `grid` 0.3.47 – 0.3.61 (a pull request: the ones in the field, below) | each version's **release wheel** on GitHub Releases | the CLI a user runs |
| the harness's pinned `grid` | its version, read from the manifest the harness daemon follows | the same |
| the harness daemon's own reads | `test_harness_reads.py`, checked against `grid-protocol` | credential-less HTTP, as `gridReader.ts` |

**The server side** is what serves every grid since 2026-10-22 (grid-platform M5 ticket 07; before it, a single-grid
master behind the proxy's legacy path):
- grid-src's **relay instances** in grid mode, on two relay pools of one instance each, over a relay database of the
  run's own. They verify RS256 tokens against a JWKS the run writes.
- the control plane's **sync log** and the **real projector** between it and the relay database: each grid is created by
  rows, stopped, started and deleted as the control plane does it.
- grid-apis' **real proxy app** in front, routing by placement over the instances' trusted sockets; every routing and
  refusal decision is the proxy's own.
- A stub stands in for the one control-plane route old CLIs call (a grid's status), and a fake OpenAI-compatible engine
  for what a provider serves.

That stack is **grid-platform's pool conformance target** (`grid-src/conformance_pool/pool_target.py`), taken from the
sibling grid-src (`stack.pool_target_from`) rather than copied here, so the two can never drift. Each test gets a grid
of its own, created by rows on the first pool.

**What each client is checked for.**
- reads (`models`, `engines`, `stats`) of a running grid;
- `grid join --at`: register, heartbeat, poll and upload, proven by a consumer's request through the proxy being served
  and resumed;
- on a deleted grid, a provider stops from 0.3.48 and keeps retrying before it;
- a provider whose node row is gone registers again and serves;
- `grid task` / `grid project` are told the task plane is retired.

**The harness reads** are the overview and discovery of a running grid, and the codes a grid on a relay pool answers
(`grid_stopped`, `grid_deleted`, `feature_retired`) with their status.

**The relay's own refusals** for a model no engine serves now, in both envelopes: `no_providers_available` (503,
retryable) for a model the grid has served whose engine has left, and `model_not_found` (404, at once) for a model no
engine on the grid ever served, once it has had one. Each test makes the history it needs.

**What a sleeping grid did** — `--no-wake` refused `grid_asleep`, a signed-in read woke it, a provider outlived its
sleep, `last_known`, `grid_master_down` — is no longer asked: no grid sleeps on a relay pool, and the proxy stops sending
those codes with M5 ticket 07 (released clients still parse them). Their pool counterparts (`--no-wake` reading as
without it, a provider parked by its owner's stop and serving after the start, `grid info` saying `running`, the
Anthropic and Responses dialects, each grid type) are grid-platform's pool target's own checks
(`conformance_pool/test_pool.py`, `test_pool_types.py`), which call the checks here.

## What it does NOT cover

The stand-ins are what make it run in CI without the fleet. A green run therefore says nothing about these:
- **The control plane** beyond a grid's status: login, token refresh and rotation, session revoke. Tokens are minted
  here and expire in 2100.
- **Two instances behind one front** (the pool runs one per pool): grid-platform's cluster DDL suites ask it.
- **`relay_restarting`:** it needs a SIGTERM in the middle of a request, and no client may parse it (the register).
  Only its schema exists.
- **Anything a schema leaves optional:** a renamed optional field passes the schema. The tests name what the harness
  reads (the overview's `models[].id`, discovery's `raw_model_id`), but that list is only as complete as they are.

## Running it

```bash
GRID_SRC_REPO=~/Projects/grid-platform/grid-src GRID_APIS_REPO=~/Projects/grid-platform/grid-apis \
  ~/Projects/grid-platform/grid-src/.venv/bin/python -m pytest conformance -o addopts=""
```

- It runs under **grid-src's interpreter**, which has every dependency it needs (grid-apis' own `.venv` runs the proxy).
- The relay database needs a PostgreSQL **superuser**: `GRID_TEST_POSTGRES_URL`, else a throwaway cluster the run starts
  from the server binaries it finds (`initdb` on PATH, Debian's `/usr/lib/postgresql/*/bin`, Homebrew's), removed at
  exit.
- A plain `pytest` here leaves it alone (`norecursedirs`).
- Without the siblings it skips. With `GRID_CONFORMANCE_REQUIRED=1` a missing sibling FAILS: pytest exits 0 on an
  all-skipped run.
- `GRID_HARNESS_PIN` overrides the manifest's pin. `GRID_CONFORMANCE_CACHE` is where the venvs live.
- **Every answer is checked against THIS checkout's `protocol/`**, not grid-src's pinned `grid-protocol`, so a schema
  changed here first is what the server is held to.

## CI

`.github/workflows/conformance.yml` in the private **grid-platform** monorepo runs the **pool** target on every pull
request touching grid-src or grid-apis (its `conformance_pool/`, the same stack, with this suite's checks and its own),
as shards; this repository is checked out with no token, at the PR's own branch name when it has one, else at `main` or
`CONFORMANCE_SUITE_REF`. A last step reads the JUnit report and fails on zero tests or any skip. (Its second job,
`released-clients`, ran this directory against a single-grid master until M5 ticket 07 folded it into the pool target:
the pool target already asked every released client the same checks.)

## Changing it

- **A new public release:** add it to `clients.RELEASED` when the harness pins it, or when anything in the field
  can run it, and to `clients.IN_THE_FIELD` (the newest always is; `tests/test_conformance_clients.py` refuses it
  missing). Drop a version only when nothing can.

## Which releases a run tests

`GRID_CONFORMANCE_CLIENTS` picks the set: `all` (the default) is every release in `RELEASED`; `field` is
`IN_THE_FIELD`, the releases PROD has seen. The harness's pin runs in both. grid-platform's `conformance.yml` runs
`field` on a pull request (each release costs every PR's conformance jobs minutes) and `all` on its weekly schedule and
on a dispatch, so a release PROD has stopped seeing is still tested once a week. Any other value fails the run.

`IN_THE_FIELD` is a measurement, so re-take it rather than guess. Read only on PROD: count in `awk`, never `sort` there
(it spills to a temp file on a big input), and nothing redirected on the box:

```bash
ssh root@<PROD> 'cd /var/log/nginx && zcat -f grid.autonomous.ai-access.log* api-grid.autonomous.ai-access.log* \
  | awk "match(\$0, /grid-cli\/[0-9]+\.[0-9]+\.[0-9]+/) { n[substr(\$0, RSTART, RLENGTH)]++ } END { for (v in n) print n[v], v }"'
```

nginx keeps ~14 days. A release that read no longer shows can leave `IN_THE_FIELD`; it stays in `RELEASED`.
- **A client that changed behaviour on purpose:** gate the assertion on the version it changed in (`parse(version) >=
  …`), so the old behaviour stays pinned for the old clients.
