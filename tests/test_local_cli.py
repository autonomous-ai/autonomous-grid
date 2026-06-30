from __future__ import annotations

import argparse
import base64
import json
import os
import stat
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import cli
from cli import dispatch
from local import config
from shared import paths
from shared import state
from local import runtime
from shared.engine import comfyui, installer, launcher
from shared.models import catalog, download, media_bundles
from local import media_server
from local.server import create_app
from shared.system import detect


def _engine_args(**overrides) -> SimpleNamespace:
    base = dict(
        grid="http://192.168.1.25:8090",
        node_id="node-test",
        name="eng",
        models=[],
        advertise_as=[],
        endpoint_url=None,
        endpoint_port=8081,
        advertise_host=None,
        enable_media=False,
        media_bundles=[],
        comfyui_port=8188,
        media_port=8190,
        heartbeat_interval=15.0,
        ctx_size=None,
        n_predict=None,
        parallel=None,
        flash_attn=None,
        temp=None,
        reasoning_budget=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_cli_drops_auth_and_legacy_commands():
    parser = cli.build_parser()

    for argv in (
        ["auth", "login"],
        ["network", "create", "home"],
        ["provider", "start", "--network", "home"],
        ["consumer", "env", "--network", "home"],
        ["request", "chat", "--network", "home"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


def test_lan_internet_rename_is_a_hard_cutover():
    """The old ``lan``/``internet`` spellings are gone; ``local``/``remote`` are the modes.

    Pins the lan→local / internet→remote rename as a hard cutover: no alias, no
    back-compat for the removed names, at both the ``grid mode`` and one-shot
    ``--flag`` layers.
    """
    assert "local" in state.VALID_MODES
    assert "remote" in state.VALID_MODES
    assert "lan" not in state.VALID_MODES
    assert "internet" not in state.VALID_MODES

    parser = cli.build_parser()
    # ``grid mode lan``/``grid mode internet`` are rejected (removed choices);
    # ``grid mode local``/``grid mode remote`` are accepted.
    with pytest.raises(SystemExit):
        parser.parse_args(["mode", "lan"])
    with pytest.raises(SystemExit):
        parser.parse_args(["mode", "internet"])
    assert parser.parse_args(["mode", "local"]).target == "local"
    assert parser.parse_args(["mode", "remote"]).target == "remote"

    # The one-shot override: ``--local``/``--remote`` are recognised and stripped;
    # the removed ``--lan``/``--internet`` are not (they fall through to argparse).
    assert dispatch.resolve_override(["--local", "engines"]) == ("local", ["engines"])
    assert dispatch.resolve_override(["--remote", "engines"]) == ("remote", ["engines"])
    for removed in ("--lan", "--internet"):
        override, cleaned = dispatch.resolve_override([removed, "engines"])
        assert override is None and removed in cleaned


def test_init_grid_config_is_local_permissionless(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    monkeypatch.setattr(runtime, "detect_local_ip", lambda: "192.168.1.25")

    cfg = runtime.init_grid_config(name="home", port=48090)

    assert cfg["grid_type"] == runtime.GRID_TYPE
    assert cfg["managed_server"] is True
    assert cfg["lan_signaling_url"] == "http://192.168.1.25:48090"


def test_select_grid_accepts_signaling_url(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))

    cfg = config.select_grid("http://192.168.1.25:8090/")

    assert cfg["grid_type"] == runtime.GRID_TYPE
    assert cfg["managed_server"] is False
    assert cfg["lan_signaling_url"] == "http://192.168.1.25:8090"


def test_select_grid_defaults_to_only_grid_then_home(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))

    with pytest.raises(SystemExit):
        config.select_grid(None)

    runtime.init_grid_config(name="solo", port=8090)
    assert config.select_grid(None)["name"] == "solo"

    runtime.init_grid_config(name="home", port=8091)
    assert config.select_grid(None)["name"] == "home"


def test_server_registers_and_discovers_provider_without_auth():
    app = create_app(grid_id="ag-test", grid_name="test")
    client = TestClient(app)

    info = client.get("/grid/info")
    assert info.status_code == 200
    assert info.json()["auth_required"] is False

    update = client.put(
        "/nodes/node-1",
        json={
            "role": "engine",
            "models": ["qwen-local"],
            "endpoint_url": "http://192.168.1.50:8081/v1",
        },
    )
    assert update.status_code == 200

    discover = client.get("/nodes/discover", params={"model": "qwen-local"})
    assert discover.status_code == 200
    providers = discover.json()["engines"]
    assert providers[0]["node_id"] == "node-1"
    assert providers[0]["endpoint_url"] == "http://192.168.1.50:8081/v1"


def test_server_exposes_media_routes_without_auth():
    app = create_app(grid_id="ag-test", grid_name="test")
    client = TestClient(app)

    resp = client.post("/v1/media/image/generate", json={"prompt": "desk"})

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "engine_unavailable"


def test_server_accepts_media_only_provider_without_endpoint_url():
    app = create_app(grid_id="ag-test", grid_name="test")
    client = TestClient(app)

    update = client.put(
        "/nodes/node-media",
        json={
            "role": "engine",
            "models": ["comfyui:image_editing"],
            "media_url": "http://192.168.1.50:8190",
        },
    )

    assert update.status_code == 200
    discover = client.get("/nodes/discover", params={"model": "comfyui:image_editing"})
    providers = discover.json()["engines"]
    assert providers[0]["node_id"] == "node-media"
    assert providers[0]["endpoint_url"] is None
    assert providers[0]["media_url"] == "http://192.168.1.50:8190"


def test_server_rejects_provider_missing_required_capability_url():
    app = create_app(grid_id="ag-test", grid_name="test")
    client = TestClient(app)

    text = client.put(
        "/nodes/node-text",
        json={
            "role": "engine",
            "models": ["qwen-local"],
        },
    )
    media = client.put(
        "/nodes/node-media",
        json={
            "role": "engine",
            "models": ["comfyui:image_editing"],
        },
    )

    assert text.status_code == 400
    assert text.json()["detail"] == "endpoint_url is required for text engines"
    assert media.status_code == 400
    assert media.json()["detail"] == "media_url is required for media engines"


def test_info_env_prints_openai_compat_without_real_secret(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))

    args = argparse.Namespace(grid="http://192.168.1.25:8090", json=False, env=True)
    assert cli.cmd_info(args) == 0

    out = capsys.readouterr().out
    assert 'OPENAI_BASE_URL="http://192.168.1.25:8090/v1"' in out
    assert 'OPENAI_API_KEY="local-grid"' in out


def test_cli_accepts_engine_and_model_commands():
    parser = cli.build_parser()

    default_install = parser.parse_args(["engine", "install", "llama.cpp"])
    assert default_install.handler is cli.cmd_engine_install
    assert default_install.name == "llama.cpp"
    assert default_install.from_source is False

    source_install = parser.parse_args(["engine", "install", "llama.cpp", "--from-source"])
    assert source_install.from_source is True

    assert parser.parse_args(["catalog"]).handler is cli.cmd_catalog
    assert parser.parse_args(["pull", "qwen36-35b-a3b-mtp"]).model == "qwen36-35b-a3b-mtp"
    rm = parser.parse_args(["rm", "your-model.gguf", "--yes"])
    assert rm.handler is cli.cmd_rm
    assert rm.yes is True

    join = parser.parse_args([
        "join",
        "http://192.168.1.25:8090",
        "--at",
        "http://192.168.1.10:11434/v1",
        "-m",
        "your-model.gguf",
        "--advertise-as",
        "your-model",
    ])
    assert join.handler is cli.cmd_join
    assert join.at == "http://192.168.1.10:11434/v1"
    assert join.models == ["your-model.gguf"]
    assert join.advertise_as == ["your-model"]


def test_cli_accepts_engine_pull_and_media_use_commands():
    parser = cli.build_parser()

    assert parser.parse_args(["engine", "install", "comfyui"]).name == "comfyui"
    pull = parser.parse_args(["engine", "pull", "image_generation"])
    assert pull.handler is cli.cmd_engine_pull
    assert pull.bundle == "image_generation"

    gen = parser.parse_args(["image", "a small house"])
    assert gen.handler is cli.cmd_image
    assert gen.prompt == "a small house"
    assert gen.width == 720

    serve = parser.parse_args(["join", "home", "--serve", "Qwen3.5-2B-UD-IQ2_M.gguf"])
    assert serve.serve == "Qwen3.5-2B-UD-IQ2_M.gguf"

    media = parser.parse_args(["join", "home", "--media", "--bundle", "i2v"])
    assert media.media is True
    assert media.bundles == ["i2v"]


def test_cli_accepts_engine_runtime_commands():
    parser = cli.build_parser()

    status = parser.parse_args(["engine", "status"])
    assert status.handler is cli.cmd_engine_status
    assert status.port == 8188

    start = parser.parse_args(["engine", "start", "--port", "8200", "--detach"])
    assert start.handler is cli.cmd_engine_start
    assert start.port == 8200
    assert start.detach is True

    assert parser.parse_args(["engine", "stop"]).handler is cli.cmd_engine_stop


def test_engine_stop_delegates_to_comfyui(monkeypatch):
    calls = []
    monkeypatch.setattr(comfyui, "stop_running", lambda: calls.append("stop") or 0)

    rc = cli.cmd_engine_stop(argparse.Namespace())

    assert rc == 0
    assert calls == ["stop"]


def test_wait_for_media_server_fails_fast_when_child_exits():
    from local import media_runtime

    class _DeadProc:
        returncode = 1

        def poll(self):
            return 1

    # A child that has already exited must raise immediately, not poll until timeout.
    with pytest.raises(SystemExit, match="exited"):
        media_runtime.wait_for_media_server(
            _DeadProc(), port=59999, log_path=Path("/tmp/media_test.log"), timeout=5
        )


def test_media_pinned_engine_versions_and_bundles_are_ported():
    assert comfyui.COMFYUI_PINNED_COMMIT == "47ccecaee009cce148e8c2a5bdc2ecb302cc52ee"
    assert comfyui.COMFYUI_GGUF_PINNED_COMMIT == "6ea2651e7df66d7585f6ffee804b20e92fb38b8a"
    assert comfyui.GGUF_PINNED_VERSION == "gguf==0.18.0"
    assert comfyui.TORCH_PINNED == "torch==2.13.0.dev20260423"
    assert comfyui.COMFYUI_REQUIREMENT_PINS == (
        "comfyui_frontend_package==1.42.14",
        "comfyui_workflow_templates==0.9.62",
    )
    assert set(media_bundles.BUNDLES) == {"image_generation", "image_editing", "i2v"}
    assert media_bundles.CAPABILITY_NAME["image_generation"] == "comfyui:image_generation"


def test_provider_media_server_streams_sse_events(monkeypatch):
    class FakeHandler:
        def __init__(self, comfyui_url):
            self.comfyui_url = comfyui_url

        def handle_request(self, endpoint_path, body):
            assert endpoint_path == "media/image/generate"
            assert body["prompt"] == "desk"
            yield 'data: {"type": "progress", "progress": 1}'
            yield "data: [DONE]"

    monkeypatch.setattr(media_server, "MediaHandler", FakeHandler)
    client = TestClient(media_server.create_app(comfyui_url="http://localhost:8188/api"))

    resp = client.post("/media/image/generate", json={"prompt": "desk"})

    assert resp.status_code == 200
    assert 'data: {"type": "progress", "progress": 1}\n\n' in resp.text
    assert "data: [DONE]\n\n" in resp.text


def test_catalog_contains_reference_readme_qwen36_models():
    apple = catalog.find("qwen36-35b-a3b-mtp")
    nvidia = catalog.find("qwen36-27b-mtp")

    assert apple is not None
    assert apple.hf_repo == "unsloth/Qwen3.6-35B-A3B-MTP-GGUF"
    assert apple.quantized_file == "Qwen3.6-35B-A3B-UD-IQ3_S.gguf"
    assert apple.target == catalog.TARGET_APPLE_SILICON

    assert nvidia is not None
    assert nvidia.hf_repo == "unsloth/Qwen3.6-27B-MTP-GGUF"
    assert nvidia.quantized_file == "Qwen3.6-27B-UD-Q5_K_XL.gguf"
    assert nvidia.target == catalog.TARGET_NVIDIA


def test_download_surfaces_non_2xx_as_clean_systemexit(monkeypatch, tmp_path):
    # A non-2xx HF response must yield "Download failed (<status>): <body>",
    # not an httpx.ResponseNotRead traceback from reading .text on an unread stream.
    class _FakeStreamResponse:
        status_code = 404

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"Entry not found"

        @property
        def text(self):  # an unread streaming response raises, exactly like real httpx
            raise httpx.ResponseNotRead()

    monkeypatch.setattr(download.httpx, "stream", lambda *a, **k: _FakeStreamResponse())

    with pytest.raises(SystemExit) as exc:
        download.download("nope/missing-GGUF", "missing.gguf", out=tmp_path / "missing.gguf")

    # A clean SystemExit (status + body) means no ResponseNotRead leaked through.
    assert "404" in str(exc.value)
    assert "Entry not found" in str(exc.value)


def test_download_non_2xx_with_unreadable_body_still_exits_cleanly(monkeypatch, tmp_path):
    # If reading the error body fails (e.g. connection dropped mid-body), still
    # surface a clean SystemExit with the status — never a raw httpx traceback.
    class _BrokenStreamResponse:
        status_code = 404

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            raise httpx.ReadError("connection dropped")

    monkeypatch.setattr(download.httpx, "stream", lambda *a, **k: _BrokenStreamResponse())

    with pytest.raises(SystemExit) as exc:
        download.download("nope/missing-GGUF", "missing.gguf", out=tmp_path / "missing.gguf")

    assert "404" in str(exc.value)
    assert "could not read response body" in str(exc.value)


def test_rm_yes_deletes_local_model(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    model_path = paths.models_dir() / "your-model.gguf"
    model_path.parent.mkdir(parents=True)
    model_path.write_bytes(b"model")

    args = argparse.Namespace(model="your-model.gguf", yes=True)
    assert cli.cmd_rm(args) == 0

    assert not model_path.exists()
    assert "Removed" in capsys.readouterr().out


def test_install_macos_homebrew_installs_and_links(monkeypatch, tmp_path):
    grid_home = tmp_path / "grid-home"
    brew_root = tmp_path / "homebrew"
    brew = brew_root / "bin" / "brew"
    formula_prefix = brew_root / "opt" / "llama.cpp"
    llama_server = formula_prefix / "bin" / "llama-server"
    llama_server.parent.mkdir(parents=True)
    llama_server.write_text("#!/bin/sh\n", encoding="utf-8")
    llama_server.chmod(0o755)
    calls = []

    monkeypatch.setenv("GRID_HOME", str(grid_home))

    def fake_which(name):
        if name == "brew":
            return str(brew)
        return None

    def fake_run(args, stdout=None, stderr=None, check=False):
        calls.append(tuple(args))
        return subprocess.CompletedProcess(args, 1)

    def fake_check_call(args):
        calls.append(tuple(args))

    def fake_check_output(args, text=False):
        calls.append(tuple(args))
        if args == [str(brew), "--prefix", "llama.cpp"]:
            return f"{formula_prefix}\n"
        if args == [str(brew), "--prefix"]:
            return f"{brew_root}\n"
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(installer.shutil, "which", fake_which)
    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    monkeypatch.setattr(installer.subprocess, "check_call", fake_check_call)
    monkeypatch.setattr(installer.subprocess, "check_output", fake_check_output)

    target = installer.install_macos_homebrew()

    assert target == grid_home / "bin" / "llama-server"
    assert target.is_symlink()
    assert target.readlink() == llama_server
    assert (str(brew), "install", "llama.cpp") in calls


def test_engine_install_apple_silicon_uses_homebrew_by_default(monkeypatch):
    calls = []
    target = Path("/tmp/grid/bin/llama-server")

    monkeypatch.setattr(installer, "is_apple_silicon", lambda: True)
    monkeypatch.setattr(installer, "install_macos_homebrew", lambda: calls.append("brew") or target)
    monkeypatch.setattr(installer, "install_metal_from_source", lambda: calls.append("source") or target)

    rc = cli.cmd_engine_install(argparse.Namespace(name="llama.cpp", target_sm=None, from_source=False))

    assert rc == 0
    assert calls == ["brew"]


class FakeProc:
    pid = 12345

    def poll(self):
        return None


def test_launcher_start_llm_adds_alias_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    model_path = paths.models_dir() / "your-model.gguf"
    model_path.parent.mkdir(parents=True)
    model_path.write_bytes(b"model")
    calls = {}

    monkeypatch.setattr(launcher, "llama_server_path", lambda: "/usr/local/bin/llama-server")

    def fake_popen(cmd, **kwargs):
        calls["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)

    launched = launcher.start_llm("your-model.gguf", port=8081, alias="your-model", mmproj=None)

    assert launched.port == 8081
    assert calls["cmd"][:5] == [
        "/usr/local/bin/llama-server",
        "-m",
        str(model_path),
        "--alias",
        "your-model",
    ]


def test_run_engine_launches_local_llama_server_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    calls = {}
    monkeypatch.setattr(runtime, "detect_local_ip", lambda: "192.168.1.50")

    def fake_start_llm(model, **kwargs):
        calls["model"] = model
        calls["kwargs"] = kwargs
        return launcher.LlamaProcess(proc=FakeProc(), port=kwargs["port"], log=tmp_path / "llama.log")

    monkeypatch.setattr(launcher, "is_port_in_use", lambda port: False)
    monkeypatch.setattr(launcher, "assert_supported_build", lambda: None)
    monkeypatch.setattr(launcher, "start_llm", fake_start_llm)
    monkeypatch.setattr(launcher, "wait_for_models", lambda proc: calls.setdefault("waited", proc.port))
    monkeypatch.setattr(launcher, "stop", lambda proc: calls.setdefault("stopped", proc.port))
    monkeypatch.setattr(cli.provider, "_register_engine", lambda url, node_id, payload: calls.setdefault("payload", payload))
    monkeypatch.setattr(cli.httpx, "delete", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()))

    args = _engine_args(models=["Qwen3.5-2B-UD-IQ2_M.gguf"])

    assert cli.provider._run_engine(args) == 0
    assert calls["model"] == "Qwen3.5-2B-UD-IQ2_M.gguf"
    assert calls["kwargs"]["port"] == 8081
    assert calls["waited"] == 8081
    assert calls["stopped"] == 8081
    assert calls["payload"]["endpoint_url"] == "http://192.168.1.50:8081/v1"


def test_run_engine_advertise_as_routes_alias_and_sets_llama_alias(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    calls = {}
    monkeypatch.setattr(runtime, "detect_local_ip", lambda: "192.168.1.50")

    def fake_start_llm(model, **kwargs):
        calls["model"] = model
        calls["kwargs"] = kwargs
        return launcher.LlamaProcess(proc=FakeProc(), port=kwargs["port"], log=tmp_path / "llama.log")

    monkeypatch.setattr(launcher, "is_port_in_use", lambda port: False)
    monkeypatch.setattr(launcher, "assert_supported_build", lambda: None)
    monkeypatch.setattr(launcher, "start_llm", fake_start_llm)
    monkeypatch.setattr(launcher, "wait_for_models", lambda proc: None)
    monkeypatch.setattr(launcher, "stop", lambda proc: calls.setdefault("stopped", proc.port))
    monkeypatch.setattr(cli.provider, "_register_engine", lambda url, node_id, payload: calls.setdefault("payload", payload))
    monkeypatch.setattr(cli.httpx, "delete", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()))

    args = _engine_args(models=["your-model.gguf"], advertise_as=["your-model"])

    assert cli.provider._run_engine(args) == 0
    assert calls["model"] == "your-model.gguf"
    assert calls["kwargs"]["alias"] == "your-model"
    assert calls["payload"]["models"] == ["your-model"]
    assert calls["payload"]["endpoint_url"] == "http://192.168.1.50:8081/v1"


def test_run_engine_endpoint_url_skips_local_llama_server(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    calls = {}

    monkeypatch.setattr(launcher, "start_llm", lambda *args, **kwargs: pytest.fail("start_llm should not be called"))
    monkeypatch.setattr(cli.provider, "_register_engine", lambda url, node_id, payload: calls.setdefault("payload", payload))
    monkeypatch.setattr(cli.httpx, "delete", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()))

    args = _engine_args(models=["custom-model"], endpoint_url="http://192.168.1.50:8081/v1")

    assert cli.provider._run_engine(args) == 0
    assert calls["payload"]["endpoint_url"] == "http://192.168.1.50:8081/v1"


def test_run_engine_enable_media_advertises_media_models(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    calls = {}
    monkeypatch.setattr(runtime, "detect_local_ip", lambda: "192.168.1.50")
    monkeypatch.setattr(launcher, "is_port_in_use", lambda port: False)
    monkeypatch.setattr(launcher, "assert_supported_build", lambda: None)
    monkeypatch.setattr(
        launcher,
        "start_llm",
        lambda model, **kwargs: launcher.LlamaProcess(proc=FakeProc(), port=kwargs["port"], log=tmp_path / "llama.log"),
    )
    monkeypatch.setattr(launcher, "wait_for_models", lambda proc: None)
    monkeypatch.setattr(launcher, "stop", lambda proc: calls.setdefault("stopped_llama", proc.port))
    monkeypatch.setattr(
        cli.provider,
        "_prepare_media_engine",
        lambda args: {
            "models": ["comfyui:image_generation", "comfyui:image_editing", "comfyui:i2v"],
            "proc": None,
            "media_url": "http://192.168.1.50:8190",
            "comfyui_started": False,
        },
    )
    monkeypatch.setattr(cli.provider, "_register_engine", lambda url, node_id, payload: calls.setdefault("payload", payload))
    monkeypatch.setattr(cli.httpx, "delete", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()))

    args = _engine_args(models=["Qwen3.5-2B-UD-IQ2_M.gguf"], enable_media=True)

    assert cli.provider._run_engine(args) == 0
    assert calls["payload"]["media_url"] == "http://192.168.1.50:8190"
    assert "comfyui:image_generation" in calls["payload"]["models"]
    assert calls["payload"]["capabilities"]["models"]["comfyui:i2v"]["endpoints"] == ["media"]


def test_run_engine_media_only_skips_local_llama_server(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    calls = {}

    monkeypatch.setattr(launcher, "start_llm", lambda *args, **kwargs: pytest.fail("start_llm should not be called"))
    monkeypatch.setattr(
        cli.provider,
        "_prepare_media_engine",
        lambda args: {
            "models": ["comfyui:image_editing"],
            "proc": None,
            "media_url": "http://192.168.1.50:8190",
            "comfyui_started": False,
        },
    )
    monkeypatch.setattr(cli.provider, "_register_engine", lambda url, node_id, payload: calls.setdefault("payload", payload))
    monkeypatch.setattr(cli.httpx, "delete", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()))

    args = _engine_args(enable_media=True, media_bundles=["image_editing"])

    assert cli.provider._run_engine(args) == 0
    assert calls["payload"]["models"] == ["comfyui:image_editing"]
    assert calls["payload"]["endpoint_url"] is None
    assert calls["payload"]["media_url"] == "http://192.168.1.50:8190"
    assert calls["payload"]["capabilities"]["models"]["comfyui:image_editing"]["endpoints"] == ["media"]


def test_join_at_writes_record_and_spawns_detached(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    monkeypatch.setattr(runtime, "detect_local_ip", lambda: "192.168.1.50")
    cfg = runtime.init_grid_config(name="home", port=8090)
    spawned = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            spawned["cmd"] = cmd
            spawned["kwargs"] = kwargs
            self.pid = 4321

    monkeypatch.setattr(cli.provider.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(cli.provider, "_await_engine_start", lambda *a, **k: "registered")

    args = cli.build_parser().parse_args([
        "join",
        "home",
        "--at",
        "http://192.168.1.10:11434/v1",
        "-m",
        "llama3",
        "--name",
        "mac",
    ])
    assert cli.cmd_join(args) == 0

    records = cli.provider._read_records(cfg["grid_id"])
    assert "mac" in records
    assert records["mac"]["endpoint_url"] == "http://192.168.1.10:11434/v1"
    assert records["mac"]["models"] == ["llama3"]
    assert records["mac"]["pid"] == 4321
    assert spawned["cmd"][-3:] == ["__engine", cfg["grid_id"], "mac"]
    assert spawned["kwargs"]["start_new_session"] is True


def test_join_no_flags_single_detected_engine_joins_it(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    runtime.init_grid_config(name="home", port=8090)
    monkeypatch.setattr(
        cli.provider,
        "_detect",
        lambda host: [detect.DetectedEngine(label="ollama", endpoint_url="http://192.168.1.50:11434/v1", models=["llama3"])],
    )
    monkeypatch.setattr(cli.provider.subprocess, "Popen", lambda cmd, **kw: type("P", (), {"pid": 1})())
    monkeypatch.setattr(cli.provider, "_await_engine_start", lambda *a, **k: "registered")

    args = cli.build_parser().parse_args(["join", "home"])
    assert cli.cmd_join(args) == 0
    records = cli.provider._read_records(config.select_grid("home")["grid_id"])
    assert records["ollama"]["endpoint_url"] == "http://192.168.1.50:11434/v1"


def test_join_multiple_detected_non_interactive_requires_all(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    runtime.init_grid_config(name="home", port=8090)
    monkeypatch.setattr(
        cli.provider,
        "_detect",
        lambda host: [
            detect.DetectedEngine(label="ollama", endpoint_url="http://h:11434/v1", models=["llama3"]),
            detect.DetectedEngine(label="vllm", endpoint_url="http://h:8000/v1", models=["mistral"]),
        ],
    )
    monkeypatch.setattr(cli.provider, "_interactive", lambda: False)

    args = cli.build_parser().parse_args(["join", "home"])
    with pytest.raises(SystemExit):
        cli.cmd_join(args)


def test_join_all_joins_every_detected_engine(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    runtime.init_grid_config(name="home", port=8090)
    monkeypatch.setattr(
        cli.provider,
        "_detect",
        lambda host: [
            detect.DetectedEngine(label="ollama", endpoint_url="http://h:11434/v1", models=["llama3"]),
            detect.DetectedEngine(label="vllm", endpoint_url="http://h:8000/v1", models=["mistral"]),
        ],
    )
    monkeypatch.setattr(cli.provider.subprocess, "Popen", lambda cmd, **kw: type("P", (), {"pid": 1})())
    monkeypatch.setattr(cli.provider, "_await_engine_start", lambda *a, **k: "registered")

    args = cli.build_parser().parse_args(["join", "home", "--all"])
    assert cli.cmd_join(args) == 0
    records = cli.provider._read_records(config.select_grid("home")["grid_id"])
    assert set(records) == {"ollama", "vllm"}


def test_join_cleans_up_record_when_engine_dies(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    cfg = runtime.init_grid_config(name="home", port=8090)
    monkeypatch.setattr(cli.provider.subprocess, "Popen", lambda cmd, **kw: type("P", (), {"pid": 999_999})())
    monkeypatch.setattr(cli.provider, "_await_engine_start", lambda *a, **k: "died")

    args = cli.build_parser().parse_args([
        "join", "home", "--at", "http://192.168.1.10:11434/v1", "-m", "llama3", "--name", "bad",
    ])
    with pytest.raises(SystemExit):
        cli.cmd_join(args)
    # The stale record must not survive a failed start.
    assert cli.provider._read_records(cfg["grid_id"]) == {}


def test_join_parser_accepts_unified_remote_flags(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    args = cli.build_parser().parse_args([
        "join", "--serve", "m", "--engine-label", "rig", "--pricing-input", "0.5",
        "--pricing-output", "1.0", "--max-concurrency", "4", "--llama-port", "9001",
    ])
    assert args.engine_label == "rig"
    assert args.pricing_input == 0.5 and args.pricing_output == 1.0
    assert args.max_concurrency == 4
    assert args.endpoint_port == 9001  # --llama-port is an alias for --endpoint-port


def test_join_local_rejects_remote_only_flags(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    runtime.init_grid_config(name="home", port=8090)
    args = cli.build_parser().parse_args(["join", "home", "--serve", "m", "--max-concurrency", "4"])
    with pytest.raises(SystemExit) as exc:
        cli.cmd_join(args)  # local handler rejects a remote-only flag before doing any work
    assert "--max-concurrency" in str(exc.value) and "remote" in str(exc.value).lower()


def test_await_engine_start_distinguishes_died_registered_starting(monkeypatch):
    monkeypatch.setattr(cli.provider, "_is_registered", lambda url, node: node == "live")

    class _Proc:
        def __init__(self, code):
            self._code = code

        def poll(self):
            return self._code

    assert cli.provider._await_engine_start("http://x", "live", _Proc(None), grace=1.0) == "registered"
    assert cli.provider._await_engine_start("http://x", "n", _Proc(1), grace=1.0) == "died"
    assert cli.provider._await_engine_start("http://x", "n", _Proc(None), grace=0.3) == "starting"


def test_detect_keeps_loopback_when_local_not_bound(monkeypatch):
    monkeypatch.setattr(detect.runtime, "detect_local_ip", lambda: "10.0.0.5")

    class FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def fake_get(url, timeout=None):
        # Engine answers on loopback only; the local IP is not bound.
        if "10.0.0.5" in url:
            raise httpx.ConnectError("refused")
        if url.endswith("/api/tags"):
            return FakeResp({"models": [{"name": "llama3"}]})
        if url.endswith("/v1/models"):
            return FakeResp({"data": [{"id": "llama3"}]})
        raise httpx.ConnectError("refused")  # no comfyui

    monkeypatch.setattr(detect.httpx, "get", fake_get)

    engines = detect.detect_engines()
    assert engines
    assert all(e.endpoint_url.startswith("http://127.0.0.1:") for e in engines)


def test_detect_prefers_local_when_bound(monkeypatch):
    monkeypatch.setattr(detect.runtime, "detect_local_ip", lambda: "10.0.0.5")

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "llama3"}], "models": [{"name": "llama3"}]}

    def fake_get(url, timeout=None):
        if "system_stats" in url:
            raise httpx.ConnectError("no comfyui")
        return FakeResp()  # reachable on both loopback and local

    monkeypatch.setattr(detect.httpx, "get", fake_get)

    engines = detect.detect_engines()
    assert engines
    assert all(e.endpoint_url.startswith("http://10.0.0.5:") for e in engines)


def test_leave_all_removes_records(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    cfg = runtime.init_grid_config(name="home", port=8090)
    grid_id = cfg["grid_id"]
    cli.provider._write_record(grid_id, "mac", {"engine_id": "mac", "node_id": "n", "grid_id": grid_id, "pid": 0})
    cli.provider._write_record(grid_id, "gpu", {"engine_id": "gpu", "node_id": "n2", "grid_id": grid_id, "pid": 0})

    args = cli.build_parser().parse_args(["leave", "home", "--all"])
    assert cli.cmd_leave(args) == 0
    assert cli.provider._read_records(grid_id) == {}


def test_cli_accepts_engines_and_json_and_aliases():
    parser = cli.build_parser()

    assert parser.parse_args(["engines"]).handler is cli.cmd_engines
    assert parser.parse_args(["engines", "home", "--json"]).json is True
    assert parser.parse_args(["models", "--json"]).json is True
    assert parser.parse_args(["catalog", "--json"]).json is True
    assert parser.parse_args(["ls", "--json"]).json is True
    assert parser.parse_args(["chat", "-m", "x", "hi", "--json"]).json is True
    # aliases route to the same handlers as ls / rm
    assert parser.parse_args(["list"]).handler is cli.cmd_ls
    assert parser.parse_args(["remove", "m.gguf"]).handler is cli.cmd_rm


_FAKE_ENGINES = [
    {"name": "mac", "endpoint_url": "http://192.168.1.10:8080/v1", "models": ["gemma4-31b"]},
    {"name": "gpu", "endpoint_url": "http://192.168.1.20:8000/v1", "models": ["devstral", "gemma4-31b"]},
]


def test_models_default_is_deduped_names(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    runtime.init_grid_config(name="home", port=8090)
    monkeypatch.setattr(cli.provider, "_discover", lambda cfg: _FAKE_ENGINES)

    assert cli.cmd_models(cli.build_parser().parse_args(["models", "home"])) == 0
    assert capsys.readouterr().out.splitlines() == ["gemma4-31b", "devstral"]


def test_models_verbose_table_and_json(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    runtime.init_grid_config(name="home", port=8090)
    monkeypatch.setattr(cli.provider, "_discover", lambda cfg: _FAKE_ENGINES)

    assert cli.cmd_models(cli.build_parser().parse_args(["models", "home", "--verbose"])) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].split() == ["MODEL", "ENGINE", "WHERE"]
    assert "gemma4-31b" in out and "mac" in out and "http://192.168.1.20:8000/v1" in out

    assert cli.cmd_models(cli.build_parser().parse_args(["models", "home", "--json"])) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {"model": "devstral", "engine": "gpu", "where": "http://192.168.1.20:8000/v1"} in payload


def test_engines_json_lists_joined_engines(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    runtime.init_grid_config(name="home", port=8090)
    monkeypatch.setattr(cli.provider, "_discover", lambda cfg: _FAKE_ENGINES)

    assert cli.cmd_engines(cli.build_parser().parse_args(["engines", "home", "--json"])) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [e["engine"] for e in payload] == ["mac", "gpu"]
    assert payload[1]["models"] == ["devstral", "gemma4-31b"]


def test_info_json_uses_contract_keys(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    runtime.init_grid_config(name="home", port=8090)
    monkeypatch.setattr(cli.grid, "_live_engines", lambda url: (_FAKE_ENGINES, True))

    assert cli.cmd_info(cli.build_parser().parse_args(["info", "home", "--json"])) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"grid", "grid_url", "engines", "models"}
    assert payload["grid"] == "home"
    assert payload["models"] == ["gemma4-31b", "devstral"]


# ---------------------------------------------------------------------------
# atomic write primitive (shared/jsonio.py)
# ---------------------------------------------------------------------------

def test_atomic_write_bytes_is_0600_even_under_hostile_umask(tmp_path):
    from shared import jsonio

    target = tmp_path / "secret.bin"
    old = os.umask(0o277)  # would mask O_CREAT's mode down to 0o400 without an explicit fchmod
    try:
        jsonio.atomic_write_bytes(target, b"shhh")
    finally:
        os.umask(old)

    assert target.read_bytes() == b"shhh"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600  # fchmod defeats the umask
    assert not (tmp_path / "secret.bin.tmp").exists()  # no temp left behind


# ---------------------------------------------------------------------------
# Mode state kernel (shared/state.py)
# ---------------------------------------------------------------------------

def test_state_defaults_to_local_with_no_active_when_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))

    assert state.get_mode() == "local"
    assert state.get_active("local") is None
    assert state.get_active("remote") is None
    assert not state.state_path().exists()


def test_state_set_mode_persists_and_preserves_active(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))

    state.set_active("local", "home")
    state.set_mode("remote")

    assert state.get_mode() == "remote"
    assert state.get_active("local") == "home"  # switching mode keeps each mode's active
    payload = json.loads(state.state_path().read_text())
    assert payload["version"] == 1
    assert payload["mode"] == "remote"


def test_state_active_is_per_mode_and_preserves_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))

    state.set_mode("remote")
    state.set_active("local", "home")
    state.set_active("remote", "team")

    assert state.get_active("local") == "home"
    assert state.get_active("remote") == "team"
    assert state.get_mode() == "remote"  # setting active never changes the mode

    state.set_active("remote", None)  # clear
    assert state.get_active("remote") is None
    assert state.get_active("local") == "home"


def test_resolve_mode_override_beats_persisted(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    state.set_mode("local")

    assert state.resolve_mode("remote") == "remote"  # override wins
    assert state.resolve_mode(None) == "local"       # falls back to persisted


def test_state_rejects_unknown_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))

    with pytest.raises(SystemExit):
        state.set_mode("public")
    with pytest.raises(SystemExit):
        state.resolve_mode("public")


def test_state_recovers_from_malformed_file(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    state.state_path().parent.mkdir(parents=True, exist_ok=True)
    state.state_path().write_text("{ this is not json")

    assert state.get_mode() == "local"  # lenient: corrupt file => defaults
    state.set_mode("remote")           # self-heals on next write
    assert state.get_mode() == "remote"


# ---------------------------------------------------------------------------
# grid mode / grid use commands
# ---------------------------------------------------------------------------

def test_grid_mode_reads_and_persists(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))

    assert cli.cmd_mode(cli.build_parser().parse_args(["mode"])) == 0
    assert capsys.readouterr().out.strip() == "local"

    assert cli.cmd_mode(cli.build_parser().parse_args(["mode", "remote"])) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "remote"
    assert "grid login" in out  # switching to remote mode points at sign-in + grid management
    assert "grid chat" in out  # consume has shipped — the switch points at using a remote grid
    assert "later release" not in out  # no stale "chatting comes later" line
    assert state.get_mode() == "remote"

    assert cli.cmd_mode(cli.build_parser().parse_args(["mode", "--json"])) == 0
    assert json.loads(capsys.readouterr().out) == {"mode": "remote"}


def test_grid_use_sets_reads_and_clears_active_local(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    runtime.init_grid_config(name="home", port=8090)
    runtime.init_grid_config(name="beta", port=8091)

    assert cli.cmd_use(cli.build_parser().parse_args(["use"])) == 0
    assert "no active grid" in capsys.readouterr().out

    assert cli.cmd_use(cli.build_parser().parse_args(["use", "beta"])) == 0
    capsys.readouterr()
    assert state.get_active("local") == "beta"

    assert cli.cmd_use(cli.build_parser().parse_args(["use", "--json"])) == 0
    assert json.loads(capsys.readouterr().out) == {"mode": "local", "active": "beta"}

    assert cli.cmd_use(cli.build_parser().parse_args(["use", "--none"])) == 0
    capsys.readouterr()
    assert state.get_active("local") is None


def test_grid_use_rejects_unknown_grid_in_local(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    runtime.init_grid_config(name="home", port=8090)

    with pytest.raises(SystemExit):
        cli.cmd_use(cli.build_parser().parse_args(["use", "ghost"]))
    assert state.get_active("local") is None  # nothing persisted on failure


# ---------------------------------------------------------------------------
# mode-aware dispatch (cli/dispatch.py)
# ---------------------------------------------------------------------------

def test_resolve_override_strips_flag_in_any_position():
    assert dispatch.resolve_override(["--remote", "up"]) == ("remote", ["up"])
    assert dispatch.resolve_override(["up", "--remote"]) == ("remote", ["up"])
    assert dispatch.resolve_override(["models", "--local", "home"]) == ("local", ["models", "home"])
    assert dispatch.resolve_override(["up"]) == (None, ["up"])
    with pytest.raises(SystemExit):
        dispatch.resolve_override(["--local", "--remote", "up"])


def test_dispatch_stubs_unimplemented_remote_commands(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    state.set_mode("remote")

    # Gated commands without a real remote handler yet still hit the "not available" stub.
    # (up/down/ls/info and join/leave now have handlers — covered by their own tests below.)
    for command in ("models", "engines"):
        with pytest.raises(SystemExit) as exc:
            cli.main([command])
        assert "remote mode yet" in str(exc.value).lower()


def test_remote_lifecycle_requires_session_when_signed_out(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    state.set_mode("remote")  # remote mode, but not signed in

    # The lifecycle verbs are no longer stubbed: they reach the auth gate, not the old stub.
    for argv in (["up", "team"], ["down", "team"], ["info", "team"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert "login" in str(exc.value).lower()


def test_remote_up_create_rejects_missing_network_id(monkeypatch, tmp_path):
    _seed_remote(monkeypatch, tmp_path)
    # 200 OK but no network_id (API regression): a clean error, not false success + a later KeyError.
    _mock_lifecycle(monkeypatch, create={"name": "team", "network_type": "permissioned-public"})
    with pytest.raises(SystemExit) as exc:
        cli.main(["up", "team"])
    assert "no usable id" in str(exc.value).lower()

    from remote import credentials
    assert credentials.load_credentials()["networks"] == []  # nothing persisted


def test_remote_down_rejects_unsafe_network_id(monkeypatch, tmp_path):
    # A stored id that could re-target the request path is refused before any control-plane call.
    _seed_remote(monkeypatch, tmp_path, networks=[{"network_id": "n1/../admin", "name": "team"}])
    calls = _mock_lifecycle(monkeypatch, stop={"status": "stopped"})
    with pytest.raises(SystemExit):
        cli.main(["down", "team"])
    assert "stop" not in calls  # rejected before reaching the network


def test_remote_down_bare_stops_active_grid(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                networks=[{"network_id": "n1", "name": "team"}, {"network_id": "n2", "name": "lab"}],
                active="lab")
    calls = _mock_lifecycle(monkeypatch, stop={"status": "stopped"})
    assert cli.main(["down"]) == 0  # no name → the active grid
    assert calls.get("stop") == {"session": "sess-tok", "network_id": "n2"}
    assert "lab" in capsys.readouterr().out


def test_remote_info_bare_uses_sole_grid(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                networks=[{"network_id": "n1", "name": "team", "network_type": "permissioned-public"}])
    _mock_lifecycle(monkeypatch, status={"state": "running", "signaling_url": "https://r"})
    assert cli.main(["info"]) == 0  # no name → the sole grid
    out = capsys.readouterr().out
    assert "grid=team" in out and "status=running" in out


# ---------------------------------------------------------------------------
# Remote `grid join` / `grid leave` (cli/remote_provider.py + dispatch + __remote-engine)
# ---------------------------------------------------------------------------

def _seed_running_remote_grid(monkeypatch, tmp_path, *, access_token="AT"):
    """A signed-in remote mode user with one *running* grid (status mocked) for the join tests."""
    net = {"network_id": "n1", "name": "team", "network_type": "permissioned-public"}
    if access_token is not None:
        net["access_token"], net["refresh_token"] = access_token, "RT"
    _seed_remote(monkeypatch, tmp_path, networks=[net], active="team")
    _mock_lifecycle(monkeypatch, status={"state": "running", "signaling_url": "https://relay.example"})


def _mock_remote_spawn(monkeypatch, *, pid=4242):
    """Capture the detached __remote-engine spawn and skip the real liveness wait."""
    spawned = {}

    def fake_popen(cmd, **kw):
        spawned["cmd"] = cmd
        return type("P", (), {"pid": pid})()

    monkeypatch.setattr(cli.remote_provider.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cli.remote_provider, "_await_remote_engine_start", lambda *a, **k: "starting")
    return spawned


def test_remote_join_serve_writes_record_and_spawns_remote_engine(monkeypatch, tmp_path):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    spawned = _mock_remote_spawn(monkeypatch)

    assert cli.main(["join", "--serve", "m"]) == 0

    records = cli.provider._read_records("n1")  # remote records live under engines_dir(network_id)
    assert len(records) == 1
    (engine_id, record), = records.items()
    assert record["signaling_url"] == "https://relay.example"
    assert record["models"] == ["m"] and record["endpoint_url"] is None and record["grid_id"] == "n1"
    assert "access_token" not in record  # the token stays in credentials.toml, never the run record
    assert spawned["cmd"][-3:] == ["__remote-engine", "n1", engine_id]


def test_remote_join_at_serves_external_engine(monkeypatch, tmp_path):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)

    assert cli.main(["join", "--at", "http://192.168.1.9:11434/v1", "-m", "llama3", "--name", "ext"]) == 0
    record = cli.provider._read_records("n1")["ext"]
    assert record["endpoint_url"] == "http://192.168.1.9:11434/v1" and record["models"] == ["llama3"]


def test_remote_join_member_falls_back_to_bundle_url_when_status_forbidden(monkeypatch, tmp_path):
    """A provider MEMBER (not the grid creator) gets 403 from the creator-only status endpoint; join
    must fall back to the lan_signaling_url the login bundle carries instead of failing."""
    from remote import control_plane

    net = {"network_id": "n1", "name": "team", "network_type": "permissioned-public",
           "access_token": "AT", "refresh_token": "RT", "lan_signaling_url": "https://grid.example/n1"}
    _seed_remote(monkeypatch, tmp_path, networks=[net], active="team")

    def _forbidden(session_token, network_id, api_url=None):
        raise SystemExit("GET .../status failed (403): Only the network creator can manage it")

    monkeypatch.setattr(control_plane, "get_managed_network_status", _forbidden)
    _mock_remote_spawn(monkeypatch)

    assert cli.main(["join", "--serve", "m"]) == 0
    record = next(iter(cli.provider._read_records("n1").values()))
    assert record["signaling_url"] == "https://grid.example/n1"  # from the bundle, not the denied status


def test_remote_join_member_without_stored_url_surfaces_status_error(monkeypatch, tmp_path):
    """Status denied AND no relay URL in the bundle → surface the original error, never silently pass."""
    from remote import control_plane

    net = {"network_id": "n1", "name": "team", "access_token": "AT", "refresh_token": "RT"}  # no lan_signaling_url
    _seed_remote(monkeypatch, tmp_path, networks=[net], active="team")

    def _forbidden(session_token, network_id, api_url=None):
        raise SystemExit("GET .../status failed (403): Only the network creator can manage it")

    monkeypatch.setattr(control_plane, "get_managed_network_status", _forbidden)
    with pytest.raises(SystemExit) as exc:
        cli.main(["join", "--serve", "m"])
    assert "creator" in str(exc.value).lower()


def test_remote_join_autodetects_single_engine(monkeypatch, tmp_path):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    monkeypatch.setattr(cli.provider, "_detect", lambda host: [
        detect.DetectedEngine(label="ollama", endpoint_url="http://h:11434/v1", models=["llama3"]),
    ])

    assert cli.main(["join"]) == 0
    record = next(iter(cli.provider._read_records("n1").values()))
    assert record["endpoint_url"] == "http://h:11434/v1" and record["models"] == ["llama3"]


def test_remote_join_all_serves_every_detected(monkeypatch, tmp_path):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    spawned = _mock_remote_spawn(monkeypatch)
    monkeypatch.setattr(cli.provider, "_detect", lambda host: [
        detect.DetectedEngine(label="ollama", endpoint_url="http://h:11434/v1", models=["llama3"]),
        detect.DetectedEngine(label="vllm", endpoint_url="http://h:8000/v1", models=["mistral"]),
    ])

    assert cli.main(["join", "--all"]) == 0

    records = cli.provider._read_records("n1")
    assert len(records) == 1  # one identity serves both engines
    (engine_id, record), = records.items()
    assert [e["endpoint_url"] for e in record["engines"]] == ["http://h:11434/v1", "http://h:8000/v1"]
    assert record["models"] == ["llama3", "mistral"]  # union, in detect order
    assert record["endpoint_url"] is None  # no single endpoint when several engines
    assert spawned["cmd"][-3:] == ["__remote-engine", "n1", engine_id]


def test_remote_join_all_warns_on_shadowed_model(monkeypatch, tmp_path, capsys):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    monkeypatch.setattr(cli.provider, "_detect", lambda host: [
        detect.DetectedEngine(label="ollama", endpoint_url="http://h:11434/v1", models=["llama3"]),
        detect.DetectedEngine(label="lm-studio", endpoint_url="http://h:1234/v1", models=["llama3"]),
    ])

    assert cli.main(["join", "--all"]) == 0
    err = capsys.readouterr().err
    assert "llama3" in err and "more than one engine" in err  # operator sees the shadowing at join time


def test_remote_join_all_rejects_advertise_as(monkeypatch, tmp_path):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    monkeypatch.setattr(cli.provider, "_detect", lambda host: [
        detect.DetectedEngine(label="ollama", endpoint_url="http://h:11434/v1", models=["llama3"]),
        detect.DetectedEngine(label="vllm", endpoint_url="http://h:8000/v1", models=["mistral"]),
    ])
    with pytest.raises(SystemExit) as exc:
        cli.main(["join", "--all", "--advertise-as", "x"])
    assert "advertise-as" in str(exc.value).lower()


def test_remote_join_rejects_media(monkeypatch, tmp_path):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        cli.main(["join", "--media"])
    assert "media" in str(exc.value).lower()


def test_remote_join_rejects_advertise_host(monkeypatch, tmp_path):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        cli.main(["join", "--serve", "m", "--advertise-host", "1.2.3.4"])
    assert "advertise-host" in str(exc.value).lower()


def test_remote_join_rejects_multiple_detected(monkeypatch, tmp_path):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    monkeypatch.setattr(cli.provider, "_detect", lambda host: [
        detect.DetectedEngine(label="ollama", endpoint_url="http://h:11434/v1", models=["llama3"]),
        detect.DetectedEngine(label="vllm", endpoint_url="http://h:8000/v1", models=["mistral"]),
    ])
    with pytest.raises(SystemExit) as exc:
        cli.main(["join"])
    assert "multiple engines" in str(exc.value).lower()  # bare join still needs --all/--engine to disambiguate


def test_remote_join_requires_sign_in(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    state.set_mode("remote")  # remote mode, not signed in
    with pytest.raises(SystemExit) as exc:
        cli.main(["join", "--serve", "m"])
    assert "login" in str(exc.value).lower()


def test_remote_join_requires_access_token(monkeypatch, tmp_path):
    _seed_remote(monkeypatch, tmp_path, networks=[{"network_id": "n1", "name": "team"}], active="team")  # no token
    with pytest.raises(SystemExit) as exc:
        cli.main(["join", "--serve", "m"])
    assert "login" in str(exc.value).lower()


def test_remote_join_requires_grid_up(monkeypatch, tmp_path):
    _seed_remote(monkeypatch, tmp_path,
                networks=[{"network_id": "n1", "name": "team", "access_token": "AT", "refresh_token": "RT"}],
                active="team")
    _mock_lifecycle(monkeypatch, status={"state": "stopped"})  # down → no relay address
    monkeypatch.setattr(cli.remote_provider.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("must not spawn when the grid is down"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["join", "--serve", "m"])
    assert "grid up" in str(exc.value).lower()


def test_remote_join_died_cleans_up_record(monkeypatch, tmp_path):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    monkeypatch.setattr(cli.remote_provider.subprocess, "Popen", lambda cmd, **kw: type("P", (), {"pid": 999_999})())
    monkeypatch.setattr(cli.remote_provider, "_await_remote_engine_start", lambda *a, **k: "died")
    with pytest.raises(SystemExit):
        cli.main(["join", "--serve", "m", "--name", "bad"])
    assert cli.provider._read_records("n1") == {}  # stale record removed on a failed start


def test_remote_leave_stops_and_removes_record(monkeypatch, tmp_path, capsys):
    from shared import run_records

    _seed_remote(monkeypatch, tmp_path,
                networks=[{"network_id": "n1", "name": "team", "access_token": "AT"}], active="team")
    run_records.write_record("n1", "rig", {"engine_id": "rig", "node_id": "node-x", "grid_id": "n1", "pid": 0})

    assert cli.main(["leave", "--engine", "rig"]) == 0
    assert cli.provider._read_records("n1") == {}
    assert "Left engine rig" in capsys.readouterr().out


def test_dispatch_runs_agnostic_command_in_remote(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    state.set_mode("remote")

    assert cli.main(["catalog", "--json"]) == 0  # agnostic: runs even in remote mode
    assert json.loads(capsys.readouterr().out)  # produced the catalog payload


def test_override_sets_remote_active_without_persisting_mode(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))  # persisted mode stays local

    assert cli.main(["--remote", "use", "team"]) == 0  # G1: --remote reaches cmd_use
    capsys.readouterr()
    assert state.get_active("remote") == "team"
    assert state.get_active("local") is None
    assert state.get_mode() == "local"  # the one-shot override did not persist


def test_mode_query_ignores_override(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))

    assert cli.main(["--remote", "mode"]) == 0
    assert capsys.readouterr().out.strip() == "local"  # prints persisted mode, not the override


def test_both_mode_flags_is_error(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    with pytest.raises(SystemExit):
        cli.main(["--local", "--remote", "mode"])


def test_every_command_is_classified_for_dispatch():
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    classified = set(dispatch.AGNOSTIC) | set(dispatch.REMOTE_HANDLERS) | set(dispatch.REMOTE_ONLY)
    unclassified = set(sub.choices) - classified
    assert not unclassified, f"unclassified commands: {unclassified}"


def test_active_selection_flows_through_select_grid(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    runtime.init_grid_config(name="home", port=8090)
    runtime.init_grid_config(name="beta", port=8091)

    assert config.select_grid(None)["name"] == "home"  # default is home

    state.set_active("local", "beta")
    assert config.select_grid(None)["name"] == "beta"  # active overrides the default

    state.set_active("local", "ghost")
    assert config.select_grid(None)["name"] == "home"  # stale active is ignored


# ---------------------------------------------------------------------------
# bare `grid` overview (mode-aware)
# ---------------------------------------------------------------------------

def test_overview_honors_active_with_multiple_grids(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    runtime.init_grid_config(name="alpha", port=8090)
    runtime.init_grid_config(name="beta", port=8091)
    monkeypatch.setattr(cli.grid, "_live_engines", lambda url: ([], False))

    state.set_active("local", "beta")
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "mode: local"
    assert "Grid: beta" in out  # G2: active honored even with two non-home grids


def test_overview_json_local_contract(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    runtime.init_grid_config(name="home", port=8090)
    monkeypatch.setattr(cli.grid, "_live_engines", lambda url: (_FAKE_ENGINES, True))

    assert cli.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "local"
    assert payload["grid"] == "home"
    assert payload["models"] == ["gemma4-31b", "devstral"]


def test_overview_remote_is_stub_without_network(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    state.set_mode("remote")

    def _boom(url):
        raise AssertionError("overview must not hit the network in remote mode")

    monkeypatch.setattr(cli.grid, "_live_engines", _boom)

    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "mode: remote"
    assert "grid login" in out  # accurate remote guidance, still no network call
    assert "grid chat" in out  # consume has shipped — overview points at it
    assert "later release" not in out  # the stale "chatting comes later" line is gone


def test_overview_json_local_no_grids_has_stable_keys(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))

    assert cli.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"mode", "grid", "grid_url", "engines", "models"}
    assert payload["grid"] is None and payload["grid_url"] is None


def test_grid_use_name_and_none_conflict(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    runtime.init_grid_config(name="home", port=8090)

    with pytest.raises(SystemExit):
        cli.cmd_use(cli.build_parser().parse_args(["use", "home", "--none"]))


def test_load_json_reports_corrupt_file_cleanly(tmp_path):
    from shared import jsonio

    bad = tmp_path / "config.json"
    bad.write_text("{ not valid json")
    with pytest.raises(SystemExit):
        jsonio.load_json(bad)


def test_live_engines_tolerates_non_dict_response(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return ["not", "a", "dict"]

    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: _Resp())
    assert cli.grid._live_engines("http://192.168.1.1:8090") == ([], False)


# ---------------------------------------------------------------------------
# Remote credential store (remote/credentials.py)
# ---------------------------------------------------------------------------

def test_device_id_generates_once_and_survives_logout(monkeypatch, tmp_path):
    from remote import credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    first = credentials.device_id()
    assert first  # a uuid string
    assert credentials.device_id() == first  # reused, not regenerated
    assert paths.device_file().exists()

    credentials.save_credentials({"session_token": "s"})
    credentials.clear_credentials()  # logout
    assert credentials.device_id() == first  # device id is independent of credentials


def test_credentials_roundtrip_is_0600(monkeypatch, tmp_path):
    from remote import credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    bundle = {"session_token": "tok", "api_url": "https://api.example", "networks": []}
    credentials.save_credentials(bundle)

    assert credentials.load_credentials() == bundle
    assert stat.S_IMODE(paths.credentials_file().stat().st_mode) == 0o600
    assert tomllib.loads(paths.credentials_file().read_text()) == bundle


def test_clear_credentials_reports_existence(monkeypatch, tmp_path):
    from remote import credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    assert credentials.clear_credentials() is False  # nothing to clear
    credentials.save_credentials({"session_token": "tok"})
    assert credentials.clear_credentials() is True
    assert not paths.credentials_file().exists()


def test_require_session_gates_when_signed_out(monkeypatch, tmp_path):
    from remote import credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        credentials.require_session()
    assert "grid login" in str(exc.value)

    credentials.save_credentials({"session_token": "tok"})
    assert credentials.require_session() == "tok"


def test_api_url_resolution_precedence(monkeypatch, tmp_path):
    from remote import credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    monkeypatch.delenv("GRID_CONTROL_PLANE_URL", raising=False)
    assert credentials.api_url() == "https://api-grid.autonomous.ai"  # default

    monkeypatch.setenv("GRID_CONTROL_PLANE_URL", "https://env.example/")
    assert credentials.api_url() == "https://env.example"  # env, normalized

    credentials.save_credentials({"api_url": "https://stored.example"})
    assert credentials.api_url() == "https://stored.example"  # stored beats env
    assert credentials.api_url("https://explicit.example/") == "https://explicit.example"  # arg wins


def test_default_website_url_empty_env_falls_back_to_server(monkeypatch):
    from remote import credentials

    monkeypatch.delenv("GRID_WEBSITE_URL", raising=False)
    assert credentials.default_website_url() == "https://autonomous.ai"
    monkeypatch.setenv("GRID_WEBSITE_URL", "")  # opt out of the constructed URL
    assert credentials.default_website_url() == ""


def test_update_network_tokens_replaces_in_place_and_preserves_rest(monkeypatch, tmp_path):
    from remote import credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    credentials.save_credentials({
        "session_token": "sess", "api_url": "https://api.example", "user": {"email": "a@b.com"},
        "networks": [
            {"network_id": "n1", "name": "team", "access_token": "AT1", "refresh_token": "RT1"},
            {"network_id": "n2", "name": "lab", "access_token": "ATx", "refresh_token": "RTx"},
        ],
    })

    credentials.update_network_tokens("n1", access_token="AT2", refresh_token="RT2")

    data = credentials.load_credentials()
    assert data["session_token"] == "sess" and data["user"] == {"email": "a@b.com"}  # rest untouched
    assert [n["network_id"] for n in data["networks"]] == ["n1", "n2"]  # order preserved
    nets = {n["network_id"]: n for n in data["networks"]}
    assert nets["n1"] == {"network_id": "n1", "name": "team", "access_token": "AT2", "refresh_token": "RT2"}
    assert nets["n2"]["access_token"] == "ATx"  # the other grid is left alone
    assert stat.S_IMODE(paths.credentials_file().stat().st_mode) == 0o600  # still secret-only


def test_update_network_tokens_keeps_refresh_when_not_rotated(monkeypatch, tmp_path):
    from remote import credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    credentials.save_credentials({"networks": [{"network_id": "n1", "access_token": "AT1", "refresh_token": "RT1"}]})

    credentials.update_network_tokens("n1", access_token="AT2")  # server didn't rotate the refresh token

    assert credentials.load_credentials()["networks"][0] == {
        "network_id": "n1", "access_token": "AT2", "refresh_token": "RT1",
    }


# ---------------------------------------------------------------------------
# Remote control-plane client (remote/control_plane.py)
# ---------------------------------------------------------------------------

def _mock_control_plane(monkeypatch, handler):
    """Serve the remote client's HTTP via httpx.MockTransport — real request-building, no network."""
    from remote import control_plane

    real_client = httpx.Client
    monkeypatch.setattr(
        control_plane.httpx,
        "Client",
        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )


def test_control_plane_start_device_login(monkeypatch, tmp_path):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def handler(request):
        seen["method"], seen["path"] = request.method, request.url.path
        return httpx.Response(200, json={"user_code": "UC", "device_code": "dc", "interval": 2})

    _mock_control_plane(monkeypatch, handler)
    assert control_plane.start_device_login()["user_code"] == "UC"
    assert (seen["method"], seen["path"]) == ("POST", "/v1/grid/auth/device/start")


def test_control_plane_poll_sends_device_code(monkeypatch, tmp_path):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def handler(request):
        seen["path"], seen["body"] = request.url.path, json.loads(request.content)
        return httpx.Response(200, json={"status": "pending"})

    _mock_control_plane(monkeypatch, handler)
    assert control_plane.poll_device_login("dc123")["status"] == "pending"
    assert seen["path"] == "/v1/grid/auth/device/poll"
    assert seen["body"] == {"device_code": "dc123"}


def test_control_plane_fetch_tokens_attaches_bearer_and_query(monkeypatch, tmp_path):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["device_id"] = request.url.params.get("device_id")
        return httpx.Response(200, json={"networks": [{"network_id": "n1", "name": "team"}]})

    _mock_control_plane(monkeypatch, handler)
    nets = control_plane.fetch_tokens("sess-tok", "dev-1")
    assert nets == [{"network_id": "n1", "name": "team"}]
    assert seen["auth"] == "Bearer sess-tok"
    assert seen["device_id"] == "dev-1"


def test_control_plane_fetch_tokens_defaults_missing_networks_to_empty(monkeypatch, tmp_path):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _mock_control_plane(monkeypatch, lambda r: httpx.Response(200, json={"networks": None}))
    assert control_plane.fetch_tokens("sess", "dev-1") == []


def test_control_plane_raises_on_error_status(monkeypatch, tmp_path):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _mock_control_plane(monkeypatch, lambda r: httpx.Response(401, text="denied"))
    with pytest.raises(SystemExit) as exc:
        control_plane.start_device_login()
    assert "401" in str(exc.value)


def test_control_plane_refresh_network_token_posts_refresh_unauthenticated(monkeypatch, tmp_path):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def handler(request):
        seen["method"], seen["path"] = request.method, request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"access_token": "AT2", "refresh_token": "RT2"})

    _mock_control_plane(monkeypatch, handler)
    bundle = control_plane.refresh_network_token(network_id="n1", refresh_token="RT1")
    assert bundle == {"access_token": "AT2", "refresh_token": "RT2"}
    assert (seen["method"], seen["path"]) == ("POST", "/v1/grid/tokens/n1")
    assert seen["body"] == {"refresh_token": "RT1"}
    assert seen["auth"] is None  # the refresh token IS the credential — no Bearer header


def test_control_plane_refresh_network_token_raises_on_error(monkeypatch, tmp_path):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _mock_control_plane(monkeypatch, lambda r: httpx.Response(401, text="bad refresh"))
    with pytest.raises(SystemExit) as exc:
        control_plane.refresh_network_token(network_id="n1", refresh_token="RT1")
    assert "401" in str(exc.value)


# ---------------------------------------------------------------------------
# Remote relay client (remote/relay.py)
# ---------------------------------------------------------------------------

def _mock_relay(monkeypatch, handler, _real=httpx.Client):
    """Serve the relay client's HTTP via httpx.MockTransport — real request-building, no network.

    ``_real`` is bound to the genuine ``httpx.Client`` once at import, so a test can call this more
    than once (re-mocking per status) without the second patch wrapping the first.
    """
    from remote import relay

    monkeypatch.setattr(
        relay.httpx,
        "Client",
        lambda *a, **k: _real(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )


def test_relay_register_node_puts_envelope_with_bearer(monkeypatch, tmp_path):
    from remote import relay

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def handler(request):
        seen["method"], seen["path"] = request.method, request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "ok"})

    _mock_relay(monkeypatch, handler)
    relay.register_node(
        "https://relay.example", "AT", "node-1",
        models=["m"], capabilities={"schema_version": 1, "models": {}},
        meta={"name": "e1", "engine": "llama.cpp"}, max_concurrency=4,
    )
    assert (seen["method"], seen["path"]) == ("PUT", "/nodes/node-1")
    assert seen["auth"] == "Bearer AT"
    assert seen["body"]["role"] == "provider" and seen["body"]["models"] == ["m"]
    assert seen["body"]["capabilities"] == {"schema_version": 1, "models": {}}
    assert seen["body"]["meta"]["engine"] == "llama.cpp" and seen["body"]["max_concurrency"] == 4


def test_relay_register_node_raises_typed_errors(monkeypatch, tmp_path):
    from remote import relay

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _mock_relay(monkeypatch, lambda r: httpx.Response(401, text="nope"))
    with pytest.raises(relay.RelayUnauthorized):
        relay.register_node("https://r", "AT", "n", models=["m"])
    _mock_relay(monkeypatch, lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(relay.RelayError):
        relay.register_node("https://r", "AT", "n", models=["m"])


def test_relay_poll_maps_status_to_job_none_or_signal(monkeypatch, tmp_path):
    from remote import relay

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    job = {"transaction_id": "t1", "endpoint_path": "chat/completions", "body": {"model": "m"}, "is_stream": False}
    seen = {}

    def ok(request):
        seen["path"], seen["auth"] = request.url.path, request.headers.get("authorization")
        return httpx.Response(200, json=job)

    _mock_relay(monkeypatch, ok)
    assert relay.poll("https://r", "AT") == job
    assert seen["path"] == "/relay/v1/poll" and seen["auth"] == "Bearer AT"

    _mock_relay(monkeypatch, lambda r: httpx.Response(204))
    assert relay.poll("https://r", "AT") is None  # no work

    _mock_relay(monkeypatch, lambda r: httpx.Response(401))
    with pytest.raises(relay.RelayUnauthorized):
        relay.poll("https://r", "AT")

    _mock_relay(monkeypatch, lambda r: httpx.Response(503))
    with pytest.raises(relay.RelayError):  # transient — caller backs off, doesn't die
        relay.poll("https://r", "AT")


def test_relay_heartbeat_body_has_no_node_id(monkeypatch, tmp_path):
    from remote import relay

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def handler(request):
        seen["path"], seen["body"] = request.url.path, json.loads(request.content)
        return httpx.Response(200, json={"ttl_seconds": 120})

    _mock_relay(monkeypatch, handler)
    assert relay.heartbeat("https://r", "AT", load={"active_tasks": 1}) == "ok"
    assert seen["path"] == "/nodes/heartbeat"
    assert seen["body"] == {"load": {"active_tasks": 1}}  # token identifies the node, not a body field

    _mock_relay(monkeypatch, lambda r: httpx.Response(404))
    assert relay.heartbeat("https://r", "AT", load={}) == "missing"  # pruned → caller re-registers

    _mock_relay(monkeypatch, lambda r: httpx.Response(401))
    with pytest.raises(relay.RelayUnauthorized):
        relay.heartbeat("https://r", "AT", load={})


def test_relay_submit_response_non_stream_and_stream(monkeypatch, tmp_path):
    from remote import relay

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def handler(request):
        seen["path"], seen["ctype"], seen["content"] = (
            request.url.path, request.headers.get("content-type"), request.content,
        )
        return httpx.Response(200, json={"status": "delivered"})

    _mock_relay(monkeypatch, handler)
    relay.submit_response("https://r", "AT", "t1", content=b'{"ok":true}', stream=False)
    assert seen["path"] == "/relay/v1/response/t1" and "application/json" in seen["ctype"]
    assert seen["content"] == b'{"ok":true}'

    def chunks():
        yield b"data: a\n\n"
        yield b"data: [DONE]\n\n"

    _mock_relay(monkeypatch, handler)
    relay.submit_response("https://r", "AT", "t1", content=chunks(), stream=True)
    assert "text/event-stream" in seen["ctype"]
    assert seen["content"] == b"data: a\n\ndata: [DONE]\n\n"


def test_relay_submit_error_posts_message(monkeypatch, tmp_path):
    from remote import relay

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def handler(request):
        seen["path"], seen["body"] = request.url.path, json.loads(request.content)
        return httpx.Response(200, json={"status": "failed"})

    _mock_relay(monkeypatch, handler)
    relay.submit_error("https://r", "AT", "t1", message="LLM error: 500", tokens_delivered=0)
    assert seen["path"] == "/relay/v1/error/t1"
    assert seen["body"] == {"error": "LLM error: 500", "tokens_delivered": 0}


def test_relay_unregister_flips_role_to_consumer_best_effort(monkeypatch, tmp_path):
    from remote import relay

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def handler(request):
        seen["method"], seen["path"], seen["body"] = (
            request.method, request.url.path, json.loads(request.content),
        )
        return httpx.Response(200, json={"status": "updated"})

    _mock_relay(monkeypatch, handler)
    relay.unregister_node("https://r", "AT", "node-1")
    assert (seen["method"], seen["path"]) == ("PUT", "/nodes/node-1")
    assert seen["body"]["role"] == "consumer" and seen["body"]["models"] == []

    _mock_relay(monkeypatch, lambda r: httpx.Response(500))
    relay.unregister_node("https://r", "AT", "n")  # best-effort: a failed drain never raises


# ---------------------------------------------------------------------------
# Remote capability probe + benchmark (remote/probe.py)
# ---------------------------------------------------------------------------

def _mock_engine(monkeypatch, handler, _real=httpx.Client):
    """Serve a probed local engine's HTTP via httpx.MockTransport (patches remote.probe's client)."""
    from remote import probe

    monkeypatch.setattr(
        probe.httpx,
        "Client",
        lambda *a, **k: _real(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )


def _capable_engine_handler(request):
    """A local engine that supports vision + tools + json modes, for the happy-path probe."""
    path = request.url.path
    if path == "/props":
        return httpx.Response(200, json={
            "chat_template_caps": {
                "supports_tools": True, "supports_tool_calls": True, "supports_parallel_tool_calls": True,
            },
            "modalities": {"vision": True},
        })
    if path.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": "m", "capabilities": ["vision"]}]})
    if path.endswith("/chat/completions"):
        body = json.loads(request.content)
        if body.get("tools"):
            return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [{"id": "1"}]}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})
    return httpx.Response(404)


def test_probe_capabilities_builds_envelope_from_live_probe(monkeypatch, tmp_path):
    from remote import probe

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _mock_engine(monkeypatch, _capable_engine_handler)

    env = probe.capabilities("http://h:8081/v1", "m")

    assert env["schema_version"] == 1
    entry = env["models"]["m"]
    assert entry["endpoints"] == ["chat/completions", "completions"]
    assert entry["input_modalities"] == ["text", "image"]  # vision detected
    assert entry["features"]["vision"] is True
    assert entry["features"]["tools"] is True
    assert entry["features"]["parallel_tool_calls"] is True
    assert entry["features"]["json_object"] is True
    assert entry["features"]["json_schema"] is True


def test_probe_capabilities_degrades_to_text_only_on_probe_failure(monkeypatch, tmp_path):
    from remote import probe

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _mock_engine(monkeypatch, lambda r: httpx.Response(500))  # engine refuses every probe

    env = probe.capabilities("http://h:8081/v1", "m")

    assert env["schema_version"] == 1 and set(env["models"]) == {"m"}  # still a valid envelope
    entry = env["models"]["m"]
    assert entry["input_modalities"] == ["text"]
    assert entry["features"]["vision"] is False
    assert entry["features"]["tools"] is False
    assert entry["features"]["json_object"] is False


def test_probe_benchmark_tok_s_prefers_predicted_per_second(monkeypatch, tmp_path):
    from remote import probe

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _mock_engine(monkeypatch, lambda r: httpx.Response(
        200, json={"timings": {"predicted_per_second": 42.5}, "usage": {"completion_tokens": 10}}))
    assert probe.benchmark_tok_s("http://h:8081/v1", "m") == 42.5


def test_probe_benchmark_tok_s_is_none_on_engine_error(monkeypatch, tmp_path):
    from remote import probe

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _mock_engine(monkeypatch, lambda r: httpx.Response(500))
    assert probe.benchmark_tok_s("http://h:8081/v1", "m") is None


def test_probe_tok_s_from_response_extracts_or_none():
    from remote import probe

    assert probe.tok_s_from_response({"timings": {"predicted_per_second": 12.0}}) == 12.0
    assert probe.tok_s_from_response({"timings": {}}) is None
    assert probe.tok_s_from_response({}) is None


# ---------------------------------------------------------------------------
# Remote serve loop (remote/serve.py)
# ---------------------------------------------------------------------------

def _mock_serve_engine(monkeypatch, handler, _real=httpx.Client):
    """Serve the local engine the serve loop forwards to, via httpx.MockTransport."""
    monkeypatch.setattr(
        httpx, "Client",
        lambda *a, **k: _real(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )


def _serve_state(monkeypatch, tmp_path, **overrides):
    from remote import serve

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    kwargs = dict(
        signaling_url="https://relay.example", node_id="node-1", network_id="n1",
        llm_url="http://127.0.0.1:8081/v1", access_token="AT", refresh_token="RT",
        models=["m"], capabilities={"schema_version": 1, "models": {}},
        meta={"name": "e1", "engine": "llama.cpp"}, pricing={}, max_concurrency=1,
    )
    kwargs.update(overrides)
    return serve._ServeState(**kwargs)


def test_serve_node_id_comes_from_access_token_jwt():
    """The relay binds node_id to the token (PUT /nodes/{id} → 403 'Cannot access another node'
    otherwise), so the serve loop must read node_id from the JWT claim, never invent a random one."""
    import base64
    import json as _json

    from remote import serve

    def _jwt(claims):
        body = base64.urlsafe_b64encode(_json.dumps(claims).encode()).decode().rstrip("=")
        return f"header.{body}.sig"

    assert serve._node_id_from_token(_jwt({"node_id": "node-abc", "sub": "u1"})) == "node-abc"
    assert serve._node_id_from_token(_jwt({"sub": "u1"})) == ""        # JWT without a node_id claim
    assert serve._node_id_from_token("opaque-not-a-jwt") == ""         # not a JWT at all


def test_serve_register_sends_cached_payload(monkeypatch, tmp_path):
    from remote import relay, serve

    state = _serve_state(monkeypatch, tmp_path,
                         capabilities={"schema_version": 1, "models": {"m": {}}}, max_concurrency=3)
    seen = {}
    monkeypatch.setattr(relay, "register_node", lambda url, tok, node, **kw: seen.update(url=url, tok=tok, node=node, **kw))

    serve.register(state)
    assert (seen["url"], seen["tok"], seen["node"]) == ("https://relay.example", "AT", "node-1")
    assert seen["models"] == ["m"] and seen["max_concurrency"] == 3
    assert seen["capabilities"] == {"schema_version": 1, "models": {"m": {}}}


def test_serve_handle_job_non_stream_forwards_then_submits(monkeypatch, tmp_path):
    from remote import relay, serve

    state = _serve_state(monkeypatch, tmp_path)
    captured = {}
    monkeypatch.setattr(relay, "submit_response",
                        lambda url, tok, txn, *, content, stream: captured.update(content=content, stream=stream, txn=txn))
    monkeypatch.setattr(relay, "submit_error", lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))

    def engine(request):
        assert request.url.path == "/v1/chat/completions"
        assert json.loads(request.content)["model"] == "m"
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    _mock_serve_engine(monkeypatch, engine)
    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "chat/completions", "body": {"model": "m"}, "is_stream": False})
    assert captured["stream"] is False and captured["txn"] == "t1"
    assert b'"hi"' in captured["content"] and "error" not in captured


def test_serve_handle_job_stream_passes_sse_through(monkeypatch, tmp_path):
    from remote import relay, serve

    state = _serve_state(monkeypatch, tmp_path)
    captured = {}

    def cap_submit(url, tok, txn, *, content, stream):
        captured.update(stream=stream, body=b"".join(content))  # materialise the generator while open

    monkeypatch.setattr(relay, "submit_response", cap_submit)
    _mock_serve_engine(monkeypatch, lambda r: httpx.Response(200, content=b"data: a\n\ndata: [DONE]\n\n"))

    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "chat/completions", "body": {}, "is_stream": True})
    assert captured["stream"] is True and captured["body"] == b"data: a\n\ndata: [DONE]\n\n"


def test_serve_handle_job_engine_error_submits_error(monkeypatch, tmp_path):
    from remote import relay, serve

    state = _serve_state(monkeypatch, tmp_path)
    captured = {}
    monkeypatch.setattr(relay, "submit_response", lambda *a, **k: captured.update(submitted=True))
    monkeypatch.setattr(relay, "submit_error", lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))
    _mock_serve_engine(monkeypatch, lambda r: httpx.Response(500, text="boom"))

    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "chat/completions", "body": {}, "is_stream": False})
    assert "500" in captured["error"] and "submitted" not in captured


def test_serve_handle_job_rejects_media(monkeypatch, tmp_path):
    from remote import relay, serve

    state = _serve_state(monkeypatch, tmp_path)
    captured = {}
    monkeypatch.setattr(relay, "submit_error", lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))

    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "media/image/generate", "body": {}})
    assert "media" in captured["error"].lower()


