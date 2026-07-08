from __future__ import annotations

import argparse
import base64
import json
import os
import stat
import subprocess
import threading
import time
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
from shared.models import api_catalog, catalog, download, media_bundles
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
    assert parser.parse_args(["catalog"]).api is None
    api_args = parser.parse_args(["catalog", "--api", "openai"])
    assert api_args.handler is cli.cmd_catalog
    assert api_args.api == "openai"
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
    assert comfyui.TORCH_PINNED == "torch==2.13.0.dev20260504"
    assert comfyui.COMFYUI_REQUIREMENT_PINS == (
        "comfyui_frontend_package==1.42.14",
        "comfyui_workflow_templates==0.9.62",
    )
    assert set(media_bundles.BUNDLES) == {"image_generation", "image_editing", "i2v"}
    assert media_bundles.CAPABILITY_NAME["image_generation"] == "comfyui:image_generation"


def test_create_venv_seeds_pip_into_the_uv_venv(monkeypatch, tmp_path):
    """`uv venv` ships no pip; when uv resolves a system Python that lacks ensurepip (a 3.11 rc did
    exactly this) grid's pip bootstrap has nothing to fall back to. `_create_venv` must pass `--seed`
    so uv installs pip directly, independent of the base interpreter."""
    from shared.engine import comfyui

    calls: list[list[str]] = []
    monkeypatch.setattr(comfyui.shutil, "which", lambda name: "/opt/uv" if name == "uv" else None)
    monkeypatch.setattr(comfyui, "comfyui_venv", lambda: tmp_path / "ComfyUI" / ".venv")
    monkeypatch.setattr(comfyui, "_run", lambda cmd, **kw: calls.append(list(cmd)))

    comfyui._create_venv()

    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[0] == "/opt/uv" and cmd[1] == "venv" and "--seed" in cmd


def test_create_venv_errors_clearly_without_uv_or_python311(monkeypatch, tmp_path):
    """Neither uv nor python3.11 on PATH → an actionable SystemExit naming both, not a raw
    FileNotFoundError from a bare `python3.11 -m venv`."""
    from shared.engine import comfyui

    monkeypatch.setattr(comfyui.shutil, "which", lambda name: None)  # no uv, no python3.11
    monkeypatch.setattr(comfyui, "comfyui_venv", lambda: tmp_path / "ComfyUI" / ".venv")
    monkeypatch.setattr(comfyui, "_run", lambda cmd, **kw: pytest.fail(f"must not exec anything: {cmd}"))

    with pytest.raises(SystemExit) as exc:
        comfyui._create_venv()
    msg = str(exc.value).lower()
    assert "uv" in msg and "python 3.11" in msg


def test_create_venv_fallback_errors_when_stdlib_venv_cannot_bootstrap_pip(monkeypatch, tmp_path):
    """No uv, python3.11 present but `-m venv` fails (stripped ensurepip) → actionable SystemExit, not
    a raw CalledProcessError."""
    from shared.engine import comfyui

    monkeypatch.setattr(comfyui.shutil, "which",
                        lambda name: "/usr/bin/python3.11" if name == "python3.11" else None)
    monkeypatch.setattr(comfyui, "comfyui_venv", lambda: tmp_path / "ComfyUI" / ".venv")

    def boom(cmd, **kw):
        raise comfyui.subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(comfyui, "_run", boom)
    with pytest.raises(SystemExit) as exc:
        comfyui._create_venv()
    assert "ensurepip" in str(exc.value).lower()


def test_ensure_c_compiler_errors_without_compiler_on_linux(monkeypatch):
    """No cc/gcc/clang on a non-macOS host → actionable SystemExit at install time, not a Triton
    'Failed to find C compiler' crash after ~40 GB of downloads at serve time."""
    from shared.engine import comfyui

    monkeypatch.setattr(comfyui, "_is_macos", lambda: False)
    monkeypatch.setattr(comfyui.shutil, "which", lambda name: None)  # nothing on PATH
    with pytest.raises(SystemExit) as exc:
        comfyui._ensure_c_compiler()
    msg = str(exc.value).lower()
    assert "compiler" in msg and ("gcc" in msg or "build-essential" in msg)


def test_ensure_c_compiler_ok_when_present(monkeypatch):
    from shared.engine import comfyui

    monkeypatch.setattr(comfyui, "_is_macos", lambda: False)
    monkeypatch.setattr(comfyui.shutil, "which", lambda name: "/usr/bin/cc" if name == "cc" else None)
    comfyui._ensure_c_compiler()  # must not raise


def test_ensure_c_compiler_skips_on_macos(monkeypatch):
    """Triton is CUDA-only; on macOS the compiler check must not fire even with nothing on PATH."""
    from shared.engine import comfyui

    monkeypatch.setattr(comfyui, "_is_macos", lambda: True)
    monkeypatch.setattr(comfyui.shutil, "which", lambda name: None)
    comfyui._ensure_c_compiler()  # must not raise


def test_wait_for_ready_fails_fast_when_comfyui_exited(monkeypatch, tmp_path):
    """A ComfyUI whose process already exited must raise immediately (not poll the full timeout) and
    surface the log tail, not a bare connection error."""
    from shared.engine import comfyui

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "comfyui_8188.log").write_text("Traceback ...\nRuntimeError: Failed to find C compiler.\n")

    class _DeadProc:
        returncode = 1

        def poll(self):
            return 1

    monkeypatch.setattr(comfyui.httpx, "get",
                        lambda *a, **k: pytest.fail("must not poll a dead ComfyUI proc"))
    with pytest.raises(SystemExit) as exc:
        comfyui.wait_for_ready(port=8188, proc=_DeadProc())
    assert "Failed to find C compiler" in str(exc.value)


def test_wait_for_ready_surfaces_comfyui_log_on_timeout(monkeypatch, tmp_path):
    """On timeout the error includes the ComfyUI log tail so the real crash reason is visible."""
    from shared.engine import comfyui

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "comfyui_8188.log").write_text("some import error deep in comfy\n")

    def _refuse(*a, **k):
        raise comfyui.httpx.ConnectError("connection refused")

    monkeypatch.setattr(comfyui.httpx, "get", _refuse)
    monkeypatch.setattr(comfyui.time, "sleep", lambda s: None)
    # deadline call, one in-loop check (enter), one check (exit) → force a fast single-pass timeout.
    ticks = iter([0.0, 0.0, 1000.0])
    monkeypatch.setattr(comfyui.time, "monotonic", lambda: next(ticks))
    with pytest.raises(SystemExit) as exc:
        comfyui.wait_for_ready(port=8188, timeout=1.0)
    assert "some import error deep in comfy" in str(exc.value)


def _seed_media_bringup(monkeypatch, tmp_path):
    """Common mocks so `prepare_media_engine` reaches the ComfyUI/media-server bring-up: ComfyUI
    'installed', one bundle gated in, its files 'present'."""
    from shared.engine import comfyui
    from shared.media import media_gating
    from shared.models import media_bundles as mb

    present = tmp_path / "present"
    present.write_text("x")
    monkeypatch.setattr(comfyui, "comfyui_dir", lambda: tmp_path)
    monkeypatch.setattr(media_gating, "select_bundles", lambda mem, requested=None: [media_gating.GATES[0]])
    monkeypatch.setattr(mb, "target_path", lambda spec: present)


def test_present_bundles_helper(monkeypatch, tmp_path):
    """`present_bundles()` returns only bundles whose every file is on disk, in canonical order."""
    from shared.models import media_bundles as mb

    present = tmp_path / "present"
    present.write_text("x")
    missing = tmp_path / "missing"
    monkeypatch.setattr(mb, "target_path",
                        lambda spec: present if spec in mb.IMAGE_GENERATION else missing)

    assert mb.bundle_is_present("image_generation") is True
    assert mb.bundle_is_present("image_editing") is False
    assert mb.present_bundles() == ["image_generation"]


def _seed_media_probe(monkeypatch):
    """Force the NVIDIA path with one >=22 GB card so the real `select_bundles` gates a bundle in
    (a no-GPU dev/CI box otherwise returns [] and short-circuits before the assertion)."""
    from shared.media import media_gating
    from shared.system import gpu as gpu_probe

    monkeypatch.setattr(media_gating, "is_apple_silicon", lambda: False)
    monkeypatch.setattr(gpu_probe, "enumerate_gpus", lambda: [SimpleNamespace(memory_total_mb=24564)])


def test_prepare_media_engine_defaults_to_present_bundles(monkeypatch, tmp_path):
    """`grid join --media` with no --bundle advertises only the bundle(s) actually pulled, not all 3."""
    from local import media_engine, media_runtime, runtime
    from shared.engine import comfyui
    from shared.models import media_bundles as mb

    monkeypatch.setattr(comfyui, "comfyui_dir", lambda: tmp_path)
    present = tmp_path / "present"
    present.write_text("x")
    missing = tmp_path / "missing"
    monkeypatch.setattr(mb, "target_path",
                        lambda spec: present if spec in mb.IMAGE_GENERATION else missing)
    _seed_media_probe(monkeypatch)
    monkeypatch.setattr(comfyui, "is_running", lambda port: True)  # skip real ComfyUI start()
    monkeypatch.setattr(media_runtime, "start_media_server", lambda **kw: SimpleNamespace(pid=7))
    monkeypatch.setattr(runtime, "engine_endpoint_url", lambda *a, **k: "http://x/v1")

    out = media_engine.prepare_media_engine(
        media_bundles=None, comfyui_port=8188, media_port=8190, advertise_host=None)
    assert out["models"] == ["comfyui:image_generation"]


def test_prepare_media_engine_errors_when_no_bundle_present(monkeypatch, tmp_path):
    """No --bundle and nothing pulled → a clear 'pull a bundle' SystemExit, not a confusing all-3 fail."""
    from local import media_engine
    from shared.engine import comfyui
    from shared.models import media_bundles as mb

    monkeypatch.setattr(comfyui, "comfyui_dir", lambda: tmp_path)
    monkeypatch.setattr(mb, "target_path", lambda spec: tmp_path / "missing")  # nothing present
    _seed_media_probe(monkeypatch)
    monkeypatch.setattr(comfyui, "is_running", lambda port: pytest.fail("must fail before starting ComfyUI"))

    with pytest.raises(SystemExit) as exc:
        media_engine.prepare_media_engine(
            media_bundles=None, comfyui_port=8188, media_port=8190, advertise_host=None)
    assert "pull" in str(exc.value).lower()


def test_prepare_media_engine_explicit_bundle_missing_still_errors(monkeypatch, tmp_path):
    """Explicit --bundle stays strict: naming an un-pulled bundle errors with 'missing files'."""
    from local import media_engine
    from shared.engine import comfyui
    from shared.models import media_bundles as mb

    monkeypatch.setattr(comfyui, "comfyui_dir", lambda: tmp_path)
    monkeypatch.setattr(mb, "target_path", lambda spec: tmp_path / "missing")
    _seed_media_probe(monkeypatch)
    monkeypatch.setattr(comfyui, "is_running", lambda port: pytest.fail("must fail before starting ComfyUI"))

    with pytest.raises(SystemExit) as exc:
        media_engine.prepare_media_engine(
            media_bundles=["image_editing"], comfyui_port=8188, media_port=8190, advertise_host=None)
    assert "missing files" in str(exc.value).lower()


def test_prepare_media_engine_rejects_colliding_ports(monkeypatch, tmp_path):
    """comfyui_port == media_port is rejected before anything is spawned."""
    from local import media_engine
    from shared.engine import comfyui

    _seed_media_bringup(monkeypatch, tmp_path)
    monkeypatch.setattr(comfyui, "is_running", lambda port: pytest.fail("must reject before starting ComfyUI"))
    with pytest.raises(SystemExit) as exc:
        media_engine.prepare_media_engine(
            media_bundles=["image_generation"], comfyui_port=8188, media_port=8188, advertise_host=None)
    assert "differ" in str(exc.value).lower()


def test_prepare_media_engine_stops_comfyui_when_media_server_fails(monkeypatch, tmp_path):
    """If the media server fails to launch after ComfyUI started, the ComfyUI we started is stopped,
    not orphaned (the reviewers' HIGH finding)."""
    from local import media_engine, media_runtime
    from shared.engine import comfyui

    _seed_media_bringup(monkeypatch, tmp_path)
    monkeypatch.setattr(comfyui, "is_running", lambda port: False)
    monkeypatch.setattr(comfyui, "start",
                        lambda port: type("CP", (), {"proc": type("P", (), {"pid": 7})(), "log": "l"})())
    monkeypatch.setattr(comfyui, "wait_for_ready", lambda port, proc=None: None)
    stops = {"n": 0}
    monkeypatch.setattr(comfyui, "stop", lambda **kw: stops.__setitem__("n", stops["n"] + 1))

    def media_boom(**kw):
        raise SystemExit("media server failed to start")

    monkeypatch.setattr(media_runtime, "start_media_server", media_boom)
    with pytest.raises(SystemExit):
        media_engine.prepare_media_engine(
            media_bundles=["image_generation"], comfyui_port=8188, media_port=8190, advertise_host=None)
    assert stops["n"] == 1  # the ComfyUI we started was stopped, not left orphaned


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


def test_catalog_api_openai_prints_whitelist(monkeypatch, capsys):
    # The api catalog is static data: no key, and any network attempt is a bug.
    def _no_network(*args, **kwargs):
        raise AssertionError("`grid catalog --api` must not touch the network")

    monkeypatch.setattr(httpx, "Client", _no_network)
    monkeypatch.setattr(httpx, "request", _no_network)
    monkeypatch.setattr(httpx, "stream", _no_network)

    rc = cli.cmd_catalog(argparse.Namespace(api="openai", json=False))

    out = capsys.readouterr().out
    assert rc == 0
    assert api_catalog.OPENAI_LAST_VERIFIED in out
    entries = api_catalog.WHITELISTS["openai"].entries
    assert entries
    for entry in entries:
        assert api_catalog.advertised_name("openai", entry) in out
        assert f"{entry.context_window:,}" in out
    # Requests to these models leave the grid — the disclosure must be printed.
    assert "leave the grid" in out


def test_catalog_api_unknown_kind_errors():
    with pytest.raises(SystemExit) as exc:
        cli.cmd_catalog(argparse.Namespace(api="anthropic", json=False))

    msg = str(exc.value)
    assert "anthropic" in msg  # the kind that was rejected
    assert "openai" in msg  # ... and the supported kinds, so the fix is one edit away

    # `--api ""` (e.g. an unset shell variable) must error too, not silently
    # fall through to the GGUF catalog.
    with pytest.raises(SystemExit):
        cli.cmd_catalog(argparse.Namespace(api="", json=False))


def test_catalog_without_api_unchanged(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))

    rc = cli.cmd_catalog(argparse.Namespace(api=None, json=False))

    out = capsys.readouterr().out
    assert rc == 0
    assert "Grid can pull:" in out
    assert "openai:" not in out


def test_api_whitelist_integrity():
    from datetime import date

    assert api_catalog.supported_kinds() == ("openai",)
    for kind, whitelist in api_catalog.WHITELISTS.items():
        assert whitelist.entries, f"{kind} whitelist must not be empty"
        names = [entry.vendor_name for entry in whitelist.entries]
        assert all(names), f"{kind} has an entry with an empty vendor name"
        assert len(set(names)) == len(names), f"{kind} has duplicate vendor names"
        for entry in whitelist.entries:
            assert entry.context_window > 0
            assert api_catalog.advertised_name(kind, entry) == f"{kind}:{entry.vendor_name}"
        date.fromisoformat(whitelist.last_verified)  # dated, ISO format
        assert whitelist.base_url.startswith("https://"), f"{kind} needs a vendor base URL"
        assert not whitelist.base_url.endswith("/"), f"{kind} base URL must not end with '/'"
        assert whitelist.env_var, f"{kind} needs the env var its key is read from"


def test_api_whitelist_carries_base_url_and_env_var():
    openai = api_catalog.WHITELISTS["openai"]
    assert openai.base_url == "https://api.openai.com/v1"
    assert openai.env_var == "OPENAI_API_KEY"


def test_api_catalog_find_advertised_and_probed_features():
    entry = api_catalog.find_advertised("openai", "openai:gpt-5.5")
    assert entry is not None and entry.vendor_name == "gpt-5.5"
    # Only the advertised (namespaced) form resolves — a bare vendor name does not.
    assert api_catalog.find_advertised("openai", "gpt-5.5") is None
    assert api_catalog.find_advertised("openai", "openai:nope") is None
    assert api_catalog.find_advertised("anthropic", "anthropic:claude") is None

    features = api_catalog.probed_features(entry)
    # Exactly the probed-dict shape remote/probe.capability_entry consumes.
    assert set(features) == {"vision", "tools", "parallel_tool_calls", "json_object", "json_schema"}
    assert features["tools"] is entry.supports_tools
    assert features["parallel_tool_calls"] is entry.supports_tools
    assert features["vision"] is entry.supports_vision
    assert features["json_object"] is entry.supports_json_mode
    assert features["json_schema"] is entry.supports_structured_outputs


def test_catalog_api_json_roundtrips(capsys):
    rc = cli.cmd_catalog(argparse.Namespace(api="openai", json=True))

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["kind"] == "openai"
    assert payload["last_verified"] == api_catalog.OPENAI_LAST_VERIFIED
    entries = api_catalog.WHITELISTS["openai"].entries
    assert len(payload["models"]) == len(entries)
    first, first_entry = payload["models"][0], entries[0]
    assert first["advertised"] == api_catalog.advertised_name("openai", first_entry)
    assert first["vendor_name"] == first_entry.vendor_name
    assert first["context_window"] == first_entry.context_window
    assert first["supports_tools"] is first_entry.supports_tools
    assert first["supports_vision"] is first_entry.supports_vision


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
    # built-in llama-server answers to its --alias, so upstream is identity (no forward rewrite)
    assert calls["payload"]["upstream"] == {"your-model": "your-model"}


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


def test_run_engine_external_advertise_as_maps_upstream(monkeypatch, tmp_path):
    """External `--at` engine under `--advertise-as`: upstream maps the alias to the engine's REAL
    model name so the local proxy can rewrite it before forwarding (mirrors the remote serve fix)."""
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    calls = {}
    monkeypatch.setattr(launcher, "start_llm", lambda *a, **k: pytest.fail("start_llm should not be called"))
    monkeypatch.setattr(cli.provider, "_register_engine", lambda url, node_id, payload: calls.setdefault("payload", payload))
    monkeypatch.setattr(cli.httpx, "delete", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()))

    args = _engine_args(models=["qwen3:0.6b"], advertise_as=["ollama-model"],
                        endpoint_url="http://192.168.1.9:11434/v1")
    assert cli.provider._run_engine(args) == 0
    assert calls["payload"]["models"] == ["ollama-model"]  # consumers see the alias
    assert calls["payload"]["upstream"] == {"ollama-model": "qwen3:0.6b"}  # engine gets the real name


def test_local_proxy_rewrites_alias_to_upstream_model(monkeypatch):
    """The local grid proxy must forward the engine's REAL model name, not the advertised alias the
    consumer used — else an external engine (Ollama/vLLM) 404s on the unknown alias (Issue 1, local)."""
    from local import server as local_server

    app = create_app(grid_id="ag-test", grid_name="test")
    client = TestClient(app)
    reg = client.put("/nodes/node-ext", json={
        "role": "engine",
        "models": ["ollama-model"],
        "endpoint_url": "http://192.168.1.9:11434/v1",
        "upstream": {"ollama-model": "qwen3:0.6b"},
    })
    assert reg.status_code == 200

    seen = {}

    def engine(request):
        seen["path"] = request.url.path
        seen["model"] = json.loads(request.content)["model"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(local_server.httpx, "AsyncClient",
                        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(engine)}))

    resp = client.post("/v1/chat/completions", json={"model": "ollama-model", "messages": []})
    assert resp.status_code == 200
    assert seen["path"].endswith("/chat/completions")
    assert seen["model"] == "qwen3:0.6b"  # alias rewritten to the engine's real model name


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


