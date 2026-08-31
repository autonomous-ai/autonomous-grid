"""No-SSH launcher for physical multi-machine Grid Goal acceptance tests.

This is deliberately a test utility, not a production relay installer. Until the hosted relay has
the feature branch, one test machine can run the matching private relay checkout in the foreground.
Every other machine joins it through ordinary Grid HTTP/Git traffic; nobody logs into another
machine and no task workspace is copied.

Relay host (either physical machine)::

    uv run python tests/e2e_cross_repo/physical_goal_lab.py relay \
      --relay-repo /private/tmp/autonomous-grid-cli-goal-relay \
      --root /private/tmp/grid-goal-physical \
      --joining-workers 2

Internet-separated workers should put the same listener behind TLS and advertise the public root::

    uv run python tests/e2e_cross_repo/physical_goal_lab.py relay \
      --relay-repo /private/tmp/autonomous-grid-cli-goal-relay \
      --root /private/tmp/grid-goal-physical \
      --joining-workers 2 \
      --bind-host 127.0.0.1 \
      --advertise-url https://goal-lab.example.test

Joining workers (the other physical machines, each with its own printed bundle)::

    uv run python tests/e2e_cross_repo/physical_goal_lab.py configure \
      --home /private/tmp/grid-goal-worker

The relay command discovers the relay host's LAN address, prints a short-lived pairing bundle, and
keeps the relay in the foreground. Paste that bundle at the joining worker's hidden prompt. The
physical A/B labels are intentionally absent: either machine may host the relay. The hidden prompt
is safest. Automation should use an owner-only ``--bundle-file``; ``--bundle`` is retained for
disposable environments that accept exposure in shell history and the local process list.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import getpass
import hashlib
import hmac
import json
import os
import secrets
import signal
import socket
import stat
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit


NETWORK_ID = "grid-goal-physical"
NETWORK_NAME = "goal-physical"
PAIR_VERSION = 1
DEFAULT_PORT = 8090
DEFAULT_TOKEN_HOURS = 48
MAX_PAIR_BYTES = 16_384
SCOPES = [
    "inference:create",
    "inference:models",
    "inference:resume",
    "provider:heartbeat",
    "provider:update",
    "provider:poll",
    "provider:submit",
    "provider:error",
]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def issue_token(secret: str, *, user_id: str, node_id: str, expires_at: int) -> str:
    """Mint the same HS256 JWT shape the non-Grid-mode relay verifies."""
    now = int(time.time())
    header = _b64(json.dumps(
        {"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64(json.dumps({
        "user_id": user_id,
        "node_id": node_id,
        "email": "goal-lab@invalid",
        "role": "both",
        "scopes": SCOPES,
        "iat": now,
        "exp": expires_at,
    }, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode()
    signature = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64(signature)}"


def token_claims(token: str) -> dict[str, Any]:
    """Read pairing claims for local validation; the relay remains the signature authority."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("pairing credential is not a three-segment JWT")
    try:
        claims = json.loads(_unb64(parts[1]))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("pairing credential has an invalid JWT payload") from exc
    if not isinstance(claims, dict):
        raise ValueError("pairing credential JWT payload is not an object")
    return claims


def relay_url(host: str, port: int) -> str:
    """Build an HTTP URL, bracketing a literal IPv6 address when needed."""
    clean = host.strip().strip("[]")
    if not clean or any(char in clean for char in "/?#@"):
        raise ValueError(f"invalid relay host {host!r}")
    rendered = f"[{clean}]" if ":" in clean else clean
    return f"http://{rendered}:{port}"