def test_serve_poll_once_returns_none_when_no_work(monkeypatch, tmp_path):
    from remote import relay, serve

    state = _serve_state(monkeypatch, tmp_path)
    monkeypatch.setattr(relay, "poll", lambda url, tok: None)
    assert serve.poll_once(state) is None


def test_serve_poll_once_refreshes_then_retries_on_401(monkeypatch, tmp_path):
    from remote import control_plane, credentials, relay, serve

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    credentials.save_credentials({"networks": [{"network_id": "n1", "access_token": "AT", "refresh_token": "RT"}]})
    state = _serve_state(monkeypatch, tmp_path, access_token="AT", refresh_token="RT")
    monkeypatch.setattr(control_plane, "refresh_network_token",
                        lambda *, network_id, refresh_token, api_url=None: {"access_token": "AT2", "refresh_token": "RT2"})
    tokens = []

    def fake_poll(url, tok):
        tokens.append(tok)
        if len(tokens) == 1:
            raise relay.RelayUnauthorized()
        return {"transaction_id": "t1", "endpoint_path": "chat/completions", "body": {}}

    monkeypatch.setattr(relay, "poll", fake_poll)
    job = serve.poll_once(state)
    assert job["transaction_id"] == "t1"
    assert tokens == ["AT", "AT2"]  # retried with the refreshed token
    assert state.token() == "AT2"
    assert credentials.load_credentials()["networks"][0]["access_token"] == "AT2"  # persisted


