"""`grid train`: RL fine-tuning on the machines you already run (ADR 0019).

Verbs:
  init             write a starter grid-train.toml (or install a task pack)
  packs            list the bundled task packs
  doctor           readiness check — deps, rollout endpoint, data/rewards (no training)
  run              the climb: GRPO via TRL, rollouts served by the grid
  serve            run THIS Mac as a rollout node (MLX; serves the training contract)
  convert-adapter  move a LoRA adapter between torch/peft and MLX formats
  deploy           hot-load a trained adapter onto serving nodes
  ui               local read-only dashboard of runs and their curves
  eval             score a trained model against the one you serve (the gate)
  web              the point-and-click interface for non-engineers
"""
from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path

DEFAULT_CONFIG = "grid-train.toml"


def cmd_train_init(args: argparse.Namespace) -> int:
    if getattr(args, "pack", None):
        from train.packs import install_pack

        dest = Path(args.dest or args.pack)
        installed = install_pack(args.pack, dest)
        print(f"Installed pack {args.pack!r} -> {dest}/ ({len(installed)} files)")
        print(f"Next: cd {dest} && cat README.md")
        return 0

    from train.config import SAMPLE

    path = Path(args.config or DEFAULT_CONFIG)
    if path.exists() and not args.force:
        raise SystemExit(f"grid train: {path} exists (pass --force to overwrite)")
    path.write_text(SAMPLE, encoding="utf-8")
    print(f"Wrote {path}")
    print("Edit it (model, rollout endpoint, data, rewards), then: grid train doctor")
    return 0


def cmd_train_packs(args: argparse.Namespace) -> int:
    from train.packs import available_packs

    packs = available_packs()
    if getattr(args, "json", False):
        print(json.dumps(packs, indent=2))
        return 0
    print("Task packs (install with `grid train init --pack <name>`):")
    for name, description in packs.items():
        print(f"  {name:<18} {description}")
    return 0


def cmd_train_doctor(args: argparse.Namespace) -> int:
    from train.config import load_config
    from train.run import doctor

    report = doctor(load_config(args.config or DEFAULT_CONFIG))
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2))
        return 0 if _healthy(report) else 1

    print("Dependencies")
    for name, version in report["deps"].items():
        mark = "ok " if version else "-- "
        note = version or ("optional" if name == "verifiers" else "missing — pip install 'grid[train]'")
        print(f"  {mark}{name:<12} {note}")
    endpoint = report["endpoint"]
    print("Rollout endpoint")
    print(f"  {'ok ' if endpoint['ok'] else 'NO '}{endpoint['model']}  ·  {endpoint['detail']}")
    data = report["data"]
    print("Data & rewards")
    if data.get("ok"):
        print(f"  ok {data['prompts']} prompts  ·  {data['reward_funcs']} reward function(s)")
    else:
        print(f"  NO {data.get('detail')}")
    # Two rungs, two verdicts. Reporting only the feedback rung's needs told a Mac owner
    # "Not ready" on the exact machine where the browser and the nightly cycle train fine — the
    # imitation rung needs no engine at all.
    trainer = report.get("trainer") or {}
    print("Trainer")
    print(f"  {'ok ' if trainer.get('ok') else 'NO '}{trainer.get('detail', 'unknown')}")
    imitation_ok, imitation_note = _imitation_ready(report)
    # The feedback rung needs BOTH halves: an engine to sample from and a trainer to learn with.
    # Reporting only the engine told a Mac with `grid train serve` running that it was ready for
    # a run whose trainer is a CUDA job, and the failure surfaced 20 minutes later as a model
    # that never finished loading.
    feedback_ok = _healthy(report) and trainer.get("ok", False)
    if feedback_ok:
        feedback_note = "ready"
    elif not _healthy(report):
        feedback_note = ("needs an engine that serves the training contract "
                         "— `grid train serve` on a Mac, or vLLM")
    else:
        feedback_note = "the engine is ready; the trainer above is not"
    print("What this computer can do now")
    print(f"  {'ok ' if imitation_ok else 'NO '}learn from answers your team already wrote "
          f"(`grid train sft`)  ·  {imitation_note}")
    print(f"  {'ok ' if feedback_ok else 'NO '}learn from feedback (`grid train run`)  ·  "
          f"{feedback_note}")
    if feedback_ok:
        print("Ready to train, both rungs.")
    elif imitation_ok:
        print("Ready for stage one. `grid train sft` works on this computer right now.")
    else:
        print("Not ready — fix the NO lines above.")
    return 0 if (imitation_ok or feedback_ok) else 1


