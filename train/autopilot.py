"""Autopilot: the loop that improves a model without anyone opening a training tool.

`grid train nightly` trains from a dataset someone prepared. Autopilot removes that someone: it
builds tonight's dataset from the work the grid actually did today, trains on it, proves it on
held-out work, and serves it only if it won. Put it in cron and a business's models get better
while the office is empty.

    grid train autopilot --config grid-train.toml     # once, now
    0 23 * * *  cd ~/support-model && grid train autopilot

What keeps it honest, and why each guard exists:

* **It refuses to train on unjudged output.** Rows only become examples when a human edited or
  accepted the answer, or when a stronger model produced it (`train/capture.py`). A model imitating
  its own guesses drifts; there is no version of "fully automatic" worth that.
* **It refuses to start on too little.** A run on forty examples wastes a night and teaches nothing.
* **It refuses to serve a model that didn't win** — the same eval gate as every other path.
* **It leaves the machine alone if someone is using it.** Borrowed capacity, negotiated.
* **It never grows without bound.** Old captures are pruned on the retention window each cycle.

Every cycle appends one line to `autopilot.jsonl`, so a month of unattended nights is a file you
can read rather than a mystery.
"""
from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path

from .config import TrainRunConfig

# Below this, a night's work cannot teach anything worth serving.
MIN_EXAMPLES = 120


@dataclasses.dataclass
class AutopilotResult:
    started: str
    ok: bool
    stage: str        # "waiting" | "skipped" | "trained" | "proved" | "deployed" | "failed"
    detail: str
    examples: int = 0
    delta: float | None = None
    adapter: str = ""

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def dataset_dir(cfg: TrainRunConfig) -> Path:
    """Where tonight's dataset is written — beside the workspace, replaced each cycle."""
    base = Path(cfg.trainer.output_dir).expanduser() if cfg.trainer.output_dir else Path.cwd()
    return base / "autopilot-data"


def build_tonights_dataset(cfg: TrainRunConfig, *, days: int = 30,
                           system_prompt: str = "") -> tuple[int, Path]:
    """Turn captured work into the same files every other training path reads."""
    from .capture import build_examples, prune, write_training_files

    prune()                                   # keep the store inside its retention window
    examples = build_examples(days=days)
    dest = dataset_dir(cfg)
    write_training_files(examples, dest, system_prompt=system_prompt)
    return len(examples), dest


def _with_dataset(cfg: TrainRunConfig, dest: Path) -> TrainRunConfig:
    """The same run config, pointed at tonight's dataset instead of a fixed file."""
    return dataclasses.replace(
        cfg,
        data=dataclasses.replace(cfg.data, prompts_jsonl=str(dest / "prompts.jsonl")),
    )