def test_join_kind_filters_to_one_engine(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    grid_id = runtime.init_grid_config(name="home", port=8090)["grid_id"]
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

    assert cli.cmd_join(cli.build_parser().parse_args(["join", "home", "--kind", "vllm"])) == 0
    assert set(cli.provider._read_records(grid_id)) == {"vllm"}  # only the vllm engine joined
    # the --engine alias drives the same filter (and errors on no match)
    assert cli.cmd_join(cli.build_parser().parse_args(["join", "home", "--engine", "ollama"])) == 0
    assert set(cli.provider._read_records(grid_id)) == {"vllm", "ollama"}
    with pytest.raises(SystemExit):
        cli.cmd_join(cli.build_parser().parse_args(["join", "home", "--kind", "nope"]))


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


def test_join_parser_accepts_api_kind(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    args = cli.build_parser().parse_args(["join", "--api", "openai", "-m", "openai:gpt-5.5"])
    assert args.api == "openai"
    assert cli.build_parser().parse_args(["join"]).api is None


def test_join_local_rejects_api_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    runtime.init_grid_config(name="home", port=8090)
    args = cli.build_parser().parse_args(["join", "home", "--api", "openai", "-m", "openai:gpt-5.5"])
    with pytest.raises(SystemExit) as exc:
        cli.cmd_join(args)  # API engines live in remote mode; local points the user there
    assert "--api" in str(exc.value) and "remote" in str(exc.value).lower()


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


def test_leave_media_engine_stops_comfyui(monkeypatch, tmp_path):
    """Leaving a media engine reaps the ComfyUI it OWNS, targeting that engine's port."""
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    cfg = runtime.init_grid_config(name="home", port=8090)
    grid_id = cfg["grid_id"]
    cli.provider._write_record(grid_id, "media1",
                               {"engine_id": "media1", "node_id": "n", "grid_id": grid_id, "pid": 0,
                                "media": True, "comfyui_started": True, "comfyui_port": 8288})

    calls = []
    monkeypatch.setattr(comfyui, "stop_running", lambda port=8188: calls.append(port) or 0)
    args = cli.build_parser().parse_args(["leave", "home", "--engine", "media1"])
    assert cli.cmd_leave(args) == 0
    assert calls == [8288]  # reaped the engine's own port, not the global default
    assert cli.provider._read_records(grid_id) == {}


def test_leave_non_media_engine_skips_comfyui(monkeypatch, tmp_path):
    """A text engine's leave must not touch ComfyUI."""
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    cfg = runtime.init_grid_config(name="home", port=8090)
    grid_id = cfg["grid_id"]
    cli.provider._write_record(grid_id, "text1",
                               {"engine_id": "text1", "node_id": "n", "grid_id": grid_id, "pid": 0, "media": False})

    monkeypatch.setattr(comfyui, "stop_running",
                        lambda *a, **k: pytest.fail("must not reap ComfyUI for a text engine"))
    args = cli.build_parser().parse_args(["leave", "home", "--engine", "text1"])
    assert cli.cmd_leave(args) == 0


def test_leave_media_engine_without_ownership_skips_comfyui(monkeypatch, tmp_path):
    """A media engine that did NOT start ComfyUI (reused a shared/pre-existing one) must not reap it —
    else leaving one media join kills another engine's still-serving ComfyUI."""
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    cfg = runtime.init_grid_config(name="home", port=8090)
    grid_id = cfg["grid_id"]
    cli.provider._write_record(grid_id, "shared1",
                               {"engine_id": "shared1", "node_id": "n", "grid_id": grid_id, "pid": 0,
                                "media": True, "comfyui_started": False, "comfyui_port": 8188})

    monkeypatch.setattr(comfyui, "stop_running",
                        lambda *a, **k: pytest.fail("must not reap a ComfyUI this engine didn't start"))
    args = cli.build_parser().parse_args(["leave", "home", "--engine", "shared1"])
    assert cli.cmd_leave(args) == 0
    assert cli.provider._read_records(grid_id) == {}


def test_run_engine_reaps_record_when_never_registered(monkeypatch, tmp_path):
    """A media engine that dies before registering (ComfyUI never ready) must reap its own record,
    not leave a ghost that needs `grid leave --all` to clear."""
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    cfg = runtime.init_grid_config(name="home", port=8090)
    grid_id = cfg["grid_id"]
    cli.provider._write_record(grid_id, "media1",
                               {"engine_id": "media1", "node_id": "n", "grid_id": grid_id, "pid": 123, "media": True})

    def boom(_args):
        raise SystemExit("ComfyUI did not become ready")

    monkeypatch.setattr(cli.provider, "_prepare_media_engine", boom)
    args = _engine_args(grid=grid_id, name="media1", enable_media=True)
    with pytest.raises(SystemExit):
        cli.provider._run_engine(args)
    assert cli.provider._read_records(grid_id) == {}


def test_run_engine_persists_comfyui_ownership(monkeypatch, tmp_path):
    """When the engine starts ComfyUI, `comfyui_started` is written to the record so a later
    `grid leave` reaps only a ComfyUI this engine owns."""
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    cfg = runtime.init_grid_config(name="home", port=8090)
    grid_id = cfg["grid_id"]
    cli.provider._write_record(grid_id, "media1",
                               {"engine_id": "media1", "node_id": "n", "grid_id": grid_id, "pid": 1, "media": True})

    monkeypatch.setattr(cli.provider, "_prepare_media_engine", lambda args: {
        "models": ["comfyui:image_generation"], "proc": None,
        "media_url": "http://x:8190", "comfyui_started": True,
    })
    monkeypatch.setattr(cli.provider, "_register_engine", lambda url, node_id, payload: None)
    monkeypatch.setattr(cli.httpx, "delete", lambda *a, **k: None)
    monkeypatch.setattr(cli.time, "sleep", lambda s: (_ for _ in ()).throw(KeyboardInterrupt()))

    args = _engine_args(grid=grid_id, name="media1", enable_media=True)
    assert cli.provider._run_engine(args) == 0
    assert cli.provider._read_records(grid_id)["media1"]["comfyui_started"] is True


def _seed_local_engine(grid_id, engine_id, *, endpoint_url=None, models=None):
    cli.provider._write_record(grid_id, engine_id, {
        "engine_id": engine_id, "node_id": engine_id, "grid_id": grid_id, "pid": 0,
        "endpoint_url": endpoint_url, "models": models or [],
    })


def _leave(*argv):
    return cli.cmd_leave(cli.build_parser().parse_args(["leave", *argv]))


def test_leave_local_matches_by_endpoint_url(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    grid_id = runtime.init_grid_config(name="home", port=8090)["grid_id"]
    _seed_local_engine(grid_id, "mac", endpoint_url="http://h:8000/v1", models=["mistral"])
    _seed_local_engine(grid_id, "gpu", endpoint_url="http://h:9000/v1", models=["devstral"])
    assert _leave("home", "--engine", "http://h:8000/v1") == 0
    assert set(cli.provider._read_records(grid_id)) == {"gpu"}


def test_leave_local_matches_by_served_model(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    grid_id = runtime.init_grid_config(name="home", port=8090)["grid_id"]
    _seed_local_engine(grid_id, "mac", endpoint_url="http://h:8000/v1", models=["mistral"])
    _seed_local_engine(grid_id, "gpu", endpoint_url="http://h:9000/v1", models=["devstral"])
    assert _leave("home", "--engine", "devstral") == 0
    assert set(cli.provider._read_records(grid_id)) == {"mac"}


def test_leave_local_matches_by_url_fragment(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    grid_id = runtime.init_grid_config(name="home", port=8090)["grid_id"]
    _seed_local_engine(grid_id, "mac", endpoint_url="http://h:8000/v1", models=["mistral"])
    _seed_local_engine(grid_id, "gpu", endpoint_url="http://h:9000/v1", models=["devstral"])
    assert _leave("home", "--engine", ":9000") == 0
    assert set(cli.provider._read_records(grid_id)) == {"mac"}


def test_leave_local_exact_id_wins_over_url_substring(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    grid_id = runtime.init_grid_config(name="home", port=8090)["grid_id"]
    _seed_local_engine(grid_id, "mac", endpoint_url="http://h:8000/v1", models=["m1"])
    _seed_local_engine(grid_id, "gpu", endpoint_url="http://mac-host:9000/v1", models=["m2"])
    assert _leave("home", "--engine", "mac") == 0  # exact id, not the substring of gpu's URL
    assert set(cli.provider._read_records(grid_id)) == {"gpu"}


def test_leave_local_ambiguous_model_lists_ids(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    grid_id = runtime.init_grid_config(name="home", port=8090)["grid_id"]
    _seed_local_engine(grid_id, "mac", endpoint_url="http://h:8000/v1", models=["shared"])
    _seed_local_engine(grid_id, "gpu", endpoint_url="http://h:9000/v1", models=["shared"])
    with pytest.raises(SystemExit) as exc:
        _leave("home", "--engine", "shared")
    msg = str(exc.value)
    assert "several" in msg.lower() and "mac" in msg and "gpu" in msg


def test_leave_local_unknown_selector_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    grid_id = runtime.init_grid_config(name="home", port=8090)["grid_id"]
    _seed_local_engine(grid_id, "mac", endpoint_url="http://h:8000/v1", models=["mistral"])
    with pytest.raises(SystemExit) as exc:
        _leave("home", "--engine", "ghost")
    assert "ghost" in str(exc.value)
    assert set(cli.provider._read_records(grid_id)) == {"mac"}  # nothing dropped on a miss


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


# ---------------------------------------------------------------------------
# CLI UX overhaul (ADR 0011): help text, join flag groups, `grid ls` id column
# ---------------------------------------------------------------------------

def _subcmd_help(cmd: str) -> str:
    """Whitespace-collapsed --help text for a subcommand. argparse re-wraps help to the terminal
    width, so collapsing runs of whitespace lets substring asserts survive line breaks."""
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return " ".join(sub.choices[cmd].format_help().split())


def test_leave_help_lists_all_match_kinds():
    help_text = _subcmd_help("leave")
    for kind in ("engine id", "endpoint URL", "served model", "URL fragment"):
        assert kind in help_text, f"leave --engine help should mention {kind!r}"


def test_leave_all_help_states_multi_engine_needs_all():
    assert "multi-engine" in _subcmd_help("leave")


def test_join_help_groups_flags():
    help_text = _subcmd_help("join")
    for title in ("Choose an engine", "Name & display", "Media", "Built-in", "Local only", "Remote only"):
        assert title in help_text, f"join -h should have a {title!r} group"


def test_join_help_marks_mode_scoped_flags():
    help_text = _subcmd_help("join")
    assert "(local only)" in help_text and "(remote only)" in help_text


def test_positional_grid_help_present():
    assert "Grid name or id" in _subcmd_help("up")
    assert "Grid name or id" in _subcmd_help("join")


def test_ls_shows_grid_id_local(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    cfg = runtime.init_grid_config(name="home", port=8090)
    assert cli.cmd_ls(cli.build_parser().parse_args(["ls"])) == 0
    assert cfg["grid_id"] in capsys.readouterr().out  # id column present in human output
    assert cli.cmd_ls(cli.build_parser().parse_args(["ls", "--json"])) == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == cfg["grid_id"]


def test_join_kind_flag_with_engine_alias():
    parser = cli.build_parser()
    assert parser.parse_args(["join", "--kind", "ollama"]).kind == "ollama"
    assert parser.parse_args(["join", "--engine", "vllm"]).kind == "vllm"  # --engine is a back-compat alias


def _parse_join(*argv):
    return cli.build_parser().parse_args(["join", *argv])


def test_join_inline_alias_desugars_into_record(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    cfg = runtime.init_grid_config(name="home", port=8090)

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            self.pid = 4321

    monkeypatch.setattr(cli.provider.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(cli.provider, "_await_engine_start", lambda *a, **k: "registered")
    args = _parse_join("home", "--at", "http://h:11434/v1", "-m", "real-a=pub-a", "--name", "mac")
    assert cli.cmd_join(args) == 0
    rec = cli.provider._read_records(cfg["grid_id"])["mac"]
    assert rec["models"] == ["real-a"]
    assert rec["advertise_as"] == ["pub-a"]


def test_inline_alias_rejects_mixing_with_advertise_as():
    args = _parse_join("--at", "u", "-m", "real=pub", "--advertise-as", "x")
    with pytest.raises(SystemExit) as exc:
        cli.provider._apply_inline_aliases(args)
    assert "not both" in str(exc.value)


def test_inline_alias_all_or_nothing():
    args = _parse_join("--at", "u", "-m", "real=pub", "-m", "plain")
    with pytest.raises(SystemExit):
        cli.provider._apply_inline_aliases(args)


def test_inline_alias_rejects_empty_side():
    for bad in ("=pub", "real="):
        with pytest.raises(SystemExit):
            cli.provider._apply_inline_aliases(_parse_join("--at", "u", "-m", bad))


def test_inline_alias_no_equals_is_untouched():
    args = _parse_join("--at", "u", "-m", "qwen3:0.6b")
    cli.provider._apply_inline_aliases(args)
    assert args.models == ["qwen3:0.6b"] and args.advertise_as == []


def test_serve_rejects_inline_equals():
    args = _parse_join("--serve", "real=pub")
    with pytest.raises(SystemExit) as exc:
        cli.provider._apply_inline_aliases(args)
    assert "--advertise-as" in str(exc.value)


def test_serve_rejects_extra_model_flag():
    # --serve serves one built-in model; a separate -m (aliased or not) would be silently dropped.
    for argv in (["--serve", "modelY", "-m", "real=pub"], ["--serve", "modelY", "-m", "foo"]):
        with pytest.raises(SystemExit) as exc:
            cli.cmd_join(_parse_join(*argv))
        assert "--serve" in str(exc.value)


def test_inline_alias_rejects_multiple_equals():
    with pytest.raises(SystemExit):
        cli.provider._apply_inline_aliases(_parse_join("--at", "u", "-m", "a=b=c"))


def test_remote_join_inline_alias_desugars_into_record(monkeypatch, tmp_path):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    assert cli.main(["join", "--at", "http://h:11434/v1", "-m", "real-a=pub-a"]) == 0
    rec = cli.provider._read_records("n1")["remote"]
    assert rec["models"] == ["real-a"]
    assert rec["advertise_as"] == ["pub-a"]


def test_engine_label_is_deprecated_but_still_recorded(monkeypatch, tmp_path, capsys):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    assert cli.main(["join", "--serve", "m", "--engine-label", "rig"]) == 0
    err = capsys.readouterr().err
    assert "engine-label" in err and "deprecated" in err.lower()
    # still stored so `grid leave --engine <label>` keeps matching (ADR 0011 D-c)
    assert cli.provider._read_records("n1")["remote"]["engine_label"] == "rig"


def test_engine_ls_and_list_route_to_cmd_engine_list():
    parser = cli.build_parser()
    assert parser.parse_args(["engine", "ls"]).handler is cli.cmd_engine_list
    assert parser.parse_args(["engine", "list"]).handler is cli.cmd_engine_list


def test_engine_ls_accepts_grid_and_json():
    args = cli.build_parser().parse_args(["engine", "ls", "home", "--json"])
    assert args.grid == "home" and args.json is True


def test_engine_ls_local_delegates_to_cmd_engines(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def fake_engines(args):
        seen["grid"] = args.grid
        return 0

    monkeypatch.setattr(cli.provider, "cmd_engines", fake_engines)
    assert cli.main(["engine", "ls", "home"]) == 0  # default mode is local
    assert seen["grid"] == "home"


def test_engine_ls_remote_delegates_to_remote_engines(monkeypatch, tmp_path):
    _seed_remote(monkeypatch, tmp_path)
    seen = {}

    def fake_remote_engines(args):
        seen["hit"] = True
        return 0

    monkeypatch.setattr(cli.remote_overview, "cmd_remote_engines", fake_remote_engines)
    assert cli.main(["engine", "ls"]) == 0
    assert seen.get("hit") is True


def test_looks_like_grid_id_detector():
    assert cli.grid._looks_like_grid_id("ag-home-deadbeef") is True
    assert cli.grid._looks_like_grid_id("workshop") is False
    assert cli.grid._looks_like_grid_id("ag-team") is False  # no hex8 suffix → still a creatable name


def test_up_rejects_unknown_grid_id_without_creating(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        cli.main(["up", "ag-home-deadbeef"])
    assert "grid ls" in str(exc.value)
    from local import config as local_config
    assert local_config.iter_grid_configs() == []  # nothing created, nothing spawned


def test_up_creates_for_human_name(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    monkeypatch.setattr(cli.grid.runtime, "start_grid", lambda cfg: None)
    assert cli.main(["up", "workshop"]) == 0
    from local import config as local_config
    assert any(c["name"] == "workshop" for c in local_config.iter_grid_configs())


def test_up_starts_existing_grid_by_id(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    cfg = runtime.init_grid_config(name="home", port=8090)
    monkeypatch.setattr(cli.grid.runtime, "start_grid", lambda cfg: None)
    assert cli.main(["up", cfg["grid_id"]]) == 0  # found by id → guard skipped


def test_up_rejects_known_remote_grid_in_local_mode(monkeypatch, tmp_path):
    _seed_remote(monkeypatch, tmp_path, networks=[{"network_id": "n1", "name": "team"}])
    state.set_mode("local")  # the remote grid is known via credentials, but we're in local mode
    from local import config as local_config
    for arg in ("team", "n1"):  # matched by name and by network_id
        with pytest.raises(SystemExit) as exc:
            cli.main(["up", arg])
        assert "remote" in str(exc.value).lower()
    assert local_config.iter_grid_configs() == []  # nothing created


_FAKE_ENGINES = [
    {"name": "mac", "endpoint_url": "http://192.168.1.10:8080/v1", "models": ["gemma4-31b"]},
    {"name": "gpu", "endpoint_url": "http://192.168.1.20:8000/v1", "models": ["devstral", "gemma4-31b"],
     "max_concurrency": 4},
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


def test_engines_shows_max_concurrency(monkeypatch, tmp_path, capsys):
    """`grid engines` surfaces the advertised max_concurrency (remote-only): in --json always (null
    when the engine didn't advertise it), and in the table only for engines that report it."""
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    runtime.init_grid_config(name="home", port=8090)
    monkeypatch.setattr(cli.provider, "_discover", lambda cfg: _FAKE_ENGINES)

    assert cli.cmd_engines(cli.build_parser().parse_args(["engines", "home", "--json"])) == 0
    by_name = {e["engine"]: e for e in json.loads(capsys.readouterr().out)}
    assert by_name["gpu"]["max_concurrency"] == 4
    assert by_name["mac"]["max_concurrency"] is None  # engine didn't advertise it

    assert cli.cmd_engines(cli.build_parser().parse_args(["engines", "home"])) == 0
    out = capsys.readouterr().out
    assert "concurrency: 4" in out            # gpu advertised it
    assert out.count("concurrency:") == 1     # mac (no field) omits it


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
# cross-process file lock (shared/filelock.py)
# ---------------------------------------------------------------------------

def test_file_lock_is_mutually_exclusive_and_reusable(tmp_path):
    """`file_lock` serializes a read-merge-write across processes: while held, another holder's
    non-blocking acquire fails; after the block, it is free again (no deadlock on re-entry)."""
    import fcntl

    from shared.filelock import file_lock

    target = tmp_path / "engines" / "n1" / "remote.json"  # parent dirs don't exist yet — lock must create them
    with file_lock(target):
        fd = os.open(str(target) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # a rival can't take it while we hold it
        finally:
            os.close(fd)
    with file_lock(target):  # released → reacquirable
        pass
    assert (target.parent / "remote.json.lock").exists()


def test_terminate_pid_sigterms_but_does_not_touch_the_record(monkeypatch, tmp_path):
    """`terminate_pid` stops a detached child (SIGTERM) without removing its record — the caller keeps
    the record for a respawn that rewrites it, unlike `stop_engine` which unlinks."""
    import signal as signal_mod

    from shared import run_records

    sent = []
    alive = {"v": True}
    monkeypatch.setattr(run_records, "pid_alive", lambda pid: alive["v"])
    monkeypatch.setattr(run_records.os, "kill", lambda pid, sig: (sent.append((pid, sig)), alive.__setitem__("v", False)))
    monkeypatch.setattr(run_records.time, "sleep", lambda s: None)

    record_file = run_records.record_path("n1", "remote")
    record_file.parent.mkdir(parents=True, exist_ok=True)
    record_file.write_text("{}")

    run_records.terminate_pid(4242)
    assert sent == [(4242, signal_mod.SIGTERM)]  # graceful stop
    assert record_file.exists()  # record left intact for the caller to rewrite


def test_terminate_pid_escalates_to_sigkill_group_when_stubborn(monkeypatch):
    """A child that ignores SIGTERM past the grace window is SIGKILL'd by process group, and
    `terminate_pid` reports False because it could not confirm the process died."""
    from shared import run_records

    killed = []
    clock = {"v": 1000.0}
    monkeypatch.setattr(run_records, "pid_alive", lambda pid: True)  # never dies on its own
    monkeypatch.setattr(run_records, "kill_group", lambda pid: killed.append(pid))
    monkeypatch.setattr(run_records.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(run_records.time, "time", lambda: clock["v"])
    monkeypatch.setattr(run_records.time, "sleep", lambda s: clock.__setitem__("v", clock["v"] + 10))

    assert run_records.terminate_pid(4242) is False  # could not confirm it died
    assert killed == [4242]


def test_terminate_pid_reports_true_when_process_exits(monkeypatch):
    from shared import run_records

    alive = {"v": True}
    monkeypatch.setattr(run_records, "pid_alive", lambda pid: alive["v"])
    monkeypatch.setattr(run_records.os, "kill", lambda pid, sig: alive.__setitem__("v", False))
    monkeypatch.setattr(run_records.time, "sleep", lambda s: None)
    assert run_records.terminate_pid(4242) is True  # SIGTERM took → confirmed dead


def test_pid_alive_treats_permission_error_as_alive(monkeypatch):
    """A process owned by another uid answers `os.kill(pid, 0)` with EPERM — it EXISTS, so it is alive.
    Reporting it dead would let a join spawn a clobbering second node under the same token id."""
    from shared import run_records

    def raise_eperm(pid, sig):
        raise PermissionError()

    monkeypatch.setattr(run_records.os, "kill", raise_eperm)
    assert run_records.pid_alive(4242) is True


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


def test_remote_engines_models_require_session_when_signed_out(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    state.set_mode("remote")  # remote mode, but not signed in

    # engines/models now have real handlers (the last GATED stubs to land): signed out they reach
    # the auth gate, not the old "remote mode yet" stub.
    for command in ("engines", "models"):
        with pytest.raises(SystemExit) as exc:
            cli.main([command])
        assert "login" in str(exc.value).lower()
        assert "remote mode yet" not in str(exc.value).lower()


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
    """Capture the detached __remote-engine spawn AND any SIGHUP hot-reload signal, and skip the real
    liveness wait. Returns a dict with 'cmd' (the spawn argv) and 'signals' (list of (pid, sig)) — the
    os.kill mock also keeps a stray SIGHUP from ever hitting a real process with the test pid."""
    spawned = {"signals": []}

    def fake_popen(cmd, **kw):
        spawned["cmd"] = cmd
        return type("P", (), {"pid": pid})()

    monkeypatch.setattr(cli.remote_provider.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cli.remote_provider, "_await_remote_engine_start", lambda *a, **k: "starting")
    monkeypatch.setattr(cli.remote_provider.os, "kill", lambda p, s: spawned["signals"].append((p, s)))
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
    record = cli.provider._read_records("n1")["remote"]  # remote is a singleton identity per grid
    assert record["endpoint_url"] == "http://192.168.1.9:11434/v1" and record["models"] == ["llama3"]
    assert record["meta_name"] == "ext"  # --name in remote is the grid-page display name, not the record key


def test_remote_join_api_unknown_kind_lists_supported(monkeypatch, tmp_path):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        cli.main(["join", "--api", "anthropic", "-m", "anthropic:claude"])
    assert "anthropic" in str(exc.value)  # the kind that was rejected
    assert "openai" in str(exc.value)  # ... and the supported kinds, so the fix is one edit away

    # `--api ""` (e.g. an unset shell variable) must error too, not silently join hardware.
    with pytest.raises(SystemExit):
        cli.main(["join", "--api", "", "-m", "openai:gpt-5.5"])
    assert cli.provider._read_records("n1") == {}


def test_remote_join_api_mutually_exclusive_flags(monkeypatch, tmp_path):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)

    conflicts = [
        (["--at", "http://h:11434/v1"], "--at"),
        (["--serve", "m"], "--serve"),
        (["--advertise-as", "alias"], "--advertise-as"),
        (["--media"], "--media"),
        (["--bundle", "image_generation"], "--bundle"),
    ]
    for extra, flag in conflicts:
        with pytest.raises(SystemExit) as exc:
            cli.main(["join", "--api", "openai", "-m", "openai:gpt-5.5", *extra])
        assert flag in str(exc.value), f"{flag} must be named in the error"
        assert "--api" in str(exc.value)
    assert cli.provider._read_records("n1") == {}


def test_remote_join_api_requires_model_flag(monkeypatch, tmp_path):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        cli.main(["join", "--api", "openai"])
    msg = str(exc.value)
    assert "-m" in msg and "grid catalog --api openai" in msg
    assert cli.provider._read_records("n1") == {}


def test_remote_join_api_rejects_inline_alias(monkeypatch, tmp_path):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        cli.main(["join", "--api", "openai", "-m", "openai:gpt-5.5=alias"])
    assert "advertise" in str(exc.value).lower() or "alias" in str(exc.value).lower()
    assert cli.provider._read_records("n1") == {}


def test_remote_join_api_model_outside_whitelist_lists_valid_names(monkeypatch, tmp_path):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    spawned = _mock_remote_spawn(monkeypatch)
    # No env key on purpose: the whitelist is static data, checked before the key or any network.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: pytest.fail("whitelist check must precede any vendor call"))

    with pytest.raises(SystemExit) as exc:
        cli.main(["join", "--api", "openai", "-m", "openai:nope"])
    msg = str(exc.value)
    assert "openai:nope" in msg
    for entry in api_catalog.WHITELISTS["openai"].entries:  # every valid name, so the fix is one edit away
        assert api_catalog.advertised_name("openai", entry) in msg
    assert cli.provider._read_records("n1") == {} and "cmd" not in spawned


def test_remote_join_api_requires_env_key(monkeypatch, tmp_path):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    spawned = _mock_remote_spawn(monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: pytest.fail("no key, no vendor call"))

    with pytest.raises(SystemExit) as exc:
        cli.main(["join", "--api", "openai", "-m", "openai:gpt-5.5"])
    assert "OPENAI_API_KEY" in str(exc.value)
    assert cli.provider._read_records("n1") == {} and "cmd" not in spawned


def _mock_vendor(monkeypatch, handler, _real=httpx.Client):
    """Serve the vendor's model-listing endpoint the `join --api` key validation calls, via
    httpx.MockTransport (the `_mock_serve_engine` pattern). Returns what the vendor saw."""
    seen = {}

    def wrapped(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return handler(request)

    monkeypatch.setattr(
        httpx, "Client",
        lambda *a, **k: _real(*a, **{**k, "transport": httpx.MockTransport(wrapped)}),
    )
    return seen


def test_remote_join_api_invalid_key_is_terminal_no_spawn(monkeypatch, tmp_path):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    spawned = _mock_remote_spawn(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    seen = _mock_vendor(monkeypatch, lambda request: httpx.Response(401, text="invalid key"))

    with pytest.raises(SystemExit) as exc:
        cli.main(["join", "--api", "openai", "-m", "openai:gpt-5.5"])
    msg = str(exc.value)
    assert "401" in msg
    assert "sk-test-123" not in msg  # the key never appears in terminal output
    assert seen["url"] == "https://api.openai.com/v1/models"
    assert seen["auth"] == "Bearer sk-test-123"
    assert cli.provider._read_records("n1") == {} and "cmd" not in spawned


def test_remote_join_api_writes_kind_generic_record_and_spawns(monkeypatch, tmp_path, capsys):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    spawned = _mock_remote_spawn(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    _mock_vendor(monkeypatch, lambda request: httpx.Response(
        200, json={"data": [{"id": "gpt-5.5"}, {"id": "gpt-5.4-mini"}]}
    ))

    assert cli.main(["join", "--api", "openai", "-m", "openai:gpt-5.5"]) == 0

    record = cli.provider._read_records("n1")["remote"]
    # Kind-generic spec: service kind + vendor base URL + advertised names — never the key.
    assert record["engines"] == [{
        "endpoint_url": "https://api.openai.com/v1",
        "models": ["openai:gpt-5.5"],
        "engine_label": "openai",
        "api_kind": "openai",
    }]
    assert record["models"] == ["openai:gpt-5.5"]
    assert record["endpoint_url"] == "https://api.openai.com/v1"
    assert spawned["cmd"][-3:] == ["__remote-engine", "n1", "remote"]
    # Secret hygiene: the key appears in no record, no terminal output.
    from shared import run_records
    record_text = run_records.record_path("n1", "remote").read_text()
    assert "sk-test-123" not in record_text
    out_err = capsys.readouterr()
    assert "sk-test-123" not in out_err.out + out_err.err


def test_remote_join_api_excludes_models_key_cannot_see(monkeypatch, tmp_path, capsys):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    # The key sees gpt-5.5 but not gpt-5.4-mini: the whitelisted-but-unavailable model is excluded.
    _mock_vendor(monkeypatch, lambda request: httpx.Response(200, json={"data": [{"id": "gpt-5.5"}]}))

    assert cli.main(["join", "--api", "openai", "-m", "openai:gpt-5.5", "-m", "openai:gpt-5.4-mini"]) == 0

    record = cli.provider._read_records("n1")["remote"]
    assert record["models"] == ["openai:gpt-5.5"]
    assert "openai:gpt-5.4-mini" in capsys.readouterr().err  # the exclusion is reported, not silent


def test_remote_join_api_all_models_unavailable_errors(monkeypatch, tmp_path):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    spawned = _mock_remote_spawn(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    _mock_vendor(monkeypatch, lambda request: httpx.Response(200, json={"data": []}))

    with pytest.raises(SystemExit) as exc:  # error rather than joining nothing
        cli.main(["join", "--api", "openai", "-m", "openai:gpt-5.5"])
    assert "openai:gpt-5.5" in str(exc.value)
    assert cli.provider._read_records("n1") == {} and "cmd" not in spawned


def test_remote_join_api_unexpected_listing_shape_is_diagnostic(monkeypatch, tmp_path):
    """A 200 whose body isn't the documented {"data": [...]} shape must be its own diagnostic
    error, not masquerade as 'your key can't see these models' (which points the operator at
    permissions instead of the vendor/proxy)."""
    _seed_running_remote_grid(monkeypatch, tmp_path)
    spawned = _mock_remote_spawn(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    _mock_vendor(monkeypatch, lambda request: httpx.Response(200, json=[{"id": "gpt-5.5"}]))

    with pytest.raises(SystemExit) as exc:
        cli.main(["join", "--api", "openai", "-m", "openai:gpt-5.5"])
    msg = str(exc.value)
    assert "shape" in msg.lower() or "unexpected" in msg.lower()
    assert "available to this" not in msg  # not the key-permissions message
    assert cli.provider._read_records("n1") == {} and "cmd" not in spawned


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


def test_remote_join_media_only_writes_media_record_and_spawns(monkeypatch, tmp_path):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    spawned = _mock_remote_spawn(monkeypatch)

    assert cli.main(["join", "--media", "--bundle", "image_generation"]) == 0

    records = cli.provider._read_records("n1")
    (engine_id, record), = records.items()
    assert record["media"] is True
    assert record["media_bundles"] == ["image_generation"]
    # A media-only join carries no text engine; the comfyui:* models are resolved from bundle gating
    # at serve time, so the record's models/endpoint stay empty.
    assert record["models"] == [] and record["endpoint_url"] is None and record["engines"] == []
    assert record["comfyui_port"] == 8188 and record["media_port"] == 8190
    assert spawned["cmd"][-3:] == ["__remote-engine", "n1", engine_id]


def test_remote_join_media_with_serve_carries_both(monkeypatch, tmp_path):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)

    assert cli.main(["join", "--serve", "m", "--media"]) == 0

    record = next(iter(cli.provider._read_records("n1").values()))
    assert record["media"] is True  # media coexists with a built-in text engine under one identity
    assert record["models"] == ["m"] and record["engines"][0]["models"] == ["m"]


def test_remote_join_detect_requires_confirmation_for_text_plus_media(monkeypatch, tmp_path):
    """A detected media engine counts toward the multi-engine prompt (like local): a bare join with
    1 text + 1 media must not silently join both — non-interactive, it asks for --all."""
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    monkeypatch.setattr(cli.provider, "_detect", lambda host: [
        detect.DetectedEngine(label="ollama", endpoint_url="http://h:11434/v1", models=["llama3"]),
        detect.DetectedEngine(label="comfyui", endpoint_url="http://h:8188", models=[], media=True),
    ])
    with pytest.raises(SystemExit) as exc:
        cli.main(["join"])
    assert "multiple engines" in str(exc.value).lower()


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


def test_remote_join_appends_via_hot_reload(monkeypatch, tmp_path):
    """A second `grid join` on a remote grid is ADDITIVE and ZERO-DROP: it merges into the one singleton
    identity and SIGHUP-hot-reloads the live process in place — no restart, no terminate (ADR 0010 D3)."""
    import signal as _sig

    _seed_running_remote_grid(monkeypatch, tmp_path)
    spawned = _mock_remote_spawn(monkeypatch)
    terminated = []
    monkeypatch.setattr(cli.remote_provider.run_records, "terminate_pid", lambda pid: terminated.append(pid) or True)

    assert cli.main(["join", "--at", "http://h:11434/v1", "-m", "llama3"]) == 0  # first join spawns
    # The first identity is now live — make its recorded pid look alive so the 2nd join appends to it.
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)
    assert cli.main(["join", "--at", "http://h:8000/v1", "-m", "mistral"]) == 0  # second appends via SIGHUP

    records = cli.provider._read_records("n1")
    assert list(records) == ["remote"]  # exactly one singleton identity per grid
    rec = records["remote"]
    assert [e["endpoint_url"] for e in rec["engines"]] == ["http://h:11434/v1", "http://h:8000/v1"]  # union
    assert rec["models"] == ["llama3", "mistral"] and rec["endpoint_url"] is None
    assert spawned["signals"] == [(4242, _sig.SIGHUP)]  # hot-reloaded the live singleton in place
    assert terminated == []                             # never stopped the live process (zero-drop)


def test_remote_join_api_append_onto_live_hardware_respawns_not_reloads(monkeypatch, tmp_path):
    """Appending an API engine must RESPAWN, not SIGHUP: the live process's environment has no
    vendor key, so a hot-reload would advertise openai:* models whose every job 401s upstream.
    The respawned process inherits the joining CLI's env — where the key was just validated."""
    import signal as _sig

    _seed_running_remote_grid(monkeypatch, tmp_path)
    spawned = _mock_remote_spawn(monkeypatch)
    terminated = []
    monkeypatch.setattr(cli.remote_provider.run_records, "terminate_pid", lambda pid: terminated.append(pid) or True)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    _mock_vendor(monkeypatch, lambda request: httpx.Response(200, json={"data": [{"id": "gpt-5.5"}]}))

    assert cli.main(["join", "--at", "http://h:11434/v1", "-m", "llama3"]) == 0  # first join spawns
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)
    assert cli.main(["join", "--api", "openai", "-m", "openai:gpt-5.5"]) == 0

    rec = cli.provider._read_records("n1")["remote"]
    assert [e["endpoint_url"] for e in rec["engines"]] == ["http://h:11434/v1", "https://api.openai.com/v1"]
    assert rec["models"] == ["llama3", "openai:gpt-5.5"]
    assert terminated == [4242]  # stopped the keyless live process...
    assert (4242, _sig.SIGHUP) not in spawned["signals"]  # ...instead of hot-reloading it


def test_remote_join_additive_preserves_max_concurrency(monkeypatch, tmp_path):
    """An additive `grid join` that doesn't re-pass --max-concurrency PRESERVES the live identity's value
    (it sizes the poll-worker pool), instead of silently resetting it to the default 1 and collapsing an
    N-worker engine to one on the next respawn/hot-reload."""
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    monkeypatch.setattr(cli.remote_provider.run_records, "terminate_pid", lambda pid: True)

    assert cli.main(["join", "--at", "http://h:11434/v1", "-m", "llama3", "--max-concurrency", "8"]) == 0
    assert cli.provider._read_records("n1")["remote"]["max_concurrency"] == 8
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)
    assert cli.main(["join", "--at", "http://h:8000/v1", "-m", "mistral"]) == 0  # no --max-concurrency re-passed

    assert cli.provider._read_records("n1")["remote"]["max_concurrency"] == 8  # preserved, not reset to 1


def test_remote_join_appending_same_url_is_noop(monkeypatch, tmp_path, capsys):
    """Re-joining an engine already in the union adds nothing and does not respawn (idempotent)."""
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    respawned = []
    monkeypatch.setattr(cli.remote_provider.run_records, "terminate_pid", lambda pid: respawned.append(pid))

    assert cli.main(["join", "--at", "http://h:11434/v1", "-m", "llama3"]) == 0
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)
    assert cli.main(["join", "--at", "http://h:11434/v1", "-m", "llama3"]) == 0  # same engine again

    rec = cli.provider._read_records("n1")["remote"]
    assert len(rec["engines"]) == 1  # deduped by endpoint_url
    assert respawned == []  # nothing new → no restart
    assert "already serving" in capsys.readouterr().out.lower()


def test_remote_join_appending_builtin_into_external_union_is_rejected(monkeypatch, tmp_path):
    """The built-in `--serve` engine serves one model and cannot join a multi-engine union (external-only)."""
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    monkeypatch.setattr(cli.remote_provider.run_records, "terminate_pid", lambda pid: None)

    assert cli.main(["join", "--at", "http://h:11434/v1", "-m", "llama3"]) == 0
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)
    with pytest.raises(SystemExit) as exc:
        cli.main(["join", "--serve", "qwen"])
    assert "leave" in str(exc.value).lower()  # guidance: leave then re-join all as --at


def test_remote_join_adopts_and_stops_legacy_live_record(monkeypatch, tmp_path):
    """Migration: a pre-singleton `engine-<uuid>` record that is still live gets its engines adopted into
    the singleton and its process stopped, so two children never share the token node_id."""
    from shared import run_records

    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    terminated = []
    monkeypatch.setattr(cli.remote_provider.run_records, "terminate_pid", lambda pid: terminated.append(pid) or True)
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)
    run_records.write_record("n1", "engine-abc123", {
        "engine_id": "engine-abc123", "grid_id": "n1", "pid": 999, "media": False,
        "engines": [{"endpoint_url": "http://legacy:11434/v1", "models": ["llama3"], "engine_label": "ollama"}],
        "models": ["llama3"], "endpoint_url": "http://legacy:11434/v1",
    })

    assert cli.main(["join", "--at", "http://new:8000/v1", "-m", "mistral"]) == 0

    records = cli.provider._read_records("n1")
    assert list(records) == ["remote"]  # legacy file removed; one singleton remains
    assert [e["endpoint_url"] for e in records["remote"]["engines"]] == [
        "http://legacy:11434/v1", "http://new:8000/v1",  # adopted legacy engine + the new one
    ]
    assert 999 in terminated  # legacy process stopped so it can't clobber the token node_id


def test_remote_join_same_url_new_model_is_added(monkeypatch, tmp_path):
    """Re-joining an engine already in the union but with an extra `-m` model ADDS the model (additive),
    rather than dedup-skipping the whole spec and silently dropping the new model."""
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    monkeypatch.setattr(cli.remote_provider.run_records, "terminate_pid", lambda pid: True)

    assert cli.main(["join", "--at", "http://h:11434/v1", "-m", "llama3"]) == 0
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)
    assert cli.main(["join", "--at", "http://h:11434/v1", "-m", "llama3", "-m", "mistral"]) == 0

    rec = cli.provider._read_records("n1")["remote"]
    assert len(rec["engines"]) == 1  # still one engine (same URL)
    assert rec["engines"][0]["models"] == ["llama3", "mistral"]  # the new model was merged in, not dropped
    assert rec["models"] == ["llama3", "mistral"]


def test_remote_join_serve_rejoin_same_model_is_noop(monkeypatch, tmp_path, capsys):
    """Re-running `grid join --serve <model>` (built-in, endpoint_url=None) with the same model is a
    no-op, not a bogus 'multiple engines' rejection."""
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    respawned = []
    monkeypatch.setattr(cli.remote_provider.run_records, "terminate_pid", lambda pid: respawned.append(pid) or True)

    assert cli.main(["join", "--serve", "qwen"]) == 0
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)
    assert cli.main(["join", "--serve", "qwen"]) == 0  # same built-in model again

    assert respawned == []  # nothing new → no restart, no SystemExit
    assert "already serving" in capsys.readouterr().out.lower()


def test_remote_join_rename_hot_reloads_meta_name(monkeypatch, tmp_path):
    """A re-join that only changes --name hot-reloads (zero-drop) so the new display name takes effect —
    the reload recomputes meta from the rewritten record (ADR 0010 D3)."""
    import signal as _sig

    _seed_running_remote_grid(monkeypatch, tmp_path)
    spawned = _mock_remote_spawn(monkeypatch)
    terminated = []
    monkeypatch.setattr(cli.remote_provider.run_records, "terminate_pid", lambda pid: terminated.append(pid) or True)

    assert cli.main(["join", "--at", "http://h:11434/v1", "-m", "llama3", "--name", "first"]) == 0
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)
    assert cli.main(["join", "--at", "http://h:11434/v1", "-m", "llama3", "--name", "second"]) == 0

    assert cli.provider._read_records("n1")["remote"]["meta_name"] == "second"  # rename applied
    assert spawned["signals"] == [(4242, _sig.SIGHUP)]  # via a zero-drop hot-reload, not a respawn
    assert terminated == []


def test_remote_join_append_onto_aliased_identity_is_rejected(monkeypatch, tmp_path):
    """Appending a 2nd engine onto a single-engine identity that uses --advertise-as is rejected (aliases
    are single-engine only, ADR 0007 D4) instead of silently dropping the alias."""
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    monkeypatch.setattr(cli.remote_provider.run_records, "terminate_pid", lambda pid: True)

    assert cli.main(["join", "--at", "http://h:11434/v1", "-m", "real", "--advertise-as", "public"]) == 0
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)
    with pytest.raises(SystemExit) as exc:
        cli.main(["join", "--at", "http://h:8000/v1", "-m", "mistral"])
    assert "advertise-as" in str(exc.value).lower()


def test_remote_join_rejoin_aliased_engine_with_new_alias_is_rejected(monkeypatch, tmp_path):
    """Re-joining ONE engine with a second -m/--advertise-as would mismatch the alias/model counts and
    crash the reload's _advertised_models (SystemExit); the CLI rejects it up front instead — aliases
    don't merge across joins (reviewer CRITICAL root cause)."""
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    monkeypatch.setattr(cli.remote_provider.run_records, "terminate_pid", lambda pid: True)

    assert cli.main(["join", "--at", "http://h:11434/v1", "-m", "m1", "--advertise-as", "a1"]) == 0
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)
    with pytest.raises(SystemExit) as exc:
        cli.main(["join", "--at", "http://h:11434/v1", "-m", "m2", "--advertise-as", "a2"])  # same engine, 2nd pair
    assert "advertise-as" in str(exc.value).lower()


def test_remote_join_aborts_when_prior_process_wont_die(monkeypatch, tmp_path):
    """On the respawn path (here a pre-handler prior that can't be SIGHUP-hot-reloaded), if the prior
    can't be confirmed stopped the join aborts BEFORE spawning — spawning anyway would put two processes
    on the same token node_id (the original clobber bug)."""
    from shared import run_records as _rr

    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    _rr.write_record("n1", "remote", {  # a pre-handler (no reload_signal) live singleton → append respawns
        "engine_id": "remote", "grid_id": "n1", "pid": 4242, "media": False, "reload_signal": None,
        "signaling_url": "https://relay.example", "meta_name": "mybox",
        "engines": [{"endpoint_url": "http://h:11434/v1", "models": ["llama3"], "engine_label": None}],
        "models": ["llama3"], "endpoint_url": "http://h:11434/v1",
    })
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)
    monkeypatch.setattr(cli.remote_provider.run_records, "terminate_pid", lambda pid: False)  # won't die
    spawns = []
    monkeypatch.setattr(cli.remote_provider, "_spawn_remote_engine",
                        lambda *a, **k: spawns.append(a) or type("P", (), {"pid": 1})())
    with pytest.raises(SystemExit) as exc:
        cli.main(["join", "--at", "http://h:8000/v1", "-m", "mistral"])
    assert "could not stop" in str(exc.value).lower()
    assert spawns == []  # never spawned the clobbering second process


def test_remote_join_pre_handler_singleton_respawns_not_sighup(monkeypatch, tmp_path):
    """C1: appending onto a live singleton with NO reload_signal (a pre-Slice-2 process with no SIGHUP
    handler) RESPAWNS — a raw SIGHUP would terminate that process and drop every in-flight request."""
    from shared import run_records as _rr

    _seed_running_remote_grid(monkeypatch, tmp_path)
    spawned = _mock_remote_spawn(monkeypatch)
    terminated = []
    monkeypatch.setattr(cli.remote_provider.run_records, "terminate_pid", lambda pid: terminated.append(pid) or True)
    _rr.write_record("n1", "remote", {  # pre-handler live singleton (no reload_signal)
        "engine_id": "remote", "grid_id": "n1", "pid": 4242, "media": False,
        "signaling_url": "https://relay.example", "meta_name": "mybox",
        "engines": [{"endpoint_url": "http://h:11434/v1", "models": ["llama3"], "engine_label": None}],
        "models": ["llama3"], "endpoint_url": "http://h:11434/v1",
    })
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)

    assert cli.main(["join", "--at", "http://h:8000/v1", "-m", "mistral"]) == 0
    assert terminated == [4242]              # respawned: the pre-handler process was stopped, not SIGHUP'd
    assert spawned["signals"] == []          # no SIGHUP to a process that can't handle it
    rec = cli.provider._read_records("n1")["remote"]
    assert [e["endpoint_url"] for e in rec["engines"]] == ["http://h:11434/v1", "http://h:8000/v1"]
    assert rec["reload_signal"] == "sighup"  # the respawned record is now Slice-2 (future joins hot-reload)


def test_remote_join_hot_reload_falls_back_to_respawn_if_pid_vanished(monkeypatch, tmp_path, capsys):
    """If the live singleton dies between the liveness check and the SIGHUP (os.kill → ProcessLookupError),
    the append falls back to a respawn so the merged record is actually served, not lost to a dead pid —
    and the CLI reports the respawn honestly, not a false 'hot-reloaded, no drops' (reviewer HIGH)."""
    from shared import run_records as _rr

    _seed_running_remote_grid(monkeypatch, tmp_path)
    spawned = _mock_remote_spawn(monkeypatch)  # provides the respawn Popen + await

    def _vanished(pid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr(cli.remote_provider.os, "kill", _vanished)  # process gone at signal time
    _rr.write_record("n1", "remote", {
        "engine_id": "remote", "grid_id": "n1", "pid": 4242, "media": False, "reload_signal": "sighup",
        "signaling_url": "https://relay.example", "meta_name": "mybox",
        "engines": [{"endpoint_url": "http://h:11434/v1", "models": ["llama3"], "engine_label": None}],
        "models": ["llama3"], "endpoint_url": "http://h:11434/v1",
    })
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)

    assert cli.main(["join", "--at", "http://h:8000/v1", "-m", "mistral"]) == 0
    rec = cli.provider._read_records("n1")["remote"]
    assert [e["endpoint_url"] for e in rec["engines"]] == ["http://h:11434/v1", "http://h:8000/v1"]  # served
    assert spawned["cmd"][-3:] == ["__remote-engine", "n1", "remote"]  # respawned fresh after the vanish
    out = capsys.readouterr().out
    assert "hot-reloaded" not in out and "starting" in out  # honest: reported the respawn, not zero-drop


def test_remote_join_media_bundle_add_respawns_not_sighup(monkeypatch, tmp_path):
    """C3: adding a media --bundle to a live media identity RESPAWNS (a new bundle needs a ComfyUI
    bring-up the hot-reload can't do), rather than SIGHUP'ing and silently dropping the new models."""
    from shared import run_records as _rr

    _seed_running_remote_grid(monkeypatch, tmp_path)
    spawned = _mock_remote_spawn(monkeypatch)
    terminated = []
    monkeypatch.setattr(cli.remote_provider.run_records, "terminate_pid", lambda pid: terminated.append(pid) or True)
    _rr.write_record("n1", "remote", {  # a live Slice-2 media identity serving one bundle
        "engine_id": "remote", "grid_id": "n1", "pid": 4242, "media": True, "reload_signal": "sighup",
        "signaling_url": "https://relay.example", "meta_name": "mybox",
        "media_bundles": ["image_generation"], "comfyui_port": 8188, "media_port": 8190,
        "engines": [], "models": [], "endpoint_url": None,
    })
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)

    assert cli.main(["join", "--media", "--bundle", "i2v"]) == 0  # add a new bundle
    assert terminated == [4242]        # respawned to bring up the new bundle
    assert spawned["signals"] == []    # not a SIGHUP hot-reload (which would drop the new bundle's models)
    rec = cli.provider._read_records("n1")["remote"]
    assert set(rec["media_bundles"]) == {"image_generation", "i2v"}


def _seed_remote_identity(monkeypatch, tmp_path, engines, *, pid=4242, media=False, reload_signal="sighup"):
    from shared import run_records

    _seed_remote(monkeypatch, tmp_path,
                networks=[{"network_id": "n1", "name": "team", "access_token": "AT"}], active="team")
    run_records.write_record("n1", "remote", {
        "engine_id": "remote", "grid_id": "n1", "pid": pid, "media": media,
        "reload_signal": reload_signal,  # a Slice-2 identity supports SIGHUP hot-reload; None = pre-handler
        "signaling_url": "https://relay.example", "meta_name": "mybox",
        "engines": engines,
        "models": list(dict.fromkeys(m for e in engines for m in e.get("models") or [])),
        "endpoint_url": engines[0]["endpoint_url"] if len(engines) == 1 else None,
    })


def test_remote_leave_tears_down_the_identity(monkeypatch, tmp_path, capsys):
    _seed_remote_identity(monkeypatch, tmp_path,
                          [{"endpoint_url": "http://h:11434/v1", "models": ["llama3"], "engine_label": "ollama"}], pid=0)

    assert cli.main(["leave"]) == 0  # bare leave tears down the whole singleton identity
    assert cli.provider._read_records("n1") == {}
    assert "Left team" in capsys.readouterr().out


def test_remote_leave_engine_shrinks_union(monkeypatch, tmp_path):
    """`grid leave --engine` drops one engine and SIGHUP-hot-reloads the reduced union in place — zero
    drop, no restart (ADR 0010 D3)."""
    import signal as _sig

    _seed_remote_identity(monkeypatch, tmp_path, [
        {"endpoint_url": "http://h:11434/v1", "models": ["llama3"], "engine_label": "ollama"},
        {"endpoint_url": "http://h:8000/v1", "models": ["mistral"], "engine_label": "vllm"},
    ])
    spawned = _mock_remote_spawn(monkeypatch)
    terminated = []
    monkeypatch.setattr(cli.remote_provider.run_records, "terminate_pid", lambda pid: terminated.append(pid) or True)
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)

    assert cli.main(["leave", "--engine", "http://h:11434/v1"]) == 0  # drop one engine by endpoint URL
    rec = cli.provider._read_records("n1")["remote"]
    assert [e["endpoint_url"] for e in rec["engines"]] == ["http://h:8000/v1"]  # only the survivor remains
    assert rec["models"] == ["mistral"] and rec["endpoint_url"] == "http://h:8000/v1"
    assert spawned["signals"] == [(4242, _sig.SIGHUP)]  # hot-reloaded the reduced union
    assert terminated == []                             # never stopped the live process


def test_remote_leave_engine_last_tears_down(monkeypatch, tmp_path, capsys):
    _seed_remote_identity(monkeypatch, tmp_path,
                          [{"endpoint_url": "http://h:11434/v1", "models": ["llama3"], "engine_label": "ollama"}], pid=0)

    assert cli.main(["leave", "--engine", "http://h:11434/v1"]) == 0  # the only engine
    assert cli.provider._read_records("n1") == {}  # last engine removed → whole identity torn down
    assert "last engine" in capsys.readouterr().out.lower()


def test_remote_leave_engine_unknown_errors(monkeypatch, tmp_path):
    _seed_remote_identity(monkeypatch, tmp_path,
                          [{"endpoint_url": "http://h:11434/v1", "models": ["llama3"], "engine_label": "ollama"}], pid=0)
    with pytest.raises(SystemExit) as exc:
        cli.main(["leave", "--engine", "http://nope:9999/v1"])
    msg = str(exc.value).lower()
    assert "no engine" in msg and "http://h:11434/v1" in msg  # lists what IS serving so the operator can pick


def test_remote_leave_engine_matches_by_served_model(monkeypatch, tmp_path):
    """`grid leave --engine <model>` drops the engine serving that model — friendlier than the full URL."""
    import signal as _sig

    _seed_remote_identity(monkeypatch, tmp_path, [
        {"endpoint_url": "http://h:11434/v1", "models": ["llama3"], "engine_label": "ollama"},
        {"endpoint_url": "http://h:8000/v1", "models": ["mistral"], "engine_label": "vllm"},
    ])
    spawned = _mock_remote_spawn(monkeypatch)
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)

    assert cli.main(["leave", "--engine", "mistral"]) == 0  # match by served model
    rec = cli.provider._read_records("n1")["remote"]
    assert [e["endpoint_url"] for e in rec["engines"]] == ["http://h:11434/v1"]  # mistral's engine dropped
    assert spawned["signals"] == [(4242, _sig.SIGHUP)]


def test_remote_leave_engine_matches_by_url_fragment(monkeypatch, tmp_path):
    """`grid leave --engine :8000` drops the engine whose URL contains that fragment (host/port)."""
    _seed_remote_identity(monkeypatch, tmp_path, [
        {"endpoint_url": "http://h:11434/v1", "models": ["llama3"], "engine_label": "ollama"},
        {"endpoint_url": "http://h:8000/v1", "models": ["mistral"], "engine_label": "vllm"},
    ])
    _mock_remote_spawn(monkeypatch)
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)

    assert cli.main(["leave", "--engine", ":8000"]) == 0  # match by port fragment
    rec = cli.provider._read_records("n1")["remote"]
    assert [e["endpoint_url"] for e in rec["engines"]] == ["http://h:11434/v1"]


def test_remote_leave_engine_ambiguous_lists_engines(monkeypatch, tmp_path):
    """An ambiguous selector (a model two engines serve) errors and lists the engines, asking for the URL."""
    _seed_remote_identity(monkeypatch, tmp_path, [
        {"endpoint_url": "http://h:1/v1", "models": ["shared"], "engine_label": "a"},
        {"endpoint_url": "http://h:2/v1", "models": ["shared"], "engine_label": "b"},
    ])
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)
    with pytest.raises(SystemExit) as exc:
        cli.main(["leave", "--engine", "shared"])
    msg = str(exc.value).lower()
    assert "several" in msg and "http://h:1/v1" in msg and "http://h:2/v1" in msg


def test_remote_leave_shrink_preserves_media(monkeypatch, tmp_path):
    """Dropping a text engine from an identity that also serves media keeps media on and hot-reloads —
    media is unchanged, so no respawn/bring-up is needed."""
    import signal as _sig

    _seed_remote_identity(monkeypatch, tmp_path, [
        {"endpoint_url": "http://h:11434/v1", "models": ["llama3"], "engine_label": "ollama"},
        {"endpoint_url": "http://h:8000/v1", "models": ["mistral"], "engine_label": "vllm"},
    ], media=True)
    spawned = _mock_remote_spawn(monkeypatch)
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)

    assert cli.main(["leave", "--engine", "http://h:11434/v1"]) == 0
    rec = cli.provider._read_records("n1")["remote"]
    assert rec["media"] is True  # media survives an engine drop
    assert [e["endpoint_url"] for e in rec["engines"]] == ["http://h:8000/v1"]
    assert spawned["signals"] == [(4242, _sig.SIGHUP)]  # hot-reloaded (media unchanged)


def test_remote_leave_shrink_reports_a_dead_respawn(monkeypatch, tmp_path):
    """On the respawn path (a pre-handler identity), a shrink whose respawn dies must surface an error,
    not print 're-serving N engine(s)' success while the grid serves nothing."""
    _seed_remote_identity(monkeypatch, tmp_path, [
        {"endpoint_url": "http://h:11434/v1", "models": ["llama3"], "engine_label": "ollama"},
        {"endpoint_url": "http://h:8000/v1", "models": ["mistral"], "engine_label": "vllm"},
    ], reload_signal=None)
    monkeypatch.setattr(cli.remote_provider.subprocess, "Popen", lambda cmd, **kw: type("P", (), {"pid": 7})())
    monkeypatch.setattr(cli.remote_provider, "_await_remote_engine_start", lambda *a, **k: "died")
    monkeypatch.setattr(cli.remote_provider.run_records, "terminate_pid", lambda pid: True)
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)

    with pytest.raises(SystemExit) as exc:
        cli.main(["leave", "--engine", "http://h:11434/v1"])
    assert "not serving" in str(exc.value).lower() or "exited" in str(exc.value).lower()


def test_remote_leave_shrink_respawn_refreshes_reload_signal(monkeypatch, tmp_path):
    """A leave-shrink respawn from a pre-handler identity stamps reload_signal so future ops hot-reload
    (self-heal, matching the join side — reviewer MEDIUM)."""
    _seed_remote_identity(monkeypatch, tmp_path, [
        {"endpoint_url": "http://h:11434/v1", "models": ["llama3"], "engine_label": "ollama"},
        {"endpoint_url": "http://h:8000/v1", "models": ["mistral"], "engine_label": "vllm"},
    ], reload_signal=None)  # pre-handler → shrink respawns rather than hot-reloads
    _mock_remote_spawn(monkeypatch)
    terminated = []
    monkeypatch.setattr(cli.remote_provider.run_records, "terminate_pid", lambda pid: terminated.append(pid) or True)
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)

    assert cli.main(["leave", "--engine", "http://h:11434/v1"]) == 0
    rec = cli.provider._read_records("n1")["remote"]
    assert rec["reload_signal"] == "sighup"   # respawn stamped it → future leave/join hot-reload
    assert terminated == [4242]               # pre-handler → respawn (not SIGHUP)


