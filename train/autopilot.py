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
    # waiting  — not enough examples yet          trained  — trained, but never compared
    # skipped  — the machine was in use            refused  — compared, and it lost
    # proved   — compared, and it won              deployed — it won and is now serving
    # failed   — it did not finish
    stage: str
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
    # Only the traffic this model answered. The store is grid-wide; a routing model trained on a
    # support model's tickets spends the night learning the wrong job.
    served_as = [n for n in (cfg.deploy.adapter_name, cfg.model_name) if n]
    examples = build_examples(days=days, models=served_as or None)
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

    examples, dest = build_tonights_dataset(
        cfg, days=days, system_prompt=system_prompt or _workspace_system_prompt(cfg))
    if examples < min_examples:
        # Waiting is the correct outcome, not a failure: the store grows on its own as people work.
        # But "0 examples" has two very different causes, and only one of them fixes itself.
        detail = (f"{examples} examples so far — waiting for {min_examples}. They accumulate as "
                  "your team uses the grid and your app reports what was sent.")
        if examples == 0:
            detail += _nothing_for_this_model(cfg, days)
        return _log(cfg, AutopilotResult(started, True, "waiting", detail, examples=examples))

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
    from .evaluate import CandidateNotLoaded, prove_candidate

    eval_source = _pick_eval_source(cfg, run_dir, dest)
    if eval_source is None:
        return _log(cfg, AutopilotResult(
            started, False, "trained",
            "trained, but there is no held-out set to prove it against — so it will not be served.",
            examples=examples, adapter=str(adapter)))
    try:
        # The candidate goes onto the node under a staging name first. Scoring `name` would score
        # last night's adapter and ship tonight's unmeasured.
        result = prove_candidate(cfg, eval_source, adapter, name)
    except (SystemExit, CandidateNotLoaded) as exc:
        return _log(cfg, AutopilotResult(started, False, "trained",
                                         f"trained, but could not be checked: {exc}",
                                         examples=examples, adapter=str(adapter)))
    if not result["passed"]:
        # "refused", not "proved". The night DID happen and the comparison DID run — what it
        # produced was worse, so nothing changed. Recording that as "proved" made the record read
        # as a win on the page that shows a customer their week.
        return _log(cfg, AutopilotResult(started, False, "refused", result["verdict"],
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


def _nothing_for_this_model(cfg: TrainRunConfig, days: int) -> str:
    """When the store has traffic but none of it is ours, say which names it does have.

    Silence here would be indistinguishable from "nobody used the grid today", and the fix — point
    your tools at the model's name — is not something anyone would guess.
    """
    from .capture import build_examples

    everything = build_examples(days=days)
    if not everything:
        return ""
    return (" The store does have work in it, but none of it was answered by this model. Check "
            f"that your tools ask for “{cfg.deploy.adapter_name or cfg.model_name}”.")


def _workspace_system_prompt(cfg: TrainRunConfig) -> str:
    """The instruction line this model was taught with, reused for tonight's captured work.

    Every other path that writes sft.jsonl puts a system prompt in front — the sorting pack's
    answers are only parseable because of it. Training tonight's rows without one teaches the model
    a second, contradictory shape for the same job, and the graders then score it as wrong.
    The workspace's own sft.jsonl already holds the right line, so take it from there.
    """
    rewards_dir = _rewards_dir(cfg)
    source = (rewards_dir / "sft.jsonl") if rewards_dir else None
    if not source or not source.is_file():
        return ""
    for line in source.read_text(encoding="utf-8").splitlines():
        try:
            messages = json.loads(line).get("messages") or []
        except json.JSONDecodeError:
            continue
        for message in messages:
            if message.get("role") == "system" and message.get("content"):
                return str(message["content"])
        break                       # first row decides; a mixed file is not a thing we write
    return ""


def _rewards_dir(cfg: TrainRunConfig) -> Path | None:
    """Where the graders live — and therefore where they look for their answer key."""
    return Path(cfg.rewards.python_file).expanduser().parent if cfg.rewards.python_file else None


def _pick_eval_source(cfg: TrainRunConfig, run_dir: Path, dataset: Path) -> Path | None:
    """Which held-out set tonight is judged on — and make sure the graders can score it.

    A grader with no answer key for a prompt returns the neutral 0.5. It does that for *both*
    models, so the gate can only ever say "no meaningful gain": the night trains correctly and can
    never ship. That is what happened with captured work, whose prompts appear in none of the
    workspace's reference files.

    Two shapes, two answers:

    * **Answer-key packs** (a `labels.jsonl` beside the graders — the sorting pack, lead triage):
      captured traffic carries no label and we must not invent one, so tonight is judged on the
      workspace's own held-out slice, which has labels.
    * **Reference packs** (`refs.jsonl` — replies, and anything written): the captured examples
      *are* references (the human's correction, or the stronger model's answer), so they are
      merged into the graders' lookup and tonight is judged on tonight's held-out work.
    """
    rewards_dir = _rewards_dir(cfg)
    captured = run_dir if (run_dir / "eval_prompts.jsonl").is_file() else None
    workspace_holdout = _find_holdout(cfg)

    if rewards_dir and (rewards_dir / "labels.jsonl").is_file():
        return workspace_holdout or captured
    if captured and rewards_dir:
        merge_references(rewards_dir, dataset)
    return captured or workspace_holdout


def merge_references(rewards_dir: Path, dataset: Path, *, keep: int = 20_000) -> int:
    """Teach the workspace's graders about tonight's examples. Returns how many it knows now.

    The generated graders read `refs.jsonl` from their own directory; tonight's prompts are in the
    dataset directory. Merging (newest wins, oldest dropped past `keep`) is what lets the gate say
    anything at all about captured work.
    """
    source = dataset / "refs.jsonl"
    if not source.is_file():
        return 0
    target = rewards_dir / "refs.jsonl"
    known: dict[str, str] = {}
    # The workspace's own file goes in LAST and wins. Those references are what a person wrote in
    # the export; a captured one may be the model's own answer that nobody objected to (weight
    # 0.6). Letting that overwrite a human's reply would quietly make the model its own answer key
    # — the same collapse the capture rules exist to prevent, arriving through the gate instead.
    for path in (source, target):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("prompt"):
                known[row["prompt"]] = row.get("reference", "")
    rows = list(known.items())[-keep:]
    target.write_text(
        "".join(json.dumps({"prompt": p, "reference": r}) + "\n" for p, r in rows),
        encoding="utf-8",
    )
    return len(rows)


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
