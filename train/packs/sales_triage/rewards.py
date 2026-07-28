"""Rewards for lead triage. The priority label is checked against your CRM's real outcome —
a verifiable reward, the strongest kind a business dataset offers."""
from __future__ import annotations

import json
import re
from pathlib import Path

_LABELS: dict[str, str] = {}
_labels_file = Path(__file__).parent / "labels.jsonl"
if _labels_file.is_file():
    for _line in _labels_file.read_text(encoding="utf-8").splitlines():
        _row = json.loads(_line)
        _LABELS[_row["prompt"]] = _row["label"]

_PRIORITY = re.compile(r"^PRIORITY:\s*(hot|warm|cold)\s*$", re.IGNORECASE | re.MULTILINE)
_NEXT = re.compile(r"^NEXT:\s*(\S.*)$", re.IGNORECASE | re.MULTILINE)


def _extract(text: str) -> str | None:
    match = _PRIORITY.search(text)
    return match.group(1).lower() if match else None


def reward_correct_priority(prompts, completions=None, completions_text=None, **kwargs):
    """1.0 when the predicted priority matches the historical outcome's label; the core."""
    texts = completions_text if completions_text is not None else completions
    scores = []
    for prompt, text in zip(prompts, texts):
        truth = _LABELS.get(prompt)
        predicted = _extract(text)
        if truth is None:
            scores.append(0.5)  # blind on unknown prompts — never push the policy
        else:
            scores.append(1.0 if predicted == truth else 0.0)
    return scores


def reward_parseable(prompts, completions=None, completions_text=None, **kwargs):
    """The output must drive automation: exactly one PRIORITY line, a non-empty NEXT line,
    nothing rambling (<= 4 lines)."""
    texts = completions_text if completions_text is not None else completions
    scores = []
    for text in texts:
        score = 0.0
        if len(_PRIORITY.findall(text)) == 1:
            score += 0.6
        if _NEXT.search(text):
            score += 0.3
        if len([line for line in text.strip().splitlines() if line.strip()]) <= 4:
            score += 0.1
        scores.append(score)
    return scores
