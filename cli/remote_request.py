"""Remote-mode `grid chat` / `image` / `edit` / `video`: requests through the active grid's relay.

Mirrors the local handlers (`cli/request.py`) — same verbs, same output — but routes to the hosted
relay (`{signaling_url}/relay/v1/...`) with the per-grid **access token** (Bearer) instead of the
local grid proxy, and accepts the remote-only `--target-provider` / `--allow-self-provider` routing
flags (DECISIONS D16). The media SSE consumption + file IO are the shared ones (`cli/media_io.py`).

The relay address is **live-only** (the login bundle carries the access token but not the
`signaling_url`), so each handler resolves it from `…/status` exactly like `grid join`/`start`/`info`.
A 401 is a clear "run `grid login`" — refresh-on-401 stays in the long-running serve loop (ADR 0004),
not on this one-shot path.

Import rule mirrors `cli/remote_grid.py`: `remote.*` and the remote-specific `cli` siblings are imported
lazily inside each handler, because `cli.dispatch` imports this module while the `cli` package is
still initialising. `cli.media_io` is a leaf (stdlib + httpx only), so it is safe at module top.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import httpx

from shared import paths, run_records

from . import media_io


_TOKEN_EXPIRED = "Your access token has expired. Run `grid login` to refresh, then retry."


def _resolve(args: argparse.Namespace) -> tuple[str, str, str, str]:
    """``(relay base, access token, grid label, network_id)`` for the active remote grid, or a clean
    ``SystemExit``.

    Gates in order — signed in → a grid resolves → it has an access token → it is up — mirroring the
    guard in ``cli/remote_provider.cmd_remote_join``. The relay address comes from live status (creator)
    or the login bundle (member), via ``remote_grid.resolve_relay_base``.
    """
    from remote import credentials

    from . import remote_grid

    session = credentials.require_session()
    rec = remote_grid._select(getattr(args, "grid", None))
    network_id = remote_grid._network_id(rec)
    label = rec.get("name") or network_id
    if not rec.get("access_token"):
        raise SystemExit(
            f"Grid {label} has no access token locally. Run `grid login` to refresh your grids."
        )
    base, _status = remote_grid.resolve_relay_base(session, rec, network_id, label)
    return base, str(rec["access_token"]), label, network_id




def _consumer_headers(args: argparse.Namespace) -> dict[str, str]:
    from remote import relay

    target = getattr(args, "target_provider", None)
    # The value lands in an HTTP header; a control char (e.g. a stray CR/LF) would be a header-
    # injection attempt and otherwise surfaces as an opaque httpx traceback. Reject it cleanly.
    if target is not None and any(ord(ch) < 32 for ch in target):
        raise SystemExit("--target-provider must not contain control characters.")
    return relay.consumer_headers(
        target_provider=target,
        allow_self_provider=getattr(args, "allow_self_provider", False),
    )


def cmd_remote_chat(args: argparse.Namespace) -> int:
    from remote import relay

    from .request import chat_content, reject_responses_only_model

    # Before `_resolve`: that gate starts with the sign-in check and then calls the control
    # plane, and a codex:* request must get THIS refusal — not "not signed in", not a network
    # round-trip (issue 05).
    reject_responses_only_model(args.model)
    try:
        # `_resolve` reaches the control plane before the chat request ever does (to find the
        # relay base), so the try has to start here — the first attempt at this fix wrapped only
        # the `.post()` below, and Ctrl-C during `_resolve`'s own network call still raised the
        # exact traceback this exists to prevent.
        base, token, _label, network_id = _resolve(args)
        model = run_records.own_model_case(network_id, args.model)
        body = {
            "model": model,
            "messages": [{
                "role": "user",
                "content": chat_content(args.message, getattr(args, "image", None)),
            }],
        }
        headers = _consumer_headers(args)
        with relay.open_consumer_client(base, token, timeout=args.timeout) as client:
            resp = client.post("/relay/v1/chat/completions", json=body, headers=headers)
            if resp.status_code == 401:  # before --json/raw: an expired token is never a useful "result"
                print(_TOKEN_EXPIRED, file=sys.stderr)
                return 1
            if getattr(args, "json", False) or resp.status_code >= 400:
                print(resp.text)
                return 0 if resp.status_code < 400 else 1
            # Default: print just the assistant message; fall back to raw on any surprise (mirrors local).
            try:
                print(resp.json()["choices"][0]["message"]["content"])
            except (KeyError, IndexError, ValueError):
                print(resp.text)
            return 0
    except httpx.RequestError as exc:
        raise SystemExit(f"Request failed: {exc}") from exc
    except KeyboardInterrupt:
        # Same fix as the model-download cancel: KeyboardInterrupt during a blocking socket read
        # comes up through ssl -> httpcore -> httpx uncaught by `except httpx.RequestError` (it
        # is not one — a `BaseException`, not a `RequestError`), so Ctrl-C used to surface as a
        # 30-line traceback through three libraries instead of a clean cancellation. A slow
        # reasoning model is exactly when someone reaches for Ctrl-C.
        raise SystemExit("\nCancelled.") from None


def cmd_remote_image(args: argparse.Namespace) -> int:
    return _post_media(
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


def cmd_remote_edit(args: argparse.Namespace) -> int:
    if len(args.input_images) > 3:
        raise SystemExit("Image editing supports at most three -i/--image values.")
    return _post_media(
        args,
        "media/image/edit",
        {
            "model": args.model,
            "prompt": args.prompt,
            "steps": args.steps,
            "input_images": [media_io.load_media_file(path) for path in args.input_images],
        },
    )


def cmd_remote_video(args: argparse.Namespace) -> int:
    return _post_media(
        args,
        "media/video/i2v",
        {
            "model": args.model,
            "prompt": args.prompt,
            "duration": args.duration,
            "aspect_ratio": args.aspect_ratio,
            "input_image": media_io.load_media_file(args.image),
        },
    )


def _post_media(args: argparse.Namespace, endpoint_path: str, payload: dict[str, Any]) -> int:
    from remote import relay

    try:
        # `_resolve` reaches the control plane before the media request does — see the identical
        # note in `cmd_remote_chat`. Ctrl-C during that first network call needs the same catch.
        base, token, _label, network_id = _resolve(args)
        if "model" in payload:  # same fix as `cmd_remote_chat` — see `run_records.own_model_case`
            payload["model"] = run_records.own_model_case(network_id, payload["model"])
        headers = _consumer_headers(args)
        timeout = httpx.Timeout(float(args.timeout), read=float(args.timeout))
        output_dir = Path(args.output_dir).expanduser() if args.output_dir else paths.grid_home() / "outputs"
        with (
            relay.open_consumer_client(base, token, timeout=timeout) as client,
            client.stream(
                "POST", f"/relay/v1/{endpoint_path}", json=payload, headers=headers
            ) as resp,
        ):
            if resp.status_code == 401:
                print(_TOKEN_EXPIRED, file=sys.stderr)
                return 1
            if resp.status_code >= 400:
                print(resp.read().decode("utf-8", errors="replace"))
                return 1
            return media_io.consume_media_sse(resp, output_dir)
    except httpx.RequestError as exc:
        print(f"Media request failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # Same fix as `cmd_remote_chat` — a video/image job is the slowest request this file
        # makes, so it is the one most likely to get Ctrl-C'd, and was raising the exact same
        # uncaught traceback through ssl/httpcore/httpx before this.
        print("\nCancelled.", file=sys.stderr)
        return 1