def test_remote_leave_media_engine_stops_comfyui(monkeypatch, tmp_path, capsys):
    """Remote `grid leave` on the media identity reaps ComfyUI too (same shared teardown as local).

    Remote is one identity per grid (ADR 0010), so the media engine is the singleton record keyed
    ``_REMOTE_IDENTITY``; a bare `grid leave` tears the whole identity down through the shared
    `provider._stop_engine`, which reaps a ComfyUI this engine started."""
    from cli.remote_provider import _REMOTE_IDENTITY
    from shared import run_records

    _seed_remote(monkeypatch, tmp_path,
                 networks=[{"network_id": "n1", "name": "team", "access_token": "AT"}], active="team")
    run_records.write_record("n1", _REMOTE_IDENTITY,
                             {"engine_id": _REMOTE_IDENTITY, "node_id": "node-x", "grid_id": "n1", "pid": 0,
                              "media": True, "comfyui_started": True, "comfyui_port": 8188})

    calls = []
    monkeypatch.setattr(comfyui, "stop_running", lambda port=8188: calls.append(port) or 0)
    assert cli.main(["leave"]) == 0  # bare leave tears down the singleton identity + reaps its ComfyUI
    assert calls == [8188]
    assert cli.provider._read_records("n1") == {}


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


def _ollama_handler(caps):
    """An Ollama-shaped engine: no /props, /v1/models without capabilities, /api/show declares `caps`,
    and its OpenAI endpoint ignores a forced tool_choice (replies with content, never tool_calls)."""
    def handler(request):
        path = request.url.path
        if path == "/props":
            return httpx.Response(404)  # Ollama serves no llama-server /props
        if path == "/api/show":
            return httpx.Response(200, json={"capabilities": caps})
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m"}]})  # no per-model capabilities
        if path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})
        return httpx.Response(404)
    return handler


