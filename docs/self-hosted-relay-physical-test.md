# Self-hosted relay physical acceptance

Status: **in progress**. The relay PR remains draft until Machine B and the forced three-machine
Goal handoff pass.

Test date: 2026-09-01

## Revisions and topology

- Public member CLI/Goal worker: `grid-relay` at `c0dee9c`
- Separate relay/server runtime: `grid-relay` at `4bcfefd`
- Fresh isolated Grid: `relay-physical-3`
- Machine A: macOS arm64, Codex task worker
- Machine C: Linux, two NVIDIA RTX 4090 D GPUs, Codex task worker and local
  `qwen3.6-27b` inference provider
- Machine B: pending its physical join

## Passed checks

- A -> self-hosted relay -> C inference returned `RELAY_C_OK`.
- `grid relay info`, `grid relay status` delegation, `grid engines`, `grid goal list`, pairing by
  file, disconnect, revocation, and credential reissue behaved as specified.
- Goal `fd4a7477-c53c-4fb4-bcb6-0e7743dede61` completed in two durable Codex turns using 80,607
  local-model tokens. Relay evaluation scored the exact final commit `1.0` and accepted it.
- Evidence verification passed with required inference and exact clean worker revision `c0dee9c`.
- The completed artifact was readable from the relay-owned project repository and the Goal cleared
  from the active list.
- A live relay restart preserved Goal/eval history; A and C re-registered automatically and
  post-restart inference returned `RESTART_RECOVERY_OK`.
- Public exact-head suite: `3399 passed, 57 skipped, 7 deselected`.
- Public relay/process focused suite: `37 passed`.
- The public sdist and wheel built, installed into a fresh disposable environment, and exposed the
  expected `grid relay` command surface.

## Remaining gates

- Join physical Machine B and prove all three hardware identities.
- Force and verify a B -> A -> C Goal continuation with worker loss.
- Run a full relay down/up recovery cycle.
- Update this record with the final evidence before marking the PR ready.
