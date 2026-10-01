# grid-protocol

The wire contract between the grid relay (grid-src), the platform (grid-apis), the public `grid` CLI (this
repository) and the harness, defined once as JSON Schema (ADR 0004 of the grid-platform architecture review: the wire
contract lives in the public repository).

Before this package the shapes that cross those repositories were hand-copied into each of them and kept in
step by a lockstep register and by pins that skipped in CI. A value renamed on one side compiled, passed every
test and broke the seam at runtime. Now the schema is the one place a shape is written down.

## What is in it

| Schema (`grid_protocol/schemas/`) | What it is |
|---|---|
| `refusal` | The platform's coded refusal `{detail, code}`: `grid_asleep` (with `last_known`), `grid_stopped`, `grid_deleted`, `grid_master_down`, `feature_retired` |
| `openai-error` | A master's OpenAI-envelope failure: `no_providers_available`, `relay_restarting` |
| `last-known` | What a sleeping grid served when it was put to sleep |
| `overview`, `discover` | The two public reads, which never wake a sleeping grid when sent without a credential |
| `models` | The relay's model list (needs a credential, so it wakes a grid) |
| `node-registration`, `node-heartbeat`, `node-poll`, `node-upload` | A provider's traffic: register, heartbeat, poll, result and error report |
| `cli-error` | The public CLI's `--json` refusal envelope, and `--no-wake` |
| `timing` | The timing values two or more parties must agree on |

Each schema's `description` says why the shape is the way it is. Named values carry an `x-constant` (a
`const` a program imports), `x-route` (a route's path), `x-names` (a flag or a header name) or `x-status` (the
HTTP status a refusal code travels with); `x-headers` documents the headers an answer may carry.

What checks it, in this repository's CI: every answer shape against a real recorded answer; every request
shape against what this CLI really sends (`register`, `heartbeat`, `poll`, the result and the error report,
caught at its HTTP boundary); the rules between timing values; and every value this CLI still writes by hand.

## Using it

```python
from grid_protocol import constants
from grid_protocol import validator            # needs the `validate` extra

if answer.get("code") == constants.GRID_ASLEEP_CODE: ...
validator("refusal", "GridAsleep").validate(answer)
```

`grid_protocol.constants` is plain Python with no dependencies, so a single-binary build needs nothing else.
TypeScript gets the same values and one type per shape in `typescript/gridProtocol.ts`, which the harness
copies in whole.

## Changing a shape

1. Edit the schema. A change a released `grid` CLI cannot read is a breaking change: the public CLI is the one
   party that cannot be upgraded at once (0.3.47 onwards is supported).
2. Run `python -m grid_protocol._codegen --write` and commit both generated files.
   `tests/test_protocol_package.py` fails while they are stale.
3. Record a real answer for any new answer shape under `recordings/` (sanitized; see below).
   `tests/test_protocol_contract.py` fails while an answer shape has neither a recording nor a stated reason.
4. Bump `version` in `pyproject.toml` and `__version__` in `grid_protocol/__init__.py`, regenerate, and have
   the operator tag `protocol-vX.Y.Z` — the release workflow publishes the wheel.

## Recordings

`recordings/*.json` are real answers, each validated against the shape it names. Every identifying value is
replaced before a recording is committed — machine names become `node-a`, provider addresses
`provider@example.com`, node ids `grid-000…01` (in route ids too), the per-model hashes in route ids `model0000001`,
and device labels `example-device` — because this repository is public. `test_a_recording_is_sanitized` fails on an
IP address, a token, an address outside `example.com` or a real node id anywhere in a recording.
`source` says where the answer came from: the DEV VM, or a server's own code run in-process at a named sha
when the platform cannot produce the answer without a write.