def test_serve_poll_once_raises_when_refresh_unavailable(monkeypatch, tmp_path):
    from remote import relay, serve

    state = _serve_state(monkeypatch, tmp_path, refresh_token=None)  # nothing to refresh with

    def _raise_unauth(url, tok):
        raise relay.RelayUnauthorized()

    monkeypatch.setattr(relay, "poll", _raise_unauth)
    with pytest.raises(relay.RelayUnauthorized):
        serve.poll_once(state)


def test_serve_heartbeat_once_ok_reports_inflight_load(monkeypatch, tmp_path):
    from remote import relay, serve

    state = _serve_state(monkeypatch, tmp_path)
    seen = {}
    monkeypatch.setattr(relay, "heartbeat", lambda url, tok, *, load: seen.update(load=load) or "ok")
    assert serve.heartbeat_once(state) == "ok"
    assert seen["load"] == {"active_tasks": 0}


def test_serve_heartbeat_once_re_registers_when_pruned(monkeypatch, tmp_path):
    from remote import relay, serve

    state = _serve_state(monkeypatch, tmp_path)
    monkeypatch.setattr(relay, "heartbeat", lambda url, tok, *, load: "missing")
    calls = []
    monkeypatch.setattr(serve, "register", lambda s: calls.append(s))
    assert serve.heartbeat_once(state) == "missing"
    assert calls == [state]  # 404 → re-register with the cached payload