def _imitation_ready(report: dict) -> tuple[bool, str]:
    """Can stage one run here? It needs a trainer and data — no serving engine.

    On Apple Silicon that is mlx-lm; everywhere else it is torch + trl. Data is the same
    requirement either way.
    """
    from train.sft import pick_backend

    if not report["data"].get("ok"):
        return False, str(report["data"].get("detail", "no examples yet"))
    backend = pick_backend("auto")
    if backend == "mlx":
        return True, "MLX on this Mac's own chip"
    if all(report["deps"].get(m) for m in ("torch", "transformers", "trl", "peft")):
        return True, "torch on this machine"
    return False, "missing the trainer — pip install 'grid[train]'"


def _healthy(report: dict) -> bool:
    core = ("torch", "transformers", "trl", "peft", "datasets")
    return (
        all(report["deps"].get(m) for m in core)
        and report["endpoint"].get("ok", False)
        and report["data"].get("ok", False)
    )


def cmd_train_run(args: argparse.Namespace) -> int:
    from train.config import load_config
    from train.sft import pick_backend

    cfg = load_config(args.config or DEFAULT_CONFIG)
    backend = pick_backend(getattr(args, "backend", "auto"))
    steps = getattr(args, "steps", None)
    if backend == "mlx":
        # In-process sampling: on one Mac that is the fast path, and the rollout contract exists
        # for spreading a group across machines, which is the torch path's job.
        from train.mlx.grpo import run_grpo

        print("Learning from feedback on mlx — sampling and updating on this Mac.")
        adapter_dir = run_grpo(cfg, steps=steps)
    else:
        from train.run import run_training

        adapter_dir = run_training(cfg)
    print(f"Adapter saved: {adapter_dir}")
    # Training NEVER serves the result on its own. It used to: whenever [deploy].nodes was set,
    # a finished run pushed the fresh adapter straight onto the serving machines — which made the
    # product's central promise ("nothing reaches anyone until it beats what you have") false, and
    # made the browser's own reassuring sentence a lie. Proving it is a separate, deliberate act.
    print("\nIt has not been served to anyone yet — prove it first:")
    print(f"  grid train deploy --gate --adapter {adapter_dir}")
    print("That scores it against the model you're serving on work it never trained on, and only "
          "loads it if it won.")
    return 0


def cmd_train_eval(args: argparse.Namespace) -> int:
    """The gate. Given an adapter on disk, it is loaded for checking — never scored by name.

    Asking the endpoint for the candidate's *serving name* scores whatever weights it already
    holds, which is the incumbent (or nothing at all on the first run). That was live here and in
    the browser's Check button after the same bug was fixed in the unattended paths.
    """
    from train.config import load_config
    from train.evaluate import CandidateNotLoaded, prove_candidate, run_eval

    cfg = load_config(args.config or DEFAULT_CONFIG)
    run_dir = Path(args.run).expanduser()
    adapter = Path(args.adapter).expanduser() if args.adapter else run_dir / "adapter"
    if adapter.is_dir():
        try:
            result = prove_candidate(cfg, run_dir, adapter, args.candidate)
        except CandidateNotLoaded as exc:
            raise SystemExit(f"grid train: {exc}") from None
    else:
        # No adapter on disk: the caller is scoring something the engine already serves by name.
        result = run_eval(cfg, run_dir, args.candidate, base_model=args.base)
    _print_eval(result, run_dir)
    return 0 if result["passed"] else 1


def _print_eval(result: dict, run_dir: Path) -> None:
    before, after = result["before"], result["after"]
    width = max((len(n) for n in after["per_grader"]), default=6)
    print(f"Held-out work: {after['n']} items it never trained on\n")
    print(f"  {'check':<{width}}  {'serving':>8}  {'trained':>8}  {'change':>8}")
    for name in sorted(after["per_grader"]):
        b = before["per_grader"].get(name, 0.0)
        a = after["per_grader"][name]
        print(f"  {name:<{width}}  {b:>8.3f}  {a:>8.3f}  {a - b:>+8.3f}")
    print(f"  {'overall':<{width}}  {before['overall']:>8.3f}  {after['overall']:>8.3f}  "
          f"{result['delta']:>+8.3f}")
    print(f"\n{'PASS' if result['passed'] else 'HOLD'} — {result['verdict']}")
    print(f"Card: {run_dir / 'eval-card.html'}")


