"""Grade a trained model on whether its replies are CORRECT, not whether they sound right.

The 1.5B run made the case for this. Its loss fell cleanly from 3.88 to 0.587 and
its replies read like a competent support engineer, and it told a customer that
"E12 is the factory calibration" and gave the wrong reset for E01. Loss measures
agreement with the training distribution; it cannot see that a fluent answer is
false. So this grades four things a reply has to get right, none of which loss
covers:

  facts    every string in the ticket's `must_contain` appears. These come from
           Autonomous's published resolutions — "20 minutes" and "unplug" for
           E01, "hard reset"/"swap"/"cable" for E12 — so a reply that omits one
           is missing the actual fix, however well it reads.
  order    the reply cites the order id from the ticket. The similarity reward
           cannot distinguish a reply that grounds itself in this ticket from
           one that recites the house pattern.
  role     the reply does not answer in the CUSTOMER's voice. The 1.5B model did
           exactly that on a shipping ticket, and no loss curve showed it.
  clean    no invented history ("I have raised this with the manufacturer",
           "this has happened three times"), which is the failure that looks
           most like competence.

Usage:
    python eval_facts.py <adapter-path|none> [--n 60] [--model <mlx-model>]

Held-out selection is by hash of the ticket body, so the same rows are graded
across every run and two models are comparable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys

DEFAULT_MODEL = "mlx-community/Qwen2.5-7B-Instruct-4bit"
SYS = ("You are the support agent. Read the ticket and draft the reply to send. "
       "Be specific to this ticket, resolve the issue, and keep the reply under 160 words.")

# Phrases that mark the customer speaking. A reply containing one has swapped roles.
CUSTOMER_VOICE = [
    "i work from home", "this is blocking me", "i am running out of patience",
    "i need this sorted", "my whole setup is unusable", "i have written in",
    "i would rather not have to chase", "i am getting nowhere",
    "please advise", "any help appreciated", "hoping you can help",
]
# Claims about history or third parties that no ticket supports.
INVENTED = [
    "raised the issue with the manufacturer", "raised it with the manufacturer",
    "has had", "three times", "i have already had to", "spoken to the factory",
    "our engineers have confirmed", "we have seen this before on your",
]


def held_out(rows, n):
    """Deterministic slice: hash the body so the set is stable across runs."""
    ranked = sorted(rows, key=lambda r: hashlib.sha1(r["body"].encode()).hexdigest())
    return ranked[:n]


def grade(reply: str, must: list[str], body: str) -> dict:
    low = reply.lower()
    missing = [m for m in must if m.lower() not in low]
    order_ids = re.findall(r"AN-\d{6}", body)
    return {
        "facts": not missing,
        "missing": missing,
        "order": (not order_ids) or any(o in reply for o in order_ids),
        "role": not any(p in low for p in CUSTOMER_VOICE),
        "clean": not any(p in low for p in INVENTED),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("adapter", help="adapter dir, or 'none' for the base model")
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--facts", default="facts.jsonl")
    ap.add_argument("--show", type=int, default=3, help="print this many failures")
    args = ap.parse_args()

    with open(args.facts) as fh:
        rows = [json.loads(line) for line in fh]
    sample = held_out(rows, args.n)

    from mlx_lm import generate, load
    adapter = None if args.adapter.lower() == "none" else args.adapter
    model, tok = load(args.model, adapter_path=adapter) if adapter else load(args.model)

    tally = {"facts": 0, "order": 0, "role": 0, "clean": 0}
    failures = []
    for i, r in enumerate(sample, 1):
        prompt = tok.apply_chat_template(
            [{"role": "system", "content": SYS},
             {"role": "user", "content": f"Subject: {r['subject']}\n\n{r['body']}"}],
            add_generation_prompt=True, tokenize=False)
        reply = generate(model, tok, prompt=prompt, max_tokens=230, verbose=False).strip()
        g = grade(reply, r["must_contain"], r["body"])
        for k in tally:
            tally[k] += bool(g[k])
        if not all(g[k] for k in tally):
            failures.append((r, reply, g))
        print(f"\r  graded {i}/{len(sample)}", end="", file=sys.stderr, flush=True)
    print("", file=sys.stderr)

    n = len(sample)
    label = "BASE (untrained)" if adapter is None else args.adapter.split("/")[-2]
    print(f"\n{label}   n={n}   model={args.model}")
    for k, name in (("facts", "correct fix stated"), ("order", "cites the order id"),
                    ("role", "speaks as the agent"), ("clean", "no invented history")):
        pct = 100 * tally[k] / n
        bar = "#" * round(pct / 4)
        print(f"  {name:<22} {tally[k]:>3}/{n}  {pct:5.1f}%  {bar}")
    allpass = sum(1 for r, _, g in failures if False) + (n - len(failures))
    print(f"  {'ALL FOUR':<22} {allpass:>3}/{n}  {100*allpass/n:5.1f}%")

    for r, reply, g in failures[:args.show]:
        bad = [k for k in tally if not g[k]]
        print(f"\n  --- failed {bad} on: {r['subject']}")
        if g["missing"]:
            print(f"      missing facts: {g['missing']}")
        print("      " + reply[:300].replace("\n", " "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
