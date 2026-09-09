"""Zero-decision TLS material for LAN grids: a private CA and server certificates.

Nobody hunting for an `openssl` incantation should stand between a machine and its LAN grid.
`grid start --tls` therefore mints its own short-lived private CA inside the grid directory and
signs a server certificate whose SANs cover exactly the addresses the grid is reachable at
(loopback, the advertised host, and the machine's own LAN interfaces). The CA private key never
leaves this machine; the public half is a single small file a peer is handed over — the same
copy-onto-the-other-box ritual as an SSH key, and the only artifact a worker ever needs.

Re-running is cheap and idempotent: an existing CA is reused, and an existing certificate is
reused verbatim whenever its SAN set still covers every requested host (so a restart never
reshuffles material peers may already trust).

The work is delegated to the `openssl` binary — present on macOS (LibreSSL) and Linux alike —
through a config file rather than `-addext`, because LibreSSL's support for that flag is not
uniform. A missing binary surfaces as a clear `SystemExit` naming the manual flags, never as a
half-written key.
"""

from __future__ import annotations

import ipaddress
import os
import subprocess
import tempfile
from pathlib import Path

CA_COMMON_NAME = "autonomous-grid local CA"
CA_DAYS = 3650
CERT_DAYS = 398  # browsers reject TLS server certificates valid for more than 398 days
_CA_SUBJECT = f"/CN={CA_COMMON_NAME}"
SERVER_SUBJECT = "/CN=autonomous-grid"
CERT_HOSTS_FILENAME = "hosts.txt"  # records which SANs the signed cert covers


class TlsToolMissing(RuntimeError):
    """`openssl` was not found; auto-TLS cannot proceed and the caller names the manual flags."""


def _run(openssl: str, *args: str) -> None:
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


def _config_text() -> str:
    # A minimal but complete OpenSSL config: LibreSSL ignores -addext on some builds, so the CA
    # extensions come through `-extensions` here instead.
    return f"""[req]
distinguished_name = dn
prompt = no
[dn]
CN = {CA_COMMON_NAME}
[v3_ca]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
"""


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
    if crt.is_file() or key.is_file():
        if crt.is_file() and key.is_file() and hosts_file.is_file():
            covered = [line.strip() for line in hosts_file.read_text().splitlines() if line.strip()]
            if requested and all(host in covered for host in requested):
                ca_crt, _ = ensure_ca(directory)
                return crt, key, ca_crt
        # Stale SAN set, or half-written material from an interrupted signing run: openssl refuses
        # to overwrite a key, so clear the pair before re-signing.
        crt.unlink(missing_ok=True)
        key.unlink(missing_ok=True)
    tool = _find_openssl()
    ca_crt, ca_key = ensure_ca(directory, tool)
    with tempfile.TemporaryDirectory(prefix="grid-cert-") as tmp:
        tmpdir = Path(tmp)
        conf = tmpdir / "cert.cnf"
        conf.write_text(_config_text(), encoding="utf-8")
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
            "basicConstraints = CA:FALSE\nkeyUsage = digitalSignature, keyEncipherment\n"
            "extendedKeyUsage = serverAuth\nsubjectAltName = " + ", ".join(entries) + "\n",
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
    hosts_file.write_text("".join(f"{host}\n" for host in requested), encoding="utf-8")
    return crt, key, ca_crt
