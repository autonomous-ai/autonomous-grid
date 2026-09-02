from __future__ import annotations

import ctypes
import socket
from types import SimpleNamespace

import pytest

from cli import _main as cli_main
from local import config, runtime
from local.allocator_node import AllocatorNodeAgent
from shared.system import host, hostsignals


def test_server_spawn_persists_nonce_birth_marker_and_argv(tmp_path, monkeypatch):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    cfg = runtime.init_grid_config(
        name="home", port=48090, advertise_host="192.168.1.4"
    )
    captured: dict[str, object] = {}

    class Process:
        pid = 42_424

    def spawn(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Process()

    monkeypatch.setattr(runtime, "_tcp_port_in_use", lambda _host, _port: False)
    monkeypatch.setattr(runtime, "_cli_subprocess_command", lambda: ["grid"])
    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    monkeypatch.setattr(
        runtime,
        "_stamp_server",
        lambda pid: {
            "server_pid": pid,
            "server_pid_start_time": None,
            "server_pgid": None,
        },
    )
    monkeypatch.setattr(
        runtime, "_capture_process_start_marker", lambda _pid: "proc:123456"
    )
    monkeypatch.setattr(runtime, "wait_for_health", lambda _cfg: None)

    assert runtime.start_grid(cfg) == Process.pid
    persisted = config.load_grid_config(cfg["grid_id"])
    command = captured["command"]
    assert isinstance(command, list)
    assert command[:3] == ["grid", "__server", cfg["grid_id"]]
    instance_index = command.index("--instance-id") + 1
    assert command[instance_index] == persisted["server_instance_id"]
    assert len(persisted["server_instance_id"]) == 32
    assert persisted["server_start_marker"] == "proc:123456"
    assert persisted["server_pid"] == Process.pid


def test_stop_server_fails_closed_when_persisted_identity_is_ambiguous(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    cfg = runtime.init_grid_config(
        name="home", port=48090, advertise_host="192.168.1.4"
    )
    cfg.update(
        server_pid=42_424,
        server_instance_id="nonce-a",
        server_start_marker="proc:old-birth",
    )
    config.save_grid_config(cfg["grid_id"], cfg)
    monkeypatch.setattr(runtime.run_records, "pid_alive", lambda _pid: True)
    monkeypatch.setattr(runtime.run_records, "process_matches", lambda *_a, **_kw: False)
    signaled: list[int] = []
    monkeypatch.setattr(
        runtime.run_records,
        "terminate_pid",
        lambda pid: signaled.append(pid) or True,
    )

    with pytest.raises(SystemExit, match="ownership cannot be proven"):
        runtime.stop_grid(cfg)

    assert signaled == []
    persisted = config.load_grid_config(cfg["grid_id"])
    assert persisted["server_pid"] == 42_424
    assert persisted["server_start_marker"] == "proc:old-birth"


def test_start_server_also_refuses_to_replace_an_ambiguous_live_pid(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    cfg = runtime.init_grid_config(
        name="home", port=48090, advertise_host="192.168.1.4"
    )
    cfg.update(server_pid=42_424, server_instance_id="", server_start_marker="")
    config.save_grid_config(cfg["grid_id"], cfg)
    monkeypatch.setattr(runtime.run_records, "pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        runtime,
        "wait_for_health",
        lambda *_a, **_kw: (_ for _ in ()).throw(SystemExit()),
    )
    spawned: list[object] = []
    original_popen = runtime.subprocess.Popen

    def capture_server(command, *args, **kwargs):
        if "__server" in command:
            spawned.append(object())
        return original_popen(command, *args, **kwargs)

    monkeypatch.setattr(runtime.subprocess, "Popen", capture_server)

    with pytest.raises(SystemExit, match="cannot be proven"):
        runtime.start_grid(cfg)

    assert spawned == []


def test_stop_server_verifies_nonce_argv_and_birth_before_cross_platform_stop(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    cfg = runtime.init_grid_config(
        name="home", port=48090, advertise_host="192.168.1.4"
    )
    cfg.update(
        server_pid=42_424,
        server_instance_id="nonce-a",
        server_start_marker="proc:123456",
    )
    config.save_grid_config(cfg["grid_id"], cfg)
    monkeypatch.setattr(runtime.run_records, "pid_alive", lambda _pid: True)
    checks: list[tuple[int, tuple[str, ...], str | None]] = []

    def matches(pid, *, required_args, start_marker=None):
        checks.append((pid, required_args, start_marker))
        return True

    stopped: list[int] = []
    monkeypatch.setattr(runtime.run_records, "process_matches", matches)
    monkeypatch.setattr(
        runtime.run_records,
        "terminate_pid",
        lambda pid, **_kwargs: stopped.append(pid) or True,
    )

    runtime.stop_grid(cfg)

    assert checks == [
        (
            42_424,
            ("__server", cfg["grid_id"], "--instance-id", "nonce-a"),
            "proc:123456",
        )
    ]
    assert stopped == [42_424]
    persisted = config.load_grid_config(cfg["grid_id"])
    assert persisted["server_pid"] == 0
    assert persisted["server_instance_id"] == ""
    assert persisted["server_start_marker"] == ""


def test_internal_server_cli_requires_and_forwards_instance_nonce(monkeypatch):
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        cli_main,
        "cmd_internal_server",
        lambda grid_id, instance_id=None: calls.append((grid_id, instance_id)) or 0,
    )

    assert (
        cli_main._maybe_internal(
            ["__server", "ag-home", "--instance-id", "nonce-a"]
        )
        == 0
    )
    assert calls == [("ag-home", "nonce-a")]


def test_linux_memory_probe_uses_memavailable_instead_of_claiming_zero_pressure(
    monkeypatch,
):
    meminfo = """\
MemTotal:       1000000 kB
MemFree:         100000 kB
MemAvailable:    250000 kB
Buffers:          10000 kB
Cached:          200000 kB
"""
    monkeypatch.setattr(host.Path, "read_text", lambda *_a, **_kw: meminfo)

    total, available, used = host._linux_memory_snapshot() or (0, 0, 0.0)

    assert total == 1_000_000 * 1024
    assert available == 250_000 * 1024
    assert used == 75.0


def test_macos_memory_probe_uses_free_inactive_and_speculative_pages(monkeypatch):
    monkeypatch.setattr(host, "_sysctl", lambda _name: str(1_024_000))
    vm_stat = b"""\
Mach Virtual Memory Statistics: (page size of 4096 bytes)
Pages free:                              100.
Pages inactive:                          50.
Pages speculative:                       25.
Pages active:                            75.
"""
    monkeypatch.setattr(host.subprocess, "check_output", lambda *_a, **_kw: vm_stat)

    total, available, used = host._macos_memory_snapshot() or (0, 0, 0.0)

    assert total == 1_024_000
    assert available == 175 * 4096
    assert used == 30.0


def test_total_only_memory_probe_reports_pressure_as_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(host, "_memory_snapshot", lambda: (8 * 1024**3, None, None))

    info = host.gather(str(tmp_path))

    assert info.memory_total_gb == 8.0
    assert info.memory_available_gb is None
    assert info.memory_percent is None


def test_windows_memory_probe_uses_global_memory_status(monkeypatch):
    class Kernel32:
        @staticmethod
        def GlobalMemoryStatusEx(pointer):
            status = pointer._obj
            status.ullTotalPhys = 16 * 1024**3
            status.ullAvailPhys = 4 * 1024**3
            return 1

    monkeypatch.setattr(
        ctypes,
        "windll",
        SimpleNamespace(kernel32=Kernel32()),
        raising=False,
    )

    assert host._windows_memory_snapshot() == (16 * 1024**3, 4 * 1024**3, 75.0)


def test_linux_ipv6_only_default_route_is_available(monkeypatch):
    ipv6_default = " ".join(
        [
            "0" * 32,
            "00",
            "0" * 32,
            "00",
            "fe800000000000000000000000000001",
            "00000400",
            "00000000",
            "00000000",
            "00000003",
            "eth0",
        ]
    )

    def read_text(path):
        value = str(path)
        if value == "/proc/net/route":
            return "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT"
        if value == "/proc/net/ipv6_route":
            return ipv6_default
        if value == "/sys/class/net/eth0/carrier":
            return "1"
        return ""

    monkeypatch.setattr(hostsignals.platform, "system", lambda: "Linux")
    monkeypatch.setattr(hostsignals, "_read_text", read_text)

    assert hostsignals._network_available(0.1) is True


def test_macos_ipv6_only_default_route_is_available(monkeypatch):
    calls: list[list[str]] = []

    def run(command, _timeout):
        calls.append(command)
        if command == ["route", "-n", "get", "-inet6", "default"]:
            return "route to: default\ninterface: en0"
        return ""

    monkeypatch.setattr(hostsignals.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(hostsignals, "_run", run)
    monkeypatch.setattr(hostsignals.shutil, "which", lambda _name: "/sbin/route")

    assert hostsignals._network_available(0.1) is True
    assert calls == [
        ["route", "-n", "get", "default"],
        ["route", "-n", "get", "-inet6", "default"],
    ]


def test_windows_ipv6_only_default_route_is_available(monkeypatch):
    calls: list[list[str]] = []

    def run(command, _timeout):
        calls.append(command)
        if command == ["route", "print", "-6", "::/0"]:
            return " 11    25 ::/0                     fe80::1"
        return ""

    monkeypatch.setattr(hostsignals.platform, "system", lambda: "Windows")
    monkeypatch.setattr(hostsignals, "_run", run)
    monkeypatch.setattr(hostsignals.shutil, "which", lambda _name: "C:/Windows/route.exe")

    assert hostsignals._network_available(0.1) is True
    assert calls == [
        ["route", "print", "0.0.0.0"],
        ["route", "print", "-6", "::/0"],
    ]


class _RouteSocket:
    def __init__(self, local_host: str):
        self.local_host = local_host

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def connect(self, _destination):
        return None

    def getsockname(self):
        return (self.local_host, 50_000)


def test_advertise_ip_follows_route_to_configured_grid(monkeypatch):
    monkeypatch.setattr(
        runtime.socket,
        "getaddrinfo",
        lambda *_a, **_kw: [
            (socket.AF_INET, socket.SOCK_DGRAM, 17, "", ("10.20.0.8", 8090))
        ],
    )
    monkeypatch.setattr(
        runtime.socket, "socket", lambda *_a, **_kw: _RouteSocket("10.20.0.44")
    )

    assert runtime.detect_local_ip_for_url("http://grid.internal:8090") == "10.20.0.44"


def test_remote_grid_never_gets_an_unreachable_loopback_advertisement(monkeypatch):
    monkeypatch.setattr(
        runtime.socket,
        "getaddrinfo",
        lambda *_a, **_kw: [
            (socket.AF_INET, socket.SOCK_DGRAM, 17, "", ("10.20.0.8", 8090))
        ],
    )
    monkeypatch.setattr(
        runtime.socket, "socket", lambda *_a, **_kw: _RouteSocket("127.0.0.1")
    )

    with pytest.raises(SystemExit, match="--advertise-host"):
        runtime.detect_local_ip_for_url("http://grid.internal:8090")


def test_ipv6_advertise_hosts_are_bracketed_in_urls(tmp_path):
    assert runtime.make_local_url(8090, "2001:db8::44") == "http://[2001:db8::44]:8090"
    managed = SimpleNamespace(
        state_path=tmp_path / "node.json",
        host_id="host-a",
        residencies=(),
    )
    agent = AllocatorNodeAgent(
        grid_url="https://grid.example",
        control_token="node-token",
        runtime=managed,
        advertise_host="2001:db8::44",
    )
    try:
        # Keep the canonical host raw internally; URL construction brackets/zone-encodes once.
        assert agent.advertise_host == "2001:db8::44"
    finally:
        agent.client.close()
