"""The hello-world RL task: reverse the word order of a short phrase.

Why this task is the field's smoke test (prime-rl's own CI trains "reverse-text"): the reward
is deterministic and dense (partial credit, no LLM judge), there is nothing to download (tasks
are generated from a word bank), a 0.5B instruct model starts imperfect but not hopeless — so
a working GRPO loop shows a visible climb in tens of steps, and a broken one visibly doesn't.
Word-level (not character-level) reversal on purpose: BPE tokenization makes character reversal
gratuitously hard for small models, which would test the model, not the loop.

Everything in this module is pure Python so it runs — and is tested — on any machine.
"""
from __future__ import annotations

import difflib
import random
import re

_WORD_BANK = ["red", "blue", "green", "black", "white", "silver", "amber", "copper", "violet", "coral", "house", "tree", "river", "stone", "cloud", "bridge", "garden", "window", "candle", "mirror", "fox", "owl", "bear", "wolf", "crane", "otter", "heron", "badger", "salmon", "sparrow", "run", "jump", "swim", "climb", "drift", "glide", "wander", "gather", "listen", "carry", "cold", "warm", "bright", "quiet", "heavy", "gentle", "hollow", "steady", "narrow", "golden"]

SYSTEM_PROMPT = (
    "You reverse word order. Reply with ONLY the words in reverse order, "
    "separated by single spaces. No punctuation, no explanations. "
    "Example: 'Reverse the words: sun moon star' -> 'star moon sun'"
    # The worked example is deliberate: it lifts small models' baseline enough that some
    # completions in a group score above others — GRPO learns from reward *spread*, and a
    # flat-zero group teaches nothing.
)


def make_task(rng: random.Random, min_words: int = 3, max_words: int = 6) -> tuple[str, str]:
    """One task: (user prompt, target answer). Words are sampled without replacement so the
    target is unambiguous even if the model reorders duplicates."""
    words = rng.sample(_WORD_BANK, rng.randint(min_words, max_words))
    prompt = "Reverse the words: " + " ".join(words)
    target = " ".join(reversed(words))
    return prompt, target


def make_eval_set(
    seed: int, n: int, min_words: int = 3, max_words: int = 6
) -> list[tuple[str, str]]:
    """Fixed held-out tasks; a separate seed stream from training so the eval never leaks."""
    rng = random.Random(seed)
    return [make_task(rng, min_words, max_words) for _ in range(n)]


def normalize(text: str) -> list[str]:
    """Completion -> word list for grading: lowercase, strip punctuation, split on whitespace."""
    return re.sub(r"[^a-z\s]", " ", text.lower()).split()


def reward(completion: str, target: str) -> float:
    """Dense reward in [0, 1]: word-sequence similarity to the target, 1.0 for exact.

    SequenceMatcher over *word lists* gives partial credit for partially-correct order —
    the gradient signal that makes the climb visible early — while only the exact reversal
    earns 1.0. Empty or off-format output degrades toward 0 naturally.
    """
    predicted = normalize(completion)
    expected = normalize(target)
    if not predicted:
        return 0.0
    return difflib.SequenceMatcher(None, predicted, expected).ratio()


def group_advantages(rewards: list[float], eps: float = 1e-6) -> list[float]:
    """GRPO advantages: per-group mean-centered, std-normalized. A group with no reward
    spread returns all zeros — the caller should skip the update (nothing to learn)."""
    n = len(rewards)
    mean = sum(rewards) / n
    var = sum((r - mean) ** 2 for r in rewards) / n
    std = var**0.5
    if std < eps:
        return [0.0] * n
    return [(r - mean) / (std + eps) for r in rewards]