def test_probe_tools_from_ollama_show_when_live_probe_silent(monkeypatch, tmp_path):
    """Ollama declares tool support via /api/show even though the forced-tool_choice probe stays
    silent and there is no /props — the node must still advertise tools (else the relay 400s tool calls)."""
    from remote import probe

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _mock_engine(monkeypatch, _ollama_handler(["completion", "tools"]))

    env = probe.capabilities("http://h:11434/v1", "deepseek-r1:1.5b")

    assert env["models"]["deepseek-r1:1.5b"]["features"]["tools"] is True


def test_probe_vision_from_ollama_show_capabilities(monkeypatch, tmp_path):
    from remote import probe

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _mock_engine(monkeypatch, _ollama_handler(["completion", "vision"]))

    env = probe.capabilities("http://h:11434/v1", "llava")
    entry = env["models"]["llava"]
    assert entry["features"]["vision"] is True and entry["input_modalities"] == ["text", "image"]


def test_probe_ollama_show_without_tools_stays_text_only(monkeypatch, tmp_path):
    """/api/show that omits `tools` must not fabricate tool support — a non-tool model stays tools=False."""
    from remote import probe

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _mock_engine(monkeypatch, _ollama_handler(["completion"]))

    env = probe.capabilities("http://h:11434/v1", "gemma")
    assert env["models"]["gemma"]["features"]["tools"] is False


def test_probe_tools_from_live_probe_without_props_or_show(monkeypatch, tmp_path):
    """An OpenAI engine with no /props and no /api/show (LM Studio / MLX / vLLM shape) still detects
    tools via the live forced-tool_choice probe when the engine honors it and emits a tool_call."""
    from remote import probe

    monkeypatch.setenv("GRID_HOME", str(tmp_path))

    def handler(request):
        path = request.url.path
        if path in ("/props", "/api/show"):
            return httpx.Response(404)  # neither llama.cpp nor Ollama metadata
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m"}]})
        if path.endswith("/chat/completions"):
            if json.loads(request.content).get("tools"):
                return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [{"id": "1"}]}}]})
            return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})
        return httpx.Response(404)

    _mock_engine(monkeypatch, handler)
    env = probe.capabilities("http://h:1234/v1", "m")
    assert env["models"]["m"]["features"]["tools"] is True  # live probe carries LM Studio/MLX/vLLM


def _has_image_part(body):
    """True if a chat-completions body carries an OpenAI image_url content part (the live vision probe)."""
    for msg in body.get("messages", []):
        content = msg.get("content")
        if isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") == "image_url" for part in content
        ):
            return True
    return False


def test_probe_vision_from_live_image_probe_when_engine_accepts(monkeypatch, tmp_path):
    """An OpenAI engine with no /props, no /api/show, and no /v1/models capabilities (vLLM / LM Studio /
    MLX shape) still detects vision via a live image probe: send a tiny image, and a model that ACCEPTS
    image input answers 200 → vision True. This is the only signal for engines exposing no modality
    metadata."""
    from remote import probe

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def handler(request):
        path = request.url.path
        if path in ("/props", "/api/show"):
            return httpx.Response(404)  # neither llama.cpp nor Ollama metadata
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m"}]})  # no per-model capabilities
        if path.endswith("/chat/completions"):
            if _has_image_part(json.loads(request.content)):
                seen["image_probe"] = True
                return httpx.Response(200, json={"choices": [{"message": {"content": "a pixel"}}]})
            return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})
        return httpx.Response(404)

    _mock_engine(monkeypatch, handler)
    entry = probe.capabilities("http://h:8000/v1", "m")["models"]["m"]
    assert entry["features"]["vision"] is True and entry["input_modalities"] == ["text", "image"]
    assert seen.get("image_probe") is True  # the probe actually sent an image_url part


def test_probe_vision_live_probe_rejected_stays_text_only(monkeypatch, tmp_path):
    """A text-only OpenAI engine rejects image content (4xx); the live vision probe must NOT falsely
    advertise vision — over-claiming would route image jobs to a text model and hard-fail."""
    from remote import probe

    monkeypatch.setenv("GRID_HOME", str(tmp_path))

    def handler(request):
        path = request.url.path
        if path in ("/props", "/api/show"):
            return httpx.Response(404)
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m"}]})
        if path.endswith("/chat/completions"):
            if _has_image_part(json.loads(request.content)):
                return httpx.Response(400, json={"error": {"message": "this model does not support image input"}})
            return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})
        return httpx.Response(404)

    _mock_engine(monkeypatch, handler)
    entry = probe.capabilities("http://h:8000/v1", "m")["models"]["m"]
    assert entry["features"]["vision"] is False and entry["input_modalities"] == ["text"]


def test_probe_vision_live_survives_non_dict_200_body(monkeypatch, tmp_path):
    """A 200 whose JSON body isn't an object (list/str/number) must degrade vision to False, not crash:
    `_bring_up_engines` probes every model and relies on `probe.capabilities` never raising."""
    from remote import probe

    monkeypatch.setenv("GRID_HOME", str(tmp_path))

    def handler(request):
        path = request.url.path
        if path in ("/props", "/api/show"):
            return httpx.Response(404)
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m"}]})
        if path.endswith("/chat/completions"):
            return httpx.Response(200, json=[])  # 200, but not a JSON object
        return httpx.Response(404)

    _mock_engine(monkeypatch, handler)
    entry = probe.capabilities("http://h:8000/v1", "m")["models"]["m"]  # must not raise
    assert entry["features"]["vision"] is False


def test_probe_vision_metadata_present_negative_skips_live_probe(monkeypatch, tmp_path):
    """When /props authoritatively reports vision:false, the live image probe must NOT run (nor override
    the metadata) — even if the engine would 200 an image. Guards the metadata-absence gate: a text-only
    llama.cpp/Ollama model must not pay a needless probe or risk a false positive."""
    from remote import probe

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {"image_probe": False}

    def handler(request):
        path = request.url.path
        if path == "/props":
            return httpx.Response(200, json={"chat_template_caps": {}, "modalities": {"vision": False}})
        if path == "/api/show":
            return httpx.Response(404)
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m"}]})  # no per-model capabilities
        if path.endswith("/chat/completions"):
            if _has_image_part(json.loads(request.content)):
                seen["image_probe"] = True  # a permissive engine would answer — but we must never ask
                return httpx.Response(200, json={"choices": [{"message": {"content": "a pixel"}}]})
            return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})
        return httpx.Response(404)

    _mock_engine(monkeypatch, handler)
    entry = probe.capabilities("http://h:8081/v1", "m")["models"]["m"]
    assert entry["features"]["vision"] is False and entry["input_modalities"] == ["text"]
    assert seen["image_probe"] is False  # /props answered (vision:false) → live probe gated out


def _lmstudio_v0_handler(entries):
    """LM Studio's native GET /api/v0/models shape (rich per-model metadata); other paths 404."""
    def handler(request):
        if request.url.path == "/api/v0/models":
            return httpx.Response(200, json={"data": entries, "object": "list"})
        return httpx.Response(404)
    return handler


def test_probe_lmstudio_caps_reads_vlm_and_tool_use(monkeypatch, tmp_path):
    from remote import probe

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _mock_engine(monkeypatch, _lmstudio_v0_handler([
        {"id": "google/gemma-4-e4b", "type": "vlm", "capabilities": ["tool_use"], "max_context_length": 131072},
    ]))
    assert probe._probe_lmstudio_caps("http://h:1234/v1", "google/gemma-4-e4b") == {"vision": True, "tools": True}


def test_probe_lmstudio_caps_llm_without_tool_use(monkeypatch, tmp_path):
    from remote import probe

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _mock_engine(monkeypatch, _lmstudio_v0_handler([
        {"id": "some-llm", "type": "llm", "capabilities": [], "max_context_length": 4096},
    ]))
    assert probe._probe_lmstudio_caps("http://h:1234/v1", "some-llm") == {"vision": False, "tools": False}


def test_probe_lmstudio_caps_model_not_listed_is_empty(monkeypatch, tmp_path):
    from remote import probe

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _mock_engine(monkeypatch, _lmstudio_v0_handler([{"id": "other", "type": "llm"}]))
    assert probe._probe_lmstudio_caps("http://h:1234/v1", "missing") == {}


def test_probe_lmstudio_caps_non_lmstudio_is_empty(monkeypatch, tmp_path):
    from remote import probe

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _mock_engine(monkeypatch, lambda r: httpx.Response(404))  # vLLM/llama.cpp: no /api/v0/models
    assert probe._probe_lmstudio_caps("http://h:8000/v1", "m") == {}


def test_probe_lmstudio_caps_ignores_error_body(monkeypatch, tmp_path):
    from remote import probe

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    # LM Studio 200s unknown/edge paths with a JSON error object (no `data` list) — must not be read as caps.
    _mock_engine(monkeypatch, lambda r: httpx.Response(200, json={"error": "Unexpected endpoint"}))
    assert probe._probe_lmstudio_caps("http://h:1234/v1", "m") == {}


def test_probe_props_and_models_survive_non_dict_200_body(monkeypatch, tmp_path):
    """A 200 whose JSON body is a list/null (not an object) must degrade, not raise: probe.capabilities
    must never raise (`_bring_up_engines` has no per-model try/except around it)."""
    from remote import probe

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _mock_engine(monkeypatch, lambda r: httpx.Response(200, json=[]))  # every path returns a JSON array
    assert probe._probe_props("http://h:8081/v1") == {}
    assert probe._probe_models("http://h:8081/v1", "m") == set()
    # And end-to-end: no raise, degrades to all-False text-only.
    entry = probe.capabilities("http://h:8081/v1", "m")["models"]["m"]
    assert entry["features"]["vision"] is False and entry["features"]["tools"] is False


def test_probe_props_rejects_error_body_masquerade(monkeypatch, tmp_path):
    """LM Studio 200s /props with an error object; _probe_props must return {} (not a non-empty all-False
    dict), else it masquerades as authoritative metadata and gates out other probes."""
    from remote import probe

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _mock_engine(monkeypatch, lambda r: httpx.Response(200, json={"error": "Unexpected endpoint or method."}))
    assert probe._probe_props("http://h:1234/v1") == {}


