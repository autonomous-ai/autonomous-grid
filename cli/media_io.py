"""Transport-agnostic media IO for the `grid image`/`edit`/`video` clients.

Encode a local file for upload, consume the streamed media SSE, and write the returned
files to disk. These are shared by both modes — local (`cli/request.py`) talks to the grid
proxy and remote (`cli/remote_request.py`) talks to the relay, but the request body, the SSE
event shape (`progress` / `result` / `[DONE]`), and the on-disk output are identical, so the
only difference is how the request is built. Keep this module free of any local/remote routing.
"""
from __future__ import annotations

import base64
import binascii
import json
import sys
from pathlib import Path
from typing import Any

import httpx


def consume_media_sse(resp: httpx.Response, output_dir: Path) -> int:
    exit_code = 0
    saw_result = False
    wrote_any = False
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
            written = write_media_outputs(event.get("output_files") or [], output_dir)
            if written:
                wrote_any = True
            for path in written:
                print(path)
            continue
        print(json.dumps(event, sort_keys=True))
    if not wrote_any and exit_code == 0:
        # A result event that produced no files is as much a failure as no result at all — never
        # exit 0 having written nothing.
        message = "The media result contained no files." if saw_result else "No media result returned."
        print(message, file=sys.stderr)
        return 1
    return exit_code


def load_media_file(path_value: str) -> dict[str, str]:
    path = Path(path_value).expanduser()
    if not path.is_file():
        raise SystemExit(f"Input image not found: {path}")
    return {
        "filename": path.name,
        "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
    }


# Exactly what llama.cpp can decode, and no more. Its `libmtmd` links stb_image, whose compiled-in
# decoders are visible in the shipped binary as its own error strings — `bad IHDR len` (PNG),
# `bad DQT table`/`no SOI` (JPEG), `bad BMP`, `bad Image Descriptor` (GIF). There is no WebP
# decoder in there at all, so a `.webp` is refused here rather than sent: the engine does not
# report a decode failure, it simply proceeds with no image, and the model then answers about a
# picture it never received ("I can't access external links…") — a wrong answer that reads like a
# real one. Better a refusal naming the formats than a confident reply to nothing.
_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
}


def image_data_uri(path_value: str) -> str:
    """[path_value] as a ``data:`` URI for a chat request's ``image_url`` part.

    A data URI, not a path or an ``http://`` link: the engine that answers may be on another
    machine entirely, so anything it would have to fetch for itself — a local path, a URL only
    this box can reach — is not a thing it can read. The bytes travel with the request.
    """
    path = Path(path_value).expanduser()
    if not path.is_file():
        raise SystemExit(f"Image not found: {path}")
    mime = _IMAGE_MIME.get(path.suffix.lower())
    if mime is None:
        kinds = ", ".join(sorted(_IMAGE_MIME))
        raise SystemExit(f"{path.name}: not an image Grid can send. Use one of: {kinds}.")
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def write_media_outputs(output_files: list[dict[str, Any]], output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for index, item in enumerate(output_files, start=1):
        filename = Path(str(item.get("filename") or f"media_output_{index}")).name
        content_base64 = item.get("content_base64")
        if not content_base64:
            continue
        try:
            data = base64.b64decode(content_base64)
        except (binascii.Error, ValueError) as exc:
            # A malformed/truncated payload shouldn't crash with a traceback; skip it loudly.
            print(f"Skipping {filename}: invalid base64 data ({exc}).", file=sys.stderr)
            continue
        out_path = unused_path(output_dir / filename)
        out_path.write_bytes(data)
        written.append(out_path)
    return written


def unused_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 10_000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise SystemExit(f"Could not find an unused output path for {path}")
