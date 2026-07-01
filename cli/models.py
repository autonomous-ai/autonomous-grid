"""`grid catalog` / `grid pull` / `grid rm`: manage local GGUF model files."""
from __future__ import annotations

import argparse
import json


def cmd_catalog(args: argparse.Namespace) -> int:
    from shared.models import catalog, store

    if getattr(args, "json", False):
        print(json.dumps([
            {
                "label": entry.label,
                "hf_repo": entry.hf_repo,
                "file": entry.quantized_file,
                "min_vram_gb": entry.min_vram_gb,
                "kind": entry.kind,
                "target": entry.target,
            }
            for entry in catalog.recommended_entries()
        ], indent=2))
        return 0

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
    from shared.models import catalog, download

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


def cmd_ctx(args: argparse.Namespace) -> int:
    from pathlib import Path

    from shared.models import gguf, store

    candidate = Path(args.model).expanduser()
    if candidate.is_file():
        path = candidate
    else:
        model = store.find(args.model)
        if not model:
            raise SystemExit(
                f"No such model: {args.model} (not a file, and not under ~/.grid/models/)"
            )
        path = model.path

    ctx = gguf.read_context_length(path)
    if ctx is None:
        raise SystemExit(f"Could not read context length from GGUF metadata: {path}")

    if getattr(args, "json", False):
        print(json.dumps({"file": str(path), "context_length": ctx}))
    else:
        print(ctx)
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    from shared.models import store

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
