"""Zero-decision TLS material for LAN grids: a private CA and server certificates.

Nobody hunting for an `openssl` incantation should stand between a machine and its LAN grid.
`grid start --tls` therefore mints its own short-lived private CA inside the grid directory and
signs a server certificate whose SANs cover exactly the addresses the grid is reachable at
(loopback, the advertised host, and the machine's own LAN interfaces). The CA private key never
leaves this machine; the public half is a single small file a peer is handed over — the same
copy-onto-the-other-box ritual as an SSH key, and the only artifact a worker ever needs.

Re-running is cheap and idempotent: an existing CA is reused, and an existing certificate is
reused verbatim whenever its SAN set still covers every requested host (so a restart never
reshuffles material peers may already trust) and its subject and issuer still read as a leaf and
its CA. A freshly signed certificate is read back the same way before it is returned: the one
failure this file has actually shipped was invisible to `curl --cacert` and `openssl verify`, and
surfaced two minutes and three layers downstream as a readiness timeout.

The work is delegated to the `openssl` binary — present on macOS (LibreSSL) and Linux alike —
through a config file rather than `-addext`, because LibreSSL's support for that flag is not
uniform. A missing binary surfaces as a clear `SystemExit` naming the manual flags, never as a
half-written key.
"""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import tempfile
from pathlib import Path

CA_COMMON_NAME = "autonomous-grid local CA"
CA_DAYS = 3650
CERT_DAYS = 398  # browsers reject TLS server certificates valid for more than 398 days
_CA_SUBJECT = f"/CN={CA_COMMON_NAME}"
SERVER_COMMON_NAME = "autonomous-grid"
SERVER_SUBJECT = f"/CN={SERVER_COMMON_NAME}"
CERT_HOSTS_FILENAME = "hosts.txt"  # records which SANs the signed cert covers


class TlsToolMissing(RuntimeError):
    """`openssl` was not found; auto-TLS cannot proceed and the caller names the manual flags."""


