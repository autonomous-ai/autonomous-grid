from __future__ import annotations

import os

import pytest

from remote import serve


def _route(key_file, *, endpoint="http://127.0.0.1:18081/v1"):
    return {
        "endpoint_url": endpoint,
        "models": ["smollm"],
        "allocator_host_id": "host-a",
        "allocator_api_key_file": str(key_file),
    }


def test_allocator_engine_bearer_is_loaded_from_owner_only_file(tmp_path):
    key_file = tmp_path / "engine.key"
    key_file.write_text("private-allocator-engine-key-123\n")
    key_file.chmod(0o600)

    assert serve._api_bearers({"engines": [_route(key_file)]}) == {
        "http://127.0.0.1:18081/v1": "private-allocator-engine-key-123"
    }


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner-mode check")
def test_allocator_engine_bearer_rejects_group_readable_file(tmp_path):
    key_file = tmp_path / "engine.key"
    key_file.write_text("private-allocator-engine-key-123\n")
    key_file.chmod(0o640)

    with pytest.raises(SystemExit, match="owner-only"):
        serve._api_bearers({"engines": [_route(key_file)]})


def test_allocator_engine_bearer_never_leaves_loopback(tmp_path):
    key_file = tmp_path / "engine.key"
    key_file.write_text("private-allocator-engine-key-123\n")
    key_file.chmod(0o600)

    with pytest.raises(SystemExit, match="loopback"):
        serve._api_bearers(
            {"engines": [_route(key_file, endpoint="https://engine.example/v1")]}
        )


def test_allocator_engine_bearer_rejects_symlink(tmp_path):
    key_file = tmp_path / "engine.key"
    key_file.write_text("private-allocator-engine-key-123\n")
    key_file.chmod(0o600)
    link = tmp_path / "linked.key"
    link.symlink_to(key_file)

    with pytest.raises(SystemExit, match="protected regular file"):
        serve._api_bearers({"engines": [_route(link)]})