def test_serve_state_refresh_adopts_already_rotated_token(monkeypatch, tmp_path):
    from remote import control_plane, credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    credentials.save_credentials({"networks": [{"network_id": "n1", "access_token": "AT2", "refresh_token": "RT2"}]})
    state = _serve_state(monkeypatch, tmp_path, access_token="AT", refresh_token="RT")

    def _boom(**_kw):
        raise AssertionError("must not hit the network when credentials already advanced")

    monkeypatch.setattr(control_plane, "refresh_network_token", _boom)
    assert state.refresh() is True
    assert state.token() == "AT2"


def test_serve_handle_job_rejects_unknown_endpoint(monkeypatch, tmp_path):
    from remote import relay, serve

    state = _serve_state(monkeypatch, tmp_path)
    captured = {}
    monkeypatch.setattr(relay, "submit_error", lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))
    # A path the relay shouldn't be sending (e.g. traversal) is never forwarded to the local engine.
    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "../admin", "body": {}})
    assert "unsupported endpoint" in captured["error"].lower()


def test_serve_handle_job_drops_malformed_job_without_crashing(monkeypatch, tmp_path):
    from remote import relay, serve

    state = _serve_state(monkeypatch, tmp_path)
    called = []
    monkeypatch.setattr(relay, "submit_error", lambda *a, **k: called.append(k))
    serve.handle_job(state, {"endpoint_path": "chat/completions", "body": {}})  # no transaction_id
    assert called == []  # dropped with a log line, nothing to report against, no exception


