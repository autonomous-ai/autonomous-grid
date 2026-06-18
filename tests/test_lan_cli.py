from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from grid import cli, config, paths, runtime
from grid.engine import comfyui, installer, launcher
from grid.models import catalog, media_bundles
from grid.provider import media_server
from grid.server import create_app
from grid.system import detect


def _provider_args(**overrides) -> SimpleNamespace:
    base = dict(
        network="http://192.168.1.25:8090",
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


def test_init_network_config_is_lan_permissionless(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    monkeypatch.setattr(runtime, "detect_lan_ip", lambda: "192.168.1.25")

    cfg = runtime.init_network_config(name="home", port=48090)

    assert cfg["network_type"] == runtime.NETWORK_TYPE
    assert cfg["managed_server"] is True
    assert cfg["lan_signaling_url"] == "http://192.168.1.25:48090"


def test_select_network_accepts_signaling_url(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))

    cfg = config.select_network("http://192.168.1.25:8090/")

    assert cfg["network_type"] == runtime.NETWORK_TYPE
    assert cfg["managed_server"] is False
    assert cfg["lan_signaling_url"] == "http://192.168.1.25:8090"


def test_select_grid_defaults_to_only_grid_then_home(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))

    with pytest.raises(SystemExit):
        config.select_grid(None)

    runtime.init_network_config(name="solo", port=8090)
    assert config.select_grid(None)["name"] == "solo"

    runtime.init_network_config(name="home", port=8091)
    assert config.select_grid(None)["name"] == "home"


def test_server_registers_and_discovers_provider_without_auth():
    app = create_app(network_id="ag-test", network_name="test")
    client = TestClient(app)

    info = client.get("/server/info")
    assert info.status_code == 200
    assert info.json()["auth_required"] is False

    update = client.put(
        "/nodes/node-1",
        json={
            "role": "provider",
            "models": ["qwen-local"],
            "endpoint_url": "http://192.168.1.50:8081/v1",
        },
    )
    assert update.status_code == 200

    discover = client.get("/nodes/discover", params={"model": "qwen-local"})
    assert discover.status_code == 200
    providers = discover.json()["providers"]
    assert providers[0]["node_id"] == "node-1"
    assert providers[0]["endpoint_url"] == "http://192.168.1.50:8081/v1"


def test_server_exposes_media_routes_without_auth():
    app = create_app(network_id="ag-test", network_name="test")
    client = TestClient(app)

    resp = client.post("/v1/media/image/generate", json={"prompt": "desk"})

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "provider_unavailable"


def test_server_accepts_media_only_provider_without_endpoint_url():
    app = create_app(network_id="ag-test", network_name="test")
    client = TestClient(app)

    update = client.put(
        "/nodes/node-media",
        json={
            "role": "provider",
            "models": ["comfyui:image_editing"],
            "media_url": "http://192.168.1.50:8190",
        },
    )

    assert update.status_code == 200
    discover = client.get("/nodes/discover", params={"model": "comfyui:image_editing"})
    providers = discover.json()["providers"]
    assert providers[0]["node_id"] == "node-media"
    assert providers[0]["endpoint_url"] is None
    assert providers[0]["media_url"] == "http://192.168.1.50:8190"


def test_server_rejects_provider_missing_required_capability_url():
    app = create_app(network_id="ag-test", network_name="test")
    client = TestClient(app)

    text = client.put(
        "/nodes/node-text",
        json={
            "role": "provider",
            "models": ["qwen-local"],
        },
    )
    media = client.put(
        "/nodes/node-media",
        json={
            "role": "provider",
            "models": ["comfyui:image_editing"],
        },
    )

    assert text.status_code == 400
    assert text.json()["detail"] == "endpoint_url is required for text providers"
    assert media.status_code == 400
    assert media.json()["detail"] == "media_url is required for media providers"


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


def test_run_provider_launches_local_llama_server_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    calls = {}
    monkeypatch.setattr(runtime, "detect_lan_ip", lambda: "192.168.1.50")

    def fake_start_llm(model, **kwargs):
        calls["model"] = model
        calls["kwargs"] = kwargs
        return launcher.LlamaProcess(proc=FakeProc(), port=kwargs["port"], log=tmp_path / "llama.log")

    monkeypatch.setattr(launcher, "is_port_in_use", lambda port: False)
    monkeypatch.setattr(launcher, "assert_supported_build", lambda: None)
    monkeypatch.setattr(launcher, "start_llm", fake_start_llm)
    monkeypatch.setattr(launcher, "wait_for_models", lambda proc: calls.setdefault("waited", proc.port))
    monkeypatch.setattr(launcher, "stop", lambda proc: calls.setdefault("stopped", proc.port))
    monkeypatch.setattr(cli.provider, "_register_provider", lambda url, node_id, payload: calls.setdefault("payload", payload))
    monkeypatch.setattr(cli.httpx, "delete", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()))

    args = _provider_args(models=["Qwen3.5-2B-UD-IQ2_M.gguf"])

    assert cli.provider._run_provider(args) == 0
    assert calls["model"] == "Qwen3.5-2B-UD-IQ2_M.gguf"
    assert calls["kwargs"]["port"] == 8081
    assert calls["waited"] == 8081
    assert calls["stopped"] == 8081
    assert calls["payload"]["endpoint_url"] == "http://192.168.1.50:8081/v1"