def validate_relay_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("pairing bundle has no valid HTTP relay URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("pairing bundle relay URL must not contain credentials, query, or fragment")
    if parsed.path not in ("", "/"):
        raise ValueError("pairing bundle relay URL must name the relay root")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("pairing bundle relay URL has an invalid port") from exc
    return value.rstrip("/")


def encode_pair(*, url: str, token: str, node_id: str, expires_at: int) -> str:
    payload = json.dumps({
        "version": PAIR_VERSION,
        "network_id": NETWORK_ID,
        "network_name": NETWORK_NAME,
        "relay_url": validate_relay_url(url),
        "access_token": token,
        "node_id": node_id,
        "expires_at": expires_at,
    }, sort_keys=True, separators=(",", ":")).encode()
    return _b64(payload)


def decode_pair(bundle: str, *, now: int | None = None) -> dict[str, Any]:
    if len(bundle) > MAX_PAIR_BYTES:
        raise ValueError("pairing bundle is unexpectedly large")
    try:
        value = json.loads(_unb64("".join(bundle.split())))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("pairing bundle is not valid base64url JSON") from exc
    if not isinstance(value, dict) or value.get("version") != PAIR_VERSION:
        raise ValueError(f"pairing bundle must use version {PAIR_VERSION}")
    if value.get("network_id") != NETWORK_ID or value.get("network_name") != NETWORK_NAME:
        raise ValueError("pairing bundle names an unexpected Grid")
    value["relay_url"] = validate_relay_url(str(value.get("relay_url") or ""))
    token = value.get("access_token")
    node_id = value.get("node_id")
    expires_at = value.get("expires_at")
    if not isinstance(token, str) or not isinstance(node_id, str) or not node_id:
        raise ValueError("pairing bundle has no usable node credential")
    if not isinstance(expires_at, int) or isinstance(expires_at, bool):
        raise ValueError("pairing bundle has no valid expiry")
    claims = token_claims(token)
    if claims.get("node_id") != node_id or claims.get("exp") != expires_at:
        raise ValueError("pairing bundle identity does not match its signed credential")
    if not claims.get("user_id"):
        raise ValueError("pairing credential has no user identity")
    if claims.get("role") != "both" or not set(SCOPES).issubset(set(claims.get("scopes") or [])):
        raise ValueError("pairing credential does not carry the required Grid scopes")
    if expires_at <= (int(time.time()) if now is None else now):
        raise ValueError("pairing bundle has expired; restart the disposable relay")
    return value


@contextlib.contextmanager
def _grid_home(home: Path) -> Iterator[None]:
    previous = os.environ.get("GRID_HOME")
    os.environ["GRID_HOME"] = str(home)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("GRID_HOME", None)
        else:
            os.environ["GRID_HOME"] = previous


def configure_home(home: Path, pairing: dict[str, Any]) -> None:
    """Write a normal isolated remote-mode Grid credential store for one physical node."""
    from remote import credentials
    from shared import state

    home = home.expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        home.chmod(0o700)
    claims = token_claims(pairing["access_token"])
    record = {
        "network_id": NETWORK_ID,
        "name": NETWORK_NAME,
        "signaling_url": pairing["relay_url"],
        "lan_signaling_url": pairing["relay_url"],
        "access_token": pairing["access_token"],
    }
    with _grid_home(home):
        credentials.save_credentials({
            # Goal, project, provider and overview operations use the per-grid token directly.  A
            # nonempty disposable session keeps remote-mode resolution honest without pretending
            # this test relay has a hosted control-plane account behind it.
            "session_token": "physical-goal-lab",
            "api_url": "https://control-plane.invalid",
            "user": {"email": claims.get("email") or "goal-lab@invalid"},
            "networks": [record],
        })
        state.set_mode("remote")
        state.set_active("remote", NETWORK_ID)


def discover_lan_host() -> str:
    """Ask the OS which source address it would use; no packet or remote login is involved."""
    probes = ((socket.AF_INET, ("192.0.2.1", 9)), (socket.AF_INET6, ("2001:db8::1", 9)))
    for family, target in probes:
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.connect(target)
                candidate = str(sock.getsockname()[0])
        except OSError:
            continue
        if candidate and candidate not in ("127.0.0.1", "::1"):
            return candidate
    raise SystemExit(
        "Could not discover a LAN address. Pass --advertise-host with an address the joining "
        "worker can reach.")


def _safe_root(raw: str) -> Path:
    expanded = Path(raw).expanduser()
    # A disposable pairing helper must never be pointed through a friendly-looking symlink at a
    # real Grid home. Resolve parents for the containment check, but remember whether the leaf the
    # operator actually typed was itself a link before resolution erased that fact.
    leaf_is_symlink = expanded.is_symlink()
    root = expanded.resolve()
    user_home = Path.home().resolve()
    inside_user_home = root == user_home or user_home in root.parents
    if leaf_is_symlink or inside_user_home or root == Path("/").resolve() or len(root.parts) < 3:
        raise SystemExit(f"Refusing unsafe lab root: {root}")
    return root


def _write_secret(path: Path, value: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, value.encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_private(path: Path, value: str) -> None:
    """Atomically replace a disposable credential artifact and leave it owner-readable only."""
    from shared import jsonio

    jsonio.atomic_write_bytes(path, value.encode(), 0o600)


def validate_physical_node_ids(relay_node: str, worker_nodes: list[str]) -> None:
    """Refuse an acceptance topology that collapses physical machines onto one identity."""
    node_ids = [relay_node, *worker_nodes]
    if not relay_node or not worker_nodes or any(not node_id for node_id in node_ids):
        raise SystemExit("Physical Goal lab identity state has a missing relay or worker node id")
    if len(set(node_ids)) != len(node_ids):
        raise SystemExit(
            "Physical Goal lab identities must be distinct; use one pairing bundle per machine")


def prepare_relay(args: argparse.Namespace) -> tuple[Path, dict[str, str], str]:
    root = _safe_root(args.root)
    relay_repo = Path(args.relay_repo).expanduser().resolve()
    server_dir = relay_repo / "grid_cli" / "private_server"
    relay_python = relay_repo / ".venv" / "bin" / "python"
    if not server_dir.is_dir():
        raise SystemExit(f"Private relay server not found at {server_dir}")
    if not relay_python.is_file():
        raise SystemExit(f"Private relay virtualenv not found at {relay_python}; run `uv sync` there")

    if root.exists() and not args.reuse:
        raise SystemExit(f"Lab root already exists: {root}. Use a new root or pass --reuse.")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        root.chmod(0o700)

    secret_path = root / "jwt-secret"
    identity_path = root / "identity.json"
    requested_workers = getattr(args, "joining_workers", None)
    if secret_path.exists() and identity_path.exists() and args.reuse:
        secret = secret_path.read_text(encoding="utf-8")
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        user_id = str(identity["user_id"])
        # Accept roots created by the first lab helper, whose A/B names encoded one arbitrary
        # topology. New roots persist role names so either physical machine can host the relay.
        stored_workers = identity.get("worker_node_ids")
        if isinstance(stored_workers, list) and stored_workers:
            worker_nodes = [str(node) for node in stored_workers if str(node)]
        else:
            worker_nodes = [str(identity.get("worker_node_id") or identity["node_a"])]
        relay_node = str(identity.get("relay_node_id") or identity["node_b"])
        if requested_workers is not None:
            while len(worker_nodes) < requested_workers:
                worker_nodes.append(f"goal-worker-{uuid.uuid4()}")
        # Upgrade legacy identities and persist any added workers. Never shrink a reused lab: an
        # already-paired physical worker must keep its signed identity across relay restarts.
        validate_physical_node_ids(relay_node, worker_nodes)
        _write_private(identity_path, json.dumps({
            "user_id": user_id,
            "worker_node_ids": worker_nodes,
            "relay_node_id": relay_node,
        }, indent=2) + "\n")
    elif secret_path.exists() or identity_path.exists():
        raise SystemExit(f"Lab root {root} is incomplete; use a new root instead of reusing it")
    else:
        secret = secrets.token_urlsafe(48)
        user_id = f"goal-lab-{uuid.uuid4()}"
        worker_nodes = [
            f"goal-worker-{uuid.uuid4()}"
            for _ in range(requested_workers or 1)
        ]
        relay_node = f"goal-relay-{uuid.uuid4()}"
        validate_physical_node_ids(relay_node, worker_nodes)
        _write_secret(secret_path, secret)
        _write_private(identity_path, json.dumps({
            "user_id": user_id,
            "worker_node_ids": worker_nodes,
            "relay_node_id": relay_node,
        }, indent=2) + "\n")

    advertised = getattr(args, "advertise_url", None)
    if advertised:
        url = validate_relay_url(advertised)
    else:
        host = args.advertise_host or discover_lan_host()
        url = relay_url(host, args.port)
    expires_at = int(time.time()) + int(args.token_hours * 3600)
    relay_token = issue_token(
        secret, user_id=user_id, node_id=relay_node, expires_at=expires_at)
    worker_pairs = [encode_pair(
        url=url,
        token=issue_token(
            secret, user_id=user_id, node_id=worker_node, expires_at=expires_at),
        node_id=worker_node, expires_at=expires_at,
    ) for worker_node in worker_nodes]
    relay_pair = decode_pair(encode_pair(
        url=url, token=relay_token, node_id=relay_node, expires_at=expires_at))
    configure_home(root / "grid-home-relay", relay_pair)
    # Keep the original filename as the first-worker compatibility alias. Numbered private files
    # make it possible to stage credentials independently without ever giving B and C one node id.
    _write_private(root / "joining-worker-pairing.txt", worker_pairs[0] + "\n")
    for index, worker_pair in enumerate(worker_pairs, start=1):
        _write_private(root / f"joining-worker-{index}-pairing.txt", worker_pair + "\n")

    revision = subprocess.run(
        ["git", "-C", str(relay_repo), "rev-parse", "HEAD"], capture_output=True,
        text=True, timeout=10).stdout.strip() or "unknown"

    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{root / 'relay.db'}",
        "KEYS_PATH": str(root / "keys"),
        "JWT_SECRET": secret,
        "API_KEYS_ENABLED": "false",
        "TASK_REPO_ROOT": str(root / "projects"),
        "TASK_LEASE_SECONDS": str(args.lease_seconds),
        "TASK_REAPER_INTERVAL_SECONDS": str(args.reaper_seconds),
        "TASK_CLAIM_TIMEOUT_SECONDS": str(args.claim_timeout_seconds),
        "GRID_MODE": "false",
        "PYTHONPATH": str(server_dir),
    }
    metadata = {
        "url": url,
        "worker_pair": worker_pairs[0],
        "worker_node_id": worker_nodes[0],
        "worker_pairs": worker_pairs,
        "worker_node_ids": worker_nodes,
        "relay_node_id": relay_node,
        "relay_home": str(root / "grid-home-relay"),
        "server_dir": str(server_dir),
        "python": str(relay_python),
        "relay_revision": revision,
    }
    return root, env, json.dumps(metadata)


def _wait_for_health(proc: subprocess.Popen, url: str, timeout: float = 90.0) -> None:
    import httpx

    deadline = time.monotonic() + timeout
    local = f"http://127.0.0.1:{urlsplit(url).port or DEFAULT_PORT}"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"Goal relay exited during startup with status {proc.returncode}")
        try:
            if httpx.get(f"{local}/relay/v1/health", timeout=2.0).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise SystemExit("Goal relay did not become healthy within 90 seconds")


