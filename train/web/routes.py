"""The routes. Thin: every handler reads a workspace, calls one thing, renders one page."""
from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path
from urllib.parse import parse_qs

# Module level, not inside register(): with `from __future__ import annotations` FastAPI resolves
# each handler's type hints against THIS module's globals, so a Request imported in a local scope
# is invisible to it and every handler 422s on a "missing query field".
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import jobs, pages, workspace


def _nodes() -> dict:
    """What machines can help, in words a non-engineer can act on.

    Uses the same local-port probe `grid join` uses, so what the page shows is what the grid
    would actually find — no separate notion of a machine.
    """
    try:
        from shared.system.detect import detect_engines

        found = detect_engines(timeout=0.4)
    except Exception:  # noqa: BLE001 — a probe failure must degrade to "none found", never 500
        found = []
    nodes = []
    trainable = False
    for engine in found:
        # vLLM and Grid's MLX server return what training needs; the others serve inference,
        # judging and evaluation only. Stated on the page rather than discovered at step one.
        can_train = engine.label in ("vllm", "mlx")
        trainable = trainable or can_train
        nodes.append({
            "name": engine.label,
            "detail": ", ".join(engine.models[:3]) or engine.endpoint_url,
            "role": "attempts + judging" if can_train else "judging and checking only",
        })
    if not nodes:
        summary = ("Nothing is serving models on this computer yet. On a Mac, `grid train serve` "
                   "makes it available for training.")
    else:
        summary = (f"{len(nodes)} engine(s) found on this computer. "
                   + ("At least one can run training attempts."
                      if trainable else
                      "None of them can run training attempts yet — that needs vLLM, or "
                      "`grid train serve` on a Mac."))
    return {"count": len(nodes), "nodes": nodes, "trainable": trainable, "summary": summary}


def _model_choices(backend: str) -> list[dict]:
    """The starting models, described by what they cost her rather than by repository id.

    A repo id like "Qwen/Qwen3-4B-Instruct-2507" is meaningless to the person choosing, and the
    thing she actually needs to weigh is download size against quality. MLX and torch want
    differently packaged weights, so the list depends on which trainer will run.
    """
    if backend == "mlx":
        return [
            {"id": "mlx-community/Qwen2.5-1.5B-Instruct-4bit", "label": "Small and quick",
             "size": "1 GB", "detail": "Fine for sorting and short replies. Trains in minutes."},
            {"id": "mlx-community/Qwen3-4B-Instruct-2507-4bit", "label": "Recommended",
             "size": "2.5 GB", "detail": "The usual choice for drafting replies.",
             "default": True},
            {"id": "mlx-community/Qwen3-8B-4bit", "label": "Best quality",
             "size": "5 GB", "detail": "Noticeably better writing; wants 32 GB of memory."},
        ]
    return [
        {"id": "HuggingFaceTB/SmolLM2-135M-Instruct", "label": "Tiny — for trying this out",
         "size": "0.3 GB", "detail": "Learns fast, writes poorly. Good for a first pass."},
        {"id": "Qwen/Qwen2.5-1.5B-Instruct", "label": "Small and quick",
         "size": "3 GB", "detail": "Fine for sorting and short replies."},
        {"id": "Qwen/Qwen3-4B-Instruct-2507", "label": "Recommended",
         "size": "8 GB", "detail": "The usual choice for drafting replies.", "default": True},
    ]


async def _form(request) -> dict[str, list[str]]:
    """Parse an urlencoded form body ourselves.

    Starlette's `request.form()` needs python-multipart, and a whole extra dependency is a poor
    trade for one POST body. Parsing it here also keeps every page working with plain HTML forms
    and no JavaScript. Values come back as lists, because checkbox groups repeat their name.
    """
    raw = (await request.body()).decode("utf-8", errors="replace")
    return parse_qs(raw, keep_blank_values=True)


def _one(form: dict[str, list[str]], key: str, default: str = "") -> str:
    values = form.get(key) or []
    return values[0].strip() if values else default


