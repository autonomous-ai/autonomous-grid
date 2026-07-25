"""The HTML. Server-rendered, no build step, no framework — one shell and a page per step.

Written for someone who manages a support queue, not a training run: the words are "examples",
"what good looks like", "start using this". The only numbers on screen are ones that change a
decision. Every refusal says what to do instead.
"""
from __future__ import annotations

import html
import json

STEPS = ("data", "checks", "machines", "running", "done")
STEP_LABELS = {
    "data": "Your examples",
    "checks": "What good looks like",
    "machines": "Machines",
    "running": "Learning",
    "done": "Result",
}

_CSS = """
:root{color-scheme:light dark;
  --paper:#f6f7f9;--card:#fff;--wash:#eef2f7;--ink:#14181f;--ink2:#5c6672;--ink3:#8792a0;
  --rule:#d2d8e0;--rule2:#b3bcc7;--blue:#2a78d6;--orange:#eb6834;--good:#1baf7a;
  --sans:-apple-system,"Helvetica Neue",Segoe UI,sans-serif;
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
@media(prefers-color-scheme:dark){:root:where(:not([data-theme=light])){
  --paper:#101419;--card:#171c23;--wash:#1c222a;--ink:#e9edf3;--ink2:#98a2af;--ink3:#78838f;
  --rule:#2a323c;--rule2:#3a444f;--blue:#3987e5;--orange:#d95926;--good:#199e70}}
:root[data-theme=dark]{--paper:#101419;--card:#171c23;--wash:#1c222a;--ink:#e9edf3;--ink2:#98a2af;
  --ink3:#78838f;--rule:#2a323c;--rule2:#3a444f;--blue:#3987e5;--orange:#d95926;--good:#199e70}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.6 var(--sans)}
a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline}
a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,
label:focus-within{outline:2px solid var(--blue);outline-offset:2px}
header.top{border-bottom:1px solid var(--rule);background:var(--card)}
.top-in{max-width:60rem;margin:0 auto;padding:.85rem 1.2rem;display:flex;gap:1rem;
  align-items:baseline;justify-content:space-between}
.brand{font-weight:600;letter-spacing:-.01em}
.brand span{color:var(--ink3);font-weight:400}
main{max-width:60rem;margin:0 auto;padding:2rem 1.2rem 5rem}
h1{font-size:1.6rem;letter-spacing:-.02em;margin:0 0 .3rem;text-wrap:balance}
h2{font-size:1.05rem;margin:2.2rem 0 .5rem}
.lede{color:var(--ink2);margin:0 0 1.8rem;font-size:1.05rem;max-width:40rem}
.card{background:var(--card);border:1px solid var(--rule);padding:1.2rem 1.3rem;margin:0 0 1rem}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(15rem,1fr));gap:1rem}
button,.btn{font:inherit;font-weight:500;padding:.6rem 1.1rem;border-radius:3px;cursor:pointer;
  border:1px solid var(--rule2);background:var(--card);color:var(--ink);display:inline-block}
.btn.primary,button.primary{background:var(--blue);border-color:var(--blue);color:#fff}
.btn.ghost{border-color:transparent;color:var(--ink2);padding-left:0}
button[disabled]{opacity:.5;cursor:not-allowed}
input[type=text],input[type=number],select{font:inherit;padding:.55rem .7rem;width:100%;
  border:1px solid var(--rule2);border-radius:3px;background:var(--paper);color:var(--ink)}
label.field{display:block;margin:0 0 1rem}
label.field>span{display:block;font-size:.85rem;color:var(--ink2);margin-bottom:.3rem}
.steps{display:flex;gap:.4rem;flex-wrap:wrap;margin:0 0 1.6rem;font-size:.82rem}
.steps b,.steps i{font-style:normal;padding:.25rem .6rem;border:1px solid var(--rule);
  border-radius:99px;color:var(--ink3)}
.steps b{background:var(--blue);border-color:var(--blue);color:#fff;font-weight:500}
.steps .done{color:var(--ink2);border-color:var(--rule2)}
.note{border-left:3px solid var(--rule2);padding:.7rem 0 .7rem 1rem;color:var(--ink2);margin:1rem 0}
.note.good{border-color:var(--good)}.note.warn{border-color:var(--orange)}
.note b{color:var(--ink);display:block;margin-bottom:.15rem}
.pick{display:block;border:1px solid var(--rule);background:var(--card);padding:1rem 1.1rem;
  margin:0 0 .7rem;cursor:pointer}
.pick:hover{border-color:var(--blue)}
.pick>input{float:left;margin:.3rem .7rem 0 0;transform:scale(1.15)}
.pick b,.pick small{margin-left:1.6rem;display:block}
.pick b{display:block;font-weight:600}
.pick small{color:var(--ink2);display:block;margin-top:.2rem;line-height:1.5}
.pick .tag{float:right;font:11px var(--mono);color:var(--orange);letter-spacing:.05em}
table{border-collapse:collapse;width:100%;font-size:.93rem}
th,td{text-align:left;padding:.45rem .8rem .45rem 0;border-bottom:1px solid var(--rule);
  vertical-align:top}
th{font:600 11px var(--mono);letter-spacing:.09em;text-transform:uppercase;color:var(--ink2)}
td.n{font-family:var(--mono);font-variant-numeric:tabular-nums;text-align:right}
.up{color:var(--good)}.down{color:var(--orange)}
.chip{display:inline-block;font:11px var(--mono);letter-spacing:.06em;text-transform:uppercase;
  padding:.15em .5em;border:1px solid currentColor;border-radius:2px;color:var(--ink2)}
.chip.live{color:var(--blue)}.chip.ok{color:var(--good)}.chip.bad{color:var(--orange)}
.sample{background:var(--wash);border:1px solid var(--rule);padding:.8rem .9rem;margin:.6rem 0;
  font-size:.9rem;white-space:pre-wrap;word-break:break-word}
.sample .lbl{font:11px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--ink3);
  display:block;margin:.9rem 0 .3rem;padding-top:.6rem;border-top:1px solid var(--rule)}
.sample .lbl:first-child{margin-top:0;padding-top:0;border-top:0}
pre.log{background:var(--wash);border:1px solid var(--rule);padding:.8rem;overflow-x:auto;
  font:12px/1.55 var(--mono);color:var(--ink2);max-height:16rem}
svg.curve{width:100%;height:auto;display:block;margin:.6rem 0}
.muted{color:var(--ink2)}.small{font-size:.88rem}
.row{display:flex;gap:.7rem;align-items:center;flex-wrap:wrap;margin-top:1.2rem}
.empty{color:var(--ink2);padding:2rem 0;text-align:center}
.bar{height:6px;background:var(--wash);border-radius:99px;overflow:hidden;margin:0 0 .5rem}
.bar i{display:block;height:100%;background:var(--blue);transition:width .6s ease}
@media(prefers-reduced-motion:reduce){.bar i{transition:none}}
/* the playground: ask your own model something */
textarea{font:inherit;width:100%;min-height:8.5rem;padding:.7rem .8rem;border:1px solid var(--rule2);
  border-radius:3px;background:var(--paper);color:var(--ink);resize:vertical;line-height:1.55}
.chips{display:flex;gap:.4rem;flex-wrap:wrap;margin:.6rem 0 0}
.chips button{font-size:.82rem;padding:.3rem .65rem;color:var(--ink2)}
.answers{display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-top:1.2rem}
@media(max-width:720px){.answers{grid-template-columns:1fr}}
.answer{background:var(--card);border:1px solid var(--rule);padding:1rem 1.1rem}
.answer.mine{border-color:var(--blue);box-shadow:0 0 0 1px var(--blue) inset}
.answer h3{margin:0 0 .5rem;font-size:.8rem;letter-spacing:.08em;text-transform:uppercase;
  color:var(--ink2);font-weight:600;display:flex;justify-content:space-between;gap:.6rem}
.answer.mine h3{color:var(--blue)}
.answer p{margin:0;white-space:pre-wrap;line-height:1.62}
.answer .took{font:11px var(--mono);color:var(--ink3);letter-spacing:0;text-transform:none}
.answer .bad{color:var(--orange)}
.thinking{color:var(--ink2)}
.thinking:after{content:'';display:inline-block;width:.5em;height:.5em;margin-left:.4em;
  border-radius:50%;background:var(--blue);animation:pulse 1.1s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:.25}50%{opacity:1}}
@media(prefers-reduced-motion:reduce){.thinking:after{animation:none;opacity:.6}}
"""


