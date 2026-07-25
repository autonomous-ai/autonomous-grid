"""CRM export -> training files: prompts.jsonl (RL), labels.jsonl (rewards), sft.jsonl.

Input: CSV or JSONL, one record per lead with a KNOWN outcome:
  lead      - the inbound message / form text / call notes   (required)
  outcome   - what happened (see OUTCOME_TO_LABEL below)     (required)
  context   - company size, source, product line, ...        (optional, joined into prompt)

Edit OUTCOME_TO_LABEL to match your pipeline's stage names — that mapping IS the ground truth
the model trains toward. Classes are balanced by downsampling the majority so the reward can't
be farmed by always predicting the most common label.

    python prepare_data.py --input leads.sample.jsonl
"""
from __future__ import annotations

import argparse
import csv
import json
import random
from collections import Counter
from pathlib import Path

OUTCOME_TO_LABEL = {
    "closed-won": "hot",
    "demo-booked": "hot",
    "qualified": "warm",
    "meeting-held": "warm",
    "closed-lost": "warm",     # engaged enough to lose late = real lead
    "no-response": "cold",
    "disqualified": "cold",
    "spam": "cold",
}

SYSTEM_PROMPT = (
    "You triage inbound sales leads. Reply in EXACTLY this shape and nothing else:\n"
    "PRIORITY: <hot|warm|cold>\nNEXT: <one concrete next action>"
)


def read_records(path: Path) -> list[dict]:
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    else:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [{k.lower().strip(): v for k, v in row.items()} for row in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", required=True)
    parser.add_argument("--out-dir", default=".")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    by_label: dict[str, list[dict]] = {"hot": [], "warm": [], "cold": []}
    unknown: Counter = Counter()
    for row in read_records(Path(args.input)):
        lead = (row.get("lead") or "").strip()
        outcome = (row.get("outcome") or "").strip().lower()
        if not lead:
            continue
        label = OUTCOME_TO_LABEL.get(outcome)
        if label is None:
            unknown[outcome] += 1
            continue
        context = (row.get("context") or "").strip()
        prompt = f"Lead: {lead}" + (f"\nContext: {context}" if context else "")
        by_label[label].append({"prompt": prompt, "label": label})

    counts = {k: len(v) for k, v in by_label.items()}
    if not any(counts.values()):
        raise SystemExit(f"prepare_data: no usable leads (unknown outcomes: {dict(unknown)})")
    floor = min(c for c in counts.values() if c) or 1
    kept = []
    for label, rows in by_label.items():
        rng.shuffle(rows)
        kept.extend(rows[:floor])
    rng.shuffle(kept)

    out_dir = Path(args.out_dir)
    with (out_dir / "prompts.jsonl").open("w", encoding="utf-8") as fh:
        for row in kept:
            fh.write(json.dumps({"prompt": row["prompt"]}) + "\n")
    with (out_dir / "labels.jsonl").open("w", encoding="utf-8") as fh:
        for row in kept:
            fh.write(json.dumps(row) + "\n")
    with (out_dir / "sft.jsonl").open("w", encoding="utf-8") as fh:
        for row in kept:
            fh.write(json.dumps({"messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": row["prompt"]},
                {"role": "assistant", "content": f"PRIORITY: {row['label']}\nNEXT: "},
            ]}) + "\n")

    print(f"prepare_data: raw distribution {counts}; kept {len(kept)} "
          f"({floor} per class, balanced)")
    if unknown:
        print(f"  unmapped outcomes skipped: {dict(unknown)} — extend OUTCOME_TO_LABEL")
    print("Files: prompts.jsonl, labels.jsonl, sft.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