def _run(openssl: str, *args: str) -> str:
    result = subprocess.run(
        [openssl, *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise RuntimeError(
            f"openssl {' '.join(args[:2])} failed: {detail[-1] if detail else 'unknown error'}"
        )
    return result.stdout


def _write_private(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    os.chmod(path, 0o600)


def _find_openssl() -> str:
    from shutil import which

    found = which("openssl")
    if not found:
        raise TlsToolMissing(
            "openssl was not found on PATH, so Grid cannot create its own TLS certificate. "
            "Install OpenSSL, or pass --tls-cert/--tls-key with a certificate you already have."
        )
    return found


def _config_text(common_name: str = CA_COMMON_NAME) -> str:
    # A minimal but complete OpenSSL config: LibreSSL ignores -addext on some builds, so the CA
    # extensions come through `-extensions` here instead.
    #
    # The CN must be written into the config rather than left to `-subj`: LibreSSL (stock on macOS)
    # takes the DN from a `prompt = no` config and silently ignores `-subj`, which handed the server
    # leaf the CA's own subject. issuer == subject makes a strict chain builder (Python's `ssl`)
    # read the leaf as an untrusted self-signed root -- the allocator readiness probe then fails
    # forever with "self-signed certificate" while curl and `openssl verify` both still pass.
    return f"""[req]
distinguished_name = dn
prompt = no
[dn]
CN = {common_name}
[v3_ca]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
"""


def _reported_common_name(text: str, label: str) -> str:
    """The CN openssl printed on the ``label`` ("subject"/"issuer") line, in either dialect.

    LibreSSL prints `subject= /CN=x` and OpenSSL 3 prints `subject=CN = x` — the leading slash,
    the component separator and the spacing all differ — so the CN is pulled out rather than the
    whole DN string compared.
    """
    for line in text.splitlines():
        head, sep, dn = line.strip().partition("=")
        if not sep or head.strip().lower() != label:
            continue
        for part in re.split(r"[/,]", dn):
            name, has_value, value = part.partition("=")
            if has_value and name.strip().upper() == "CN":
                return value.strip()
    return ""


def _leaf_identity_problem(openssl: str, crt: Path) -> str | None:
    """``None`` if ``crt`` really is a CA-signed leaf, else a sentence saying what it is instead.

    `openssl verify -CAfile ca.crt server.crt` passes on the broken artifact this catches
    (measured), and so does `curl --cacert` — the CA did sign it. What breaks is that the leaf
    carries the CA's own subject, so subject == issuer and a strict chain builder (Python's `ssl`,
    httpx) reads it as an untrusted self-signed root: CERTIFICATE_VERIFY_FAILED, three layers and
    two minutes away from here. Only the DNs tell the two apart, so the DNs are what is checked.
    """
    reported = _run(openssl, "x509", "-noout", "-subject", "-issuer", "-in", str(crt))
    subject = _reported_common_name(reported, "subject")
    issuer = _reported_common_name(reported, "issuer")
    if subject == SERVER_COMMON_NAME and issuer == CA_COMMON_NAME:
        # A leaf minted before the AKI extension was added verifies here but is rejected outright
        # by an OpenSSL 3.x peer, so the reuse path has to re-sign it rather than serve it forever.
        text = _run(openssl, "x509", "-noout", "-text", "-in", str(crt))
        if "Authority Key Identifier" not in text:
            return (
                f"{crt} carries no Authority Key Identifier; OpenSSL 3.x peers refuse such a leaf "
                "with 'certificate verify failed: Missing Authority Key Identifier' even though "
                "openssl verify -CAfile accepts it"
            )
        return None
    problem = (
        f"{crt} has subject CN={subject!r} and issuer CN={issuer!r}, but a usable leaf has "
        f"subject CN={SERVER_COMMON_NAME!r} issued by CN={CA_COMMON_NAME!r}"
    )
    if subject == CA_COMMON_NAME:
        problem += (
            "; subject == issuer, so Python's ssl and httpx reject it as an untrusted "
            "self-signed root even though curl --cacert and openssl verify -CAfile both pass. "
            "The likely cause is an openssl that ignores -subj when the config sets "
            "prompt = no — LibreSSL 3.3.6, stock on macOS, does exactly that, and the leaf then "
            "inherits the CA's CN from the config"
        )
    return problem


def ensure_ca(directory: Path, openssl: str | None = None) -> tuple[Path, Path]:
    """The grid's private CA as ``(ca.crt, ca.key)``, created once and reused forever after."""
    tool = openssl or _find_openssl()
    directory.mkdir(parents=True, exist_ok=True)
    crt = directory / "ca.crt"
    key = directory / "ca.key"
    if crt.is_file() and key.is_file():
        return crt, key
    if crt.is_file() != key.is_file():
        raise RuntimeError(
            f"{directory} holds half a CA ({'crt' if crt.is_file() else 'key'} missing); "
            "remove both files and start again."
        )
    with tempfile.TemporaryDirectory(prefix="grid-ca-") as tmp:
        conf = Path(tmp) / "ca.cnf"
        conf.write_text(_config_text(), encoding="utf-8")
        _run(
            tool,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(crt),
            "-days",
            str(CA_DAYS),
            "-config",
            str(conf),
            "-extensions",
            "v3_ca",
            "-subj",
            _CA_SUBJECT,
        )
    os.chmod(key, 0o600)
    return crt, key


def requested_san_hosts(loopback: bool, hosts: list[str]) -> list[str]:
    """Deduplicated, ordered SAN set: requested hosts plus loopback names.

    The certificate must cover every name a client may dial. Callers pass the advertise host and
    the machine's own interface addresses; loopback is always added because the master reaches its
    own control plane that way too. Bare IPv6 literals keep their canonical (unbracketed) form —
    OpenSSL wants the address, not the URL spelling.
    """
    names: list[str] = []

    def add(value: str) -> None:
        value = str(value or "").strip().strip("[]").split("%", 1)[0]
        if not value:
            return
        try:
            value = str(ipaddress.ip_address(value))  # canonicalise ::1 spellings etc.
        except ValueError:
            pass
        if value not in names:
            names.append(value)

    for host in hosts:
        add(host)
    if loopback:
        add("127.0.0.1")
        add("localhost")
    return names


def ensure_server_cert(
    directory: Path,
    hosts: list[str],
    *,
    loopback: bool = True,
) -> tuple[Path, Path, Path]:
    """A CA-signed certificate for ``hosts`` at ``(server.crt, server.key, ca.crt)``.

    Idempotent: the CA is reused if present; the certificate is reused when its recorded SAN set
    still covers every requested host. The host list is recorded beside the certificate because
    reading SANs back would need another openssl invocation on every start for no benefit.
    """
    requested = requested_san_hosts(loopback, hosts)
    if not requested:
        raise ValueError("a TLS certificate needs at least one host to cover")
    directory.mkdir(parents=True, exist_ok=True)
    crt = directory / "server.crt"
    key = directory / "server.key"
    hosts_file = directory / CERT_HOSTS_FILENAME
    # Found before the reuse check, because reuse now reads the existing leaf back with it.
    tool = _find_openssl()
    if crt.is_file() or key.is_file():
        if crt.is_file() and key.is_file() and hosts_file.is_file():
            covered = [line.strip() for line in hosts_file.read_text().splitlines() if line.strip()]
            try:
                broken = _leaf_identity_problem(tool, crt)
            except RuntimeError as unreadable:  # truncated leaf: re-sign rather than die
                broken = str(unreadable)
            if requested and all(host in covered for host in requested) and broken is None:
                ca_crt, _ = ensure_ca(directory, tool)
                return crt, key, ca_crt
        # Stale SAN set, a leaf carrying the CA's own subject (what every host that ran the
        # pre-fix signing code holds — an upgrade alone would keep serving it forever), or
        # half-written material from an interrupted signing run: openssl refuses to overwrite a
        # key, so clear the pair before re-signing.
        crt.unlink(missing_ok=True)
        key.unlink(missing_ok=True)
    ca_crt, ca_key = ensure_ca(directory, tool)
    with tempfile.TemporaryDirectory(prefix="grid-cert-") as tmp:
        tmpdir = Path(tmp)
        conf = tmpdir / "cert.cnf"
        conf.write_text(_config_text(SERVER_COMMON_NAME), encoding="utf-8")
        csr = tmpdir / "server.csr"
        _run(
            tool,
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(csr),
            "-config",
            str(conf),
            "-subj",
            SERVER_SUBJECT,
        )
        ext = tmpdir / "san.ext"
        entries = []
        for host in requested:
            try:
                ipaddress.ip_address(host)
                entries.append(f"IP:{host}")
            except ValueError:
                entries.append(f"DNS:{host}")
        _write_private(
            ext,
            # subjectKeyIdentifier/authorityKeyIdentifier are not decoration. OpenSSL 3.x refuses a
            # leaf without an AKI outright ("certificate verify failed: Missing Authority Key
            # Identifier"), which is how a healthy engine became unreachable from a relay running a
            # newer OpenSSL than the machine that minted the certificate. They also let a verifier
            # match issuer BY KEY ID instead of by subject name — the property that keeps two of
            # this codebase's CAs, which necessarily share CN=autonomous-grid local CA, from being
            # confused for one another in a single trust store.
            "basicConstraints = CA:FALSE\nkeyUsage = digitalSignature, keyEncipherment\n"
            "extendedKeyUsage = serverAuth\nsubjectKeyIdentifier = hash\n"
            "authorityKeyIdentifier = keyid:always,issuer\n"
            "subjectAltName = " + ", ".join(entries) + "\n",
        )
        _run(
            tool,
            "x509",
            "-req",
            "-in",
            str(csr),
            "-CA",
            str(ca_crt),
            "-CAkey",
            str(ca_key),
            # LibreSSL (macOS stock) refuses to sign without a serial file and never creates one
            # itself; OpenSSL 3 happens to tolerate the omission. The two-machine E2E died here on
            # the LibreSSL worker — the flag is a no-op when the serial file already exists.
            "-CAcreateserial",
            "-out",
            str(crt),
            "-days",
            str(CERT_DAYS),
            "-extfile",
            str(ext),
        )
    os.chmod(key, 0o600)
    # Read the signed leaf back before anyone is handed it: the failure this catches surfaces as
    # an allocator readiness timeout 120s and three layers downstream, and every hand-check on the
    # way there says the certificate is fine. Fail here, where the cause is still in view.
    problem = _leaf_identity_problem(tool, crt)
    if problem:
        raise RuntimeError(f"openssl x509 -req produced an unusable certificate: {problem}")
    hosts_file.write_text("".join(f"{host}\n" for host in requested), encoding="utf-8")
    return crt, key, ca_crt