def shell(title: str, body: str, *, refresh: int = 0) -> str:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{meta}
<title>{html.escape(title)} · grid train</title><style>{_CSS}</style></head><body>
<header class="top"><div class="top-in">
  <span class="brand"><a href="/">Teach a model</a> <span>· on your own computers</span></span>
  <span class="small muted">nothing leaves your network</span>
</div></header><main>{body}</main></body></html>"""


def steps_bar(current: str) -> str:
    out = []
    reached = STEPS.index(current) if current in STEPS else 0
    for i, key in enumerate(STEPS):
        label = html.escape(STEP_LABELS[key])
        if i == reached:
            out.append(f"<b>{label}</b>")
        elif i < reached:
            out.append(f'<i class="done">{label}</i>')
        else:
            out.append(f"<i>{label}</i>")
    return f'<div class="steps">{"".join(out)}</div>'


# --- home ----------------------------------------------------------------------------------

def home(workspaces: list, nodes: dict) -> str:
    if workspaces:
        rows = "".join(
            f"<tr><td><a href='/w/{html.escape(w.slug)}'>{html.escape(w.name)}</a></td>"
            f"<td class='muted small'>{html.escape(PACK_TITLES.get(w.pack, w.pack))}</td>"
            f"<td>{_stage_chip(w)}</td>"
            f"<td class='muted small'>{html.escape(str(w.meta.get('updated', ''))[:16].replace('T', ' '))}</td>"
            f"<td>{_row_action(w)}</td></tr>"
            for w in workspaces
        )
        table = f"""<table><tr><th>model</th><th>learning to</th><th>state</th><th>updated</th>