def test_serve_handle_job_survives_failed_error_report(monkeypatch, tmp_path):
    from remote import relay, serve

    state = _serve_state(monkeypatch, tmp_path)

    def boom(*a, **k):
        raise relay.RelayError("relay down")

    monkeypatch.setattr(relay, "submit_error", boom)
    _mock_serve_engine(monkeypatch, lambda r: httpx.Response(500))  # engine fails → error report attempted
    # A failed error-report must be swallowed, not propagate out of handle_job and kill the loop.
    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "chat/completions", "body": {}, "is_stream": False})


def test_serve_handle_job_routes_to_engine_for_requested_model(monkeypatch, tmp_path):
    from remote import relay, serve

    # Two engines under one identity; the job for m2 must reach the engine that serves m2.
    state = _serve_state(
        monkeypatch, tmp_path, models=["m1", "m2"],
        routes={"m1": "http://e1.local/v1", "m2": "http://e2.local/v1"},
    )
    captured = {}
    monkeypatch.setattr(relay, "submit_response",
                        lambda url, tok, txn, *, content, stream: captured.update(content=content))
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))

    def engine(request):
        assert request.url.host == "e2.local"  # routed by model, not to the first engine
        return httpx.Response(200, json={"ok": True})

    _mock_serve_engine(monkeypatch, engine)
    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "chat/completions",
                             "body": {"model": "m2"}, "is_stream": False})
    assert "error" not in captured and b'"ok"' in captured["content"]


def test_serve_handle_job_unknown_model_submits_error(monkeypatch, tmp_path):
    from remote import relay, serve

    state = _serve_state(
        monkeypatch, tmp_path, models=["m1", "m2"],
        routes={"m1": "http://e1.local/v1", "m2": "http://e2.local/v1"},
    )
    captured = {}
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))

    def engine(request):  # an unknown model must never be forwarded to any engine
        raise AssertionError(f"must not forward: {request.url}")

    _mock_serve_engine(monkeypatch, engine)
    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "chat/completions",
                             "body": {"model": "nope"}, "is_stream": False})
    assert "no engine serves" in captured["error"].lower()


def test_serve_handle_job_single_engine_forwards_unknown_model(monkeypatch, tmp_path):
    from remote import relay, serve

    # One engine serving two models is still ONE engine (one distinct URL): an unknown model still
    # forwards to it, preserving the pre-multi-engine "forward the body unchanged" contract.
    state = _serve_state(
        monkeypatch, tmp_path, models=["a", "b"],
        routes={"a": "http://sole.local/v1", "b": "http://sole.local/v1"},
    )
    captured = {}
    monkeypatch.setattr(relay, "submit_response",
                        lambda url, tok, txn, *, content, stream: captured.update(content=content))
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))
    _mock_serve_engine(monkeypatch, lambda r: httpx.Response(200, json={"ok": True}))

    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "chat/completions",
                             "body": {"model": "unknown"}, "is_stream": False})
    assert "error" not in captured  # forwarded to the sole engine, not rejected


def test_serve_heartbeat_loop_stops_when_auth_exhausted(monkeypatch, tmp_path):
    from remote import relay, serve

    state = _serve_state(monkeypatch, tmp_path, refresh_token=None)  # nothing to refresh with

    def _raise_unauth(url, tok, *, load):
        raise relay.RelayUnauthorized()

    monkeypatch.setattr(relay, "heartbeat", _raise_unauth)
    serve._heartbeat_loop(state)  # must return, not spin: it sets stop on auth exhaustion
    assert state.stop.is_set()


# ---------------------------------------------------------------------------
# Multi-engine routing under one remote identity (remote/serve.py:_build_routing, DECISIONS D9)
# ---------------------------------------------------------------------------

def test_build_routing_single_engine_maps_each_model(monkeypatch, tmp_path):
    from remote import serve

    routes, union, caps, warns = serve._build_routing([
        ("http://127.0.0.1:8081/v1", ["m1", "m2"], {"schema_version": 1, "models": {"m1": {"x": 1}}}),
    ])
    assert routes == {"m1": "http://127.0.0.1:8081/v1", "m2": "http://127.0.0.1:8081/v1"}
    assert union == ["m1", "m2"]  # both advertised; only the probed first model carries caps
    assert caps == {"schema_version": 1, "models": {"m1": {"x": 1}}}
    assert warns == []


def test_build_routing_disjoint_engines_union_and_merge(monkeypatch, tmp_path):
    from remote import serve

    routes, union, caps, warns = serve._build_routing([
        ("http://127.0.0.1:8081/v1", ["a"], {"schema_version": 1, "models": {"a": {"f": "A"}}}),
        ("http://127.0.0.1:8000/v1", ["b"], {"schema_version": 1, "models": {"b": {"f": "B"}}}),
    ])
    assert routes == {"a": "http://127.0.0.1:8081/v1", "b": "http://127.0.0.1:8000/v1"}
    assert union == ["a", "b"]
    assert caps == {"schema_version": 1, "models": {"a": {"f": "A"}, "b": {"f": "B"}}}
    assert warns == []


def test_build_routing_duplicate_model_first_wins_with_warning(monkeypatch, tmp_path):
    from remote import serve

    routes, union, caps, warns = serve._build_routing([
        ("http://127.0.0.1:8081/v1", ["dup"], {"schema_version": 1, "models": {"dup": {"f": "first"}}}),
        ("http://127.0.0.1:8000/v1", ["dup"], {"schema_version": 1, "models": {"dup": {"f": "second"}}}),
    ])
    assert routes == {"dup": "http://127.0.0.1:8081/v1"}  # first detected wins
    assert union == ["dup"]  # advertised once
    assert caps == {"schema_version": 1, "models": {"dup": {"f": "first"}}}  # caps follow the winner
    assert len(warns) == 1 and "dup" in warns[0]


def test_build_routing_tolerates_failed_probe_empty_caps(monkeypatch, tmp_path):
    from remote import serve

    # A failed probe degrades to {} upstream — the merge must still route, not KeyError.
    routes, union, caps, warns = serve._build_routing([
        ("http://127.0.0.1:8081/v1", ["m"], {}),
    ])
    assert routes == {"m": "http://127.0.0.1:8081/v1"} and union == ["m"]
    assert caps == {}  # no capabilities → registers text-only
    assert warns == []


def test_bring_up_engines_external_multi_probes_each(monkeypatch, tmp_path):
    from remote import probe, serve

    record = {
        "engines": [
            {"endpoint_url": "http://h:11434/v1", "models": ["llama3"], "engine_label": "ollama"},
            {"endpoint_url": "http://h:8000/v1", "models": ["mistral"], "engine_label": "vllm"},
        ],
        "advertise_as": [],
    }
    seen = []
    monkeypatch.setattr(probe, "capabilities", lambda url, model: seen.append((url, model))
                        or {"schema_version": 1, "models": {model: {"f": model}}})

    results, launched, launcher = serve._bring_up_engines(record)
    assert launched == [] and launcher is None  # external engines: nothing launched/owned
    assert results == [
        ("http://h:11434/v1", ["llama3"], {"schema_version": 1, "models": {"llama3": {"f": "llama3"}}}),
        ("http://h:8000/v1", ["mistral"], {"schema_version": 1, "models": {"mistral": {"f": "mistral"}}}),
    ]
    assert seen == [("http://h:11434/v1", "llama3"), ("http://h:8000/v1", "mistral")]  # each probed once


def test_bring_up_engines_falls_back_to_flat_record(monkeypatch, tmp_path):
    from remote import probe, serve

    # A record written before multi-engine has no `engines` list — synthesise one spec from flat fields.
    record = {"endpoint_url": "http://h:11434/v1", "models": ["llama3"], "advertise_as": []}
    monkeypatch.setattr(probe, "capabilities", lambda url, model: {"schema_version": 1, "models": {model: {}}})

    results, launched, _ = serve._bring_up_engines(record)
    assert launched == []  # external engine: nothing launched
    assert results[0][0] == "http://h:11434/v1"
    assert results[0][1] == ["llama3"]


def test_bring_up_engines_rejects_multi_without_endpoints(monkeypatch, tmp_path):
    from remote import serve

    record = {"engines": [
        {"endpoint_url": "http://h:11434/v1", "models": ["a"], "engine_label": "ollama"},
        {"endpoint_url": None, "models": ["b"], "engine_label": None},  # would need a built-in launch
    ], "advertise_as": []}
    with pytest.raises(SystemExit) as exc:
        serve._bring_up_engines(record)
    assert "external endpoints" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# grid login / grid logout (cli/auth.py + dispatch gate)
# ---------------------------------------------------------------------------

def _device_flow(monkeypatch, *, poll_statuses, networks, started=None):
    """Wire control_plane + webbrowser + sleep for a cmd_login run; return a calls record."""
    from cli import auth
    from remote import control_plane

    calls = {"browser": 0, "fetch_device_id": None, "fetch_session": None,
             "browser_url": None, "sleeps": []}
    started = started or {"device_code": "dc", "user_code": "UC", "interval": 0, "expires_in": 600}
    monkeypatch.setattr(control_plane, "start_device_login", lambda api_url=None: started)

    seq = iter(poll_statuses)
    monkeypatch.setattr(control_plane, "poll_device_login", lambda dc, api_url=None: next(seq))

    def fetch(session_token, device_id, api_url=None):
        calls["fetch_device_id"], calls["fetch_session"] = device_id, session_token
        return networks

    monkeypatch.setattr(control_plane, "fetch_tokens", fetch)

    def open_browser(url):
        calls["browser"] += 1
        calls["browser_url"] = url
        return True

    monkeypatch.setattr(auth.webbrowser, "open", open_browser)
    monkeypatch.setattr(auth.time, "sleep", lambda s: calls["sleeps"].append(s))
    return calls


_APPROVED = {"status": "approved", "session_token": "SESS-secret", "user": {"email": "a@b.com"}}


def test_parser_accepts_login_and_logout():
    parser = cli.build_parser()

    login = parser.parse_args(["login"])
    assert login.handler is cli.cmd_login
    assert login.no_browser is False
    assert parser.parse_args(["login", "--no-browser"]).no_browser is True
    assert parser.parse_args(["logout"]).handler is cli.cmd_logout


def test_login_logout_classified_remote_only():
    assert {"login", "logout"} <= set(dispatch.REMOTE_ONLY)
    assert not (set(dispatch.AGNOSTIC) & set(dispatch.REMOTE_ONLY))
    assert not (set(dispatch.REMOTE_HANDLERS) & set(dispatch.REMOTE_ONLY))


