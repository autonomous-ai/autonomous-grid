from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import cli
from cli import allocator
from cli import dispatch as cli_dispatch
from local import config, runtime
from shared import jsonio, paths, run_records
from shared.allocator.auth import decode_node_token, verify_node_token
from shared.allocator.runtime import local_override_path, shutdown_request_path


def grid_config(*, managed: bool = True) -> dict:
    return {
        "grid_id": "ag-test",
        "name": "test",
        "managed_server": managed,
        "port": 8090,
        "lan_signaling_url": "http://127.0.0.1:8090",
        "allocator_control_token": "secret-token" if managed else "",
    }


def response(status: int = 200, payload: dict | None = None) -> httpx.Response:
    request = httpx.Request("GET", "http://127.0.0.1")
    return httpx.Response(status, json=payload or {}, request=request)


def patch_http_client(monkeypatch, request):
    class Client:
        def __init__(self, **kwargs):
            self.options = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def request(self, method, url, **kwargs):
            return request(method, url, **kwargs)

    monkeypatch.setattr(allocator.httpx, "Client", Client)


def test_parser_exposes_complete_allocator_surface():
    parser = cli.build_parser()
    assert parser.parse_args(
        ["allocator", "join", "forge"]
    ).handler is cli.cmd_allocator_join
    assert parser.parse_args(
        ["allocator", "join", "forge", "--dedicated", "--restart"]
    ).restart is True
    assert parser.parse_args(["allocator", "status"]).handler is cli.cmd_allocator_status
    set_args = parser.parse_args(
        [
            "allocator",
            "model",
            "set",
            "qwen.gguf",
            "--memory-mb",
            "8000",
            "--min-replicas",
            "0",
            "--max-replicas",
            "3",
            "--backend",
            "metal",
        ]
    )
    assert set_args.handler is cli.cmd_allocator_model_set
    assert set_args.runtimes is None
    assert set_args.backends == ["metal"]
    explicit_runtime_args = parser.parse_args(
        [
            "allocator",
            "model",
            "set",
            "qwen.gguf",
            "--memory-mb",
            "8000",
            "--runtime",
            "vllm",
        ]
    )
    assert explicit_runtime_args.runtimes == ["vllm"]
    assert parser.parse_args(
        ["allocator", "model", "remove", "qwen.gguf"]
    ).handler is cli.cmd_allocator_model_remove
    assert parser.parse_args(
        ["allocator", "mode", "automatic"]
    ).handler is cli.cmd_allocator_mode
    assert parser.parse_args(["allocator", "tick"]).handler is cli.cmd_allocator_tick
    assert parser.parse_args(
        ["allocator", "token", "write", "/tmp/token"]
    ).handler is cli.cmd_allocator_token_write
    assert parser.parse_args(
        ["allocator", "node", "start"]
    ).handler is cli.cmd_allocator_node_start
    assert parser.parse_args(
        ["allocator", "node", "stop"]
    ).handler is cli.cmd_allocator_node_stop
    assert parser.parse_args(
        ["allocator", "node", "status"]
    ).handler is cli.cmd_allocator_node_status
    assert parser.parse_args(
        ["allocator", "node", "pause"]
    ).handler is cli.cmd_allocator_node_override
    assert parser.parse_args(
        ["allocator", "node", "resume"]
    ).handler is cli.cmd_allocator_node_resume


def test_allocator_is_remote_gated():
    args = cli.build_parser().parse_args(["allocator", "status"])
    with pytest.raises(SystemExit, match="isn't available in remote mode"):
        cli_dispatch.dispatch(args, "remote")


def test_allocator_join_is_the_remote_provider_opt_in(monkeypatch):
    args = cli.build_parser().parse_args(["allocator", "join", "forge"])
    monkeypatch.setattr(args, "handler", lambda value: int(value.grid == "forge"))

    assert cli_dispatch.dispatch(args, "remote") == 1


def test_allocator_join_reuses_remote_membership_and_keeps_node_token_in_memory(
    monkeypatch, tmp_path
):
    from remote import credentials
    from cli import remote_grid

    args = cli.build_parser().parse_args(
        ["allocator", "join", "forge", "--dedicated"]
    )
    rec = {
        "network_id": "grid-forge",
        "name": "forge",
        "access_token": "existing-membership",
    }
    monkeypatch.setattr(credentials, "require_session", lambda: "session")
    monkeypatch.setattr(remote_grid, "_select", lambda _value: rec)
    monkeypatch.setattr(remote_grid, "_network_id", lambda _rec: "grid-forge")
    monkeypatch.setattr(
        remote_grid,
        "resolve_relay_base",
        lambda *_args: ("https://forge.example", {}),
    )
    monkeypatch.setattr(
        allocator, "_remote_provider_network_id", lambda value: value
    )
    monkeypatch.setattr(
        allocator,
        "_request",
        lambda *_args, **_kwargs: {"grid_id": "allocator-control"},
    )
    node_token = "grid-node-v1.payload.signature"
    monkeypatch.setattr(
        allocator,
        "_request_remote_enrollment",
        lambda relay, token, label: node_token,
    )
    monkeypatch.setattr(
        allocator,
        "_node_record_path",
        lambda _scope: tmp_path / "allocator-node.json",
    )
    monkeypatch.setattr("cli.engine.ensure_allocator_llama_cpp", lambda: False)
    captured = {}

    def start(_args, **kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(allocator, "_start_allocator_node_locked", start)

    assert allocator.cmd_allocator_join(args) == 0
    assert captured["cfg"]["lan_signaling_url"] == (
        "https://forge.example/allocator-control"
    )
    assert captured["supplied_node_token"] == node_token
    assert args.provider_grid == "grid-forge"
    assert args.dedicated is True
    assert args.token_file is None


def test_allocator_join_verifies_runtime_before_requesting_enrollment(
    monkeypatch, tmp_path
):
    from cli import remote_grid
    from remote import credentials

    args = cli.build_parser().parse_args(["allocator", "join", "forge"])
    rec = {
        "network_id": "grid-forge",
        "name": "forge",
        "access_token": "existing-membership",
    }
    monkeypatch.setattr(credentials, "require_session", lambda: "session")
    monkeypatch.setattr(remote_grid, "_select", lambda _value: rec)
    monkeypatch.setattr(remote_grid, "_network_id", lambda _rec: "grid-forge")
    monkeypatch.setattr(
        remote_grid,
        "resolve_relay_base",
        lambda *_args: ("https://forge.example", {}),
    )
    monkeypatch.setattr(allocator, "_remote_provider_network_id", lambda value: value)
    monkeypatch.setattr(
        allocator,
        "_request",
        lambda *_args, **_kwargs: {"grid_id": "allocator-control"},
    )
    monkeypatch.setattr(
        allocator,
        "_node_record_path",
        lambda _scope: tmp_path / "allocator-node.json",
    )
    calls: list[str] = []
    monkeypatch.setattr(
        "cli.engine.ensure_allocator_llama_cpp",
        lambda: calls.append("runtime") or True,
    )
    monkeypatch.setattr(
        allocator,
        "_request_remote_enrollment",
        lambda *_args: calls.append("enroll") or "grid-node-v1.payload.signature",
    )
    monkeypatch.setattr(
        allocator,
        "_start_allocator_node_locked",
        lambda *_args, **_kwargs: calls.append("start") or 0,
    )

    assert allocator.cmd_allocator_join(args) == 0
    assert calls == ["runtime", "enroll", "start"]


def test_remote_enrollment_retries_until_provider_registration_is_visible(monkeypatch):
    node_token = allocator.mint_node_token(
        "operator-secret", "host-d", now=1_000, token_id="token-d"
    )
    replies = [
        response(409, {"detail": "This machine must join the Grid as a provider before allocator enrollment"}),
        response(409, {"detail": "This machine must join the Grid as a provider before allocator enrollment"}),
        response(200, {"node_token": node_token, "host_id": "host-d"}),
    ]
    now = [0.0]
    sleeps: list[float] = []

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, *_args, **_kwargs):
            return replies.pop(0)

    monkeypatch.setattr(allocator.httpx, "Client", Client)
    monkeypatch.setattr(allocator.time, "monotonic", lambda: now[0])

    def advance(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(allocator.time, "sleep", advance)

    assert allocator._request_remote_enrollment(
        "https://forge.example", "access", "forge"
    ) == node_token
    assert sleeps == [0.25, 0.25]
    assert replies == []


def test_remote_enrollment_does_not_retry_an_unrelated_conflict(monkeypatch):
    calls = []

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, *_args, **_kwargs):
            calls.append("post")
            return response(409, {"detail": "provider identity is already bound"})

    monkeypatch.setattr(allocator.httpx, "Client", Client)
    monkeypatch.setattr(
        allocator.time,
        "sleep",
        lambda _seconds: pytest.fail("an unrelated 409 must fail immediately"),
    )

    with pytest.raises(SystemExit, match="already bound"):
        allocator._request_remote_enrollment(
            "https://forge.example", "access", "forge"
        )
    assert calls == ["post"]


