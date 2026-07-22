"""Vendor handler for the Doggi media API.

Translates grid media requests into Doggi API calls and yields SSE events
for the relay. Registered by remote/serve.py when the run record contains
a doggi API engine.

The consumer sends media bodies in the grid's standard format:
  - t2i: {prompt, width, height, steps}
  - i2i: {prompt, steps, input_images: [{filename, content_base64}]}
  - i2v: {prompt, duration, aspect_ratio, input_image: {filename, content_base64}}

This handler converts those to Doggi's API format, submits, polls the receipt,
downloads the result, and emits SSE events matching the ComfyUI media handler's
shape so the consumer's media_io.consume_media_sse() works unchanged.
"""
from __future__ import annotations

import base64
import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

# MIME types for result files, keyed by extension.
_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".mp4": "video/mp4",
}

# Aspect ratios (w/h) for mapping (width, height) → Doggi's image_size enum.
_VIDEO_ASPECTS = {
    "21:9": 21.0 / 9.0,
    "16:9": 16.0 / 9.0,
    "4:3": 4.0 / 3.0,
    "1:1": 1.0,
    "3:4": 3.0 / 4.0,
    "9:16": 9.0 / 16.0,
}

_IMAGE_SIZE_ASPECTS = {
    "square_hd": 1.0,
    "square": 1.0,
    "portrait_4_3": 3.0 / 4.0,
    "portrait_16_9": 9.0 / 16.0,
    "landscape_4_3": 4.0 / 3.0,
    "landscape_16_9": 16.0 / 9.0,
}


class DoggiHandler:
    """Translate grid media bodies → Doggi API → SSE events for the relay."""

    def __init__(self, base_url: str, api_key: str) -> None:
        from doggi import DoggiClient
        self.client = DoggiClient(base_url=base_url, api_key=api_key)

    def forward(self, body: dict, endpoint: str):
        """Yield SSE data lines. The caller (handle_job) submits them to the relay."""
        model = body.get("model", "").partition(":")[2]
        if not model:
            raise ValueError("model is required in body")

        if endpoint == "media/image/generate":
            receipt = self._submit_t2i(model, body)
        elif endpoint == "media/image/edit":
            receipt = self._submit_i2i(model, body)
        elif endpoint == "media/video/i2v":
            receipt = self._submit_i2v(model, body)
        else:
            raise ValueError(f"unsupported Doggi endpoint: {endpoint!r}")

        yield _sse("progress", {"progress": 0.0, "status": "submitted"})
        result = receipt.wait()
        files = result.get("result_files") or result.get("files") or []
        if not files:
            # A completed task with no files is a gateway/worker fault, not a client bug: the
            # worker never POSTed its content to `/hooks/{request_id}/webhooks/content`. Name the
            # request id so the operator can chase it upstream instead of re-running blind.
            raise RuntimeError(
                f"Doggi task {result.get('request_id', '?')} finished with status "
                f"{result.get('status', '?')!r} but attached no result files"
            )
        # Every file, not just the first: `num_images > 1` returns several and the consumer's
        # `write_media_outputs` already handles a list (dropping the rest silently loses paid work).
        output_files = []
        for item in files:
            url = item.get("file_url") or item.get("url")
            if not url:
                raise RuntimeError(f"Doggi result file has no URL: {item}")
            # The gateway states `filename` and `content_type` on every ResultFile; trust those and
            # fall back to URL/endpoint sniffing only when a field is absent.
            output_files.append({
                "filename": item.get("filename") or os.path.basename(url.split("?", 1)[0]),
                "content_base64": _download_as_base64(url),
                "media_type": item.get("content_type") or _infer_media_type(url, endpoint),
            })
        yield _sse("result", {"output_files": output_files})
        yield "data: [DONE]\n\n"

    def _submit_t2i(self, model, body):
        return self.client.t2i.submit(
            model,
            prompt=body.get("prompt", ""),
            image_size=_dimensions_to_image_size(
                body.get("width", 0), body.get("height", 0)),
            num_inference_steps=body.get("steps", 4),
        )

    def _submit_i2i(self, model, body):
        input_images = body.get("input_images", [])
        if not input_images:
            raise ValueError("input_images is required for media/image/edit")
        # Convert the first image's base64 to a data URI.
        first = input_images[0]
        content_b64 = first.get("content_base64", "")
        filename = first.get("filename", "input.png")
        ext = os.path.splitext(filename)[1].lower()
        mime = _MIME_BY_EXT.get(ext, "image/png")
        image_url = f"data:{mime};base64,{content_b64}"
        return self.client.i2i.submit(
            model,
            prompt=body.get("prompt", ""),
            image_url=image_url,
            aspect_ratio=body.get("aspect_ratio", "auto"),
            num_inference_steps=body.get("steps", 4),
        )

    def _submit_i2v(self, model, body):
        input_image = body.get("input_image", {})
        if not input_image:
            raise ValueError("input_image is required for media/video/i2v")
        content_b64 = input_image.get("content_base64", "")
        filename = input_image.get("filename", "input.png")
        ext = os.path.splitext(filename)[1].lower()
        mime = _MIME_BY_EXT.get(ext, "image/png")
        image_url = f"data:{mime};base64,{content_b64}"
        # `duration` / `aspect_ratio` arrive in the GRID's vocabulary (`--duration 5s`,
        # `--aspect-ratio 2:3`), which is the built-in ComfyUI engine's. Doggi wants an integer
        # count of seconds and its own ratio set, so translate here — untranslated, `duration="5s"`
        # is a ValueError inside the client and `2:3`/`3:2` are not ratios its i2v accepts.
        return self.client.i2v.submit(
            model,
            image_url=image_url,
            prompt=body.get("prompt", ""),
            duration=_duration_to_seconds(body.get("duration", 5)),
            aspect_ratio=_to_video_aspect_ratio(body.get("aspect_ratio")),
        )