def test_probe_capabilities_lmstudio_metadata_beats_silent_inference(monkeypatch, tmp_path):
    """The real LM Studio failure→fix: /props 200s an error body, /api/show 404s, /v1/models is bare,
    /api/v0/models declares type:vlm + tool_use, and the model is a reasoning model whose forced-tool
    inference probe emits NO tool_call. Deterministic metadata must win → vision + tools both True."""
    from remote import probe

    monkeypatch.setenv("GRID_HOME", str(tmp_path))

    def handler(request):
        path = request.url.path
        if path == "/props":
            return httpx.Response(200, json={"error": "Unexpected endpoint or method. (GET /props)"})
        if path == "/api/show":
            return httpx.Response(404)
        if path == "/api/v0/models":
            return httpx.Response(200, json={"data": [
                {"id": "google/gemma-4-e4b", "type": "vlm", "capabilities": ["tool_use"], "max_context_length": 131072},
            ]})
        if path.endswith("/models"):  # /v1/models — bare, no per-model capabilities
            return httpx.Response(200, json={"data": [{"id": "google/gemma-4-e4b"}]})
        if path.endswith("/chat/completions"):  # reasoning model: empty completion, no tool_call
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": ""}}]})
        return httpx.Response(404)

    _mock_engine(monkeypatch, handler)
    f = probe.capabilities("http://h:1234/v1", "google/gemma-4-e4b")["models"]["google/gemma-4-e4b"]["features"]
    assert f["vision"] is True and f["tools"] is True


def test_probe_tools_metadata_skips_inference_probe(monkeypatch, tmp_path):
    """When a metadata source declares tools, the forced-tool /chat/completions inference probe must not
    be sent (it's the flaky, reasoning-fragile fallback)."""
    from remote import probe

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {"tool_probe": False}

    def handler(request):
        path = request.url.path
        if path == "/api/v0/models":
            return httpx.Response(200, json={"data": [{"id": "m", "type": "llm", "capabilities": ["tool_use"]}]})
        if path in ("/props", "/api/show"):
            return httpx.Response(404)
        if path.endswith("/chat/completions"):
            if json.loads(request.content).get("tools"):
                seen["tool_probe"] = True
            return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m"}]})
        return httpx.Response(404)

    _mock_engine(monkeypatch, handler)
    f = probe.capabilities("http://h:1234/v1", "m")["models"]["m"]["features"]
    assert f["tools"] is True
    assert seen["tool_probe"] is False  # metadata answered → no inference tool-probe


def test_probe_tools_inference_uses_high_max_tokens(monkeypatch, tmp_path):
    """No metadata → the forced-tool inference probe runs, and must request enough tokens for a reasoning
    model to still emit the tool_call after thinking (LM Studio gemma needed >=500)."""
    from remote import probe

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def handler(request):
        path = request.url.path
        if path in ("/props", "/api/show", "/api/v0/models"):
            return httpx.Response(404)
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m"}]})
        if path.endswith("/chat/completions"):
            body = json.loads(request.content)
            if body.get("tools"):
                seen["tool_max_tokens"] = body.get("max_tokens")
                return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [{"id": "1"}]}}]})
            return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})
        return httpx.Response(404)

    _mock_engine(monkeypatch, handler)
    probe.capabilities("http://h:8000/v1", "m")
    assert seen.get("tool_max_tokens", 0) >= 500


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
    from shared.system import host

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    # Pin the heartbeat's platform tag so load-shape assertions are deterministic across runner OSes.
    monkeypatch.setattr(host, "platform_kind", lambda: "linux")
    kwargs = dict(
        signaling_url="https://relay.example", node_id="node-1", network_id="n1",
        llm_url="http://127.0.0.1:8081/v1", access_token="AT", refresh_token="RT",
        models=["m"], capabilities={"schema_version": 1, "models": {}},
        meta={"name": "e1", "engine": "llama.cpp"}, pricing={}, max_concurrency=1,
    )
    kwargs.update(overrides)
    return serve._ServeState(**kwargs)


def test_serve_loop_runs_one_poll_worker_per_slot(monkeypatch, tmp_path):
    """Each concurrency slot gets its own poll worker, so up to max_concurrency jobs run at once."""
    from remote import serve

    calls: list[str] = []

    def fake_poll(state):
        calls.append(threading.current_thread().name)
        state.stop.set()  # release the main-thread waiter so _serve_loop returns

    monkeypatch.setattr(serve, "_poll_loop", fake_poll)
    monkeypatch.setattr(serve, "_heartbeat_loop", lambda state: None)
    state = _serve_state(monkeypatch, tmp_path, max_concurrency=4)

    serve._serve_loop(state)

    assert len(calls) == 4


def test_serve_loop_single_worker_by_default(monkeypatch, tmp_path):
    """Default max_concurrency=1 runs exactly one poll worker — the pre-fix behavior, unchanged."""
    from remote import serve

    calls: list[str] = []

    def fake_poll(state):
        calls.append(threading.current_thread().name)
        state.stop.set()

    monkeypatch.setattr(serve, "_poll_loop", fake_poll)
    monkeypatch.setattr(serve, "_heartbeat_loop", lambda state: None)
    state = _serve_state(monkeypatch, tmp_path, max_concurrency=1)

    serve._serve_loop(state)

    assert len(calls) == 1


def test_serve_loop_starts_heartbeat_once(monkeypatch, tmp_path):
    """One heartbeat thread regardless of how many poll workers run."""
    from remote import serve

    heartbeats: list[str] = []
    monkeypatch.setattr(serve, "_poll_loop", lambda state: state.stop.set())
    monkeypatch.setattr(serve, "_heartbeat_loop", lambda state: heartbeats.append("hb"))
    state = _serve_state(monkeypatch, tmp_path, max_concurrency=3)

    serve._serve_loop(state)

    assert heartbeats == ["hb"]


def test_serve_loop_teardown_bounded_by_drain_timeout(monkeypatch, tmp_path, capsys):
    """Shutdown is bounded by one shared drain deadline, not summed per parked worker: workers stuck
    in a long-poll must not serialize teardown (state.stop can't wake a blocking relay.poll)."""
    from remote import serve

    monkeypatch.setattr(serve, "_DRAIN_TIMEOUT", 0.2)
    parked = threading.Event()  # never set during drain → workers stay "in a long-poll"

    def stuck_poll(state):
        state.stop.set()   # trigger shutdown (release the main-thread waiter)
        parked.wait(30)    # then block, ignoring state.stop, like a real relay.poll

    monkeypatch.setattr(serve, "_poll_loop", stuck_poll)
    monkeypatch.setattr(serve, "_heartbeat_loop", lambda state: parked.wait(30))
    state = _serve_state(monkeypatch, tmp_path, max_concurrency=8)

    start = time.monotonic()
    try:
        serve._serve_loop(state)
        elapsed = time.monotonic() - start
    finally:
        parked.set()  # release the daemon workers so they don't linger across the suite

    # One shared 0.2s deadline → ~0.2s total; a per-worker join would be ~8 × 0.2 = 1.6s.
    assert elapsed < 1.0
    # Workers still in flight at the deadline are logged, not dropped silently.
    assert "still in flight after" in capsys.readouterr().err


def test_serve_state_inflight_counter_is_thread_safe(monkeypatch, tmp_path):
    """enter/exit_inference must be atomic — the N poll workers now hit the counter concurrently."""
    from shared.system import gpu

    monkeypatch.setattr(gpu, "load_snapshot", lambda timeout=3.0: {})  # no-GPU box: load is just active_tasks + platform
    state = _serve_state(monkeypatch, tmp_path, max_concurrency=8)

    def hammer():
        for _ in range(1000):
            state.enter_inference()
            state.exit_inference()

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert state.load() == {"active_tasks": 0, "platform": "linux"}


def test_serve_loop_worker_death_stops_engine(monkeypatch, tmp_path):
    """A poll worker dying from an unexpected fault stops the whole engine (sets stop) instead of
    silently vanishing and stranding the node at reduced/zero capacity."""
    from remote import serve

    def boom(state):
        raise RuntimeError("relay handed us garbage")

    monkeypatch.setattr(serve, "_poll_loop", boom)
    monkeypatch.setattr(serve, "_heartbeat_loop", lambda state: state.stop.wait(60))
    state = _serve_state(monkeypatch, tmp_path, max_concurrency=3)

    serve._serve_loop(state)  # returns only because a dead worker set state.stop

    assert state.stop.is_set()


def test_serve_loop_drain_lets_inflight_job_finish(monkeypatch, tmp_path):
    """A job that finishes within the drain budget submits its result before _serve_loop returns —
    'drain', not 'kill on stop'."""
    from remote import serve

    submitted: list[str] = []

    def fake_poll(state):
        state.stop.set()          # shutdown begins while this 'job' is mid-flight
        time.sleep(0.05)          # in-flight work, well within _DRAIN_TIMEOUT
        submitted.append("done")  # result submitted before the worker returns

    monkeypatch.setattr(serve, "_poll_loop", fake_poll)
    monkeypatch.setattr(serve, "_heartbeat_loop", lambda state: None)
    state = _serve_state(monkeypatch, tmp_path, max_concurrency=1)

    serve._serve_loop(state)

    assert submitted == ["done"]


def test_relay_poll_malformed_body_raises_relay_error(monkeypatch):
    """A 200 with a non-JSON body is a transient relay fault → RelayError (retryable by _poll_loop),
    not a raw JSONDecodeError that would escape the loop's guards and kill the worker."""
    from remote import relay

    _mock_serve_engine(monkeypatch, lambda r: httpx.Response(200, content=b"<html>gateway hiccup</html>"))
    with pytest.raises(relay.RelayError):
        relay.poll("https://relay.example", "AT")


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


def test_serve_register_once_sends_cached_payload(monkeypatch, tmp_path):
    from remote import relay, serve

    state = _serve_state(monkeypatch, tmp_path,
                         capabilities={"schema_version": 1, "models": {"m": {}}}, max_concurrency=3)
    seen = {}
    monkeypatch.setattr(relay, "register_node", lambda url, tok, node, **kw: seen.update(url=url, tok=tok, node=node, **kw))

    serve.register_once(state)
    assert (seen["url"], seen["tok"], seen["node"]) == ("https://relay.example", "AT", "node-1")
    assert seen["models"] == ["m"] and seen["max_concurrency"] == 3
    assert seen["capabilities"] == {"schema_version": 1, "models": {"m": {}}}


def test_serve_register_once_refreshes_then_retries_on_401(monkeypatch, tmp_path):
    """A reload/re-register that lands at token expiry refreshes + retries once (like poll/heartbeat),
    so it re-advertises instead of silently leaving the old union registered."""
    from remote import control_plane, credentials, relay, serve

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    credentials.save_credentials({"networks": [{"network_id": "n1", "access_token": "AT", "refresh_token": "RT"}]})
    state = _serve_state(monkeypatch, tmp_path, access_token="AT", refresh_token="RT")
    monkeypatch.setattr(control_plane, "refresh_network_token",
                        lambda *, network_id, refresh_token, api_url=None: {"access_token": "AT2", "refresh_token": "RT2"})
    tokens = []

    def fake_register(url, tok, node, **kw):
        tokens.append(tok)
        if len(tokens) == 1:
            raise relay.RelayUnauthorized()

    monkeypatch.setattr(relay, "register_node", fake_register)
    serve.register_once(state)
    assert tokens == ["AT", "AT2"]  # retried with the refreshed token


def test_serve_register_once_holds_register_lock_during_put(monkeypatch, tmp_path):
    """The PUT runs under _register_lock so the reload's register and the heartbeat-404 re-register
    can never interleave two PUTs on the one token-pinned node_id (ADR 0010 D4 F5)."""
    from remote import relay, serve

    state = _serve_state(monkeypatch, tmp_path)
    held = []
    monkeypatch.setattr(relay, "register_node", lambda *a, **k: held.append(state._register_lock.locked()))
    serve.register_once(state)
    assert held == [True]


def test_serve_apply_swaps_snapshot_atomically(monkeypatch, tmp_path):
    """apply() rebinds one immutable snapshot: readers see the new routing union, and a snapshot
    captured before the swap stays frozen-intact (F4 — the swap never mutates a published snapshot)."""
    from remote import serve

    state = _serve_state(monkeypatch, tmp_path, models=["a"], routes={"a": "http://e1/v1"})
    before = state.snapshot()
    assert state.route("a") == "http://e1/v1" and state.models == ["a"]

    new = serve._Snapshot.build(
        routes={"a": "http://e1/v1", "b": "http://e2/v1"}, upstream={},
        models=["a", "b"], capabilities={"schema_version": 1, "models": {}},
        meta={"name": "e1", "engine": "external"}, pricing={}, max_concurrency=1,
    )
    state.apply(new, [("http://e1/v1", ["a"], ["a"], {}), ("http://e2/v1", ["b"], ["b"], {})])

    assert state.route("b") == "http://e2/v1"          # the appended engine is now routable
    assert state.models == ["a", "b"]                  # the property reflects the swapped snapshot
    assert before.routes == {"a": "http://e1/v1"}      # the captured old snapshot is unchanged


def _seed_reload_state(monkeypatch, tmp_path, engines, *, retained, media_models=None,
                       record_media=False, record_bundles=None, startup_bundles=None):
    """A ``_ServeState`` wired for a ``_reload_once`` test: ``GRID_HOME`` set, the singleton record
    written with ``engines``, and the retained probe results + media state the reload reads. ``retained``
    is the ``[(url, advertised, upstream, caps), ...]`` the live snapshot was built from; the state's
    ``media_signature`` reflects the STARTUP media config (``startup_bundles``), which the reload
    compares the re-read record against."""
    from remote import serve
    from shared import run_records

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    routes, upstream, models, caps, _warn = (
        serve._build_routing([tuple(r) for r in retained]) if retained else ({}, {}, [], {}, [])
    )
    state = _serve_state(monkeypatch, tmp_path, models=models, routes=routes,
                         upstream=upstream, capabilities=caps)
    state._engine_results = [tuple(r) for r in retained]
    state.media_models = list(media_models or [])
    state.media_signature = serve._media_signature(
        {"media": record_media, "media_bundles": list(startup_bundles or [])})
    run_records.write_record("n1", "remote", {
        "engine_id": "remote", "grid_id": "n1", "media": record_media,
        "media_bundles": list(record_bundles if record_bundles is not None else (startup_bundles or [])),
        "engines": engines,
    })
    return state


def test_serve_reload_probes_only_new_engine_and_registers_union(monkeypatch, tmp_path):
    """A hot-reload reuses retained caps for engines already serving and probes ONLY newly-added --at
    endpoints, then re-registers the full union after the swap (ADR 0010 D3 / D4 F6)."""
    from remote import probe, relay, serve

    state = _seed_reload_state(monkeypatch, tmp_path, retained=[
        ("http://e1/v1", ["a"], ["a"], {"schema_version": 1, "models": {"a": {}}}),
    ], engines=[
        {"endpoint_url": "http://e1/v1", "models": ["a"], "engine_label": None},
        {"endpoint_url": "http://e2/v1", "models": ["b"], "engine_label": None},
    ])
    probed = []
    monkeypatch.setattr(probe, "capabilities",
                        lambda url, model, **kw: probed.append((url, model)) or {"schema_version": 1, "models": {model: {}}})
    seen = {}
    monkeypatch.setattr(relay, "register_node", lambda url, tok, node, **kw: seen.update(kw))

    serve._reload_once(state, "remote")

    assert probed == [("http://e2/v1", "b")]            # only the new engine probed; A reused from retained
    assert state.route("a") == "http://e1/v1" and state.route("b") == "http://e2/v1"
    assert seen["models"] == ["a", "b"]                 # re-registered the full union after the swap


def test_serve_reload_probes_every_model_of_new_multi_model_engine(monkeypatch, tmp_path):
    """A hot-reload of a newly-added MULTI-model --at engine probes EVERY model it serves, not just the
    first, so all of them carry a capability envelope — the shared `_probe_spec_caps` keeps startup and
    reload in step (main's 5078c8c fix; without it models 2..N would advertise no caps silently)."""
    from remote import probe, relay, serve

    state = _seed_reload_state(monkeypatch, tmp_path, retained=[
        ("http://e1/v1", ["a"], ["a"], {"schema_version": 1, "models": {"a": {}}}),
    ], engines=[
        {"endpoint_url": "http://e1/v1", "models": ["a"], "engine_label": None},
        {"endpoint_url": "http://e2/v1", "models": ["b", "c"], "engine_label": None},
    ])
    probed = []
    monkeypatch.setattr(probe, "capabilities",
                        lambda url, model, **kw: probed.append((url, model)) or {"schema_version": 1, "models": {model: {}}})
    seen = {}
    monkeypatch.setattr(relay, "register_node", lambda url, tok, node, **kw: seen.update(kw))

    serve._reload_once(state, "remote")

    assert probed == [("http://e2/v1", "b"), ("http://e2/v1", "c")]  # BOTH models of the new engine probed
    assert seen["models"] == ["a", "b", "c"]                         # full union advertised
    assert set(seen["capabilities"]["models"]) == {"a", "b", "c"}    # every model has caps, not just the first


def test_serve_reload_appended_model_kept_no_reprobe(monkeypatch, tmp_path):
    """Re-joining an engine already serving [a] with an extra model b keeps the union [a,b] and does NOT
    re-probe it — advertised/upstream come from the record, only the caps are reused (ADR 0010 C2)."""
    from remote import probe, relay, serve

    state = _seed_reload_state(monkeypatch, tmp_path, retained=[
        ("http://e1/v1", ["a"], ["a"], {"schema_version": 1, "models": {"a": {}}}),
    ], engines=[
        {"endpoint_url": "http://e1/v1", "models": ["a", "b"], "engine_label": None},
    ])
    probed = []
    monkeypatch.setattr(probe, "capabilities", lambda url, model, **kw: probed.append(url) or {})
    seen = {}
    monkeypatch.setattr(relay, "register_node", lambda url, tok, node, **kw: seen.update(kw))

    serve._reload_once(state, "remote")

    assert probed == []                                 # same engine, first model unchanged → no re-probe
    assert seen["models"] == ["a", "b"]                 # the appended model is served, not dropped
    assert state.route("b") == "http://e1/v1"


def test_serve_reload_new_api_spec_static_caps_and_vendor_upstream(monkeypatch, tmp_path):
    """A hot-reload that gains an API spec must take its caps from the static whitelist (no probe
    ever targets the vendor) and derive vendor upstream names — the same `_probe_spec_caps` seam as
    startup, so the two can't drift. (Reachable via leave-shrink; a NEW api join respawns instead,
    for the env key.)"""
    from remote import probe, relay, serve

    state = _seed_reload_state(monkeypatch, tmp_path, retained=[
        ("http://e1/v1", ["a"], ["a"], {"schema_version": 1, "models": {"a": {}}}),
    ], engines=[
        {"endpoint_url": "http://e1/v1", "models": ["a"], "engine_label": None},
        {"endpoint_url": "https://api.openai.com/v1", "models": ["openai:gpt-5.5"],
         "engine_label": "openai", "api_kind": "openai"},
    ])
    monkeypatch.setattr(probe, "capabilities",
                        lambda *a, **k: pytest.fail("api engines are never probed, reload included"))
    seen = {}
    monkeypatch.setattr(relay, "register_node", lambda url, tok, node, **kw: seen.update(kw))

    serve._reload_once(state, "remote")

    assert seen["models"] == ["a", "openai:gpt-5.5"]
    assert seen["capabilities"]["models"]["openai:gpt-5.5"]["context_window"] == 1_050_000
    assert state.upstream_model("openai:gpt-5.5") == "gpt-5.5"  # vendor name, not the advertised name
    assert state.route("openai:gpt-5.5") == "https://api.openai.com/v1"


def test_serve_reload_retained_api_spec_keeps_vendor_upstream(monkeypatch, tmp_path):
    """A reload triggered by an unrelated append must not break a LIVE api engine's rewrite: the
    retained branch rebuilds upstream from the record's advertised names, which for an api spec must
    re-derive the vendor names — and still never probe the vendor."""
    from remote import probe, relay, serve

    state = _seed_reload_state(monkeypatch, tmp_path, retained=[
        ("https://api.openai.com/v1", ["openai:gpt-5.5"], ["gpt-5.5"],
         {"schema_version": 1, "models": {"openai:gpt-5.5": {"context_window": 1_050_000}}}),
    ], engines=[
        {"endpoint_url": "https://api.openai.com/v1", "models": ["openai:gpt-5.5"],
         "engine_label": "openai", "api_kind": "openai"},
        {"endpoint_url": "http://e2/v1", "models": ["b"], "engine_label": None},
    ])
    probed = []
    monkeypatch.setattr(probe, "capabilities",
                        lambda url, model, **kw: probed.append((url, model))
                        or {"schema_version": 1, "models": {model: {}}})
    seen = {}
    monkeypatch.setattr(relay, "register_node", lambda url, tok, node, **kw: seen.update(kw))

    serve._reload_once(state, "remote")

    assert probed == [("http://e2/v1", "b")]                    # only the new hardware engine probed
    assert state.upstream_model("openai:gpt-5.5") == "gpt-5.5"  # the live api rewrite survives the reload
    assert seen["models"] == ["openai:gpt-5.5", "b"]


def test_serve_reload_drops_removed_engine(monkeypatch, tmp_path):
    """Leave-shrink: dropping an engine from the record removes it from the union on the next reload."""
    from remote import probe, relay, serve

    state = _seed_reload_state(monkeypatch, tmp_path, retained=[
        ("http://e1/v1", ["a"], ["a"], {"schema_version": 1, "models": {"a": {}}}),
        ("http://e2/v1", ["b"], ["b"], {"schema_version": 1, "models": {"b": {}}}),
    ], engines=[
        {"endpoint_url": "http://e1/v1", "models": ["a"], "engine_label": None},
    ])
    monkeypatch.setattr(probe, "capabilities", lambda *a, **k: pytest.fail("a pure drop must not probe"))
    seen = {}
    monkeypatch.setattr(relay, "register_node", lambda url, tok, node, **kw: seen.update(kw))

    serve._reload_once(state, "remote")

    assert seen["models"] == ["a"]                      # b's engine dropped from the advertised union
    assert state.snapshot().routes == {"a": "http://e1/v1"}  # e2 no longer routed


def test_serve_reload_refuses_builtin_spec(monkeypatch, tmp_path):
    """A record needing a built-in launch (a spec with no endpoint_url) is refused; the reload never
    launches — old routing intact, nothing registered (ADR 0010 D4 F6)."""
    from remote import probe, relay, serve

    state = _seed_reload_state(monkeypatch, tmp_path, retained=[
        ("http://e1/v1", ["a"], ["a"], {"schema_version": 1, "models": {"a": {}}}),
    ], engines=[
        {"endpoint_url": "http://e1/v1", "models": ["a"], "engine_label": None},
        {"endpoint_url": None, "models": ["builtin"], "engine_label": None},
    ])
    monkeypatch.setattr(probe, "capabilities", lambda *a, **k: pytest.fail("must not probe when refusing"))
    monkeypatch.setattr(relay, "register_node", lambda *a, **k: pytest.fail("must not register when refusing"))
    before = state.snapshot()

    serve._reload_once(state, "remote")
    assert state.snapshot() is before                  # refused: the live snapshot is untouched