def test_remote_enrollment_provider_wait_is_bounded(monkeypatch):
    attempts = []
    now = [0.0]

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, *_args, **_kwargs):
            attempts.append(now[0])
            return response(
                409,
                {"detail": "This machine must join the Grid as a provider before allocator enrollment"},
            )

    monkeypatch.setattr(allocator.httpx, "Client", Client)
    monkeypatch.setattr(allocator, "REMOTE_ENROLLMENT_READY_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(allocator.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        allocator.time,
        "sleep",
        lambda seconds: now.__setitem__(0, now[0] + seconds),
    )

    with pytest.raises(SystemExit, match="must join the Grid as a provider"):
        allocator._request_remote_enrollment(
            "https://forge.example", "access", "forge"
        )
    assert attempts == [0.0, 0.25, 0.5]


def test_allocator_runtime_bootstrap_installs_and_verifies_pinned_llama(monkeypatch):
    from cli import engine
    from shared.engine import launcher

    resolutions = [SystemExit("llama-server not found. Run install."), "/grid/bin/llama-server"]
    installs = []

    def resolve():
        value = resolutions.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(launcher, "llama_server_path", resolve)
    monkeypatch.setattr(
        engine,
        "_install_llama_cpp",
        lambda args: installs.append((args.target_sm, args.from_source)) or 0,
    )

    assert engine.ensure_allocator_llama_cpp() is True
    assert installs == [(None, False)]
    assert resolutions == []


def test_allocator_runtime_bootstrap_preserves_invalid_explicit_override(monkeypatch):
    from cli import engine
    from shared.engine import launcher

    monkeypatch.setattr(
        launcher,
        "llama_server_path",
        lambda: (_ for _ in ()).throw(
            SystemExit("LLAMA_SERVER is set but not an executable file: /bad")
        ),
    )
    monkeypatch.setattr(
        engine,
        "_install_llama_cpp",
        lambda _args: pytest.fail("must not install around an explicit broken override"),
    )

    with pytest.raises(SystemExit, match="LLAMA_SERVER"):
        engine.ensure_allocator_llama_cpp()


def test_detached_allocator_child_uses_resolvable_remote_control_url():
    cfg = grid_config(managed=False)
    cfg["grid_id"] = "grid-forge"
    cfg["lan_signaling_url"] = "https://forge.example/allocator-control"

    assert allocator._allocator_node_selector(cfg) == (
        "https://forge.example/allocator-control"
    )
    assert allocator._allocator_node_selector(grid_config()) == "ag-test"


def test_status_prints_summary_or_json_without_control_token(monkeypatch, capsys):
    monkeypatch.setattr(config, "select_grid", lambda _value: grid_config())
    payload = {
        "mode": "recommend",
        "last_tick_at": 123.5,
        "last_error": "",
        "nodes": [
            {
                "node_id": "host-a",
                "state": "accepting",
                "capacity_mb": 16_000,
                "residencies": [{"model_id": "qwen.gguf", "state": "ready"}],
            }
        ],
        "models": [{"model_id": "qwen.gguf"}],
        "pending_commands": [],
        "plan": {
            "unsatisfied": [],
        },
        "portfolio_policy": {
            "joint": True,
            "objective": "resource pressure then request coverage",
            "workloads": 2,
            "selected_models": ["general"],
            "exploration_models": ["general"],
        },
        "portfolio_admissions": [
            {
                "workload": "video",
                "state": "starting",
                "model_id": "video-model",
                "ready_replicas": 0,
                "desired_replicas": 1,
                "reason": "selected capacity is loading or warming",
            }
        ],
        "portfolio_projections": [
            {
                "workload": "video",
                "demand_correlation_sources": ["image"],
                "demand_correlation_confidence": 6 / 7,
            }
        ],
    }
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return response(payload=payload)

    patch_http_client(monkeypatch, request)
    args = cli.build_parser().parse_args(["allocator", "status"])
    assert args.handler(args) == 0
    output = capsys.readouterr().out
    assert "Allocator recommend · 1 hosts · 1 models" in output
    assert "joint portfolio    2 workloads -> 1 models" in output
    assert "objective          resource pressure then request coverage" in output
    assert "exploration slot  general" in output
    assert "workload video" in output
    assert "starting via video-model · 0/1 ready" in output
    assert "learned workflow image → video · 86% confidence" in output
    assert "why              selected capacity is loading or warming" in output
    assert "secret-token" not in output
    assert calls[0][2]["headers"] == {}

    args = cli.build_parser().parse_args(["allocator", "status", "--json"])
    args.handler(args)
    assert json.loads(capsys.readouterr().out)["mode"] == "recommend"