# --- helpers ---------------------------------------------------------------

def _dimensions_to_image_size(w, h):
    """Convert (width, height) to the closest Doggi image_size enum. (0,0) → auto."""
    if not w or not h:
        return "auto"
    target = float(w) / float(h)
    best, best_dist = "auto", None
    for name, ratio in _IMAGE_SIZE_ASPECTS.items():
        d = abs(ratio - target)
        if best_dist is None or d < best_dist:
            best, best_dist = name, d
    return best


def _duration_to_seconds(value):
    """Grid's ``--duration`` ("5s" / "8s") -> the integer seconds Doggi expects.

    The client does ``str(int(duration))``, so an unconverted "5s" raises deep inside the payload
    builder rather than here, where the message can name the offending value.
    """
    if isinstance(value, bool):  # bool is an int subclass; a bool duration is a caller bug
        raise ValueError(f"invalid duration {value!r}: expected seconds, e.g. '5s'")
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower().removesuffix("s")
    try:
        return int(text)
    except ValueError:
        raise ValueError(f"invalid duration {value!r}: expected seconds, e.g. '5s'") from None


def _to_video_aspect_ratio(value):
    """Grid's ``--aspect-ratio`` -> the nearest ratio Doggi's i2v accepts.

    Grid offers the built-in engine's set (2:3 / 3:2 / 1:1); Doggi's video set is
    ``auto, 21:9, 16:9, 4:3, 1:1, 3:4, 9:16``. Only 1:1 is common to both, so 2:3 and 3:2 are
    snapped to the closest supported ratio (the same nearest-match rule as
    `_dimensions_to_image_size`) instead of being passed through and rejected.
    """
    if not value or value == "auto":
        return "auto"
    if value in _VIDEO_ASPECTS:
        return value
    try:
        width, height = (float(part) for part in str(value).split(":", 1))
        target = width / height
    except (ValueError, ZeroDivisionError):
        return "auto"
    return min(_VIDEO_ASPECTS, key=lambda name: abs(_VIDEO_ASPECTS[name] - target))


def _download_as_base64(url):
    """Download a file from a URL and return its base64-encoded contents."""
    resp = httpx.get(url, timeout=300)
    resp.raise_for_status()
    return base64.b64encode(resp.content).decode("ascii")


def _infer_media_type(url, endpoint):
    """Guess the MIME type from the result URL's extension, with endpoint fallback."""
    ext = os.path.splitext(url.split("?", 1)[0])[1].lower()
    if ext in _MIME_BY_EXT:
        return _MIME_BY_EXT[ext]
    return "video/mp4" if endpoint == "media/video/i2v" else "image/png"


def _sse(event_type, payload):
    """Format a payload as an SSE data line.

    ``type`` is what the consumer dispatches on (`cli/media_io.consume_media_sse`): an event
    without it falls through to the raw-print branch, so a `result` would never be written to
    disk. It is set here rather than by each caller so no future event can omit it.
    """
    return f"data: {json.dumps({'type': event_type, **payload})}\n\n"