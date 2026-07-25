# support-replies — train a reply-drafting model on your resolved tickets

Turn your support history (the tickets your team already answered well) into a model that
drafts replies in your house style, on your own hardware, with the data never leaving your
network.

## The recipe ladder (do them in order)

1. **SFT first.** Your resolved tickets are labeled examples of the behavior you want —
   imitation is the right first tool, and it is cheap. `prepare_data.py` emits `sft.jsonl`
   (chat-format pairs) for any SFT trainer: `mlx_lm.lora` on a Mac
   (`python -m mlx_lm.lora --data . --train`) or TRL's SFTTrainer on a CUDA box.
2. **RL on top, once SFT plateaus.** GRPO sharpens what imitation can't: format discipline,
   grounding (using the ticket's actual order/tracking details), and length control.
   `rewards.py` starts with verifiable, heuristic rewards; the optional LLM-judge reward is
   commented and OFF by default.

## Files

- `prepare_data.py` — your ticket export (CSV or JSONL) → `prompts.jsonl` (RL), `sft.jsonl`
  (SFT), `refs.jsonl` (historical replies, used by the similarity reward).
- `rewards.py` — reward functions (`grid train` picks up every `reward_*`).
- `grid-train.toml` — run config wired to the files above; edit model + endpoint.
- `tickets.sample.jsonl` — synthetic sample data in the expected shape (replace with yours).

## Quick start

```
grid train init --pack support-replies && cd support-replies
python prepare_data.py --input tickets.sample.jsonl   # swap in your real export
grid train doctor && grid train run
```

## Honest limits (read before trusting a climb)

- **The similarity reward teaches your historical style, including its flaws.** It rewards
  closeness to what your team actually wrote — curate the export to *resolved, well-rated*
  tickets or you will faithfully clone mediocrity.
- **Similarity alone can teach parroting.** It is deliberately weighted alongside grounding
  and format rewards; don't run it solo for many steps.
- **The judge reward must stay local.** If you enable it, point it at your own grid endpoint
  (`GRID_JUDGE_URL`), never a cloud API — otherwise every ticket leaves the building and the
  privacy story this product exists for is gone.