<th></th></tr>
{rows}</table>"""
    else:
        table = ('<p class="empty">No models yet. Teaching one takes about ten minutes of your '
                 "time, plus a night of your computers'.</p>")
    return shell("Your models", f"""
<h1>Your models</h1>
<p class="lede">A model here is trained on your own examples, by your own computers, and it only
starts serving anyone once it has beaten the model you use today on work it has never seen.</p>
<div class="card">{table}</div>
<div class="row"><a class="btn primary" href="/new">Teach a new model</a>
<a class="btn ghost" href="/machines">See the machines helping ({nodes.get('count', 0)})</a></div>
""")


def _row_action(w) -> str:
    """The most useful next click for this model, right in the list."""
    if w.stage == "done":
        return (f"<a href='/w/{html.escape(w.slug)}/try'>Try it</a> · "
                f"<a href='/w/{html.escape(w.slug)}/result'>Result</a>")
    if w.stage == "running":
        return f"<a href='/w/{html.escape(w.slug)}/progress'>Watch</a>"
    return f"<a href='/w/{html.escape(w.slug)}'>Continue</a>"


def _stage_chip(w) -> str:
    stage = w.stage
    if stage == "running":
        return '<span class="chip live">learning</span>'
    if stage == "done":
        return '<span class="chip ok">ready</span>'
    return f'<span class="chip">{html.escape(STEP_LABELS.get(stage, stage)).lower()}</span>'


PACK_TITLES = {
    "support-replies": "Draft replies to support tickets",
    "sales-triage": "Sort and prioritise incoming leads",
}
PACK_BLURBS = {
    "support-replies": ("Give it your resolved tickets and it learns to draft the reply your team "
                        "would have written — in your voice, using the details of each ticket."),
    "sales-triage": ("Give it leads whose outcome you already know and it learns to flag which new "
                     "ones are worth your morning. The answer key is what actually closed, so this "
                     "is the most reliable kind of training there is."),
}
PACK_NEEDS = {
    "support-replies": "An export with the customer's message and the reply your team sent.",
    "sales-triage": "An export with each enquiry and what happened to it (won, lost, no response).",
}


def new_model() -> str:
    cards = "".join(
        f"""<label class="pick"><input type="radio" name="pack" value="{key}"{' checked' if i == 0 else ''}>
<b>{html.escape(PACK_TITLES[key])}</b>
<small>{html.escape(PACK_BLURBS[key])}<br><em>You'll need:</em> {html.escape(PACK_NEEDS[key])}</small></label>"""
        for i, key in enumerate(PACK_TITLES)
    )
    return shell("Teach a new model", f"""
<h1>What should it learn?</h1>
<p class="lede">Pick the job. You'll add your own examples next — the model learns from those,
not from anything generic.</p>
<form method="post" action="/new" class="card">
{cards}
<label class="field"><span>Give it a name (so you recognise it later)</span>
<input type="text" name="name" value="Support replies" required maxlength="60"></label>
<button class="primary" type="submit">Next — add your examples</button>
</form>
<p class="small muted">Something else in mind? The command line takes any task you can write a
check for — see <code>grid train packs</code>.</p>
""")


