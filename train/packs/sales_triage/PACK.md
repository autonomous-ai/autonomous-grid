# sales-triage — train a lead-qualification model on your closed outcomes

Train a small model to read an inbound lead and produce the triage your best rep would —
priority + reason + next action — using your CRM history (leads whose outcomes you already
know) as ground truth. The reward is **verifiable**: the model's priority call is checked
against what actually happened, so there is no judge to fool and no style to parrot. This is
the cleanest reward shape a business dataset offers — start here if you're choosing your
first pack.

## How the labels work

`prepare_data.py` maps outcomes to priorities (edit the mapping to your pipeline's truth):

| outcome (yours)                    | label |
|------------------------------------|-------|
| closed-won                         | hot   |
| qualified / meeting-held / lost-late | warm |
| no-response / disqualified / spam  | cold  |

The model must answer in a strict shape — `PRIORITY: <hot|warm|cold>` then `NEXT: <action>` —
and is rewarded for (1) the correct priority (the verifiable core), (2) keeping the shape
parseable (so the output can drive automation), with the label distribution rebalanced at
prep time so "cold" (usually the majority class) can't be farmed by always answering cold.

## Files

- `prepare_data.py` — CRM export (CSV/JSONL: lead text + outcome) → `prompts.jsonl`,
  `labels.jsonl`, `sft.jsonl` (balanced; also prints your class distribution).
- `rewards.py` — exact-label reward + format reward.
- `grid-train.toml` — run config.
- `leads.sample.jsonl` — synthetic sample in the expected shape.

## Quick start

```
grid train init --pack sales-triage && cd sales-triage
python prepare_data.py --input leads.sample.jsonl
grid train doctor && grid train run
```

## Honest limits

- **Your labels encode your past pipeline, not truth.** A lead your team ignored gets "cold"
  even if it was gold. The model learns your history's judgment — audit the mapping.
- **Class balance is enforced at prep time** (downsampling the majority); check the printed
  distribution before training on a skewed export.
- **Priority is verifiable; `NEXT:` is not.** The next-action text is only format-checked
  here. Don't read quality into it until you add a judge or SFT it on your best reps' notes.
