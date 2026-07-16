"""`grid catalog` / `grid pull` / `grid rm`: manage local GGUF model files,
plus the static API-engine whitelist (`grid catalog --api <kind>`)."""
from __future__ import annotations

import argparse
import json


def cmd_catalog(args: argparse.Namespace) -> int:
    # `is not None`, not truthiness: `--api ""` must reach the unknown-kind error,
    # not silently fall through to the GGUF catalog.
    if getattr(args, "api", None) is not None:
        return _catalog_api(args)

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


def _catalog_api(args: argparse.Namespace) -> int:
    from shared.models import api_catalog

    kind = args.api
    whitelist = api_catalog.WHITELISTS.get(kind)
    # "Is this kind known?" is dict membership, and ONLY dict membership — the same question
    # `_reject_api_conflicts` asks, answered the same way. This used to lean on "has entries" as a
    # proxy, which held only while every row had some; a credential-only row (ADR 0015 D-c: codex
    # ships its credential path before issue 05's per-tier model table) made the proxy disagree with
    # the real predicate and print "Unknown API kind 'codex'. Supported: codex, openai".
    if whitelist is None:
        supported = ", ".join(api_catalog.supported_kinds())
        raise SystemExit(f"Unknown API kind {kind!r}. Supported: {supported}")

    if getattr(args, "json", False):
        # Explicit keys: this is the stable machine-readable contract (ADR 0012);
        # renaming an ApiModelEntry field must not silently rename a JSON key.
        print(json.dumps({
            "kind": kind,
            "last_verified": whitelist.last_verified,
            "models": [
                {
                    "advertised": api_catalog.advertised_name(kind, entry),
                    "vendor_name": entry.vendor_name,
                    "context_window": entry.context_window,
                    "supports_tools": entry.supports_tools,
                    "supports_vision": entry.supports_vision,
                    "supports_json_mode": entry.supports_json_mode,
                    "supports_structured_outputs": entry.supports_structured_outputs,
                    "notes": entry.notes,
                }
                for entry in whitelist.entries
            ],
        }, indent=2))
        return 0

    if not whitelist.entries:
        # A known kind with no models listed yet — not an error, and emphatically not "unknown".
        # `grid catalog` answered the question it was asked (ADR 0012 D-a: no credential, no network);
        # the answer happens to be "none". Exit 0, like `--json`'s `models: []`.
        print(f"No models are listed for `{kind}` in this version of grid yet.")
        print(
            f"`grid join --api {kind}` will still sign you in, but there is nothing for it to serve "
            "until a release carries the model list."
        )
        return 0

    print(
        f"Models a `grid join --api {kind}` would serve "
        f"(verified {whitelist.last_verified}):"
    )
    for entry in whitelist.entries:
        print(api_catalog.format_api_entry(kind, entry))
    print()
    print(f"No key needed to view. Requests to {kind}:* models leave the grid for the vendor.")
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
