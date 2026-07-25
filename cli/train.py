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
    healthy = _healthy(report)
    print("Ready to train." if healthy else "Not ready — fix the NO lines above.")
    return 0 if healthy else 1


def _healthy(report: dict) -> bool:
    core = ("torch", "transformers", "trl", "peft", "datasets")
    return (
        all(report["deps"].get(m) for m in core)
        and report["endpoint"].get("ok", False)
        and report["data"].get("ok", False)
    )


def cmd_train_run(args: argparse.Namespace) -> int:
    from train.config import load_config
    from train.run import run_training

    cfg = load_config(args.config or DEFAULT_CONFIG)
    adapter_dir = run_training(cfg)
    print(f"Adapter saved: {adapter_dir}")
    if cfg.deploy.nodes:
        from train.deploy import deploy_adapter

        results = deploy_adapter(adapter_dir, cfg.deploy.nodes, cfg.deploy.adapter_name)
        _print_deploy(results)
    else:
        print("Deploy it with: grid train deploy --adapter", adapter_dir)
    return 0


def cmd_train_eval(args: argparse.Namespace) -> int:
    from train.config import load_config
    from train.evaluate import run_eval

    cfg = load_config(args.config or DEFAULT_CONFIG)
    run_dir = Path(args.run).expanduser()
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
        # The gate: prove it on held-out work first. The candidate must already be loadable
        # under `name` on the endpoint — deploy to a staging name, or run this after a plain
        # deploy and before pointing traffic at it.
        from train.evaluate import run_eval

        run_dir = Path(args.run or Path(args.adapter).expanduser().parent)
        result = run_eval(cfg, run_dir, name)
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

    from train.web import build_app

    print(f"grid train web -> http://127.0.0.1:{args.port}  (Ctrl-C to stop)")
    print("Share it on your network with --host 0.0.0.0 (anyone who can reach it can train).")
    uvicorn.run(build_app(), host=args.host, port=args.port, log_level="warning")
    return 0
