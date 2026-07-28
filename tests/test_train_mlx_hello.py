"""Tests for the RL hello world's pure half (task, reward, advantages) — runs on any
machine, no mlx required. The mlx-touching module is import-checked only."""
from __future__ import annotations

import importlib
import random

import pytest

from train.hello_task import (
    group_advantages,
    make_eval_set,
    make_task,
    normalize,
    reward,
)


def test_make_task_target_is_reversal():
    prompt, target = make_task(random.Random(3))
    words = prompt.removeprefix("Reverse the words: ").split()
    assert target.split() == list(reversed(words))
    assert 3 <= len(words) <= 6
    assert len(set(words)) == len(words)  # no duplicates -> unambiguous target


def test_eval_set_is_deterministic_and_disjoint_from_training_stream():
    a = make_eval_set(seed=1017, n=8)
    b = make_eval_set(seed=1017, n=8)
    assert a == b
    # Deterministic given the default seeds (17 train / 17+1000 eval), so assert it for real:
    # 200 training tasks share no prompt with the eval set the trainer actually uses.
    train_rng = random.Random(17)
    train_tasks = {make_task(train_rng)[0] for _ in range(200)}
    assert not train_tasks & {prompt for prompt, _ in a}


def test_reward_exact_match_is_one():
    assert reward("tree house red", "tree house red") == 1.0
    # Grading is format-tolerant: case and punctuation don't cost reward.
    assert reward("  Tree, HOUSE red.", "tree house red") == 1.0


def test_reward_partial_and_empty():
    assert reward("", "tree house red") == 0.0
    partial = reward("tree house blue", "tree house red")
    assert 0.0 < partial < 1.0
    # The unreversed input scores below the exact reversal — the climb has a direction.
    assert reward("red house tree", "tree house red") < 1.0


def test_reward_verbose_answer_scores_below_clean_answer():
    clean = reward("tree house red", "tree house red")
    verbose = reward("Sure! The reversed words are: tree house red", "tree house red")
    assert verbose < clean


def test_normalize_strips_noise():
    assert normalize("Tree,  HOUSE!!\nred.") == ["tree", "house", "red"]


def test_group_advantages_mean_zero_unit_scale():
    adv = group_advantages([0.0, 0.5, 1.0])
    assert sum(adv) == pytest.approx(0.0, abs=1e-9)
    assert adv[0] < adv[1] < adv[2]


def test_group_advantages_no_spread_is_all_zero():
    assert group_advantages([0.7, 0.7, 0.7]) == [0.0, 0.0, 0.0]


def test_grpo_hello_importable_without_mlx():
    # The runnable module must be importable (and fail with a clear message only when *run*)
    # on machines without mlx — x86 CI included.
    module = importlib.import_module("train.mlx.grpo_hello")
    assert hasattr(module, "main")
