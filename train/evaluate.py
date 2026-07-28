"""`grid train eval`: score a model on held-out work, and gate deployment on the result.

The question a training run must answer is not "did the loss go down" but **"is this better than
what we already serve, on work it has never seen?"** This module answers it the only honest way:
run both models over the run's held-out prompts, score with the same reward functions used in
training, and compare per-grader — then let `deploy --gate` refuse anything that didn't win.

Greedy decoding (temperature 0) on both sides, so the comparison is deterministic and a rerun
gives the same number. Scores come back per reward function, because an overall average can hide
a model that got tidier and less correct.
"""
from __future__ import annotations

import dataclasses
import html
import json
import statistics
from pathlib import Path

from .config import ConfigError, TrainRunConfig
from .rollout import GridRolloutClient


@dataclasses.dataclass(frozen=True)
class Scored:
    """One model's performance over the held-out set."""

    model: str
    n: int
    per_grader: dict[str, float]
    overall: float
    samples: list[dict]  # [{prompt, completion, scores}] — the qualitative half of the card


def load_eval_prompts(run_dir: Path) -> list[str]:
    path = run_dir / "eval_prompts.jsonl"
    if not path.is_file():
        raise ConfigError(
            f"no held-out set at {path} — it is written when a run starts "
            "(older runs predate the split; retrain or point --run at a newer one)"
        )
    prompts = [
        json.loads(line)["prompt"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not prompts:
        raise ConfigError(f"{path} is empty")
    return prompts


def score_model(
    cfg: TrainRunConfig,
    model: str,
    prompts: list[str],
    reward_funcs: list,
    *,
    keep_samples: int = 6,
    transport=None,
) -> Scored:
    """Greedy-decode every held-out prompt and score it with each reward function."""
    client = GridRolloutClient(cfg.rollout, model, transport=transport)
    completions: list[str] = []
    try:
        for index, prompt in enumerate(prompts, 1):
            rollout = client.generate_group(prompt, 1, temperature=0.0)[0]
            completions.append(rollout.text)
            # Printed, not logged: scoring can take minutes and the page reads this to say
            # "scored 12 of 30" instead of showing a blank screen.
            print(f"[grid train] scored {index} of {len(prompts)} with {model}", flush=True)
    finally:
        client.close()

    per_grader: dict[str, float] = {}
    columns: dict[str, list[float]] = {}
    for func in reward_funcs:
        name = getattr(func, "__name__", "reward").removeprefix("reward_")
        scores = [
            float(x)
            for x in func(prompts, completions=completions, completions_text=completions)
        ]
        columns[name] = scores
        per_grader[name] = round(statistics.fmean(scores), 4)

    samples = [
        {
            "prompt": prompt,
            "completion": completion,
            "scores": {name: round(columns[name][i], 3) for name in columns},
        }
        for i, (prompt, completion) in enumerate(zip(prompts, completions))
    ][:keep_samples]

    return Scored(
        model=model,
        n=len(prompts),
        per_grader=per_grader,
        overall=round(statistics.fmean(per_grader.values()), 4) if per_grader else 0.0,
        samples=samples,
    )


def compare(before: Scored, after: Scored, *, min_gain: float = 0.01) -> dict:
    """The verdict. `min_gain` keeps noise from reading as improvement."""
    delta = round(after.overall - before.overall, 4)
    regressions = sorted(
        name
        for name, value in after.per_grader.items()
        if value < before.per_grader.get(name, 0) - 0.02
    )
    passed = delta >= min_gain and not regressions
    if passed:
        verdict = f"better by {delta:+.3f} — safe to serve"
    elif regressions:
        verdict = f"blocked: {', '.join(regressions)} got worse (overall {delta:+.3f})"
    else:
        verdict = f"no meaningful gain ({delta:+.3f}) — keep the model you have"
    return {
        "passed": passed,
        "delta": delta,
        "regressions": regressions,
        "verdict": verdict,
        "before": dataclasses.asdict(before),
        "after": dataclasses.asdict(after),
    }


def write_card(result: dict, run_dir: Path) -> tuple[Path, Path]:
    """Persist the comparison as JSON (for machines) and HTML (for people)."""
    json_path = run_dir / "eval-card.json"
    json_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    html_path = run_dir / "eval-card.html"
    html_path.write_text(card_html(result, run_dir.name), encoding="utf-8")
    return json_path, html_path


def card_html(result: dict, run_name: str) -> str:
    """A single self-contained page: the verdict, the per-grader table, and real answers."""
    before, after = result["before"], result["after"]
    graders = sorted(set(before["per_grader"]) | set(after["per_grader"]))
    rows = "".join(
        f"<tr><td>{html.escape(name)}</td>"
        f"<td class='n'>{before['per_grader'].get(name, 0):.3f}</td>"
        f"<td class='n'>{after['per_grader'].get(name, 0):.3f}</td>"
        f"<td class='n {'up' if after['per_grader'].get(name, 0) >= before['per_grader'].get(name, 0) else 'down'}'>"
        f"{after['per_grader'].get(name, 0) - before['per_grader'].get(name, 0):+.3f}</td></tr>"
        for name in graders
    )
    pairs = "".join(
        "<div class='pair'>"
        f"<p class='q'>{html.escape(b['prompt'][:400])}</p>"
        f"<div class='cols'><div><span class='tag'>now serving</span><p>{html.escape(b['completion'][:600])}</p></div>"
        f"<div><span class='tag new'>trained</span><p>{html.escape(a['completion'][:600])}</p></div></div></div>"
        for b, a in zip(before["samples"], after["samples"])
    )
    state = "pass" if result["passed"] else "fail"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>eval card · {html.escape(run_name)}</title>
<style>
:root{{color-scheme:light dark;--bg:#f6f7f9;--card:#fff;--ink:#14181f;--ink2:#5c6672;--rule:#d2d8e0;
--ok:#1baf7a;--no:#eb6834;--blue:#2a78d6}}
@media(prefers-color-scheme:dark){{:root{{--bg:#101419;--card:#171c23;--ink:#e9edf3;--ink2:#98a2af;
--rule:#2a323c;--ok:#199e70;--no:#d95926;--blue:#3987e5}}}}
body{{margin:0;padding:2.5rem 1.2rem;background:var(--bg);color:var(--ink);
font:16px/1.6 -apple-system,"Segoe UI",sans-serif}}
main{{max-width:52rem;margin:0 auto}}
h1{{font-size:1.3rem;margin:0 0 .2rem}}.sub{{color:var(--ink2);font-size:.9rem;margin:0 0 1.6rem}}
.verdict{{border:1px solid var(--rule);border-left:4px solid var(--{state == 'pass' and 'ok' or 'no'});
background:var(--card);padding:1rem 1.2rem;margin-bottom:1.6rem}}
.verdict b{{color:var(--{state == 'pass' and 'ok' or 'no'});text-transform:uppercase;font-size:.75rem;letter-spacing:.1em}}
.verdict p{{margin:.3rem 0 0;font-size:1.1rem}}
table{{border-collapse:collapse;width:100%;background:var(--card);border:1px solid var(--rule)}}
th,td{{text-align:left;padding:.55rem .9rem;border-bottom:1px solid var(--rule);font-size:.92rem}}
th{{font-size:.7rem;letter-spacing:.09em;text-transform:uppercase;color:var(--ink2)}}
td.n{{font-variant-numeric:tabular-nums;text-align:right;font-family:ui-monospace,Menlo,monospace}}
.up{{color:var(--ok)}}.down{{color:var(--no)}}
h2{{font-size:1rem;margin:2.2rem 0 .6rem}}
.pair{{background:var(--card);border:1px solid var(--rule);padding:1rem 1.1rem;margin:.8rem 0}}
.q{{margin:0 0 .7rem;color:var(--ink2);font-size:.9rem;white-space:pre-wrap}}
.cols{{display:grid;grid-template-columns:1fr 1fr;gap:1.1rem}}
@media(max-width:640px){{.cols{{grid-template-columns:1fr}}}}
.cols p{{margin:.3rem 0 0;white-space:pre-wrap;font-size:.93rem}}
.tag{{font-size:.68rem;letter-spacing:.09em;text-transform:uppercase;color:var(--ink2);
border:1px solid var(--rule);padding:.1em .45em}}
.tag.new{{color:var(--blue);border-color:var(--blue)}}
</style></head><body><main>
<h1>Is the trained model better?</h1>
<p class="sub">{html.escape(run_name)} · {after['n']} pieces of held-back work it never trained on · greedy decoding</p>
<div class="verdict"><b>{'ready to serve' if result['passed'] else 'not ready'}</b>
<p>{html.escape(result['verdict'])}</p></div>
<table><tr><th>what we check</th><th>now serving</th><th>trained</th><th>change</th></tr>{rows}
<tr><th>overall</th><td class="n">{before['overall']:.3f}</td><td class="n">{after['overall']:.3f}</td>
<td class="n {'up' if result['delta'] >= 0 else 'down'}">{result['delta']:+.3f}</td></tr></table>
<h2>The same work, answered by both</h2>{pairs}
</main></body></html>"""


class CandidateNotLoaded(RuntimeError):
    """The candidate could not be put in front of the grader, so nothing was proved."""


def prove_candidate(
    cfg: TrainRunConfig,
    run_dir: Path,
    adapter_dir: Path,
    serving_name: str,
    *,
    nodes=None,
    transport=None,
) -> dict:
    """Score the model we are about to ship — not the one already serving under its name.

    This exists because of a real defect. An unattended cycle trained a fresh adapter, then called
    the gate with the *serving* name; the node resolves a name to whatever weights it already
    holds, so on every night after the first the gate scored **last night's** adapter and then
    deployed tonight's, never scored. The verdict was true of a model that was not the one shipped.

    The fix for that was to load the candidate under a staging name and score *that* — which broke
    the product's primary engine, because **Grid's own MLX server holds exactly one set of
    weights**. `reload_adapter` mutates the model in place and the name is only a label, so the
    staging load replaced the incumbent, the probe for the incumbent's name then failed, the
    comparison fell back to the base model id — which now answered with the candidate's weights —
    and every night scored a model against itself. Greedy decoding made that exactly +0.000, so an
    all-Mac office would have been told its model lost, every night, forever.

    Hence the order below, which is the only one that is correct on a one-slot engine and on a
    many-slot one alike:

      1. **Score the incumbent first**, before anything is loaded. Whatever the node is serving now
         is what the customer has, and it can only be measured while it is still there.
      2. Then load the candidate and score it.
      3. If it lost, put the incumbent's weights back — a one-slot engine is now holding a model
         nobody approved, under a name nobody asked for, and "nothing changed tonight" has to be
         true rather than merely printed.
    """
    from .deploy import deploy_adapter, last_deployed
    from .rewards import load_reward_funcs
    from .rollout import probe_endpoint

    nodes = tuple(nodes or cfg.deploy.nodes or (cfg.rollout.base_url,))
    staged = f"{serving_name}-candidate"
    prompts = load_eval_prompts(run_dir)
    reward_funcs = load_reward_funcs(cfg.rewards, cfg.data)

    incumbent = cfg.rollout_model
    if serving_name != incumbent:
        probe = probe_endpoint(cfg.rollout, serving_name, transport=transport)
        if probe.get("ok"):
            incumbent = serving_name
    before = score_model(cfg, incumbent, prompts, reward_funcs, transport=transport)

    # From here the node may be holding the candidate instead of the customer's model, so every
    # exit from this block goes through the restore. A check is supposed to be an observation.
    # record=False: this load exists only to measure. Recording it would keep an 80 MB copy of
    # every candidate ever checked, under a name nobody serves.
    outcomes = deploy_adapter(adapter_dir, nodes, staged, transport=transport, record=False)
    loaded = [o for o in outcomes if o.get("ok")]
    if not loaded:
        detail = "; ".join(f"{o['node']} ({o.get('detail', '')})" for o in outcomes)
        raise CandidateNotLoaded(f"no node would load it for checking: {detail}")

    try:
        after = score_model(cfg, staged, prompts, reward_funcs, transport=transport)
        result = compare(before, after)
    finally:
        # Always, and whatever happened. Restoring only on a refusal left the node answering to
        # "<name>-candidate" after a PASS — so the name customers point at 404'd until someone
        # pressed a button that might be minutes or hours away — and left it displaced entirely if
        # scoring raised. The winner is deployed by the caller, once, after this returns.
        put_back = _put_back(cfg, nodes, serving_name, last_deployed(serving_name),
                             transport=transport)

    result["run"] = run_dir.name
    result["staged_as"] = staged
    result["incumbent"] = incumbent
    result["restored"] = put_back
    # Which weights this verdict is about. Without it, a card written for last week's adapter
    # authorises whatever is in run/adapter today — press "check", retrain, press "use".
    result["adapter"] = str(Path(adapter_dir).expanduser().resolve())
    result["adapter_fingerprint"] = _fingerprint(adapter_dir)
    if not put_back:
        result["verdict"] += (
            f" — note: the checking copy is still loaded on {', '.join(n['node'] for n in loaded)}"
            f", so restart the engine or deploy {serving_name} before relying on it")

    write_card(result, run_dir)
    return result


def _fingerprint(adapter_dir) -> str:
    """Which weights these are — a hash of the bytes, not of the file's size and timestamp.

    Size-and-mtime is the cheap version and it collides exactly where it matters: two runs a
    second apart producing files of the same length, which is what an adapter of a fixed rank
    does on every run. Reading the file costs a fraction of a second, once per check.
    """
    import hashlib

    digest = hashlib.sha256()
    found = False
    for path in sorted(Path(adapter_dir).glob("*.safetensors")):
        try:
            with path.open("rb") as handle:
                digest.update(path.name.encode())
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(chunk)
            found = True
        except OSError:
            continue
    return digest.hexdigest()[:16] if found else ""


def _put_back(cfg, nodes, serving_name: str, adapter, *, transport=None) -> bool:
    """Restore the model that was serving before the check. True if it is back.

    On an engine with one weight slot the check itself displaces the customer's model. Leaving it
    displaced after a refusal would mean the interface's "nothing changed" was false in the one
    case it matters most — the night the candidate was worse.
    """
    if not adapter or not Path(adapter).is_dir():
        return False
    if Path(adapter).resolve() == Path(cfg.trainer.output_dir or ".").resolve() / "run" / "adapter":
        # Belt and braces: the ledger keeps copies precisely so this cannot happen, but restoring
        # from the live training directory would put back the model that just lost.
        return False
    from .deploy import DeployError, deploy_adapter

    try:
        outcomes = deploy_adapter(adapter, nodes, serving_name, transport=transport)
    except (DeployError, SystemExit):
        return False
    return any(o.get("ok") for o in outcomes)


def run_eval(
    cfg: TrainRunConfig,
    run_dir: Path,
    candidate_model: str,
    *,
    base_model: str | None = None,
    transport=None,
) -> dict:
    """Score the incumbent and the candidate over the run's held-out set; write the card."""
    from .rewards import load_reward_funcs

    prompts = load_eval_prompts(run_dir)
    reward_funcs = load_reward_funcs(cfg.rewards, cfg.data)
    before = score_model(cfg, base_model or cfg.rollout_model, prompts, reward_funcs, transport=transport)
    after = score_model(cfg, candidate_model, prompts, reward_funcs, transport=transport)
    result = compare(before, after)
    result["run"] = run_dir.name
    write_card(result, run_dir)
    return result