def test_run_provider_advertise_as_routes_alias_and_sets_llama_alias(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    calls = {}
    monkeypatch.setattr(runtime, "detect_lan_ip", lambda: "192.168.1.50")

    def fake_start_llm(model, **kwargs):
        calls["model"] = model
        calls["kwargs"] = kwargs
        return launcher.LlamaProcess(proc=FakeProc(), port=kwargs["port"], log=tmp_path / "llama.log")

    monkeypatch.setattr(launcher, "is_port_in_use", lambda port: False)
    monkeypatch.setattr(launcher, "assert_supported_build", lambda: None)
    monkeypatch.setattr(launcher, "start_llm", fake_start_llm)
    monkeypatch.setattr(launcher, "wait_for_models", lambda proc: None)
    monkeypatch.setattr(launcher, "stop", lambda proc: calls.setdefault("stopped", proc.port))
    monkeypatch.setattr(cli.provider, "_register_provider", lambda url, node_id, payload: calls.setdefault("payload", payload))
    monkeypatch.setattr(cli.httpx, "delete", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()))

    args = _provider_args(models=["your-model.gguf"], advertise_as=["your-model"])

    assert cli.provider._run_provider(args) == 0
    assert calls["model"] == "your-model.gguf"
    assert calls["kwargs"]["alias"] == "your-model"
    assert calls["payload"]["models"] == ["your-model"]
    assert calls["payload"]["endpoint_url"] == "http://192.168.1.50:8081/v1"


def test_run_provider_endpoint_url_skips_local_llama_server(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    calls = {}

    monkeypatch.setattr(launcher, "start_llm", lambda *args, **kwargs: pytest.fail("start_llm should not be called"))
    monkeypatch.setattr(cli.provider, "_register_provider", lambda url, node_id, payload: calls.setdefault("payload", payload))
    monkeypatch.setattr(cli.httpx, "delete", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()))

    args = _provider_args(models=["custom-model"], endpoint_url="http://192.168.1.50:8081/v1")

    assert cli.provider._run_provider(args) == 0
    assert calls["payload"]["endpoint_url"] == "http://192.168.1.50:8081/v1"


def test_run_provider_enable_media_advertises_media_models(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    calls = {}
    monkeypatch.setattr(runtime, "detect_lan_ip", lambda: "192.168.1.50")
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
        "_prepare_media_provider",
        lambda args: {
            "models": ["comfyui:image_generation", "comfyui:image_editing", "comfyui:i2v"],
            "proc": None,
            "media_url": "http://192.168.1.50:8190",
            "comfyui_started": False,
        },
    )
    monkeypatch.setattr(cli.provider, "_register_provider", lambda url, node_id, payload: calls.setdefault("payload", payload))
    monkeypatch.setattr(cli.httpx, "delete", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()))

    args = _provider_args(models=["Qwen3.5-2B-UD-IQ2_M.gguf"], enable_media=True)

    assert cli.provider._run_provider(args) == 0
    assert calls["payload"]["media_url"] == "http://192.168.1.50:8190"
    assert "comfyui:image_generation" in calls["payload"]["models"]
    assert calls["payload"]["capabilities"]["models"]["comfyui:i2v"]["endpoints"] == ["media"]


def test_run_provider_media_only_skips_local_llama_server(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    calls = {}

    monkeypatch.setattr(launcher, "start_llm", lambda *args, **kwargs: pytest.fail("start_llm should not be called"))
    monkeypatch.setattr(
        cli.provider,
        "_prepare_media_provider",
        lambda args: {
            "models": ["comfyui:image_editing"],
            "proc": None,
            "media_url": "http://192.168.1.50:8190",
            "comfyui_started": False,
        },
    )
    monkeypatch.setattr(cli.provider, "_register_provider", lambda url, node_id, payload: calls.setdefault("payload", payload))
    monkeypatch.setattr(cli.httpx, "delete", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()))

    args = _provider_args(enable_media=True, media_bundles=["image_editing"])

    assert cli.provider._run_provider(args) == 0
    assert calls["payload"]["models"] == ["comfyui:image_editing"]
    assert calls["payload"]["endpoint_url"] is None
    assert calls["payload"]["media_url"] == "http://192.168.1.50:8190"
    assert calls["payload"]["capabilities"]["models"]["comfyui:image_editing"]["endpoints"] == ["media"]


