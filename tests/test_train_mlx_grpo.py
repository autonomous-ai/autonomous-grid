"""The MLX feedback stage: held-out split, reward totalling, and backend dispatch.

None of these import mlx. The GRPO arithmetic needs an Apple Silicon machine and is verified by
running `python -m train.mlx.grpo_hello` (a fixed seed, so the trajectory is reproducible); what
is checkable everywhere is the wiring around it, and the wiring is where the bugs were.
"""
from __future__ import annotations

import argparse
import types

import pytest

from train.mlx.grpo import score, split_holdout


def test_the_held_out_prompts_are_the_ones_the_trainer_never_sees():
    """Disjoint, and stable across runs — the property the gate's meaning rests on.

    The torch side once shuffled to choose what to withhold and then sliced the ORIGINAL list to
    choose what to train on, so 8 of every 10 "unseen" prompts had been trained on and a model
    could pass by memorising. One shuffle, both halves taken from it.
    """
    prompts = [f"ticket {i}" for i in range(200)]
    held, learn = split_holdout(prompts)

    assert not (set(held) & set(learn))            # no overlap, at all
    assert set(held) | set(learn) == set(prompts)  # and nothing lost
    assert split_holdout(prompts) == (held, learn)  # same answer next run


def test_the_held_out_set_stays_usable_on_a_tiny_corpus():
    """A handful of prompts must still leave something to train on AND something to score."""
    held, learn = split_holdout([f"t{i}" for i in range(6)])
    assert held and learn
    assert not (set(held) & set(learn))


def test_every_reward_function_is_summed():
    """TRL sums `reward_funcs`; a different rule here would score the same rewards.py
    differently on the two backends, which is the one thing the packs cannot tolerate."""
    def reward_a(prompts, completions=None, completions_text=None, **kw):
        return [1.0] * len(completions_text)

    def reward_b(prompts, completions=None, completions_text=None, **kw):
        return [0.25, 0.5]

    assert score([reward_a, reward_b], ["p", "p"], ["x", "y"]) == [1.25, 1.5]


def test_a_reward_function_is_called_batched_with_the_texts():
    seen = {}

    def reward_probe(prompts, completions=None, completions_text=None, **kw):
        seen["prompts"], seen["texts"] = prompts, completions_text
        return [0.0] * len(prompts)

    score([reward_probe], ["a", "b"], ["x", "y"])
    assert seen["prompts"] == ["a", "b"]
    assert seen["texts"] == ["x", "y"]


def test_a_reward_that_returns_the_wrong_shape_is_reported_not_broadcast():
    """A scalar silently broadcast would train every completion on the same advantage — a run
    that looks healthy and learns nothing in particular."""
    def reward_scalar(prompts, completions=None, completions_text=None, **kw):
        return 1.0

    with pytest.raises(SystemExit, match="one score per completion"):
        score([reward_scalar], ["p", "p"], ["x", "y"])


def test_run_dispatches_to_mlx_on_apple_silicon(monkeypatch):
    """`grid train run` had no --backend at all, which is what left this machine able to do the
    imitation stage and not the feedback stage."""
    from cli.train import cmd_train_run

    called = {}
    monkeypatch.setattr("train.config.load_config", lambda path: "CFG")
    monkeypatch.setattr("train.sft.pick_backend", lambda choice: "mlx")
    monkeypatch.setitem(
        __import__("sys").modules, "train.mlx.grpo",
        types.SimpleNamespace(run_grpo=lambda cfg, steps=None: called.setdefault("mlx", cfg)))

    cmd_train_run(argparse.Namespace(config=None, backend="mlx", steps=7))
    assert called["mlx"] == "CFG"


def test_run_dispatches_to_torch_elsewhere(monkeypatch):
    from cli.train import cmd_train_run

    called = {}
    monkeypatch.setattr("train.config.load_config", lambda path: "CFG")
    monkeypatch.setattr("train.sft.pick_backend", lambda choice: "torch")
    monkeypatch.setattr("train.run.run_training", lambda cfg: called.setdefault("torch", cfg))

    cmd_train_run(argparse.Namespace(config=None, backend="torch", steps=None))
    assert called["torch"] == "CFG"


def test_the_mlx_modules_import_without_mlx_installed():
    """CI has no mlx. Importing must not require it — only running does."""
    import train.mlx.core as core
    import train.mlx.grpo as grpo

    assert callable(core.Policy)
    assert callable(grpo.run_grpo)