def test_login_logout_gated_in_local_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))  # default mode is local
    for command in ("login", "logout"):
        with pytest.raises(SystemExit) as exc:
            cli.main([command])
        assert "remote" in str(exc.value).lower()
    assert not paths.credentials_file().exists()  # gated before any work happens


def test_login_happy_path_persists_tokens_and_sets_no_active(monkeypatch, tmp_path, capsys):
    from remote import credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    state.set_mode("remote")
    calls = _device_flow(
        monkeypatch,
        poll_statuses=[{"status": "pending"}, _APPROVED],
        networks=[{"network_id": "n1", "name": "team", "network_type": "permissioned-public",
                   "access_token": "AT-secret", "refresh_token": "RT-secret"}],
    )

    assert cli.main(["login"]) == 0  # routes through dispatch in remote mode
    out = capsys.readouterr().out

    assert calls["browser"] == 1  # browser opened by default
    saved = credentials.load_credentials()
    assert saved["session_token"] == "SESS-secret"
    assert saved["user"]["email"] == "a@b.com"
    assert [n["name"] for n in saved["networks"]] == ["team"]
    assert stat.S_IMODE(paths.credentials_file().stat().st_mode) == 0o600
    assert state.get_active("remote") is None  # Q1: login never auto-selects
    assert "Signed in as a@b.com" in out and "team" in out and "grid use" in out
    for secret in ("SESS-secret", "AT-secret", "RT-secret"):
        assert secret not in out


def test_login_no_browser_prints_url_and_code(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    calls = _device_flow(monkeypatch, poll_statuses=[_APPROVED], networks=[])

    assert cli.cmd_login(cli.build_parser().parse_args(["login", "--no-browser"])) == 0
    out = capsys.readouterr().out
    assert calls["browser"] == 0  # browser NOT opened
    assert "UC" in out and "device-login" in out  # user code + constructed sign-in URL
    assert "don't belong to any grids yet" in out  # zero-grid guidance


@pytest.mark.parametrize("status", ["denied", "expired", "consumed"])
def test_login_aborts_on_terminal_poll_status(monkeypatch, tmp_path, status):
    from remote import credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _device_flow(monkeypatch, poll_statuses=[{"status": status}], networks=[])
    with pytest.raises(SystemExit) as exc:
        cli.cmd_login(cli.build_parser().parse_args(["login", "--no-browser"]))
    assert status in str(exc.value)
    assert credentials.load_credentials() == {}  # nothing persisted


def test_login_times_out_when_never_approved(monkeypatch, tmp_path):
    from cli import auth

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _device_flow(monkeypatch, poll_statuses=[{"status": "pending"}] * 5, networks=[])
    ticks = iter([0.0, 100.0, 700.0])  # deadline = 0 + 600; third read passes it
    monkeypatch.setattr(auth.time, "monotonic", lambda: next(ticks))
    with pytest.raises(SystemExit) as exc:
        cli.cmd_login(cli.build_parser().parse_args(["login", "--no-browser"]))
    assert "timed out" in str(exc.value).lower()


def test_login_rejects_malformed_bundle(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _device_flow(
        monkeypatch,
        poll_statuses=[{"status": "approved", "session_token": "S", "user": {}}],
        networks=[{"network_id": "n1"}],  # missing name
    )
    with pytest.raises(SystemExit) as exc:
        cli.cmd_login(cli.build_parser().parse_args(["login", "--no-browser"]))
    assert "malformed" in str(exc.value)
    assert not paths.credentials_file().exists()  # no corrupt store written


def test_login_multi_grid_lists_all_and_sets_no_active(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _device_flow(
        monkeypatch,
        poll_statuses=[_APPROVED],
        networks=[{"network_id": "n1", "name": "alpha"}, {"network_id": "n2", "name": "beta"}],
    )
    assert cli.cmd_login(cli.build_parser().parse_args(["login", "--no-browser"])) == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "beta" in out and "grid use" in out
    assert state.get_active("remote") is None


def test_relogin_reuses_device_id_and_overwrites_tokens(monkeypatch, tmp_path):
    from remote import credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    credentials.save_credentials({"session_token": "OLD", "networks": []})
    did = credentials.device_id()  # establish a stable device.toml

    calls = _device_flow(
        monkeypatch,
        poll_statuses=[{"status": "approved", "session_token": "NEW", "user": {"email": "a@b.com"}}],
        networks=[{"network_id": "n1", "name": "team"}],
    )
    assert cli.cmd_login(cli.build_parser().parse_args(["login", "--no-browser"])) == 0
    assert credentials.load_credentials()["session_token"] == "NEW"  # refreshed
    assert calls["fetch_device_id"] == did  # same machine id across logins


def test_login_json_emits_names_only_and_no_tokens(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _device_flow(
        monkeypatch,
        poll_statuses=[_APPROVED],
        networks=[{"network_id": "n1", "name": "team", "network_type": "permissioned-public",
                   "access_token": "AT-secret", "refresh_token": "RT-secret"}],
    )
    assert cli.cmd_login(cli.build_parser().parse_args(["login", "--no-browser", "--json"])) == 0
    captured = capsys.readouterr()

    assert json.loads(captured.out) == {
        "signed_in": True, "email": "a@b.com",
        "grids": [{"name": "team", "type": "permissioned-public"}], "active": None,
    }
    for secret in ("SESS-secret", "AT-secret", "RT-secret"):
        assert secret not in captured.out
        assert secret not in captured.err  # not leaked via the stderr prompt either
    assert "UC" in captured.err  # the prompt goes to stderr so stdout stays clean JSON


def test_logout_clears_credentials_and_active(monkeypatch, tmp_path, capsys):
    from remote import credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    credentials.save_credentials({"session_token": "S"})
    state.set_active("remote", "team")

    assert cli.cmd_logout(cli.build_parser().parse_args(["logout"])) == 0
    assert "Signed out." in capsys.readouterr().out
    assert not paths.credentials_file().exists()
    assert state.get_active("remote") is None

    assert cli.cmd_logout(cli.build_parser().parse_args(["logout"])) == 0  # idempotent
    assert "not signed in" in capsys.readouterr().out.lower()


def test_logout_json(monkeypatch, tmp_path, capsys):
    from remote import credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    credentials.save_credentials({"session_token": "S"})
    assert cli.cmd_logout(cli.build_parser().parse_args(["logout", "--json"])) == 0
    assert json.loads(capsys.readouterr().out) == {"signed_out": True}


# ---------------------------------------------------------------------------
# sign-in robustness: untrusted-response guards, perms, transport (review fixes)
# ---------------------------------------------------------------------------

def test_save_credentials_locks_home_dir_to_0700(monkeypatch, tmp_path):
    from remote import credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    credentials.save_credentials({"session_token": "S"})
    assert stat.S_IMODE(paths.grid_home().stat().st_mode) == 0o700


def test_load_credentials_reports_corrupt_file(monkeypatch, tmp_path):
    from remote import credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    paths.credentials_file().parent.mkdir(parents=True, exist_ok=True)
    paths.credentials_file().write_text("this is = not = valid = toml ==")
    with pytest.raises(SystemExit) as exc:
        credentials.load_credentials()
    assert "Cannot read" in str(exc.value)


def test_control_plane_wraps_transport_error_as_systemexit(monkeypatch, tmp_path):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))

    def boom(request):
        raise httpx.ConnectError("connection refused")

    _mock_control_plane(monkeypatch, boom)
    with pytest.raises(SystemExit) as exc:
        control_plane.start_device_login()
    assert "control plane" in str(exc.value).lower()  # clean message, not a raw traceback


def test_login_aborts_when_approved_without_session_token(monkeypatch, tmp_path):
    from remote import credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _device_flow(monkeypatch, poll_statuses=[{"status": "approved", "user": {"email": "a@b.com"}}],
                 networks=[])
    with pytest.raises(SystemExit) as exc:
        cli.cmd_login(cli.build_parser().parse_args(["login", "--no-browser"]))
    assert "session token" in str(exc.value).lower()
    assert credentials.load_credentials() == {}  # nothing persisted


def test_login_aborts_when_start_omits_device_code(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _device_flow(monkeypatch, poll_statuses=[_APPROVED], networks=[], started={"user_code": "UC"})
    with pytest.raises(SystemExit) as exc:
        cli.cmd_login(cli.build_parser().parse_args(["login", "--no-browser"]))
    assert "device code" in str(exc.value).lower()


def test_login_caps_server_supplied_poll_interval(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    calls = _device_flow(
        monkeypatch,
        poll_statuses=[{"status": "pending"}, _APPROVED],
        networks=[],
        started={"device_code": "dc", "user_code": "UC", "interval": 3600, "expires_in": 600},
    )
    assert cli.cmd_login(cli.build_parser().parse_args(["login", "--no-browser"])) == 0
    assert calls["sleeps"] and max(calls["sleeps"]) <= 30  # a huge interval can't freeze the CLI


def test_device_login_url_falls_back_to_server_uri_when_website_empty(monkeypatch):
    from cli import auth

    monkeypatch.setenv("GRID_WEBSITE_URL", "")
    url = auth._device_login_url({"user_code": "UC", "verification_uri_complete": "https://srv/login?x=1"})
    assert url == "https://srv/login?x=1"


@pytest.mark.parametrize("started", [
    {"user_code": "UC", "verification_uri_complete": "javascript:alert(1)"},  # non-https
    {"user_code": "UC"},  # key absent
])
def test_device_login_url_rejects_unsafe_server_uri(monkeypatch, started):
    from cli import auth

    monkeypatch.setenv("GRID_WEBSITE_URL", "")
    with pytest.raises(SystemExit):
        auth._device_login_url(started)


# ---------------------------------------------------------------------------
# grid sync — refresh the grid list without re-login (cli/auth.py:cmd_sync)
# ---------------------------------------------------------------------------

def _sync_bundle(network_id: str, *, name: str | None = None,
                 network_type: str = "permissioned-public", access_token: str = "AT",
                 **extra: Any) -> dict[str, Any]:
    """A minimal TOML-serialisable grid bundle (no None values)."""
    bundle = {"network_id": network_id, "name": name or network_id,
              "network_type": network_type, "access_token": access_token}
    bundle.update(extra)
    return bundle


def _sync_seed(networks: list[dict[str, Any]] | None = None, *, session_token: str = "sess-1",
               api_url: str = "https://api.example.com", user: dict[str, Any] | None = None) -> None:
    """Seed credentials.toml as a prior `grid login` would (the active grid lives in state.json)."""
    from remote import credentials

    data = {"session_token": session_token, "api_url": api_url}
    if user is not None:
        data["user"] = user
    if networks is not None:
        data["networks"] = networks
    credentials.save_credentials(data)


def _sync_patch_fetch(monkeypatch: pytest.MonkeyPatch, networks: list[dict[str, Any]],
                      calls: list[dict[str, Any]] | None = None) -> None:
    """Patch control_plane.fetch_tokens to return `networks` (no live control plane)."""
    from remote import control_plane

    def fake_fetch_tokens(session_token, device_id, api_url=None):
        if calls is not None:
            calls.append({"session_token": session_token, "device_id": device_id, "api_url": api_url})
        return [dict(n) for n in networks]

    monkeypatch.setattr(control_plane, "fetch_tokens", fake_fetch_tokens)


def _run_sync(args_list: tuple[str, ...] = ("sync",)) -> int:
    return cli.cmd_sync(cli.build_parser().parse_args(list(args_list)))


def test_sync_parser_wires_handler_and_json_flag():
    parser = cli.build_parser()
    args = parser.parse_args(["sync"])
    assert args.handler is cli.cmd_sync
    assert args.json is False
    assert parser.parse_args(["sync", "--json"]).json is True


def test_sync_classified_remote_only():
    assert "sync" in dispatch.REMOTE_ONLY
    assert not (set(dispatch.AGNOSTIC) & {"sync"})
    assert not (set(dispatch.REMOTE_HANDLERS) & {"sync"})


def test_sync_gated_in_local_mode(monkeypatch, tmp_path):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))  # default mode is local
    called = []
    monkeypatch.setattr(control_plane, "fetch_tokens", lambda *a, **k: called.append(1) or [])
    with pytest.raises(SystemExit) as exc:
        cli.main(["sync"])
    assert "remote" in str(exc.value).lower()
    assert not called  # gated before any control-plane call
    assert not paths.credentials_file().exists()


def test_sync_requires_login(monkeypatch, tmp_path):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))  # not signed in
    called = []
    monkeypatch.setattr(control_plane, "fetch_tokens", lambda *a, **k: called.append(1) or [])
    with pytest.raises(SystemExit) as exc:
        _run_sync()
    assert "signed in" in str(exc.value).lower()  # require_session message
    assert not called  # never reached the control plane


def test_sync_adds_new_grid_and_preserves_session_fields(monkeypatch, tmp_path):
    from remote import credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _sync_seed([_sync_bundle("net-a")], session_token="sess-1",
               api_url="https://api.example.com", user={"email": "u@example.com"})
    device = credentials.device_id()
    calls = []
    _sync_patch_fetch(monkeypatch, [_sync_bundle("net-a"), _sync_bundle("net-b")], calls)

    assert _run_sync() == 0
    data = credentials.load_credentials()
    assert {n["network_id"] for n in data["networks"]} == {"net-a", "net-b"}
    # session_token / api_url / user all survive the {**data, "networks": …} merge
    assert data["session_token"] == "sess-1"
    assert data["api_url"] == "https://api.example.com"
    assert data["user"] == {"email": "u@example.com"}
    # reused the stored session + device id — no re-login
    assert calls and calls[0]["session_token"] == "sess-1"
    assert calls[0]["device_id"] == device


def test_sync_removes_stale_grid(monkeypatch, tmp_path):
    from remote import credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _sync_seed([_sync_bundle("net-a"), _sync_bundle("net-b")])
    _sync_patch_fetch(monkeypatch, [_sync_bundle("net-b")])

    assert _run_sync() == 0
    data = credentials.load_credentials()
    assert [n["network_id"] for n in data["networks"]] == ["net-b"]


def test_sync_refreshes_existing_token(monkeypatch, tmp_path):
    from remote import credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _sync_seed([_sync_bundle("net-a", access_token="old")])
    _sync_patch_fetch(monkeypatch, [_sync_bundle("net-a", access_token="new")])

    assert _run_sync() == 0
    data = credentials.load_credentials()
    net = next(n for n in data["networks"] if n["network_id"] == "net-a")
    assert net["access_token"] == "new"


def test_sync_never_touches_active_when_grid_vanishes(monkeypatch, tmp_path):
    from remote import credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _sync_seed([_sync_bundle("net-a")])
    state.set_active("remote", "net-a")
    _sync_patch_fetch(monkeypatch, [_sync_bundle("net-b")])  # net-a is gone

    assert _run_sync() == 0
    data = credentials.load_credentials()
    assert [n["network_id"] for n in data["networks"]] == ["net-b"]
    # Q1: the active pointer is left as a tolerated stale value — sync never writes state.json
    assert state.get_active("remote") == "net-a"


def test_sync_never_auto_selects_active(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _sync_seed([])  # signed in, no grids, no active selection
    _sync_patch_fetch(monkeypatch, [_sync_bundle("net-a"), _sync_bundle("net-b")])

    assert _run_sync() == 0
    assert state.get_active("remote") is None  # mirrors login: never auto-selects


def test_sync_empty_list_clears_warns_and_keeps_active(monkeypatch, tmp_path, capsys):
    from remote import credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _sync_seed([_sync_bundle("net-a")])
    state.set_active("remote", "net-a")
    _sync_patch_fetch(monkeypatch, [])

    assert _run_sync() == 0
    data = credentials.load_credentials()
    assert data["networks"] == []
    err = capsys.readouterr().err
    assert "cleared locally" in err  # stderr-unique token (stdout also says "0 grids")
    # Q1: active is never written, even when the list is wiped
    assert state.get_active("remote") == "net-a"


def test_sync_concurrent_logout_does_not_strand_partial_file(monkeypatch, tmp_path):
    """A `grid logout` racing mid-sync must not yield a partial file missing the session: the merge
    writes the single snapshot it gated on, so session/user survive (mirrors the concurrent-logout
    guard in credentials.update_network_tokens)."""
    from remote import control_plane, credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _sync_seed([_sync_bundle("net-a")], session_token="sess-1", user={"email": "u@x"})

    def logout_then_return(session_token, device_id, api_url=None):
        credentials.clear_credentials()  # concurrent `grid logout` deletes the file mid-call
        return [dict(_sync_bundle("net-a"))]

    monkeypatch.setattr(control_plane, "fetch_tokens", logout_then_return)
    assert _run_sync() == 0
    data = credentials.load_credentials()
    assert data.get("session_token") == "sess-1"  # not silently dropped
    assert data["user"] == {"email": "u@x"}
    assert [n["network_id"] for n in data["networks"]] == ["net-a"]


def test_sync_rejects_malformed_bundle_and_keeps_store(monkeypatch, tmp_path):
    from remote import credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _sync_seed([_sync_bundle("net-a")])
    _sync_patch_fetch(monkeypatch, [{"network_id": "net-b"}])  # missing name

    with pytest.raises(SystemExit) as exc:
        _run_sync()
    assert "malformed" in str(exc.value)
    # the existing store is left untouched when the response is rejected
    data = credentials.load_credentials()
    assert [n["network_id"] for n in data["networks"]] == ["net-a"]


@pytest.mark.parametrize("status", [401, 403])
def test_sync_expired_session_is_actionable(monkeypatch, tmp_path, status):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _sync_seed([_sync_bundle("net-a")])

    def fake_auth_fail(session_token, device_id, api_url=None):
        raise SystemExit(
            f"GET https://api-grid.autonomous.ai/v1/grid/tokens failed ({status}): denied"
        )

    monkeypatch.setattr(control_plane, "fetch_tokens", fake_auth_fail)
    with pytest.raises(SystemExit) as exc:
        _run_sync()
    assert "grid login" in str(exc.value).lower()
    assert "expired" in str(exc.value).lower()


def test_sync_other_error_propagates_unchanged(monkeypatch, tmp_path):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _sync_seed([_sync_bundle("net-a")])

    def fake_500(session_token, device_id, api_url=None):
        # The 5xx body itself contains "failed (401):" — the method-anchored regex must NOT rewrite it.
        raise SystemExit(
            "GET https://api-grid.autonomous.ai/v1/grid/tokens failed (500): upstream failed (401): no"
        )

    monkeypatch.setattr(control_plane, "fetch_tokens", fake_500)
    with pytest.raises(SystemExit) as exc:
        _run_sync()
    assert "500" in str(exc.value)  # raw error propagates...
    assert "expired" not in str(exc.value).lower()  # ...never rewritten into the re-login message


def test_sync_json_emits_names_only_and_no_tokens(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _sync_seed([_sync_bundle("net-a", name="team", access_token="AT-secret",
                             refresh_token="RT-secret")], session_token="SESS-secret")
    _sync_patch_fetch(monkeypatch, [_sync_bundle("net-a", name="team", access_token="AT-secret",
                                                 refresh_token="RT-secret")])

    assert _run_sync(["sync", "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "synced": True, "grids": [{"name": "team", "type": "permissioned-public"}],
    }
    for secret in ("SESS-secret", "AT-secret", "RT-secret"):
        assert secret not in captured.out


def test_sync_json_survives_empty_list_warning(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _sync_seed([_sync_bundle("net-a")])
    _sync_patch_fetch(monkeypatch, [])

    assert _run_sync(["sync", "--json"]) == 0
    captured = capsys.readouterr()
    # stdout stays clean, parseable JSON; the human warning is confined to stderr
    assert json.loads(captured.out) == {"synced": True, "grids": []}
    assert "Warning" not in captured.out
    assert "cleared locally" in captured.err


# ---------------------------------------------------------------------------
# Remote grid lifecycle — control plane (remote/control_plane.py managed-networks)
# ---------------------------------------------------------------------------

def test_create_managed_network_posts_name_and_type_with_bearer(monkeypatch, tmp_path):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def handler(request):
        seen["method"], seen["path"] = request.method, request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "network_id": "n1", "name": "team", "network_type": "permissioned-public",
            "signaling_url": "https://relay.example", "status": "running",
        })

    _mock_control_plane(monkeypatch, handler)
    out = control_plane.create_managed_network("sess-tok", "team", "permissioned-public")
    assert out["network_id"] == "n1"
    assert (seen["method"], seen["path"]) == ("POST", "/v1/grid/managed-networks")
    assert seen["auth"] == "Bearer sess-tok"
    assert seen["body"] == {"name": "team", "network_type": "permissioned-public"}


def test_start_managed_network_posts_to_start_endpoint(monkeypatch, tmp_path):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def handler(request):
        seen["method"], seen["path"] = request.method, request.url.path
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"status": "running", "signaling_url": "https://relay.example"})

    _mock_control_plane(monkeypatch, handler)
    out = control_plane.start_managed_network("sess-tok", "n1")
    assert out["status"] == "running"
    assert (seen["method"], seen["path"]) == ("POST", "/v1/grid/managed-networks/n1/start")
    assert seen["auth"] == "Bearer sess-tok"