def test_model_set_sends_validated_profile_and_secret_header(monkeypatch, capsys):
    monkeypatch.setattr(config, "select_grid", lambda _value: grid_config())
    captured = {}

    def request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return response(payload={"model": kwargs["json"]})

    patch_http_client(monkeypatch, request)
    args = cli.build_parser().parse_args(
        [
            "allocator",
            "model",
            "set",
            "qwen.gguf",
            "--memory-mb",
            "8000",
            "--min-replicas",
            "1",
            "--max-replicas",
            "2",
            "--backend",
            "metal",
            "--replica-concurrency",
            "8",
            "--required-tag",
            "finance",
            "--artifact-sha256",
            "A" * 64,
            "--artifact-source",
            "hf://owner/repo/qwen.gguf",
            "--artifact-size-mb",
            "4096",
            "--max-colocated-models",
            "1",
            "--colocation-exclude",
            "image.gguf",
        ]
    )
    assert args.handler(args) == 0
    assert captured["method"] == "PUT"
    assert captured["headers"] == {"X-Grid-Allocator-Token": "secret-token"}
    assert captured["json"]["model_id"] == "qwen.gguf"
    assert captured["json"]["runtimes"] == ("llama.cpp",)
    assert captured["json"]["replica_concurrency"] == 8
    assert captured["json"]["required_tags"] == ("finance",)
    assert captured["json"]["artifact_sha256"] == "a" * 64
    assert captured["json"]["artifact_source"] == "hf://owner/repo/qwen.gguf"
    assert captured["json"]["artifact_size_mb"] == 4096
    assert captured["json"]["max_colocated_models"] == 1
    assert captured["json"]["colocation_excludes"] == ("image.gguf",)
    assert "secret-token" not in capsys.readouterr().out


def test_model_set_explicit_runtime_replaces_llama_default(monkeypatch):
    monkeypatch.setattr(config, "select_grid", lambda _value: grid_config())
    captured: dict[str, object] = {}

    def request(_method, _url, **kwargs):
        captured.update(kwargs)
        return response(payload={"model": kwargs["json"]})

    patch_http_client(monkeypatch, request)
    args = cli.build_parser().parse_args(
        [
            "allocator",
            "model",
            "set",
            "qwen.gguf",
            "--memory-mb",
            "8000",
            "--runtime",
            "vllm",
        ]
    )

    assert args.handler(args) == 0
    body = captured["json"]
    assert isinstance(body, dict)
    assert body["runtimes"] == ("vllm",)


def test_model_set_accepts_runtime_specific_memory(monkeypatch):
    monkeypatch.setattr(config, "select_grid", lambda _value: grid_config())
    captured: dict[str, object] = {}

    def request(_method, _url, **kwargs):
        captured.update(kwargs)
        return response(payload={"model": kwargs["json"]})

    patch_http_client(monkeypatch, request)
    args = cli.build_parser().parse_args(
        [
            "allocator",
            "model",
            "set",
            "qwen",
            "--memory-mb",
            "8000",
            "--runtime",
            "llama.cpp",
            "--runtime",
            "vllm",
            "--runtime-memory-mb",
            "llama.cpp=10000",
            "--runtime-memory-mb",
            "vllm=24000",
            "--workload-score",
            "coding=1.0",
            "--workload-score",
            "research=0.8",
            "--min-gpu-count",
            "2",
            "--min-gpu-memory-mb",
            "48000",
            "--min-gpu-interconnect-gbps",
            "50",
            "--single-numa-node",
            "--disallow-mig",
        ]
    )

    assert args.handler(args) == 0
    body = captured["json"]
    assert isinstance(body, dict)
    assert body["runtime_memory_mb"] == (("llama.cpp", 10_000), ("vllm", 24_000))
    assert body["workload_scores"] == (("coding", 1.0), ("research", 0.8))
    assert body["min_gpu_count"] == 2
    assert body["min_gpu_memory_mb"] == 48_000
    assert body["min_gpu_interconnect_gbps"] == 50
    assert body["require_single_numa_node"] is True
    assert body["allow_mig"] is False


def test_model_set_rejects_malformed_runtime_specific_memory(monkeypatch):
    monkeypatch.setattr(config, "select_grid", lambda _value: grid_config())
    args = cli.build_parser().parse_args(
        [
            "allocator",
            "model",
            "set",
            "qwen",
            "--memory-mb",
            "8000",
            "--runtime-memory-mb",
            "vllm:24000",
        ]
    )

    with pytest.raises(SystemExit, match="RUNTIME=MB"):
        args.handler(args)


def test_model_set_rejects_malformed_workload_score(monkeypatch):
    monkeypatch.setattr(config, "select_grid", lambda _value: grid_config())
    args = cli.build_parser().parse_args(
        [
            "allocator",
            "model",
            "set",
            "qwen",
            "--memory-mb",
            "8000",
            "--workload-score",
            "coding:1.0",
        ]
    )

    with pytest.raises(SystemExit, match="WORKLOAD=SCORE"):
        args.handler(args)


def test_model_profile_cli_encodes_model_id_as_one_path_value(monkeypatch):
    monkeypatch.setattr(config, "select_grid", lambda _value: grid_config())
    urls: list[str] = []

    def request(_method, url, **kwargs):
        urls.append(url)
        return response(payload={"model": kwargs.get("json", {}), "deleted": "org/model#1"})

    patch_http_client(monkeypatch, request)
    set_args = cli.build_parser().parse_args(
        [
            "allocator",
            "model",
            "set",
            "org/model#1",
            "--memory-mb",
            "8000",
        ]
    )
    remove_args = cli.build_parser().parse_args(
        ["allocator", "model", "remove", "org/model#1"]
    )

    assert set_args.handler(set_args) == 0
    assert remove_args.handler(remove_args) == 0
    assert all(url.endswith("/allocator/models/org%2Fmodel%231") for url in urls)


