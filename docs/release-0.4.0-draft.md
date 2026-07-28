# v0.4.0 — training, on the computers you already own (DRAFT)

**Not tagged.** This is the notes draft for the `grid-rl` branch, for Dee to edit and cut when the
branch merges. Tagging publishes a GitHub release, so that stays a human decision. The mechanics
are in the release-grid-cli skill; the short version is: bump `pyproject.toml` to 0.4.0, then
`git tag -a v0.4.0 -F <this file>` and push the tag to `public`.

---

Grid could run inference on machines you own. It can now **train** on them too — and the models
get better on their own, overnight, from the work your team is already doing.

## The one-paragraph version

Point Grid at your own examples — a helpdesk export, a CRM export, or the traffic already flowing
through your grid — and it trains a small model to do one job the way your team does it. Training
runs entirely on your machines: MacBooks, a Mac Studio, an RTX box, or all three at once. The
result only gets served if it beats the model you already had, measured on work it has never seen.
Nothing is uploaded anywhere.

## What is actually here

**Two ways in.** `grid train` for engineers; `grid train web` for the people whose work the model
is learning — a support manager, a sales manager, a logistics lead. Same engine, same config file,
no vocabulary to learn. Five steps: your examples, what good looks like, machines, learning,
result.

**Two rungs.** `grid train sft` imitates the answers your team already wrote and needs nothing but
the computer in front of you. `grid train run` sharpens it with a feedback loop (GRPO through TRL)
and needs an engine that can serve attempts. Apple Silicon does both — the MLX rollout server
means an all-Mac office needs no NVIDIA card at all.

**Four jobs out of the box.** Draft support replies · prioritise inbound leads · sort work into
your own categories (routing, tagging, triage — exact reward, checked against your team's own past
choices) · anything else your team answers in writing.

**A gate that refuses.** Every run scores the incumbent and the candidate over a held-out slice
with greedy decoding, per grader. A candidate that gains less than 0.01 overall, or loses more than
0.02 on any single grader, does not get served. Refusing is the normal, healthy outcome and the
interface says so.

Several defects in that gate were found and fixed in the two nights before this release, and they
are worth stating plainly because they are the kind that pass a test suite: the imitation rung's
"held-out" slice overlapped the training set (8 rows in 10); the unattended cycle scored the model
already being served rather than the candidate; a night that lost was recorded with the same word
as a night that won; and the first repair for that third one assumed a candidate could be loaded
beside the incumbent, which is impossible on Grid's own MLX engine — it holds one set of weights —
so it briefly made every night score a model against itself at exactly +0.000.

The gate now scores the incumbent first, while it is still what the node holds, then loads the
candidate and scores that, and then restores the incumbent — always, whatever the verdict, because
a check is supposed to be an observation. The winner is deployed once, afterwards, by the caller. The measured climbs
below are unaffected — they come from the feedback rung, whose split was always correct — but any
imitation number produced before this release was partly memorisation.

**It improves overnight.** `grid train collect --on` keeps the work the grid does (locally,
redacted, pruned). `grid train autopilot` turns that into a night's training. `grid train schedule
on` puts it in the computer's own scheduler — a LaunchAgent on macOS, a `systemd --user` timer on
Linux. The rule that keeps it honest: **a model's own unjudged output is stored but never trained
on**; an example earns its place from a human correction (1.0), a stronger model's answer (0.8), or
"nobody complained" (0.6), and a rejected answer is never imitated.

**It waits for the machine to be free.** Mains power and an idle keyboard, checked in code — a
training run must never be the reason someone's laptop is slow at 3pm.

## Measured, not asserted

| what | result |
|---|---|
| single process, CPU, word-reversal task | 0.220 → 0.674 in 5.6 min |
| two processes, weights synced every 2 steps | 0.188 → **0.768** in 6.1 min |
| same, synced every 5 steps | 0.188 → 0.261 — below the bar |

The third row is the point: sync cadence is the dial, and distributed beat single-process on the
same wall clock only because the weights moved often enough.

## Numbers a room should hear

- 1,078 tests green.
- The whole training plane is 62 files and about 10,600 lines, in `train/`.
- One unauthenticated request could stall a grid for 49 seconds before v0.4.0. It can't now
  (bounded quantifiers, clip-before-scan, a body cap, and a background writer).

## Known limits

- **Local-mode node auth is unresolved.** An unauthenticated `PUT /nodes/{id}` on a LAN means a
  peer can redirect a node's endpoint — with training in the picture that is a rollout-poisoning
  vector, not just a nuisance. No public privacy claim until this or relay-on-prem is settled.
- Placement is manual: `[rollout].target_provider` pins rollouts to one engine, and pool isolation
  means a second grid.
- The RL rung needs an engine that returns sampled token ids and their logprobs. vLLM does, and so
  does Grid's own MLX server. A text-only chat API yields zero trainable samples, and
  `grid train doctor` says so before you waste a night.