def test_stop_managed_network_posts_to_stop_endpoint(monkeypatch, tmp_path):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def handler(request):
        seen["method"], seen["path"] = request.method, request.url.path
        return httpx.Response(200, json={"status": "stopped"})

    _mock_control_plane(monkeypatch, handler)
    out = control_plane.stop_managed_network("sess-tok", "n1")
    assert out["status"] == "stopped"
    assert (seen["method"], seen["path"]) == ("POST", "/v1/grid/managed-networks/n1/stop")


def test_get_managed_network_status_gets_status_endpoint(monkeypatch, tmp_path):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def handler(request):
        seen["method"], seen["path"] = request.method, request.url.path
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"status": "running", "signaling_url": "https://relay.example"})

    _mock_control_plane(monkeypatch, handler)
    out = control_plane.get_managed_network_status("sess-tok", "n1")
    assert out["status"] == "running"
    assert (seen["method"], seen["path"]) == ("GET", "/v1/grid/managed-networks/n1/status")
    assert seen["auth"] == "Bearer sess-tok"


def test_add_network_appends_and_is_idempotent_by_id(monkeypatch, tmp_path):
    from remote import credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    credentials.save_credentials({"session_token": "tok", "api_url": "https://api", "networks": []})

    credentials.add_network({"network_id": "n1", "name": "team", "network_type": "permissioned-public"})
    credentials.add_network({"network_id": "n2", "name": "lab", "network_type": "permissioned-providers"})
    assert [n["name"] for n in credentials.load_credentials()["networks"]] == ["team", "lab"]

    # Re-adding the same id replaces in place of a duplicate (idempotent) and preserves other keys.
    credentials.add_network({"network_id": "n1", "name": "team", "network_type": "permissioned-providers"})
    saved = credentials.load_credentials()
    nets = saved["networks"]
    assert [n["network_id"] for n in nets] == ["n2", "n1"]  # n1 de-duped, re-appended at the tail
    assert next(n for n in nets if n["network_id"] == "n1")["network_type"] == "permissioned-providers"
    assert saved["session_token"] == "tok" and saved["api_url"] == "https://api"


# ---------------------------------------------------------------------------
# Remote grid lifecycle — commands (cli/remote_grid.py via dispatch)
# ---------------------------------------------------------------------------

def _seed_remote(monkeypatch, tmp_path, networks=None, session="sess-tok", active=None):
    """Sign in + switch to remote mode for the lifecycle command tests."""
    from remote import credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    state.set_mode("remote")
    credentials.save_credentials({
        "session_token": session, "api_url": "https://api.example",
        "user": {"email": "a@b.com"}, "networks": networks or [],
    })
    if active:
        state.set_active("remote", active)


def _mock_lifecycle(monkeypatch, *, create=None, start=None, stop=None, status=None):
    """Stub the four control-plane lifecycle calls; record what each was invoked with."""
    from remote import control_plane

    calls = {}

    def _create(session_token, name, network_type, api_url=None):
        calls["create"] = {"session": session_token, "name": name, "network_type": network_type}
        return create or {}

    def _start(session_token, network_id, api_url=None):
        calls["start"] = {"session": session_token, "network_id": network_id}
        return start or {}

    def _stop(session_token, network_id, api_url=None):
        calls["stop"] = {"session": session_token, "network_id": network_id}
        return stop or {}

    def _status(session_token, network_id, api_url=None):
        calls["status"] = {"session": session_token, "network_id": network_id}
        return status or {}

    monkeypatch.setattr(control_plane, "create_managed_network", _create)
    monkeypatch.setattr(control_plane, "start_managed_network", _start)
    monkeypatch.setattr(control_plane, "stop_managed_network", _stop)
    monkeypatch.setattr(control_plane, "get_managed_network_status", _status)
    return calls


def test_remote_up_creates_when_name_unknown(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path)
    calls = _mock_lifecycle(monkeypatch, create={
        "network_id": "n-new", "name": "team", "network_type": "permissioned-public",
        "signaling_url": "https://relay.example", "status": "running",
    })

    assert cli.main(["up", "team"]) == 0
    out = capsys.readouterr().out
    assert calls["create"] == {"session": "sess-tok", "name": "team", "network_type": "permissioned-public"}
    assert "create" in calls and "start" not in calls
    assert "grid=team" in out and "grid_url=https://relay.example" in out

    from remote import credentials
    nets = credentials.load_credentials()["networks"]  # persisted so ls/use/info see it
    assert [n["network_id"] for n in nets] == ["n-new"]
    assert nets[0]["signaling_url"] == "https://relay.example"


def test_remote_up_starts_when_name_known(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path, networks=[
        {"network_id": "n1", "name": "team", "network_type": "permissioned-public",
         "signaling_url": "https://relay.example", "status": "stopped"}])
    calls = _mock_lifecycle(monkeypatch, start={"status": "running", "signaling_url": "https://relay.example"})

    assert cli.main(["up", "team"]) == 0
    out = capsys.readouterr().out
    assert calls.get("start") == {"session": "sess-tok", "network_id": "n1"}
    assert "create" not in calls  # known grid → start, not create
    assert "grid=team" in out and "grid_url=https://relay.example" in out


def test_remote_up_bare_starts_active_grid(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path, networks=[
        {"network_id": "n1", "name": "team", "signaling_url": "https://r1"},
        {"network_id": "n2", "name": "lab", "signaling_url": "https://r2"}],
        active="lab")
    calls = _mock_lifecycle(monkeypatch, start={"status": "running"})

    assert cli.main(["up"]) == 0  # no name → the active grid
    out = capsys.readouterr().out
    assert calls.get("start") == {"session": "sess-tok", "network_id": "n2"}
    assert "create" not in calls
    assert "grid=lab" in out


def test_remote_up_bare_errors_when_unresolvable(monkeypatch, tmp_path):
    _seed_remote(monkeypatch, tmp_path, networks=[])  # signed in, but no grids and no active
    _mock_lifecycle(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        cli.main(["up"])
    assert "name" in str(exc.value).lower()  # guidance to name a grid to create


def test_remote_up_type_on_create_sets_network_type(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path)
    calls = _mock_lifecycle(monkeypatch, create={
        "network_id": "n1", "name": "lab", "network_type": "permissioned-providers",
        "signaling_url": "https://r"})

    assert cli.main(["up", "lab", "--type", "permissioned-providers"]) == 0
    capsys.readouterr()
    assert calls["create"]["network_type"] == "permissioned-providers"


def test_remote_up_type_on_start_warns_and_ignores(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path, networks=[
        {"network_id": "n1", "name": "team", "signaling_url": "https://r"}])
    calls = _mock_lifecycle(monkeypatch, start={"status": "running"})

    assert cli.main(["up", "team", "--type", "permissioned-providers"]) == 0
    out = capsys.readouterr().out
    assert "start" in calls and "create" not in calls
    assert "type" in out.lower()  # a note that --type is ignored on an existing grid
    assert calls["start"] == {"session": "sess-tok", "network_id": "n1"}  # start carries no type


def test_remote_up_start_reports_grid_url_from_status(monkeypatch, tmp_path, capsys):
    # Live shape: start → {network_id, status} (no signaling_url); the bundle has none stored either,
    # so `up` reads the address from the status endpoint.
    _seed_remote(monkeypatch, tmp_path, networks=[{"network_id": "n1", "name": "team"}])
    _mock_lifecycle(monkeypatch, start={"network_id": "n1", "status": "running"},
                    status={"state": "running", "signaling_url": "https://live.relay"})

    assert cli.main(["up", "team"]) == 0
    assert "grid_url=https://live.relay" in capsys.readouterr().out


def test_remote_down_stops(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path, networks=[{"network_id": "n1", "name": "team"}])
    calls = _mock_lifecycle(monkeypatch, stop={"status": "stopped"})

    assert cli.main(["down", "team"]) == 0
    out = capsys.readouterr().out
    assert calls.get("stop") == {"session": "sess-tok", "network_id": "n1"}
    assert "team" in out and "down" in out.lower()


def test_remote_down_errors_when_unresolvable(monkeypatch, tmp_path):
    _seed_remote(monkeypatch, tmp_path, networks=[{"network_id": "n1", "name": "team"}])
    _mock_lifecycle(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        cli.main(["down", "ghost"])  # not a known grid
    assert "ghost" in str(exc.value)


def test_remote_ls_lists_local_without_network_call(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path, networks=[
        {"network_id": "n1", "name": "team", "network_type": "permissioned-public"},
        {"network_id": "n2", "name": "lab", "network_type": "permissioned-providers"}])

    def boom(request):  # any control-plane HTTP would prove ls isn't local
        raise AssertionError("ls must not hit the control plane")

    _mock_control_plane(monkeypatch, boom)
    assert cli.main(["ls"]) == 0
    out = capsys.readouterr().out
    assert "team" in out and "permissioned-public" in out
    assert "lab" in out and "permissioned-providers" in out


def test_remote_ls_json_emits_grid_and_type(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path, networks=[
        {"network_id": "n1", "name": "team", "network_type": "permissioned-public"}])
    assert cli.main(["ls", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [{"grid": "team", "type": "permissioned-public"}]


def test_remote_list_alias_lists_like_ls(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path, networks=[
        {"network_id": "n1", "name": "team", "network_type": "permissioned-public"}])
    assert cli.main(["list"]) == 0  # alias of `ls` must work in remote mode, not the stub
    assert "team" in capsys.readouterr().out


def test_remote_ls_requires_session(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    state.set_mode("remote")  # remote mode, but not signed in
    with pytest.raises(SystemExit) as exc:
        cli.main(["ls"])
    assert "login" in str(exc.value).lower()


def test_remote_info_maps_status_and_hides_internals(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path, networks=[
        {"network_id": "n1", "name": "team", "network_type": "permissioned-public"}])
    # Real managed-network status shape (see live probe): run state is `state`, plus server-side
    # internals we must not surface.
    _mock_lifecycle(monkeypatch, status={
        "state": "running", "signaling_url": "https://relay.example",
        "server_pid": 4242, "sync_pid": 99,
        "postgres": {"container": "container-x", "running": True},
        "base_url": "https://relay.example/relay/v1", "plan": "free"})

    assert cli.main(["info", "team"]) == 0
    out = capsys.readouterr().out
    assert "grid=team" in out and "type=permissioned-public" in out
    assert "status=running" in out and "grid_url=https://relay.example" in out
    for internal in ("4242", "server_pid", "sync_pid", "postgres", "container-x", "base_url", "plan"):
        assert internal not in out  # proprietary server internals never reach the surface


def test_remote_info_json_projects_fixed_shape(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path, networks=[
        {"network_id": "n1", "name": "team", "network_type": "permissioned-public"}])
    _mock_lifecycle(monkeypatch, status={
        "state": "running", "signaling_url": "https://relay.example", "server_pid": 4242})

    assert cli.main(["info", "team", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data == {"grid": "team", "type": "permissioned-public", "status": "running",
                    "grid_url": "https://relay.example"}


def test_remote_info_member_falls_back_to_bundle_url(monkeypatch, tmp_path, capsys):
    """`grid info` for a member (can't read creator-only status) shows the bundle's relay URL and a
    blank run-state instead of erroring."""
    from remote import control_plane

    net = {"network_id": "n1", "name": "team", "network_type": "permissioned-public",
           "access_token": "AT", "lan_signaling_url": "https://grid.example/n1"}
    _seed_remote(monkeypatch, tmp_path, networks=[net], active="team")

    def _forbidden(session_token, network_id, api_url=None):
        raise SystemExit("GET .../status failed (403): Only the network creator can manage it")

    monkeypatch.setattr(control_plane, "get_managed_network_status", _forbidden)
    assert cli.main(["info", "team"]) == 0
    out = capsys.readouterr().out
    assert "grid=team" in out and "grid_url=https://grid.example/n1" in out


def test_remote_lifecycle_never_prints_tokens(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path, networks=[
        {"network_id": "n1", "name": "team", "network_type": "permissioned-public",
         "signaling_url": "https://relay.example",
         "access_token": "AT-secret", "refresh_token": "RT-secret"}])
    _mock_lifecycle(monkeypatch, status={"state": "running", "signaling_url": "https://relay.example"})

    assert cli.main(["ls"]) == 0
    assert cli.main(["ls", "--json"]) == 0
    assert cli.main(["info", "team"]) == 0
    assert cli.main(["info", "team", "--json"]) == 0
    out = capsys.readouterr().out
    for secret in ("AT-secret", "RT-secret"):
        assert secret not in out


# ---------------------------------------------------------------------------
# Remote consume path: `grid chat` / `image` / `edit` / `video` + `info --env`
# (cli/remote_request.py + cli/remote_grid.cmd_remote_info --env, via the relay)
# ---------------------------------------------------------------------------

def test_remote_chat_posts_through_relay_with_bearer(monkeypatch, tmp_path, capsys):
    _seed_running_remote_grid(monkeypatch, tmp_path)  # bundle has access_token "AT", no signaling_url
    seen = {}

    def handler(request):
        seen["method"], seen["path"] = request.method, request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["tp"] = request.headers.get("x-target-provider")
        seen["asp"] = request.headers.get("x-allow-self-provider")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi there"}}]})

    _mock_relay(monkeypatch, handler)
    assert cli.main(["chat", "-m", "llama3", "hello"]) == 0

    out = capsys.readouterr().out
    assert (seen["method"], seen["path"]) == ("POST", "/relay/v1/chat/completions")
    assert seen["auth"] == "Bearer AT"
    assert seen["body"] == {"model": "llama3", "messages": [{"role": "user", "content": "hello"}]}
    assert seen["tp"] is None and seen["asp"] is None  # no routing headers unless asked
    assert "hi there" in out


def test_remote_chat_json_prints_raw_response(monkeypatch, tmp_path, capsys):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_relay(monkeypatch, lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]}))
    assert cli.main(["chat", "-m", "m", "hi", "--json"]) == 0
    assert '"choices"' in capsys.readouterr().out  # raw JSON, not the extracted content


def test_remote_chat_member_uses_bundle_url_when_status_forbidden(monkeypatch, tmp_path, capsys):
    """A consumer-member (not the creator) gets 403 from creator-only status; chat must still route
    through the lan_signaling_url the login bundle carries."""
    from remote import control_plane

    net = {"network_id": "n1", "name": "team", "network_type": "permissioned-public",
           "access_token": "AT", "lan_signaling_url": "https://grid.example/n1"}
    _seed_remote(monkeypatch, tmp_path, networks=[net], active="team")

    def _forbidden(session_token, network_id, api_url=None):
        raise SystemExit("GET .../status failed (403): Only the network creator can manage it")

    monkeypatch.setattr(control_plane, "get_managed_network_status", _forbidden)
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"choices": [{"message": {"content": "from member"}}]})

    _mock_relay(monkeypatch, handler)
    assert cli.main(["chat", "-m", "minimax", "hi"]) == 0
    assert seen["url"] == "https://grid.example/n1/relay/v1/chat/completions"  # bundle base, not status
    assert "from member" in capsys.readouterr().out


def test_remote_chat_sends_routing_headers(monkeypatch, tmp_path):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    seen = {}

    def handler(request):
        seen["tp"] = request.headers.get("x-target-provider")
        seen["asp"] = request.headers.get("x-allow-self-provider")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    _mock_relay(monkeypatch, handler)
    assert cli.main(["chat", "-m", "m", "hi", "--target-provider", "engine-7", "--allow-self-provider"]) == 0
    assert seen["tp"] == "engine-7" and seen["asp"] == "true"


def test_remote_chat_401_is_clear_error_without_leaking_token(monkeypatch, tmp_path, capsys):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_relay(monkeypatch, lambda r: httpx.Response(401, json={"detail": "nope"}))
    assert cli.main(["chat", "-m", "m", "hi"]) == 1
    err = capsys.readouterr().err
    assert "expired" in err.lower() and "grid login" in err.lower()
    assert "AT" not in err  # never echo the access token


def test_remote_chat_requires_sign_in(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    state.set_mode("remote")  # remote mode, not signed in
    with pytest.raises(SystemExit) as exc:
        cli.main(["chat", "-m", "m", "hi"])
    assert "signed in" in str(exc.value).lower()


def test_remote_chat_requires_active_grid(monkeypatch, tmp_path):
    _seed_remote(monkeypatch, tmp_path, networks=[
        {"network_id": "n1", "name": "a", "access_token": "AT"},
        {"network_id": "n2", "name": "b", "access_token": "AT"},
    ])  # signed in, two grids, none active
    with pytest.raises(SystemExit) as exc:
        cli.main(["chat", "-m", "m", "hi"])
    assert "name a grid" in str(exc.value).lower()


def test_remote_chat_requires_grid_up(monkeypatch, tmp_path):
    _seed_remote(monkeypatch, tmp_path,
                networks=[{"network_id": "n1", "name": "team", "access_token": "AT"}], active="team")
    _mock_lifecycle(monkeypatch, status={"state": "stopped"})
    with pytest.raises(SystemExit) as exc:
        cli.main(["chat", "-m", "m", "hi"])
    assert "isn't up" in str(exc.value).lower()


def test_remote_chat_requires_access_token(monkeypatch, tmp_path):
    _seed_remote(monkeypatch, tmp_path,
                networks=[{"network_id": "n1", "name": "team"}], active="team")  # no access_token
    with pytest.raises(SystemExit) as exc:
        cli.main(["chat", "-m", "m", "hi"])
    assert "access token" in str(exc.value).lower()


def test_remote_image_streams_and_saves_output(monkeypatch, tmp_path, capsys):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    b64 = base64.b64encode(b"PNGDATA").decode("ascii")
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        result = json.dumps({"type": "result",
                             "output_files": [{"filename": "out.png", "content_base64": b64}]})
        return httpx.Response(200, content=f"data: {result}\n\ndata: [DONE]\n\n".encode())

    _mock_relay(monkeypatch, handler)
    outdir = tmp_path / "out"
    assert cli.main(["image", "a cat", "-o", str(outdir)]) == 0
    assert seen["path"] == "/relay/v1/media/image/generate"
    saved = list(outdir.glob("*.png"))
    assert saved and saved[0].read_bytes() == b"PNGDATA"


def test_remote_edit_posts_to_image_edit_with_routing_headers(monkeypatch, tmp_path):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    img = tmp_path / "a.png"
    img.write_bytes(b"x")
    b64 = base64.b64encode(b"EDITED").decode("ascii")
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["tp"] = request.headers.get("x-target-provider")  # routing headers on the streaming path
        seen["asp"] = request.headers.get("x-allow-self-provider")
        result = json.dumps({"type": "result",
                             "output_files": [{"filename": "out.png", "content_base64": b64}]})
        return httpx.Response(200, content=f"data: {result}\n\ndata: [DONE]\n\n".encode())

    _mock_relay(monkeypatch, handler)
    outdir = tmp_path / "out"
    assert cli.main(["edit", "fix it", "-i", str(img), "-o", str(outdir),
                     "--target-provider", "engine-3", "--allow-self-provider"]) == 0
    assert seen["path"] == "/relay/v1/media/image/edit"
    assert seen["tp"] == "engine-3" and seen["asp"] == "true"
    assert list(outdir.glob("*.png"))[0].read_bytes() == b"EDITED"


def test_remote_image_result_without_files_is_an_error(monkeypatch, tmp_path, capsys):
    _seed_running_remote_grid(monkeypatch, tmp_path)

    def handler(request):  # a result event whose only file lacks content_base64 → nothing written
        result = json.dumps({"type": "result", "output_files": [{"filename": "out.png"}]})
        return httpx.Response(200, content=f"data: {result}\n\ndata: [DONE]\n\n".encode())

    _mock_relay(monkeypatch, handler)
    assert cli.main(["image", "a cat", "-o", str(tmp_path / "out")]) == 1
    assert "no files" in capsys.readouterr().err.lower()


def test_remote_edit_rejects_more_than_three_images(monkeypatch, tmp_path):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    images = []
    for index in range(4):
        path = tmp_path / f"i{index}.png"
        path.write_bytes(b"x")
        images += ["-i", str(path)]
    with pytest.raises(SystemExit) as exc:
        cli.main(["edit", "make it pop", *images])
    assert "three" in str(exc.value).lower()


def test_remote_video_posts_to_i2v(monkeypatch, tmp_path):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    image = tmp_path / "in.png"
    image.write_bytes(b"x")
    b64 = base64.b64encode(b"MP4DATA").decode("ascii")
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        result = json.dumps({"type": "result",
                             "output_files": [{"filename": "out.mp4", "content_base64": b64}]})
        return httpx.Response(200, content=f"data: {result}\n\ndata: [DONE]\n\n".encode())

    _mock_relay(monkeypatch, handler)
    outdir = tmp_path / "out"
    assert cli.main(["video", "make it move", "-i", str(image), "-o", str(outdir)]) == 0
    assert seen["path"] == "/relay/v1/media/video/i2v"
    assert list(outdir.glob("*.mp4"))[0].read_bytes() == b"MP4DATA"


def test_remote_info_env_prints_relay_base_and_token(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                networks=[{"network_id": "n1", "name": "team", "access_token": "AT"}], active="team")
    _mock_lifecycle(monkeypatch, status={"state": "running", "signaling_url": "https://relay.example"})
    assert cli.main(["info", "--env"]) == 0
    out = capsys.readouterr().out
    assert 'export OPENAI_BASE_URL="https://relay.example/relay/v1"' in out
    assert 'export OPENAI_API_KEY="AT"' in out  # the one deliberate token-printing carve-out


def test_remote_info_env_requires_access_token(monkeypatch, tmp_path):
    _seed_remote(monkeypatch, tmp_path,
                networks=[{"network_id": "n1", "name": "team"}], active="team")  # no access_token
    with pytest.raises(SystemExit) as exc:
        cli.main(["info", "--env"])
    assert "access token" in str(exc.value).lower()


def test_local_chat_rejects_remote_only_flags(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))  # default local mode
    with pytest.raises(SystemExit) as exc:
        cli.main(["chat", "-m", "m", "hi", "--target-provider", "e1"])
    assert "remote mode" in str(exc.value).lower()


def test_local_image_rejects_allow_self_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))  # default local mode
    with pytest.raises(SystemExit) as exc:
        cli.main(["image", "a cat", "--allow-self-provider"])
    assert "remote mode" in str(exc.value).lower()


