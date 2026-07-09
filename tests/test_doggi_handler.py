"""Unit tests for the Doggi handler's translation logic and SSE formatting.

No network calls, no API key required. Tests the pure logic:
  - dimension → image_size mapping
  - base64 → data URI conversion
  - SSE event formatting
  - model name stripping ("doggi:hunyuan-image-3-t2i" → "hunyuan-image-3-t2i")
"""
import base64
import json
import os
from pathlib import Path

import pytest

from remote.handlers.doggi import (
    DoggiHandler,
    _dimensions_to_image_size,
    _infer_media_type,
    _sse,
)


# ---------------------------------------------------------------------------
# Dimension → image_size mapping
# ---------------------------------------------------------------------------

def test_dimensions_to_image_size_square():
    assert _dimensions_to_image_size(720, 720) == "square_hd"


def test_dimensions_to_image_size_portrait():
    assert _dimensions_to_image_size(720, 960) == "portrait_4_3"


def test_dimensions_to_image_size_landscape():
    assert _dimensions_to_image_size(960, 720) == "landscape_4_3"


def test_dimensions_to_image_size_portrait_16_9():
    assert _dimensions_to_image_size(720, 1280) == "portrait_16_9"


def test_dimensions_to_image_size_landscape_16_9():
    assert _dimensions_to_image_size(1280, 720) == "landscape_16_9"


def test_dimensions_to_image_size_zero_returns_auto():
    assert _dimensions_to_image_size(0, 0) == "auto"
    assert _dimensions_to_image_size(0, 720) == "auto"
    assert _dimensions_to_image_size(720, 0) == "auto"


# ---------------------------------------------------------------------------
# Media type inference
# ---------------------------------------------------------------------------

def test_infer_media_type_from_extension():
    assert _infer_media_type("https://example.com/out.png", "media/image/generate") == "image/png"
    assert _infer_media_type("https://example.com/out.jpg", "media/image/generate") == "image/jpeg"
    assert _infer_media_type("https://example.com/out.mp4", "media/video/i2v") == "video/mp4"


def test_infer_media_type_fallback_to_endpoint():
    # No extension → use endpoint to guess
    assert _infer_media_type("https://example.com/out", "media/video/i2v") == "video/mp4"
    assert _infer_media_type("https://example.com/out", "media/image/generate") == "image/png"
    assert _infer_media_type("https://example.com/out", "media/image/edit") == "image/png"


# ---------------------------------------------------------------------------
# SSE formatting
# ---------------------------------------------------------------------------

def test_sse_event_format():
    line = _sse("result", {"output_files": [{"filename": "out.png"}]})
    assert line.startswith("data: ")
    assert line.endswith("\n\n")
    parsed = json.loads(line.split("data: ", 1)[1].strip())
    assert parsed["output_files"][0]["filename"] == "out.png"


def test_sse_progress_event():
    line = _sse("progress", {"progress": 50.0, "status": "running"})
    parsed = json.loads(line.split("data: ", 1)[1].strip())
    assert parsed["progress"] == 50.0
    assert parsed["status"] == "running"


# ---------------------------------------------------------------------------
# Model name stripping (via handler.forward)
# ---------------------------------------------------------------------------

def test_handler_forward_strips_doggi_prefix(monkeypatch, tmp_path):
    """Verify the handler strips "doggi:" from the model name before calling the API."""
    # Stub DoggiClient to capture the model name
    class FakeReceipt:
        def wait(self):
            return {"result_files": [{"file_url": "https://example.com/out.png"}]}

    class FakeT2i:
        def __init__(self):
            self.last_model = None
        def submit(self, model, **kwargs):
            self.last_model = model
            return FakeReceipt()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.t2i = FakeT2i()

    # DoggiClient is imported inside __init__, so patch doggi.DoggiClient
    import doggi
    monkeypatch.setattr(doggi, "DoggiClient", FakeClient)
    # Stub _download_as_base64 to avoid network calls
    monkeypatch.setattr("remote.handlers.doggi._download_as_base64", lambda url: "ZmFrZS1pbWFnZS1kYXRh")

    handler = DoggiHandler(base_url="http://fake", api_key="fake")
    body = {
        "model": "doggi:hunyuan-image-3-t2i",
        "prompt": "test",
        "width": 720,
        "height": 720,
    }
    lines = list(handler.forward(body, "media/image/generate"))
    # The handler should have called t2i.submit with the stripped model name
    assert handler.client.t2i.last_model == "hunyuan-image-3-t2i"
    # Should emit progress, result, and [DONE]
    assert len(lines) == 3
    assert '"progress"' in lines[0]
    assert '"output_files"' in lines[1]
    assert "[DONE]" in lines[2]


def test_handler_forward_missing_model_raises():
    """Verify the handler raises when model is missing."""
    handler = DoggiHandler(base_url="http://fake", api_key="fake")
    body = {"prompt": "test"}
    with pytest.raises(ValueError, match="model is required"):
        list(handler.forward(body, "media/image/generate"))


def test_handler_forward_unsupported_endpoint_raises():
    """Verify the handler raises for unsupported endpoints."""
    handler = DoggiHandler(base_url="http://fake", api_key="fake")
    body = {"model": "doggi:test", "prompt": "test"}
    with pytest.raises(ValueError, match="unsupported Doggi endpoint"):
        list(handler.forward(body, "media/unknown"))