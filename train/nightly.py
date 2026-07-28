"""`grid train nightly`: one unattended cycle — train, prove it, ship it or bin it.

This is the loop that turns a run you launch by hand into something that compounds. It is
deliberately *one cycle per invocation* rather than a daemon: a cron entry or a launchd job is
already a better scheduler than anything shipped here, and a process that exits is one you can
reason about at 3am.

    grid train nightly --config grid-train.toml            # once, now
    0 23 * * *  cd ~/support-model && grid train nightly    # every night at 11

The order is not negotiable, because each step protects the next:

  1. **Idle check** — refuse to start if the machine is on battery or someone is using it. Borrowed
     capacity is negotiated, never assumed.
  2. **Train** — an ordinary run, adapter written to the run directory.
  3. **Prove** — score the candidate against the model currently serving, on held-out work.
  4. **Ship or bin** — deploy only on a pass. A failed night leaves production untouched, logs
     why, and exits non-zero so cron mail tells you.

Every cycle appends one line to `nightly.jsonl` in the run directory, so a week of nights is a
readable history rather than a folder of guesses.
"""
from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path

from .config import TrainRunConfig


@dataclasses.dataclass
class CycleResult:
    started: str
    ok: bool
    # skipped — machine in use · trained — never compared · refused — compared and lost
    # proved  — compared and won · deployed — won and serving · failed — did not finish
    stage: str
    detail: str
    delta: float | None = None
    adapter: str = ""

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def host_is_free(*, require_ac: bool = True, idle_minutes: float = 5.0) -> tuple[bool, str]:
    """Is it fair to take this machine? (The host-priority rule, in code rather than prose.)

    Two signals, both cheap and both Mac/Linux-safe: mains power, and how long since someone
    touched the keyboard. Unknown counts as free — a machine that can't answer shouldn't block
    the entire feature — but AC is checked first and a laptop on battery is always skipped.
    """
    from .hostsignals import on_battery, user_idle_seconds

    if require_ac:
        battery = on_battery()
        if battery is True:
            return False, "on battery — training would drain it, so this night is skipped"
    idle = user_idle_seconds()
    if idle is not None and idle < idle_minutes * 60:
        return False, (f"someone is using this machine (active {int(idle)}s ago) — "
                       "training waits for an idle machine")
    return True, "machine is idle and on power"


def run_cycle(
    cfg: TrainRunConfig,
    *,
    candidate_name: str | None = None,
    check_host: bool = True,
    deploy: bool = True,
) -> CycleResult:
    """One full night. Returns what happened; never raises for an expected refusal."""
    started = time.strftime("%Y-%m-%dT%H:%M:%S")

    if check_host:
        free, why = host_is_free()
        if not free:
            return _log(cfg, CycleResult(started, True, "skipped", why))

    # 1. Train.
    from .run import run_training

    try:
        adapter_dir = run_training(cfg)
    except SystemExit as exc:            # config/dependency problems arrive as SystemExit
        return _log(cfg, CycleResult(started, False, "failed", f"training did not start: {exc}"))
    except Exception as exc:  # noqa: BLE001 — unattended at 3am: log the reason, never traceback
        return _log(cfg, CycleResult(started, False, "failed",
                                     f"training crashed: {type(exc).__name__}: {exc}"))
    run_dir = adapter_dir.parent

    # 2. Prove it on held-out work — the CANDIDATE, loaded under a staging name. Asking the node
    # for the serving name would score whatever it already holds (see evaluate.prove_candidate).
    from .evaluate import CandidateNotLoaded, prove_candidate

    name = candidate_name or cfg.deploy.adapter_name or adapter_dir.parent.name
    try:
        result = prove_candidate(cfg, run_dir, adapter_dir, name)
    except (SystemExit, CandidateNotLoaded) as exc:
        return _log(cfg, CycleResult(started, False, "trained",
                                     f"trained, but could not be checked: {exc}",
                                     adapter=str(adapter_dir)))
    if not result["passed"]:
        # A night that produced nothing better is a *successful* night for the customer: the model
        # they rely on is untouched. Non-zero exit is for the operator, not a failure of the loop.
        return _log(cfg, CycleResult(started, False, "refused", result["verdict"],
                                     delta=result["delta"], adapter=str(adapter_dir)))

    if not deploy:
        return _log(cfg, CycleResult(started, True, "proved",
                                     f"{result['verdict']} (not deployed: --no-deploy)",
                                     delta=result["delta"], adapter=str(adapter_dir)))

    # 3. Ship it.
    from .deploy import deploy_adapter

    nodes = cfg.deploy.nodes or (cfg.rollout.base_url,)
    outcomes = deploy_adapter(adapter_dir, nodes, name)
    failed = [o for o in outcomes if not o["ok"]]
    if failed:
        return _log(cfg, CycleResult(
            started, False, "proved",
            "it won, but loading it failed on: "
            + "; ".join(f"{o['node']} ({o['detail']})" for o in failed),
            delta=result["delta"], adapter=str(adapter_dir)))
    return _log(cfg, CycleResult(started, True, "deployed",
                                 f"{result['verdict']} — now serving as {name}",
                                 delta=result["delta"], adapter=str(adapter_dir)))


def _log(cfg: TrainRunConfig, result: CycleResult) -> CycleResult:
    """Append the night to a history file next to the runs."""
    from .run import artifacts_root

    root = Path(cfg.trainer.output_dir).expanduser() if cfg.trainer.output_dir else artifacts_root()
    root.mkdir(parents=True, exist_ok=True)
    with (root / "nightly.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(result.as_dict()) + "\n")
    return result


def history(cfg: TrainRunConfig, limit: int = 30) -> list[dict]:
    from .run import artifacts_root

    root = Path(cfg.trainer.output_dir).expanduser() if cfg.trainer.output_dir else artifacts_root()
    path = root / "nightly.jsonl"
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-limit:]