def _keep_a_copy(w, adapter: Path) -> str:
    """Copy the adapter aside before it becomes the served one.

    `run/adapter` is overwritten by the next training run, so without this the "go back" button
    would point at whatever was trained most recently — which is the thing being reverted.
    """
    import shutil

    stamp = time.strftime("%Y%m%d-%H%M%S")
    root = Path(w.path) / "served"
    kept = root / stamp
    # Two deploys inside one second would otherwise land in the same folder, and "the previous
    # model" would point at the model being replaced — a revert button that does nothing.
    suffix = 2
    while kept.exists():
        kept = root / f"{stamp}-{suffix}"
        suffix += 1
    try:
        kept.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(adapter, kept, dirs_exist_ok=True)
    except OSError:
        return str(adapter)      # better a live pointer than none; the revert page checks it
    return str(kept)


def register(app) -> None:
    def _load(slug: str):
        try:
            return workspace.load(slug)
        except KeyError:
            raise HTTPException(404, f"no model called {slug!r}") from None

    @app.get("/", response_class=HTMLResponse)
    def home():
        # Ask each workspace's job what is true now. The stage in meta.json was written when a run
        # STARTED, so a crashed trainer left the list saying "learning" indefinitely — the exact
        # failure jobs.py exists to prevent.
        rows = []
        for w in workspace.list_all():
            live = jobs.status(w.path) if w.stage in ("running", "done") else {}
            rows.append((w, live.get("state", "")))
        return pages.home(rows, _nodes())

    @app.get("/machines", response_class=HTMLResponse)
    def machines():
        return pages.machines_page(_nodes())

    @app.get("/new", response_class=HTMLResponse)
    def new_form():
        return pages.new_model()

    @app.post("/new")
    async def new_submit(request: Request):
        form = await _form(request)
        pack = _one(form, "pack", "support-replies")
        name = _one(form, "name") or pages.PACK_TITLES.get(pack, "My model")
        if pack not in pages.PACK_TITLES:
            raise HTTPException(400, "unknown kind of training")
        created = workspace.create(name, pack)
        return RedirectResponse(f"/w/{created.slug}", status_code=303)

    @app.get("/w/{slug}", response_class=HTMLResponse)
    def workspace_page(slug: str, step: str = ""):
        w = _load(slug)
        # `?step=` lets any earlier step be revisited: adding examples or changing the checks must
        # never mean starting a new model from scratch.
        stage = step or w.stage
        if stage == "data":
            return pages.data_step(w)
        if stage == "checks":
            return pages.checks_step(w, workspace.CHECKS[w.pack])
        if stage == "machines":
            if not w.report.get("ok"):
                return HTMLResponse(pages.error_page(
                    "Add your examples first",
                    "There is nothing to learn from yet, so there is nothing to start.",
                    back=f"/w/{slug}?step=data"), status_code=400)
            from train.web.machines import capability, find_machines

            machines = find_machines()
            cap = capability(machines)
            return pages.machines_step(w, machines, cap, _model_choices(cap.backend))
        if stage in ("done", "serving"):
            return RedirectResponse(f"/w/{slug}/result", status_code=303)
        return RedirectResponse(f"/w/{slug}/progress", status_code=303)

    @app.get("/w/{slug}/again")
    def again(slug: str):
        """Train this model again — same examples, same checks, a fresh run."""
        w = _load(slug)
        w.meta["stage"] = "machines"
        w.save()
        return RedirectResponse(f"/w/{slug}", status_code=303)

    @app.post("/w/{slug}/data")
    async def upload(slug: str, request: Request):
        w = _load(slug)
        body = await request.json()
        content = body.get("content")
        if not isinstance(content, str) or not content.strip():
            raise HTTPException(400, "that file looked empty")
        if len(content) > 64 * 1024 * 1024:
            raise HTTPException(400, "that file is over 64 MB — try a shorter date range")
        report = workspace.attach_data(w, str(body.get("filename") or "upload.csv"), content)
        return JSONResponse({"ok": report.ok, "level": report.level, "headline": report.headline})

    @app.get("/w/{slug}/checks", response_class=HTMLResponse)
    def checks_page(slug: str):
        w = _load(slug)
        if not w.report.get("ok"):
            return HTMLResponse(pages.error_page(
                "Add some examples first", "There is not enough usable data yet to train on.",
                back=f"/w/{slug}"), status_code=400)
        if w.stage == "data":
            w.meta["stage"] = "checks"
            w.save()
        return pages.checks_step(w, workspace.CHECKS[w.pack])

    @app.post("/w/{slug}/checks")
    async def choose_checks(slug: str, request: Request):
        w = _load(slug)
        form = await _form(request)
        chosen = list(dict.fromkeys(form.get("check") or []))
        workspace.write_checks(w, chosen)
        return RedirectResponse(f"/w/{slug}", status_code=303)

    @app.post("/w/{slug}/start")
    async def start(slug: str, request: Request):
        w = _load(slug)
        form = await _form(request)
        from train.web.machines import capability, effort, find_machines

        machines = find_machines()
        cap = capability(machines)
        if not cap.ready:
            return HTMLResponse(pages.error_page(
                "Almost ready", cap.detail, back=f"/w/{slug}"), status_code=400)

        choices = _model_choices(cap.backend)
        picked_model = _one(form, "model")
        model = ""
        if picked_model.isdigit() and int(picked_model) < len(choices):
            model = choices[int(picked_model)]["id"]
        if not model:
            return HTMLResponse(pages.error_page(
                "Pick a starting model", "Choose one of the options and press Start again.",
                back=f"/w/{slug}"), status_code=400)
        # She chose a label; the server maps it to an address. Immune to the list re-sorting
        # between the page being drawn and Start being pressed.
        label = _one(form, "machine")
        endpoint = next((m.url for m in machines if m.label == label), "")
        endpoint = endpoint or _one(form, "endpoint") or (
            machines[0].url if machines else "http://127.0.0.1:8080/v1")
        chosen_effort = effort(_one(form, "effort", "evening"))

        config = workspace.write_config(w, model=model, endpoint=endpoint,
                                       steps=chosen_effort["steps"])
        verb = "sft" if cap.recommended == "sft" else "run"
        extra = ["--run-dir", str(w.path / "run")] if verb == "sft" else []
        if verb == "sft":
            extra += ["--iters", str(chosen_effort["iters"])]
        jobs.start(w.path, config, verb=verb, extra=extra,
                   expected_steps=chosen_effort["steps" if verb == "run" else "iters"])
        w.meta["stage"] = "running"
        w.meta["run"] = {"verb": verb, "effort": chosen_effort["id"], "model": model,
                         "endpoint": endpoint, "expected_steps": chosen_effort["steps"]}
        w.save()
        return RedirectResponse(f"/w/{slug}/progress", status_code=303)

    @app.get("/w/{slug}/progress", response_class=HTMLResponse)
    def progress(slug: str):
        w = _load(slug)
        job = jobs.status(w.path)
        if job.get("finished_ok") and w.stage == "running":
            w.meta["stage"] = "done"
            w.save()
        return pages.running_step(w, job)

    @app.post("/w/{slug}/stop")
    def stop(slug: str):
        w = _load(slug)
        jobs.stop(w.path)
        return RedirectResponse(f"/w/{slug}/progress", status_code=303)

    @app.get("/w/{slug}/result", response_class=HTMLResponse)
    def result(slug: str):
        w = _load(slug)
        card_path = w.path / "run" / "eval-card.json"
        card = json.loads(card_path.read_text(encoding="utf-8")) if card_path.is_file() else None
        scoring = jobs.status(w.path, job="eval")
        if scoring.get("state") == "running" or (scoring.get("state") == "failed" and not card):
            return pages.scoring_step(w, scoring)
        return pages.result_step(w, card, jobs.status(w.path))

    @app.post("/w/{slug}/check")
    def check(slug: str):
        """Score the trained model against the incumbent — the gate.

        Launched as a job, not run inside this request: scoring means generating an answer for
        every held-out item with two models, which is minutes of white page if done here — and a
        reload would start a second one racing the first over the same card file.
        """
        w = _load(slug)
        config = w.path / "grid-train.toml"
        if not config.is_file():
            return HTMLResponse(pages.error_page(
                "Nothing to check yet", "This model has not been trained.",
                back=f"/w/{slug}"), status_code=400)
        name = (w.meta.get("run") or {}).get("adapter_name") or w.slug
        jobs.start(w.path, config, verb="eval", job="eval",
                   extra=["--run", str(w.path / "run"), "--candidate", name])
        return RedirectResponse(f"/w/{slug}/result", status_code=303)

    @app.get("/w/{slug}/try", response_class=HTMLResponse)
    def try_form(slug: str):
        w = _load(slug)
        from train.web.playground import sample_prompts

        return pages.try_step(w, sample_prompts(w.path / "run"),
                              serving=bool(w.meta.get("serving")))

    @app.post("/w/{slug}/try", response_class=HTMLResponse)
    async def try_ask(slug: str, request: Request):
        """Ask both models the same thing. Never 500s: a sleeping machine is a sentence, not a
        stack trace (train/web/playground.py::detail_of)."""
        w = _load(slug)
        form = await _form(request)
        prompt = _one(form, "prompt")
        from train.config import load_config
        from train.web.playground import compare, sample_prompts

        try:
            cfg = load_config(w.path / "grid-train.toml")
        except SystemExit as exc:
            return HTMLResponse(pages.error_page(
                "Nothing to try yet", str(exc), back=f"/w/{slug}"), status_code=400)
        trained = (w.meta.get("serving") or {}).get("name") or ""
        answers = compare(cfg, prompt, trained)
        return pages.try_step(w, sample_prompts(w.path / "run"), prompt=prompt, answers=answers,
                              serving=bool(trained))

    @app.post("/w/{slug}/use")
    def use(slug: str):
        """Deploy — but only if the card says it won. The gate is not a suggestion."""
        w = _load(slug)
        card_path = w.path / "run" / "eval-card.json"
        if not card_path.is_file():
            return HTMLResponse(pages.error_page(
                "Not checked yet", "Run the comparison first — nothing serves anyone unproven.",
                back=f"/w/{slug}/result"), status_code=400)
        card = json.loads(card_path.read_text(encoding="utf-8"))
        if not card.get("passed"):
            return HTMLResponse(pages.error_page(
                "This one did not earn it",
                "It did not beat the model you already use on held-back work, so it will not be "
                "served. Training again with more examples is the usual fix.",
                back=f"/w/{slug}/result"), status_code=400)

        from train.config import load_config
        from train.deploy import deploy_adapter

        try:
            cfg = load_config(w.path / "grid-train.toml")
            adapter = Path(w.path) / "run" / "adapter"
            results = deploy_adapter(adapter, cfg.deploy.nodes or (cfg.rollout.base_url,),
                                     cfg.deploy.adapter_name or w.slug)
        except SystemExit as exc:
            # Same contract as /check and /try: a machine problem is a sentence, not a 500.
            return HTMLResponse(pages.error_page(
                "Could not load it onto the machine", str(exc),
                back=f"/w/{slug}/result"), status_code=400)
        failed = [r for r in results if not r["ok"]]
        if any(r["ok"] for r in results):
            # Recorded only for machines that really loaded it: otherwise the payoff page said
            # "is answering now" about a model nothing was serving.
            previous = (w.meta.get("serving") or {}).get("adapter", "")
            kept = _keep_a_copy(w, adapter)
            w.meta["serving"] = {"nodes": [r for r in results if r["ok"]],
                                 "name": cfg.deploy.adapter_name or w.slug,
                                 "adapter": kept, "replaced": previous,
                                 "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            w.save()
        if failed:
            return HTMLResponse(pages.error_page(
                "Trained, but not loaded everywhere",
                "; ".join(f"{r['node']}: {r['detail']}" for r in failed),
                back=f"/w/{slug}/result"), status_code=502)
        w.meta["stage"] = "serving"
        w.save()
        return RedirectResponse(f"/w/{slug}/live", status_code=303)

    @app.post("/w/{slug}/revert")
    def revert(slug: str):
        """Put back the model that was serving before this one.

        Every other button here is reversible by not pressing it again. "Start using this model"
        was not: it replaced what her team relies on, and the only route back was a terminal. The
        gate is good but it is one comparison on held-back work — real use finds things it cannot,
        and the answer to that has to be a button, not a support ticket.
        """
        w = _load(slug)
        serving = w.meta.get("serving") or {}
        previous = serving.get("replaced") or ""
        if not previous or not Path(previous).is_dir():
            return HTMLResponse(pages.error_page(
                "There is nothing to go back to",
                "This is the first model you have served here, so what came before it is the "
                "starting model — putting that back means restarting the engine on that machine "
                "(`grid train serve`). Everything you uploaded and every check stays as it is.",
                back=f"/w/{slug}/live"), status_code=400)

        from train.config import load_config
        from train.deploy import deploy_adapter

        try:
            cfg = load_config(w.path / "grid-train.toml")
            results = deploy_adapter(Path(previous), cfg.deploy.nodes or (cfg.rollout.base_url,),
                                     serving.get("name") or w.slug)
        except SystemExit as exc:
            return HTMLResponse(pages.error_page("Could not put it back", str(exc),
                                                 back=f"/w/{slug}/live"), status_code=400)
        if not any(r["ok"] for r in results):
            return HTMLResponse(pages.error_page(
                "Could not put it back",
                "; ".join(f"{r['node']}: {r['detail']}" for r in results),
                back=f"/w/{slug}/live"), status_code=502)
        w.meta["serving"] = {"nodes": [r for r in results if r["ok"]],
                             "name": serving.get("name") or w.slug,
                             "adapter": previous, "replaced": "",
                             "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                             "reverted_from": serving.get("adapter", "")}
        w.save()
        return RedirectResponse(f"/w/{slug}/live", status_code=303)

    @app.get("/w/{slug}/live", response_class=HTMLResponse)
    def live(slug: str):
        w = _load(slug)
        serving = w.meta.get("serving")
        if not serving:
            return RedirectResponse(f"/w/{slug}/result", status_code=303)
        from train.capture import summarize

        return pages.live_step(w, serving, summarize(days=30),
                               nightly_on=bool(w.meta.get("nightly")))

    @app.get("/w/{slug}/overnight", response_class=HTMLResponse)
    def overnight(slug: str):
        """The unattended half, made visible: the store, tonight's plan, every past night.

        Nothing here is new machinery — `capture.summarize()` and `autopilot.history()` already
        answered these questions on the command line. The gap was that the person who owns the
        model could not see any of it, which makes "it improves overnight" a claim rather than
        a record.
        """
        from train import autopilot, hostsignals
        from train import schedule as sched
        from train.capture import summarize
        from train.config import load_config

        w = _load(slug)
        rows: list[dict] = []
        config_path = w.path / "grid-train.toml"
        if config_path.is_file():
            try:
                rows = autopilot.history(load_config(config_path))
            except (SystemExit, OSError, ValueError):
                rows = []          # an unreadable config must not hide the rest of the page
        return pages.overnight_page(
            w, summarize(days=30), rows, hostsignals.summary(),
            nightly_on=bool(w.meta.get("nightly")), min_examples=autopilot.MIN_EXAMPLES,
            schedule=sched.status(slug=w.slug, workspace=w.path),
        )

    @app.post("/w/{slug}/nightly")
    async def nightly(slug: str, request: Request):
        """Turn unattended improvement on or off for this model.

        This used to record an intention and print a crontab line for her to paste, which is
        another way of saying the model would never actually improve. It now installs a real
        per-user job (train/schedule.py) and reports exactly what happened — including the
        failure, because a page that claims a schedule that does not exist is the worst outcome
        available here.
        """
        w = _load(slug)
        form = await _form(request)
        wanted = _one(form, "nightly", "on") == "on"
        from train import schedule as sched

        config = w.path / "grid-train.toml"
        if wanted:
            from train.capture import Policy, load_policy, save_policy

            policy = load_policy()
            if not policy.enabled:      # nothing to learn from without it
                save_policy(Policy(**{**dataclasses.asdict(policy), "enabled": True,
                                      "teachers": list(policy.teachers)}))
            result = sched.install(w.path, slug=w.slug,
                                   config=config if config.is_file() else None)
        else:
            result = sched.remove(slug=w.slug, workspace=w.path)
        # The toggle follows what the scheduler actually did, not what was asked for.
        w.meta["nightly"] = bool(wanted and result.ok)
        w.meta["schedule"] = {"ok": result.ok, "detail": result.detail, "wanted": wanted}
        w.save()
        return RedirectResponse(f"/w/{slug}/live", status_code=303)

    @app.get("/api/workspaces")
    def api_workspaces():
        return JSONResponse([
            {"slug": w.slug, "name": w.name, "pack": w.pack, "stage": w.stage,
             "report": w.report.get("headline", "")}
            for w in workspace.list_all()
        ])