def cmd_train_deploy(args: argparse.Namespace) -> int:
    from train.config import load_config
    from train.deploy import deploy_adapter

    nodes = tuple(args.node or ())
    name = args.name
    cfg = None
    if args.config or args.gate or not (nodes and name):
        # Fill the gaps from config when present; explicit flags win.
        cfg = load_config(args.config or DEFAULT_CONFIG)
        nodes = nodes or cfg.deploy.nodes
        name = name or cfg.deploy.adapter_name

    if args.gate:
        # The gate: prove it on held-out work first — the ADAPTER on disk, loaded for checking.
        # This used to score `name`, which is whatever the endpoint already holds under that name:
        # the incumbent, or nothing at all. It was the last caller still doing that after the
        # unattended paths and `grid train eval` were fixed.
        from train.evaluate import CandidateNotLoaded, prove_candidate

        run_dir = Path(args.run or Path(args.adapter).expanduser().parent)
        try:
            result = prove_candidate(cfg, run_dir, Path(args.adapter).expanduser(), name,
                                     nodes=nodes)
        except CandidateNotLoaded as exc:
            raise SystemExit(f"grid train: {exc}") from None
        _print_eval(result, run_dir)
        if not result["passed"]:
            print("\nRefusing to deploy: it did not beat the model you are already serving.")
            return 1
        print()

    results = deploy_adapter(args.adapter, nodes, name)
    _print_deploy(results)
    return 0 if all(r["ok"] for r in results) else 1


def _print_deploy(results: list[dict]) -> None:
    for r in results:
        print(f"  {'ok ' if r['ok'] else 'NO '}{r['node']}  ·  {r['detail']}")


def cmd_train_collect(args: argparse.Namespace) -> int:
    """Turn on (or off) learning from the work the grid is already doing."""
    from train.capture import Policy, load_policy, prune, save_policy, summarize

    policy = load_policy()
    changing = (args.on or args.off or args.teacher or args.retain_days
                or args.sample is not None or args.no_redact)
    if changing:
        policy = Policy(
            enabled=True if args.on else (False if args.off else policy.enabled),
            sample=args.sample if args.sample is not None else policy.sample,
            retain_days=args.retain_days or policy.retain_days,
            redact=False if args.no_redact else policy.redact,
            teachers=tuple(args.teacher) if args.teacher else policy.teachers,
        )
        save_policy(policy)
        print("Collecting is ON." if policy.enabled else "Collecting is OFF.")
        if policy.enabled:
            print(f"  kept for {policy.retain_days} days on this machine, "
                  f"{'redacted' if policy.redact else 'NOT redacted'}, "
                  f"{int(policy.sample * 100)}% of requests")
            if policy.teachers:
                print(f"  answers from {', '.join(policy.teachers)} count as teaching examples")
        print()

    if args.prune:
        print(f"Removed {prune(policy)} day file(s) past the retention window.")

    summary = summarize(days=args.days)
    if getattr(args, "json", False):
        print(json.dumps({**dataclasses.asdict(summary), "headline": summary.headline,
                          "advice": summary.advice}, indent=2))
        return 0
    print(summary.headline)
    print(f"  {summary.advice}")
    if summary.requests:
        print(f"\n  requests kept   {summary.requests}")
        print(f"  with a verdict  {summary.judged}"
              f"  (sent as-is {summary.accepted} · edited {summary.edited} · "
              f"discarded {summary.rejected})")
        print(f"  from a teacher  {summary.teacher_rows}")
        print(f"  ready to learn  {summary.trainable}")
        if summary.models:
            top = sorted(summary.models.items(), key=lambda kv: -kv[1])[:4]
            print("  models          " + ", ".join(f"{m} ({n})" for m, n in top))
    return 0


def cmd_train_autopilot(args: argparse.Namespace) -> int:
    """One unattended cycle over captured work: build tonight's data, train, prove, ship or bin."""
    from train.autopilot import history, run_cycle
    from train.config import load_config

    cfg = load_config(args.config or DEFAULT_CONFIG)
    if args.history:
        rows = history(cfg)
        if not rows:
            print("No autopilot cycles recorded yet.")
            return 0
        for row in rows:
            mark = "ok " if row["ok"] else "-- "
            delta = f"{row['delta']:+.3f}" if row.get("delta") is not None else "      "
            print(f"  {mark}{row['started'][:16].replace('T', ' ')}  {delta}  "
                  f"{row['examples']:>5} ex  {row['stage']:<9} {row['detail']}")
        return 0

    result = run_cycle(cfg, days=args.days, min_examples=args.min_examples,
                       check_host=not args.ignore_host, deploy=not args.no_deploy,
                       stage=args.stage)
    print(f"{result.stage.upper()}: {result.detail}")
    if result.adapter:
        print(f"Adapter: {result.adapter}")
    return 0 if result.ok else 1