def test_model_set_rejects_invalid_bounds_before_network(monkeypatch):
    monkeypatch.setattr(config, "select_grid", lambda _value: grid_config())
    monkeypatch.setattr(
        allocator.httpx,
        "request",
        lambda *_args, **_kwargs: pytest.fail("network should not be called"),
    )
    args = cli.build_parser().parse_args(
        [
            "allocator",
            "model",
            "set",
            "qwen.gguf",
            "--memory-mb",
            "8000",
            "--min-replicas",
            "2",
            "--max-replicas",
            "1",
        ]
    )
    with pytest.raises(SystemExit, match="replica bounds"):
        args.handler(args)


@pytest.mark.parametrize(
    ("argv", "method", "path"),
    [
        (["allocator", "model", "remove", "qwen.gguf"], "DELETE", "/allocator/models/qwen.gguf"),
        (["allocator", "mode", "observe"], "PUT", "/allocator/mode"),
        (["allocator", "tick"], "POST", "/allocator/tick"),
    ],
)
def test_mutating_commands_use_authenticated_routes(
    monkeypatch, capsys, argv, method, path
):
    monkeypatch.setattr(config, "select_grid", lambda _value: grid_config())
    calls = []

    def request(actual_method, url, **kwargs):
        calls.append((actual_method, url, kwargs))
        if path == "/allocator/mode":
            payload = {"mode": "observe"}
        elif path == "/allocator/tick":
            payload = {"actions": [], "deferred": []}
        else:
            payload = {"deleted": "qwen.gguf"}
        return response(payload=payload)

    patch_http_client(monkeypatch, request)
    assert cli.build_parser().parse_args(argv).handler(cli.build_parser().parse_args(argv)) == 0
    assert calls[0][0] == method
    assert calls[0][1].endswith(path)
    assert calls[0][2]["headers"]["X-Grid-Allocator-Token"] == "secret-token"
    assert "secret-token" not in capsys.readouterr().out


def test_token_resolution_for_remote_url_uses_env_or_file(monkeypatch, tmp_path):
    cfg = grid_config(managed=False)
    monkeypatch.setenv(allocator.TOKEN_ENV, "from-env")
    assert allocator._control_token(cfg, None) == "from-env"
    token_file = tmp_path / "token"
    token_file.write_text("from-file\n")
    assert allocator._control_token(cfg, str(token_file)) == "from-file"
    monkeypatch.delenv(allocator.TOKEN_ENV)
    with pytest.raises(SystemExit, match="control token required"):
        allocator._control_token(cfg, None)


