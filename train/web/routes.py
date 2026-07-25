"""The routes. Thin: every handler reads a workspace, calls one thing, renders one page."""
from __future__ import annotations

import json
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


def register(app) -> None:
    def _load(slug: str):
        try:
            return workspace.load(slug)
        except KeyError:
            raise HTTPException(404, f"no model called {slug!r}") from None

    @app.get("/", response_class=HTMLResponse)
    def home():
        return pages.home(workspace.list_all(), _nodes())

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
    def workspace_page(slug: str):
        w = _load(slug)
        stage = w.stage
        if stage == "data":
            return pages.data_step(w)
        if stage == "checks":
            return pages.checks_step(w, workspace.CHECKS[w.pack])
        if stage == "machines":
            from train.web.machines import capability, find_machines

            machines = find_machines()
            cap = capability(machines)
            return pages.machines_step(w, machines, cap, _model_choices(cap.backend))
        return RedirectResponse(f"/w/{slug}/progress", status_code=303)

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
        # The form sends the chosen option's position; only this side knows the addresses.
        endpoint = ""
        picked = _one(form, "machine")
        if picked.isdigit() and int(picked) < len(machines):
            endpoint = machines[int(picked)].url
        endpoint = endpoint or _one(form, "endpoint") or (
            machines[0].url if machines else "http://127.0.0.1:8080/v1")
        chosen_effort = effort(_one(form, "effort", "evening"))

        config = workspace.write_config(w, model=model, endpoint=endpoint,
                                       steps=chosen_effort["steps"])
        verb = "sft" if cap.recommended == "sft" else "run"
        extra = ["--run-dir", str(w.path / "run")] if verb == "sft" else []
        if verb == "sft":
            extra += ["--iters", str(chosen_effort["iters"])]
        jobs.start(w.path, config, verb=verb, extra=extra)
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
        return pages.result_step(w, card, jobs.status(w.path))

    @app.post("/w/{slug}/check")
    def check(slug: str):
        """Score the trained model against the incumbent — the gate, run on demand."""
        w = _load(slug)
        from train.config import load_config
        from train.evaluate import run_eval

        try:
            cfg = load_config(w.path / "grid-train.toml")
            adapter_name = w.meta.get("config", {}).get("adapter_name") or w.slug
            run_eval(cfg, w.path / "run", adapter_name)
        except SystemExit as exc:
            return HTMLResponse(pages.error_page(
                "Could not check it yet", str(exc), back=f"/w/{slug}/progress"), status_code=400)
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

        cfg = load_config(w.path / "grid-train.toml")
        adapter = Path(w.path) / "run" / "adapter"
        results = deploy_adapter(adapter, cfg.deploy.nodes or (cfg.rollout.base_url,),
                                 cfg.deploy.adapter_name or w.slug)
        w.meta["serving"] = {"nodes": results, "name": cfg.deploy.adapter_name or w.slug}
        w.save()
        failed = [r for r in results if not r["ok"]]
        if failed:
            return HTMLResponse(pages.error_page(
                "Trained, but not loaded everywhere",
                "; ".join(f"{r['node']}: {r['detail']}" for r in failed),
                back=f"/w/{slug}/result"), status_code=502)
        return RedirectResponse("/", status_code=303)

    @app.get("/api/workspaces")
    def api_workspaces():
        return JSONResponse([
            {"slug": w.slug, "name": w.name, "pack": w.pack, "stage": w.stage,
             "report": w.report.get("headline", "")}
            for w in workspace.list_all()
        ])