def cmd_train_pull(args: argparse.Namespace) -> int:
    """Pull examples out of a helpdesk or CRM, into the same file shape an export would give.

    For whoever administers the tool. The token comes from the environment and is never written
    down; what lands on disk is rows, which then go through the same preparation and the same
    honest refusals as an uploaded file.
    """
    from train.connectors import PULLERS, ConnectorError, write_jsonl

    out = Path(args.out) if args.out else Path(f"{args.source}-export.jsonl")
    try:
        if args.source == "zendesk":
            if not args.subdomain or not args.email:
                raise SystemExit("grid train: zendesk needs --subdomain and --email")
            pulled = PULLERS["zendesk"](args.subdomain, args.email, max_rows=args.max_rows,
                                        status=args.status)
        else:
            pulled = PULLERS[args.source](max_rows=args.max_rows)
    except ConnectorError as exc:
        raise SystemExit(f"grid train: {exc}") from None

    if not pulled.rows:
        # Checked BEFORE writing: an empty pull that overwrites last month's good export is a
        # worse outcome than no pull at all.
        print(pulled.detail)
        print("Nothing usable came back, so nothing was written. Check the account has the "
              "history you expect, and that the token can read it.")
        return 1
    write_jsonl(pulled, out)
    print(f"{pulled.detail}\nWrote {out}")
    if pulled.trustworthy:
        print(f"Take care: {pulled.trustworthy}.")
    if pulled.fetched >= args.max_rows:
        # Gated on what the vendor handed us: tickets with no public reply are dropped after the
        # cap is counted, so len(rows) is nearly always below it and the hint never printed.
        print(f"Stopped at {args.max_rows} rows — raise --max-rows for more history.")
    print("Next: upload it in `grid train web`, or point [data] at it and run `grid train sft`.")
    return 0


def cmd_train_outcomes(args: argparse.Namespace) -> int:
    """Ask the system of record what happened to the answers we served, and write it down.

    This is the station that makes the loop continuous without asking anyone to click anything: the
    helpdesk already holds the reply that was actually sent and whether the ticket stayed solved.
    """
    from train.connectors import ConnectorError
    from train.outcomes import join, refs_needed, zendesk_records

    if args.source == "zendesk" and not (args.subdomain and args.email):
        raise SystemExit("grid train: zendesk needs --subdomain and --email")

    refs = refs_needed(args.source, days=args.days)
    if not refs:
        print("Nothing to join: no captured answer in that window carries a reference we trust.")
        print("Have your app send `X-Grid-Ref: zendesk:<ticket id>` with each request, or make "
              "sure the ticket number is in the text you send.")
        return 0
    try:
        records = zendesk_records(args.subdomain, args.email, refs)
    except ConnectorError as exc:
        raise SystemExit(f"grid train: {exc}") from None

    result = join(args.source, records, days=args.days, dry_run=args.dry_run)
    print(result.summary())
    if args.dry_run:
        print("(dry run — nothing was recorded)")
    elif result.written:
        print("Those count as examples tonight: a reply a person rewrote is ground truth, one sent "
              "as we wrote it is weaker, and one never sent is never imitated.")
    return 0


def cmd_train_schedule(args: argparse.Namespace) -> int:
    """Put the nightly cycle in this computer's own scheduler — or take it out.

    `grid train autopilot` is written for cron, but asking a support manager to edit a crontab is
    the same as telling her the model will not improve. This installs a per-user job instead.
    """
    from train import schedule as sched

    workspace = Path.cwd()
    config = Path(args.config).expanduser() if args.config else None
    slug = args.name or workspace.name
    try:
        hour, minute = (int(part) for part in str(args.at).split(":", 1))
    except ValueError:
        raise SystemExit(f"grid train: --at wants HH:MM, got {args.at!r}") from None

    if args.action == "status":
        state = sched.status(slug=slug, workspace=workspace)
        if state["installed"]:
            print(f"On — {state['mechanism']} runs it every night at {state['when']}.")
            print(f"  {state['where']}")
            if not state.get("mine", True):
                # Do NOT then tell them to turn it off from here: that used to delete another
                # folder's job and report success.
                print(f"  Careful: that job runs in {state.get('workspace')}, not here. "
                      "Turn it off from that folder, or use --name to schedule this one too.")
            else:
                print("  Turn it off with `grid train schedule off`.")
        else:
            print("Off — nothing is scheduled for this model on this computer.")
            print(sched.describe(workspace, slug=slug, hour=hour, minute=minute, config=config))
            print("Turn it on with `grid train schedule on`.")
        return 0

    if args.action == "off":
        result = sched.remove(slug=slug, workspace=workspace)
        print(result.detail)
        return 0 if result.ok else 1

    print(sched.describe(workspace, slug=slug, hour=hour, minute=minute, config=config))
    result = sched.install(workspace, slug=slug, hour=hour, minute=minute, config=config)
    print(result.detail)
    if result.ok:
        print("It only trains when this computer is on mains power and not in use, and it only "
              "serves a new model if it beats the one you have.")
    return 0 if result.ok else 1


