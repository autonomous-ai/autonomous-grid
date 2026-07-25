"""`grid train ui`: a zero-config local dashboard for training runs.

Reads what the trainers already write — `~/.grid/artifacts/train/<run>/log.jsonl` +
`summary.json` — and renders one page: every run, its reward/eval curve, and a table view.
Server-rendered HTML with inline SVG; FastAPI + uvicorn are already core CLI deps, so the
dashboard adds no dependency and works offline. Read-only by design: the page can't start,
stop, or edit anything, which is what makes it safe to leave open on a shared screen.

Chart conventions follow the org's dataviz method: two series max (train reward, held-out
eval) on one fixed [0,1] axis, categorical slots 1/2 (validated light+dark), 2px lines,
8px markers, recessive grid, legend + end labels, hover tooltip, and a <details> table view.
"""
from __future__ import annotations

import html
import json
from pathlib import Path

from shared import paths


def runs_root() -> Path:
    return paths.grid_home() / "artifacts" / "train"


def load_runs(limit: int = 25) -> list[dict]:
    root = runs_root()
    if not root.is_dir():
        return []
    runs = []
    for run_dir in sorted(root.iterdir(), reverse=True)[:limit]:
        log_file = run_dir / "log.jsonl"
        if not log_file.is_file():
            continue
        points = []
        for line in log_file.read_text(encoding="utf-8").splitlines():
            try:
                points.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        summary = {}
        summary_file = run_dir / "summary.json"
        if summary_file.is_file():
            try:
                summary = json.loads(summary_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                summary = {}
        runs.append({"name": run_dir.name, "summary": summary, "points": points})
    return runs


def build_app():
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse

    app = FastAPI(title="grid train", docs_url=None, redoc_url=None)

    @app.get("/api/runs")
    def api_runs() -> JSONResponse:
        return JSONResponse(load_runs())

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return render_page(load_runs())

    return app


# --- rendering -----------------------------------------------------------------------------

_W, _H, _PAD_L, _PAD_R, _PAD_Y = 640, 170, 34, 96, 14


def _polyline(xs: list[float], ys: list[float]) -> str:
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))


def _chart_svg(points: list[dict]) -> str:
    steps = [p["step"] for p in points if "reward_mean" in p or "eval" in p]
    if not steps:
        return "<p class='muted'>no data points yet</p>"
    max_step = max(steps) or 1

    def sx(step: float) -> float:
        return _PAD_L + (step / max_step) * (_W - _PAD_L - _PAD_R)

    def sy(value: float) -> float:
        return _H - _PAD_Y - value * (_H - 2 * _PAD_Y)

    reward = [(p["step"], p["reward_mean"]) for p in points if "reward_mean" in p]
    evals = [(p.get("step", 0), p["eval"]) for p in points if "eval" in p]

    grid_lines = "".join(
        f'<line x1="{_PAD_L}" y1="{sy(v):.1f}" x2="{_W - _PAD_R}" y2="{sy(v):.1f}" class="grid"/>'
        f'<text x="{_PAD_L - 6}" y="{sy(v) + 4:.1f}" class="axis" text-anchor="end">{v:.1f}</text>'
        for v in (0.0, 0.5, 1.0)
    )
    reward_line = (
        f'<polyline class="s1" points="{_polyline([sx(s) for s, _ in reward], [sy(v) for _, v in reward])}"/>'
        if reward
        else ""
    )
    eval_line = (
        f'<polyline class="s2" points="{_polyline([sx(s) for s, _ in evals], [sy(v) for _, v in evals])}"/>'
        + "".join(f'<circle class="s2m" cx="{sx(s):.1f}" cy="{sy(v):.1f}" r="4"/>' for s, v in evals)
        if evals
        else ""
    )
    end_labels = ""
    if reward:
        end_labels += (
            f'<circle class="s1m" cx="{_W - _PAD_R + 8}" cy="{sy(reward[-1][1]):.1f}" r="4"/>'
            f'<text x="{_W - _PAD_R + 16}" y="{sy(reward[-1][1]) + 4:.1f}" class="label">train</text>'
        )
    if evals:
        end_labels += (
            f'<circle class="s2m" cx="{_W - _PAD_R + 8}" cy="{sy(evals[-1][1]) + (10 if reward and abs(sy(reward[-1][1]) - sy(evals[-1][1])) < 12 else 0):.1f}" r="4"/>'
            f'<text x="{_W - _PAD_R + 16}" y="{sy(evals[-1][1]) + 4 + (10 if reward and abs(sy(reward[-1][1]) - sy(evals[-1][1])) < 12 else 0):.1f}" class="label">eval</text>'
        )
    payload = html.escape(json.dumps({"reward": reward, "eval": evals, "maxStep": max_step}), quote=True)
    return (
        f'<svg class="viz" viewBox="0 0 {_W} {_H}" role="img" data-points="{payload}" '
        f'aria-label="reward and eval curves">'
        f"{grid_lines}{reward_line}{eval_line}{end_labels}"
        f'<line class="cross" x1="0" y1="{_PAD_Y}" x2="0" y2="{_H - _PAD_Y}" visibility="hidden"/>'
        "</svg>"
    )


def _table(points: list[dict]) -> str:
    rows = "".join(
        f"<tr><td>{p.get('step', '')}</td><td>{p.get('reward_mean', '')}</td>"
        f"<td>{p.get('eval', '')}</td><td>{p.get('loss', p.get('skipped', ''))}</td></tr>"
        for p in points
    )
    return (
        "<details><summary>table view</summary><table>"
        "<tr><th>step</th><th>train reward</th><th>eval</th><th>loss</th></tr>"
        f"{rows}</table></details>"
    )