def test_local_edit_and_video_reject_remote_only_flags(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))  # default local mode
    img = tmp_path / "a.png"
    img.write_bytes(b"x")
    with pytest.raises(SystemExit) as exc:  # reject runs before the >3-image check / file reads
        cli.main(["edit", "p", "-i", str(img), "--target-provider", "e1"])
    assert "remote mode" in str(exc.value).lower()
    with pytest.raises(SystemExit) as exc:
        cli.main(["video", "p", "-i", str(img), "--allow-self-provider"])
    assert "remote mode" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Remote membership — `grid members add|remove|list` (cli/remote_grid.py + control_plane)
# ---------------------------------------------------------------------------

def _mock_members(monkeypatch, *, add=None, remove=None, members=None):
    """Stub the three control-plane member calls; record what each was invoked with."""
    from remote import control_plane

    calls = {}

    def _add(session_token, network_id, email, roles, api_url=None):
        calls["add"] = {
            "session": session_token, "network_id": network_id, "email": email, "roles": roles,
        }
        return {} if add is None else add

    def _remove(session_token, network_id, email, api_url=None):
        calls["remove"] = {"session": session_token, "network_id": network_id, "email": email}
        return {} if remove is None else remove

    def _list(session_token, network_id, api_url=None):
        calls["list"] = {"session": session_token, "network_id": network_id}
        return members if members is not None else []

    monkeypatch.setattr(control_plane, "add_member", _add)
    monkeypatch.setattr(control_plane, "remove_member", _remove)
    monkeypatch.setattr(control_plane, "list_members", _list)
    return calls


# -- handler: add ----------------------------------------------------------

def test_remote_members_add_defaults_to_consumer_role(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                networks=[{"network_id": "n1", "name": "team"}], active="team")
    calls = _mock_members(monkeypatch, add={"ok": True})

    assert cli.main(["members", "add", "alice@example.com"]) == 0
    out = capsys.readouterr().out
    assert calls["add"] == {
        "session": "sess-tok", "network_id": "n1", "email": "alice@example.com", "roles": ["consumer"],
    }
    assert "alice@example.com" in out and "consumer" in out


def test_remote_members_add_role_both_is_sent_without_expansion(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path, networks=[
        {"network_id": "n1", "name": "team"}, {"network_id": "n2", "name": "lab"}])
    calls = _mock_members(monkeypatch)

    assert cli.main(["members", "add", "lab", "bob@example.com", "--role", "both"]) == 0
    capsys.readouterr()
    # explicit [grid] resolves the named grid; `both` is a first-class role, NOT expanded
    assert calls["add"] == {
        "session": "sess-tok", "network_id": "n2", "email": "bob@example.com", "roles": ["both"],
    }


def test_remote_members_add_role_provider(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                networks=[{"network_id": "n1", "name": "team"}], active="team")
    calls = _mock_members(monkeypatch)
    assert cli.main(["members", "add", "c@example.com", "--role", "provider"]) == 0
    capsys.readouterr()
    assert calls["add"]["roles"] == ["provider"]


def test_remote_members_add_json_echoes_raw_result(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                networks=[{"network_id": "n1", "name": "team"}], active="team")
    _mock_members(monkeypatch, add={"member": {"email": "alice@example.com", "roles": ["consumer"]}})
    assert cli.main(["members", "add", "alice@example.com", "--json"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out) == {"member": {"email": "alice@example.com", "roles": ["consumer"]}}


# -- handler: remove -------------------------------------------------------

def test_remote_members_remove(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                networks=[{"network_id": "n1", "name": "team"}], active="team")
    calls = _mock_members(monkeypatch, remove={"ok": True})
    assert cli.main(["members", "remove", "alice@example.com"]) == 0
    out = capsys.readouterr().out
    assert calls["remove"] == {"session": "sess-tok", "network_id": "n1", "email": "alice@example.com"}
    assert "Removed alice@example.com" in out


def test_remote_members_remove_json_echoes_raw_result(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                networks=[{"network_id": "n1", "name": "team"}], active="team")
    _mock_members(monkeypatch, remove={"ok": True})
    assert cli.main(["members", "remove", "alice@example.com", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True}


# -- handler: list ---------------------------------------------------------

def test_remote_members_list_human_shows_email_and_roles(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                networks=[{"network_id": "n1", "name": "team"}], active="team")
    _mock_members(monkeypatch, members=[
        {"email": "alice@example.com", "roles": ["consumer", "provider"]},
        {"email": "bob@example.com", "roles": ["provider"]},
    ])
    assert cli.main(["members", "list"]) == 0
    out = capsys.readouterr().out
    assert "alice@example.com" in out and "consumer,provider" in out
    assert "bob@example.com" in out and "provider" in out


def test_remote_members_list_tolerates_missing_fields(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                networks=[{"network_id": "n1", "name": "team"}], active="team")
    _mock_members(monkeypatch, members=[{"email": "x@example.com"}])  # no roles key
    assert cli.main(["members", "list"]) == 0  # .get() everywhere → no crash
    assert "x@example.com" in capsys.readouterr().out


def test_remote_members_list_json_emits_raw_list(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                networks=[{"network_id": "n1", "name": "team"}], active="team")
    _mock_members(monkeypatch, members=[{"email": "a@example.com", "roles": ["consumer"]}])
    assert cli.main(["members", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [{"email": "a@example.com", "roles": ["consumer"]}]


def test_remote_members_list_empty(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                networks=[{"network_id": "n1", "name": "team"}], active="team")
    _mock_members(monkeypatch, members=[])
    assert cli.main(["members", "list"]) == 0
    assert "no members" in capsys.readouterr().out.lower()


# -- selection + gates -----------------------------------------------------

def test_remote_members_requires_sign_in(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    state.set_mode("remote")  # remote mode, not signed in
    with pytest.raises(SystemExit) as exc:
        cli.main(["members", "list"])
    assert "signed in" in str(exc.value).lower()


def test_remote_members_requires_grid_resolution(monkeypatch, tmp_path):
    _seed_remote(monkeypatch, tmp_path, networks=[
        {"network_id": "n1", "name": "a"}, {"network_id": "n2", "name": "b"}])  # none active
    with pytest.raises(SystemExit) as exc:
        cli.main(["members", "list"])
    assert "name a grid" in str(exc.value).lower()


def test_remote_members_grid_not_found(monkeypatch, tmp_path):
    _seed_remote(monkeypatch, tmp_path, networks=[{"network_id": "n1", "name": "team"}])
    with pytest.raises(SystemExit) as exc:
        cli.main(["members", "list", "nope"])
    assert "not found" in str(exc.value).lower()


def test_remote_members_gated_in_local_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))  # default local mode
    with pytest.raises(SystemExit) as exc:
        cli.main(["members", "list"])
    assert "remote" in str(exc.value).lower()


def test_remote_members_never_prints_session_token(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                networks=[{"network_id": "n1", "name": "team"}], active="team")
    _mock_members(monkeypatch, add={"member": {"email": "a@example.com"}}, remove={"ok": True},
                  members=[{"email": "a@example.com", "roles": ["consumer"]}])
    cli.main(["members", "add", "a@example.com"])
    cli.main(["members", "remove", "a@example.com"])
    cli.main(["members", "list"])
    assert "sess-tok" not in capsys.readouterr().out


def test_remote_members_unknown_subcommand_errors(monkeypatch, tmp_path):
    _seed_remote(monkeypatch, tmp_path,
                networks=[{"network_id": "n1", "name": "team"}], active="team")
    _mock_members(monkeypatch)
    with pytest.raises(SystemExit) as exc:  # the parser blocks this via the CLI; the guard catches misuse
        cli.cmd_remote_members(SimpleNamespace(subcommand="bogus", grid=None, json=False))
    assert "subcommand" in str(exc.value).lower()


def test_members_classified_remote_only():
    assert "members" in dispatch.REMOTE_ONLY
    assert not (set(dispatch.AGNOSTIC) & set(dispatch.REMOTE_ONLY))
    assert not (set(dispatch.REMOTE_HANDLERS) & set(dispatch.REMOTE_ONLY))


def test_members_parser_wires_subcommands_to_handler():
    parser = cli.build_parser()
    for argv in (["members", "add", "e@x.com"], ["members", "remove", "e@x.com"], ["members", "list"]):
        assert parser.parse_args(argv).handler is cli.cmd_remote_members


# -- control-plane wire contract -------------------------------------------

def test_control_plane_add_member_posts_with_session_bearer(monkeypatch, tmp_path):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def handler(request):
        seen["method"], seen["path"] = request.method, request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"member": {"email": "a@b.com", "roles": ["both"]}})

    _mock_control_plane(monkeypatch, handler)
    result = control_plane.add_member("sess-tok", "n1", "a@b.com", ["both"])
    assert result == {"member": {"email": "a@b.com", "roles": ["both"]}}
    assert (seen["method"], seen["path"]) == ("POST", "/v1/grid/managed-networks/n1/members")
    assert seen["auth"] == "Bearer sess-tok"
    assert seen["body"] == {"email": "a@b.com", "roles": ["both"]}


def test_control_plane_remove_member_deletes_with_encoded_email(monkeypatch, tmp_path):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def handler(request):
        seen["method"], seen["url"] = request.method, str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"ok": True})

    _mock_control_plane(monkeypatch, handler)
    control_plane.remove_member("sess-tok", "n1", "a@b.com")
    assert seen["method"] == "DELETE"
    # email is percent-encoded into a single path segment (no path traversal possible)
    assert "/v1/grid/managed-networks/n1/members/a%40b.com" in seen["url"]
    assert seen["auth"] == "Bearer sess-tok"


def test_control_plane_remove_member_email_cannot_traverse_path(monkeypatch, tmp_path):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={})

    _mock_control_plane(monkeypatch, handler)
    control_plane.remove_member("sess-tok", "n1", "../../admin")
    assert "/members/" in seen["url"] and "/members/../" not in seen["url"]  # slash encoded, no escape
    assert "..%2F" in seen["url"]  # the traversal slash is percent-encoded, present only as data


def test_control_plane_remove_member_handles_no_content(monkeypatch, tmp_path):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _mock_control_plane(monkeypatch, lambda r: httpx.Response(204))  # successful DELETE, empty body
    assert control_plane.remove_member("sess-tok", "n1", "a@b.com") == {}  # no JSON-decode crash


def test_control_plane_list_members_parses_envelope(monkeypatch, tmp_path):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def handler(request):
        seen["method"], seen["path"] = request.method, request.url.path
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"members": [{"email": "a@b.com", "roles": ["consumer"]}]})

    _mock_control_plane(monkeypatch, handler)
    members = control_plane.list_members("sess-tok", "n1")
    assert members == [{"email": "a@b.com", "roles": ["consumer"]}]
    assert (seen["method"], seen["path"]) == ("GET", "/v1/grid/managed-networks/n1/members")
    assert seen["auth"] == "Bearer sess-tok"


def test_control_plane_list_members_tolerates_bare_array(monkeypatch, tmp_path):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _mock_control_plane(monkeypatch, lambda r: httpx.Response(200, json=[{"email": "a@b.com"}]))
    assert control_plane.list_members("sess-tok", "n1") == [{"email": "a@b.com"}]


def test_control_plane_list_members_defaults_missing_to_empty(monkeypatch, tmp_path):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _mock_control_plane(monkeypatch, lambda r: httpx.Response(200, json={"members": None}))
    assert control_plane.list_members("sess-tok", "n1") == []


def test_control_plane_list_members_handles_no_content(monkeypatch, tmp_path):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _mock_control_plane(monkeypatch, lambda r: httpx.Response(204))  # empty body → no decode crash
    assert control_plane.list_members("sess-tok", "n1") == []
