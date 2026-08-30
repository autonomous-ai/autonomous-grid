from __future__ import annotations

import httpx
import pytest

from shared.models import download


class _Stream:
    def __init__(self, *, status: int, chunks, content_length: int):
        self.status_code = status
        self._chunks = chunks
        self.headers = {"Content-Length": str(content_length)}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self, _chunk_size):
        yield from self._chunks


def test_large_download_reconnects_and_resumes_after_stalled_stream(monkeypatch, tmp_path):
    calls: list[dict[str, str]] = []

    def first_chunks():
        yield b"abc"
        raise httpx.ReadTimeout("stalled")

    streams = iter(
        (
            _Stream(status=200, chunks=first_chunks(), content_length=6),
            _Stream(status=206, chunks=(b"def",), content_length=3),
        )
    )

    def stream(*_args, **kwargs):
        calls.append(dict(kwargs.get("headers") or {}))
        return next(streams)

    monkeypatch.setattr(download.httpx, "stream", stream)
    target = tmp_path / "model.bin"

    assert download.download("repo/model", "model.bin", out=target) == target
    assert target.read_bytes() == b"abcdef"
    assert calls == [{}, {"Range": "bytes=3-"}]
    assert not target.with_suffix(".bin.part").exists()


def test_server_ignoring_range_restarts_partial_instead_of_appending(monkeypatch, tmp_path):
    target = tmp_path / "model.bin"
    partial = target.with_suffix(".bin.part")
    partial.write_bytes(b"abc")
    calls: list[dict[str, str]] = []

    def stream(*_args, **kwargs):
        calls.append(dict(kwargs.get("headers") or {}))
        return _Stream(status=200, chunks=(b"abcdef",), content_length=6)

    monkeypatch.setattr(download.httpx, "stream", stream)

    assert download.download("repo/model", "model.bin", out=target) == target
    assert target.read_bytes() == b"abcdef"
    assert calls == [{"Range": "bytes=3-"}]


def test_download_refuses_declared_or_streamed_bytes_above_operator_bound(
    monkeypatch,
    tmp_path,
):
    target = tmp_path / "model.bin"
    monkeypatch.setattr(
        download.httpx,
        "stream",
        lambda *_args, **_kwargs: _Stream(
            status=200,
            chunks=(b"abcdef",),
            content_length=6,
        ),
    )

    with pytest.raises(SystemExit, match="above the configured 5 byte limit"):
        download.download(
            "repo/model",
            "model.bin",
            out=target,
            max_bytes=5,
        )

    assert not target.exists()