# --- step 1: data --------------------------------------------------------------------------

def data_step(w) -> str:
    report = w.report
    banner = ""
    if report:
        level = report.get("level")
        cls = {"good": "good", "thin": "", "blocked": "warn"}.get(level, "")
        samples = "".join(
            f"""<div class="sample"><span class="lbl">the work</span>{html.escape(s['prompt'])}
<span class="lbl" style="margin-top:.6rem">what your team did</span>{html.escape(s['reference'])}</div>"""
            for s in report.get("samples", [])
        )
        dist = report.get("distribution") or {}
        dist_line = ""
        if dist:
            dist_line = ('<p class="small muted">Groups found: '
                         + ", ".join(f"{html.escape(k)} {v}" for k, v in dist.items()) + "</p>")
        banner = f"""<div class="note {cls}"><b>{html.escape(report['headline'])}</b>
{html.escape(report['detail'])}</div>
<p class="small muted">Read {report.get('rows_seen', 0)} rows · using
{report.get('rows_usable', 0)} of them.</p>{dist_line}
<h2>What it will learn from</h2>{samples}"""
        if report.get("ok"):
            banner += f"""<div class="row"><a class="btn primary" href="/w/{html.escape(w.slug)}/checks">
Next — what good looks like</a></div>"""

    return shell("Add your examples", steps_bar("data") + f"""
<h1>Add your examples</h1>
<p class="lede">{html.escape(PACK_NEEDS.get(w.pack, ''))} A CSV or JSONL export from your helpdesk,
CRM or a spreadsheet all work. It stays on this computer.</p>
<div class="card">
  <label class="field"><span>Choose your export file</span>
  <input type="file" id="file" accept=".csv,.jsonl,.json,.ndjson,.txt"></label>
  <button class="primary" id="go" type="button">Check my file</button>
  <span id="status" class="small muted" style="margin-left:.7rem"></span>
</div>
{banner}
<script>
const go = document.getElementById('go'), input = document.getElementById('file'),
      note = document.getElementById('status');
go.addEventListener('click', async () => {{
  const file = input.files && input.files[0];
  if (!file) {{ note.textContent = 'Pick a file first.'; return; }}
  go.disabled = true; note.textContent = 'Reading ' + file.name + '…';
  const text = await file.text();
  const response = await fetch('/w/{w.slug}/data', {{
    method: 'POST', headers: {{'content-type': 'application/json'}},
    body: JSON.stringify({{filename: file.name, content: text}})
  }});
  if (response.ok) {{ location.reload(); }}
  else {{ go.disabled = false; note.textContent = 'That upload failed: ' + await response.text(); }}
}});
</script>
""")


# --- step 2: checks ------------------------------------------------------------------------

def checks_step(w, checks: list[dict]) -> str:
    boxes = "".join(
        f"""<label class="pick"><input type="checkbox" name="check" value="{c['id']}"
{'checked' if c.get('default') or c.get('locked') else ''}{' disabled' if c.get('locked') else ''}>
{f'<span class="tag">{html.escape(c["cost"])}</span>' if c.get('cost') else ''}
<b>{html.escape(c['label'])}{' (always on)' if c.get('locked') else ''}</b>
<small>{html.escape(c['help'])}</small>
{f'<input type="hidden" name="check" value="{c["id"]}">' if c.get('locked') else ''}</label>"""
        for c in checks
    )
    return shell("What good looks like", steps_bar("checks") + f"""
<h1>What does a good answer look like?</h1>
<p class="lede">This is the part only you can decide. The model tries something, these checks score
it, and it keeps what scores well — so what you tick here is what it learns to be.</p>
<form method="post" action="/w/{html.escape(w.slug)}/checks" class="card">{boxes}
<button class="primary" type="submit">Next — choose machines</button></form>
<p class="small muted">You can change these and train again; each attempt is cheap.</p>
""")


# --- step 3: machines ---------------------------------------------------------------------

