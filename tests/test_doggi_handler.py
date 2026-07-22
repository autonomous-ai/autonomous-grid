"""Unit tests for the Doggi handler's translation logic and SSE formatting.

No network calls, no API key required. Tests the pure logic:
  - dimension → image_size mapping
  - base64 → data URI conversion
  - SSE event formatting
  - model name stripping ("doggi:hunyuan-image-3-t2i" → "hunyuan-image-3-t2i")
"""
import base64
import json

import pytest

from shared.handlers.doggi import (
    DoggiHandler,
    _dimensions_to_image_size,
    _duration_to_seconds,
    _infer_media_type,
    _sse,
    _to_video_aspect_ratio,
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
# i2v: grid's duration/aspect vocabulary → Doggi's
# ---------------------------------------------------------------------------

def test_duration_accepts_the_cli_suffixed_form():
    """`grid video --duration` yields "5s"/"8s"; the client does str(int(...)) and would raise."""
    assert _duration_to_seconds("5s") == 5
    assert _duration_to_seconds("8s") == 8
    assert _duration_to_seconds(5) == 5


def test_duration_rejects_nonsense_with_a_message_naming_the_value():
    with pytest.raises(ValueError, match="banana"):
        _duration_to_seconds("banana")


@pytest.mark.parametrize("grid_ratio,expected", [
    ("1:1", "1:1"),    # the only ratio common to both vocabularies
    ("2:3", "3:4"),    # portrait -> nearest portrait Doggi accepts
    ("3:2", "4:3"),    # landscape -> nearest landscape Doggi accepts
])
def test_cli_aspect_ratios_map_onto_doggis_video_set(grid_ratio, expected):
    """Every `grid video --aspect-ratio` choice must land on a ratio Doggi's i2v accepts."""
    assert _to_video_aspect_ratio(grid_ratio) == expected


def test_aspect_ratio_defaults_and_passthrough():
    assert _to_video_aspect_ratio(None) == "auto"
    assert _to_video_aspect_ratio("auto") == "auto"
    assert _to_video_aspect_ratio("16:9") == "16:9"  # already a Doggi ratio
    assert _to_video_aspect_ratio("nonsense") == "auto"


def test_i2v_submits_translated_duration_and_aspect(monkeypatch):
    """End-to-end through the handler: the CLI's own values reach the client already converted."""
    captured = {}

    class FakeI2v:
        def submit(self, model, **kwargs):
            captured.update(kwargs)
            return type("R", (), {"wait": lambda self, *a, **k: {
                "request_id": "req-v", "status": "completed",
                "result_files": [{"filename": "v.mp4", "content_type": "video/mp4",
                                  "file_url": "https://x/v.mp4"}]}})()

    class FakeClient:
        def __init__(self, *a, **kw):
            self.i2v = FakeI2v()

    import doggi
    monkeypatch.setattr(doggi, "DoggiClient", FakeClient)
    monkeypatch.setattr("shared.handlers.doggi._download_as_base64", lambda url: "eA==")

    handler = DoggiHandler(base_url="http://fake", api_key="fake")
    list(handler.forward({
        "model": "doggi:Wan-AI/Wan2.2-I2V-A14B-Lightning", "prompt": "pan",
        "duration": "5s", "aspect_ratio": "2:3",
        "input_image": {"filename": "in.jpeg", "content_base64": "eA=="},
    }, "media/video/i2v"))

    assert captured["duration"] == 5, "must be an int; the client does str(int(duration))"
    assert captured["aspect_ratio"] == "3:4"
    assert captured["image_url"].startswith("data:image/jpeg;base64,")


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
    # `type` is what media_io.consume_media_sse dispatches on — without it the consumer's
    # result branch never fires and nothing is written to disk.
    assert parsed["type"] == "result"


def test_sse_progress_event():
    line = _sse("progress", {"progress": 50.0, "status": "running"})
    parsed = json.loads(line.split("data: ", 1)[1].strip())
    assert parsed["type"] == "progress"
    assert parsed["progress"] == 50.0
    assert parsed["status"] == "running"


def test_handler_events_are_consumable_by_media_io(monkeypatch, tmp_path):
    """The handler's SSE must round-trip through the real consumer and land a file on disk.

    This is the end-to-end contract between `shared/handlers/doggi.py` and `cli/media_io.py`:
    a shape the consumer can't dispatch is silently a zero-file success on the engine and a
    non-zero exit with no output for the user.
    """
    from cli import media_io

    class FakeReceipt:
        def wait(self, *a, **kw):
            return {"request_id": "req-1", "status": "completed", "result_files": [
                {"filename": "bike.png", "content_type": "image/png",
                 "file_url": "https://example.com/bike.png"},
            ]}

    class FakeT2i:
        def submit(self, model, **kwargs):
            return FakeReceipt()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.t2i = FakeT2i()

    import doggi
    monkeypatch.setattr(doggi, "DoggiClient", FakeClient)
    payload = base64.b64encode(b"not-really-a-png").decode("ascii")
    monkeypatch.setattr("shared.handlers.doggi._download_as_base64", lambda url: payload)

    handler = DoggiHandler(base_url="http://fake", api_key="fake")
    body = {"model": "doggi:hunyuan-image-3-t2i", "prompt": "a red bicycle",
            "width": 720, "height": 720}

    class FakeResponse:
        """Minimal stand-in for the httpx streamed response media_io reads."""
        def __init__(self, text):
            self._text = text

        def iter_lines(self):
            return iter(self._text.splitlines())

    sse = "".join(handler.forward(body, "media/image/generate"))
    exit_code = media_io.consume_media_sse(FakeResponse(sse), tmp_path)

    assert exit_code == 0, "the consumer must accept the handler's own SSE"
    written = list(tmp_path.iterdir())
    assert [p.name for p in written] == ["bike.png"]
    assert written[0].read_bytes() == b"not-really-a-png"


def test_handler_forwards_every_result_file(monkeypatch):
    """num_images > 1 returns several files; dropping all but the first loses paid work."""
    class FakeReceipt:
        def wait(self, *a, **kw):
            return {"request_id": "req-2", "status": "completed", "result_files": [
                {"filename": "a.png", "content_type": "image/png", "file_url": "https://x/a.png"},
                {"filename": "b.png", "content_type": "image/png", "file_url": "https://x/b.png"},
            ]}

    class FakeClient:
        def __init__(self, *a, **kw):
            self.t2i = type("T", (), {"submit": lambda self, model, **kw: FakeReceipt()})()

    import doggi
    monkeypatch.setattr(doggi, "DoggiClient", FakeClient)
    monkeypatch.setattr("shared.handlers.doggi._download_as_base64", lambda url: "eA==")

    handler = DoggiHandler(base_url="http://fake", api_key="fake")
    lines = list(handler.forward({"model": "doggi:m", "prompt": "p"}, "media/image/generate"))
    result = json.loads(lines[1].split("data: ", 1)[1].strip())
    assert [f["filename"] for f in result["output_files"]] == ["a.png", "b.png"]


def test_handler_raises_when_gateway_attaches_no_files(monkeypatch):
    """A completed task with result_files=null is a gateway fault — fail loudly, naming the id."""
    class FakeReceipt:
        def wait(self, *a, **kw):
            return {"request_id": "req-3", "status": "completed", "result_files": None}

    class FakeClient:
        def __init__(self, *a, **kw):
            self.t2i = type("T", (), {"submit": lambda self, model, **kw: FakeReceipt()})()

    import doggi
    monkeypatch.setattr(doggi, "DoggiClient", FakeClient)

    handler = DoggiHandler(base_url="http://fake", api_key="fake")
    with pytest.raises(RuntimeError, match="req-3"):
        list(handler.forward({"model": "doggi:m", "prompt": "p"}, "media/image/generate"))


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
    monkeypatch.setattr("shared.handlers.doggi._download_as_base64", lambda url: "ZmFrZS1pbWFnZS1kYXRh")

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