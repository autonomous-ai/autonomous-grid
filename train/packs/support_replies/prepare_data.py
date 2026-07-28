"""Ticket export -> training files: prompts.jsonl (RL), sft.jsonl (SFT), refs.jsonl (rewards).

Input: CSV or JSONL, one record per resolved ticket, fields (case-insensitive):
  subject   - short ticket subject                       (required)
  body      - the customer's message                     (required)
  reply     - the reply your team sent                   (required)
  resolved  - truthy = keep; anything else is skipped    (optional; default keep)

Only records with a real reply survive. Curate upstream: this pipeline clones whatever your
export contains, so feed it your *good* tickets (resolved, well-rated), not everything.

    python prepare_data.py --input tickets.sample.jsonl
    python prepare_data.py --input zendesk_export.csv --min-reply-words 20
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

SYSTEM_PROMPT = (
    "You are the support agent. Read the ticket and draft the reply to send. "
    "Be specific to this ticket, resolve the issue, and keep the reply under 160 words."
)


def read_records(path: Path) -> list[dict]:
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    else:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    return [{k.lower().strip(): v for k, v in row.items()} for row in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", required=True, help="Ticket export (.csv or .jsonl)")
    parser.add_argument("--out-dir", default=".", help="Where the training files go")
    parser.add_argument("--min-reply-words", type=int, default=10,
                        help="Drop tickets whose reply is shorter than this")
    args = parser.parse_args()

    records = read_records(Path(args.input))
    out_dir = Path(args.out_dir)
    kept, skipped = [], 0
    for row in records:
        subject = (row.get("subject") or "").strip()
        body = (row.get("body") or "").strip()
        reply = (row.get("reply") or "").strip()
        resolved = str(row.get("resolved", "true")).lower() not in ("false", "0", "no", "")
        if not (subject or body) or len(reply.split()) < args.min_reply_words or not resolved:
            skipped += 1
            continue
        ticket = f"Subject: {subject}\n\n{body}" if subject else body
        kept.append({"ticket": ticket, "reply": reply})

    if not kept:
        raise SystemExit("prepare_data: no usable tickets (need subject/body + a real reply)")

    with (out_dir / "prompts.jsonl").open("w", encoding="utf-8") as fh:
        for row in kept:
            fh.write(json.dumps({"prompt": row["ticket"]}) + "\n")
    with (out_dir / "sft.jsonl").open("w", encoding="utf-8") as fh:
        for row in kept:
            fh.write(json.dumps({"messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": row["ticket"]},
                {"role": "assistant", "content": row["reply"]},
            ]}) + "\n")
    # refs.jsonl keys the historical reply by the exact prompt string; rewards.py looks it up.
    with (out_dir / "refs.jsonl").open("w", encoding="utf-8") as fh:
        for row in kept:
            fh.write(json.dumps({"prompt": row["ticket"], "reference": row["reply"]}) + "\n")

    print(f"prepare_data: {len(kept)} tickets -> prompts.jsonl, sft.jsonl, refs.jsonl "
          f"({skipped} skipped)")
    # The honest verdict beats a wasted training night: refuse to bless a doomed run.
    if len(kept) < 50:
        print(f"VERDICT: NOT ENOUGH DATA — {len(kept)} usable tickets. SFT wants 100+, the RL "
              "pass a few hundred. Export more history (or lower --min-reply-words) before "
              "spending a night training.")
        return 1
    if len(kept) < 200:
        print(f"VERDICT: OK FOR A FIRST SFT PASS ({len(kept)} tickets). RL will be marginal at "
              "this size — do SFT on sft.jsonl first and judge that before the RL night.")
    else:
        print(f"VERDICT: GOOD TO GO ({len(kept)} tickets). SFT on sft.jsonl first, then "
              "`grid train run`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