def test_join_at_writes_record_and_spawns_detached(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    monkeypatch.setattr(runtime, "detect_lan_ip", lambda: "192.168.1.50")
    cfg = runtime.init_network_config(name="home", port=8090)
    spawned = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            spawned["cmd"] = cmd
            spawned["kwargs"] = kwargs
            self.pid = 4321

    monkeypatch.setattr(cli.provider.subprocess, "Popen", FakePopen)

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

    records = cli.provider._read_records(cfg["network_id"])
    assert "mac" in records
    assert records["mac"]["endpoint_url"] == "http://192.168.1.10:11434/v1"
    assert records["mac"]["models"] == ["llama3"]
    assert records["mac"]["pid"] == 4321
    assert spawned["cmd"][-3:] == ["__provider", cfg["network_id"], "mac"]
    assert spawned["kwargs"]["start_new_session"] is True


def test_join_no_flags_single_detected_engine_joins_it(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    runtime.init_network_config(name="home", port=8090)
    monkeypatch.setattr(
        cli.provider,
        "_detect",
        lambda host: [detect.DetectedEngine(label="ollama", endpoint_url="http://192.168.1.50:11434/v1", models=["llama3"])],
    )
    monkeypatch.setattr(cli.provider.subprocess, "Popen", lambda cmd, **kw: type("P", (), {"pid": 1})())

    args = cli.build_parser().parse_args(["join", "home"])
    assert cli.cmd_join(args) == 0
    records = cli.provider._read_records(config.select_grid("home")["network_id"])
    assert records["ollama"]["endpoint_url"] == "http://192.168.1.50:11434/v1"


def test_join_multiple_detected_non_interactive_requires_all(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    runtime.init_network_config(name="home", port=8090)
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
    runtime.init_network_config(name="home", port=8090)
    monkeypatch.setattr(
        cli.provider,
        "_detect",
        lambda host: [
            detect.DetectedEngine(label="ollama", endpoint_url="http://h:11434/v1", models=["llama3"]),
            detect.DetectedEngine(label="vllm", endpoint_url="http://h:8000/v1", models=["mistral"]),
        ],
    )
    monkeypatch.setattr(cli.provider.subprocess, "Popen", lambda cmd, **kw: type("P", (), {"pid": 1})())

    args = cli.build_parser().parse_args(["join", "home", "--all"])
    assert cli.cmd_join(args) == 0
    records = cli.provider._read_records(config.select_grid("home")["network_id"])
    assert set(records) == {"ollama", "vllm"}


def test_leave_all_removes_records(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    cfg = runtime.init_network_config(name="home", port=8090)
    grid_id = cfg["network_id"]
    cli.provider._write_record(grid_id, "mac", {"engine_id": "mac", "node_id": "n", "network_id": grid_id, "pid": 0})
    cli.provider._write_record(grid_id, "gpu", {"engine_id": "gpu", "node_id": "n2", "network_id": grid_id, "pid": 0})

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
    runtime.init_network_config(name="home", port=8090)
    monkeypatch.setattr(cli.provider, "_discover", lambda cfg: _FAKE_ENGINES)

    assert cli.cmd_models(cli.build_parser().parse_args(["models", "home"])) == 0
    assert capsys.readouterr().out.splitlines() == ["gemma4-31b", "devstral"]


def test_models_verbose_table_and_json(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    runtime.init_network_config(name="home", port=8090)
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
    runtime.init_network_config(name="home", port=8090)
    monkeypatch.setattr(cli.provider, "_discover", lambda cfg: _FAKE_ENGINES)

    assert cli.cmd_engines(cli.build_parser().parse_args(["engines", "home", "--json"])) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [e["engine"] for e in payload] == ["mac", "gpu"]
    assert payload[1]["models"] == ["devstral", "gemma4-31b"]


def test_info_json_uses_contract_keys(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    runtime.init_network_config(name="home", port=8090)
    monkeypatch.setattr(cli.grid, "_live_engines", lambda url: (_FAKE_ENGINES, True))

    assert cli.cmd_info(cli.build_parser().parse_args(["info", "home", "--json"])) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"grid", "grid_url", "engines", "models"}
    assert payload["grid"] == "home"
    assert payload["models"] == ["gemma4-31b", "devstral"]
