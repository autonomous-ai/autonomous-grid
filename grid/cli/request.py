"""`grid request` commands: smoke-test chat and media requests through a network."""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from typing import Any

import httpx

from .. import config, paths, runtime


def cmd_request_chat(args: argparse.Namespace) -> int:
    cfg = config.select_network(args.network)
    try:
        resp = httpx.post(
            f"{runtime.network_url(cfg)}/v1/chat/completions",
            json={"model": args.model, "messages": [{"role": "user", "content": args.message}]},
            timeout=args.timeout,
        )
    except httpx.RequestError as exc:
        raise SystemExit(f"Request failed: {exc}") from exc
    print(resp.text)
    return 0 if resp.status_code < 400 else 1


def cmd_request_media_image_generate(args: argparse.Namespace) -> int:
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


def cmd_request_media_image_edit(args: argparse.Namespace) -> int:
    if len(args.input_images) > 3:
        raise SystemExit("Image editing supports at most three --image values.")
    return _post_media_request(
        args,
        "media/image/edit",
        {
            "prompt": args.prompt,
            "steps": args.steps,
            "input_images": [_load_media_file(path) for path in args.input_images],
        },
    )


def cmd_request_media_i2v(args: argparse.Namespace) -> int:
    payload = {
        "prompt": args.prompt,
        "duration": args.duration,
        "aspect_ratio": args.aspect_ratio,
        "input_image": _load_media_file(args.image),
    }
    return _post_media_request(args, "media/video/i2v", payload)


def _post_media_request(args: argparse.Namespace, endpoint_path: str, payload: dict[str, Any]) -> int:
    cfg = config.select_network(args.network)
    timeout = httpx.Timeout(float(args.timeout), read=float(args.timeout))
    url = f"{runtime.network_url(cfg)}/v1/{endpoint_path}"
    output_dir = Path(args.output_dir).expanduser() if args.output_dir else paths.grid_home() / "outputs"
    try:
        with httpx.stream("POST", url, json=payload, timeout=timeout) as resp:
            if resp.status_code >= 400:
                print(resp.read().decode("utf-8", errors="replace"))
                return 1
            return _consume_media_sse(resp, output_dir)
    except httpx.RequestError as exc:
        print(f"Media request failed: {exc}", file=sys.stderr)
        return 1


def _consume_media_sse(resp: httpx.Response, output_dir: Path) -> int:
    exit_code = 0
    saw_result = False
    for line in resp.iter_lines():
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            print(line)
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            print(data)
            continue
        if "error" in event:
            print(f"Error: {event['error']}", file=sys.stderr)
            exit_code = 1
            continue
        if event.get("type") == "progress":
            progress = event.get("progress")
            status = event.get("status", "running")
            print(f"progress={progress}% status={status}", file=sys.stderr)
            continue
        if event.get("type") == "result":
            saw_result = True
            written = _write_media_outputs(event.get("output_files") or [], output_dir)
            for path in written:
                print(path)
            continue
        print(json.dumps(event, sort_keys=True))
    if not saw_result and exit_code == 0:
        print("No media result returned.", file=sys.stderr)
        return 1
    return exit_code


def _load_media_file(path_value: str) -> dict[str, str]:
    path = Path(path_value).expanduser()
    if not path.is_file():
        raise SystemExit(f"Input image not found: {path}")
    return {
        "filename": path.name,
        "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
    }


def _write_media_outputs(output_files: list[dict[str, Any]], output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for index, item in enumerate(output_files, start=1):
        filename = Path(str(item.get("filename") or f"media_output_{index}")).name
        content_base64 = item.get("content_base64")
        if not content_base64:
            continue
        out_path = _unused_path(output_dir / filename)
        out_path.write_bytes(base64.b64decode(content_base64))
        written.append(out_path)
    return written


def _unused_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 10_000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise SystemExit(f"Could not find an unused output path for {path}")