def test_serve_reload_refuses_media_bundle_change(monkeypatch, tmp_path):
    """A reload whose record grew media_bundles needs a ComfyUI bring-up the reload can't do → refused,
    old snapshot intact (ADR 0010 C3)."""
    from remote import relay, serve

    state = _seed_reload_state(monkeypatch, tmp_path, retained=[
        ("http://e1/v1", ["a"], ["a"], {"schema_version": 1, "models": {"a": {}}}),
    ], media_models=["comfyui:image_generation"], record_media=True,
        startup_bundles=["image_generation"], record_bundles=["image_generation", "video_generation"],
        engines=[{"endpoint_url": "http://e1/v1", "models": ["a"], "engine_label": None}])
    monkeypatch.setattr(relay, "register_node", lambda *a, **k: pytest.fail("must not register on a media change"))
    before = state.snapshot()

    serve._reload_once(state, "remote")
    assert state.snapshot() is before                  # refused: respawn required for a media change


def test_serve_reload_preserves_media_models_and_caps(monkeypatch, tmp_path):
    """A reload that adds a text engine keeps the identity's media (comfyui:*) models + caps, appended
    after the text models."""
    from remote import probe, relay, serve
    from shared.media import media_gating

    state = _seed_reload_state(monkeypatch, tmp_path, retained=[
        ("http://e1/v1", ["a"], ["a"], {"schema_version": 1, "models": {"a": {}}}),
    ], media_models=["comfyui:image_generation"], record_media=True, startup_bundles=["image_generation"],
        engines=[
            {"endpoint_url": "http://e1/v1", "models": ["a"], "engine_label": None},
            {"endpoint_url": "http://e2/v1", "models": ["b"], "engine_label": None},
        ])
    monkeypatch.setattr(probe, "capabilities", lambda url, model, **kw: {"schema_version": 1, "models": {model: {}}})
    seen = {}
    monkeypatch.setattr(relay, "register_node", lambda url, tok, node, **kw: seen.update(kw))

    serve._reload_once(state, "remote")

    assert seen["models"] == ["a", "b", "comfyui:image_generation"]  # media last, preserved across reload
    assert seen["capabilities"]["models"]["comfyui:image_generation"] == media_gating.capability_entry()


def test_serve_reload_skips_when_record_missing(monkeypatch, tmp_path):
    """A concurrent full `grid leave` removes the record mid-reload → keep the current snapshot, don't
    register (SIGTERM tears the process down)."""
    from remote import relay, serve

    state = _seed_reload_state(monkeypatch, tmp_path, retained=[
        ("http://e1/v1", ["a"], ["a"], {"schema_version": 1, "models": {"a": {}}}),
    ], engines=[{"endpoint_url": "http://e1/v1", "models": ["a"], "engine_label": None}])
    monkeypatch.setattr(serve.run_records, "read_record", lambda net, eid: None)
    monkeypatch.setattr(relay, "register_node", lambda *a, **k: pytest.fail("must not register when record gone"))
    before = state.snapshot()

    serve._reload_once(state, "remote")
    assert state.snapshot() is before


def test_serve_reload_loop_runs_reload_then_clears(monkeypatch, tmp_path):
    """The reload loop wakes on a set reload_requested, clears it, and runs one reload."""
    from remote import serve

    state = _serve_state(monkeypatch, tmp_path)
    calls = []

    def fake_reload(s, eid):
        calls.append(eid)
        s.stop.set()  # stop after the first reload so the loop exits

    monkeypatch.setattr(serve, "_reload_once", fake_reload)
    state.reload_requested.set()
    serve._reload_loop(state, "remote")

    assert calls == ["remote"]                    # ran the reload once
    assert not state.reload_requested.is_set()    # cleared after handling


def test_serve_reload_loop_stops_when_stop_set(monkeypatch, tmp_path):
    """A stopped state exits the reload loop immediately without reloading."""
    from remote import serve

    state = _serve_state(monkeypatch, tmp_path)
    monkeypatch.setattr(serve, "_reload_once", lambda *a: pytest.fail("must not reload once stopped"))
    state.stop.set()
    serve._reload_loop(state, "remote")  # returns immediately


def test_serve_reload_loop_coalesces_resignal(monkeypatch, tmp_path):
    """A write+SIGHUP that lands while a reload is running re-sets the event and is picked up by one more
    reload — never lost (clear-before-read coalescing)."""
    from remote import serve

    state = _serve_state(monkeypatch, tmp_path)
    calls = []

    def fake_reload(s, eid):
        calls.append(eid)
        if len(calls) == 1:
            s.reload_requested.set()   # a signal arrives during the first reload
        else:
            s.stop.set()               # the second reload handles it, then stop

    monkeypatch.setattr(serve, "_reload_once", fake_reload)
    state.reload_requested.set()
    serve._reload_loop(state, "remote")

    assert calls == ["remote", "remote"]           # the re-signal triggered exactly one more reload


def test_serve_reload_loop_survives_a_systemexit_reload(monkeypatch, tmp_path):
    """A reload that raises SystemExit (jsonio.load_json on a corrupt record, or _advertised_models on an
    alias/model mismatch) must NOT kill the watcher thread — SystemExit is a BaseException, so the loop
    catches (Exception, SystemExit) (ADR 0010 D4 F6, reviewer CRITICAL)."""
    from remote import serve

    state = _serve_state(monkeypatch, tmp_path)
    calls = []

    def flaky_reload(s, eid):
        calls.append(eid)
        if len(calls) == 1:
            s.reload_requested.set()    # keep the loop awake, then raise SystemExit
            raise SystemExit("--advertise-as must be provided once for each model.")
        s.stop.set()                    # the loop survived to a second reload; now stop

    monkeypatch.setattr(serve, "_reload_once", flaky_reload)
    state.reload_requested.set()
    serve._reload_loop(state, "remote")   # must not propagate the SystemExit / kill the thread
    assert calls == ["remote", "remote"]  # kept looping after the first reload raised SystemExit


def test_serve_reload_rearms_on_post_swap_register_failure(monkeypatch, tmp_path):
    """Swap succeeds but the re-register fails (transient) → the reload re-arms reload_requested so a
    later tick retries; the new union is not left unadvertised (ADR 0010 C5)."""
    from remote import probe, relay, serve

    state = _seed_reload_state(monkeypatch, tmp_path, retained=[
        ("http://e1/v1", ["a"], ["a"], {"schema_version": 1, "models": {"a": {}}}),
    ], engines=[
        {"endpoint_url": "http://e1/v1", "models": ["a"], "engine_label": None},
        {"endpoint_url": "http://e2/v1", "models": ["b"], "engine_label": None},
    ])
    monkeypatch.setattr(probe, "capabilities", lambda url, model, **kw: {"schema_version": 1, "models": {model: {}}})

    def boom(*a, **k):
        raise relay.RelayError("relay 503")

    monkeypatch.setattr(relay, "register_node", boom)
    monkeypatch.setattr(state.stop, "wait", lambda t: None)  # skip the back-off in the test

    serve._reload_once(state, "remote")

    assert state.route("b") == "http://e2/v1"   # the swap happened — the new union serves locally
    assert state.reload_requested.is_set()      # re-armed to retry the re-register


def test_serve_reload_does_not_rearm_on_exhausted_auth(monkeypatch, tmp_path):
    """A post-swap re-register that fails auth with refresh exhausted does NOT re-arm — the heartbeat loop
    stops the process on the same condition, so re-arming would just spin (reviewer HIGH)."""
    from remote import probe, relay, serve

    state = _seed_reload_state(monkeypatch, tmp_path, retained=[
        ("http://e1/v1", ["a"], ["a"], {"schema_version": 1, "models": {"a": {}}}),
    ], engines=[
        {"endpoint_url": "http://e1/v1", "models": ["a"], "engine_label": None},
        {"endpoint_url": "http://e2/v1", "models": ["b"], "engine_label": None},
    ])
    monkeypatch.setattr(probe, "capabilities", lambda url, model, **kw: {"schema_version": 1, "models": {model: {}}})

    def unauth(*a, **k):
        raise relay.RelayUnauthorized()

    monkeypatch.setattr(relay, "register_node", unauth)
    monkeypatch.setattr(state, "refresh", lambda stale_token=None: False)  # refresh exhausted

    serve._reload_once(state, "remote")

    assert state.route("b") == "http://e2/v1"   # the swap still happened (serves locally)
    assert not state.reload_requested.is_set()  # but did NOT re-arm on exhausted auth


def test_serve_reload_failing_probe_keeps_old_routing(monkeypatch, tmp_path):
    """If probing a newly-added --at engine raises, the reload aborts BEFORE the swap — the old snapshot
    keeps serving and nothing is registered; the loop's guard then logs and survives (ADR 0010 D4 F6)."""
    from remote import probe, relay, serve

    state = _seed_reload_state(monkeypatch, tmp_path, retained=[
        ("http://e1/v1", ["a"], ["a"], {"schema_version": 1, "models": {"a": {}}}),
    ], engines=[
        {"endpoint_url": "http://e1/v1", "models": ["a"], "engine_label": None},
        {"endpoint_url": "http://e2/v1", "models": ["b"], "engine_label": None},
    ])

    def boom(*a, **k):
        raise RuntimeError("probe timeout")

    monkeypatch.setattr(probe, "capabilities", boom)
    monkeypatch.setattr(relay, "register_node", lambda *a, **k: pytest.fail("must not register on a failed probe"))
    before = state.snapshot()

    with pytest.raises(RuntimeError):
        serve._reload_once(state, "remote")     # propagates; _reload_loop's guard catches it in production
    assert state.snapshot() is before           # no partial apply — the old routing is untouched


def test_serve_route_survives_concurrent_swap(monkeypatch, tmp_path):
    """route() binds the snapshot once, so a reload swapping mid-call never raises or returns a torn
    result; and published snapshots' dicts are never mutated in place (ADR 0010 D4 F4)."""
    import threading as _t

    from remote import serve

    state = _serve_state(monkeypatch, tmp_path, models=["a"], routes={"a": "http://e1/v1"}, upstream={})
    snap_a = state.snapshot()
    snap_b = serve._Snapshot.build(routes={"a": "http://e1/v1", "b": "http://e2/v1"}, upstream={},
                                   models=["a", "b"], capabilities={}, meta={}, pricing={}, max_concurrency=1)
    errors = []
    iters = 5000  # fixed count → deterministic, can't hang

    def swapper():
        for i in range(iters):
            state.apply(snap_b if i % 2 else snap_a, [])

    def router():
        for _ in range(iters):
            try:
                result = state.route("b")
            except Exception as exc:  # a torn read would KeyError here
                errors.append(exc)
                return
            if result not in ("http://e1/v1", "http://e2/v1"):  # both valid; never garbage/None/torn
                errors.append(result)
                return

    threads = [_t.Thread(target=swapper, daemon=True)] + [_t.Thread(target=router, daemon=True) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert errors == []                                                  # never raised, never torn
    assert snap_a.routes == {"a": "http://e1/v1"}                        # published snapshots not mutated
    assert snap_b.routes == {"a": "http://e1/v1", "b": "http://e2/v1"}


def test_serve_start_reload_watcher_installs_handler_and_thread(monkeypatch, tmp_path):
    """_start_reload_watcher retains the reload state, installs a SIGHUP handler that ONLY sets the
    reload flag (never raises), and starts a live reload daemon (ADR 0010 C4)."""
    import signal as _sig

    from remote import serve

    state = _serve_state(monkeypatch, tmp_path)
    monkeypatch.setattr(serve, "_reload_loop", lambda s, e: s.stop.wait(5))  # keep the thread alive, no-op
    orig = _sig.getsignal(_sig.SIGHUP) if hasattr(_sig, "SIGHUP") else None
    thread = serve._start_reload_watcher(
        state, "remote", [("http://e1/v1", ["a"], ["a"], {})], ["comfyui:x"],
        {"media": True, "media_bundles": ["x"]})
    try:
        assert state.engine_results() == [("http://e1/v1", ["a"], ["a"], {})]
        assert state.media_models == ["comfyui:x"]
        assert state.media_signature == serve._media_signature({"media": True, "media_bundles": ["x"]})
        assert thread.is_alive()
        if hasattr(_sig, "SIGHUP"):
            _sig.getsignal(_sig.SIGHUP)(_sig.SIGHUP, None)  # simulate signal delivery on the main thread
            assert state.reload_requested.is_set()          # the handler only sets the flag
    finally:
        state.stop.set()
        thread.join(2)
        if hasattr(_sig, "SIGHUP"):
            _sig.signal(_sig.SIGHUP, orig)


def test_remote_engine_startup_missing_api_env_key_exits_naming_var(monkeypatch, tmp_path):
    """A respawned serve process whose record carries an API spec but whose environment lost the
    key must exit naming the env var — not come up advertising models whose every job would 401."""
    from remote import serve

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    record = {"grid_id": "n1", "signaling_url": "https://relay.example", "media": False,
              "engines": [{"endpoint_url": "https://api.openai.com/v1", "models": ["openai:gpt-5.5"],
                           "engine_label": "openai", "api_kind": "openai"}]}
    monkeypatch.setattr(serve.run_records, "read_record", lambda g, e: record)
    monkeypatch.setattr(serve, "_load_tokens", lambda net: ("AT", "RT"))
    monkeypatch.setattr(serve, "_node_id_from_token", lambda t: "node-1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(SystemExit) as exc:
        serve.run_remote_engine_from_record("n1", "remote")
    assert "OPENAI_API_KEY" in str(exc.value)


def test_remote_engine_api_record_registers_static_caps_and_kind(monkeypatch, tmp_path):
    """Startup-seam proof for the API-engine tracer bullet: a record with one api spec comes up with
    whitelist caps (no probe), advertises the namespaced models, reports kind `openai` on the grid
    page, and holds the env key ready for forwards — while the record itself stays key-free."""
    from remote import probe, relay, serve

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    record = {"grid_id": "n1", "signaling_url": "https://relay.example", "media": False,
              "engines": [{"endpoint_url": "https://api.openai.com/v1", "models": ["openai:gpt-5.5"],
                           "engine_label": "openai", "api_kind": "openai"}]}
    monkeypatch.setattr(serve.run_records, "read_record", lambda g, e: record)
    monkeypatch.setattr(serve, "_load_tokens", lambda net: ("AT", "RT"))
    monkeypatch.setattr(serve, "_node_id_from_token", lambda t: "node-1")
    monkeypatch.setattr(probe, "capabilities", lambda *a, **k: pytest.fail("api engines are never probed"))
    seen = {}
    monkeypatch.setattr(relay, "register_node", lambda url, tok, node, **kw: seen.update(kw, node=node))
    monkeypatch.setattr(relay, "unregister_node", lambda *a, **k: None)
    monkeypatch.setattr(serve, "_heartbeat_loop", lambda s: None)
    state_seen = {}

    def fake_poll(s):
        state_seen["bearer_by_url"] = dict(s.bearer_by_url)
        s.stop.set()

    monkeypatch.setattr(serve, "_poll_loop", fake_poll)

    assert serve.run_remote_engine_from_record("n1", "remote") == 0

    assert seen["node"] == "node-1" and seen["models"] == ["openai:gpt-5.5"]
    entry = seen["capabilities"]["models"]["openai:gpt-5.5"]
    assert entry["context_window"] == 1_050_000 and entry["features"]["tools"] is True
    assert seen["meta"]["engine"] == "openai"  # the grid page shows the API engine's kind
    assert state_seen["bearer_by_url"] == {"https://api.openai.com/v1": "sk-test-123"}


def test_run_remote_engine_starts_reload_thread_with_sighup_blocked(monkeypatch, tmp_path):
    """C4: every daemon — the reload watcher, the heartbeat, AND the N poll workers — starts while SIGHUP
    is blocked (so they inherit the block), then `_serve_loop` unblocks SIGHUP on the MAIN thread last.
    Post-merge the main thread is a pure waiter (ADR 0009 concurrency), so `_poll_loop` runs in a worker
    with SIGHUP blocked — the signal can only land on the main waiter, never on a worker mid-forward."""
    import signal as _sig

    from remote import relay, serve

    if not hasattr(_sig, "SIGHUP"):
        pytest.skip("no SIGHUP on this platform")

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    record = {"grid_id": "n1", "signaling_url": "https://relay.example", "media": False,
              "engines": [{"endpoint_url": "http://e1/v1", "models": ["a"], "engine_label": None}]}
    monkeypatch.setattr(serve.run_records, "read_record", lambda g, e: record)
    monkeypatch.setattr(serve, "_load_tokens", lambda net: ("AT", "RT"))
    monkeypatch.setattr(serve, "_node_id_from_token", lambda t: "node-1")
    monkeypatch.setattr(serve, "_bring_up_engines",
                        lambda rec: ([("http://e1/v1", ["a"], ["a"], {"schema_version": 1, "models": {"a": {}}})], [], None))
    registered = []
    monkeypatch.setattr(serve, "register_once", lambda s: registered.append(s))
    monkeypatch.setattr(serve, "_heartbeat_loop", lambda s: None)
    monkeypatch.setattr(relay, "unregister_node", lambda *a, **k: None)

    reload_seen = {}
    done = threading.Event()

    def fake_reload_loop(s, eid):
        reload_seen["blocked"] = _sig.SIGHUP in _sig.pthread_sigmask(_sig.SIG_BLOCK, [])  # the daemon's own mask
        reload_seen["engine_id"] = eid
        done.set()

    poll_mask = {}

    def fake_poll(s):
        # Runs in a poll WORKER (spawned before the unblock), so it inherited the SIGHUP block — the
        # signal can never land here mid-forward. Record the mask, then set stop so `_serve_loop` returns.
        poll_mask["worker_blocked"] = _sig.SIGHUP in _sig.pthread_sigmask(_sig.SIG_BLOCK, [])
        done.wait(2)   # let the reload daemon run + record before we tear down
        s.stop.set()   # release the main-thread waiter so `_serve_loop` returns (no 60s park)

    monkeypatch.setattr(serve, "_reload_loop", fake_reload_loop)
    monkeypatch.setattr(serve, "_poll_loop", fake_poll)

    orig_handler = _sig.getsignal(_sig.SIGHUP)
    orig_blocked = _sig.SIGHUP in _sig.pthread_sigmask(_sig.SIG_BLOCK, [])
    try:
        rc = serve.run_remote_engine_from_record("n1", "remote")
        # This pytest thread IS the one that ran `_serve_loop`; it unblocked SIGHUP on itself (the waiter)
        # before parking and never re-blocked it, so SIGHUP is unblocked here — only on main (C4).
        main_unblocked = _sig.SIGHUP not in _sig.pthread_sigmask(_sig.SIG_BLOCK, [])
    finally:
        _sig.signal(_sig.SIGHUP, orig_handler)
        _sig.pthread_sigmask(_sig.SIG_BLOCK if orig_blocked else _sig.SIG_UNBLOCK, {_sig.SIGHUP})

    assert rc == 0 and registered                   # startup registered the identity
    assert reload_seen["blocked"] is True           # the reload daemon inherited the SIGHUP block (C4)
    assert reload_seen["engine_id"] == "remote"
    assert poll_mask["worker_blocked"] is True      # poll workers inherited the block too — never SIGHUP'd
    assert main_unblocked is True                    # main unblocked SIGHUP for the waiter that receives it


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


def _api_serve_state(monkeypatch, tmp_path, **overrides):
    """A `_ServeState` serving one API-engine model: advertised `openai:gpt-5.5` routed to the vendor
    base URL, vendor-name rewrite, and the key attached per target URL."""
    kwargs = dict(
        models=["openai:gpt-5.5"],
        routes={"openai:gpt-5.5": "https://api.openai.com/v1"},
        upstream={"openai:gpt-5.5": "gpt-5.5"},
        bearer_by_url={"https://api.openai.com/v1": "sk-test-123"},
    )
    kwargs.update(overrides)
    return _serve_state(monkeypatch, tmp_path, **kwargs)


def test_serve_handle_job_api_forward_carries_bearer_and_vendor_model(monkeypatch, tmp_path):
    from remote import relay, serve

    state = _api_serve_state(monkeypatch, tmp_path)
    captured = {}
    monkeypatch.setattr(relay, "submit_response",
                        lambda url, tok, txn, *, content, stream: captured.update(content=content, stream=stream))
    monkeypatch.setattr(relay, "submit_error", lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))

    def vendor(request):
        assert str(request.url) == "https://api.openai.com/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer sk-test-123"
        assert json.loads(request.content)["model"] == "gpt-5.5"  # vendor name, not openai:gpt-5.5
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    _mock_serve_engine(monkeypatch, vendor)
    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "chat/completions",
                             "body": {"model": "openai:gpt-5.5"}, "is_stream": False})
    assert captured["stream"] is False and b'"hi"' in captured["content"] and "error" not in captured


def test_serve_handle_job_api_stream_passes_sse_with_bearer(monkeypatch, tmp_path):
    from remote import relay, serve

    state = _api_serve_state(monkeypatch, tmp_path)
    captured = {}

    def cap_submit(url, tok, txn, *, content, stream):
        captured.update(stream=stream, body=b"".join(content))  # materialise the generator while open

    monkeypatch.setattr(relay, "submit_response", cap_submit)

    def vendor(request):
        assert request.headers["authorization"] == "Bearer sk-test-123"
        return httpx.Response(200, content=b"data: a\n\ndata: [DONE]\n\n")

    _mock_serve_engine(monkeypatch, vendor)
    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "chat/completions",
                             "body": {"model": "openai:gpt-5.5"}, "is_stream": True})
    assert captured["stream"] is True and captured["body"] == b"data: a\n\ndata: [DONE]\n\n"


def test_serve_handle_job_hardware_forward_has_no_bearer(monkeypatch, tmp_path):
    """A hardware engine in the same identity as an API engine must never see the vendor key."""
    from remote import relay, serve

    state = _api_serve_state(
        monkeypatch, tmp_path,
        models=["llama3", "openai:gpt-5.5"],
        routes={"llama3": "http://127.0.0.1:8081/v1", "openai:gpt-5.5": "https://api.openai.com/v1"},
    )
    captured = {}
    monkeypatch.setattr(relay, "submit_response", lambda url, tok, txn, *, content, stream: captured.update(ok=True))

    def engine(request):
        assert request.url.host == "127.0.0.1"
        assert "authorization" not in request.headers  # the key rides only to the vendor URL
        return httpx.Response(200, json={})

    _mock_serve_engine(monkeypatch, engine)
    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "chat/completions",
                             "body": {"model": "llama3"}, "is_stream": False})
    assert captured.get("ok") is True