def machines_step(w, machines: list, cap, models: list[dict], chosen_model: str = "") -> str:
    """Which computers, how long — and nothing she would have to ask an engineer about.

    Gone from this screen: the grid's address, the model's repository id, and a number of
    "attempts". Each was a question only an engineer can answer, and each was a place to stop.
    """
    if not cap.ready:
        return shell("Machines", steps_bar("machines") + f"""
<h1>{html.escape(cap.headline)}</h1>
<div class="note warn"><b>Almost ready</b>{html.escape(cap.detail)}</div>
<div class="row"><a class="btn" href="/w/{html.escape(w.slug)}">Back</a>
<a class="btn ghost" href="/w/{html.escape(w.slug)}/checks">Change what good looks like</a></div>
""")

    if machines:
        # The value is the option's position, not its address: she never reads a URL, and a
        # tampered form can't aim the trainer at an arbitrary host.
        options = "".join(
            f"""<label class="pick"><input type="radio" name="machine" value="{i}"
{' checked' if i == 0 else ''}>
<b>{html.escape(m.label)}</b>
<small>{html.escape(m.detail)}{'' if m.serves_training else ' · can answer, but cannot run the feedback stage'}</small></label>"""
            for i, m in enumerate(machines)
        )
        machine_block = f"""<h2>Which computer answers</h2>{options}
<details><summary>Somewhere else</summary>
<label class="field"><span>Address of a machine that is already serving</span>
<input type="text" name="endpoint" placeholder="http://a-machine.local:8080/v1"></label></details>"""
    else:
        machine_block = """<div class="note warn"><b>Nothing is serving models on this computer</b>
That is fine for this stage — it learns from your examples locally. To have other machines help,
run <code>grid train serve</code> on them.</div>
<input type="hidden" name="endpoint" value="">"""

    # Again the position, not the identifier: the page shows what it costs her, and the server
    # owns the mapping to a real model — so nothing a form can say becomes a download URL.
    model_options = "".join(
        f"""<label class="pick"><input type="radio" name="model" value="{i}"
{' checked' if m['id'] == chosen_model or (not chosen_model and m.get('default')) else ''}>
<span class="tag">{html.escape(m['size'])}</span>
<b>{html.escape(m['label'])}</b>
<small>{html.escape(m['detail'])}</small></label>"""
        for i, m in enumerate(models)
    )
    from .machines import EFFORT_CHOICES

    effort_options = "".join(
        f"""<label class="pick"><input type="radio" name="effort" value="{c['id']}"
{' checked' if c.get('default') else ''}>
<b>{html.escape(c['label'])}</b><small>{html.escape(c['detail'])}</small></label>"""
        for c in EFFORT_CHOICES
    )

    return shell("Machines", steps_bar("machines") + f"""
<h1>Where should it learn?</h1>
<p class="lede">{html.escape(cap.headline)} {html.escape(cap.detail)}</p>
<form method="post" action="/w/{html.escape(w.slug)}/start">
  <div class="card">{machine_block}</div>
  <div class="card"><h2>Which model to start from</h2>
  <p class="small muted">A smaller one is faster and needs less space; a larger one usually writes
  better. It downloads once.</p>{model_options}</div>
  <div class="card"><h2>How long to give it</h2>{effort_options}</div>
  <div class="row"><button class="primary" type="submit">Start learning</button>
  <a class="btn ghost" href="/w/{html.escape(w.slug)}/checks">Back</a></div>
</form>
<p class="small muted">You can close this page — it keeps going. Nothing is served to anyone until
you have seen the before-and-after and pressed the button yourself.</p>
""")


# --- step 4: running ---------------------------------------------------------------------

def running_step(w, job: dict) -> str:
    """What is happening, how long is left, and — if it stopped — why, with a way forward.

    The version of this page that said "learning now" forever after a dead trainer was worse than
    an error: she waits all night and never trusts the tool again. Every state here ends in an
    action.
    """
    if not job.get("exists"):
        return shell("Learning", steps_bar("running") + f"""
<h1>Nothing running yet</h1>
<p class="lede">This model has not been started.</p>
<div class="row"><a class="btn primary" href="/w/{html.escape(w.slug)}">Set it going</a></div>""")

    state = job.get("state", "running")
    slug = html.escape(w.slug)
    curve = curve_svg(job["points"])
    progress = ""
    if job.get("expected_steps") and job["steps_done"]:
        done = min(job["steps_done"], job["expected_steps"])
        pct = int(100 * done / job["expected_steps"])
        progress = (f'<div class="bar"><i style="width:{pct}%"></i></div>'
                    f'<p class="small muted">step {done} of {job["expected_steps"]}'
                    + (f" · {html.escape(job['eta'])}" if job.get("eta") else "") + "</p>")

    if state == "running":
        chip = '<span class="chip live">learning now</span>'
        lede = (f"{html.escape(job.get('phase') or 'Working.')} "
                f"{job['minutes']} minutes in so far.")
        action = (f"""<form method="post" action="/w/{slug}/stop">
<button type="submit">Stop</button></form>
<a class="btn ghost" href="/">Leave it running</a>""")
    elif state == "done":
        chip = '<span class="chip ok">finished</span>'
        lede = f"Done in {job['minutes']} minutes. The next step is to see whether it is better."
        action = f"""<a class="btn primary" href="/w/{slug}/result">See whether it's better</a>
<a class="btn" href="/w/{slug}/try">Try it on a real one</a>"""
    else:
        chip = '<span class="chip bad">stopped</span>'
        lede = html.escape(job.get("reason") or "It stopped before finishing.")
        action = f"""<a class="btn primary" href="/w/{slug}/again">Start again</a>
<a class="btn" href="/w/{slug}/checks">Change what good looks like</a>
<a class="btn ghost" href="/w/{slug}?step=data">Add more examples</a>"""

    return shell("Learning", steps_bar("running") + f"""
<h1>{html.escape(w.name)} {chip}</h1>
<p class="lede">{lede}</p>
<div class="card">{progress}{curve}</div>
<div class="row">{action}</div>
<details><summary>Technical details</summary>
<pre class="log">{html.escape(job['log_tail'] or 'starting…')}</pre></details>
""", refresh=15 if state == "running" else 0)