def run_cycle(cfg: TrainRunConfig, *, days: int = 30, min_examples: int = MIN_EXAMPLES,
              check_host: bool = True, deploy: bool = True, stage: str = "auto",
              system_prompt: str = "") -> AutopilotResult:
    """One unattended cycle over captured work. Never raises for an expected refusal."""
    started = time.strftime("%Y-%m-%dT%H:%M:%S")

    if check_host:
        from .nightly import host_is_free

        free, why = host_is_free()
        if not free:
            return _log(cfg, AutopilotResult(started, True, "skipped", why))

    examples, dest = build_tonights_dataset(cfg, days=days, system_prompt=system_prompt)
    if examples < min_examples:
        # Waiting is the correct outcome, not a failure: the store grows on its own as people work.
        return _log(cfg, AutopilotResult(
            started, True, "waiting",
            f"{examples} examples so far — waiting for {min_examples}. They accumulate as your "
            "team uses the grid and your app reports what was sent.",
            examples=examples))

    run_cfg = _with_dataset(cfg, dest)

    # Imitation first when the data is fresh corrections; the RL pass sharpens what exists.
    # "auto" means: teach it to write like this (SFT), because that is what captured corrections
    # are — worked examples. The RL stage is a deliberate choice, not a default, since it needs a
    # rollout engine and a grader.
    chosen = "sft" if stage == "auto" else stage
    try:
        if chosen == "sft":
            from .sft import run_sft

            # Into run/ so the dashboard and the browser's progress page find the curve.
            base = Path(cfg.trainer.output_dir).expanduser() if cfg.trainer.output_dir else None
            adapter = run_sft(run_cfg, run_dir=(base / "run") if base else None)
        else:
            from .run import run_training

            adapter = run_training(run_cfg)
    except SystemExit as exc:
        return _log(cfg, AutopilotResult(started, False, "failed",
                                         f"training did not start: {exc}", examples=examples))
    except Exception as exc:  # noqa: BLE001 — unattended: log the reason, never traceback
        return _log(cfg, AutopilotResult(started, False, "failed",
                                         f"training crashed: {type(exc).__name__}: {exc}",
                                         examples=examples))

    run_dir = adapter.parent
    name = cfg.deploy.adapter_name or run_dir.name

    # Prove it. An SFT run has no held-out slice of its own, so borrow the workspace's if present.
    from .evaluate import run_eval

    eval_source = run_dir if (run_dir / "eval_prompts.jsonl").is_file() else _find_holdout(cfg)
    if eval_source is None:
        return _log(cfg, AutopilotResult(
            started, False, "trained",
            "trained, but there is no held-out set to prove it against — so it will not be served.",
            examples=examples, adapter=str(adapter)))
    try:
        result = run_eval(cfg, eval_source, name)
    except SystemExit as exc:
        return _log(cfg, AutopilotResult(started, False, "trained",
                                         f"trained, but could not be checked: {exc}",
                                         examples=examples, adapter=str(adapter)))
    if not result["passed"]:
        return _log(cfg, AutopilotResult(started, False, "proved", result["verdict"],
                                         examples=examples, delta=result["delta"],
                                         adapter=str(adapter)))
    if not deploy:
        return _log(cfg, AutopilotResult(started, True, "proved",
                                         f"{result['verdict']} (not deployed: --no-deploy)",
                                         examples=examples, delta=result["delta"],
                                         adapter=str(adapter)))

    from .deploy import deploy_adapter

    nodes = cfg.deploy.nodes or (cfg.rollout.base_url,)
    outcomes = deploy_adapter(adapter, nodes, name)
    failed = [o for o in outcomes if not o["ok"]]
    if failed:
        return _log(cfg, AutopilotResult(
            started, False, "proved",
            "it won, but loading it failed on: "
            + "; ".join(f"{o['node']} ({o['detail']})" for o in failed),
            examples=examples, delta=result["delta"], adapter=str(adapter)))
    return _log(cfg, AutopilotResult(started, True, "deployed",
                                     f"{result['verdict']} — now serving as {name}",
                                     examples=examples, delta=result["delta"],
                                     adapter=str(adapter)))


def _find_holdout(cfg: TrainRunConfig) -> Path | None:
    """The most recent run directory that has a held-out slice we can reuse."""
    base = Path(cfg.trainer.output_dir).expanduser() if cfg.trainer.output_dir else None
    if base is None or not base.is_dir():
        return None
    candidates = sorted(
        (p.parent for p in base.glob("*/eval_prompts.jsonl")),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    return candidates[0] if candidates else None


def _history_file(cfg: TrainRunConfig) -> Path:
    from .run import artifacts_root

    base = Path(cfg.trainer.output_dir).expanduser() if cfg.trainer.output_dir else artifacts_root()
    base.mkdir(parents=True, exist_ok=True)
    return base / "autopilot.jsonl"


def _log(cfg: TrainRunConfig, result: AutopilotResult) -> AutopilotResult:
    with _history_file(cfg).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(result.as_dict()) + "\n")
    return result


def history(cfg: TrainRunConfig, limit: int = 60) -> list[dict]:
    path = _history_file(cfg)
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-limit:]
