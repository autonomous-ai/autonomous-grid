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


def cmd_train_deploy(args: argparse.Namespace) -> int:
    from train.deploy import deploy_adapter

    nodes = tuple(args.node or ())
    name = args.name
    if args.config or not (nodes and name):
        # Fill the gaps from config when present; explicit flags win.
        from train.config import load_config

        cfg = load_config(args.config or DEFAULT_CONFIG)
        nodes = nodes or cfg.deploy.nodes
        name = name or cfg.deploy.adapter_name
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
