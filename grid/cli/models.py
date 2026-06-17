"""`grid models` commands: list, pull, and remove local GGUF model files."""
from __future__ import annotations

import argparse


def cmd_models_list(args: argparse.Namespace) -> int:
    from ..models import catalog, store

    stored = store.list_all()
    if not stored:
        print("(no local models - try `grid models pull <hf-repo>:<file>`)")
    else:
        for model in stored:
            print(f"{model.name:<60} {model.size_bytes / 1e9:>7.2f} GB")
    if args.catalog:
        print()
        print("Recommended catalog:")
        for entry in catalog.recommended_entries():
            print(catalog.format_catalog_entry(entry))
    return 0


def cmd_models_pull(args: argparse.Namespace) -> int:
    from ..models import catalog, download

    entry = catalog.find(args.spec)
    if entry:
        repo, filename = entry.hf_repo, entry.quantized_file
        print(f"Resolved catalog label {entry.label!r} -> {repo}/{filename}")
    else:
        repo, filename = download.parse_spec(args.spec)
    print(f"Downloading {repo}/{filename} ...")
    target = download.download(repo, filename, on_progress=download.stderr_progress)
    print(f"Saved {target}")
    return 0


def cmd_models_rm(args: argparse.Namespace) -> int:
    from ..models import store

    model = store.find(args.name)
    if not model:
        raise SystemExit(f"No such model: {args.name}")
    if not args.yes:
        response = input(f"Delete {model.path} ({model.size_bytes / 1e9:.2f} GB)? [y/N] ").strip().lower()
        if response != "y":
            print("Aborted.")
            return 1
    store.remove(args.name)
    print(f"Removed {model.path}")
    return 0


