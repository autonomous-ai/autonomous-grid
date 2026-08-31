"""Unit checks for the no-SSH physical Goal lab bootstrap.

The live acceptance remains physical and cross-repository.  These checks protect the part most
likely to turn a real run into a misleading one: two machines accidentally sharing one node id,
an expired/copied credential, or a config that silently resolves back to the hosted Grid.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import stat
import time
from pathlib import Path

import pytest


_SOURCE = Path(__file__).parent / "e2e_cross_repo" / "physical_goal_lab.py"
_SPEC = importlib.util.spec_from_file_location("physical_goal_lab", _SOURCE)
assert _SPEC and _SPEC.loader
lab = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lab)


def _pair(*, node="goal-a", expires=None):
    expiry = expires if expires is not None else int(time.time()) + 3600
    token = lab.issue_token(
        "secret-padded-past-thirty-two-bytes", user_id="one-owner",
        node_id=node, expires_at=expiry)
    return lab.decode_pair(lab.encode_pair(
        url="http://192.0.2.44:8090", token=token, node_id=node, expires_at=expiry))


def test_pairing_round_trip_pins_relay_node_owner_and_expiry():
    pair = _pair()
    claims = lab.token_claims(pair["access_token"])

    assert pair["relay_url"] == "http://192.0.2.44:8090"
    assert pair["network_id"] == lab.NETWORK_ID
    assert pair["node_id"] == claims["node_id"] == "goal-a"
    assert claims["user_id"] == "one-owner"
    assert set(lab.SCOPES).issubset(claims["scopes"])


def test_physical_identity_set_refuses_shared_or_missing_worker_ids():
    lab.validate_physical_node_ids("relay", ["worker-b", "worker-c"])
    with pytest.raises(SystemExit, match="distinct"):
        lab.validate_physical_node_ids("relay", ["worker", "worker"])
    with pytest.raises(SystemExit, match="missing"):
        lab.validate_physical_node_ids("relay", [])


def test_disposable_lab_root_refuses_the_real_user_home_tree():
    with pytest.raises(SystemExit, match="unsafe lab root"):
        lab._safe_root(str(Path.home() / ".grid" / "looks-disposable"))


def test_disposable_lab_root_refuses_a_symlink_even_when_its_target_is_temporary(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "friendly-name"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(SystemExit, match="unsafe lab root"):
        lab._safe_root(str(link))


def test_configure_exposes_automation_bundle_parameter_with_warning():
    parser = lab.build_parser()
    args = parser.parse_args(["configure", "--bundle", "disposable"])

    assert args.bundle == "disposable"
    help_text = parser._subparsers._group_actions[0].choices["configure"].format_help()
    assert "--bundle BUNDLE" in help_text
    assert "shell history/process listings" in help_text


def test_configure_exposes_private_bundle_file_and_refuses_both_inputs():
    parser = lab.build_parser()
    args = parser.parse_args(["configure", "--bundle-file", "/tmp/private-pairing"])

    assert args.bundle is None
    assert args.bundle_file == "/tmp/private-pairing"
    help_text = parser._subparsers._group_actions[0].choices["configure"].format_help()
    assert "--bundle-file BUNDLE_FILE" in help_text
    assert "owner-only" in help_text and "0600 file" in help_text
    with pytest.raises(SystemExit):
        parser.parse_args([
            "configure", "--bundle", "visible", "--bundle-file", "/tmp/private-pairing",
        ])


def test_configure_reads_private_bundle_file_without_printing_secret(
        tmp_path, monkeypatch, capsys):
    expiry = int(time.time()) + 3600
    bundle = lab.encode_pair(
        url="http://192.0.2.44:8090",
        token=lab.issue_token(
            "secret-padded-past-thirty-two-bytes", user_id="one-owner",
            node_id="goal-file", expires_at=expiry),
        node_id="goal-file", expires_at=expiry,
    )
    path = tmp_path / "pairing.txt"
    path.write_text(bundle + "\n", encoding="utf-8")
    path.chmod(0o600)
    captured = {}
    monkeypatch.setattr(lab, "configure_home", lambda home, pair: captured.update(
        home=home, node=pair["node_id"]))

    result = lab.cmd_configure(argparse.Namespace(
        bundle=None, bundle_file=str(path), home=str(tmp_path / "home"), replace=False))

    assert result == 0
    assert captured == {"home": (tmp_path / "home").resolve(), "node": "goal-file"}
    assert bundle not in capsys.readouterr().out


def test_configure_replace_refuses_runtime_state_from_an_old_worker(
        tmp_path, monkeypatch):
    home = tmp_path / "old-worker-home"
    (home / "run" / "engines").mkdir(parents=True)
    called = False

    def configure(*_args):
        nonlocal called
        called = True

    monkeypatch.setattr(lab, "configure_home", configure)
    pair = _pair(node="replacement")
    bundle = lab.encode_pair(
        url=pair["relay_url"], token=pair["access_token"],
        node_id=pair["node_id"], expires_at=pair["expires_at"])
    with pytest.raises(SystemExit, match="runtime or unknown artifacts"):
        lab.cmd_configure(argparse.Namespace(
            bundle=bundle, bundle_file=None, home=str(home), replace=True))
    assert called is False


def test_configure_replace_allows_only_the_helpers_two_state_files(
        tmp_path, monkeypatch, capsys):
    home = tmp_path / "expired-pairing"
    home.mkdir()
    (home / "state.json").write_text("{}", encoding="utf-8")
    (home / "credentials.toml").write_text("", encoding="utf-8")
    captured = {}
    monkeypatch.setattr(lab, "configure_home", lambda path, pair: captured.update(
        home=path, node=pair["node_id"]))
    pair = _pair(node="replacement")
    bundle = lab.encode_pair(
        url=pair["relay_url"], token=pair["access_token"],
        node_id=pair["node_id"], expires_at=pair["expires_at"])

    assert lab.cmd_configure(argparse.Namespace(
        bundle=bundle, bundle_file=None, home=str(home), replace=True)) == 0
    assert captured == {"home": home.resolve(), "node": "replacement"}
    assert bundle not in capsys.readouterr().out


def test_configure_refuses_group_readable_bundle_file(tmp_path):
    path = tmp_path / "pairing.txt"
    path.write_text("credential", encoding="utf-8")
    path.chmod(0o640)

    with pytest.raises(SystemExit, match="owner-only"):
        lab.cmd_configure(argparse.Namespace(
            bundle=None, bundle_file=str(path), home=str(tmp_path / "home"), replace=False))


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform has no no-follow open flag")
def test_configure_never_follows_a_pairing_bundle_symlink(tmp_path):
    target = tmp_path / "pairing-target.txt"
    target.write_text("credential", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "pairing-link.txt"
    link.symlink_to(target)

    with pytest.raises(SystemExit, match="Could not read private pairing bundle file"):
        lab.cmd_configure(argparse.Namespace(
            bundle=None, bundle_file=str(link), home=str(tmp_path / "home"), replace=False))


def test_configure_bounds_bundle_file_size(tmp_path):
    path = tmp_path / "pairing.txt"
    path.write_bytes(b"x" * (lab.MAX_PAIR_BYTES + 1))
    path.chmod(0o600)

    with pytest.raises(SystemExit, match="exceeds"):
        lab.cmd_configure(argparse.Namespace(
            bundle=None, bundle_file=str(path), home=str(tmp_path / "home"), replace=False))


def test_relay_restart_can_suppress_pairing_credential_output():
    parser = lab.build_parser()
    args = parser.parse_args([
        "relay", "--relay-repo", "/tmp/private-relay", "--reuse", "--no-print-bundle",
    ])

    assert args.reuse is True
    assert args.no_print_bundle is True
    help_text = parser._subparsers._group_actions[0].choices["relay"].format_help()
    assert "--no-print-bundle" in help_text
    assert "credential" in help_text


def test_relay_can_mint_multiple_distinct_joining_worker_identities():
    parser = lab.build_parser()
    args = parser.parse_args([
        "relay", "--relay-repo", "/tmp/private-relay", "--joining-workers", "2",
    ])

    assert args.joining_workers == 2
    help_text = parser._subparsers._group_actions[0].choices["relay"].format_help()
    assert "--joining-workers N" in help_text
    assert "three-machine acceptance" in help_text
    with pytest.raises(SystemExit, match="between 1 and 8"):
        lab.main([
            "relay", "--relay-repo", "/tmp/private-relay", "--joining-workers", "0",
        ])


def test_relay_accepts_one_exact_https_advertised_root():
    parser = lab.build_parser()
    args = parser.parse_args([
        "relay", "--relay-repo", "/tmp/private-relay",
        "--bind-host", "127.0.0.1",
        "--advertise-url", "https://goal-lab.example.test/",
    ])
    assert args.bind_host == "127.0.0.1"
    assert args.advertise_url == "https://goal-lab.example.test/"
    with pytest.raises(SystemExit):
        parser.parse_args([
            "relay", "--relay-repo", "/tmp/private-relay",
            "--advertise-host", "100.64.0.3",
            "--advertise-url", "https://goal-lab.example.test",
        ])


def test_relay_can_supervise_cloudflared_but_not_mix_advertisement_modes():
    parser = lab.build_parser()
    args = parser.parse_args([
        "relay", "--relay-repo", "/tmp/private-relay", "--cloudflared",
    ])
    assert args.cloudflared == "cloudflared"
    explicit = parser.parse_args([
        "relay", "--relay-repo", "/tmp/private-relay",
        "--cloudflared", "/opt/bin/cloudflared",
    ])
    assert explicit.cloudflared == "/opt/bin/cloudflared"
    help_text = parser._subparsers._group_actions[0].choices["relay"].format_help()
    normalized_help = " ".join(help_text.split())
    assert "--cloudflared [PATH]" in normalized_help
    assert "forces the relay listener to loopback" in normalized_help
    with pytest.raises(SystemExit):
        parser.parse_args([
            "relay", "--relay-repo", "/tmp/private-relay",
            "--advertise-url", "https://goal-lab.example.test", "--cloudflared",
        ])


def test_cloudflared_url_pattern_accepts_only_quick_tunnel_https_roots():
    line = "INF +-------------------------------- https://Sane-Cat-42.trycloudflare.com ---+"
    assert lab.CLOUDFLARED_URL.search(line).group(0).lower() == (
        "https://sane-cat-42.trycloudflare.com")
    assert lab.CLOUDFLARED_URL.search("http://unsafe.trycloudflare.com") is None
    assert lab.CLOUDFLARED_URL.search("https://example.test") is None


def test_cloudflared_launcher_extracts_url_and_drains_combined_output(monkeypatch):
    class Tunnel:
        returncode = None
        stdout = iter([
            "INF requesting quick tunnel\n",
            "INF https://Blue-Bird-7.trycloudflare.com ready\n",
        ])

        def poll(self):
            return None

    tunnel = Tunnel()
    launched = {}
    monkeypatch.setattr(
        lab.subprocess, "Popen",
        lambda command, **kwargs: launched.update(command=command, kwargs=kwargs) or tunnel)

    process, url = lab._start_cloudflared_tunnel("/opt/cloudflared", 9876, timeout=1)

    assert process is tunnel
    assert url == "https://blue-bird-7.trycloudflare.com"
    assert launched["command"] == [
        "/opt/cloudflared", "tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:9876",
    ]
    assert launched["kwargs"]["stderr"] is lab.subprocess.STDOUT


def test_cloudflared_launcher_fails_cleanly_when_binary_is_missing(monkeypatch):
    def missing(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(lab.subprocess, "Popen", missing)
    with pytest.raises(SystemExit, match="executable not found"):
        lab._start_cloudflared_tunnel("missing-cloudflared", 8090)


def test_quick_tunnel_reuse_must_reveal_updated_pairing_bundles():
    with pytest.raises(SystemExit, match="new public URL"):
        lab.main([
            "relay", "--relay-repo", "/tmp/private-relay", "--cloudflared",
            "--reuse", "--no-print-bundle",
        ])


def test_pairing_refuses_expiry_identity_mismatch_and_oversize():
    expired = int(time.time()) - 1
    token = lab.issue_token(
        "secret-padded-past-thirty-two-bytes", user_id="owner",
        node_id="goal-a", expires_at=expired)
    bundle = lab.encode_pair(
        url="http://192.0.2.44:8090", token=token, node_id="goal-a", expires_at=expired)
    with pytest.raises(ValueError, match="expired"):
        lab.decode_pair(bundle)

    live = int(time.time()) + 3600
    token = lab.issue_token(
        "secret-padded-past-thirty-two-bytes", user_id="owner",
        node_id="goal-a", expires_at=live)
    mismatched = lab.encode_pair(
        url="http://192.0.2.44:8090", token=token, node_id="goal-b", expires_at=live)
    with pytest.raises(ValueError, match="does not match"):
        lab.decode_pair(mismatched)
    with pytest.raises(ValueError, match="unexpectedly large"):
        lab.decode_pair("x" * 16_385)


@pytest.mark.parametrize("url", [
    "file:///tmp/relay", "http://user:secret@example.test:8090", "http://host/x",
    "http://host:99999", "http://host:8090?token=x",
])
def test_pairing_refuses_non_root_or_credential_bearing_relay_urls(url):
    with pytest.raises(ValueError):
        lab.validate_relay_url(url)


def test_configure_home_writes_an_isolated_remote_grid(tmp_path, monkeypatch):
    home = tmp_path / "joining-worker"
    original = os.environ.get("GRID_HOME")
    lab.configure_home(home, _pair())

    # The helper restores its caller's environment; pairing one test node must not redirect the
    # terminal from which the helper itself was invoked.
    assert os.environ.get("GRID_HOME") == original
    monkeypatch.setenv("GRID_HOME", str(home))
    from remote import credentials
    from shared import state

    saved = credentials.load_credentials()
    assert saved["session_token"] == "physical-goal-lab"
    assert saved["networks"][0]["network_id"] == lab.NETWORK_ID
    assert saved["networks"][0]["signaling_url"] == "http://192.0.2.44:8090"
    assert state.get_mode() == "remote"
    assert state.get_active("remote") == lab.NETWORK_ID
    assert stat.S_IMODE((home / "credentials.toml").stat().st_mode) == 0o600


def test_prepare_relay_mints_distinct_machine_identities_and_private_files(
        tmp_path, monkeypatch):
    relay_repo = tmp_path / "private-relay"
    (relay_repo / "grid_cli" / "private_server").mkdir(parents=True)
    relay_python = relay_repo / ".venv" / "bin" / "python"
    relay_python.parent.mkdir(parents=True)
    relay_python.write_text("", encoding="utf-8")
    root = tmp_path / "physical-state"
    monkeypatch.setattr(lab, "discover_lan_host", lambda: "192.0.2.88")
    args = argparse.Namespace(
        root=str(root), relay_repo=str(relay_repo), reuse=False, advertise_host=None,
        port=8090, token_hours=48, lease_seconds=120, reaper_seconds=5,
        claim_timeout_seconds=30, joining_workers=2)

    prepared_root, env, raw = lab.prepare_relay(args)
    metadata = lab.json.loads(raw)
    workers = [lab.decode_pair(bundle) for bundle in metadata["worker_pairs"]]
    worker = workers[0]
    monkeypatch.setenv("GRID_HOME", metadata["relay_home"])
    from remote import credentials
    b_token = credentials.load_credentials()["networks"][0]["access_token"]
    b_claims = lab.token_claims(b_token)
    worker_claims = lab.token_claims(worker["access_token"])

    assert prepared_root == root
    assert metadata["url"] == "http://192.0.2.88:8090"
    assert worker_claims["user_id"] == b_claims["user_id"]
    assert worker_claims["node_id"] != b_claims["node_id"]
    assert metadata["worker_node_id"] == worker_claims["node_id"]
    assert len(metadata["worker_node_ids"]) == len(workers) == 2
    assert len({pair["node_id"] for pair in workers}) == 2
    assert b_claims["node_id"] not in {pair["node_id"] for pair in workers}
    assert metadata["relay_node_id"] == b_claims["node_id"]
    assert env["TASK_REPO_ROOT"] == str(root / "projects")
    assert env["GRID_MODE"] == "false"
    identity = lab.json.loads((root / "identity.json").read_text(encoding="utf-8"))
    assert set(identity) == {"user_id", "worker_node_ids", "relay_node_id"}
    assert identity["worker_node_ids"] == metadata["worker_node_ids"]
    assert metadata["relay_home"] == str(root / "grid-home-relay")
    assert stat.S_IMODE((root / "jwt-secret").stat().st_mode) == 0o600
    assert stat.S_IMODE((root / "joining-worker-pairing.txt").stat().st_mode) == 0o600
    assert stat.S_IMODE((root / "joining-worker-1-pairing.txt").stat().st_mode) == 0o600
    assert stat.S_IMODE((root / "joining-worker-2-pairing.txt").stat().st_mode) == 0o600


def test_prepare_relay_preserves_https_advertised_url_for_remote_workers(
        tmp_path, monkeypatch):
    relay_repo = tmp_path / "private-relay"
    (relay_repo / "grid_cli" / "private_server").mkdir(parents=True)
    relay_python = relay_repo / ".venv" / "bin" / "python"
    relay_python.parent.mkdir(parents=True)
    relay_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        lab, "discover_lan_host",
        lambda: pytest.fail("an exact advertised URL must not inspect a LAN address"))
    args = argparse.Namespace(
        root=str(tmp_path / "physical-state"), relay_repo=str(relay_repo), reuse=False,
        advertise_host=None, advertise_url="https://goal-lab.example.test/", port=8090,
        token_hours=48, lease_seconds=120, reaper_seconds=5, claim_timeout_seconds=30,
        joining_workers=2,
    )

    _root, _env, raw = lab.prepare_relay(args)
    metadata = lab.json.loads(raw)
    assert metadata["url"] == "https://goal-lab.example.test"
    assert all(lab.decode_pair(pair)["relay_url"] == metadata["url"]
               for pair in metadata["worker_pairs"])


def test_relay_banner_prints_both_distinct_nonsecret_node_ids(tmp_path, monkeypatch, capsys):
    """A copied GRID_HOME can make two process names overwrite one signed node registration.

    The acceptance operator needs the credential-bound ids before any worker starts; hostnames and
    ``--name`` labels cannot prove two nodes.  The banner may print those ids but never the bundle
    when ``--no-print-bundle`` is selected.
    """
    class RelayProcess:
        returncode = None

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    secret_bundle = "pairing-secret-must-stay-hidden"
    monkeypatch.setattr(lab, "prepare_relay", lambda _args: (
        tmp_path,
        {},
        lab.json.dumps({
            "url": "http://192.0.2.88:8090",
            "worker_pair": secret_bundle,
            "worker_node_id": "goal-worker-distinct",
            "relay_node_id": "goal-relay-distinct",
            "relay_home": str(tmp_path / "grid-home-relay"),
            "server_dir": str(tmp_path),
            "python": "/fake/python",
            "relay_revision": "abc123",
        }),
    ))
    monkeypatch.setattr(lab.subprocess, "Popen", lambda *_args, **_kwargs: RelayProcess())
    health_probes = []
    monkeypatch.setattr(
        lab, "_wait_for_health",
        lambda *args, **kwargs: health_probes.append((args, kwargs)))

    assert lab.cmd_relay(argparse.Namespace(
        bind_host="0.0.0.0", port=8090, no_print_bundle=True)) == 0
    out = capsys.readouterr().out
    assert "relay id:    goal-relay-distinct" in out
    assert "worker 1 id: goal-worker-distinct" in out
    assert secret_bundle not in out
    assert health_probes[0][0][1] == "http://127.0.0.1:8090"


def test_relay_banner_assigns_one_bundle_to_each_joining_worker(
        tmp_path, monkeypatch, capsys):
    class RelayProcess:
        returncode = None

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(lab, "prepare_relay", lambda _args: (
        tmp_path,
        {},
        lab.json.dumps({
            "url": "http://192.0.2.88:8090",
            "worker_pair": "bundle-b",
            "worker_node_id": "goal-worker-b",
            "worker_pairs": ["bundle-b", "bundle-c"],
            "worker_node_ids": ["goal-worker-b", "goal-worker-c"],
            "relay_node_id": "goal-relay-a",
            "relay_home": str(tmp_path / "grid-home-relay"),
            "server_dir": str(tmp_path),
            "python": "/fake/python",
            "relay_revision": "abc123",
        }),
    ))
    monkeypatch.setattr(lab.subprocess, "Popen", lambda *_args, **_kwargs: RelayProcess())
    monkeypatch.setattr(lab, "_wait_for_health", lambda *_args, **_kwargs: None)

    assert lab.cmd_relay(argparse.Namespace(
        bind_host="0.0.0.0", port=8090, no_print_bundle=False)) == 0
    out = capsys.readouterr().out
    assert "worker 1 id: goal-worker-b" in out
    assert "worker 2 id: goal-worker-c" in out
    assert out.count("bundle-b") == out.count("bundle-c") == 1
    assert "--home /private/tmp/grid-goal-worker-1" in out
    assert "--home /private/tmp/grid-goal-worker-2" in out


def test_cloudflared_relay_uses_loopback_checks_public_health_and_stops_tunnel(
        tmp_path, monkeypatch, capsys):
    class Process:
        returncode = None

        def __init__(self, kind):
            self.kind = kind
            self.signals = []
            self.running = True

        def wait(self, timeout=None):
            if timeout is None and self.kind == "relay":
                self.running = False
            return 0

        def poll(self):
            return None if self.running else 0

        def send_signal(self, value):
            self.signals.append(value)
            self.running = False

        def kill(self):
            self.running = False

    tunnel = Process("tunnel")
    relay = Process("relay")
    prepared = {}
    launched = {}
    health = []
    public_health = []

    def prepare(args):
        prepared["url"] = args.advertise_url
        return tmp_path, {}, lab.json.dumps({
            "url": args.advertise_url,
            "worker_pair": "bundle-b",
            "worker_node_id": "goal-worker-b",
            "relay_node_id": "goal-relay-c",
            "relay_home": str(tmp_path / "grid-home-relay"),
            "server_dir": str(tmp_path),
            "python": "/fake/python",
            "relay_revision": "abc123",
        })

    monkeypatch.setattr(
        lab, "_start_cloudflared_tunnel",
        lambda executable, port: (tunnel, "https://quick-test.trycloudflare.com"))
    monkeypatch.setattr(lab, "prepare_relay", prepare)
    monkeypatch.setattr(
        lab.subprocess, "Popen",
        lambda command, **kwargs: launched.update(command=command, kwargs=kwargs) or relay)
    monkeypatch.setattr(lab, "_wait_for_health", lambda *args: health.append(args))
    monkeypatch.setattr(
        lab, "_wait_for_public_health", lambda *args: public_health.append(args))

    args = lab.build_parser().parse_args([
        "relay", "--relay-repo", "/tmp/private-relay", "--joining-workers", "2",
        "--cloudflared",
    ])
    assert lab.cmd_relay(args) == 0
    assert prepared["url"] == "https://quick-test.trycloudflare.com"
    assert launched["command"][launched["command"].index("--host") + 1] == "127.0.0.1"
    assert health and public_health == [(
        relay, tunnel, "https://quick-test.trycloudflare.com")]
    assert tunnel.signals == [lab.signal.SIGTERM]
    assert "relay:       https://quick-test.trycloudflare.com" in capsys.readouterr().out


def test_prepare_relay_reuses_legacy_ab_identity_by_role(tmp_path, monkeypatch):
    relay_repo = tmp_path / "private-relay"
    (relay_repo / "grid_cli" / "private_server").mkdir(parents=True)
    relay_python = relay_repo / ".venv" / "bin" / "python"
    relay_python.parent.mkdir(parents=True)
    relay_python.write_text("", encoding="utf-8")
    root = tmp_path / "legacy-state"
    root.mkdir()
    (root / "jwt-secret").write_text("secret-padded-past-thirty-two-bytes", encoding="utf-8")
    (root / "identity.json").write_text(lab.json.dumps({
        "user_id": "legacy-owner", "node_a": "legacy-worker", "node_b": "legacy-relay",
    }), encoding="utf-8")
    monkeypatch.setattr(lab, "discover_lan_host", lambda: "192.0.2.88")

    _, _, raw = lab.prepare_relay(argparse.Namespace(
        root=str(root), relay_repo=str(relay_repo), reuse=True, advertise_host=None,
        port=8090, token_hours=48, lease_seconds=120, reaper_seconds=5,
        claim_timeout_seconds=30))

    metadata = lab.json.loads(raw)
    worker = lab.decode_pair(metadata["worker_pair"])
    monkeypatch.setenv("GRID_HOME", metadata["relay_home"])
    from remote import credentials
    relay_claims = lab.token_claims(credentials.load_credentials()["networks"][0]["access_token"])
    assert worker["node_id"] == "legacy-worker"
    assert relay_claims["node_id"] == "legacy-relay"


def test_prepare_relay_reuse_preserves_all_joining_worker_ids(tmp_path, monkeypatch):
    relay_repo = tmp_path / "private-relay"
    (relay_repo / "grid_cli" / "private_server").mkdir(parents=True)
    relay_python = relay_repo / ".venv" / "bin" / "python"
    relay_python.parent.mkdir(parents=True)
    relay_python.write_text("", encoding="utf-8")
    root = tmp_path / "reused-state"
    monkeypatch.setattr(lab, "discover_lan_host", lambda: "192.0.2.88")
    common = dict(
        root=str(root), relay_repo=str(relay_repo), advertise_host=None,
        port=8090, token_hours=48, lease_seconds=120, reaper_seconds=5,
        claim_timeout_seconds=30,
    )

    _, _, first_raw = lab.prepare_relay(argparse.Namespace(
        **common, reuse=False, joining_workers=2))
    first = lab.json.loads(first_raw)
    _, _, second_raw = lab.prepare_relay(argparse.Namespace(
        **common, reuse=True, joining_workers=None))
    second = lab.json.loads(second_raw)

    assert second["worker_node_ids"] == first["worker_node_ids"]
    assert len(second["worker_pairs"]) == 2
    assert [lab.decode_pair(bundle)["node_id"] for bundle in second["worker_pairs"]] == (
        first["worker_node_ids"])
