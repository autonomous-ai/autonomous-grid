from __future__ import annotations

import pytest

from cli.remote_provider import (
    _effective_relay_transport,
    _hot_reloadable,
    _relay_transport_url,
)
from remote.serve import _provider_relay_url


PUBLIC_RELAY = "https://forge.example"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("https://relay.internal/", "https://relay.internal"),
        ("http://127.0.0.1:8090/", "http://127.0.0.1:8090"),
        ("http://localhost:8090", "http://localhost:8090"),
        (PUBLIC_RELAY + "/", None),
    ],
)
def test_relay_transport_accepts_secure_or_loopback_origins(value, expected):
    assert _relay_transport_url(value, PUBLIC_RELAY) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://10.0.0.8:8090",
        "ftp://127.0.0.1:8090",
        "https://user:secret@relay.example",
        "https://relay.example/a/path",
        "https://relay.example?target=other",
    ],
)
def test_relay_transport_rejects_credential_leaks_and_unsafe_http(value):
    with pytest.raises(SystemExit):
        _relay_transport_url(value, PUBLIC_RELAY)


def _external_record(**updates):
    record = {
        "engine_id": "remote",
        "reload_signal": "sighup",
        "signaling_url": PUBLIC_RELAY,
        "engines": [
            {
                "endpoint_url": "http://127.0.0.1:18081/v1",
                "models": ["small"],
            }
        ],
        "media": False,
        "media_bundles": [],
        "max_concurrency": 1,
    }
    record.update(updates)
    return record


def test_provider_uses_transport_without_changing_canonical_url():
    record = _external_record(relay_transport_url="http://127.0.0.1:8090/")

    assert record["signaling_url"] == PUBLIC_RELAY
    assert _provider_relay_url(record) == "http://127.0.0.1:8090"
    assert _effective_relay_transport(record) == "http://127.0.0.1:8090"


def test_changing_relay_transport_requires_provider_respawn():
    live = _external_record()
    desired = _external_record(relay_transport_url="http://127.0.0.1:8090")

    assert not _hot_reloadable([live], desired["engines"], desired)


def test_unchanged_relay_transport_remains_hot_reloadable():
    live = _external_record(relay_transport_url="http://127.0.0.1:8090")
    desired = _external_record(relay_transport_url="http://127.0.0.1:8090")

    assert _hot_reloadable([live], desired["engines"], desired)
