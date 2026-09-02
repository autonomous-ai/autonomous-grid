# Self-hosted relay physical acceptance

Status: **passed** on 2026-09-02.

## Revisions and topology

- Public member CLI/workers: `grid-relay` at `4feb975`
- Goal verifier: `grid-goal-distributed` at `bdf3e52`
- Separate relay/server package: `autonomous-grid-cli` `grid-relay` at `4f33900`
- Isolated Grid `relay-physical-4` (`grid-relay-physical-4-bfc35758`)
- Relay `relay-01d1ed2cc8fd4a0f8298342bc7789838`
- Machine A: Apple M2 Max, relay host and Codex task worker
- Machine C: Linux, two NVIDIA RTX 4090 D GPUs, `qwen3.6-27b` inference and Codex tasks
- Machine D: Linux, one NVIDIA RTX 4090 D GPU, Codex tasks

## Passed checks

- Pairing, node join/rejoin, relay info/status/list JSON, inference, member revocation/reissue, and
  automatic worker recovery passed without copying Grid homes or task roots.
- Individual Goals ran successfully on A, C, and D with independent eval score `1.0` and strict
  evidence verification.
- Forced handoff Goal `a475278a-58d1-4342-ab4f-347fb47005d1` continued A -> D -> C after two
  deliberate worker losses. It recorded four ordered Codex attempts across three execution nodes,
  used 2,261,947 local-model tokens through Grid inference, repaired an initial failed eval, passed
  all seven final evals, and produced commit `8ac063f72ea2ff43e3364fb32a31f7aff07197a3`.
- Strict evidence required three execution nodes, Grid inference, the exact clean worker revision,
  and ordered agent sequence. The completed Goal cleared from the active queue.
- A full relay/PostgreSQL down/up cycle preserved identity and history. A post-restart Goal ran on D
  with inference on C, used 41,496 tokens, scored `1.0`, and passed strict revision evidence.
- A real disaster restore deleted the test relay's dedicated PostgreSQL container and volume, then
  restored solely from a mode-`0600` backup. Relay/Grid/server ids, all five members, node
  credentials, Goal/eval history, usage, and all seven Git project repositories survived. C and D
  reconnected automatically, inference returned `RESTORE_OK`, strict historical evidence passed,
  and the restored game cloned at the exact evaluated commit and passed all seven behavior tests.
- A fresh post-restore Goal then wrote a new project on Machine D using Machine C inference. It used
  41,842 tokens, scored `1.0`, passed strict worker/inference evidence, cloned its exact evaluated
  commit through the recovered Git plane, and cleared from the active queue.
- The recovery drill found and fixed the old backup's missing Git plane. Backup v2 uses checksummed
  Git bundles, restores all refs, runs `git fsck`, streams large members, rejects malformed
  archives, and isolates project storage per relay on every OS.
- Public exact-head suite: `3405 passed, 57 skipped, 7 deselected`; lint, Python
  3.11/3.12/3.13, and Windows CI passed.
- Public and private sdists/wheels built and installed in fresh environments. `grid relay`,
  `grid goal`, and `grid-relay` entry points passed their package smoke tests.