def cmd_train_sft(args: argparse.Namespace) -> int:
    """Stage one: imitate the answers your team already wrote. Runs on a Mac with no GPU rig."""
    from train.config import load_config
    from train.sft import pick_backend, run_sft

    cfg = load_config(args.config or DEFAULT_CONFIG)
    backend = pick_backend(args.backend)
    print(f"Learning from your examples on {backend} — this is the imitation stage.")
    adapter = run_sft(cfg, backend=args.backend, iters=args.iters, run_dir=args.run_dir)
    print(f"Adapter saved: {adapter}")
    print("Next: `grid train run` sharpens it with feedback, or `grid train eval` scores it now.")
    return 0


def cmd_train_nightly(args: argparse.Namespace) -> int:
    """One unattended cycle: train, prove it on held-out work, ship it only if it won."""
    from train.config import load_config
    from train.nightly import history, run_cycle

    cfg = load_config(args.config or DEFAULT_CONFIG)
    if args.history:
        rows = history(cfg)
        if not rows:
            print("No nights recorded yet.")
            return 0
        for row in rows:
            mark = "ok " if row["ok"] else "-- "
            delta = f"{row['delta']:+.3f}" if row.get("delta") is not None else "     "
            print(f"  {mark}{row['started'][:16].replace('T', ' ')}  {delta}  "
                  f"{row['stage']:<9} {row['detail']}")
        return 0

    result = run_cycle(cfg, check_host=not args.ignore_host, deploy=not args.no_deploy)
    print(f"{result.stage.upper()}: {result.detail}")
    if result.adapter:
        print(f"Adapter: {result.adapter}")
    return 0 if result.ok else 1


def cmd_train_where(args: argparse.Namespace) -> int:
    """Show the grids training can use, in either mode, and whether weights can reach the nodes."""
    from train.endpoints import resolve
    from train.hostsignals import summary

    endpoints = resolve()
    if not endpoints:
        print("No grid found. `grid up` starts one for this network; `grid login` connects to "
              "your hosted grid.")
        return 1
    for endpoint in endpoints:
        print(f"{endpoint.mode:<7} {endpoint.label}")
        print(f"        {endpoint.base_url}")
        print(f"        adapter push: {'yes' if endpoint.can_push_adapters else 'NOT through the relay'}")
        print(f"        {endpoint.note}")
    host = summary()
    print()
    print(f"this machine: {host['power']} · {host['activity']}")
    return 0


def cmd_train_convert(args: argparse.Namespace) -> int:
    from train.adapters import convert, detect_format

    source_format = detect_format(Path(args.source).expanduser())
    written = convert(args.source, args.dest, to=args.to)
    print(f"Converted {source_format} -> {written}: {args.dest}")
    if written == "mlx":
        print("Load it on a Mac rollout node: POST /reload_adapter {\"adapter_dir\": \"<dest>\"}")
    return 0


def cmd_train_serve(args: argparse.Namespace) -> int:
    """Run the MLX rollout engine (Apple Silicon) — serves the training rollout contract."""
    import uvicorn

    from train.mlx.rollout_server import MlxEngine, build_app

    print(f"Loading {args.model} …")
    engine = MlxEngine(args.model, adapter_path=args.adapter_path)
    print(f"grid train serve -> http://{args.host}:{args.port}/v1  (model: {args.model})")
    uvicorn.run(build_app(engine), host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_train_ui(args: argparse.Namespace) -> int:
    import uvicorn

    from train.ui import build_app

    print(f"grid train ui -> http://127.0.0.1:{args.port}  (Ctrl-C to stop)")
    uvicorn.run(build_app(), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def cmd_train_web(args: argparse.Namespace) -> int:
    """The interface for people who don't use a terminal: upload examples, pick what good looks
    like, train, and see the before/after before anything is served."""
    import uvicorn

    from train.web import access, build_app

    # Off loopback this page is on the office network, showing real tickets and able to start jobs
    # on this machine. A token is the difference between "the person you sent the link to" and
    # "anyone on the wifi".
    token = access.resolve_token(args.host)
    for line in access.share_lines(args.host, args.port, token):
        print(line)
    uvicorn.run(build_app(token), host=args.host, port=args.port, log_level="warning")
    return 0
