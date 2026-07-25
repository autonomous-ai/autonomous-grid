"""Rewards for support-reply drafting. `grid train` uses every `reward_*` function here.

Three heuristic rewards ship ON (verifiable, zero-dependency, safe to start with); the
LLM-judge reward ships OFF. The mix matters: similarity alone teaches parroting, format alone
teaches empty politeness — together with grounding they approximate "a specific, on-style,
actually-responsive reply" without a judge.
"""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

_REFS: dict[str, str] = {}
for _line in (Path(__file__).parent / "refs.jsonl").read_text(encoding="utf-8").splitlines() \
        if (Path(__file__).parent / "refs.jsonl").is_file() else []:
    _row = json.loads(_line)
    _REFS[_row["prompt"]] = _row["reference"]

# Words that never appear in a reply a human would trust (see knowledge/brand-voice.md).
_BANNED = {"revolutionary", "empower", "supercharge", "unlock", "transform", "leverage",
           "synergy", "delve", "elevate"}
_PLACEHOLDER = re.compile(r"\[(?:name|order|tracking|date|link|company)[^\]]*\]|\{\{[^}]*\}\}|xxx+", re.IGNORECASE)


def reward_similarity(prompts, completions=None, completions_text=None, **kwargs):
    """Closeness to the reply your team actually sent (0..1). Blind (0.5) when no reference —
    a missing ref must not push the policy anywhere."""
    texts = completions_text if completions_text is not None else completions
    scores = []
    for prompt, text in zip(prompts, texts):
        reference = _REFS.get(prompt)
        if not reference:
            scores.append(0.5)
            continue
        scores.append(difflib.SequenceMatcher(None, text.lower().split(),
                                              reference.lower().split()).ratio())
    return scores


def reward_grounding(prompts, completions=None, completions_text=None, **kwargs):
    """Does the reply engage with THIS ticket? Fraction of the ticket's distinctive tokens
    (order ids, product names, numbers) the reply picks up (0..1)."""
    texts = completions_text if completions_text is not None else completions
    scores = []
    for prompt, text in zip(prompts, texts):
        distinctive = set(re.findall(r"[A-Z]{2,}[-\d]*\d|\d{4,}|[A-Z][a-z]+[A-Z]\w+", prompt))
        if not distinctive:
            scores.append(0.5)
            continue
        hit = sum(1 for token in distinctive if token in text)
        scores.append(hit / len(distinctive))
    return scores


def reward_format(prompts, completions=None, completions_text=None, **kwargs):
    """Shippable-reply hygiene: sane length, no template placeholders, no marketing words."""
    texts = completions_text if completions_text is not None else completions
    scores = []
    for text in texts:
        words = text.split()
        score = 1.0
        if not 20 <= len(words) <= 180:
            score -= 0.5
        if _PLACEHOLDER.search(text):
            score -= 0.5
        if any(word.lower().strip(".,!") in _BANNED for word in words):
            score -= 0.3
        scores.append(max(score, 0.0))
    return scores


# --- Optional: LLM-judge reward (OFF by default) -------------------------------------------
# Rename to `reward_judge` to enable. Point GRID_JUDGE_URL at a model served by YOUR grid —
# never a cloud API: every ticket would leave the network and break the reason this exists.
def _judge(prompts, completions=None, completions_text=None, **kwargs):
    import os

    import httpx

    url = os.environ["GRID_JUDGE_URL"].rstrip("/")
    model = os.environ.get("GRID_JUDGE_MODEL", "auto")
    texts = completions_text if completions_text is not None else completions
    scores = []
    with httpx.Client(timeout=60.0) as client:
        for prompt, text in zip(prompts, texts):
            response = client.post(f"{url}/chat/completions", json={
                "model": model,
                "messages": [{"role": "user", "content":
                    "Score this support reply 0-10 for: resolves the issue, specific to the "
                    f"ticket, professional tone.\n\nTICKET:\n{prompt}\n\nREPLY:\n{text}\n\n"
                    "Answer with ONLY the number."}],
                "max_tokens": 4,
            })
            match = re.search(r"\d+", response.json()["choices"][0]["message"]["content"])
            scores.append(min(int(match.group()) if match else 0, 10) / 10)
    return scores
