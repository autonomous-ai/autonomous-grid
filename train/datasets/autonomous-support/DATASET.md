# autonomous-support-tickets

1,070 support tickets with the reply that resolves them, for training a reply-drafting model.

| | |
|---|---|
| rows | 1,070 (963 train / 107 held out by `grid train sft`) |
| distinct replies | 1,012 · distinct bodies 1,066 |
| reply length | 67–107 words, mean 88 |
| shape | `{subject, body, reply, resolved}`, plus `facts.jsonl` answer key |
| regenerate | `python make_tickets.py` — seeded, so it reproduces exactly |

## Where it comes from

**Synthetic text, grounded in Autonomous's published technical facts.** Every resolution in here is
one the company actually documents:

- error codes **E01** (motor overheat — power off, unplug, 20 minutes), **E03** (obstacle
  detection), **E04** (control box must be mounted flat and horizontal, fixings tight, path clear)
  and **E12** (hard reset, then swap the motor leg cables between control-box ports to isolate leg
  from box) — from the help centre's desk error-code page
- overload protection locking descent when the weight limit is exceeded
- shipping windows: US 3–5 business days, Canada 5–7, most of the EU 7–9, UK and Switzerland 9–11
- warranty: 2 years on furniture and ErgoChair Pro, 1 year structural on WorkPod Pro; 30-day
  return for a full refund in the US

## Why it is not scraped

Trustpilot was the obvious source and it is the wrong one, for three reasons that compound.

It returns **403** to automated fetches. Its content is **other people's words about their own
experiences**, which is not something to copy into a training set because it is convenient. And
the decisive one: **a review is not a ticket, and a public brand reply is not a support answer.**
Replies on review sites are short deflections — *sorry to hear that, please email support@* — so a
model trained on them learns to deflect. That is worse than no model.

What a support model has to learn is the *correct fix*, stated specifically. So the corpus is built
from the published technical truth instead: the tickets are invented, the resolutions are real.

## Honest limits

- **Synthetic distribution.** Real inboxes are messier — misspellings, missing order numbers,
  three problems in one message, people who are angrier than this. A model trained only on this
  will be over-confident on tidy input.
- **No multi-turn.** Every row is one ticket and one reply. Real support is a thread.
- **Replies are uniformly good.** A real corpus contains replies the team would not send again;
  learning only from the good ones is the intent for stage one, but it means this cannot teach a
  model what a *bad* reply looks like.
- **The right next step is real history.** `grid train pull zendesk` replaces this with actual
  resolved tickets, and this corpus stops being training data and becomes a smoke test.

## Used by

**`sft-mlx-qwen2.5-7b-instruct-4bit-20260726-093515`** — 1,070 rows, Qwen2.5-7B, 600 iters,
loss 4.398 → 0.171, 29.8 min on an M2 Max. Graded by `eval_facts.py` on 50 held-out tickets,
against the same base model with no adapter as the control:

| | base 7B | trained |
|---|---|---|
| correct fix stated | 10% | **96%** |
| cites the order id | 66% | **96%** |
| speaks as the agent | 100% | 100% |
| no invented history | 100% | 100% |
| **all four** | **6%** | **94%** |

**What that does and does not prove.** The fix and order-citation gains are attributable to the
adapter: same base, same tickets, the adapter is the only variable, and 10% → 96% is not noise.
The other two rows prove nothing here — the base 7B already scored 100% on both, so this run
cannot say whether the corpus fixes for role-bleed and invented history work. Those were 1.5B
failures, and the model changed at the same time as the corpus. If that matters, the test is a
1.5B rerun on the current corpus.

The three remaining failures are not random. Two are E03 borrowing another code's procedure — one
recites the E01 unplug sequence — and E03 is the only issue whose documented fix is physical
("clear the obstruction") rather than a numbered procedure, so it has the least distinctive
vocabulary to key on. It is the hardest row in the set. The third dropped the order id from an
otherwise correct wobble reply.

**`sft-mlx-qwen2.5-1.5b-instruct-4bit-20260726-083026`** — loss 3.88 → 0.587, val 0.672, 21.7 min
on an M2 Max. **The loss curve is clean and the model is not usable.** Recorded here because a
falling loss is exactly what makes this failure easy to miss.

Held-out tickets, graded against the documented fix:

| ticket | voice | cites order | fix correct |
|---|---|---|---|
| E12 after reassembly | yes | no | **no** — invented "E12 is the factory calibration" |
| E01 overheat | yes | yes | **no** — said reset/check obstruction, not unplug-20-minutes |
| E03 obstruction | yes | yes | roughly, but invented "has had E03 three times" and "I have raised it with the manufacturer" |
| UK shipping, 19 days | **no** | no | **no** — wrote as the *customer*, not the agent |

What it learned is the **register** — open by naming the error, be direct, no "thank you for
contacting us". What it did not learn is the **facts**, and those two coming apart is the whole
problem: a model that sounds like a senior support engineer and gives the wrong reset procedure is
more dangerous than the untrained model, which was vague and harmless.

Two causes, one in the method and one in this corpus:

1. **150 examples on a 1.5B model transfers style, not knowledge.** Only 12 rows mention E12 and
   14 mention E01. That is enough to learn the shape of an answer and not enough to learn which
   answer.
2. **A corpus flaw this exposed.** The generator puts frustration lines ("I work from home so this
   is blocking me") in ticket *bodies* and urgency ("a status within 48 hours") in *replies*. With
   only 14 shipping rows the model blended the two roles and answered in the customer's voice.
   Fix before the next run: keep role-marking vocabulary disjoint between body and reply.
