"""A signed leaf must be a leaf: its subject is the server's, its issuer is the CA's.

LibreSSL 3.3.6 — stock `/usr/bin/openssl` on macOS — takes the DN from a `prompt = no` config and
ignores `-subj`, so the server leaf came out carrying the CA's own subject. `curl --cacert` and
`openssl verify -CAfile` both still pass on that certificate; Python's `ssl` reads subject ==
issuer as an untrusted self-signed root and fails, which reached an operator as an allocator
readiness timeout two minutes and three layers away. These tests hold the two guards that make
that impossible to ship again: the generator verifies its own output, and the reuse path throws
away a leaf already broken on disk instead of serving it forever.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from shared import tls


def _dn(crt: Path) -> tuple[str, str]:
    reported = tls._run(tls._find_openssl(), "x509", "-noout", "-subject", "-issuer", "-in", str(crt))
    return (
        tls._reported_common_name(reported, "subject"),
        tls._reported_common_name(reported, "issuer"),
    )


def test_reported_common_name_reads_both_openssl_dialects() -> None:
    libressl = "subject= /CN=autonomous-grid\nissuer= /CN=autonomous-grid local CA\n"
    openssl3 = "subject=CN = autonomous-grid\nissuer=CN = autonomous-grid local CA\n"
    for text in (libressl, openssl3):
        assert tls._reported_common_name(text, "subject") == tls.SERVER_COMMON_NAME
        assert tls._reported_common_name(text, "issuer") == tls.CA_COMMON_NAME


def test_generated_leaf_subject_differs_from_its_issuer(tmp_path: Path) -> None:
    crt, _key, ca = tls.ensure_server_cert(tmp_path / "tls", ["192.0.2.10"])

    subject, issuer = _dn(crt)
    assert subject == tls.SERVER_COMMON_NAME
    assert issuer == tls.CA_COMMON_NAME
    assert subject != issuer
    assert _dn(ca)[0] == tls.CA_COMMON_NAME


def test_a_healthy_pair_is_reused_untouched(tmp_path: Path) -> None:
    directory = tmp_path / "tls"
    crt, key, _ca = tls.ensure_server_cert(directory, ["192.0.2.10"])
    before = (crt.read_bytes(), key.read_bytes())

    tls.ensure_server_cert(directory, ["192.0.2.10"])

    assert (crt.read_bytes(), key.read_bytes()) == before


def test_reuse_discards_a_leaf_whose_subject_is_the_ca(tmp_path: Path) -> None:
    directory = tmp_path / "tls"
    crt, key, ca_crt = tls.ensure_server_cert(directory, ["192.0.2.10"])
    tool = tls._find_openssl()

    # Mint what the pre-fix code produced on LibreSSL: a CA-signed leaf whose own CN is the CA's,
    # left in place with the SAN record the reuse path trusts.
    conf = tmp_path / "ca-dn.cnf"
    conf.write_text(tls._config_text(tls.CA_COMMON_NAME), encoding="utf-8")
    csr = tmp_path / "broken.csr"
    crt.unlink()
    key.unlink()
    tls._run(tool, "req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
             "-out", str(csr), "-config", str(conf))
    tls._run(tool, "x509", "-req", "-in", str(csr), "-CA", str(ca_crt),
             "-CAkey", str(directory / "ca.key"), "-CAcreateserial", "-out", str(crt),
             "-days", str(tls.CERT_DAYS))
    assert _dn(crt) == (tls.CA_COMMON_NAME, tls.CA_COMMON_NAME)
    broken = crt.read_bytes()

    # The SAN set is unchanged, so only the identity check can save this host.
    fixed, key_after, _ca = tls.ensure_server_cert(directory, ["192.0.2.10"])

    assert fixed.read_bytes() != broken
    assert _dn(fixed) == (tls.SERVER_COMMON_NAME, tls.CA_COMMON_NAME)
    assert key_after.stat().st_mode & 0o777 == 0o600
    assert ca_crt.read_bytes()  # the CA itself is reused, never re-minted


def test_signing_that_returns_the_ca_subject_is_refused_loudly(monkeypatch, tmp_path: Path) -> None:
    real_run = subprocess.run

    def fake_run(command, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if "-subject" in command:  # answer the read-back the way an affected host does
            return subprocess.CompletedProcess(
                command,
                0,
                f"subject= /CN={tls.CA_COMMON_NAME}\nissuer= /CN={tls.CA_COMMON_NAME}\n",
                "",
            )
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(tls.subprocess, "run", fake_run)

    try:
        tls.ensure_server_cert(tmp_path / "tls", ["192.0.2.10"])
    except RuntimeError as refusal:
        message = str(refusal)
    else:  # pragma: no cover - the guard is the point of the test
        raise AssertionError("a leaf carrying the CA's subject was accepted")

    assert "unusable certificate" in message
    assert "LibreSSL" in message and "-subj" in message
    assert "self-signed" in message
    assert not (tmp_path / "tls" / tls.CERT_HOSTS_FILENAME).exists()


def test_reuse_re_signs_a_leaf_reported_without_an_authority_key_identifier(
    monkeypatch, tmp_path: Path
) -> None:
    """A leaf carrying no AKI must be re-signed by the reuse path, not served forever.

    This is the artifact that reached a user: the engine was healthy and its certificate passed
    `openssl verify -CAfile`, but an OpenSSL 3.x peer refused the connection with "certificate
    verify failed: Missing Authority Key Identifier", so an inference request across the LAN failed
    while every local check said the certificate was fine.

    The artifact cannot be minted here: OpenSSL 3.x adds subjectKeyIdentifier/authorityKeyIdentifier
    to a `x509 -req -CA` signature on its own, so only LibreSSL (stock on macOS, and the machine the
    real failure came from) ever produced an AKI-less leaf. The read-back is therefore stubbed to
    report what LibreSSL's output looked like, which is precisely the input the check has to catch.
    """

    directory = tmp_path / "tls"
    crt, _key, ca_crt = tls.ensure_server_cert(directory, ["192.0.2.10"])
    original = crt.read_bytes()

    real_run = tls._run
    stubbed: list[str] = []

    def fake_run(openssl, *args):  # noqa: ANN001, ANN002
        # Answer only the identity read-back of the EXISTING leaf, and only once: the re-signed
        # leaf must be judged on its real content, or the test would loop.
        if "-text" in args and not stubbed:
            stubbed.append("x")
            return "Certificate:\n        X509v3 Subject Alternative Name:\n            IP:192.0.2.10\n"
        return real_run(openssl, *args)

    monkeypatch.setattr(tls, "_run", fake_run)

    fixed, key_after, _ca = tls.ensure_server_cert(directory, ["192.0.2.10"])

    assert stubbed, "the reuse path never read the existing leaf back"
    assert fixed.read_bytes() != original, "an AKI-less leaf was reused instead of re-signed"
    monkeypatch.undo()
    text = tls._run(tls._find_openssl(), "x509", "-noout", "-text", "-in", str(fixed))
    assert "Authority Key Identifier" in text
    assert "Subject Key Identifier" in text
    assert _dn(fixed) == (tls.SERVER_COMMON_NAME, tls.CA_COMMON_NAME)
    assert key_after.stat().st_mode & 0o777 == 0o600
    assert ca_crt.read_bytes()  # the CA is reused, never re-minted