def curve_svg(points: list[dict]) -> str:
    """The one chart that matters — and it plots whichever signal this run produces.

    A feedback run scores each attempt (higher is better). An imitation run reports how far off it
    is (lower is better). Showing "no scores yet" through an entire imitation run — which is the
    path a Mac takes — was a blank chart all night, so the axis follows the data.
    """
    rising = [(p["step"], p["reward_mean"]) for p in points if "reward_mean" in p]
    falling = [(p["step"], p["loss"]) for p in points if "loss" in p and "reward_mean" not in p]
    series, better = (rising, "higher is better") if rising else (falling, "lower is better")
    if not series:
        return ('<p class="muted small">Nothing to plot yet — the first minute is spent getting '
                'the model ready.</p>')
    w, h, pad = 640, 150, 26
    max_step = max(s for s, _ in series) or 1
    hi = max(max(v for _, v in series), 1.0)

    def x(step):
        return pad + (step / max_step) * (w - pad - 60)

    def y(value):
        return h - pad - (value / hi) * (h - 2 * pad)

    line = " ".join(f"{x(s):.1f},{y(v):.1f}" for s, v in series)
    # A short moving average is the honest "is it working" read on a noisy per-attempt score.
    window = max(3, len(series) // 10)
    smooth = []
    for i in range(len(series)):
        chunk = [v for _, v in series[max(0, i - window + 1): i + 1]]
        smooth.append((series[i][0], sum(chunk) / len(chunk)))
    trend = " ".join(f"{x(s):.1f},{y(v):.1f}" for s, v in smooth)
    return f"""<svg class="curve" viewBox="0 0 {w} {h}" role="img"
 aria-label="score per attempt, rising from {series[0][1]:.2f} to {smooth[-1][1]:.2f}">
<line x1="{pad}" y1="{y(0):.1f}" x2="{w - 60}" y2="{y(0):.1f}" stroke="var(--rule)"/>
<line x1="{pad}" y1="{y(hi):.1f}" x2="{w - 60}" y2="{y(hi):.1f}" stroke="var(--rule)"/>
<polyline points="{line}" fill="none" stroke="var(--rule2)" stroke-width="1"/>
<polyline points="{trend}" fill="none" stroke="var(--blue)" stroke-width="2.5"/>
<text x="{w - 54}" y="{y(smooth[-1][1]) + 4:.1f}" font-size="12" fill="var(--blue)"
 font-family="var(--mono)">{smooth[-1][1]:.2f}</text>
<text x="{pad}" y="{h - 6}" font-size="11" fill="var(--ink3)" font-family="var(--mono)">attempts · {better}</text>
</svg>"""


# --- step 5: result -----------------------------------------------------------------------

def try_step(w, samples: list[str], prompt: str = "", answers: list | None = None,
             *, serving: bool = True) -> str:
    """The playground: paste a piece of work, see both models answer it.

    Everything else in the product is *about* a model; this is where she meets it. Her own held-out
    work is one click away so the first thing she tries is real, and the two answers sit side by
    side because that comparison is the argument.
    """
    chips = "".join(
        f'<button type="button" class="btn" data-fill="{html.escape(s, quote=True)}">'
        f'Use one of my {"tickets" if w.pack == "support-replies" else "leads"} ({i + 1})</button>'
        for i, s in enumerate(samples)
    )
    if answers is None:
        panels = ""
    elif not answers:
        panels = '<div class="note warn">Type something first.</div>'
    else:
        panels = '<div class="answers">' + "".join(
            f'<div class="answer{" mine" if i else ""}"><h3>{html.escape(a.label)}'
            f'<span class="took">{a.seconds}s</span></h3>'
            + (f"<p>{html.escape(a.text) or '<em>(it said nothing)</em>'}</p>" if a.ok
               else f'<p class="bad">{html.escape(a.error)}</p>')
            + "</div>"
            for i, a in enumerate(answers)
        ) + "</div>"

    warning = "" if serving else """<div class="note warn"><b>Only the original model is loaded</b>
Press “Start using this model” on the result page and the comparison will show both.</div>"""

    return shell("Try it", f"""
<h1>Try it out</h1>
<p class="lede">Paste a real piece of work and see what your model says — next to what you use
today. It answers on your own machine; nothing is sent anywhere.</p>
{warning}
<form method="post" action="/w/{html.escape(w.slug)}/try" class="card" id="askform">
  <label class="field"><span>The work you'd hand it</span>
  <textarea name="prompt" id="prompt" placeholder="Paste a ticket, or use one of yours below…"
   required>{html.escape(prompt)}</textarea></label>
  <div class="chips">{chips}</div>
  <div class="row"><button class="primary" type="submit" id="ask">Show me the answer</button>
  <a class="btn ghost" href="/w/{html.escape(w.slug)}/result">Back to the result</a></div>
</form>
{panels}
<script>
for (const chip of document.querySelectorAll('.chips button')) {{
  chip.addEventListener('click', () => {{
    document.getElementById('prompt').value = chip.dataset.fill;
    document.getElementById('prompt').focus();
  }});
}}
document.getElementById('askform').addEventListener('submit', () => {{
  const button = document.getElementById('ask');
  button.disabled = true;
  button.textContent = 'Thinking…';
  button.className = 'primary thinking';
}});
</script>
""")


def result_step(w, card: dict | None, job: dict) -> str:
    if card is None:
        return shell("Result", steps_bar("done") + f"""
<h1>Ready to check</h1>
<p class="lede">Training finished. The next step scores this model against the one you use today,
on examples it has never seen — that comparison decides whether it gets used.</p>
<div class="card"><form method="post" action="/w/{html.escape(w.slug)}/check">
<button class="primary" type="submit">Check it on held-back work</button></form>
<p class="small muted" style="margin-top:.8rem">Takes a couple of minutes: both models answer the
same held-back examples.</p></div>""")

    passed = card["passed"]
    graders = sorted(set(card["before"]["per_grader"]) | set(card["after"]["per_grader"]))
    rows = "".join(
        f"<tr><td>{html.escape(_pretty(name))}</td>"
        f"<td class='n'>{card['before']['per_grader'].get(name, 0):.2f}</td>"
        f"<td class='n'>{card['after']['per_grader'].get(name, 0):.2f}</td>"
        f"<td class='n {'up' if card['after']['per_grader'].get(name, 0) >= card['before']['per_grader'].get(name, 0) else 'down'}'>"
        f"{card['after']['per_grader'].get(name, 0) - card['before']['per_grader'].get(name, 0):+.2f}</td></tr>"
        for name in graders
    )
    pairs = "".join(
        f"""<div class="sample"><span class="lbl">the work</span>{html.escape(b['prompt'][:400])}</div>
<div class="grid2">
<div class="sample"><span class="lbl">what you use today</span>{html.escape(b['completion'][:500])}</div>
<div class="sample" style="border-color:var(--blue)"><span class="lbl">your trained model</span>{html.escape(a['completion'][:500])}</div>
</div>"""
        for b, a in zip(card["before"]["samples"], card["after"]["samples"])
    )
    verdict_cls = "good" if passed else "warn"
    action = (f"""<form method="post" action="/w/{html.escape(w.slug)}/use">
<button class="primary" type="submit">Start using this model</button></form>
<a class="btn" href="/w/{html.escape(w.slug)}/try">Try it on a real one first</a>"""
              if passed else
              f"""<a class="btn" href="/w/{html.escape(w.slug)}/try">See what it says anyway</a>
<a class="btn ghost" href="/new">Try again with more examples</a>""")
    return shell("Result", steps_bar("done") + f"""
<h1>{'It is better' if passed else 'Not better yet'}</h1>
<div class="note {verdict_cls}"><b>{html.escape(card['verdict'])}</b>
Measured on {card['after']['n']} held-back examples it never trained on.</div>
<div class="card"><table>
<tr><th>what we check</th><th>today</th><th>trained</th><th>change</th></tr>{rows}
<tr><th>overall</th><td class="n">{card['before']['overall']:.2f}</td>
<td class="n">{card['after']['overall']:.2f}</td>
<td class="n {'up' if card['delta'] >= 0 else 'down'}">{card['delta']:+.2f}</td></tr>
</table></div>
<div class="row">{action}<a class="btn ghost" href="/">Back to your models</a></div>
<h2>The same work, answered by both</h2>{pairs}
""")


def live_step(w, serving: dict, summary=None, nightly_on: bool = False) -> str:
    """Where she lands after pressing "Start using this model".

    The moment the product pays off should look like something, and it should tell her three
    things: it is answering now, how to point her tools at it, and that it will keep improving.
    """
    slug = html.escape(w.slug)
    name = html.escape(serving.get("name") or w.slug)
    nodes = serving.get("nodes") or []
    where = ", ".join(html.escape(str(n.get("node", ""))) for n in nodes if n.get("ok"))
    collecting = ""
    if summary is not None:
        collecting = f"""<div class="card"><h2>It keeps getting better</h2>
<p class="small muted">{html.escape(summary.headline)} {html.escape(summary.advice)}</p>
<form method="post" action="/w/{slug}/nightly" class="row">
<button class="{'' if nightly_on else 'primary'}" type="submit" name="nightly"
 value="{'off' if nightly_on else 'on'}">
{'Stop improving it nightly' if nightly_on else 'Improve it every night, automatically'}</button>
<span class="small muted">{'On — it trains overnight when the machine is free and only'
 ' replaces itself if it wins.' if nightly_on else 'Trains overnight on what your team corrected'
 ' today, and only replaces itself if it wins.'}</span></form></div>"""

    return shell("It's live", f"""
<h1>{html.escape(w.name)} is answering now 🎉</h1>
<p class="lede">Your team's own model, trained on your own examples, running on your own
computers. {('Loaded on ' + where + '.') if where else ''}</p>
<div class="card"><h2>Point your tools at it</h2>
<p class="small muted">Anything that speaks the OpenAI API works — ask whoever set up your
helpdesk to use these two lines.</p>
<pre class="log">OPENAI_BASE_URL={html.escape((w.meta.get('config') or {}).get('endpoint', ''))}
model: {name}</pre></div>
{collecting}
<div class="row"><a class="btn primary" href="/w/{slug}/try">Try it on another ticket</a>
<a class="btn" href="/w/{slug}/result">See the before and after</a>
<a class="btn ghost" href="/">Your models</a></div>
""")


def _pretty(grader: str) -> str:
    return {
        "similarity": "Sounds like your team",
        "grounding": "Uses the ticket's specifics",
        "format": "Ready to send",
        "judge": "Tone (rated locally)",
        "correct_priority": "Gets the priority right",
        "parseable": "Readable by your tools",
    }.get(grader, grader.replace("_", " "))


def machines_page(nodes: dict) -> str:
    rows = "".join(
        f"<tr><td>{html.escape(n['name'])}</td><td class='muted small'>{html.escape(n['detail'])}</td>"
        f"<td>{html.escape(n['role'])}</td></tr>" for n in nodes.get("nodes", [])
    )
    body = (f"<table><tr><th>machine</th><th>what it runs</th><th>role in training</th></tr>{rows}</table>"
            if rows else '<p class="empty">No machines found on this computer\'s grid yet.</p>')
    return shell("Machines", f"""
<h1>The machines helping</h1>
<p class="lede">{html.escape(nodes.get('summary', ''))}</p>
<div class="card">{body}</div>
<p class="small muted">A machine joins with <code>grid join</code>, and offers itself for training
with <code>grid train serve</code>. Laptops can come and go — a run skips whatever is asleep.</p>
<div class="row"><a class="btn ghost" href="/">Back to your models</a></div>
""")


def error_page(title: str, message: str, *, back: str = "/") -> str:
    return shell(title, f"""<h1>{html.escape(title)}</h1>
<div class="note warn">{html.escape(message)}</div>
<div class="row"><a class="btn" href="{html.escape(back)}">Go back</a></div>""")


def json_dump(value) -> str:
    return json.dumps(value, indent=2)
