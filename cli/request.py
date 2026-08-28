"""`grid chat` / `grid image` / `grid edit` / `grid video`: requests through a grid."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import httpx

from local import config
from shared import paths
from shared.models import api_catalog
from local import runtime

from . import media_io


def reject_responses_only_model(model: str) -> None:
    """Refuse a chat request to a model whose kind cannot serve chat/completions (issue 05).

    `codex:*` models speak the vendor's `responses` endpoint and codex traffic is never
    translated (ADR 0015 D-b), so a `grid chat` against one can only ever end in a refusal —
    this one is client-side, before any network call, and says which client to use instead.
    Shared by both modes' chat handlers; data-driven from the whitelist's `endpoints`, so a
    future responses-only kind inherits it.
    """
    kind = api_catalog.responses_only_kind(model)
    if kind is None:
        return
    raise SystemExit(
        f"'{model}' is a {kind} subscription model: it serves the vendor's `responses` endpoint, "
        f"which `grid chat` (chat/completions) cannot call — {kind} traffic is never translated. "
        "Point a Codex-compatible app (Codex CLI/Desktop) at this grid instead; `grid info --env` "
        "prints the base URL and key to configure it with."
    )


def chat_content(message: str, images: list[str] | None) -> Any:
    """A chat message's ``content``: the plain string, or the multimodal parts array when
    ``--image`` was given. Shared by both modes' chat handlers so the two can't drift.

    A bare string when there are no images, rather than a one-element parts array: the array form
    is the newer spelling of the same thing, and an engine or proxy that only implements the plain
    string must not be broken by a request that had nothing multimodal about it.

    Text first, then the images, matching the order the OpenAI vision docs use — some models
    attend to the instruction differently depending on which side of the image it sits.
    """
    if not images:
        return message
    parts: list[dict[str, Any]] = [{"type": "text", "text": message}]
    parts.extend(
        {"type": "image_url", "image_url": {"url": media_io.image_data_uri(path)}}
        for path in images
    )
    return parts


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
    reject_responses_only_model(args.model)  # before any lookup or network (issue 05)
    _reject_remote_only_flags(args)
    cfg = config.select_grid(getattr(args, "grid", None))
    try:
        resp = httpx.post(
            f"{runtime.grid_url(cfg)}/v1/chat/completions",
            json={
                "model": args.model,
                "messages": [{
                    "role": "user",
                    "content": chat_content(args.message, getattr(args, "image", None)),
                }],
            },
            timeout=args.timeout,
        )
    except httpx.RequestError as exc:
        raise SystemExit(f"Request failed: {exc}") from exc
    except KeyboardInterrupt:
        # Same class of bug as the model-download and remote-chat cancel: KeyboardInterrupt during
        # a blocking read is not an `httpx.RequestError` (it is a `BaseException`, not caught by
        # the guard above), so it used to surface as a raw traceback through ssl/httpcore/httpx
        # instead of a clean cancellation — exactly when a slow model makes Ctrl-C likely.
        raise SystemExit("\nCancelled.") from None
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
            "model": args.model,
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
            "model": args.model,
            "prompt": args.prompt,
            "steps": args.steps,
            "input_images": [media_io.load_media_file(path) for path in args.input_images],
        },
    )


def cmd_video(args: argparse.Namespace) -> int:
    _reject_remote_only_flags(args)
    payload = {
        "model": args.model,
        "prompt": args.prompt,
        "duration": args.duration,
        "aspect_ratio": args.aspect_ratio,
        "input_image": media_io.load_media_file(args.image),
    }
    return _post_media_request(args, "media/video/i2v", payload)


def _post_media_request(args: argparse.Namespace, endpoint_path: str, payload: dict[str, Any]) -> int:
    # An unset flag is an absent field, not a null one. `steps` is the case that matters: the
    # provider's workflow carries the step count its model was distilled for, and it only overrides
    # that when the request actually asks for something else. Sending `"steps": null` would be a
    # request to set it, so the key has to go.
    payload = {key: value for key, value in payload.items() if value is not None}
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
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 1