def test_token_write_creates_owner_only_file_without_printing_secret(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(config, "select_grid", lambda _value: grid_config())
    target = tmp_path / "node-token"
    args = cli.build_parser().parse_args(
        ["allocator", "token", "write", str(target), "--host-id", "host-a"]
    )
    assert args.handler(args) == 0
    node_token = target.read_text().strip()
    assert decode_node_token(node_token).host_id == "host-a"
    assert verify_node_token(node_token, "secret-token", "host-a")
    assert target.stat().st_mode & 0o777 == 0o600
    assert "secret-token" not in capsys.readouterr().out
    with pytest.raises(SystemExit, match="Refusing to overwrite"):
        args.handler(args)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_secret_atomic_write_ignores_preplanted_predictable_temp_symlink(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("untouched", encoding="utf-8")
    target = tmp_path / "node-token"
    old_predictable_temp = tmp_path / "node-token.tmp"
    old_predictable_temp.symlink_to(victim)

    jsonio.atomic_write_bytes(target, b"secret\n")

    assert target.read_bytes() == b"secret\n"
    assert victim.read_text(encoding="utf-8") == "untouched"
    assert old_predictable_temp.is_symlink()


def test_windows_acl_is_established_before_secret_bytes_are_written(tmp_path, monkeypatch):
    target = tmp_path / "node-token.json"
    observed_sizes: list[int] = []

    monkeypatch.setattr(jsonio.sys, "platform", "win32")
    monkeypatch.setattr(
        jsonio,
        "_restrict_windows_owner_only",
        lambda path: observed_sizes.append(path.stat().st_size),
    )

    jsonio.atomic_write_bytes(target, b"bearer-secret")

    assert observed_sizes == [0]
    assert target.read_bytes() == b"bearer-secret"


def test_node_start_keeps_token_out_of_argv_and_record(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GRID_HOME", str(tmp_path / "grid-home"))
    cfg = grid_config()
    # A locally owned Grid advertises a LAN discovery URL, but its authenticated control URL is
    # literal loopback. The managed engine must stay on that loopback address by default.
    cfg["lan_signaling_url"] = "http://192.168.1.9:8090"
    monkeypatch.setattr(config, "select_grid", lambda _value: cfg)
    monkeypatch.setattr(
        allocator,
        "_request",
        lambda *_args, **_kwargs: {"grid_id": cfg["grid_id"]},
    )
    monkeypatch.setattr(runtime, "cli_command", lambda: ["grid"])
    monkeypatch.setattr(
        runtime,
        "detect_local_ip_for_url",
        lambda _url: pytest.fail("local managed nodes should not advertise a LAN address"),
    )
    monkeypatch.setattr(run_records, "pid_alive", lambda _pid: False)
    scope = allocator._scope(cfg["grid_id"])
    jsonio.atomic_write_json(allocator._node_state_path(scope), {"host_id": "host-a"})
    launched = {}

    class Process:
        pid = 1234

        def poll(self):
            return None

    def popen(command, **kwargs):
        launched["command"] = command
        launched.update(kwargs)
        return Process()

    def await_start(process, startup_path, instance_id, _log_path):
        jsonio.atomic_write_json(
            startup_path,
            {
                "instance_id": instance_id,
                "pid": process.pid,
                "host_id": "host-a",
                "registered_at": 1.0,
            },
        )

    monkeypatch.setattr(allocator.subprocess, "Popen", popen)
    monkeypatch.setattr(allocator, "_await_node_start", await_start)
    monkeypatch.setattr(allocator, "_await_process_start_marker", lambda _pid: "birth")
    args = cli.build_parser().parse_args(["allocator", "node", "start"])
    assert args.handler(args) == 0
    assert "secret-token" not in launched["command"]
    node_token = launched["env"][allocator.NODE_TOKEN_ENV]
    assert verify_node_token(node_token, "secret-token", "host-a")
    assert allocator.OPERATOR_TOKEN_ENV not in launched["env"]
    record = jsonio.load_json(
        paths.run_dir() / "allocator" / f"{scope}.json"
    )
    assert "secret-token" not in json.dumps(record)
    assert record["instance_id"] in launched["command"]
    assert record["process_start_marker"] == "birth"
    advertise_index = launched["command"].index("--advertise-host") + 1
    assert launched["command"][advertise_index] == "127.0.0.1"
    assert "host=host-a" in capsys.readouterr().out


def test_node_start_preserves_crashed_daemon_children_for_fenced_replacement_adoption(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("GRID_HOME", str(tmp_path / "grid-home"))
    cfg = grid_config()
    monkeypatch.setattr(config, "select_grid", lambda _value: cfg)
    monkeypatch.setattr(
        allocator,
        "_request",
        lambda *_args, **_kwargs: {"grid_id": cfg["grid_id"]},
    )
    monkeypatch.setattr(runtime, "cli_command", lambda: ["grid"])
    monkeypatch.setattr(run_records, "pid_alive", lambda _pid: False)
    scope = allocator._scope(cfg["grid_id"])
    state_path = allocator._node_state_path(scope)
    record_path = allocator._node_record_path(scope)
    jsonio.atomic_write_json(
        state_path,
        {
            "schema_version": 1,
            "host_id": "host-a",
            "residencies": [
                {
                    "model_id": "qwen.gguf",
                    "memory_mb": 8_000,
                    "state": "ready",
                    "handle": {"pid": 42_001, "port": 18_081},
                }
            ],
        },
    )
    jsonio.atomic_write_json(
        record_path,
        {"pid": 999, "instance_id": "crashed", "state_path": str(state_path)},
    )
    monkeypatch.setattr(
        allocator,
        "_stop_persisted_allocator_children",
        lambda *_args, **_kwargs: pytest.fail(
            "replacement start must let its runtime adopt persisted children"
        ),
    )

    class Process:
        pid = 1234

        def poll(self):
            return None

    monkeypatch.setattr(allocator.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(allocator, "_await_process_start_marker", lambda _pid: "birth")

    def await_start(process, startup_path, instance_id, _log_path):
        jsonio.atomic_write_json(
            startup_path,
            {
                "instance_id": instance_id,
                "pid": process.pid,
                "host_id": "host-a",
                "registered_at": 1.0,
            },
        )

    monkeypatch.setattr(allocator, "_await_node_start", await_start)
    args = cli.build_parser().parse_args(["allocator", "node", "start"])

    assert args.handler(args) == 0
    persisted = jsonio.load_json(state_path)
    assert persisted["residencies"][0]["handle"] == {"pid": 42_001, "port": 18_081}


def test_node_start_rejects_plaintext_lan_engine_before_spawn(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path / "grid-home"))
    cfg = grid_config()
    monkeypatch.setattr(config, "select_grid", lambda _value: cfg)
    monkeypatch.setattr(
        allocator,
        "_request",
        lambda *_args, **_kwargs: {"grid_id": cfg["grid_id"]},
    )
    monkeypatch.setattr(
        allocator.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("unsafe node must fail before spawn"),
    )
    scope = allocator._scope(cfg["grid_id"])
    jsonio.atomic_write_json(allocator._node_state_path(scope), {"host_id": "host-a"})

    args = cli.build_parser().parse_args(
        [
            "allocator",
            "node",
            "start",
            "--advertise-host",
            "10.0.0.5",
            "--allow-insecure-http",
        ]
    )
    with pytest.raises(SystemExit, match="non-loopback.*end-to-end TLS"):
        args.handler(args)


def test_node_start_passes_validated_tls_files_to_hidden_worker(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("GRID_HOME", str(tmp_path / "grid-home"))
    cfg = grid_config()
    monkeypatch.setattr(config, "select_grid", lambda _value: cfg)
    monkeypatch.setattr(
        allocator,
        "_request",
        lambda *_args, **_kwargs: {"grid_id": cfg["grid_id"]},
    )
    monkeypatch.setattr(runtime, "cli_command", lambda: ["grid"])
    monkeypatch.setattr(run_records, "pid_alive", lambda _pid: False)
    monkeypatch.setattr(allocator, "_await_process_start_marker", lambda _pid: "birth")
    scope = allocator._scope(cfg["grid_id"])
    jsonio.atomic_write_json(allocator._node_state_path(scope), {"host_id": "host-a"})
    cert = tmp_path / "engine.crt"
    key = tmp_path / "engine.key"
    ca = tmp_path / "ca.crt"
    cert.write_text("certificate")
    key.write_text("private key")
    ca.write_text("ca")
    key.chmod(0o600)
    launched: dict[str, object] = {}

    class Process:
        pid = 1234

        def poll(self):
            return None

    def popen(command, **kwargs):
        launched["command"] = command
        launched.update(kwargs)
        return Process()

    def await_start(process, startup_path, instance_id, _log_path):
        jsonio.atomic_write_json(
            startup_path,
            {
                "instance_id": instance_id,
                "pid": process.pid,
                "host_id": "host-a",
                "registered_at": 1.0,
            },
        )

    monkeypatch.setattr(allocator.subprocess, "Popen", popen)
    monkeypatch.setattr(allocator, "_await_node_start", await_start)
    args = cli.build_parser().parse_args(
        [
            "allocator",
            "node",
            "start",
            "--advertise-host",
            "10.0.0.5",
            "--engine-tls-cert",
            str(cert),
            "--engine-tls-key",
            str(key),
            "--engine-tls-ca",
            str(ca),
        ]
    )

    assert args.handler(args) == 0
    command = launched["command"]
    assert isinstance(command, list)
    assert command[command.index("--advertise-host") + 1] == "10.0.0.5"
    assert command[command.index("--engine-tls-cert") + 1] == str(cert.resolve())
    assert command[command.index("--engine-tls-key") + 1] == str(key.resolve())
    assert command[command.index("--engine-tls-ca") + 1] == str(ca.resolve())


def test_engine_tls_private_key_must_be_owner_only(tmp_path):
    cert = tmp_path / "engine.crt"
    key = tmp_path / "engine.key"
    cert.write_text("certificate")
    key.write_text("private key")
    key.chmod(0o644)
    if os.name == "nt":
        pytest.skip("POSIX mode-bit assertion")
    with pytest.raises(SystemExit, match="owner-only"):
        allocator._validated_engine_tls_files(str(cert), str(key), None)


def test_node_status_and_stop_use_non_secret_process_record(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GRID_HOME", str(tmp_path / "grid-home"))
    cfg = grid_config()
    monkeypatch.setattr(config, "select_grid", lambda _value: cfg)
    scope = allocator._scope(cfg["grid_id"])
    state_path = tmp_path / "state.json"
    jsonio.atomic_write_json(
        state_path,
        {
            "host_id": "host-a",
            "residencies": [{"model_id": "qwen.gguf", "state": "ready"}],
        },
    )
    jsonio.atomic_write_json(
        allocator._node_record_path(scope),
        {
            "pid": 1234,
            "instance_id": "instance-a",
            "process_start_marker": "birth-a",
            "state_path": str(state_path),
            "log_path": "/tmp/allocator.log",
        },
    )
    monkeypatch.setattr(run_records, "pid_alive", lambda pid: pid == 1234)
    monkeypatch.setattr(
        run_records,
        "process_command_args",
        lambda _pid: ("grid", "__allocator-node", "--instance-id", "instance-a"),
    )
    monkeypatch.setattr(run_records, "process_start_marker", lambda _pid: "birth-a")
    status = cli.build_parser().parse_args(["allocator", "node", "status"])
    assert status.handler(status) == 0
    assert "qwen.gguf" in capsys.readouterr().out

    monkeypatch.setattr(
        run_records,
        "terminate_pid",
        lambda pid, **_kwargs: pid == 1234,
    )
    stop = cli.build_parser().parse_args(["allocator", "node", "stop"])
    assert stop.handler(stop) == 0
    assert not allocator._node_record_path(scope).exists()


def test_node_local_override_commands_persist_and_resume_idempotently(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("GRID_HOME", str(tmp_path / "grid-home"))
    cfg = grid_config()
    monkeypatch.setattr(config, "select_grid", lambda _value: cfg)
    scope = allocator._scope(cfg["grid_id"])
    state_path = allocator._node_state_path(scope)

    pause = cli.build_parser().parse_args(
        [
            "allocator",
            "node",
            "pause",
            "--reason",
            "presentation",
            "--for-seconds",
            "60",
        ]
    )
    assert pause.handler(pause) == 0
    stored = jsonio.load_json(local_override_path(state_path))
    assert stored["state"] == "paused"
    assert stored["reason"] == "presentation"
    assert stored["expires_at"] is not None
    assert local_override_path(state_path).stat().st_mode & 0o777 == 0o600
    capsys.readouterr()

    status = cli.build_parser().parse_args(["allocator", "node", "status", "--json"])
    assert status.handler(status) == 0
    assert json.loads(capsys.readouterr().out)["local_override"]["state"] == "paused"

    resume = cli.build_parser().parse_args(["allocator", "node", "resume"])
    assert resume.handler(resume) == 0
    assert resume.handler(resume) == 0
    assert not local_override_path(state_path).exists()


@pytest.mark.parametrize("duration", ["0", "nan", "inf"])
def test_node_local_override_rejects_nonpositive_or_nonfinite_duration(
    monkeypatch, duration
):
    monkeypatch.setattr(config, "select_grid", lambda _value: grid_config())
    args = cli.build_parser().parse_args(
        ["allocator", "node", "drain", "--for-seconds", duration]
    )
    with pytest.raises(SystemExit, match="must be positive"):
        args.handler(args)


def test_grid_down_stops_allocator_node_before_server(monkeypatch):
    cfg = grid_config()
    order = []
    monkeypatch.setattr(config, "select_grid", lambda _value: cfg)
    monkeypatch.setattr(
        allocator,
        "stop_allocator_node_for_grid",
        lambda actual: order.append(("node", actual["grid_id"])) or True,
    )
    monkeypatch.setattr(
        runtime,
        "stop_grid",
        lambda actual: order.append(("server", actual["grid_id"]))
        or SimpleNamespace(stopped=lambda: True),
    )
    args = cli.build_parser().parse_args(["down", "test"])
    assert args.handler(args) == 0
    assert order == [("node", "ag-test"), ("server", "ag-test")]


def test_node_stop_uses_cooperative_shutdown_request_before_signals(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("GRID_HOME", str(tmp_path / "grid-home"))
    cfg = grid_config()
    scope = allocator._scope(cfg["grid_id"])
    state_path = allocator._node_state_path(scope)
    request_path = shutdown_request_path(state_path)
    jsonio.atomic_write_json(
        allocator._node_record_path(scope),
        {
            "pid": 1234,
            "instance_id": "instance-a",
            "state_path": str(state_path),
            "graceful_shutdown": True,
        },
    )

    def alive(_pid):
        return not request_path.exists()

    monkeypatch.setattr(run_records, "pid_alive", alive)
    monkeypatch.setattr(
        allocator,
        "_node_process_state",
        lambda _record: "dead" if request_path.exists() else "owned",
    )
    monkeypatch.setattr(
        run_records,
        "terminate_pid",
        lambda _pid: pytest.fail("cooperative stop should avoid signal fallback"),
    )
    assert allocator.stop_allocator_node_for_grid(cfg)
    assert not request_path.exists()
    assert not allocator._node_record_path(scope).exists()


def test_node_stop_does_not_signal_before_registry_expiry_fallback_can_finish(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("GRID_HOME", str(tmp_path / "grid-home"))
    cfg = grid_config()
    scope = allocator._scope(cfg["grid_id"])
    state_path = allocator._node_state_path(scope)
    record_path = allocator._node_record_path(scope)
    jsonio.atomic_write_json(
        record_path,
        {
            "pid": 1234,
            "instance_id": "instance-a",
            "state_path": str(state_path),
            "graceful_shutdown": True,
        },
    )
    now = [0.0]
    safe_fallback_seconds = 30.0 + 60.0 + 15.0

    def alive(_pid):
        return now[0] < safe_fallback_seconds

    def advance(seconds):
        assert now[0] < allocator.NODE_STOP_COOPERATIVE_GRACE_SECONDS
        now[0] += seconds

    monkeypatch.setattr(allocator.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(allocator.time, "sleep", advance)
    monkeypatch.setattr(run_records, "pid_alive", alive)
    monkeypatch.setattr(
        allocator,
        "_node_process_state",
        lambda _record: "dead" if now[0] >= safe_fallback_seconds else "owned",
    )
    monkeypatch.setattr(
        run_records,
        "terminate_pid",
        lambda _pid, **_kwargs: pytest.fail(
            "the parent must not signal during the daemon's safe TTL fallback"
        ),
    )

    assert allocator.NODE_STOP_COOPERATIVE_GRACE_SECONDS > safe_fallback_seconds
    assert allocator.stop_allocator_node_for_grid(cfg)
    assert now[0] >= safe_fallback_seconds
    assert not record_path.exists()


def test_dead_daemon_stop_waits_full_route_lease_before_stopping_persisted_child(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("GRID_HOME", str(tmp_path / "grid-home"))
    cfg = grid_config()
    scope = allocator._scope(cfg["grid_id"])
    state_path = allocator._node_state_path(scope)
    record_path = allocator._node_record_path(scope)
    jsonio.atomic_write_json(
        state_path,
        {
            "schema_version": 1,
            "host_id": "host-a",
            "backend_config": {
                "bind_host": "127.0.0.1",
                "endpoint_host": "127.0.0.1",
                "endpoint_scheme": "http",
            },
            "residencies": [
                {
                    "model_id": "qwen.gguf",
                    "memory_mb": 8_000,
                    "state": "ready",
                    "handle": {"pid": 42_001, "port": 18_081},
                }
            ],
        },
    )
    jsonio.atomic_write_json(
        record_path,
        {"pid": 999, "instance_id": "dead", "state_path": str(state_path)},
    )
    now = [0.0]
    stop_times: list[tuple[float, float]] = []

    class Runtime:
        def __init__(self, _state_path, *, backend):
            assert backend.bind_host == "127.0.0.1"

        def stop_all(self, *, wait_timeout):
            stop_times.append((now[0], wait_timeout))

    def advance(seconds):
        assert stop_times == []
        now[0] += seconds

    monkeypatch.setattr(allocator, "ManagedModelRuntime", Runtime)
    monkeypatch.setattr(run_records, "pid_alive", lambda _pid: False)
    monkeypatch.setattr(allocator.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(allocator.time, "sleep", advance)

    assert allocator.stop_allocator_node_for_grid(cfg)
    assert stop_times == [
        (
            allocator.NODE_REGISTRY_TTL_FALLBACK_SECONDS,
            allocator.NODE_SHUTDOWN_DRAIN_SECONDS,
        )
    ]
    assert not record_path.exists()


def test_node_stop_never_signals_a_reused_foreign_pid(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path / "grid-home"))
    cfg = grid_config()
    scope = allocator._scope(cfg["grid_id"])
    record_path = allocator._node_record_path(scope)
    jsonio.atomic_write_json(
        record_path,
        {
            "pid": 1234,
            "instance_id": "allocator-instance",
            "process_start_marker": "old-birth",
        },
    )
    monkeypatch.setattr(run_records, "pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        run_records,
        "process_command_args",
        lambda _pid: ("python", "someone-elses-program"),
    )
    monkeypatch.setattr(
        run_records,
        "terminate_pid",
        lambda _pid: pytest.fail("a foreign PID must never be signaled"),
    )

    assert allocator.stop_allocator_node_for_grid(cfg) is False
    assert not record_path.exists()


def test_node_start_refuses_duplicate_when_live_pid_identity_is_ambiguous(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("GRID_HOME", str(tmp_path / "grid-home"))
    cfg = grid_config()
    monkeypatch.setattr(config, "select_grid", lambda _value: cfg)
    monkeypatch.setattr(
        allocator,
        "_request",
        lambda *_args, **_kwargs: {"grid_id": cfg["grid_id"]},
    )
    scope = allocator._scope(cfg["grid_id"])
    jsonio.atomic_write_json(allocator._node_record_path(scope), {"pid": 1234})
    monkeypatch.setattr(run_records, "pid_alive", lambda _pid: True)

    args = cli.build_parser().parse_args(["allocator", "node", "start"])
    with pytest.raises(SystemExit, match="identity cannot be verified"):
        args.handler(args)


def test_equivalent_grid_urls_share_server_grid_id_scope(monkeypatch):
    first = grid_config(managed=False)
    first["lan_signaling_url"] = "https://grid.example"
    second = {**first, "lan_signaling_url": "https://10.0.0.8:8443"}
    monkeypatch.setattr(
        allocator,
        "_request",
        lambda *_args, **_kwargs: {"grid_id": "ag-canonical"},
    )

    assert allocator._scope_for_grid(first, allow_offline=False) == allocator._scope_for_grid(
        second,
        allow_offline=False,
    )


def test_node_start_record_failure_terminates_exact_spawned_process(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("GRID_HOME", str(tmp_path / "grid-home"))
    cfg = grid_config()
    monkeypatch.setattr(config, "select_grid", lambda _value: cfg)
    monkeypatch.setattr(
        allocator,
        "_request",
        lambda *_args, **_kwargs: {"grid_id": cfg["grid_id"]},
    )
    monkeypatch.setattr(runtime, "cli_command", lambda: ["grid"])
    monkeypatch.setattr(run_records, "pid_alive", lambda _pid: False)
    monkeypatch.setattr(allocator, "_await_process_start_marker", lambda _pid: "birth")
    scope = allocator._scope(cfg["grid_id"])
    jsonio.atomic_write_json(allocator._node_state_path(scope), {"host_id": "host-a"})
    stopped = []
    request_path = shutdown_request_path(allocator._node_state_path(scope))
    assert allocator.NODE_STARTUP_CLEANUP_GRACE_SECONDS > 30.0 + 60.0 + 15.0

    class Process:
        pid = 1234

        def __init__(self):
            self.exited = False

        def poll(self):
            return 0 if self.exited else None

        def wait(self, timeout):
            assert request_path.exists()
            stopped.append(("wait", timeout))
            if timeout == allocator.NODE_STARTUP_CLEANUP_GRACE_SECONDS:
                raise allocator.subprocess.TimeoutExpired("grid", timeout)
            self.exited = True
            return 0

        def terminate(self):
            pytest.fail("startup cleanup must not signal only the daemon")

        def kill(self):
            pytest.fail("startup cleanup must not kill only the daemon")

    monkeypatch.setattr(allocator.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(
        run_records,
        "kill_group",
        lambda pid: stopped.append(("kill_group", pid)),
    )
    original_write = jsonio.atomic_write_json

    def fail_record(path, data, mode=0o600):
        if path == allocator._node_record_path(scope):
            raise OSError("disk full")
        return original_write(path, data, mode)

    monkeypatch.setattr(jsonio, "atomic_write_json", fail_record)
    args = cli.build_parser().parse_args(["allocator", "node", "start"])

    with pytest.raises(OSError, match="disk full"):
        args.handler(args)
    assert stopped == [
        ("wait", allocator.NODE_STARTUP_CLEANUP_GRACE_SECONDS),
        ("kill_group", 1234),
        ("wait", 5.0),
    ]
    assert not request_path.exists()


def test_node_start_handshake_deadline_covers_slow_restored_engines():
    assert allocator._await_node_start.__defaults__ == (
        allocator.NODE_STARTUP_TIMEOUT_SECONDS,
    )
    assert allocator.NODE_STARTUP_TIMEOUT_SECONDS >= 45


def test_pid_alive_rejects_nonpositive_process_group_values(monkeypatch):
    monkeypatch.setattr(
        run_records.os,
        "kill",
        lambda *_args: pytest.fail("nonpositive PID must never reach os.kill"),
    )
    assert not run_records.pid_alive(0)
    assert not run_records.pid_alive(-1)


def test_request_errors_are_clean(monkeypatch):
    cfg = grid_config()
    patch_http_client(
        monkeypatch,
        lambda *_args, **_kwargs: response(403, {"detail": "bad token"}),
    )
    with pytest.raises(SystemExit, match="403.*bad token"):
        allocator._request(cfg, "POST", "/allocator/tick", token="wrong")


def test_remote_operator_request_refuses_plain_http_without_explicit_override(monkeypatch):
    cfg = grid_config(managed=False)
    cfg["lan_signaling_url"] = "http://192.168.1.9:8090"
    patch_http_client(
        monkeypatch,
        lambda *_args, **_kwargs: pytest.fail("credential must not be sent"),
    )
    with pytest.raises(SystemExit, match="Refusing to send"):
        allocator._request(cfg, "POST", "/allocator/tick", token="operator")


def test_internal_node_dispatch_parses_private_arguments(monkeypatch):
    import cli._main as main_module

    captured = {}

    def run(selector, **kwargs):
        captured.update(selector=selector, **kwargs)
        return 7

    monkeypatch.setattr(main_module, "cmd_internal_allocator_node", run)
    result = main_module._maybe_internal(
        [
            "__allocator-node",
            "ag-test",
            "--state-path",
            "/tmp/state.json",
            "--instance-id",
            "instance-a",
            "--startup-path",
            "/tmp/ready.json",
            "--heartbeat-interval",
            "2",
            "--advertise-host",
            "10.0.0.5",
        ]
    )
    assert result == 7
    assert captured == {
        "selector": "ag-test",
        "state_path": "/tmp/state.json",
        "instance_id": "instance-a",
        "startup_path": "/tmp/ready.json",
        "heartbeat_interval": 2.0,
        "advertise_host": "10.0.0.5",
        "allow_insecure_http": False,
        "engine_tls_cert": None,
        "engine_tls_key": None,
        "engine_tls_ca": None,
        "provider_grid_id": None,
        "dedicated": False,
    }


@pytest.mark.parametrize(
    ("advertise_host", "expected"),
    [
        ("10.0.0.5", "0.0.0.0"),
        ("grid-worker.local", "0.0.0.0"),
        ("2001:db8::5", "::"),
        ("[2001:db8::5]", "::"),
        ("fe80::5%en0", "::"),
        ("[fe80::5%25en0]", "::"),
    ],
)
def test_allocator_engine_bind_matches_advertised_address_family(
    advertise_host,
    expected,
):
    import cli._main as main_module

    assert main_module._allocator_bind_host(advertise_host) == expected


def test_crash_cleanup_recovers_ipv6_child_family_from_legacy_argv(
    monkeypatch,
    tmp_path,
):
    state_path = tmp_path / "runtime.json"
    jsonio.atomic_write_json(
        state_path,
        {
            "schema_version": 1,
            "host_id": "host-a",
            "residencies": [
                {
                    "model_id": "qwen.gguf",
                    "memory_mb": 8_000,
                    "state": "ready",
                    "handle": {"pid": 42_001, "port": 18_081},
                }
            ],
        },
    )
    monkeypatch.setattr(
        run_records,
        "process_command_args",
        lambda _pid: (
            "/opt/llama-server",
            "-m",
            "/models/qwen.gguf",
            "--host",
            "::",
            "--port",
            "18081",
        ),
    )
    observed: dict[str, object] = {}

    class Runtime:
        def __init__(self, _state_path, *, backend):
            observed["bind_host"] = backend.bind_host
            observed["endpoint_host"] = backend.endpoint_host

        def stop_all(self, *, wait_timeout):
            observed["wait_timeout"] = wait_timeout

    monkeypatch.setattr(allocator, "ManagedModelRuntime", Runtime)

    assert allocator._stop_persisted_allocator_children(
        state_path,
        registry_ttl_seconds=0,
    )
    assert observed == {
        "bind_host": "::",
        "endpoint_host": "::1",
        "wait_timeout": 15.0,
    }


def test_crash_cleanup_reconstructs_tls_backend_after_cert_files_are_rotated(
    monkeypatch,
    tmp_path,
):
    ca_pem = (
        Path(__file__).parent / "fixtures" / "allocator_tls_ca.pem"
    ).read_text()
    missing_cert = tmp_path / "rotated-server.pem"
    missing_key = tmp_path / "rotated-server.key"
    missing_ca = tmp_path / "rotated-ca.pem"
    state_path = tmp_path / "runtime.json"
    jsonio.atomic_write_json(
        state_path,
        {
            "schema_version": 1,
            "host_id": "host-a",
            "backend_config": {
                "bind_host": "::",
                "endpoint_host": "worker.internal",
                "endpoint_scheme": "https",
                "tls_cert_file": str(missing_cert),
                "tls_key_file": str(missing_key),
                "tls_ca_file": str(missing_ca),
                "tls_ca_pem": ca_pem,
            },
            "residencies": [
                {
                    "model_id": "qwen.gguf",
                    "memory_mb": 8_000,
                    "state": "ready",
                    "handle": {"pid": 42_001, "port": 18_081},
                }
            ],
        },
    )
    observed: dict[str, object] = {}

    class Runtime:
        def __init__(self, _state_path, *, backend):
            observed.update(
                bind_host=backend.bind_host,
                endpoint_scheme=backend.endpoint_scheme,
                cert=backend.tls_cert_file,
                key=backend.tls_key_file,
                ca_pem=backend.tls_ca_pem,
            )

        def stop_all(self, *, wait_timeout):
            observed["wait_timeout"] = wait_timeout

    monkeypatch.setattr(allocator, "ManagedModelRuntime", Runtime)

    assert allocator._stop_persisted_allocator_children(
        state_path,
        registry_ttl_seconds=0,
    )
    assert observed == {
        "bind_host": "::",
        "endpoint_scheme": "https",
        "cert": str(missing_cert),
        "key": str(missing_key),
        "ca_pem": ca_pem,
        "wait_timeout": 15.0,
    }


def test_allocator_engine_bind_supports_ipv6_only_advertised_hostname(monkeypatch):
    import socket

    import cli._main as main_module

    monkeypatch.setattr(
        main_module.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("2001:db8::5", 0, 0, 0))
        ],
    )
    assert main_module._allocator_bind_host("worker-v6.internal") == "::"
