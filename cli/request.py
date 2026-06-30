"""`grid chat` / `grid image` / `grid edit` / `grid video`: requests through a grid."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import httpx

from local import config
from shared import paths
from local import runtime

from . import media_io


# Remote-only request-routing flags (DECISIONS D16): rejected in local mode, where the concept doesn't
# exist — the mirror of `cli/provider.py:_reject_remote_only_flags` for `grid join`.
def _reject_remote_only_flags(args: argparse.Namespace) -> None:
    used = []
    if getattr(args, "target_provider", None) is not None:
        used.append("--target-provider")
    # store_true defaults to False (not None), so this needs a truthiness check, not `is not None`.
    if getattr(args, "allow_self_provider", False):
        used.append("--allow-self-provider")
    if used:
        raise SystemExit(
            f"{', '.join(used)} only applies in remote mode. "
            "Switch with `grid mode remote` (or pass --remote)."
        )


def cmd_chat(args: argparse.Namespace) -> int:
    _reject_remote_only_flags(args)
    cfg = config.select_grid(getattr(args, "grid", None))
    try:
        resp = httpx.post(
            f"{runtime.grid_url(cfg)}/v1/chat/completions",
            json={"model": args.model, "messages": [{"role": "user", "content": args.message}]},
            timeout=args.timeout,
        )
    except httpx.RequestError as exc:
        raise SystemExit(f"Request failed: {exc}") from exc
    if getattr(args, "json", False) or resp.status_code >= 400:
        print(resp.text)
        return 0 if resp.status_code < 400 else 1
    # Default: print just the assistant message; fall back to raw on any surprise.
    try:
        print(resp.json()["choices"][0]["message"]["content"])
    except (KeyError, IndexError, ValueError):
        print(resp.text)
    return 0


def cmd_image(args: argparse.Namespace) -> int:
    _reject_remote_only_flags(args)
    return _post_media_request(
        args,
        "media/image/generate",
        {
            "prompt": args.prompt,
            "width": args.width,
            "height": args.height,
            "steps": args.steps,
        },
    )


def cmd_edit(args: argparse.Namespace) -> int:
    _reject_remote_only_flags(args)
    if len(args.input_images) > 3:
        raise SystemExit("Image editing supports at most three -i/--image values.")
    return _post_media_request(
        args,
        "media/image/edit",
        {
            "prompt": args.prompt,
            "steps": args.steps,
            "input_images": [media_io.load_media_file(path) for path in args.input_images],
        },
    )


def cmd_video(args: argparse.Namespace) -> int:
    _reject_remote_only_flags(args)
    payload = {
        "prompt": args.prompt,
        "duration": args.duration,
        "aspect_ratio": args.aspect_ratio,
        "input_image": media_io.load_media_file(args.image),
    }
    return _post_media_request(args, "media/video/i2v", payload)


def _post_media_request(args: argparse.Namespace, endpoint_path: str, payload: dict[str, Any]) -> int:
    cfg = config.select_grid(getattr(args, "grid", None))
    timeout = httpx.Timeout(float(args.timeout), read=float(args.timeout))
    url = f"{runtime.grid_url(cfg)}/v1/{endpoint_path}"
    output_dir = Path(args.output_dir).expanduser() if args.output_dir else paths.grid_home() / "outputs"
    try:
        with httpx.stream("POST", url, json=payload, timeout=timeout) as resp:
            if resp.status_code >= 400:
                print(resp.read().decode("utf-8", errors="replace"))
                return 1
            return media_io.consume_media_sse(resp, output_dir)
    except httpx.RequestError as exc:
        print(f"Media request failed: {exc}", file=sys.stderr)
        return 1