def _run_section(run: dict) -> str:
    summary = run["summary"]
    facts = []
    if summary.get("backend"):
        facts.append(html.escape(str(summary["backend"])))
    if summary.get("model"):
        facts.append(html.escape(str(summary["model"])))
    if "baseline_eval" in summary and "final_eval" in summary:
        facts.append(f"eval {summary['baseline_eval']} → <strong>{summary['final_eval']}</strong>")
    if summary.get("minutes") is not None:
        facts.append(f"{summary['minutes']} min")
    status = "" if summary else ' <span class="live">running</span>'
    return (
        f'<section class="run"><h2>{html.escape(run["name"])}{status}</h2>'
        f'<p class="muted">{" · ".join(facts)}</p>'
        f"{_chart_svg(run['points'])}{_table(run['points'])}</section>"
    )


def render_page(runs: list[dict]) -> str:
    body = "".join(_run_section(r) for r in runs) or (
        "<p class='muted'>No training runs yet. Start one with <code>grid train run</code> "
        "or <code>python -m train.torch_grpo_hello</code>.</p>"
    )
    legend = (
        '<p class="legend"><span class="chip s1bg"></span> train reward (sampled group mean) '
        '&nbsp; <span class="chip s2bg"></span> eval (held-out, greedy)</p>'
        if runs
        else ""
    )
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>grid train</title>
<style>
:root {{ color-scheme: light dark;
  --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --grid-ln: #e4e3df;
  --s1: #2a78d6; --s2: #eb6834; }}
@media (prefers-color-scheme: dark) {{ :root {{
  --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --grid-ln: #34332f;
  --s1: #3987e5; --s2: #d95926; }} }}
body {{ background: var(--surface); color: var(--ink); margin: 2rem auto; max-width: 720px;
  font: 15px/1.5 -apple-system, "Segoe UI", sans-serif; padding: 0 1rem; }}
h1 {{ font-size: 1.2rem; }} h2 {{ font-size: 1rem; margin: 1.6rem 0 .2rem; }}
.muted {{ color: var(--ink-2); margin: .2rem 0 .6rem; }}
.legend {{ color: var(--ink-2); font-size: .85rem; }}
.chip {{ display: inline-block; width: 10px; height: 10px; border-radius: 3px; vertical-align: -1px; }}
.s1bg {{ background: var(--s1); }} .s2bg {{ background: var(--s2); }}
svg.viz {{ width: 100%; height: auto; }}
.viz .grid {{ stroke: var(--grid-ln); stroke-width: 1; }}
.viz .axis, .viz .label {{ font: 11px -apple-system, sans-serif; fill: var(--ink-2); }}
.viz .s1 {{ fill: none; stroke: var(--s1); stroke-width: 2; }}
.viz .s2 {{ fill: none; stroke: var(--s2); stroke-width: 2; }}
.viz .s1m {{ fill: var(--s1); }} .viz .s2m {{ fill: var(--s2); stroke: var(--surface); stroke-width: 2; }}
.viz .cross {{ stroke: var(--ink-2); stroke-dasharray: 3 3; }}
.live {{ color: var(--s2); font-size: .8rem; }}
details {{ margin: .4rem 0 0; color: var(--ink-2); }} summary {{ cursor: pointer; font-size: .85rem; }}
table {{ border-collapse: collapse; font-size: .85rem; margin-top: .4rem; }}
td, th {{ border-bottom: 1px solid var(--grid-ln); padding: .15rem .8rem .15rem 0; text-align: left; }}
#tip {{ position: fixed; pointer-events: none; background: var(--ink); color: var(--surface);
  padding: .25rem .5rem; border-radius: 4px; font-size: .8rem; display: none; }}
</style></head><body>
<h1>grid train — runs</h1>{legend}{body}
<div id="tip"></div>
<script>
const tip = document.getElementById('tip');
for (const svg of document.querySelectorAll('svg.viz')) {{
  const d = JSON.parse(svg.dataset.points);
  const cross = svg.querySelector('.cross');
  svg.addEventListener('mousemove', (e) => {{
    const r = svg.getBoundingClientRect();
    const step = Math.round(((e.clientX - r.left) / r.width * {_W} - {_PAD_L}) /
                            ({_W} - {_PAD_L} - {_PAD_R}) * d.maxStep);
    const near = (arr) => arr.length ? arr.reduce((a, b) =>
      Math.abs(a[0] - step) < Math.abs(b[0] - step) ? a : b) : null;
    const rw = near(d.reward), ev = near(d.eval);
    if (!rw && !ev) return;
    const x = {_PAD_L} + ((rw || ev)[0] / d.maxStep) * ({_W} - {_PAD_L} - {_PAD_R});
    cross.setAttribute('x1', x); cross.setAttribute('x2', x); cross.setAttribute('visibility', 'visible');
    tip.style.display = 'block';
    tip.style.left = (e.clientX + 12) + 'px'; tip.style.top = (e.clientY - 10) + 'px';
    tip.textContent = 'step ' + (rw || ev)[0] +
      (rw ? '  train ' + rw[1].toFixed(3) : '') + (ev ? '  eval ' + ev[1].toFixed(3) : '');
  }});
  svg.addEventListener('mouseleave', () => {{ tip.style.display = 'none'; cross.setAttribute('visibility', 'hidden'); }});
}}
</script></body></html>"""
