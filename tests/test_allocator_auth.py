from __future__ import annotations

import hashlib

import pytest

from shared.allocator.auth import (
    control_node_id,
    decode_node_token,
    engine_node_id,
    mint_node_token,
    mint_tenant_attestation,
    secure_control_transport,
    verify_node_token,
    verify_tenant_attestation,
)


def test_node_token_is_host_scoped_and_round_trips_without_exposing_operator_secret():
    token = mint_node_token(
        "operator-secret",
        "host-a",
        ttl_seconds=60,
        now=100,
        token_id="token-1",
    )
    assert "operator-secret" not in token
    assert decode_node_token(token).host_id == "host-a"
    assert verify_node_token(token, "operator-secret", "host-a", now=159).token_id == "token-1"


def test_node_token_rejects_other_hosts_wrong_signer_tampering_and_expiry():
    token = mint_node_token("operator", "host-a", ttl_seconds=60, now=100)
    with pytest.raises(ValueError, match="this host"):
        verify_node_token(token, "operator", "host-b", now=101)
    with pytest.raises(ValueError, match="signature"):
        verify_node_token(token, "different", "host-a", now=101)
    with pytest.raises(ValueError, match="signature"):
        verify_node_token(f"{token[:-1]}x", "operator", "host-a", now=101)
    with pytest.raises(ValueError, match="expired"):
        verify_node_token(token, "operator", "host-a", now=160)


def test_tenant_attestation_is_digest_bound_short_lived_and_secret_free():
    digest = hashlib.sha256(b"anonymous-principal-a").digest()
    other = hashlib.sha256(b"anonymous-principal-b").digest()
    proof = mint_tenant_attestation("operator-secret", digest, ttl_seconds=60, now=100)

    assert "operator-secret" not in proof
    verify_tenant_attestation(proof, "operator-secret", digest, now=159)
    with pytest.raises(ValueError, match="signature"):
        verify_tenant_attestation(proof, "operator-secret", other, now=101)
    with pytest.raises(ValueError, match="signature"):
        verify_tenant_attestation(proof, "wrong-secret", digest, now=101)
    with pytest.raises(ValueError, match="currently valid"):
        verify_tenant_attestation(proof, "operator-secret", digest, now=160)
    with pytest.raises(ValueError, match="no greater than"):
        mint_tenant_attestation("operator-secret", digest, ttl_seconds=301, now=100)


@pytest.mark.parametrize(
    "operator,host,ttl",
    [("", "host", 1), ("operator", "", 1), ("operator", "bad host", 1), ("operator", "host", 0)],
)
def test_node_token_rejects_invalid_mint_inputs(operator, host, ttl):
    with pytest.raises(ValueError):
        mint_node_token(operator, host, ttl_seconds=ttl, now=100)


@pytest.mark.parametrize("host", ["host/child", "host?query", "höst", "-bad", "a" * 129])
def test_node_token_rejects_host_ids_that_are_not_url_safe_ascii(host):
    with pytest.raises(ValueError, match="URL-safe ASCII"):
        mint_node_token("operator", host, now=100)


def test_managed_registry_ids_are_stable_scoped_and_url_safe():
    assert control_node_id("host-a") == control_node_id("host-a")
    assert control_node_id("host-a") != control_node_id("host-b")
    assert engine_node_id("host-a", "qwen/model.gguf") == engine_node_id(
        "host-a", "qwen/model.gguf"
    )
    assert engine_node_id("host-a", "qwen") != engine_node_id("host-b", "qwen")


def test_node_token_decode_rejects_malformed_input():
    with pytest.raises(ValueError, match="format"):
        decode_node_token("not-a-token")


@pytest.mark.parametrize(
    "url",
    ["https://grid.example", "http://127.0.0.1:8090", "http://[::1]:8090", "http://localhost"],
)
def test_secure_control_transport_accepts_tls_and_loopback(url):
    assert secure_control_transport(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.2:8090",
        "http://127.0.0.1.evil.example",
        "http://testserver:8090",
        "ftp://127.0.0.1",
        "bad",
    ],
)
def test_secure_control_transport_rejects_plaintext_lan_and_malformed_urls(url):
    assert not secure_control_transport(url)