def cmd_relay(args: argparse.Namespace) -> int:
    root, env, raw_metadata = prepare_relay(args)
    metadata = json.loads(raw_metadata)
    command = [
        metadata["python"], "-m", "uvicorn", "server:app",
        "--host", args.bind_host, "--port", str(args.port), "--log-level", "info",
    ]
    proc = subprocess.Popen(command, cwd=metadata["server_dir"], env=env)
    try:
        # Probe the listener directly. An advertised HTTPS URL may terminate TLS at a reverse
        # proxy on port 443 while uvicorn remains private on `args.port`.
        _wait_for_health(proc, f"http://127.0.0.1:{args.port}")
        print("\nGrid Goal physical relay is ready (no SSH):", flush=True)
        print(f"  relay:       {metadata['url']}", flush=True)
        print(f"  relay node:  GRID_HOME={metadata['relay_home']}", flush=True)
        print(f"  relay id:    {metadata['relay_node_id']}", flush=True)
        worker_node_ids = metadata.get("worker_node_ids") or [metadata["worker_node_id"]]
        worker_pairs = metadata.get("worker_pairs") or [metadata["worker_pair"]]
        for index, worker_node_id in enumerate(worker_node_ids, start=1):
            print(f"  worker {index} id: {worker_node_id}", flush=True)
        print(f"  relay state: {root}", flush=True)
        print(f"  relay SHA:   {metadata['relay_revision']}", flush=True)
        if args.no_print_bundle:
            print("\nPairing bundle suppressed; already-paired workers can reconnect.", flush=True)
        else:
            print("\nEvery relay/worker id above must be different. Record them; node names alone "
                  "are not identity evidence.", flush=True)
            for index, worker_pair in enumerate(worker_pairs, start=1):
                print(f"\nOn joining worker {index}, run:", flush=True)
                print("  uv run python tests/e2e_cross_repo/physical_goal_lab.py configure \\", flush=True)
                print(f"    --home /private/tmp/grid-goal-worker-{index}", flush=True)
                print("Then paste only this worker's disposable bundle at its hidden prompt:",
                      flush=True)
                print(worker_pair, flush=True)
        print("\nKeep this terminal open. Ctrl-C stops only the disposable relay.", flush=True)
        return proc.wait()
    except KeyboardInterrupt:
        return 130
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def cmd_configure(args: argparse.Namespace) -> int:
    raw = args.bundle
    if raw is None and args.bundle_file is not None:
        # Keep the supplied filesystem object intact until ``open``: resolving first would follow a
        # symlink before O_NOFOLLOW had a chance to reject it. Pairing data is a bearer credential,
        # so validate and read one already-open inode rather than stat/path/read (a replacement race).
        path = Path(os.path.abspath(os.fspath(Path(args.bundle_file).expanduser())))
        fd = -1
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("not a regular file")
            if os.name == "posix" and info.st_mode & 0o077:
                raise OSError("permissions must be owner-only (0600)")
            if info.st_size > MAX_PAIR_BYTES:
                raise OSError(f"file exceeds {MAX_PAIR_BYTES} bytes")
            chunks: list[bytes] = []
            remaining = MAX_PAIR_BYTES + 1
            while remaining:
                chunk = os.read(fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) > MAX_PAIR_BYTES:
                raise OSError(f"file exceeds {MAX_PAIR_BYTES} bytes")
            raw = payload.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise SystemExit(f"Could not read private pairing bundle file {path}: {exc}") from None
        finally:
            if fd >= 0:
                os.close(fd)
    if raw is None:
        raw = getpass.getpass("Paste joining-worker pairing bundle (input hidden): ")
    try:
        pairing = decode_pair(raw)
    except ValueError as exc:
        raise SystemExit(f"Invalid pairing bundle: {exc}") from None
    home = _safe_root(args.home)
    existing = list(home.iterdir()) if home.exists() else []
    if existing and not args.replace:
        raise SystemExit(f"Grid home is not empty: {home}. Use a new path or pass --replace.")
    if existing:
        # Re-pairing may replace only the two credential/state files this helper writes. A `run/`
        # tree means a provider may still be alive, and cached engines/logs from another signed
        # identity invalidate a physical acceptance run. Refuse arbitrary files rather than
        # deleting a caller's directory under a flag whose name sounds harmless.
        allowed = {"credentials.toml", "state.json"}
        unsafe = [path.name for path in existing
                  if path.name not in allowed or path.is_symlink() or not path.is_file()]
        if unsafe:
            raise SystemExit(
                f"Grid home cannot be safely replaced: {home} contains runtime or unknown "
                f"artifacts {sorted(unsafe)}. Stop the worker and use a new empty --home.")
    configure_home(home, pairing)
    print("Joining worker is paired without SSH.")
    print(f"  GRID_HOME={home}")
    print(f"  relay={pairing['relay_url']}")
    print(f"  node={pairing['node_id']}")
    print("Preflight: GRID_HOME=" + str(home) + " uv run grid goal list --all --json")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="No-SSH physical Grid Goal acceptance lab")
    actions = parser.add_subparsers(dest="action", required=True)

    relay = actions.add_parser("relay", help="Run the disposable Goal relay on this host")
    relay.add_argument("--relay-repo", required=True,
                       help="Matching autonomous-grid-cli feature-branch checkout")
    relay.add_argument("--root", default="/private/tmp/grid-goal-physical",
                       help="Fresh private directory for relay state")
    relay.add_argument("--bind-host", default="0.0.0.0",
                       help="Listener address (default: all interfaces)")
    advertise = relay.add_mutually_exclusive_group()
    advertise.add_argument(
        "--advertise-host", default=None,
        help="Address the joining worker can reach (default: discover automatically)")
    advertise.add_argument(
        "--advertise-url", default=None,
        help=("Exact HTTPS relay root exposed by a reverse proxy/tunnel; use this for "
              "internet-separated workers"))
    relay.add_argument("--port", type=int, default=DEFAULT_PORT)
    relay.add_argument("--token-hours", type=float, default=DEFAULT_TOKEN_HOURS)
    relay.add_argument("--lease-seconds", type=int, default=120)
    relay.add_argument("--reaper-seconds", type=int, default=5)
    relay.add_argument("--claim-timeout-seconds", type=int, default=30)
    relay.add_argument(
        "--joining-workers", type=int, default=None, metavar="N",
        help=("Mint N distinct joining-worker identities (default: 1 for a new lab; preserve all "
              "stored identities on --reuse). Use 2 when the relay host is machine A of the "
              "three-machine acceptance test."))
    relay.add_argument("--reuse", action="store_true",
                       help="Reuse this lab identity/database after a relay restart")
    relay.add_argument("--no-print-bundle", action="store_true",
                       help="Do not print the joining-worker credential (for paired restarts)")
    relay.set_defaults(func=cmd_relay)

    configure = actions.add_parser("configure", help="Pair this joining worker with the relay")
    configure.add_argument("--home", default="/private/tmp/grid-goal-worker",
                           help="Fresh isolated GRID_HOME")
    pairing_input = configure.add_mutually_exclusive_group()
    pairing_input.add_argument(
        "--bundle", default=None,
        help=("Disposable pairing bundle (automation only; visible in shell "
              "history/process listings). Omit for a hidden prompt."))
    pairing_input.add_argument(
        "--bundle-file", default=None,
        help=("Read the disposable pairing bundle from an owner-only 0600 file without exposing "
              "it in shell history or the process list."))
    configure.add_argument("--replace", action="store_true",
                           help=("Re-pair a home containing only state.json and credentials.toml; "
                                 "runtime or unknown artifacts are refused"))
    configure.set_defaults(func=cmd_configure)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.action == "relay":
        if not 1 <= args.port <= 65535:
            raise SystemExit("--port must be between 1 and 65535")
        if args.token_hours <= 0:
            raise SystemExit("--token-hours must be positive")
        for name in ("lease_seconds", "reaper_seconds", "claim_timeout_seconds"):
            if getattr(args, name) <= 0:
                raise SystemExit(f"--{name.replace('_', '-')} must be positive")
        if args.joining_workers is not None and not 1 <= args.joining_workers <= 8:
            raise SystemExit("--joining-workers must be between 1 and 8")
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
