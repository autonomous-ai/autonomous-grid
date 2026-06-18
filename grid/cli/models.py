"""`grid catalog` / `grid pull` / `grid rm`: manage local GGUF model files."""
from __future__ import annotations

import argparse


def cmd_catalog(args: argparse.Namespace) -> int:
    from ..models import catalog, store

    stored = store.list_all()
    if stored:
        print("Local models:")
        for model in stored:
            print(f"  {model.name:<58} {model.size_bytes / 1e9:>7.2f} GB")
        print()
    print("Grid can pull:")
    for entry in catalog.recommended_entries():
        print(catalog.format_catalog_entry(entry))
    print()
    print("Also: `grid pull <hf-repo>:<file>` for any GGUF on Hugging Face.")
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    from ..models import catalog, download

    entry = catalog.find(args.model)
    if entry:
        repo, filename = entry.hf_repo, entry.quantized_file
        print(f"Resolved catalog label {entry.label!r} -> {repo}/{filename}")
    else:
        repo, filename = download.parse_spec(args.model)
    print(f"Downloading {repo}/{filename} ...")
    target = download.download(repo, filename, on_progress=download.stderr_progress)
    print(f"Saved {target}")
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    from ..models import store

    model = store.find(args.model)
    if not model:
        raise SystemExit(f"No such model: {args.model}")
    if not args.yes:
        response = input(f"Delete {model.path} ({model.size_bytes / 1e9:.2f} GB)? [y/N] ").strip().lower()
        if response != "y":
            print("Aborted.")
            return 1
    store.remove(args.model)
    print(f"Removed {model.path}")
    return 0