def test_serve_handle_job_api_upstream_401_is_job_error_not_token_refresh(monkeypatch, tmp_path):
    """A vendor 401 is a JOB error in the vendor's auth domain: it must reach the consumer with the
    upstream status and must never enter the relay-token refresh path (distinct auth domains)."""
    from remote import relay, serve

    state = _api_serve_state(monkeypatch, tmp_path)
    monkeypatch.setattr(state, "refresh",
                        lambda stale_token=None: pytest.fail("vendor 401 must not touch relay-token refresh"))
    captured = {}
    monkeypatch.setattr(relay, "submit_response", lambda *a, **k: captured.update(submitted=True))
    monkeypatch.setattr(relay, "submit_error", lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))
    _mock_serve_engine(monkeypatch, lambda r: httpx.Response(401, text="invalid api key"))

    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "chat/completions",
                             "body": {"model": "openai:gpt-5.5"}, "is_stream": False})
    assert "401" in captured["error"] and "submitted" not in captured


def test_serve_handle_job_media_without_media_url_submits_error(monkeypatch, tmp_path):
    """A text-only identity that gets a media job (mis-routed by the relay) reports it, never crashes."""
    from remote import relay, serve

    state = _serve_state(monkeypatch, tmp_path)  # no media_url → this engine serves no media
    captured = {}
    monkeypatch.setattr(relay, "submit_error", lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))

    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "media/image/generate", "body": {}})
    assert "media" in captured["error"].lower()


def test_serve_handle_job_forwards_media_to_media_server(monkeypatch, tmp_path):
    """A media identity forwards the job to its local media server and streams the SSE back — always
    streamed (media responses are SSE), regardless of the job's is_stream flag."""
    from remote import relay, serve

    state = _serve_state(monkeypatch, tmp_path, media_url="http://127.0.0.1:8190")
    captured = {}

    def cap_submit(url, tok, txn, *, content, stream):
        captured.update(stream=stream, txn=txn, body=b"".join(content))  # drain the generator while open

    monkeypatch.setattr(relay, "submit_response", cap_submit)
    monkeypatch.setattr(relay, "submit_error", lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))

    def media_server(request):
        assert request.url.path == "/media/image/generate"  # forwarded to the media server, not /v1
        assert json.loads(request.content)["prompt"] == "a cat"
        return httpx.Response(200, content=b'data: {"type":"result"}\n\ndata: [DONE]\n\n')

    _mock_serve_engine(monkeypatch, media_server)
    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "media/image/generate",
                             "body": {"prompt": "a cat"}, "is_stream": False})
    assert captured["stream"] is True and captured["txn"] == "t1"
    assert b"[DONE]" in captured["body"] and "error" not in captured


def test_serve_handle_job_rejects_unknown_media_endpoint(monkeypatch, tmp_path):
    """The relay's endpoint_path is untrusted: a media path outside the fixed allowlist is refused even
    on a media engine, so a traversal like `media/../` can never reach the media server (ADR 0004 §6)."""
    from remote import relay, serve

    state = _serve_state(monkeypatch, tmp_path, media_url="http://127.0.0.1:8190")
    captured = {}
    monkeypatch.setattr(relay, "submit_error", lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))

    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "media/bogus", "body": {}})
    assert "unsupported media endpoint" in captured["error"].lower()


def test_serve_submit_response_refreshes_and_retries_on_401(monkeypatch, tmp_path):
    """A completed non-stream result whose token expired mid-run isn't discarded: submit refreshes
    once and retries with the new token (mirrors poll/heartbeat)."""
    from remote import relay, serve

    state = _serve_state(monkeypatch, tmp_path)
    calls = {"n": 0}

    def submit(url, tok, txn, *, content, stream):
        calls["n"] += 1
        if calls["n"] == 1:
            raise relay.RelayUnauthorized()
        calls["tok"] = tok

    monkeypatch.setattr(relay, "submit_response", submit)

    def fake_refresh(stale_token=None):
        state._access_token = "AT2"
        return True

    monkeypatch.setattr(state, "refresh", fake_refresh)
    serve._submit_response(state, "t1", content=b"result", stream=False)
    assert calls["n"] == 2 and calls["tok"] == "AT2"


def test_serve_submit_response_stream_does_not_retry_on_401(monkeypatch, tmp_path):
    """A streamed body is single-use, so a 401 re-raises rather than replaying (handle_job then reports
    it via _try_submit_error, which refreshes)."""
    from remote import relay, serve

    state = _serve_state(monkeypatch, tmp_path)
    monkeypatch.setattr(relay, "submit_response",
                        lambda *a, **k: (_ for _ in ()).throw(relay.RelayUnauthorized()))
    monkeypatch.setattr(state, "refresh", lambda stale_token=None: pytest.fail("stream must not refresh/retry"))
    with pytest.raises(relay.RelayUnauthorized):
        serve._submit_response(state, "t1", content=iter([b"x"]), stream=True)


def test_serve_try_submit_error_refreshes_on_401(monkeypatch, tmp_path):
    """Job-failure reporting survives token expiry — else the consumer gets no terminal signal."""
    from remote import relay, serve

    state = _serve_state(monkeypatch, tmp_path)
    calls = {"n": 0}

    def submit_err(url, tok, txn, *, message, tokens_delivered=0):
        calls["n"] += 1
        if calls["n"] == 1:
            raise relay.RelayUnauthorized()
        calls["tok"] = tok

    monkeypatch.setattr(relay, "submit_error", submit_err)

    def fake_refresh(stale_token=None):
        state._access_token = "AT2"
        return True

    monkeypatch.setattr(state, "refresh", fake_refresh)
    serve._try_submit_error(state, "t1", "boom")
    assert calls["n"] == 2 and calls["tok"] == "AT2"


def test_serve_advertised_models_rejects_comfyui_alias():
    """A text --advertise-as alias can't hijack the reserved comfyui:* media namespace."""
    from remote import serve

    with pytest.raises(SystemExit) as exc:
        serve._advertised_models(["mymodel"], ["comfyui:image_generation"])
    assert "comfyui" in str(exc.value).lower()


def test_run_remote_engine_media_only_registers_comfyui_models(monkeypatch, tmp_path):
    """A media-only record: bring up the media engine (no text engine), register the comfyui:* models
    + media caps, and pass the loopback media_url to the serve state. Guards the has_text skip so the
    empty text spec never reaches `_bring_up_engines`."""
    import base64
    import json as _json

    from shared import run_records
    from local import media_engine, media_runtime
    from remote import relay, serve

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    run_records.write_record("n1", "media1", {
        "engine_id": "media1", "node_id": "ignored", "grid_id": "n1", "pid": 0,
        "signaling_url": "https://relay.example", "endpoint_url": None, "models": [], "engines": [],
        "media": True, "media_bundles": ["image_generation"], "comfyui_port": 8188, "media_port": 8190,
    })
    # A per-grid token whose JWT carries the node_id claim (register addresses the token's own node).
    claims = base64.urlsafe_b64encode(_json.dumps({"node_id": "node-xyz"}).encode()).decode().rstrip("=")
    monkeypatch.setattr(serve, "_load_tokens", lambda net: (f"h.{claims}.s", "RT"))

    class _Proc:
        pid = 1234

    monkeypatch.setattr(media_engine, "prepare_media_engine", lambda **kw: {
        "models": ["comfyui:image_generation"], "proc": _Proc(), "media_url": "unused", "comfyui_started": False,
    })
    monkeypatch.setattr(serve, "_bring_up_engines",
                        lambda rec: pytest.fail("media-only join must not bring up a text engine"))
    captured = {}
    monkeypatch.setattr(relay, "register_node", lambda url, tok, node, **kw: captured.update(node=node, **kw))
    monkeypatch.setattr(serve, "_poll_loop", lambda state: (captured.update(media_url=state.media_url), state.stop.set()))
    monkeypatch.setattr(relay, "heartbeat", lambda *a, **k: "ok")
    monkeypatch.setattr(relay, "unregister_node", lambda *a, **k: None)
    monkeypatch.setattr(media_runtime, "stop_media_server", lambda proc, **k: None)

    assert serve.run_remote_engine_from_record("n1", "media1") == 0
    assert captured["node"] == "node-xyz"
    assert captured["models"] == ["comfyui:image_generation"]
    assert captured["capabilities"]["models"]["comfyui:image_generation"]["endpoints"] == ["media"]
    assert captured["media_url"] == "http://127.0.0.1:8190"  # loopback forward target for the poll loop


def test_run_remote_engine_reaps_record_when_never_registered(monkeypatch, tmp_path):
    """A remote media engine that dies before registering (ComfyUI never ready) must not leave a
    stale on-disk record behind."""
    import base64
    import json as _json

    from shared import run_records
    from local import media_engine
    from remote import serve

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    run_records.write_record("n1", "media1", {
        "engine_id": "media1", "node_id": "ignored", "grid_id": "n1", "pid": 0,
        "signaling_url": "https://relay.example", "endpoint_url": None, "models": [], "engines": [],
        "media": True, "media_bundles": ["image_generation"], "comfyui_port": 8188, "media_port": 8190,
    })
    claims = base64.urlsafe_b64encode(_json.dumps({"node_id": "node-xyz"}).encode()).decode().rstrip("=")
    monkeypatch.setattr(serve, "_load_tokens", lambda net: (f"h.{claims}.s", "RT"))

    def boom(**kw):
        raise SystemExit("ComfyUI did not become ready")

    monkeypatch.setattr(media_engine, "prepare_media_engine", boom)

    assert serve.run_remote_engine_from_record("n1", "media1") == 1  # detached top level reports failure
    assert run_records.read_records("n1") == {}  # record reaped, not left stale


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
    from shared.system import gpu

    monkeypatch.setattr(gpu, "load_snapshot", lambda timeout=3.0: {})  # no-GPU box: load is just active_tasks + platform
    state = _serve_state(monkeypatch, tmp_path)
    seen = {}
    monkeypatch.setattr(relay, "heartbeat", lambda url, tok, *, load: seen.update(load=load) or "ok")
    assert serve.heartbeat_once(state) == "ok"
    assert seen["load"] == {"active_tasks": 0, "platform": "linux"}


def test_serve_heartbeat_once_re_registers_when_pruned(monkeypatch, tmp_path):
    from remote import relay, serve

    state = _serve_state(monkeypatch, tmp_path)
    monkeypatch.setattr(relay, "heartbeat", lambda url, tok, *, load: "missing")
    calls = []
    monkeypatch.setattr(serve, "register_once", lambda s: calls.append(s))
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


def test_serve_handle_job_rewrites_alias_to_upstream_model(monkeypatch, tmp_path):
    """The consumer addresses the model by its advertised alias; the external engine only knows its
    real name. handle_job must rewrite body['model'] alias→real before forwarding (Issue 1: 404)."""
    from remote import relay, serve

    state = _serve_state(
        monkeypatch, tmp_path, models=["ollama-model"],
        routes={"ollama-model": "http://sole.local/v1"},
        upstream={"ollama-model": "qwen3.5:0.8b"},
    )
    captured = {}
    monkeypatch.setattr(relay, "submit_response",
                        lambda url, tok, txn, *, content, stream: captured.update(content=content))
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))

    def engine(request):
        # the LOCAL engine must receive its real model name, not the advertised alias
        assert json.loads(request.content)["model"] == "qwen3.5:0.8b"
        return httpx.Response(200, json={"ok": True})

    _mock_serve_engine(monkeypatch, engine)
    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "chat/completions",
                             "body": {"model": "ollama-model", "messages": []}, "is_stream": False})
    assert "error" not in captured and b'"ok"' in captured["content"]


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

    routes, upstream, union, caps, warns = serve._build_routing([
        ("http://127.0.0.1:8081/v1", ["m1", "m2"], ["m1", "m2"], {"schema_version": 1, "models": {"m1": {"x": 1}}}),
    ])
    assert routes == {"m1": "http://127.0.0.1:8081/v1", "m2": "http://127.0.0.1:8081/v1"}
    assert upstream == {"m1": "m1", "m2": "m2"}  # no alias → identity
    assert union == ["m1", "m2"]  # both advertised; only the probed first model carries caps
    assert caps == {"schema_version": 1, "models": {"m1": {"x": 1}}}
    assert warns == []


def test_build_routing_disjoint_engines_union_and_merge(monkeypatch, tmp_path):
    from remote import serve

    routes, upstream, union, caps, warns = serve._build_routing([
        ("http://127.0.0.1:8081/v1", ["a"], ["a"], {"schema_version": 1, "models": {"a": {"f": "A"}}}),
        ("http://127.0.0.1:8000/v1", ["b"], ["b"], {"schema_version": 1, "models": {"b": {"f": "B"}}}),
    ])
    assert routes == {"a": "http://127.0.0.1:8081/v1", "b": "http://127.0.0.1:8000/v1"}
    assert upstream == {"a": "a", "b": "b"}
    assert union == ["a", "b"]
    assert caps == {"schema_version": 1, "models": {"a": {"f": "A"}, "b": {"f": "B"}}}
    assert warns == []


def test_build_routing_duplicate_model_first_wins_with_warning(monkeypatch, tmp_path):
    from remote import serve

    routes, upstream, union, caps, warns = serve._build_routing([
        ("http://127.0.0.1:8081/v1", ["dup"], ["dup"], {"schema_version": 1, "models": {"dup": {"f": "first"}}}),
        ("http://127.0.0.1:8000/v1", ["dup"], ["dup"], {"schema_version": 1, "models": {"dup": {"f": "second"}}}),
    ])
    assert routes == {"dup": "http://127.0.0.1:8081/v1"}  # first detected wins
    assert upstream == {"dup": "dup"}  # winner's upstream, shadowed duplicate ignored
    assert union == ["dup"]  # advertised once
    assert caps == {"schema_version": 1, "models": {"dup": {"f": "first"}}}  # caps follow the winner
    assert len(warns) == 1 and "dup" in warns[0]


def test_build_routing_tolerates_failed_probe_empty_caps(monkeypatch, tmp_path):
    from remote import serve

    # A failed probe degrades to {} upstream — the merge must still route, not KeyError.
    routes, upstream, union, caps, warns = serve._build_routing([
        ("http://127.0.0.1:8081/v1", ["m"], ["m"], {}),
    ])
    assert routes == {"m": "http://127.0.0.1:8081/v1"} and union == ["m"]
    assert upstream == {"m": "m"}
    assert caps == {}  # no capabilities → registers text-only
    assert warns == []


def test_build_routing_maps_alias_to_upstream_real_name(monkeypatch, tmp_path):
    """A `--advertise-as` alias routes + registers under the alias, but upstream carries the engine's
    real model name so a forwarded job can be rewritten to what the engine actually serves."""
    from remote import serve

    routes, upstream, union, caps, warns = serve._build_routing([
        ("http://h:11434/v1", ["ollama-model"], ["qwen3.5:0.8b"],
         {"schema_version": 1, "models": {"ollama-model": {"x": 1}}}),
    ])
    assert routes == {"ollama-model": "http://h:11434/v1"}
    assert upstream == {"ollama-model": "qwen3.5:0.8b"}  # alias → real, the crux of the forward fix
    assert union == ["ollama-model"]
    assert caps == {"schema_version": 1, "models": {"ollama-model": {"x": 1}}}
    assert warns == []


def test_meta_uses_meta_name_for_grid_page_name(monkeypatch, tmp_path):
    """The grid-page node name comes from the record's ``meta_name`` (--name, or hostname when omitted),
    not the singleton record key ``engine_id`` (which is the constant "remote")."""
    from remote import serve

    assert serve._meta({"meta_name": "mybox", "endpoint_url": "http://h/v1"}, "remote")["name"] == "mybox"
    # Fallback to engine_id when no display name was stored (defensive; join always sets meta_name).
    assert serve._meta({"endpoint_url": "http://h/v1"}, "remote")["name"] == "remote"


def test_meta_labels_all_external_union_as_external(monkeypatch, tmp_path):
    """A union of external --at engines (each engine_label=None) shows engine='external' on the grid page,
    not the built-in 'llama.cpp' default; only a built-in --serve spec (no endpoint_url) is 'llama.cpp'
    (ADR 0010 _meta fix)."""
    from remote import serve

    multi_external = {"meta_name": "mybox", "endpoint_url": None, "models": ["a", "b"], "engines": [
        {"endpoint_url": "http://e1/v1", "models": ["a"], "engine_label": None},
        {"endpoint_url": "http://e2/v1", "models": ["b"], "engine_label": None},
    ]}
    assert serve._meta(multi_external, "remote") == {"name": "mybox", "engine": "external"}

    single_external = {"endpoint_url": "http://h/v1", "models": ["a"]}  # flat single external → external
    assert serve._meta(single_external, "remote")["engine"] == "external"

    builtin = {"endpoint_url": None, "models": ["m"],
               "engines": [{"endpoint_url": None, "models": ["m"], "engine_label": None}]}
    assert serve._meta(builtin, "remote")["engine"] == "llama.cpp"  # built-in --serve still labels llama.cpp


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
    monkeypatch.setattr(probe, "capabilities",
                        lambda url, model, *, advertise_as=None, context_window=None: seen.append((url, model))
                        or {"schema_version": 1, "models": {advertise_as or model: {"f": model}}})

    results, launched, launcher = serve._bring_up_engines(record)
    assert launched == [] and launcher is None  # external engines: nothing launched/owned
    assert results == [
        ("http://h:11434/v1", ["llama3"], ["llama3"], {"schema_version": 1, "models": {"llama3": {"f": "llama3"}}}),
        ("http://h:8000/v1", ["mistral"], ["mistral"], {"schema_version": 1, "models": {"mistral": {"f": "mistral"}}}),
    ]
    assert seen == [("http://h:11434/v1", "llama3"), ("http://h:8000/v1", "mistral")]  # each probed by real name


def test_bring_up_engines_single_spec_multi_model_probes_every_model(monkeypatch, tmp_path):
    """One engine spec serving several models must probe & advertise caps for EVERY model, not just the
    first — else models B, C… register with routes but no `features`, so the relay can't route
    capability-gated (e.g. tool_choice) requests to them. This one-spec/N-models shape is what BOTH
    `grid join --at <url> -m A -m B -m C` AND a bare `grid join` against an auto-detected engine serving
    several models (e.g. one Ollama with A/B/C pulled — detect returns one spec carrying all of them)
    collapse to, so this test covers the common consumer setup, not just a hand-rolled --at."""
    from remote import probe, serve

    record = {
        "engines": [{"endpoint_url": "http://h:9000/v1", "models": ["A", "B", "C"], "engine_label": "vllm"}],
        "advertise_as": [],
    }
    seen = []
    monkeypatch.setattr(probe, "capabilities",
                        lambda url, model, *, advertise_as=None, context_window=None: seen.append(model)
                        or {"schema_version": 1, "models": {advertise_as or model: {"f": model}}})

    results, launched, _ = serve._bring_up_engines(record)
    assert launched == []
    assert seen == ["A", "B", "C"]                          # every model probed, not just the first
    (llm_url, advertised, upstream, caps), = results
    assert advertised == ["A", "B", "C"] and upstream == ["A", "B", "C"]
    assert set(caps["models"]) == {"A", "B", "C"}           # one merged envelope carries all models
    assert caps["models"]["B"] == {"f": "B"}                # later models keep their own probed entry


def test_bring_up_engines_api_spec_static_caps_never_probes(monkeypatch, tmp_path):
    """An API engine's caps come from the static whitelist — no probe or benchmark call may ever
    target the vendor (ADR 0012). Upstream names are the vendor names derived from the advertised
    `openai:*` form, so the forward rewrite sends what the vendor understands."""
    from remote import probe, serve

    record = {
        "engines": [{
            "endpoint_url": "https://api.openai.com/v1",
            "models": ["openai:gpt-5.5", "openai:gpt-5.4-mini"],
            "engine_label": "openai",
            "api_kind": "openai",
        }],
        "advertise_as": [],
    }
    monkeypatch.setattr(probe, "capabilities",
                        lambda *a, **k: pytest.fail("api engines are never probed"))

    results, launched, launcher = serve._bring_up_engines(record)
    assert launched == [] and launcher is None
    (llm_url, advertised, upstream, caps), = results
    assert llm_url == "https://api.openai.com/v1"
    assert advertised == ["openai:gpt-5.5", "openai:gpt-5.4-mini"]
    assert upstream == ["gpt-5.5", "gpt-5.4-mini"]          # vendor names, derived from the whitelist
    assert caps["schema_version"] == 1
    flagship = caps["models"]["openai:gpt-5.5"]
    assert flagship["features"]["tools"] is True and flagship["features"]["vision"] is True
    assert flagship["context_window"] == 1_050_000          # static caps straight from the whitelist


def test_bring_up_engines_api_model_gone_from_whitelist_warns_and_degrades(monkeypatch, tmp_path, capsys):
    """A model that left the whitelist between join and respawn degrades like a failed probe
    (all-False caps, prefix-strip rewrite) — but the degrade must leave a stderr trace, like every
    other serve-side degrade, so quietly broken tool/vision consumers are diagnosable."""
    from remote import probe, serve

    record = {
        "engines": [{
            "endpoint_url": "https://api.openai.com/v1",
            "models": ["openai:gpt-5.5", "openai:gpt-legacy"],
            "engine_label": "openai",
            "api_kind": "openai",
        }],
        "advertise_as": [],
    }
    monkeypatch.setattr(probe, "capabilities", lambda *a, **k: pytest.fail("api engines are never probed"))

    results, _launched, _launcher = serve._bring_up_engines(record)

    (_url, _advertised, upstream, caps), = results
    assert upstream == ["gpt-5.5", "gpt-legacy"]  # prefix-strip fallback still rewrites sanely
    legacy = caps["models"]["openai:gpt-legacy"]
    assert all(v is False for v in legacy["features"].values())  # degraded, not crashed
    err = capsys.readouterr().err
    assert "openai:gpt-legacy" in err and "whitelist" in err  # ...and the degrade is observable


def test_bring_up_engines_mixed_union_probes_only_hardware(monkeypatch, tmp_path):
    from remote import probe, serve

    record = {
        "engines": [
            {"endpoint_url": "http://h:11434/v1", "models": ["llama3"], "engine_label": "ollama"},
            {"endpoint_url": "https://api.openai.com/v1", "models": ["openai:gpt-5.5"],
             "engine_label": "openai", "api_kind": "openai"},
        ],
        "advertise_as": [],
    }
    probed = []
    monkeypatch.setattr(probe, "capabilities",
                        lambda url, model, *, advertise_as=None, context_window=None: probed.append((url, model))
                        or {"schema_version": 1, "models": {advertise_as or model: {}}})

    results, launched, _ = serve._bring_up_engines(record)
    assert launched == []
    assert probed == [("http://h:11434/v1", "llama3")]      # only the hardware engine is probed
    assert results[1][1] == ["openai:gpt-5.5"] and results[1][2] == ["gpt-5.5"]


