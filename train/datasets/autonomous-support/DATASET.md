# autonomous-support-tickets

150 support tickets with the reply that resolves them, for training a reply-drafting model.

| | |
|---|---|
| rows | 150 (135 train / 15 held out by `grid train sft`) |
| distinct replies | 145 |
| reply length | 84–133 words, mean 112 |
| shape | `{subject, body, reply, resolved}` |
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