def test_bring_up_engines_external_alias_probes_real_name_keys_alias(monkeypatch, tmp_path):
    """`--advertise-as` on an external engine: probe by the engine's REAL model name (Ollama/vLLM
    don't know the alias) but return caps + upstream so the loop registers/forwards under the alias."""
    from remote import probe, serve

    record = {
        "engines": [{"endpoint_url": "http://h:11434/v1", "models": ["qwen3.5:0.8b"], "engine_label": "ollama"}],
        "advertise_as": ["ollama-model"],
    }
    seen = {}

    def fake_caps(url, model, *, advertise_as=None, context_window=None):
        seen.update(url=url, model=model, advertise_as=advertise_as)
        return {"schema_version": 1, "models": {advertise_as or model: {"f": model}}}

    monkeypatch.setattr(probe, "capabilities", fake_caps)

    results, launched, _ = serve._bring_up_engines(record)
    assert launched == []
    assert seen["model"] == "qwen3.5:0.8b"  # probed by the real name the engine answers to
    assert seen["advertise_as"] == "ollama-model"  # caps keyed by the advertised alias
    llm_url, advertised, upstream, caps = results[0]
    assert advertised == ["ollama-model"] and upstream == ["qwen3.5:0.8b"]
    assert set(caps["models"]) == {"ollama-model"}  # relay registers under the advertised name


def test_bring_up_engines_falls_back_to_flat_record(monkeypatch, tmp_path):
    from remote import probe, serve

    # A record written before multi-engine has no `engines` list — synthesise one spec from flat fields.
    record = {"endpoint_url": "http://h:11434/v1", "models": ["llama3"], "advertise_as": []}
    monkeypatch.setattr(probe, "capabilities",
                        lambda url, model, *, advertise_as=None, context_window=None: {"schema_version": 1, "models": {advertise_as or model: {}}})

    results, launched, _ = serve._bring_up_engines(record)
    assert launched == []  # external engine: nothing launched
    assert results[0][0] == "http://h:11434/v1"
    assert results[0][1] == ["llama3"]
    assert results[0][2] == ["llama3"]  # upstream == advertised when no alias


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
    assert "n1" in out and "n2" in out  # network_id column (ADR 0011 item 9b)


def test_remote_ls_json_emits_grid_and_type(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path, networks=[
        {"network_id": "n1", "name": "team", "network_type": "permissioned-public"}])
    assert cli.main(["ls", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [
        {"grid": "team", "type": "permissioned-public", "id": "n1"}]


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


# ---------------------------------------------------------------------------
# Remote `grid engines` / `grid models` (cli/remote_overview.py + dispatch, via the relay overview)
# ---------------------------------------------------------------------------

_OVERVIEW_2NODES = {
    "grid": {"state": "running"},
    "stats": {"models": 2, "nodes": 2},
    "models": [],
    "nodes": [
        {"name": "mac-studio", "device": "Mac Studio", "chip": "M2 Ultra", "memory_gb": 192,
         "device_class": "gpu", "model": "glm-5.2", "models": ["glm-5.2", "qwen-3"],
         "engine": "MLX", "throughput_tok_s": 58.0, "max_concurrency": 1, "online": True},
        {"name": "ollama-box", "device": "Linux x86_64", "chip": None, "memory_gb": 64,
         "device_class": "gpu", "model": "glm-5.2", "models": ["glm-5.2"],
         "engine": "ollama", "throughput_tok_s": 120.0, "max_concurrency": 4, "online": True},
    ],
}


def _mock_overview(monkeypatch, payload, seen=None):
    """Serve GET /relay/v1/grid/overview with `payload` via the relay MockTransport."""
    def handler(request):
        if seen is not None:
            seen["method"], seen["path"] = request.method, request.url.path
            seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=payload)

    _mock_relay(monkeypatch, handler)


def test_remote_engines_lists_nodes_with_engine_device_and_models(monkeypatch, tmp_path, capsys):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    seen = {}
    _mock_overview(monkeypatch, _OVERVIEW_2NODES, seen)
    assert cli.main(["engines"]) == 0
    out = capsys.readouterr().out
    assert (seen["method"], seen["path"]) == ("GET", "/relay/v1/grid/overview")
    assert "mac-studio" in out and "MLX" in out and "Mac Studio" in out
    assert "ollama-box" in out and "ollama" in out
    assert "glm-5.2,qwen-3" in out  # the MLX node's models, joined


def test_remote_engines_json_passes_through_nodes_verbatim(monkeypatch, tmp_path, capsys):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_overview(monkeypatch, _OVERVIEW_2NODES)
    assert cli.main(["engines", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == _OVERVIEW_2NODES["nodes"]  # verbatim, incl. model(singular)/online/max_concurrency


def test_remote_engines_empty_when_no_nodes(monkeypatch, tmp_path, capsys):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_overview(monkeypatch, {"nodes": []})
    assert cli.main(["engines"]) == 0
    assert "no engines" in capsys.readouterr().out


def test_remote_models_lists_unique_ids_deduped_across_nodes(monkeypatch, tmp_path, capsys):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_overview(monkeypatch, _OVERVIEW_2NODES)
    assert cli.main(["models"]) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert lines == ["glm-5.2", "qwen-3"]  # first-seen order, glm-5.2 not repeated


def test_remote_models_verbose_shows_model_engine_node_rows(monkeypatch, tmp_path, capsys):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_overview(monkeypatch, _OVERVIEW_2NODES)
    assert cli.main(["models", "--verbose"]) == 0
    out = capsys.readouterr().out
    assert "MODEL" in out and "ENGINE" in out and "NODE" in out
    assert "glm-5.2" in out and "MLX" in out and "mac-studio" in out
    assert "qwen-3" in out and "ollama-box" in out  # both nodes' rows present
    assert out.count("glm-5.2") == 2  # served by both nodes → one row each


def test_remote_models_json_maps_name_to_node_key(monkeypatch, tmp_path, capsys):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_overview(monkeypatch, _OVERVIEW_2NODES)
    assert cli.main(["models", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {"model": "glm-5.2", "engine": "MLX", "node": "mac-studio"} in payload
    assert {"model": "qwen-3", "engine": "MLX", "node": "mac-studio"} in payload
    assert {"model": "glm-5.2", "engine": "ollama", "node": "ollama-box"} in payload


def test_remote_models_empty_when_no_nodes(monkeypatch, tmp_path, capsys):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_overview(monkeypatch, {"nodes": []})
    assert cli.main(["models"]) == 0
    assert "no live models" in capsys.readouterr().out


def test_remote_engines_requires_grid_up(monkeypatch, tmp_path):
    _seed_remote(monkeypatch, tmp_path,
                networks=[{"network_id": "n1", "name": "team", "access_token": "AT", "refresh_token": "RT"}],
                active="team")
    _mock_lifecycle(monkeypatch, status={"state": "stopped"})  # down → no relay address
    with pytest.raises(SystemExit) as exc:
        cli.main(["engines"])
    assert "grid up" in str(exc.value).lower()


def test_remote_engines_works_without_access_token(monkeypatch, tmp_path, capsys):
    """The overview route is public — listing must work before `grid sync` stores a per-grid token."""
    _seed_remote(monkeypatch, tmp_path,
                networks=[{"network_id": "n1", "name": "team"}], active="team")  # no access_token
    _mock_lifecycle(monkeypatch, status={"state": "running", "signaling_url": "https://relay.example"})
    _mock_overview(monkeypatch, _OVERVIEW_2NODES)
    assert cli.main(["engines"]) == 0
    assert "mac-studio" in capsys.readouterr().out


def test_dispatch_routes_engines_and_models_to_real_handlers(monkeypatch, tmp_path, capsys):
    """engines/models are no longer stubbed in remote mode."""
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_overview(monkeypatch, _OVERVIEW_2NODES)
    assert cli.main(["models"]) == 0
    assert "isn't available in remote mode" not in capsys.readouterr().out  # stub message gone


def test_remote_engines_non_json_body_is_clean_error(monkeypatch, tmp_path):
    """A 200 with a non-JSON body (e.g. a proxy maintenance page) → clean SystemExit, not a traceback."""
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_relay(monkeypatch, lambda r: httpx.Response(200, text="<html>maintenance</html>"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["engines"])
    assert "non-json" in str(exc.value).lower()


def test_remote_engines_tolerates_malformed_nodes(monkeypatch, tmp_path, capsys):
    """A non-dict node entry and a scalar `models` field render as empty, never crash the table."""
    payload = {"nodes": [
        {"name": "n1", "engine": "MLX", "models": "llama3"},   # models is a bare string, not a list
        "junk",                                                 # not an object at all
        {"name": "n2", "engine": "ollama", "models": ["real-model"]},
    ]}
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_overview(monkeypatch, payload)
    assert cli.main(["engines"]) == 0
    out = capsys.readouterr().out
    assert "n1" in out and "n2" in out and "real-model" in out
    assert "(none)" in out          # n1's bad `models` → no models, not split characters
    assert "l,l,a,m,a" not in out   # the bare string was NOT iterated into characters


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

def test_remote_members_add_defaults_to_both_role(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                networks=[{"network_id": "n1", "name": "team"}], active="team")
    calls = _mock_members(monkeypatch, add={"ok": True})

    assert cli.main(["members", "add", "alice@example.com"]) == 0
    out = capsys.readouterr().out
    assert calls["add"] == {
        "session": "sess-tok", "network_id": "n1", "email": "alice@example.com", "roles": ["both"],
    }
    assert "alice@example.com" in out and "both" in out


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


def _build_gguf(kvs: list[tuple[str, int, object]]) -> bytes:
    """Minimal GGUF v3 blob carrying the given (key, value_type, value) metadata."""
    import struct

    def gstr(s: str) -> bytes:
        b = s.encode("utf-8")
        return struct.pack("<Q", len(b)) + b

    out = bytearray(b"GGUF")
    out += struct.pack("<I", 3)          # version
    out += struct.pack("<Q", 0)          # tensor count
    out += struct.pack("<Q", len(kvs))   # kv count
    for key, vtype, value in kvs:
        out += gstr(key) + struct.pack("<I", vtype)
        if vtype == 8:      # string
            out += gstr(value)
        elif vtype == 4:    # uint32
            out += struct.pack("<I", value)
        else:
            raise ValueError(f"test helper does not encode type {vtype}")
    return bytes(out)


def test_gguf_read_context_length(tmp_path):
    from shared.models import gguf

    path = tmp_path / "m.gguf"
    path.write_bytes(_build_gguf([
        ("general.architecture", 8, "llama"),
        ("general.name", 8, "unused"),
        ("llama.context_length", 4, 4096),
    ]))
    assert gguf.read_context_length(path) == 4096


def test_gguf_read_context_length_rejects_non_gguf(tmp_path):
    from shared.models import gguf

    path = tmp_path / "not.gguf"
    path.write_bytes(b"NOPE, not a gguf header")
    assert gguf.read_context_length(path) is None


def test_ctx_command_wired_to_handler():
    from cli.models import cmd_ctx

    parser = cli.build_parser()
    ns = parser.parse_args(["ctx", "some-model.gguf", "--json"])
    assert ns.handler is cmd_ctx
    assert ns.model == "some-model.gguf"
    assert ns.json is True


# -- grid price (remote provider model pricing) ----------------------------

def test_price_classified_remote_only():
    assert "price" in dispatch.REMOTE_ONLY
    assert not (set(dispatch.AGNOSTIC) & {"price"})
    assert not (set(dispatch.REMOTE_HANDLERS) & {"price"})


def test_price_parser_wires_subcommands_to_handler():
    parser = cli.build_parser()
    for argv in (
        ["price", "set", "-m", "glm-5.1", "--input", "0.3", "--output", "1.0"],
        ["price", "rm", "-m", "glm-5.1"],
        ["price", "delete", "-m", "glm-5.1"],
        ["price", "show"],
    ):
        assert parser.parse_args(argv).handler is cli.cmd_remote_price
    a = parser.parse_args(["price", "set", "-m", "m", "--input", "0.3", "--output", "1.0", "--cache", "0.05"])
    assert (a.type, a.input, a.output, a.cache) == ("chat", 0.3, 1.0, 0.05)


def test_price_set_rejects_non_chat_type():
    from cli.remote_price import cmd_remote_price

    args = SimpleNamespace(subcommand="set", type="image", model="m", input=0.1, output=0.2, cache=0.0, grid=None)
    with pytest.raises(SystemExit, match="isn't supported yet"):
        cmd_remote_price(args)


def test_relay_set_model_price_puts_with_bearer(monkeypatch, tmp_path):
    from remote import relay

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def handler(request):
        seen["method"], seen["path"] = request.method, request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"model": "glm-5.1", "provider_id": "grid:n:s"})

    _mock_relay(monkeypatch, handler)
    relay.set_model_price("https://relay.example", "AT", model="glm-5.1", modality="chat",
                          input_rate=0.3, output_rate=1.0, cache_rate=0.05)
    assert (seen["method"], seen["path"]) == ("PUT", "/relay/v1/grid/models")
    assert seen["auth"] == "Bearer AT"
    assert seen["body"] == {"model": "glm-5.1", "modality": "chat",
                            "input_rate": 0.3, "output_rate": 1.0, "cache_rate": 0.05}


def test_relay_set_model_price_403_is_systemexit(monkeypatch, tmp_path):
    from remote import relay

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _mock_relay(monkeypatch, lambda r: httpx.Response(403, text="not serving"))
    with pytest.raises(SystemExit, match=r"\(403\)"):
        relay.set_model_price("https://r", "AT", model="m", modality="chat",
                              input_rate=0.1, output_rate=0.2, cache_rate=0.0)


def test_relay_delete_model_price_encodes_model(monkeypatch, tmp_path):
    from remote import relay

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def handler(request):
        seen["method"], seen["url"] = request.method, str(request.url)
        return httpx.Response(200, json={"deleted": True})

    _mock_relay(monkeypatch, handler)
    relay.delete_model_price("https://relay.example", "AT", "z-ai/glm-5.1")
    assert seen["method"] == "DELETE"
    # the model id is percent-encoded into one path segment (no traversal)
    assert "/relay/v1/grid/models/z-ai%2Fglm-5.1" in seen["url"]


def test_relay_set_model_price_includes_metadata_when_given(monkeypatch, tmp_path):
    from remote import relay

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"model": "deepreinforce-ai/ornith-1.0-397b"})

    _mock_relay(monkeypatch, handler)
    relay.set_model_price(
        "https://relay.example", "AT", model="deepreinforce-ai/ornith-1.0-397b", modality="chat",
        input_rate=0.3, output_rate=1.0, cache_rate=0.05,
        name="Ornith 1.0 397B", maker="DeepReinforce AI", status="available", context_length=128000,
    )
    assert seen["body"] == {
        "model": "deepreinforce-ai/ornith-1.0-397b", "modality": "chat",
        "input_rate": 0.3, "output_rate": 1.0, "cache_rate": 0.05,
        "name": "Ornith 1.0 397B", "maker": "DeepReinforce AI",
        "status": "available", "context_length": 128000,
    }


def test_relay_set_model_price_omits_unset_metadata(monkeypatch, tmp_path):
    from remote import relay

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    _mock_relay(monkeypatch, handler)
    relay.set_model_price("https://relay.example", "AT", model="m", modality="chat",
                          input_rate=0.3, output_rate=1.0, cache_rate=0.0)
    # a rates-only call sends only the five rate/id keys — no name/maker/status/context_length keys at all
    assert set(seen["body"]) == {"model", "modality", "input_rate", "output_rate", "cache_rate"}


def test_price_set_end_to_end_through_cli_main(monkeypatch, tmp_path):
    """Full remote-mode round trip: parser -> dispatch -> cmd_remote_price -> relay PUT.
    A signed-in user with a running grid runs `grid price set` and we capture the wire body."""
    _seed_running_remote_grid(monkeypatch, tmp_path)
    state.set_mode("remote")
    seen = {}

    def handler(request):
        seen["method"], seen["path"] = request.method, request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"model": "glm-5.1"})

    _mock_relay(monkeypatch, handler)
    rc = cli.main([
        "price", "set", "-m", "glm-5.1", "--input", "0.3", "--output", "1.0", "--cache", "0.05",
        "--name", "GLM 5.1", "--maker", "Z.ai", "--status", "available", "--context-length", "200000",
    ])
    assert rc == 0
    assert (seen["method"], seen["path"], seen["auth"]) == ("PUT", "/relay/v1/grid/models", "Bearer AT")
    assert seen["body"] == {
        "model": "glm-5.1", "modality": "chat",
        "input_rate": 0.3, "output_rate": 1.0, "cache_rate": 0.05,
        "name": "GLM 5.1", "maker": "Z.ai", "status": "available", "context_length": 200000,
    }


def test_price_set_403_maps_to_join_first_through_cli_main(monkeypatch, tmp_path):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    state.set_mode("remote")
    _mock_relay(monkeypatch, lambda r: httpx.Response(403, text="not serving"))
    with pytest.raises(SystemExit, match="Join it first"):
        cli.main(["price", "set", "-m", "glm-5.1", "--input", "0.3", "--output", "1.0"])


def test_price_is_gated_in_local_mode_through_cli_main(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    state.set_mode("local")
    with pytest.raises(SystemExit, match="remote-mode command"):
        cli.main(["price", "show"])


def test_capability_entry_uses_ctx_size_when_given():
    from remote import probe

    probed = {"vision": False, "tools": False, "parallel_tool_calls": False,
              "json_object": False, "json_schema": False}
    entry = probe.capability_entry(probed, 200000)
    assert entry["context_window"] == 200000
    assert entry["limits"]["max_context_tokens"] == 200000
    # falls back to the default when unknown
    default = probe.capability_entry(probed)
    assert default["context_window"] == probe.DEFAULT_CONTEXT_WINDOW


def test_capabilities_threads_ctx_size_into_envelope(monkeypatch):
    from remote import probe

    monkeypatch.setattr(probe, "probe_llama_capabilities", lambda url, model: {
        "vision": False, "tools": False, "parallel_tool_calls": False,
        "json_object": False, "json_schema": False})
    env = probe.capabilities("http://h:8081/v1", "qwen3.5:0.8b", context_window=200000)
    assert env["models"]["qwen3.5:0.8b"]["context_window"] == 200000


# -- provider heartbeat VRAM (grid provider VRAM roll-up on the grid page) --

def test_gpu_load_snapshot_sums_across_cards(monkeypatch):
    from shared.system import gpu

    monkeypatch.setattr(gpu, "enumerate_gpus", lambda timeout=3.0: [
        gpu.GpuInfo(0, "A", "550", "8.9", memory_total_mb=49152, memory_used_mb=8000, utilization_pct=10),
        gpu.GpuInfo(1, "B", "550", "8.9", memory_total_mb=131072, memory_used_mb=1000, utilization_pct=42),
    ])
    snap = gpu.load_snapshot()
    assert snap == {"gpu_count": 2.0, "memory_total_mb": 180224.0, "memory_used_mb": 9000.0, "gpu_util": 42.0}


def test_gpu_load_snapshot_empty_without_gpu(monkeypatch):
    from shared.system import gpu

    monkeypatch.setattr(gpu, "enumerate_gpus", lambda timeout=3.0: [])
    monkeypatch.setattr(gpu, "_macos_vram_mb", lambda timeout=5.0: 0.0)  # neutralize the macOS unified/discrete VRAM path
    assert gpu.load_snapshot() == {}   # no GPU anywhere → provider sends no VRAM


def test_serve_load_merges_vram_with_active_tasks(monkeypatch, tmp_path):
    from shared.system import gpu

    monkeypatch.setattr(gpu, "load_snapshot", lambda timeout=3.0: {
        "gpu_count": 1.0, "memory_total_mb": 24576.0, "memory_used_mb": 2048.0, "gpu_util": 5.0})
    state = _serve_state(monkeypatch, tmp_path)
    state.enter_inference()  # active_tasks = 1
    load = state.load()
    assert load["active_tasks"] == 1
    assert load["memory_total_mb"] == 24576.0 and load["gpu_count"] == 1.0


def test_serve_load_omits_vram_without_gpu(monkeypatch, tmp_path):
    from shared.system import gpu

    monkeypatch.setattr(gpu, "load_snapshot", lambda timeout=3.0: {})
    load = _serve_state(monkeypatch, tmp_path).load()
    assert load == {"active_tasks": 0, "platform": "linux"}   # no VRAM keys when no GPU; platform always present


def _poll_loop_state_and_poll(job_then_none):
    """A minimal serve state + a poll_once double that yields `job_then_none` then stops the loop."""
    from remote import serve

    state = SimpleNamespace(stop=threading.Event())
    queue = list(job_then_none)

    def fake_poll(_state, **_kwargs):
        if queue:
            return queue.pop(0)
        _state.stop.set()  # nothing left to serve — let the loop exit
        return None

    monkey_targets = {"poll_once": fake_poll, "handle_job": lambda _s, _job: None}
    return serve, state, monkey_targets


def test_poll_loop_is_silent_on_success_by_default(monkeypatch, capsys):
    # A healthy poll (a served job, then an empty 204) must not write to the engine log — only
    # errors and job failures are recorded, so the log stays quiet unless something is wrong.
    serve, state, targets = _poll_loop_state_and_poll(
        [{"transaction_id": "txn-1", "body": {"model": "qwen3:0.6b"}}, None]
    )
    monkeypatch.setattr(serve, "_DEBUG", False)
    for name, fn in targets.items():
        monkeypatch.setattr(serve, name, fn)

    serve._poll_loop(state)

    assert "[engine]" not in capsys.readouterr().err


def test_poll_loop_traces_each_cycle_when_debug_enabled(monkeypatch, capsys):
    # With GRID_ENGINE_DEBUG set, every cycle is traced: a claimed job (with its txn + model) and
    # an empty 204 both emit an `[engine]` line so the poll loop is visible while debugging.
    serve, state, targets = _poll_loop_state_and_poll(
        [{"transaction_id": "txn-1", "body": {"model": "qwen3:0.6b"}}, None]
    )
    monkeypatch.setattr(serve, "_DEBUG", True)
    for name, fn in targets.items():
        monkeypatch.setattr(serve, name, fn)

    serve._poll_loop(state)

    err = capsys.readouterr().err
    assert "[engine] poll: job txn=txn-1 model='qwen3:0.6b' handled" in err
    assert "[engine] poll: no job (204)" in err
