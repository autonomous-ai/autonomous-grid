from __future__ import annotations

import argparse
import base64
import contextlib
import errno
import json
import os
import shutil
import stat
import subprocess
import tarfile
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
from cli import codex_signin as _codex_signin  # noqa: F401 - binds `cli.codex_signin` for monkeypatching
from cli import dispatch
from local import config
from shared import paths
from shared import state
from local import runtime
from shared.agent import installer as agent_installer
from shared.engine import comfyui, installer, launcher
from shared.system import arch
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

    assert api_catalog.supported_kinds() == ("codex", "openai")
    for kind, whitelist in api_catalog.WHITELISTS.items():
        names = [entry.vendor_name for entry in whitelist.entries]
        assert all(names), f"{kind} has an entry with an empty vendor name"
        assert len(set(names)) == len(names), f"{kind} has duplicate vendor names"
        for entry in whitelist.entries:
            assert entry.context_window > 0
            assert api_catalog.advertised_name(kind, entry) == f"{kind}:{entry.vendor_name}"
        date.fromisoformat(whitelist.last_verified)  # dated, ISO format
        assert whitelist.base_url.startswith("https://"), f"{kind} needs a vendor base URL"
        assert not whitelist.base_url.endswith("/"), f"{kind} base URL must not end with '/'"


def test_catalog_api_codex_prints_per_tier_whitelist(monkeypatch, capsys):
    """`grid catalog --api codex` prints the static per-tier table — offline, with no credential
    (ADR 0012 D-a posture; the sign-in and the live probe belong to `grid join`). Each populated
    tier prints its rows; the fallback rule for every other tier is stated naming the minimal
    tier; and the consumer-facing facts are disclosed: these models serve the `responses`
    endpoint (an external Codex app, not `grid chat`), requests leave the grid, and jobs spend
    the seat's own monthly allowance."""
    def _no_network(*args, **kwargs):
        raise AssertionError("`grid catalog --api` must not touch the network")

    monkeypatch.setattr(httpx, "Client", _no_network)
    monkeypatch.setattr(httpx, "request", _no_network)
    monkeypatch.setattr(httpx, "stream", _no_network)

    rc = cli.cmd_catalog(argparse.Namespace(api="codex", json=False))

    out = capsys.readouterr().out
    assert rc == 0
    assert "Unknown" not in out
    assert api_catalog.CODEX_LAST_VERIFIED in out
    for tier, entries in api_catalog.CODEX_TIER_MODELS.items():
        assert f"\n{tier}:\n" in out  # a per-tier section header
        for entry in entries:
            assert api_catalog.advertised_name("codex", entry) in out
            assert f"{entry.context_window:,}" in out
    # Chat-dialect capability words never appear on codex rows (honest passthrough claims only).
    assert "json" not in out and "structured" not in out
    assert api_catalog.CODEX_MINIMAL_TIER in out  # the fallback rule names the minimal tier
    assert "responses" in out  # the endpoint disclosure...
    assert "grid chat" in out  # ...and what NOT to point at it
    assert "leave the grid" in out  # provenance disclosure (openai parity)
    assert "allowance" in out  # flat-rate seat: jobs spend the provider's own allowance


def test_catalog_api_still_rejects_a_kind_that_really_is_unknown(capsys):
    """The other half of the split: a kind absent from the catalog keeps its "Unknown" message,
    listing what IS supported. This is the assertion that stops the fix above from degrading into
    "accept anything"."""
    with pytest.raises(SystemExit) as exc:
        cli.cmd_catalog(argparse.Namespace(api="anthropic", json=False))

    msg = str(exc.value)
    assert "Unknown" in msg and "anthropic" in msg
    assert "codex" in msg and "openai" in msg  # the supported list


def test_catalog_api_codex_json_emits_per_tier_contract(capsys):
    """`--json` for codex speaks `tiers`, not the flat `models` (the reshape is safe: v0.2.1
    predates every codex commit, so the interim flat-empty shape never shipped in a release).
    Entries carry NO chat-dialect keys — json-mode/structured-outputs are chat notions a
    Responses passthrough cannot honestly claim — and `supports_parallel_tool_calls` is the one
    shared derivation the capability envelope also uses, so the two surfaces cannot disagree."""
    rc = cli.cmd_catalog(argparse.Namespace(api="codex", json=True))

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["kind"] == "codex"
    assert payload["last_verified"] == api_catalog.CODEX_LAST_VERIFIED
    assert payload["endpoints"] == ["responses"]
    assert payload["minimal_tier"] == api_catalog.CODEX_MINIMAL_TIER
    assert "models" not in payload  # per-tier kinds speak `tiers`
    assert set(payload["tiers"]) == set(api_catalog.CODEX_TIER_MODELS)
    free = payload["tiers"]["free"]
    assert len(free) == len(api_catalog.CODEX_TIER_MODELS["free"])
    first, first_entry = free[0], api_catalog.CODEX_TIER_MODELS["free"][0]
    assert first["advertised"] == api_catalog.advertised_name("codex", first_entry)
    assert first["vendor_name"] == first_entry.vendor_name
    assert first["context_window"] == first_entry.context_window
    assert first["supports_tools"] is True
    assert first["supports_vision"] is True
    assert first["supports_parallel_tool_calls"] is True
    assert first["notes"] == first_entry.notes
    assert "supports_json_mode" not in first
    assert "supports_structured_outputs" not in first


def test_api_whitelist_key_kinds_name_their_env_var():
    """A kind whose credential IS an API key must name the env var it is read from — that is the
    first step of ADR 0012 D-c's env → stored → prompt precedence. `codex` is exempt and must be:
    ADR 0015 D-c gives an OAuth seat no env-var input path at all, precisely so a stray
    CODEX_API_KEY can never masquerade as a signed-in subscription."""
    for kind, whitelist in api_catalog.WHITELISTS.items():
        if kind == "codex":
            continue
        assert whitelist.entries, f"{kind} whitelist must not be empty"
        assert whitelist.env_var, f"{kind} needs the env var its key is read from"


def test_codex_whitelist_has_no_env_var_and_no_output_cap():
    """The two fields an OAuth seat cannot have (issue 04), kept true now that issue 05 populates
    the row's model entries (the tier union — see test_codex_tier_whitelist_integrity):

    * no `env_var` — ADR 0015 D-c: no env-var input path, so nothing in the environment can pose as
      a seat, and `_api_bearers` has no name to synthesise a fallback from.
    * no `max_output_param` — facts.md #1: this backend accepts NO output-cap parameter at all
      (`max_tokens`, `max_output_tokens` and `max_completion_tokens` each 400 "Unsupported
      parameter"), so there is no name to translate to. All three, plus `temperature`, are refused
      before the round-trip instead.
    """
    codex = api_catalog.WHITELISTS["codex"]

    assert codex.env_var is None
    assert codex.max_output_param is None
    assert codex.base_url == "https://chatgpt.com/backend-api/codex"
    assert set(codex.unsupported_params) == {
        "max_tokens", "max_output_tokens", "max_completion_tokens", "temperature",
    }
    assert codex.entries  # issue 05: populated from the tier table


def test_codex_tier_whitelist_integrity():
    """The per-tier codex table (issue 05). Guards the D-f data rules: every populated tier is
    real vendor `PlanType` vocabulary; rows are non-empty, duplicate-free, and inside the flat
    union (`entries` IS the union, so `find_advertised` resolves every codex model); the minimal
    tier's row exists (the fallback every unknown/unverified tier degrades to); the hidden
    `codex-auto-review` slug is excluded everywhere (facts.md #5, visibility: "hide"); and the
    table is dated + carries the probe's pinned client version."""
    from datetime import date

    tiers = api_catalog.CODEX_TIER_MODELS
    assert tiers, "at least one tier must be populated"
    assert set(tiers) <= api_catalog.CODEX_PLAN_TYPES, "tier keys are vendor vocabulary, not ours"
    assert api_catalog.CODEX_MINIMAL_TIER in tiers, "the minimal fallback row must be populated"

    union = {entry.vendor_name for entry in api_catalog.WHITELISTS["codex"].entries}
    seen: set[str] = set()
    by_name: dict = {}
    for tier, entries in tiers.items():
        names = [entry.vendor_name for entry in entries]
        assert names, f"tier {tier!r} must not be an empty row (absent means unverified)"
        assert all(names) and len(set(names)) == len(names), f"tier {tier!r} rows must be unique"
        assert set(names) <= union, f"tier {tier!r} names something the flat union lacks"
        # issue 03: each row is a valid, gap-free vendor_rank source — positions are exactly 1..N
        # (no gaps, no duplicate slots), so codex_vendor_rank is a total order over the row.
        ranks = [api_catalog.codex_vendor_rank(tier, name) for name in names]
        assert ranks == list(range(1, len(names) + 1)), f"tier {tier!r} is not a gap-free 1..N ranking"
        for entry in entries:
            # A vendor_name shared across tiers must be the SAME entry: the flat union keeps the
            # first occurrence (`_codex_tier_union` setdefault), so a colliding second-tier entry
            # with different specs would silently lose to it in every serve-time lookup
            # (silent-failure review, latent trap — inert while one tier exists).
            assert by_name.setdefault(entry.vendor_name, entry) == entry, (
                f"{entry.vendor_name!r} appears in two tiers with different specs"
            )
        seen |= set(names)
    assert seen == union, "WHITELISTS['codex'].entries must be exactly the union of the tier rows"
    assert "codex-auto-review" not in union  # visibility: "hide" — never advertised

    # The free row is the live-verified 2026-07-15 set, in the vendor's priority order.
    free = [entry.vendor_name for entry in tiers["free"]]
    assert free == ["gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4-mini"]
    for entry in api_catalog.WHITELISTS["codex"].entries:
        assert entry.context_window == 272_000
        assert entry.supports_tools and entry.supports_vision
        # Chat-dialect notions — a passthrough kind cannot honestly claim them (issue 05).
        assert not entry.supports_json_mode and not entry.supports_structured_outputs

    date.fromisoformat(api_catalog.CODEX_LAST_VERIFIED)
    assert api_catalog.CODEX_CLIENT_VERSION  # the `GET /models?client_version=…` pin


def test_codex_tier_entries_selects_row_or_minimal():
    """D-f's selection rule as one pure lookup: a populated tier gets its row; a missing (None),
    unrecognized ("banana"), or known-but-unverified ("plus") tier degrades to the minimal row —
    the join never widens on a guess. The warn wording for the three degrade cases is the CLI's
    (they differ only in what the operator is told, not in what is advertised)."""
    free_row = api_catalog.CODEX_TIER_MODELS["free"]
    minimal = api_catalog.CODEX_TIER_MODELS[api_catalog.CODEX_MINIMAL_TIER]

    assert api_catalog.codex_tier_entries("free") == free_row
    assert api_catalog.codex_tier_entries(None) == minimal      # vendor said nothing
    assert api_catalog.codex_tier_entries("banana") == minimal  # outside the vendor vocabulary
    assert api_catalog.codex_tier_entries("plus") == minimal    # known tier, row unverified
    assert "plus" in api_catalog.CODEX_PLAN_TYPES  # pin: "plus" IS known — its degrade differs from banana's only in wording


def test_codex_vendor_rank_from_tier_row(monkeypatch):
    """The join-time capability rank (issue 03 / ADR 0016): a codex model's 1-based position within
    the seat's EFFECTIVE tier row — 1 = the row's most-capable head, the curated order we own. A
    model absent from that row has no rank (None): drift omits the fact, never fabricates a
    position. The source is the tier ROW, not the flat union — proven by a synthetic second tier
    whose order differs from the free row's."""
    # The live free row, in curated order: terra, luna, gpt-5.5, gpt-5.4-mini.
    assert api_catalog.codex_vendor_rank("free", "gpt-5.6-terra") == 1
    assert api_catalog.codex_vendor_rank("free", "gpt-5.6-luna") == 2
    assert api_catalog.codex_vendor_rank("free", "gpt-5.5") == 3
    assert api_catalog.codex_vendor_rank("free", "gpt-5.4-mini") == 4
    # Absent from the row → no rank (a drifted/unknown model omits the fact, never invents one).
    assert api_catalog.codex_vendor_rank("free", "gpt-5.6-nonesuch") is None
    # The tier degrade is codex_tier_entries': None (vendor silent), unrecognized ("banana"), and
    # known-but-unverified ("plus") all fall to the minimal (free) row, so the rank is computed
    # against the row the seat actually advertises.
    assert api_catalog.codex_vendor_rank(None, "gpt-5.6-terra") == 1
    assert api_catalog.codex_vendor_rank("banana", "gpt-5.6-terra") == 1
    assert api_catalog.codex_vendor_rank("plus", "gpt-5.6-terra") == 1  # known, unverified → free

    # Rank follows the SEAT'S TIER ROW, not the flat union. Synthetic second tier (the real table
    # has only `free`): a `plus` row that REVERSES the capability order must rank by ITS OWN order.
    # Built from REAL entries so the frozen union/whitelist still resolves the names.
    free = api_catalog.CODEX_TIER_MODELS["free"]
    terra, luna, big, mini = free  # by curated order
    monkeypatch.setattr(api_catalog, "CODEX_TIER_MODELS", {"free": free, "plus": (mini, big, luna, terra)})
    assert api_catalog.codex_vendor_rank("plus", mini.vendor_name) == 1   # head of the plus row
    assert api_catalog.codex_vendor_rank("plus", terra.vendor_name) == 4  # tail of the plus row
    assert api_catalog.codex_vendor_rank("free", terra.vendor_name) == 1  # free row unchanged


def test_api_whitelist_endpoints_per_kind():
    """Which relay endpoint a kind's models serve — the wire contract half this repo owns. The
    values are hand-duplicated with grid-src's `provider_supports` filter (absent ⇒ chat-only,
    old CLIs fail closed); the literal `"responses"` must match its `endpoint_path` byte-for-byte
    (CLAUDE.local.md lockstep rule)."""
    assert api_catalog.WHITELISTS["openai"].endpoints == ("chat/completions", "responses")
    assert api_catalog.WHITELISTS["codex"].endpoints == ("responses",)


def test_codex_kind_constant_is_defined_in_shared_and_reexported():
    """`CODEX_KIND` lives in shared/ (the run-record concurrency rule needs it and shared/ must
    not import remote/); remote/api_keys re-exports it so its existing call sites keep working
    without a second definition to drift. Equality, not `is`: CPython interns short string
    literals, so `is` would pass even against an independent literal — it proves nothing here.
    What this guards is a rename on either side."""
    from remote import api_keys

    assert api_catalog.CODEX_KIND == "codex"
    assert api_keys.CODEX_KIND == api_catalog.CODEX_KIND


def test_responses_only_kind_flags_only_kinds_that_cannot_serve_chat():
    """The `grid chat` pre-flight asks one question — "is this model namespaced under a kind that
    cannot serve chat/completions?" — data-driven from the whitelist's `endpoints`, never a hardcoded
    kind name. openai now serves `responses` TOO (issue 03), but is still NOT flagged: it is
    responses-*capable*, not responses-*only*, because it also serves chat — so `grid chat` still
    works against it. Only a kind with no chat endpoint at all (codex) is flagged. Anything that isn't
    an API namespace (a hardware model whose NAME merely contains a colon, no namespace at all) is None."""
    assert api_catalog.responses_only_kind("codex:gpt-5.5") == "codex"  # responses-only → refuse chat
    assert api_catalog.responses_only_kind("codex:") == "codex"  # still a codex-namespaced request
    assert api_catalog.responses_only_kind("openai:gpt-5.5") is None  # serves responses AND chat → not flagged
    assert api_catalog.responses_only_kind("llama3:8b") is None  # colon, but not an API kind
    assert api_catalog.responses_only_kind("gpt-5.5") is None  # no namespace
    assert api_catalog.responses_only_kind("") is None


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


# ---------------------------------------------------------------------------
# codex seat claims (ADR 0015) — the OAuth access token is a JWT carrying the seat's identity
# ---------------------------------------------------------------------------


def _codex_jwt(auth_claim=None, /, **top_level):
    """A synthetic codex access token. Never a real one: these tests must never carry a secret.

    Mirrors the real shape — three segments, base64url payload with the '=' padding stripped, and
    the seat facts nested under the vendor's namespaced claim.
    """
    import base64
    import json as _json

    claims = dict(top_level)
    if auth_claim is not None:
        claims["https://api.openai.com/auth"] = auth_claim
    body = base64.urlsafe_b64encode(_json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


def test_codex_seat_decodes_a_synthetic_seat_token():
    """The claim SHAPE is real (verified against a live seat 2026-07-15, spike 01); the values are
    synthetic. Decoding yields the account id the forward header needs, plus the seat's tier."""
    from remote import codex_auth

    seat = codex_auth.decode_seat(
        _codex_jwt(
            {
                "chatgpt_account_id": "acct-synthetic-0001",
                "chatgpt_plan_type": "free",
                "chatgpt_account_user_id": "user-synthetic-0002",
                "chatgpt_user_id": "user-synthetic-0003",
                "chatgpt_compute_residency": "no_constraint",
                "user_id": "user-synthetic-0004",
                "amr": [],
                "localhost": False,
            },
            exp=2_000_000_000,
            iss="https://auth.openai.com",
        )
    )

    assert seat.account_id == "acct-synthetic-0001"
    assert seat.plan_type == "free"
    assert seat.expires_at == 2_000_000_000


def test_codex_seat_reads_the_namespaced_claim_not_the_top_level():
    """The seat facts live INSIDE the namespaced claim. An implementation that reads the top level
    passes the happy-path test by accident, so pin it with decoys under the identical names."""
    from remote import codex_auth

    seat = codex_auth.decode_seat(
        _codex_jwt(
            {"chatgpt_account_id": "acct-real", "chatgpt_plan_type": "pro"},
            chatgpt_account_id="acct-DECOY",
            chatgpt_plan_type="DECOY",
        )
    )

    assert seat.account_id == "acct-real"
    assert seat.plan_type == "pro"


def test_codex_seat_plan_type_is_none_when_absent_or_not_a_string():
    """Tier only chooses which models we advertise, and ADR 0015 D-f already has a safe default for
    not knowing it (the minimal whitelist). Degrade to None — never fail a working seat's join."""
    from remote import codex_auth

    assert (
        codex_auth.decode_seat(_codex_jwt({"chatgpt_account_id": "a"})).plan_type
        is None
    )
    assert (
        codex_auth.decode_seat(
            _codex_jwt(
                {"chatgpt_account_id": "a", "chatgpt_plan_type": {"nested": "junk"}}
            )
        ).plan_type
        is None
    )


def test_codex_seat_expires_at_is_none_when_exp_is_missing_or_not_an_int():
    """`exp` only triggers ADR 0015 D-d's proactive refresh, which has its own fallback, so a bad
    one degrades to None. `exp: true` is the trap: bool IS an int in Python, so an unguarded
    implementation yields expires_at=1 → epoch 1970 → refreshing eagerly forever."""
    from remote import codex_auth

    base = {"chatgpt_account_id": "a"}
    assert codex_auth.decode_seat(_codex_jwt(base)).expires_at is None
    assert codex_auth.decode_seat(_codex_jwt(base, exp="soon")).expires_at is None
    assert codex_auth.decode_seat(_codex_jwt(base, exp=True)).expires_at is None
    assert (
        codex_auth.decode_seat(_codex_jwt(base, exp=2_000_000_000)).expires_at
        == 2_000_000_000
    )


def test_codex_seat_requires_a_header_safe_account_id():
    """Account id is the one field with no safe default: it is the Chatgpt-Account-Id header on
    every forward, so a token without one identifies no seat and cannot serve.

    It is checked for header-SAFETY, not just truthiness, because it is spent as a header value and
    the forward path uses httpx — which (unlike urllib) will send a header containing CRLF. Reaching
    this needs a forged token, so it is defence in depth, not a live hole.
    """
    from remote import codex_auth

    for claim in (
        {},
        {"chatgpt_account_id": ""},
        {"chatgpt_account_id": None},
        {"chatgpt_account_id": 12345},
        {"chatgpt_account_id": "   "},  # blank: would send an empty header
        {"chatgpt_account_id": "acct-a\r\nX-Injected: pwned"},  # CRLF header injection
        {"chatgpt_account_id": "acct-a\tb"},
    ):
        with pytest.raises(codex_auth.CodexTokenError):
            codex_auth.decode_seat(_codex_jwt(claim))

    # A legitimate non-ASCII id is NOT collateral damage — printable is the bar, not ASCII.
    assert codex_auth.decode_seat(
        _codex_jwt({"chatgpt_account_id": "acct-日本語"})
    ).account_id == ("acct-日本語")


def test_codex_seat_rejects_a_token_that_is_not_a_string():
    """The store's loader is `str | None`-shaped (remote/api_keys.load_key), and ADR 0015 D-d
    accepts a crash mid-rotation as a residual risk — so a half-written bundle really can hand this
    a None. Without a guard that is an AttributeError escaping the module's whole error contract,
    and a caller that catches CodexTokenError (as the design invites) would not catch it."""
    from remote import codex_auth

    for value in (None, 12345, b"header.payload.sig", ["header", "payload", "sig"]):
        with pytest.raises(codex_auth.CodexTokenError) as caught:
            codex_auth.decode_seat(value)
        assert caught.value.reason == "not-a-string"


def test_codex_seat_error_reasons_come_from_the_closed_vocabulary():
    """`.reason` exists so a vendor claim-rename is distinguishable from a corrupt store — both say
    'sign in again' to the operator, and signing in again fixes neither. It is drawn from a closed
    set precisely so it can never smuggle a token value into a log, and it stays out of `args` so
    `repr()` keeps leaking nothing."""
    from remote import codex_auth

    import base64
    import json as _json

    def _raw(payload):
        body = (
            base64.urlsafe_b64encode(_json.dumps(payload).encode()).decode().rstrip("=")
        )
        return f"header.{body}.signature"

    # One specimen per reason — every constant in REASONS must be reachable, and each case must
    # report its OWN reason (a single collapsed code would be no better than the message).
    specimens = {
        "not-a-string": None,
        "bad-segment-count": "header.payload",
        "undecodable-payload": "header.!!!not-base64!!!.sig",
        "payload-not-an-object": _raw([1, 2]),
        "claim-missing": _codex_jwt(None, sub="u1"),
        "account-id-unusable": _codex_jwt({"chatgpt_plan_type": "free"}),
    }
    assert set(specimens) == codex_auth.REASONS, (
        "every reason needs a specimen (and vice versa)"
    )

    constant_text = str(codex_auth.CodexTokenError("not-a-string"))
    for reason, token in specimens.items():
        with pytest.raises(codex_auth.CodexTokenError) as caught:
            codex_auth.decode_seat(token)
        assert caught.value.reason == reason
        # The operator-facing text is the same constant regardless of reason...
        assert str(caught.value) == constant_text
        # ...and the reason stays out of repr(), which the leak test also scans.
        assert reason not in repr(caught.value)

    # The closed vocabulary is enforced by the type itself, not merely by convention: a raise site
    # that ever passed a token-derived string (defeating the leak guarantee) fails loudly here.
    with pytest.raises(ValueError):
        codex_auth.CodexTokenError("a-reason-not-in-the-vocabulary")


def test_codex_seat_rejects_malformed_tokens():
    """Every shape that cannot yield a seat. The last two are the silent-pass traps: a 4-segment
    token still has a decodable segment 1, and a JSON payload that isn't an object still parses —
    both sail past a naive split-and-decode."""
    import base64
    import json as _json

    from remote import codex_auth

    def _payload(value):
        return (
            base64.urlsafe_b64encode(_json.dumps(value).encode()).decode().rstrip("=")
        )

    cases = {
        "empty": "",
        "not a jwt": "opaque-not-a-jwt",
        "one segment": "header",
        "two segments": f"header.{_payload({'a': 1})}",
        "four segments": f"header.{_payload({'a': 1})}.sig.extra",
        "payload not base64": "header.!!!not-base64!!!.sig",
        "payload not json": f"header.{base64.urlsafe_b64encode(b'not json').decode().rstrip('=')}.sig",
        "payload is a list": f"header.{_payload([1, 2])}.sig",
        "payload is a number": f"header.{_payload(123)}.sig",
        "payload is null": f"header.{_payload(None)}.sig",
        "claim missing": _codex_jwt(None, sub="u1"),
        "claim not a dict": _codex_jwt("not-a-dict"),
        # A deeply-nested payload makes json.loads raise RecursionError (a RuntimeError, NOT a
        # ValueError) — the decoder must still answer with CodexTokenError, never a raw crash.
        "deeply nested payload": "header."
        + base64.urlsafe_b64encode(b"[" * 100_000 + b"]" * 100_000).decode().rstrip("=")
        + ".sig",
    }
    for label, token in cases.items():
        # try/except/else rather than `pytest.raises`, so the per-case label survives a regression
        # (pytest.raises alone would report only "DID NOT RAISE", not which of the 12 shapes leaked
        # through). The `pytest.fail` is reachable here — it runs when decode_seat wrongly returns.
        try:
            codex_auth.decode_seat(token)
        except codex_auth.CodexTokenError:
            continue
        pytest.fail(f"{label!r} should not decode to a seat")


def test_codex_seat_accepts_unpadded_base64url():
    """A JWT strips base64 '=' padding, so the decoder must restore it for every residue class —
    otherwise decoding works or fails depending on how long the account id happens to be."""
    from remote import codex_auth

    for filler in ("a", "aa", "aaa", "aaaa"):
        token = _codex_jwt({"chatgpt_account_id": f"acct-{filler}"})
        assert "=" not in token.split(".")[1]  # the helper really does strip padding
        assert codex_auth.decode_seat(token).account_id == f"acct-{filler}"


def test_codex_seat_errors_never_leak_the_token():
    """Issue 04's AC: no code path prints a token value or raw JWT. A plain `token not in str(exc)`
    is too weak — it misses a truncated echo and anything reachable through the exception chain —
    so slide an 8-char window over everything a human could see."""
    import traceback

    from remote import codex_auth

    import base64

    # A payload that decodes as base64 but NOT as JSON is the one input that chains an exception
    # carrying content: json.JSONDecodeError.doc holds the DECODED PAYLOAD — exactly where the
    # account id and the operator's email live. It has to be in this list, not merely in the
    # rejects-malformed-tokens table, because it is the only branch where the chain matters.
    not_json = base64.urlsafe_b64encode(b'{"chatgpt_account_id": "acct-SECRET-42" ')
    tokens = (
        _codex_jwt({"chatgpt_plan_type": "free"}),  # decodable, no account id -> raises
        _codex_jwt({"chatgpt_account_id": "acct-SECRET-42\r\nX-Evil: 1"}),  # unusable id
        f"header.{not_json.decode().rstrip('=')}.sig",  # base64 ok, JSON fails -> chains
        "header.!!!not-base64!!!.sig",
        "opaque-not-a-jwt",
    )
    for token in tokens:
        try:
            codex_auth.decode_seat(token)
        except codex_auth.CodexTokenError as exc:
            rendered = (
                "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                + str(exc)
                + repr(exc)
            )
            leaked = sum(1 for i in range(len(token) - 7) if token[i : i + 8] in rendered)
            # Report a COUNT, never the fragment itself: a failing assertion message is an output
            # channel too, and echoing the offending bytes would leak them into the CI log.
            assert not leaked, f"{leaked} fragment(s) of the token reached a rendered error"
            # NEITHER link may survive. `from None` alone would leave __context__ reachable, and
            # with it `.doc` — so decode_seat raises outside the except block instead.
            assert exc.__cause__ is None
            assert exc.__context__ is None
        else:
            pytest.fail("expected CodexTokenError")


def test_codex_seat_repr_does_not_leak_the_account_id():
    """The account id is the operator's identity, held to the same bar as the token (issue 04): it
    may not reach a log, a terminal, or a run record.

    A plain dataclass repr prints every field, so `logger.debug(f"joined {seat}")` — the most
    natural line anyone writes when wiring up issue 05/06 — would ship it with no bug, no edge case
    and no regression required. The error-path leak test cannot catch this: it is the SUCCESS path.
    """
    from remote import codex_auth

    seat = codex_auth.decode_seat(
        _codex_jwt({"chatgpt_account_id": "acct-SECRET-42", "chatgpt_plan_type": "free"})
    )

    assert seat.account_id == "acct-SECRET-42"  # readable by the code that needs it...
    for rendered in (repr(seat), str(seat), f"{seat}"):  # ...never by anything that prints it
        assert "SECRET" not in rendered
    # The non-secret fields stay visible: this is redaction, not an opaque blob.
    assert "free" in repr(seat)


def test_codex_seat_carries_no_verification_verdict():
    """The decoder does NOT verify the JWT signature, so nothing it returns may look like an
    authorization result. Structural guard: no bool field exists for a future caller to branch on
    as though the seat had been validated."""
    import dataclasses

    from remote import codex_auth

    fields = {f.name: f for f in dataclasses.fields(codex_auth.CodexSeat)}
    assert set(fields) == {"account_id", "plan_type", "expires_at"}
    # codex_auth enables PEP 563, so every `f.type` is the annotation's SOURCE TEXT, not the type
    # object — `f.type is bool` would be dead code. Match the text, which also catches
    # `bool | None` and `Optional[bool]`.
    assert all(isinstance(f.type, str) for f in fields.values()), (
        "PEP 563 assumption broke"
    )
    assert not any("bool" in f.type for f in fields.values())

    seat = codex_auth.decode_seat(_codex_jwt({"chatgpt_account_id": "a"}))
    with pytest.raises(dataclasses.FrozenInstanceError):
        seat.account_id = "mutated"


def test_codex_seat_decodes_an_expired_token():
    """The decoder never reads the clock. ADR 0015 D-d's refresh must read the account id OUT of an
    already-expired token to rotate it — a decoder that rejected expired tokens would brick exactly
    the case refresh exists for. Expiry is the caller's decision, not the parser's."""
    from remote import codex_auth

    seat = codex_auth.decode_seat(
        _codex_jwt({"chatgpt_account_id": "acct-x", "chatgpt_plan_type": "free"}, exp=1)
    )

    assert seat.account_id == "acct-x"
    assert seat.expires_at == 1


# ---------------------------------------------------------------------------
# codex join probe (ADR 0015 D-f, issue 05) — one free GET {base}/models per changed join.
# Every test here mocks the vendor via httpx.MockTransport: this suite never makes a network call.
# ---------------------------------------------------------------------------


def _codex_bundle(plan_type="free"):
    from remote import codex_oauth

    return codex_oauth.CodexBundle(
        access_token="tok-access",
        refresh_token="tok-refresh",
        account_id="acct-1",
        plan_type=plan_type,
        last_refresh=0,
    )


def _mock_probe(monkeypatch, handler, _real=httpx.Client):
    """The `_mock_vendor` pattern for the codex probe: a REAL httpx.Client with MockTransport
    injected, capturing the whole request for the URL/header asserts."""
    seen = {}

    def wrapped(request):
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["headers"] = dict(request.headers)
        seen.setdefault("calls", 0)
        seen["calls"] += 1
        return handler(request)

    monkeypatch.setattr(
        httpx, "Client",
        lambda *a, **k: _real(*a, **{**k, "transport": httpx.MockTransport(wrapped)}),
    )
    return seen


def _probe(monkeypatch, handler):
    """Run probe_seat against a mocked vendor; returns (result_or_exc, seen)."""
    from remote import codex_probe

    seen = _mock_probe(monkeypatch, handler)
    slugs = codex_probe.probe_seat(
        _codex_bundle(),
        base_url=api_catalog.WHITELISTS["codex"].base_url,
        client_version=api_catalog.CODEX_CLIENT_VERSION,
    )
    return slugs, seen


def test_codex_probe_sends_the_verified_request_and_returns_visible_slugs(monkeypatch):
    """The probe is the ONE vendor call a changed join makes, and it is free (facts.md B1). It
    sends the verified URL + `client_version` pin and exactly the five headers the real client
    sends (spike probe.py `headers_for`). It returns the seat's visible slugs — a
    `visibility: "hide"` row (codex-auto-review) is never advertised — deduped, vendor order
    preserved; the ~42 unknown fields per model ride along ignored. A CF-RAY header on the 200
    is NOT a Cloudflare block: CF-RAY rides every response, including successes (facts.md B4)."""
    body = {"models": [
        {"slug": "gpt-5.6-terra", "visibility": "list", "context_window": 272000, "unknown_field": 1},
        {"slug": "gpt-5.5", "visibility": "list"},
        {"slug": "gpt-5.5", "visibility": "list"},            # vendor dupe → deduped
        {"slug": "codex-auto-review", "visibility": "hide"},  # hidden → excluded
    ]}

    slugs, seen = _probe(monkeypatch, lambda request: httpx.Response(
        200, json=body, headers={"CF-RAY": "a1b94a2e9cacfd7c-SIN"},
    ))

    assert slugs == ("gpt-5.6-terra", "gpt-5.5")
    assert seen["calls"] == 1  # one request, no retry
    assert seen["method"] == "GET"
    assert seen["url"] == (
        "https://chatgpt.com/backend-api/codex/models"
        f"?client_version={api_catalog.CODEX_CLIENT_VERSION}"
    )
    assert seen["headers"]["authorization"] == "Bearer tok-access"
    assert seen["headers"]["chatgpt-account-id"] == "acct-1"
    assert seen["headers"]["originator"] == "codex_cli_rs"
    assert seen["headers"]["user-agent"] == "codex_cli_rs"
    assert seen["headers"]["accept"] == "application/json"


def test_codex_probe_auth_failures_raise_the_typed_seat_rejection(monkeypatch):
    """401, and 403 WITHOUT the Cloudflare marker, are the seat's fault — the ONE class the join
    may catch to offer a stored-but-dead seat a fresh sign-in. Typed, not SystemExit: the join
    must never string-match a terminal message to decide whether a retry applies. The typed error
    carries the status only — never vendor body text (unbounded shape on an auth host)."""
    from remote import codex_probe

    for status in (401, 403):
        with pytest.raises(codex_probe.SeatRejected) as exc:
            _probe(monkeypatch, lambda request, s=status: httpx.Response(s, json={"detail": "denied"}))
        assert exc.value.status_code == status
        assert "denied" not in str(exc.value)


def test_codex_probe_cf_challenge_keys_on_cf_mitigated_not_cf_ray(monkeypatch):
    """403 + `Cf-Mitigated` = Cloudflare challenged this machine's egress IP. Refusing the join
    names that cause — a datacenter/VPS address typically cannot serve a seat, and signing in
    again cannot fix an IP, so this is deliberately NOT the auth class. Keyed on `Cf-Mitigated`,
    never on CF-RAY, which rides every response including 200s (facts.md B4; the happy-path test
    pins a CF-RAY-bearing 200 succeeding)."""

    with pytest.raises(SystemExit) as exc:
        _probe(monkeypatch, lambda request: httpx.Response(
            403,
            text="<html>Just a moment...</html>",
            headers={"Cf-Mitigated": "challenge", "CF-RAY": "a1b94a2e9cacfd7c-SIN"},
        ))

    msg = str(exc.value)
    assert "egress IP" in msg
    assert "Nothing was joined." in msg
    assert "sign in" not in msg.lower()  # an IP block is not fixed by re-authenticating
    assert "Just a moment" not in msg  # never the challenge page's HTML


def test_codex_probe_remaining_classes_have_distinct_terminal_messages(monkeypatch):
    """429 (seat rate-limited — wait), 5xx (vendor outage — not the operator's fault), 400
    (contract drift — check for a newer release), transport (unreachable). Together with the CF
    message these are the taxonomy's terminal classes; each must be distinct, name nothing
    joined, and the 400 class must never be worded as a tier problem (facts.md #5 — the vendor's
    refusals name the auth mode, never the tier)."""

    messages = {}
    for status in (429, 503, 400):
        with pytest.raises(SystemExit) as exc:
            _probe(monkeypatch, lambda request, s=status: httpx.Response(s, json={"detail": "d"}))
        messages[status] = str(exc.value)

    def _transport_fail(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(SystemExit) as exc:
        _probe(monkeypatch, _transport_fail)
    messages["transport"] = str(exc.value)

    with pytest.raises(SystemExit) as exc:
        _probe(monkeypatch, lambda request: httpx.Response(
            403, text="x", headers={"Cf-Mitigated": "challenge"},
        ))
    messages["cf"] = str(exc.value)

    assert len(set(messages.values())) == len(messages)  # pairwise distinct
    for msg in messages.values():
        assert "Nothing was joined." in msg
        assert "tier" not in msg.lower()
    assert "rate-limited" in messages[429]
    assert "outage" in messages[503]
    assert "newer grid release" in messages[400]
    assert "Could not reach" in messages["transport"]
    assert "egress IP" in messages["cf"]


def test_codex_probe_400_detail_echo_is_bounded(monkeypatch):
    """The 400 message may quote the vendor's `detail` — the one field that tells an operator
    what drifted — but vendor text is untrusted: over-long or non-printable detail is dropped,
    never truncated into the message (ANSI escapes and newlines could forge output lines)."""

    with pytest.raises(SystemExit) as exc:
        _probe(monkeypatch, lambda request: httpx.Response(
            400, json={"detail": "Unsupported parameter: client_version"},
        ))
    assert "Unsupported parameter: client_version" in str(exc.value)

    for hostile in ("x" * 500, "line\nforged", "\x1b[31mred\x1b[0m", "", 42):
        with pytest.raises(SystemExit) as exc:
            _probe(monkeypatch, lambda request, d=hostile: httpx.Response(400, json={"detail": d}))
        msg = str(exc.value)
        assert "forged" not in msg and "\x1b" not in msg and "x" * 100 not in msg


def test_codex_probe_unreadable_listing_is_contract_drift_but_empty_is_empty(monkeypatch):
    """A 200 whose body can't yield slugs is shape drift → the contract-drift error, never a
    KeyError. But a WELL-FORMED empty listing returns () — an empty seat is a selection problem
    (the join's empty-intersection error names it), not a probe failure."""

    for body in ('"not a dict"', '{"nothing": 1}', '{"models": "not-a-list"}'):
        with pytest.raises(SystemExit) as exc:
            _probe(monkeypatch, lambda request, b=body: httpx.Response(
                200, content=b, headers={"Content-Type": "application/json"},
            ))
        assert "can't read" in str(exc.value)

    # Non-empty models, zero readable slugs: drift, not "empty seat".
    with pytest.raises(SystemExit) as exc:
        _probe(monkeypatch, lambda request: httpx.Response(
            200, json={"models": [{"id": "renamed-away"}, "junk"]},
        ))
    assert "can't read" in str(exc.value)

    slugs, _ = _probe(monkeypatch, lambda request: httpx.Response(200, json={"models": []}))
    assert slugs == ()


# ---------------------------------------------------------------------------
# codex OAuth PKCE (ADR 0015 D-c) — the vendor protocol `grid join --api codex` speaks.
# Every test here mocks the vendor: this suite never makes a network call.
# ---------------------------------------------------------------------------


def test_codex_pkce_challenge_is_the_s256_transform_of_the_verifier():
    """The vendor recomputes SHA256(verifier) at exchange and compares it to the challenge the
    authorize URL carried; a pair that doesn't satisfy the transform fails the sign-in. Recomputed
    here from the RFC 7636 definition rather than from the implementation's own helper."""
    import hashlib
    import string

    from remote import codex_oauth

    pkce = codex_oauth.generate_pkce()

    digest = hashlib.sha256(pkce.verifier.encode("ascii")).digest()
    assert pkce.challenge == base64.urlsafe_b64encode(digest).decode().rstrip("=")
    # RFC 7636 §4.1: 43-128 chars from the unreserved set. A verifier outside it is rejected by
    # the vendor, and the padding-stripped challenge must stay URL-safe unencoded.
    assert 43 <= len(pkce.verifier) <= 128
    assert set(pkce.verifier) <= set(string.ascii_letters + string.digits + "-._~")
    assert "=" not in pkce.challenge
    # A generator that returned a constant would satisfy the transform above and defeat PKCE
    # entirely — the verifier is a per-sign-in nonce.
    assert codex_oauth.generate_pkce().verifier != pkce.verifier


def test_codex_authorize_url_carries_the_pkce_challenge_and_state():
    """What the operator's browser opens. The vendor pins the client id, the redirect uri and the
    S256 method; `state` is what makes a foreign redirect detectable at parse time (ADR 0015 D-c),
    so it must actually reach the vendor rather than only being remembered locally."""
    from urllib.parse import parse_qs, urlsplit

    from remote import codex_oauth

    url = codex_oauth.build_authorize_url(state="state-abc", challenge="challenge-xyz")

    split = urlsplit(url)
    assert f"{split.scheme}://{split.netloc}{split.path}" == "https://auth.openai.com/oauth/authorize"
    query = {key: values[-1] for key, values in parse_qs(split.query).items()}
    assert query["response_type"] == "code"
    assert query["client_id"] == "app_EMoamEEZ73f0CkXaXp7hrann"
    assert query["redirect_uri"] == f"http://localhost:{codex_oauth.CALLBACK_PORT}/auth/callback"
    assert query["code_challenge"] == "challenge-xyz"
    assert query["code_challenge_method"] == "S256"
    assert query["state"] == "state-abc"
    # `offline_access` is what earns the refresh token; without it the seat dies at the first expiry.
    assert "offline_access" in query["scope"].split()
    # The vendor keys the simplified Codex consent screen off these; a bare OAuth authorize request
    # is not the same flow.
    assert query["id_token_add_organizations"] == "true"
    assert query["codex_cli_simplified_flow"] == "true"
    assert query["originator"]


def test_codex_parse_redirect_returns_the_authorization_code():
    """The redirect the browser lands on (or the operator pastes back) carries the one-time code
    the exchange spends."""
    from remote import codex_oauth

    code = codex_oauth.parse_redirect(
        "http://localhost:1455/auth/callback?code=auth-code-1&state=state-abc",
        expected_state="state-abc",
    )

    assert code == "auth-code-1"


def test_codex_parse_redirect_refuses_a_redirect_whose_state_is_not_ours():
    """`state` is THE defence against OAuth code injection (ADR 0015 D-c). A redirect URL from an
    attacker's own authorize session carries a REAL code, and the token it exchanges for is
    genuinely signed — so no amount of JWT checking downstream catches it. Only this comparison
    does, which is why a mismatch is terminal and never a warning.

    Both shapes must be refused: a foreign state, and no state at all.
    """
    from remote import codex_oauth

    for query in (
        "code=attacker-code&state=attacker-state",  # a foreign/replayed authorize session
        "code=attacker-code",  # ... or one that simply drops the parameter
    ):
        with pytest.raises(SystemExit) as exc:
            codex_oauth.parse_redirect(
                f"http://localhost:1455/auth/callback?{query}", expected_state="state-abc"
            )
        assert "state" in str(exc.value).lower()
        # The refusal must not quote back what it received: this URL is attacker-supplied text, and
        # the terminal is the operator's. It must also not leak the code, which is still live.
        assert "attacker-code" not in str(exc.value)
        assert "attacker-state" not in str(exc.value)


def test_codex_parse_redirect_reports_a_vendor_error_redirect():
    """The operator clicked Deny (or the vendor refused the grant). The vendor still redirects — with
    `?error=` and no code — and RFC 6749 §4.1.2.1 requires it to echo our `state`, so this lands
    after the state check. It must not read as 'that URL had no code in it': the causes are
    different and so is the fix."""
    from remote import codex_oauth

    with pytest.raises(SystemExit) as exc:
        codex_oauth.parse_redirect(
            "http://localhost:1455/auth/callback?error=access_denied&state=state-abc",
            expected_state="state-abc",
        )

    msg = str(exc.value)
    assert "access_denied" in msg  # the vendor's own code, so the operator can look it up
    assert "denied" in msg.lower() or "refus" in msg.lower()


def test_codex_parse_redirect_reports_a_url_carrying_neither_code_nor_error():
    """Its own case, and the likeliest operator mistake in the paste flow: pasting back the
    authorize URL they were just given instead of the one the browser landed on. That URL carries
    OUR state, so it clears the state check and reaches here — nothing is wrong with the sign-in,
    they just have the wrong string, and the message has to say which string to bring."""
    from remote import codex_oauth

    for query in (
        "state=state-abc",  # the authorize URL pasted back — carries our state, no code
        "code=&state=state-abc",  # ... or a redirect whose code is empty
    ):
        with pytest.raises(SystemExit) as exc:
            codex_oauth.parse_redirect(
                f"http://localhost:1455/auth/callback?{query}", expected_state="state-abc"
            )
        msg = str(exc.value)
        assert "code" in msg.lower()
        # Distinct from both neighbours: this is not a refusal and not a foreign sign-in.
        assert "refused" not in msg.lower()
        assert "different sign-in" not in msg.lower()


def test_codex_exchange_code_posts_a_form_encoded_grant_and_derives_the_seat(monkeypatch):
    """The authorization_code grant is FORM-encoded — the refresh grant (issue 06) is JSON. Two
    grants, two encodings, verified against the real client; sending the wrong one is a 400.

    The response carries exactly three fields and NO account id: the seat is derived from the access
    token's own claim, which is the only place it exists.
    """
    from urllib.parse import parse_qs

    from remote import codex_oauth

    access = _codex_jwt(
        {"chatgpt_account_id": "acct-1", "chatgpt_plan_type": "free"}, exp=2_000_000_000
    )
    seen = _mock_vendor(monkeypatch, lambda request: httpx.Response(
        200, json={"id_token": "id-tok", "access_token": access, "refresh_token": "rt-1"},
    ))

    bundle = codex_oauth.exchange_code("auth-code-1", "verifier-1")

    assert seen["method"] == "POST"
    assert seen["url"] == "https://auth.openai.com/oauth/token"
    assert seen["content_type"] == "application/x-www-form-urlencoded"
    body = {key: values[-1] for key, values in parse_qs(seen["body"].decode()).items()}
    assert body["grant_type"] == "authorization_code"
    assert body["code"] == "auth-code-1"
    assert body["client_id"] == codex_oauth.CLIENT_ID
    assert body["code_verifier"] == "verifier-1"
    # Must match the authorize request's redirect_uri exactly — the vendor re-checks it here.
    assert body["redirect_uri"] == codex_oauth.redirect_uri()
    # A public PKCE client has no secret; the verifier is what proves possession.
    assert "client_secret" not in body

    assert bundle.access_token == access
    assert bundle.refresh_token == "rt-1"
    assert bundle.account_id == "acct-1"  # from the token's claim, not from the response
    assert bundle.plan_type == "free"  # free alongside it — issue 05's tier, at no extra cost
    assert abs(bundle.last_refresh - time.time()) < 60  # stamped now, for D-d's proactive refresh


def test_codex_exchange_code_surfaces_a_rejected_grant_as_a_clean_error(monkeypatch):
    """The usual cause is a slow paste — the code is single-use and short-lived. The operator gets a
    terminal message naming the status and the retry, never an httpx traceback, and never the
    vendor's error body (unbounded text on a path where a 200's body IS the tokens)."""
    from remote import codex_oauth

    _mock_vendor(monkeypatch, lambda request: httpx.Response(
        400, json={"error": "invalid_grant", "error_description": "code expired"},
    ))

    with pytest.raises(SystemExit) as exc:
        codex_oauth.exchange_code("stale-code", "verifier-1")
    msg = str(exc.value)
    assert "400" in msg
    assert "grid join --api codex" in msg  # the operator's next move
    assert "stale-code" not in msg and "verifier-1" not in msg


def test_codex_exchange_code_refuses_a_grant_with_no_refresh_token(monkeypatch):
    """A 200 missing either token is a vendor contract change, not something a re-sign-in fixes —
    and a bundle with a null refresh token would brick the seat at the first expiry (ADR 0015 D-d
    has nothing to rotate). Terminal, and distinct from 'the vendor rejected you'."""
    from remote import codex_oauth

    access = _codex_jwt({"chatgpt_account_id": "acct-1"})
    _mock_vendor(monkeypatch, lambda request: httpx.Response(
        200, json={"id_token": "id-tok", "access_token": access},  # offline_access silently dropped
    ))

    with pytest.raises(SystemExit) as exc:
        codex_oauth.exchange_code("auth-code-1", "verifier-1")
    msg = str(exc.value)
    assert "refresh" in msg.lower()
    assert "rejected" not in msg.lower()  # not the vendor-said-no message
    assert access not in msg


def test_codex_exchange_code_reports_an_unreadable_fresh_token_as_a_vendor_change(monkeypatch, capsys):
    """A token we exchanged one second ago that carries no readable seat is NOT a stale credential:
    the operator has just signed in successfully. `decode_seat` raises `CodexTokenError`, whose
    message is "…sign in again" — correct for a token off the disk, useless here, because signing in
    again reproduces it exactly (issue 04's amendment). So the exchange re-words it, and logs
    `.reason`, which is the ONLY thing that separates a vendor claim rename from a corrupt token.

    It must also not escape as `CodexTokenError`: that is a ValueError, and `cli/_main.py`'s `main`
    has no handler for one — the operator would get a traceback.

    The stderr assertion below is on `codex_oauth` DIRECTLY, and that is deliberate, not an
    oversight: the module's docstring names this one line as its explicit exception to "no operator
    I/O" — it is a log, not interaction, and it must stay in this module because ADR 0015 D-d's
    refresh reaches the same case from the serve loop, which never imports `cli/`. If the line ever
    moves to the CLI, this assertion is supposed to fail and be moved with it.
    """
    from remote import codex_auth, codex_oauth

    # Decodes cleanly; the seat facts are simply no longer under the claim URL we know — the vendor
    # rename that `reason="claim-missing"` exists to name.
    renamed = _codex_jwt(
        None, exp=2_000_000_000, **{"https://api.openai.com/authz": {"chatgpt_account_id": "acct-1"}}
    )
    _mock_vendor(monkeypatch, lambda request: httpx.Response(200, json={
        "id_token": "id-tok", "access_token": renamed, "refresh_token": "rt-1",
    }))

    with pytest.raises(SystemExit) as exc:
        codex_oauth.exchange_code("auth-code-1", "verifier-1")

    msg = str(exc.value)
    assert not isinstance(exc.value, codex_auth.CodexTokenError)
    assert "sign in again" not in msg.lower()  # they just did — don't send them round the loop
    assert "grid" in msg.lower()  # points at the version/contract, which is the real fix
    # `.reason` is a constant from a closed vocabulary, so it can be logged; the claims it came
    # from carry the operator's email and must not be.
    err = capsys.readouterr().err
    assert "claim-missing" in err
    assert renamed not in msg + err and "acct-1" not in msg + err


def test_codex_refresh_posts_the_json_grant_and_rotates_the_bundle(monkeypatch):
    """The refresh grant is JSON — the authorization_code exchange is FORM (facts.md #9: same
    endpoint, two encodings; unifying them 400s one of the two). A 200 rotates the whole bundle:
    new tokens, seat identity re-derived from the NEW access token, `last_refresh` restamped for
    D-d's proactive window."""
    from remote import codex_oauth

    new_access = _codex_jwt(
        {"chatgpt_account_id": "acct-1", "chatgpt_plan_type": "plus"}, exp=2_000_000_000
    )
    seen = _mock_vendor(monkeypatch, lambda request: httpx.Response(
        200, json={"id_token": "id-tok", "access_token": new_access, "refresh_token": "rt-2"},
    ))

    fresh = codex_oauth.refresh_bundle(_codex_bundle())

    assert seen["method"] == "POST"
    assert seen["url"] == "https://auth.openai.com/oauth/token"
    assert seen["content_type"] == "application/json"  # NOT the exchange's form encoding
    assert json.loads(seen["body"]) == {
        "client_id": codex_oauth.CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": "tok-refresh",
    }
    assert fresh.access_token == new_access
    assert fresh.refresh_token == "rt-2"
    assert fresh.account_id == "acct-1"
    assert fresh.plan_type == "plus"  # re-derived from the new token, not carried over
    assert abs(fresh.last_refresh - time.time()) < 60


def test_codex_refresh_keeps_the_old_refresh_token_when_the_vendor_omits_it(monkeypatch):
    """A 200 that rotates the access token but sends no new refresh token means the old one is
    still the live grant — carry it forward (the `_ServeState.refresh` pattern for relay tokens).
    Dropping it would leave a bundle with nothing to rotate; refusing the 200 would discard a
    rotation the vendor already performed."""
    from remote import codex_oauth

    new_access = _codex_jwt({"chatgpt_account_id": "acct-1"}, exp=2_000_000_000)
    _mock_vendor(monkeypatch, lambda request: httpx.Response(
        200, json={"access_token": new_access},
    ))

    fresh = codex_oauth.refresh_bundle(_codex_bundle())

    assert fresh.access_token == new_access
    assert fresh.refresh_token == "tok-refresh"  # the old grant, still live

    # A non-string refresh_token (vendor contract drift) is refused at THIS boundary the same
    # way — persisting it would surface later as a baffling RefreshRefused on the NEXT rotation,
    # far from where the bad data actually appeared (python review).
    _mock_vendor(monkeypatch, lambda request: httpx.Response(
        200, json={"access_token": new_access, "refresh_token": 12345},
    ))
    assert codex_oauth.refresh_bundle(_codex_bundle()).refresh_token == "tok-refresh"


def test_codex_refresh_error_taxonomy_covers_every_status(monkeypatch):
    """The serve loop acts on the CLASS of a refresh failure, so every response must land in
    exactly one of two buckets and none may escape as a raw httpx error:

    * `RefreshRefused` — the vendor PROCESSED the grant and said no: a definitive 4xx (never 408 /
      429, which are rate/timeout noise), or a 200 whose body carries no usable tokens (the vendor
      said success ⇒ the single-use token is spent either way ⇒ only a re-sign-in helps).
    * `RefreshUnavailable` — the grant could not be concluded: 5xx, 408, 429, a 3xx (httpx does
      not follow redirects here), or transport failure. `request_sent=False` only when the request
      provably never left the machine (connect failure) — that is what licenses journal cleanup.

    No failure may quote the refresh token back (the most dangerous string in the store)."""
    from remote import codex_oauth

    def classify(handler):
        _mock_vendor(monkeypatch, handler)
        try:
            codex_oauth.refresh_bundle(_codex_bundle())
        except (codex_oauth.RefreshRefused, codex_oauth.RefreshUnavailable) as exc:
            assert "tok-refresh" not in str(exc)  # never echo the grant
            return exc
        return None

    refused_400 = classify(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    assert isinstance(refused_400, codex_oauth.RefreshRefused)
    assert (refused_400.status_code, refused_400.reason) == (400, "grant-rejected")

    refused_401 = classify(lambda r: httpx.Response(401, json={}))
    assert isinstance(refused_401, codex_oauth.RefreshRefused)
    assert refused_401.reason == "grant-rejected"

    no_tokens = classify(lambda r: httpx.Response(200, json={"id_token": "only"}))
    assert isinstance(no_tokens, codex_oauth.RefreshRefused)
    assert (no_tokens.status_code, no_tokens.reason) == (200, "unusable-grant")

    not_json = classify(lambda r: httpx.Response(200, content=b"<html>sorry</html>"))
    assert isinstance(not_json, codex_oauth.RefreshRefused)
    assert not_json.reason == "unusable-grant"

    for status in (500, 503, 408, 429, 302):  # 3xx: the default bucket, not a refusal
        exc = classify(lambda r, s=status: httpx.Response(s, json={}))
        assert isinstance(exc, codex_oauth.RefreshUnavailable), status
        assert exc.status_code == status
        assert exc.request_sent is True

    def connect_error(request):
        raise httpx.ConnectError("dns says no", request=request)

    never_left = classify(connect_error)
    assert isinstance(never_left, codex_oauth.RefreshUnavailable)
    assert never_left.request_sent is False  # licenses the CAS to clear its own journal

    def read_timeout(request):
        raise httpx.ReadTimeout("mid-flight", request=request)

    ambiguous = classify(read_timeout)
    assert isinstance(ambiguous, codex_oauth.RefreshUnavailable)
    assert ambiguous.request_sent is True  # the grant MAY have reached the vendor — journal stays

    # `reason` is a CLOSED vocabulary, enforced at construction like CodexTokenError — a future
    # raise site that passed vendor-derived text would silently defeat the "safe to log"
    # guarantee the docstring claims (python review).
    with pytest.raises(ValueError):
        codex_oauth.RefreshRefused(400, "vendor-derived text")


def test_codex_refresh_keeps_the_stored_identity_when_the_rotated_token_is_unreadable(monkeypatch, capsys):
    """By the time the rotated token arrives the OLD refresh token is SPENT — discarding the new
    tokens would brick the seat. So unlike the exchange (terminal: at sign-in there is no fallback
    identity, and refusing loses nothing), the refresh carries the STORED account_id/plan_type
    forward — the account is stable per seat — persists the new tokens, and logs the
    closed-vocabulary reason: the only trace separating a vendor claim rename from corruption."""
    from remote import codex_oauth

    _mock_vendor(monkeypatch, lambda request: httpx.Response(
        200, json={"access_token": "not-a-jwt", "refresh_token": "rt-2"},
    ))

    fresh = codex_oauth.refresh_bundle(_codex_bundle())

    assert fresh.access_token == "not-a-jwt"  # the vendor honours it whether or not WE can read it
    assert fresh.refresh_token == "rt-2"
    assert fresh.account_id == "acct-1"  # carried from the stored bundle, not dropped
    assert fresh.plan_type == "free"
    err = capsys.readouterr().err
    assert "bad-segment-count" in err  # `.reason` is a constant — safe to log; claims are not
    assert "not-a-jwt" not in err and "rt-2" not in err


def test_codex_callback_listener_captures_the_redirect_without_logging_it(capsys):
    """The browser flow's half: bind a loopback port, let the vendor's redirect land on it, hand the
    URL back for `parse_redirect` to check. Bound to 127.0.0.1 only — a callback on 0.0.0.0 would let
    anything on the LAN post an authorization code at this operator.

    The URL carries the live authorization code, and `BaseHTTPRequestHandler` logs every request line
    to stderr by default — i.e. the default listener prints the code. That must be off.
    """
    from remote import codex_callback

    with codex_callback.listen(0, expected_state="state-abc") as listener:  # port 0: never 1455
        visitor = threading.Thread(
            target=lambda: httpx.get(
                f"http://127.0.0.1:{listener.port}/auth/callback"
                "?code=AUTHCODE-SEKRIT&state=state-abc"
            ),
            daemon=True,
        )
        visitor.start()
        url = listener.wait(deadline=time.monotonic() + 10)
        visitor.join(timeout=10)

    assert url is not None
    assert codex_oauth_parse(url, "state-abc") == "AUTHCODE-SEKRIT"
    out_err = capsys.readouterr()
    assert "AUTHCODE-SEKRIT" not in out_err.out + out_err.err


def codex_oauth_parse(url, state):
    from remote import codex_oauth

    return codex_oauth.parse_redirect(url, expected_state=state)


def test_codex_callback_listener_ignores_hits_that_are_not_our_redirect(capsys):
    """A path match is not enough to end the wait — the redirect has to be OURS.

    `do_GET` used to filter on path alone, so the first request to reach `/auth/callback` won the
    race unconditionally, even a bare `GET /auth/callback` with no code, state or error. That is
    reachable from any page in a background tab: `<img src="http://localhost:1455/auth/callback">`
    is a plain cross-origin GET, no preflight, during the sign-in window. The operator's REAL
    approval then arrives 0.2s later to a closed socket and is discarded, and they are told "That
    redirect URL is from a different sign-in" about a sign-in that genuinely succeeded at the vendor.

    So the handler compares `state` too — a filter, not the control: it only decides what ends the
    wait. `parse_redirect` on the main thread still makes the authoritative refusal, because a
    handler thread cannot raise (its `SystemExit` would be swallowed and the operator would see a
    hang instead of an error).
    """
    from remote import codex_callback

    with codex_callback.listen(0, expected_state="real-state") as listener:
        base = f"http://127.0.0.1:{listener.port}/auth/callback"
        # Each GET blocks until `wait()`'s loop serves it, so they must run off the main thread.
        # They land in order; only the last is ours.
        threading.Thread(
            target=lambda: [
                httpx.get(base, timeout=10),  # the <img src> hit: no code, no state, no error
                httpx.get(f"{base}?code=X&state=someone-elses", timeout=10),  # a foreign sign-in
                httpx.get(f"{base}?code=REAL-CODE&state=real-state", timeout=10),  # ours
            ],
            daemon=True,
        ).start()

        url = listener.wait(deadline=time.monotonic() + 20)

    assert url is not None, "the real redirect never ended the wait"
    assert codex_oauth_parse(url, "real-state") == "REAL-CODE"
    assert "REAL-CODE" not in capsys.readouterr().err


def test_codex_callback_listener_reports_a_port_someone_else_holds():
    """The callback port is the real Codex CLI's too, so an operator signing into Codex proper in
    another terminal holds it — an ordinary collision, not an exotic one. It surfaces as its own type
    so the join can fall back with guidance instead of dying.

    Bind-and-catch, never probe-then-bind: `connect_ex` (the repo's nearest neighbour, in
    `shared/engine/launcher.py`) would answer "free" and lose the race to whoever binds next.
    """
    import socket

    from remote import codex_callback

    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    try:
        with pytest.raises(codex_callback.PortInUse):
            with codex_callback.listen(holder.getsockname()[1], expected_state="s"):
                pytest.fail("must not hand back a listener it never bound")
    finally:
        holder.close()


def test_codex_callback_listener_reports_an_unclassifiable_bind_failure_cleanly(monkeypatch):
    """EADDRINUSE has its own type and a fallback. Every OTHER bind failure — an exhausted fd table,
    a sandbox refusing loopback — used to re-raise as a bare `OSError`, and nothing between `listen`
    and `cli/_main.py::main()` catches one (`cli/dispatch.py` has no try/except). The operator got a
    traceback: loud rather than silent, but still the one outcome this feature's error contract rules
    out.

    The bind is induced to fail because a real EACCES needs a privileged port, which a test cannot
    portably rely on (and would flip under a root CI runner).
    """
    from remote import codex_callback

    def refuse(address, handler):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(codex_callback, "_CallbackServer", refuse)

    with pytest.raises(SystemExit) as exc:  # not a bare OSError
        with codex_callback.listen(0, expected_state="s"):
            pytest.fail("must not yield a listener it never bound")

    assert not isinstance(exc.value, codex_callback.PortInUse)  # not dressed up as a collision
    assert "Permission denied" in str(exc.value)  # the OS's own reason, so it is diagnosable


def test_codex_callback_listener_gives_up_at_its_deadline():
    """Nobody ever approves (the operator wandered off, or the browser never opened). The wait ends
    at the deadline and says so, rather than parking the CLI on a socket forever.

    Covers only the *nobody connects* case — see the test below for the one that actually bit."""
    from remote import codex_callback

    with codex_callback.listen(0, expected_state="s") as listener:
        assert listener.wait(deadline=time.monotonic() + 0.2) is None


def test_codex_callback_listener_deadline_holds_against_a_client_that_says_nothing():
    """The deadline must bound the WHOLE wait, not just the gap before someone connects.

    `BaseServer.handle_request`'s timeout only bounds the `select()` for a *new* connection. Once one
    is accepted, control passes to the handler — a `StreamRequestHandler` whose own `timeout` class
    attribute defaults to None, so `setup()` never calls `settimeout()` and `rfile.readline()` blocks
    with no bound at all. One TCP connect that sends nothing therefore hangs `grid join --api codex`
    straight past `_SIGNIN_DEADLINE_S` with no error — the CLI just stops — and, the server being
    single-threaded, also stops the real browser's redirect from ever being accepted.

    No HTTP, no valid `state`, no local code execution: `nc 127.0.0.1 1455` is the whole attack. It
    silently defeats the deadline ADR 0015 D-c promises.

    The wait runs on a daemon thread so that a regression fails this test instead of hanging the suite.
    """
    import socket

    from remote import codex_callback

    with codex_callback.listen(0, expected_state="s") as listener:
        silent = socket.create_connection(("127.0.0.1", listener.port), timeout=5)
        try:
            done = {}
            waiter = threading.Thread(
                target=lambda: done.update(returned=listener.wait(deadline=time.monotonic() + 1.0)),
                daemon=True,
            )
            waiter.start()
            waiter.join(timeout=20)  # 20x the deadline: generous, but bounded

            assert not waiter.is_alive(), "wait() blew its deadline — a silent client hung the sign-in"
            assert done["returned"] is None
        finally:
            silent.close()


def test_codex_parse_redirect_refuses_to_verify_against_an_empty_state():
    """A guard on the CALLER, not on the URL — and the reason it exists is that the failure is
    silent: `compare_digest("", "")` is True, so an empty `expected_state` turns this check into
    accept-any-state-less-redirect while still looking like it verifies. The one real control on
    this path must fail loudly rather than pass vacuously.

    Not a `SystemExit`: no operator can act on it. It is a bug in a caller (`codex_auth` guards its
    own closed vocabulary the same way, and for the same reason).
    """
    from remote import codex_oauth

    with pytest.raises(ValueError):
        codex_oauth.parse_redirect("http://localhost:1455/auth/callback?code=c", expected_state="")


def test_codex_parse_redirect_state_check_survives_a_non_ascii_state():
    """The received state is attacker-controlled text. A constant-time compare over `str` raises
    TypeError on non-ASCII, which would turn a hostile URL into a traceback instead of a refusal —
    the one outcome issue 04 rules out."""
    from remote import codex_oauth

    with pytest.raises(SystemExit) as exc:
        codex_oauth.parse_redirect(
            "http://localhost:1455/auth/callback?code=c&state=caf%C3%A9-%F0%9F%92%A5",
            expected_state="state-abc",
        )
    assert "state" in str(exc.value).lower()


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


def _fake_llama_release(tmp_path: Path) -> tuple[Path, str]:
    """Build a stand-in for the official macOS tarball: llama-server, a real .dylib, and the
    versioned symlink alias the release ships. Returns the archive and its SHA-256."""
    stage = tmp_path / "stage" / f"llama-{installer.LLAMA_RELEASE}"
    stage.mkdir(parents=True)
    (stage / "llama-server").write_text("#!/bin/sh\n", encoding="utf-8")
    (stage / "libggml.0.dylib").write_bytes(b"lib")
    (stage / "libggml.dylib").symlink_to("libggml.0.dylib")

    archive = tmp_path / f"llama-{installer.LLAMA_RELEASE}-bin-macos-arm64.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(stage, arcname=stage.name)
    return archive, installer._sha256(archive)


def _pin_fake_release(monkeypatch, archive: Path, sha256: str) -> None:
    """Point the arm64 build at [archive] and serve it from disk instead of the network."""
    build = installer.MacosBuild(label="macos-arm64", url=f"https://example.invalid/{archive.name}", sha256=sha256)
    monkeypatch.setitem(installer.MACOS_BUILDS, "arm64", build)
    monkeypatch.setattr(installer.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(installer, "_download", lambda url, dest: shutil.copy2(archive, dest))


def test_install_macos_prebuilt_unpacks_beside_its_libraries_and_links(monkeypatch, tmp_path):
    grid_home = tmp_path / "grid-home"
    monkeypatch.setenv("GRID_HOME", str(grid_home))
    archive, sha256 = _fake_llama_release(tmp_path)
    _pin_fake_release(monkeypatch, archive, sha256)

    target = installer.install_macos_prebuilt()

    prefix = grid_home / "engines" / "llama.cpp"
    # The binary resolves its libraries via @loader_path, so they must land beside it.
    assert (prefix / "llama-server").is_file()
    assert (prefix / "libggml.0.dylib").is_file()
    # The release's versioned alias stays a link rather than a second copy of the library.
    assert (prefix / "libggml.dylib").is_symlink()

    assert target == grid_home / "bin" / "llama-server"
    assert target.is_symlink()
    assert target.resolve() == (prefix / "llama-server").resolve()


def test_install_macos_prebuilt_replaces_an_existing_install(monkeypatch, tmp_path):
    grid_home = tmp_path / "grid-home"
    monkeypatch.setenv("GRID_HOME", str(grid_home))
    archive, sha256 = _fake_llama_release(tmp_path)
    _pin_fake_release(monkeypatch, archive, sha256)
    # A Homebrew-era install left bin/llama-server as a symlink to a path that is now gone.
    bin_dir = grid_home / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "llama-server").symlink_to(tmp_path / "gone" / "llama-server")

    target = installer.install_macos_prebuilt()

    assert target.resolve() == (grid_home / "engines" / "llama.cpp" / "llama-server").resolve()


def test_install_macos_prebuilt_aborts_on_sha_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path / "grid-home"))
    archive, _ = _fake_llama_release(tmp_path)
    _pin_fake_release(monkeypatch, archive, "0" * 64)

    # The hash is the only check on a binary we downloaded and are about to run.
    with pytest.raises(SystemExit, match="SHA-256 mismatch"):
        installer.install_macos_prebuilt()


@pytest.mark.parametrize(
    ("machine", "label"),
    [("arm64", "macos-arm64"), ("aarch64", "macos-arm64"), ("x86_64", "macos-x64")],
)
def test_pick_macos_build_covers_both_architectures(machine, label):
    assert installer.pick_macos_build(machine).label == label


def test_pick_macos_build_rejects_an_unknown_architecture():
    with pytest.raises(SystemExit, match="--from-source"):
        installer.pick_macos_build("ppc64")


def test_native_machine_sees_apple_silicon_through_rosetta(monkeypatch):
    # A Grid running under Rosetta reports x86_64 for itself; the hardware is still arm64, and the
    # installers must follow the hardware or they fetch Intel binaries for an M-series Mac.
    monkeypatch.setattr(arch.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(arch.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        arch.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, stdout="1\n", stderr=""),
    )

    assert arch.native_machine() == "arm64"


def test_native_machine_reports_a_real_intel_mac_as_intel(monkeypatch):
    monkeypatch.setattr(arch.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(arch.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        arch.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 1, stdout="", stderr=""),
    )

    assert arch.native_machine() == "x86_64"


@pytest.mark.parametrize(
    ("machine", "label"),
    [("arm64", "aarch64-apple-darwin"), ("x86_64", "x86_64-apple-darwin")],
)
def test_pick_uv_build_follows_the_architecture(machine, label):
    assert agent_installer.pick_uv_build(machine).label == label


def test_agent_install_is_a_no_op_when_hermes_is_already_there(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    hermes = tmp_path / "bin" / "hermes"
    hermes.parent.mkdir(parents=True)
    hermes.write_text("#!/bin/sh\n", encoding="utf-8")

    def fail(*_args, **_kwargs):
        raise AssertionError("must not reinstall when hermes is present")

    monkeypatch.setattr(agent_installer, "install_hermes", fail)

    rc = cli.cmd_agent_install(argparse.Namespace(name="hermes", force=False))

    assert rc == 0
    assert "already installed" in capsys.readouterr().out


def test_agent_install_forces_a_reinstall_when_asked(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    hermes = tmp_path / "bin" / "hermes"
    hermes.parent.mkdir(parents=True)
    hermes.write_text("#!/bin/sh\n", encoding="utf-8")
    calls = []

    monkeypatch.setattr(agent_installer, "install_hermes", lambda: calls.append("install") or hermes)

    rc = cli.cmd_agent_install(argparse.Namespace(name="hermes", force=True))

    assert rc == 0
    assert calls == ["install"]


def test_engine_install_on_macos_uses_the_prebuilt_by_default(monkeypatch):
    calls = []
    target = Path("/tmp/grid/bin/llama-server")

    monkeypatch.setattr(installer, "is_macos", lambda: True)
    monkeypatch.setattr(installer, "install_macos_prebuilt", lambda: calls.append("prebuilt") or target)
    monkeypatch.setattr(installer, "install_metal_from_source", lambda: calls.append("source") or target)

    rc = cli.cmd_engine_install(argparse.Namespace(name="llama.cpp", target_sm=None, from_source=False))

    assert rc == 0
    assert calls == ["prebuilt"]


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


def test_remote_join_api_no_model_flag_serves_whole_whitelist_intersection(monkeypatch, tmp_path, capsys):
    """`grid join --api openai` with no -m is the zero-config default: it serves the whole
    whitelist ∩ the models the key can see, and reports the skipped whitelist models."""
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    _mock_vendor(monkeypatch, lambda request: httpx.Response(
        200, json={"data": [{"id": "gpt-5.5"}, {"id": "gpt-5.4"}]}
    ))

    assert cli.main(["join", "--api", "openai"]) == 0

    record = cli.provider._read_records("n1")["remote"]
    assert record["models"] == ["openai:gpt-5.5", "openai:gpt-5.4"]  # whitelist order, key-visible only
    err = capsys.readouterr().err
    assert "openai:gpt-5.4-mini" in err and "openai:gpt-5.4-nano" in err  # skipped models reported


def test_remote_join_api_no_model_flag_empty_intersection_errors(monkeypatch, tmp_path):
    """Default-all with a key that can see NO whitelisted model is an error naming the skipped
    models — not a join that serves nothing, and not a message claiming they were 'requested'."""
    _seed_running_remote_grid(monkeypatch, tmp_path)
    spawned = _mock_remote_spawn(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    _mock_vendor(monkeypatch, lambda request: httpx.Response(200, json={"data": [{"id": "other-model"}]}))

    with pytest.raises(SystemExit) as exc:
        cli.main(["join", "--api", "openai"])
    msg = str(exc.value)
    assert "openai:gpt-5.5" in msg          # the unavailable whitelist models are named
    assert "requested" not in msg.lower()   # nothing was requested — this is the default set
    assert cli.provider._read_records("n1") == {} and "cmd" not in spawned


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


def test_remote_join_api_no_key_anywhere_non_interactive_errors(monkeypatch, tmp_path):
    """No env var, empty key store, non-interactive (pytest is non-tty): a clear error naming the
    env var — no vendor call, no hidden prompt."""
    _seed_running_remote_grid(monkeypatch, tmp_path)
    spawned = _mock_remote_spawn(monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: pytest.fail("no key, no vendor call"))
    monkeypatch.setattr(cli.remote_provider, "_prompt_api_key",
                        lambda *a: pytest.fail("non-interactive must not prompt"))

    with pytest.raises(SystemExit) as exc:
        cli.main(["join", "--api", "openai", "-m", "openai:gpt-5.5"])
    assert "OPENAI_API_KEY" in str(exc.value)
    assert cli.provider._read_records("n1") == {} and "cmd" not in spawned


def test_remote_join_api_empty_prompt_input_is_terminal(monkeypatch, tmp_path):
    """An interactive prompt answered with nothing is a clean error — not a vendor call with an
    empty bearer, and nothing stored."""
    _seed_running_remote_grid(monkeypatch, tmp_path)
    spawned = _mock_remote_spawn(monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(cli.provider, "_interactive", lambda: True)
    monkeypatch.setattr(cli.remote_provider, "_prompt_api_key", lambda kind, env_var: "")
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: pytest.fail("empty key, no vendor call"))

    with pytest.raises(SystemExit) as exc:
        cli.main(["join", "--api", "openai", "-m", "openai:gpt-5.5"])
    assert "key" in str(exc.value).lower()
    from remote import api_keys
    assert api_keys.load_key("openai") is None
    assert cli.provider._read_records("n1") == {} and "cmd" not in spawned


def _mock_vendor(monkeypatch, handler, _real=httpx.Client):
    """Serve the vendor's model-listing endpoint the `join --api` key validation calls, via
    httpx.MockTransport (the `_mock_serve_engine` pattern). Returns what the vendor saw."""
    seen = {}

    def wrapped(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        # The codex OAuth exchange is a POST whose encoding is part of the contract (form here,
        # JSON for the refresh grant) — the listing callers above only ever read url/auth.
        seen["method"] = request.method
        seen["content_type"] = request.headers.get("content-type")
        seen["body"] = request.content
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
    # A key the vendor rejected is never persisted — later joins must not reuse it silently.
    from remote import api_keys
    assert api_keys.load_key("openai") is None


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


def test_remote_join_api_env_key_persisted_to_key_store(monkeypatch, tmp_path):
    """A validated env key lands in the machine-local key store (api_keys.toml, 0o600, keyed by
    service kind) so later joins and the detached serve process can read it without the env var."""
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    _mock_vendor(monkeypatch, lambda request: httpx.Response(200, json={"data": [{"id": "gpt-5.5"}]}))

    assert cli.main(["join", "--api", "openai", "-m", "openai:gpt-5.5"]) == 0

    import tomllib

    from remote import api_keys
    from shared import paths
    key_file = paths.api_keys_file()
    assert key_file.exists()
    assert (key_file.stat().st_mode & 0o777) == 0o600
    assert api_keys.load_key("openai") == "sk-test-123"
    # On-disk contract: one table per service kind, the key under a `key` field — room for
    # future per-kind metadata (base_url etc.) without a format change.
    data = tomllib.loads(key_file.read_text())
    assert data["openai"]["key"] == "sk-test-123"


def test_remote_join_api_reuses_stored_key_without_env(monkeypatch, tmp_path):
    """A later join with no env var silently reuses the stored key: the vendor sees the stored
    bearer, and `grid logout` beforehand doesn't matter (the store is not the credential store)."""
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from remote import api_keys
    api_keys.store_key("openai", "sk-stored-456")
    seen = _mock_vendor(monkeypatch, lambda request: httpx.Response(200, json={"data": [{"id": "gpt-5.5"}]}))

    assert cli.main(["join", "--api", "openai", "-m", "openai:gpt-5.5"]) == 0
    assert seen["auth"] == "Bearer sk-stored-456"


def test_remote_join_api_prompts_hidden_key_when_interactive(monkeypatch, tmp_path, capsys):
    """First join with no env and no stored key on a TTY: the hidden prompt supplies the key,
    which is validated, stored, and never echoed; the join proceeds."""
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(cli.provider, "_interactive", lambda: True)
    prompts = []
    monkeypatch.setattr(
        cli.remote_provider, "_prompt_api_key",
        lambda kind, env_var: (prompts.append(kind), "sk-prompted-789")[1],
    )
    seen = _mock_vendor(monkeypatch, lambda request: httpx.Response(200, json={"data": [{"id": "gpt-5.5"}]}))

    assert cli.main(["join", "--api", "openai", "-m", "openai:gpt-5.5"]) == 0

    from remote import api_keys
    assert prompts == ["openai"]
    assert seen["auth"] == "Bearer sk-prompted-789"
    assert api_keys.load_key("openai") == "sk-prompted-789"
    out_err = capsys.readouterr()
    assert "sk-prompted-789" not in out_err.out + out_err.err


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


# --- `grid join --api codex` sign-in (ADR 0015 D-c) -----------------------------------------

def _mock_codex_browser(monkeypatch, *, code="AUTHCODE-1", state=None, listen=None):
    """Stand in for the operator's browser AND the one-shot callback listener.

    The redirect echoes back the `state` grid actually generated (read out of the authorize URL it
    opened), because that is what the real vendor does — a fixed state would make every test pass
    against an implementation that never checks it. Pass `state` to play the attacker instead.
    """
    import webbrowser
    from urllib.parse import parse_qs, urlsplit

    from remote import codex_callback

    seen = {}

    class _FakeListener:
        port = 1455

        def wait(self, *, deadline):
            seen["deadline"] = deadline
            return (
                f"http://localhost:1455/auth/callback"
                f"?code={code}&state={state or seen['state']}"
            )

    @contextlib.contextmanager
    def fake_listen(port, *, expected_state):
        seen["bound_port"] = port
        seen["listener_state"] = expected_state
        yield _FakeListener()

    def fake_open(url):
        seen["authorize_url"] = url
        seen["state"] = parse_qs(urlsplit(url).query)["state"][0]
        return True

    monkeypatch.setattr(codex_callback, "listen", listen or fake_listen)
    monkeypatch.setattr(webbrowser, "open", fake_open)
    return seen


def _mock_codex_exchange(monkeypatch, *, account="acct-1", plan="free", models=None):
    """The vendor, both halves: the token endpoint (3 fields, no account id — fact 9) AND the
    free `GET /models` probe a successful sign-in now flows straight into (issue 05). Returns
    (access_token, seen) where seen records the exchange under `token_*` keys and the probe
    under `probe_*` keys — a flat `url` would be whichever call came LAST."""
    access = _codex_jwt(
        {"chatgpt_account_id": account, "chatgpt_plan_type": plan}, exp=2_000_000_000
    )
    listing = {"models": [
        {"slug": slug, "visibility": "list"}
        for slug in (models if models is not None
                     else ["gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4-mini"])
    ]}
    seen = {}

    def handler(request):
        url = str(request.url)
        if "/oauth/token" in url:
            seen["token_url"] = url
            seen["token_method"] = request.method
            seen["token_content_type"] = request.headers.get("content-type")
            seen["token_body"] = request.content
            return httpx.Response(200, json={
                "id_token": "id-tok", "access_token": access, "refresh_token": "rt-1",
            })
        seen["probe_url"] = url
        seen["probe_auth"] = request.headers.get("authorization")
        seen.setdefault("probe_calls", 0)
        seen["probe_calls"] += 1
        return httpx.Response(200, json=listing)

    _mock_vendor(monkeypatch, handler)
    return access, seen


def test_remote_join_api_codex_browser_flow_lands_the_bundle(monkeypatch, tmp_path, capsys):
    """The default sign-in (ADR 0015 D-c): bind the callback port, open the browser, catch the
    redirect, exchange the code, store the seat. Grid runs the OAuth itself — no `--api-key` flag,
    no env var, and `~/.codex/auth.json` is never read."""
    from remote import api_keys, codex_oauth

    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    access, exchanged = _mock_codex_exchange(monkeypatch)
    browser = _mock_codex_browser(monkeypatch)

    # Sign-in (issue 04) flows straight into the probe + join (issue 05): one command, seat
    # stored, engine registered.
    assert cli.main(["join", "--api", "codex"]) == 0

    assert browser["bound_port"] == codex_oauth.CALLBACK_PORT  # bound BEFORE the browser opened
    assert browser["authorize_url"].startswith("https://auth.openai.com/oauth/authorize?")
    assert exchanged["token_url"] == "https://auth.openai.com/oauth/token"
    assert exchanged["probe_calls"] == 1  # the fresh seat was probed before anything spawned

    assert api_keys.load_codex_bundle() == codex_oauth.CodexBundle(
        access_token=access, refresh_token="rt-1", account_id="acct-1",
        plan_type="free", last_refresh=api_keys.load_codex_bundle().last_refresh,
    )
    # Secret hygiene: nothing about the seat reaches the terminal.
    out_err = capsys.readouterr()
    assert access not in out_err.out + out_err.err
    assert "rt-1" not in out_err.out + out_err.err
    assert "acct-1" not in out_err.out + out_err.err


def _authorize_url_from(text):
    for line in text.splitlines():
        if line.strip().startswith("https://auth.openai.com/oauth/authorize?"):
            return line.strip()
    raise AssertionError(f"no authorize URL was printed for the operator to open:\n{text}")


def _no_browser_and_no_bind(monkeypatch):
    """Assert the paste flow needs neither of the two things a headless box hasn't got."""
    import webbrowser

    from remote import codex_callback

    monkeypatch.setattr(webbrowser, "open", lambda url: pytest.fail("--no-browser opened a browser"))
    monkeypatch.setattr(codex_callback, "listen",
                        lambda port, **kw: pytest.fail("--no-browser bound a port"))


def test_remote_join_api_codex_no_auto_open_env_prints_url_without_opening_a_browser(
    monkeypatch, tmp_path, capsys
):
    """`GRID_OAUTH_NO_OPEN`: a GUI front-end (the grid app) opens the URL itself — the way it drives
    `grid login` — so the browser flow must NOT open a browser, yet must still bind the callback,
    print the authorize URL, and land the seat from the redirect that front-end's browser triggers.
    Without the gate the CLI would open a second tab."""
    import webbrowser

    from remote import api_keys, codex_callback, codex_oauth

    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    _mock_codex_exchange(monkeypatch)

    # No browser open records the state, so the listener echoes the one grid generated (handed to it
    # as `expected_state`): proof the URL was still built and the callback still catches the redirect.
    seen = {}

    class _FakeListener:
        port = codex_oauth.CALLBACK_PORT

        def wait(self, *, deadline):
            return f"http://localhost:1455/auth/callback?code=AUTHCODE-1&state={seen['state']}"

    @contextlib.contextmanager
    def fake_listen(port, *, expected_state):
        seen["state"] = expected_state
        yield _FakeListener()

    monkeypatch.setattr(codex_callback, "listen", fake_listen)
    monkeypatch.setattr(
        webbrowser, "open",
        lambda url: pytest.fail("GRID_OAUTH_NO_OPEN must not open a browser"),
    )
    monkeypatch.setenv("GRID_OAUTH_NO_OPEN", "1")

    assert cli.main(["join", "--api", "codex"]) == 0

    out = capsys.readouterr().out
    assert _authorize_url_from(out)  # the URL is still printed for the front-end to open
    assert api_keys.load_codex_bundle() is not None  # the seat landed from the redirect


def test_remote_join_api_codex_no_browser_flow_takes_a_pasted_redirect(monkeypatch, tmp_path, capsys):
    """User story 2 — a headless box. Grid prints the URL, the operator approves it on a machine that
    has a browser, and brings the redirect back by hand. Nothing opens and nothing binds.

    The test plays the operator literally: it reads the URL off the terminal and pastes back the
    redirect the vendor would send for THAT authorize request, `state` and all.
    """
    from remote import api_keys

    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    access, exchanged = _mock_codex_exchange(monkeypatch)
    monkeypatch.setattr(cli.provider, "_interactive", lambda: True)
    _no_browser_and_no_bind(monkeypatch)

    printed = {}

    def fake_paste():
        from urllib.parse import parse_qs, urlsplit

        printed["out"] = capsys.readouterr().out
        url = _authorize_url_from(printed["out"])
        state = parse_qs(urlsplit(url).query)["state"][0]
        return f"http://localhost:1455/auth/callback?code=AUTHCODE-1&state={state}"

    monkeypatch.setattr(cli.codex_signin, "_prompt_redirect_url", fake_paste)

    assert cli.main(["join", "--api", "codex", "--no-browser"]) == 0  # sign-in flows into the join

    assert exchanged["token_url"] == "https://auth.openai.com/oauth/token"
    bundle = api_keys.load_codex_bundle()
    assert bundle is not None and bundle.access_token == access and bundle.account_id == "acct-1"
    assert "AUTHCODE-1" not in printed["out"]  # grid printed the authorize URL, never the code


def test_remote_join_api_codex_paste_deadline_stores_nothing(monkeypatch, tmp_path):
    """The operator wandered off mid-approval and pasted a URL whose code died on the way. Refused
    before the exchange is even attempted: the vendor would reject it anyway, and a message about
    a 10-minute code beats a bare HTTP 400. Nothing is stored."""
    from remote import api_keys

    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    monkeypatch.setattr(cli.provider, "_interactive", lambda: True)
    _no_browser_and_no_bind(monkeypatch)
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: pytest.fail("a dead code is never spent"))

    # The clock jumps past the code's lifetime while the operator is pasting. Read once at the start
    # of the paste flow, once after the paste returns.
    clock = iter([1000.0, 1000.0 + 601.0])
    monkeypatch.setattr(cli.codex_signin.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(
        cli.codex_signin, "_prompt_redirect_url",
        lambda: "http://localhost:1455/auth/callback?code=AUTHCODE-1&state=whatever",
    )

    with pytest.raises(SystemExit) as exc:
        cli.main(["join", "--api", "codex", "--no-browser"])

    assert "too long" in str(exc.value).lower() or "expired" in str(exc.value).lower()
    assert api_keys.load_codex_bundle() is None
    assert not paths.api_keys_file().exists()


def _callback_port_taken(monkeypatch):
    """The real Codex CLI holds the callback port. `listen` binds-and-catches, so this is what the
    join sees — raised from the bind, not from a probe that could have raced."""
    from remote import codex_callback

    def taken(port, *, expected_state):
        raise codex_callback.PortInUse(48, "Address already in use")

    monkeypatch.setattr(codex_callback, "listen", taken)


def test_remote_join_api_codex_falls_back_to_paste_when_callback_port_is_taken(
    monkeypatch, tmp_path, capsys
):
    """User story 4: the operator's real Codex CLI is signing in and holds port 1455 — grid's
    callback port is theirs too. The two tools have to coexist, so this is a fallback and not just a
    refusal: the paste flow needs no port, so the join completes through it. Never a traceback."""
    from remote import api_keys

    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    access, _ = _mock_codex_exchange(monkeypatch)
    monkeypatch.setattr(cli.provider, "_interactive", lambda: True)
    _callback_port_taken(monkeypatch)

    def fake_paste():
        from urllib.parse import parse_qs, urlsplit

        url = _authorize_url_from(capsys.readouterr().out)
        state = parse_qs(urlsplit(url).query)["state"][0]
        return f"http://localhost:1455/auth/callback?code=AUTHCODE-1&state={state}"

    monkeypatch.setattr(cli.codex_signin, "_prompt_redirect_url", fake_paste)

    assert cli.main(["join", "--api", "codex"]) == 0  # the paste flow completes the join

    bundle = api_keys.load_codex_bundle()
    assert bundle is not None and bundle.access_token == access


def test_remote_join_api_codex_port_taken_with_no_terminal_names_the_flag(monkeypatch, tmp_path, capsys):
    """Same collision on a box with no terminal to paste into: there is nothing to fall back TO. It
    ends with the cause named (that port belongs to the Codex apps too) and the flag that would have
    worked — not a traceback, and not a silent hang on a port we never got."""
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    monkeypatch.setattr(cli.provider, "_interactive", lambda: False)
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: pytest.fail("no code, no exchange"))
    _callback_port_taken(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        cli.main(["join", "--api", "codex"])

    assert "--no-browser" in str(exc.value)
    err = capsys.readouterr().err
    assert "1455" in err and "Codex" in err  # the cause, so the operator knows what to close
    from remote import api_keys
    assert api_keys.load_codex_bundle() is None


def test_remote_join_api_codex_refuses_a_redirect_from_a_foreign_sign_in(monkeypatch, tmp_path):
    """`state`, end to end. Anything else on the box can reach the loopback callback and post a code
    from its OWN authorize session; the token that code buys is genuinely signed, so no downstream
    check catches it. This wiring is the only thing that does — and a `sign_in` that generated a
    state but compared against the wrong one would pass every unit test in isolation."""
    from remote import api_keys

    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    _mock_codex_browser(monkeypatch, code="INJECTED-CODE", state="attacker-state")
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: pytest.fail("an injected code is never spent"))

    with pytest.raises(SystemExit) as exc:
        cli.main(["join", "--api", "codex"])

    assert "state" in str(exc.value).lower()
    assert "INJECTED-CODE" not in str(exc.value)
    assert api_keys.load_codex_bundle() is None
    assert not paths.api_keys_file().exists()  # nothing stored at all


def test_remote_join_api_codex_never_touches_the_real_codex_cli_credential(monkeypatch, tmp_path):
    """ADR 0015 D-c / user story 6: `~/.codex/auth.json` is never read or written. Not once.

    Adopting the real Codex CLI's bundle would look like a free sign-in and then double-spend its
    single-use, rotating refresh token — revoking the operator's actual Codex seat. Grid runs its own
    authorization from scratch instead, and keeps its own bundle.

    A tripwire rather than a grep over the source: it catches any route into that file, including one
    a later refactor adds through a helper nobody thought to check.
    """
    import builtins
    import pathlib

    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    _mock_codex_exchange(monkeypatch)
    _mock_codex_browser(monkeypatch)

    real_open, real_path_open = builtins.open, pathlib.Path.open
    touched = []

    def guard(path):
        if "/.codex/" in str(path) or str(path).endswith("/.codex"):
            touched.append(str(path))

    def fake_open(file, *a, **k):
        guard(file)
        return real_open(file, *a, **k)

    def fake_path_open(self, *a, **k):
        guard(self)
        return real_path_open(self, *a, **k)

    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr(pathlib.Path, "open", fake_path_open)

    assert cli.main(["join", "--api", "codex"]) == 0  # the whole join, sign-in through spawn

    assert touched == []


def test_remote_join_api_codex_reuses_a_stored_seat_with_no_sign_in(monkeypatch, tmp_path, capsys):
    """Acceptance: a later join reuses the stored bundle without re-auth (user story 15 —
    `grid leave --engine codex` then re-joining is one command). Nothing opens, nothing binds,
    nothing is pasted, and the token endpoint is never touched: the seat is already ours. The
    ONE vendor call is the free probe (this box has no live codex engine, so the spec changed
    and D-f says probe)."""
    import webbrowser

    from remote import api_keys, codex_callback

    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    api_keys.store_codex_bundle(_bundle())
    monkeypatch.setattr(webbrowser, "open", lambda url: pytest.fail("a stored seat needs no browser"))
    monkeypatch.setattr(codex_callback, "listen",
                        lambda port, **kw: pytest.fail("a stored seat binds nothing"))
    monkeypatch.setattr(
        cli.codex_signin, "_prompt_redirect_url", lambda: pytest.fail("a stored seat is not re-pasted")
    )

    def vendor(request):
        if "/oauth/token" in str(request.url):
            pytest.fail("a stored seat is not re-exchanged")
        return httpx.Response(200, json={"models": [{"slug": "gpt-5.5", "visibility": "list"}]})

    _mock_probe(monkeypatch, vendor)

    assert cli.main(["join", "--api", "codex"]) == 0

    assert api_keys.load_codex_bundle() == _bundle()  # untouched, not re-minted
    record = cli.provider._read_records("n1")["remote"]
    assert record["models"] == ["codex:gpt-5.5"]  # tier row ∩ what the seat actually has
    out_err = capsys.readouterr()
    assert "at-1" not in out_err.out + out_err.err and "rt-1" not in out_err.out + out_err.err


def test_remote_join_api_codex_stored_seat_probes_and_serves_the_tier_set(monkeypatch, tmp_path, capsys):
    """The tracer join (issue 05): a stored free seat, no -m. ONE free probe (`GET {base}/models`
    with the seat's bearer + account-id header) proves reachability and the entitled set; the
    advertised set is the tier row ∩ the seat's live set, namespaced `codex:*`; the record's spec
    is kind-generic (endpoint_url/models/engine_label/api_kind — never a token, never the account
    id); the serve process spawns. A populated, verified tier draws NO tier warning."""
    from remote import api_keys
    from shared import run_records

    _seed_running_remote_grid(monkeypatch, tmp_path)
    spawned = _mock_remote_spawn(monkeypatch)
    api_keys.store_codex_bundle(_codex_bundle())
    seen = _mock_probe(monkeypatch, lambda request: httpx.Response(200, json={"models": [
        {"slug": "gpt-5.6-terra", "visibility": "list"},
        {"slug": "gpt-5.6-luna", "visibility": "list"},
        {"slug": "gpt-5.5", "visibility": "list"},
        {"slug": "gpt-5.4-mini", "visibility": "list"},
        {"slug": "codex-auto-review", "visibility": "hide"},
    ]}))

    assert cli.main(["join", "--api", "codex"]) == 0

    assert seen["calls"] == 1  # exactly one vendor call: the free probe
    assert seen["url"].startswith("https://chatgpt.com/backend-api/codex/models?client_version=")
    assert seen["headers"]["authorization"] == "Bearer tok-access"
    assert seen["headers"]["chatgpt-account-id"] == "acct-1"
    record = cli.provider._read_records("n1")["remote"]
    assert record["engines"] == [{
        "endpoint_url": "https://chatgpt.com/backend-api/codex",
        "models": ["codex:gpt-5.6-terra", "codex:gpt-5.6-luna", "codex:gpt-5.5", "codex:gpt-5.4-mini"],
        "engine_label": "codex",
        "api_kind": "codex",
        "plan_type": "free",  # issue 03: the seat's tier — the row serve reads vendor_rank from
    }]
    assert record["models"] == record["engines"][0]["models"]
    assert spawned["cmd"][-3:] == ["__remote-engine", "n1", "remote"]
    # Secret hygiene: the bearer and the account id reach no record and no terminal output.
    record_text = run_records.record_path("n1", "remote").read_text()
    assert "tok-access" not in record_text and "acct-1" not in record_text
    out_err = capsys.readouterr()
    assert "tok-access" not in out_err.out + out_err.err
    assert "acct-1" not in out_err.out + out_err.err
    assert "Warning" not in out_err.err  # a verified populated tier draws no tier warning


def test_remote_join_api_codex_tier_selects_the_row_and_warns_per_degrade_case(monkeypatch, tmp_path, capsys):
    """D-f row selection + the issue's mandated warn-log, with a synthetic second tier (the real
    table has only `free`, so a bigger `plus` row is the only way to SEE selection — review L3).
    Four seats, four outcomes:

      * plus (populated row)   → the plus row, NO tier line at all
      * None (vendor silent)   → minimal row + "Warning:" — the alarm the issue amendment demands:
                                 a vendor claim-rename must not silently downgrade seats forever
      * banana (unrecognized)  → minimal row + "Warning:" (vendor vocabulary drift)
      * go (known, unverified) → minimal row + an info line that is NOT a warning
    """
    from remote import api_keys

    free_row = api_catalog.CODEX_TIER_MODELS["free"][:2]
    plus_row = api_catalog.CODEX_TIER_MODELS["free"]
    monkeypatch.setattr(api_catalog, "CODEX_TIER_MODELS", {"free": free_row, "plus": plus_row})
    listing = {"models": [{"slug": e.vendor_name, "visibility": "list"} for e in plus_row]}

    cases = [
        ("plus", 4), (None, 2), ("banana", 2), ("go", 2),
    ]
    errs = {}
    for plan, expected_count in cases:
        home = tmp_path / f"case-{plan}"
        _seed_running_remote_grid(monkeypatch, home)
        _mock_remote_spawn(monkeypatch)
        api_keys.store_codex_bundle(_codex_bundle(plan_type=plan))
        _mock_probe(monkeypatch, lambda request: httpx.Response(200, json=listing))

        assert cli.main(["join", "--api", "codex"]) == 0

        record = cli.provider._read_records("n1")["remote"]
        assert len(record["models"]) == expected_count, f"plan={plan!r}"
        # The seat's tier rides the spec verbatim (issue 03) — even None (vendor silent): serve
        # reads each model's vendor_rank from the row this plan_type selects.
        assert record["engines"][0].get("plan_type") == plan, f"plan={plan!r}"
        errs[plan] = capsys.readouterr().err

    assert "Warning" not in errs["plus"] and "isn't verified" not in errs["plus"]
    assert "Warning:" in errs[None] and "no subscription tier" in errs[None]
    assert "newer grid release" in errs[None]  # the recovery hint — this case may be OUR staleness
    assert "Warning:" in errs["banana"]
    assert "recognize" in errs["banana"]
    assert "Warning:" not in errs["go"]  # known tier, merely unverified — informational only
    assert "isn't verified" in errs["go"] and "'go'" in errs["go"]


def test_remote_join_api_codex_explicit_model_errors_name_what_is_available(monkeypatch, tmp_path):
    """D5's explicit--m semantics: an explicit ask is REFUSED, never silently narrowed (the
    deliberate divergence from openai's skip — a personal seat asked for a model it lacks
    deserves a refusal). Outside the tier row → the tier's verified list is named (D-f bounds
    advertising to the verified row whatever the seat has); in the row but not on the seat →
    what the seat CAN serve is named, and the message never claims the seat "serves none" when
    it demonstrably serves others. Nothing spawns on either."""
    from remote import api_keys

    # Synthetic split (review L3): the union whitelist keeps all 4 entries, the tier row shrinks
    # to 2 — the only way a union-valid, tier-invalid name can exist while only `free` is real.
    free_row = api_catalog.CODEX_TIER_MODELS["free"][:2]  # terra, luna
    monkeypatch.setattr(api_catalog, "CODEX_TIER_MODELS", {"free": free_row})

    _seed_running_remote_grid(monkeypatch, tmp_path)
    spawned = _mock_remote_spawn(monkeypatch)
    api_keys.store_codex_bundle(_codex_bundle())
    _mock_probe(monkeypatch, lambda request: httpx.Response(200, json={"models": [
        {"slug": "gpt-5.6-terra", "visibility": "list"},
    ]}))

    # -m outside the tier row (but inside the union whitelist): the tier bound refuses it.
    with pytest.raises(SystemExit) as exc:
        cli.main(["join", "--api", "codex", "-m", "codex:gpt-5.5"])
    msg = str(exc.value)
    assert "codex:gpt-5.5" in msg and "'free'" in msg
    assert "codex:gpt-5.6-terra" in msg and "codex:gpt-5.6-luna" in msg  # the tier's verified list

    # -m inside the tier row but absent from the live seat: name what IS available, truthfully.
    with pytest.raises(SystemExit) as exc:
        cli.main(["join", "--api", "codex", "-m", "codex:gpt-5.6-luna"])
    msg = str(exc.value)
    assert "codex:gpt-5.6-luna" in msg
    assert "codex:gpt-5.6-terra" in msg   # the seat CAN serve this...
    assert "serves none" not in msg       # ...so "serves none" would be a lie

    assert cli.provider._read_records("n1") == {} and "cmd" not in spawned


def test_remote_join_api_codex_identical_rejoin_is_noop_with_zero_vendor_calls(monkeypatch, tmp_path, capsys):
    """Acceptance (issue 05): re-running an identical join is a no-op that performs ZERO vendor
    calls — same credential, no new models ⇒ nothing a probe could inform (D-f: the probe runs
    only when the credential or the engine spec actually changed). The openai path probes before
    the no-op gate; codex must not."""
    from remote import api_keys

    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    terminated = []
    monkeypatch.setattr(cli.remote_provider.run_records, "terminate_pid", lambda pid: terminated.append(pid) or True)
    api_keys.store_codex_bundle(_codex_bundle())
    _mock_probe(monkeypatch, lambda request: httpx.Response(200, json={"models": [
        {"slug": "gpt-5.5", "visibility": "list"},
    ]}))

    assert cli.main(["join", "--api", "codex"]) == 0  # first join probes and spawns
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: pytest.fail(
        "an identical re-join must perform zero vendor calls"
    ))

    assert cli.main(["join", "--api", "codex"]) == 0  # identical → no-op, no probe
    # A -m subset of what's already served is equally unchanged (narrowing is leave-then-rejoin).
    assert cli.main(["join", "--api", "codex", "-m", "codex:gpt-5.5"]) == 0

    out = capsys.readouterr().out
    assert out.count("nothing to append") == 2
    assert terminated == []


def test_remote_join_api_codex_adding_a_model_probes_once_and_hot_reloads(monkeypatch, tmp_path):
    """A -m beyond the live union IS a spec change: exactly one fresh probe runs and the union
    hot-reloads in place (SIGHUP, zero-drop — the openai append precedent)."""
    import signal as _sig

    from remote import api_keys

    _seed_running_remote_grid(monkeypatch, tmp_path)
    spawned = _mock_remote_spawn(monkeypatch)
    terminated = []
    monkeypatch.setattr(cli.remote_provider.run_records, "terminate_pid", lambda pid: terminated.append(pid) or True)
    api_keys.store_codex_bundle(_codex_bundle())
    seen = _mock_probe(monkeypatch, lambda request: httpx.Response(200, json={"models": [
        {"slug": "gpt-5.6-terra", "visibility": "list"},
        {"slug": "gpt-5.6-luna", "visibility": "list"},
    ]}))

    assert cli.main(["join", "--api", "codex", "-m", "codex:gpt-5.6-terra"]) == 0
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)
    assert cli.main(["join", "--api", "codex", "-m", "codex:gpt-5.6-luna"]) == 0  # a NEW model

    assert seen["calls"] == 2  # one probe per CHANGED join — never more
    record = cli.provider._read_records("n1")["remote"]
    assert record["models"] == ["codex:gpt-5.6-terra", "codex:gpt-5.6-luna"]
    assert (4242, _sig.SIGHUP) in spawned["signals"]  # hot-reloaded, zero-drop
    assert terminated == []


def test_merge_engines_refreshes_codex_plan_type_on_rejoin():
    """Re-joining a live codex engine (same base URL → the `existing` branch) must carry the FRESH
    plan_type onto the stored spec, not keep the first join's tier (issue 03 — code + silent-failure
    review). serve reads plan_type off the persisted spec to compute vendor_rank, so a write-once
    plan_type would rank every model against a stale tier row the moment a second tier ships.
    Non-codex specs (no plan_type key) stay byte-identical — the refresh is key-guarded."""
    codex = "https://chatgpt.com/backend-api/codex"
    base = [{"endpoint_url": codex, "models": ["codex:a"],
             "engine_label": "codex", "api_kind": "codex", "plan_type": "free"}]
    incoming = [{"endpoint_url": codex, "models": ["codex:a", "codex:b"],
                 "engine_label": "codex", "api_kind": "codex", "plan_type": "plus"}]
    merged, changed = cli.remote_provider._merge_engines(base, incoming)
    assert changed
    assert merged[0]["models"] == ["codex:a", "codex:b"]  # union preserved
    assert merged[0]["plan_type"] == "plus"               # the fresh tier wins, not stored "free"

    # A seat whose tier claim went silent (None) refreshes too — None is a real value, not "keep old".
    q_merged, _ = cli.remote_provider._merge_engines(base, [{**incoming[0], "plan_type": None}])
    assert q_merged[0]["plan_type"] is None

    # A hardware spec never carries plan_type → the merge adds no such key (byte-identical).
    hw_base = [{"endpoint_url": "http://h:11434/v1", "models": ["llama3"], "engine_label": None}]
    hw_incoming = [{"endpoint_url": "http://h:11434/v1", "models": ["llama3", "qwen"], "engine_label": None}]
    hw_merged, _ = cli.remote_provider._merge_engines(hw_base, hw_incoming)
    assert "plan_type" not in hw_merged[0]


def test_remote_join_api_codex_fresh_signin_onto_live_seat_respawns(monkeypatch, tmp_path, capsys):
    """A fresh sign-in while a codex engine is live is a credential rotation: the identity
    RESPAWNS (openai key-rotation policy — operator certainty that the new seat is live), never
    a silent no-op, never SIGHUP."""
    import signal as _sig

    from remote import api_keys

    _seed_running_remote_grid(monkeypatch, tmp_path)
    spawned = _mock_remote_spawn(monkeypatch)
    terminated = []
    monkeypatch.setattr(cli.remote_provider.run_records, "terminate_pid", lambda pid: terminated.append(pid) or True)
    api_keys.store_codex_bundle(_codex_bundle())
    _mock_probe(monkeypatch, lambda request: httpx.Response(200, json={"models": [
        {"slug": "gpt-5.5", "visibility": "list"},
    ]}))

    assert cli.main(["join", "--api", "codex"]) == 0
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)

    # The stored seat vanishes (e.g. the operator wiped it) — the re-join signs in fresh.
    monkeypatch.setattr(api_keys, "load_codex_bundle", lambda: None)
    _mock_codex_exchange(monkeypatch, models=["gpt-5.5"])  # answers the token endpoint AND the probe
    _mock_codex_browser(monkeypatch)

    assert cli.main(["join", "--api", "codex"]) == 0

    assert terminated == [4242]                           # stopped the stale-seat process...
    assert (4242, _sig.SIGHUP) not in spawned["signals"]  # ...instead of hot-reloading it
    assert "restarting the engine" in capsys.readouterr().out


def test_chat_refuses_codex_models_client_side_in_both_modes(monkeypatch, tmp_path):
    """Issue 05 consumer clarity: a `codex:*` model serves the vendor's `responses` endpoint, and
    `grid chat` (chat/completions) can NEVER call it — codex traffic is never translated (ADR
    0015 D-b). Refused before ANY network call in both modes — before the local grid lookup and
    before the remote sign-in gate (a signed-out box still gets THIS message, not "not signed
    in") — naming the client to use instead. Other namespaces flow past untouched."""
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: pytest.fail(
        "the codex refusal must precede any network call"
    ))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail(
        "the codex refusal must precede any network call"
    ))

    for argv in (
        ["chat", "-m", "codex:gpt-5.5", "hi"],              # local mode (the default)
        ["--remote", "chat", "-m", "codex:gpt-5.5", "hi"],  # remote, signed out — still THIS message
    ):
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        msg = str(exc.value)
        assert "responses" in msg and "grid chat" in msg, argv
        assert "Codex" in msg  # which client to use instead
        assert "grid info --env" in msg  # where its base URL comes from
        assert "not signed in" not in msg.lower()

    # A chat kind's namespace is untouched: openai:* flows past the guard (and this empty home
    # then fails it later for an unrelated, non-responses reason).
    with pytest.raises(SystemExit) as exc:
        cli.main(["chat", "-m", "openai:gpt-5.5", "hi"])
    assert "responses" not in str(exc.value)


def test_remote_join_api_codex_probe_failure_spawns_and_stores_nothing(monkeypatch, tmp_path):
    """Acceptance: every probe-failure class ends with no record written and nothing spawned. The
    OAuth bundle itself deliberately SURVIVES — it is the operator's credential, not this join's
    state, and a vendor outage or rate-limit must not force a re-auth."""
    from remote import api_keys

    for status in (429, 503, 400):
        home = tmp_path / f"case-{status}"
        _seed_running_remote_grid(monkeypatch, home)
        spawned = _mock_remote_spawn(monkeypatch)
        api_keys.store_codex_bundle(_codex_bundle())
        _mock_probe(monkeypatch, lambda request, s=status: httpx.Response(s, json={"detail": "d"}))

        with pytest.raises(SystemExit) as exc:
            cli.main(["join", "--api", "codex"])

        assert "Nothing was joined." in str(exc.value), f"status={status}"
        assert cli.provider._read_records("n1") == {} and "cmd" not in spawned
        assert api_keys.load_codex_bundle() is not None  # the credential survives a failed join


def test_remote_join_api_codex_dead_stored_seat_offers_one_fresh_signin(monkeypatch, tmp_path, capsys):
    """D6 — the PRD's sign-in inline "when the stored one is dead". A STORED seat the vendor
    rejects gets exactly ONE fresh sign-in and one re-probe on an interactive run; without this,
    every re-join would load the same dead bundle and fail forever (there is no other re-sign-in
    verb). The fresh seat then serves."""
    from remote import api_keys

    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    api_keys.store_codex_bundle(_codex_bundle())
    monkeypatch.setattr(cli.provider, "_interactive", lambda: True)

    fresh_access = _codex_jwt(
        {"chatgpt_account_id": "acct-2", "chatgpt_plan_type": "free"}, exp=2_000_000_000
    )
    calls = {"probes": 0}

    def vendor(request):
        if "/oauth/token" in str(request.url):
            return httpx.Response(200, json={
                "id_token": "id", "access_token": fresh_access, "refresh_token": "rt-2",
            })
        calls["probes"] += 1
        if request.headers.get("authorization") == "Bearer tok-access":  # the dead stored seat
            return httpx.Response(401, json={"detail": "denied"})
        return httpx.Response(200, json={"models": [{"slug": "gpt-5.5", "visibility": "list"}]})

    _mock_probe(monkeypatch, vendor)
    _mock_codex_browser(monkeypatch)

    assert cli.main(["join", "--api", "codex"]) == 0

    assert calls["probes"] == 2  # the dead probe + exactly one retry
    assert api_keys.load_codex_bundle().account_id == "acct-2"  # the fresh seat replaced the dead one
    err = capsys.readouterr().err
    assert "rejected" in err and "sign-in" in err  # the operator saw why a browser opened
    assert cli.provider._read_records("n1")["remote"]["models"] == ["codex:gpt-5.5"]


def test_remote_join_api_codex_rejected_seat_non_interactive_is_terminal(monkeypatch, tmp_path):
    """The same dead seat on a non-TTY run cannot re-sign-in: the auth-class taxonomy message
    ends the join — nothing spawned — and points recovery at an interactive run."""
    from remote import api_keys

    _seed_running_remote_grid(monkeypatch, tmp_path)
    spawned = _mock_remote_spawn(monkeypatch)
    api_keys.store_codex_bundle(_codex_bundle())
    monkeypatch.setattr(cli.provider, "_interactive", lambda: False)
    _mock_probe(monkeypatch, lambda request: httpx.Response(401, json={"detail": "denied"}))

    with pytest.raises(SystemExit) as exc:
        cli.main(["join", "--api", "codex"])

    msg = str(exc.value)
    assert "401" in msg and "Nothing was joined." in msg
    assert "interactive" in msg  # where recovery lives
    assert cli.provider._read_records("n1") == {} and "cmd" not in spawned


def test_remote_join_api_codex_fresh_seat_rejected_is_terminal_not_a_loop(monkeypatch, tmp_path):
    """A seat that fails auth seconds after a successful fresh sign-in is NOT retried with
    another sign-in — that loop would never converge. One sign-in, one probe, terminal."""
    _seed_running_remote_grid(monkeypatch, tmp_path)
    spawned = _mock_remote_spawn(monkeypatch)
    monkeypatch.setattr(cli.provider, "_interactive", lambda: True)

    access = _codex_jwt(
        {"chatgpt_account_id": "acct-1", "chatgpt_plan_type": "free"}, exp=2_000_000_000
    )
    calls = {"exchanges": 0}

    def vendor(request):
        if "/oauth/token" in str(request.url):
            calls["exchanges"] += 1
            return httpx.Response(200, json={
                "id_token": "id", "access_token": access, "refresh_token": "rt-1",
            })
        return httpx.Response(401, json={"detail": "denied"})

    _mock_probe(monkeypatch, vendor)
    _mock_codex_browser(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        cli.main(["join", "--api", "codex"])

    assert "401" in str(exc.value)
    assert calls["exchanges"] == 1  # exactly one sign-in — never a sign-in loop
    assert cli.provider._read_records("n1") == {} and "cmd" not in spawned


def test_codex_probe_all_hidden_listing_is_empty_not_drift(monkeypatch):
    """(silent-failure review #1) A listing whose rows ALL carry visibility:"hide" parsed
    perfectly — it is an empty SEAT (a legitimate vendor state), not contract drift: () comes
    back and the join's selection layer then says truthfully that the seat serves nothing. The
    old discriminator sent all-hidden operators chasing a grid upgrade that doesn't exist. Only
    a non-empty listing with NO readable slug anywhere is drift."""
    slugs, _ = _probe(monkeypatch, lambda request: httpx.Response(200, json={"models": [
        {"slug": "codex-auto-review", "visibility": "hide"},
        {"slug": "under-review-model", "visibility": "hide"},
    ]}))
    assert slugs == ()


def test_remote_join_api_codex_hidden_models_never_advertised_even_if_the_flag_renames(monkeypatch, tmp_path):
    """(silent-failure review #1, containment pin) If the vendor renames `visibility` the hide
    filter no-ops — fails OPEN. What actually bounds advertising is the verified tier row: a
    model absent from it (codex-auto-review) can never be advertised whatever the live listing
    says. This test pins that containment so the filter stays defence-in-depth, not the wall."""
    from remote import api_keys

    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    api_keys.store_codex_bundle(_codex_bundle())
    _mock_probe(monkeypatch, lambda request: httpx.Response(200, json={"models": [
        {"slug": "codex-auto-review", "visible": "hidden"},  # renamed flag → filter can't see it
        {"slug": "gpt-5.5", "visible": "listed"},
    ]}))

    assert cli.main(["join", "--api", "codex"]) == 0

    assert cli.provider._read_records("n1")["remote"]["models"] == ["codex:gpt-5.5"]


def test_remote_join_api_codex_rejoin_warns_again_while_tier_is_degraded(monkeypatch, tmp_path, capsys):
    """(silent-failure review #4) The mandated tier warn fires on EVERY join — including the
    zero-vendor-call no-op re-join — while the degraded condition persists. A seat stuck at
    plan_type=None must not warn once at the first join and then never again for the life of
    the seat: habitual re-joins (reboots, timers) would otherwise never resurface it, which is
    exactly the silent decay the issue's amendment exists to prevent."""
    from remote import api_keys

    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    api_keys.store_codex_bundle(_codex_bundle(plan_type=None))
    _mock_probe(monkeypatch, lambda request: httpx.Response(200, json={"models": [
        {"slug": "gpt-5.5", "visibility": "list"},
    ]}))

    assert cli.main(["join", "--api", "codex"]) == 0
    assert "no subscription tier" in capsys.readouterr().err  # join #1 warns

    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: pytest.fail(
        "a no-op re-join still performs zero vendor calls"
    ))
    for _ in range(2):
        assert cli.main(["join", "--api", "codex"]) == 0
        err = capsys.readouterr().err
        assert "Warning:" in err and "no subscription tier" in err  # ...and still warns


def test_remote_join_api_codex_noop_requires_the_current_backend_url(monkeypatch, tmp_path):
    """(silent-failure review #3a) The no-op precheck holds only while the live spec's
    endpoint_url matches the CURRENT catalog base_url. A release that moves the codex backend
    must not be echoed away by "nothing to append" forever — and it must not silently union two
    codex engines either (`_spec_key` keys by URL, so merge would APPEND the new-URL spec beside
    the old). The mismatch is refused loudly with the leave-then-rejoin remedy, offline."""
    import dataclasses

    from remote import api_keys

    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    api_keys.store_codex_bundle(_codex_bundle())
    _mock_probe(monkeypatch, lambda request: httpx.Response(200, json={"models": [
        {"slug": "gpt-5.5", "visibility": "list"},
    ]}))

    assert cli.main(["join", "--api", "codex"]) == 0  # joined against the old base_url
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)

    moved = dataclasses.replace(
        api_catalog.WHITELISTS["codex"], base_url="https://chatgpt.com/backend-api/codex-v2"
    )
    monkeypatch.setitem(api_catalog.WHITELISTS, "codex", moved)
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: pytest.fail(
        "the URL-mismatch refusal is offline — no probe against either URL"
    ))

    with pytest.raises(SystemExit) as exc:
        cli.main(["join", "--api", "codex"])

    msg = str(exc.value)
    assert "grid leave --engine codex" in msg  # the remedy
    record = cli.provider._read_records("n1")["remote"]
    assert [e["endpoint_url"] for e in record["engines"]] == ["https://chatgpt.com/backend-api/codex"]


def test_codex_bundle_load_refuses_a_header_unsafe_account_id(monkeypatch, tmp_path):
    """(security review) The account id is spent as an HTTP header value, and httpx will happily
    send CRLF in one (facts.md B5b). `decode_seat` guards the SIGN-IN path; this guards the LOAD
    path — the one every re-join takes — so the header-safety property travels with the store
    instead of living in one caller three hops upstream. An unusable entry reads as "not signed
    in" (None): a state the join fixes by signing in fresh, exactly like a half-written bundle."""
    from remote import api_keys, credentials
    from shared import paths

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    for hostile in ("acct-a\r\nX-Injected: pwned", "acct-\x00nul", "   ", ""):
        credentials.atomic_write_toml(paths.api_keys_file(), {"codex": {
            "access_token": "at", "refresh_token": "rt",
            "account_id": hostile, "last_refresh": 1,
        }})
        assert api_keys.load_codex_bundle() is None, repr(hostile)

    # The guard rejects unusable ids, not usable ones: a normal entry still loads.
    credentials.atomic_write_toml(paths.api_keys_file(), {"codex": {
        "access_token": "at", "refresh_token": "rt", "account_id": "acct-ok", "last_refresh": 1,
    }})
    loaded = api_keys.load_codex_bundle()
    assert loaded is not None and loaded.account_id == "acct-ok"


def test_remote_leave_codex_survivor_respawns_for_concurrency_flip(monkeypatch, tmp_path):
    """(code review) The leave direction of the codex concurrency flip: dropping the codex seat
    from a codex+openai union (pinned to 1) leaves an API-only survivor whose default is 8 — the
    shrink must RESPAWN so the pool can grow (a SIGHUP would keep the live pool of 1, silently
    serving 8x fewer concurrent requests than advertised)."""
    import signal as _sig

    _seed_remote_identity(monkeypatch, tmp_path, [
        {"endpoint_url": "https://chatgpt.com/backend-api/codex", "models": ["codex:gpt-5.5"],
         "engine_label": "codex", "api_kind": "codex"},
        {"endpoint_url": "https://api.openai.com/v1", "models": ["openai:gpt-5.5"],
         "engine_label": "openai", "api_kind": "openai"},
    ])
    spawned = _mock_remote_spawn(monkeypatch)
    terminated = []
    monkeypatch.setattr(cli.remote_provider.run_records, "terminate_pid", lambda pid: terminated.append(pid) or True)
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)

    assert cli.main(["leave", "--engine", "codex"]) == 0

    rec = cli.provider._read_records("n1")["remote"]
    assert [e["endpoint_url"] for e in rec["engines"]] == ["https://api.openai.com/v1"]
    assert terminated == [4242]                           # respawned so the pool can grow to 8...
    assert (4242, _sig.SIGHUP) not in spawned["signals"]  # ...not left at 1 by a hot-reload


def test_remote_join_api_key_path_refuses_a_kind_that_names_no_env_var(monkeypatch, tmp_path):
    """The twin of the bug that already bit at `_api_bearers`, in the join's key path.

    `env_var` is `str | None` now, so `os.environ.get(whitelist.env_var)` is a `TypeError` for any
    kind that doesn't name one — an unhandled traceback, not this repo's clean-SystemExit contract.
    Unreachable today (codex routes to `_resolve_codex_targets` first, and
    `test_api_whitelist_key_kinds_name_their_env_var` forces every OTHER kind to name one), which is
    exactly what makes it a landmine: the next credential-less kind trips it, and nothing before this
    test would have caught it. `require_bearer` already guards its side; this mirrors it.
    """
    from shared.models import api_catalog

    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: pytest.fail("no credential, no vendor call"))
    # A second OAuth-shaped kind, as issue 04 leaves the door open for — entries present (so the
    # `-m` guard passes) but no env var to read a key from.
    monkeypatch.setitem(api_catalog.WHITELISTS, "futurekind", api_catalog.ApiWhitelist(
        last_verified="2026-07-15",
        base_url="https://future.example",
        entries=(api_catalog.ApiModelEntry(
            vendor_name="m1", context_window=1000, supports_tools=False, supports_vision=False,
            supports_json_mode=False, supports_structured_outputs=False,
        ),),
        env_var=None,
    ))

    with pytest.raises(SystemExit) as exc:  # not TypeError
        cli.main(["join", "--api", "futurekind"])

    assert "futurekind" in str(exc.value)
    assert "None" not in str(exc.value)  # never interpolate the absent env var's name
    assert cli.provider._read_records("n1") == {}


def test_remote_join_no_browser_says_so_when_it_cannot_apply(monkeypatch, tmp_path, capsys):
    """`--no-browser` only means anything for the codex OAuth sign-in. Everywhere else it is inert,
    and this repo says so rather than swallowing it — the same courtesy `--pricing-input` and
    `--engine-label` already get. A flag that silently does nothing teaches the operator it worked.

    Note rather than error: it is a no-op, not a conflict, and failing the join over an inert flag
    would be worse than the silence.
    """
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)

    assert cli.main(["join", "--serve", "m", "--no-browser"]) == 0

    err = capsys.readouterr().err
    assert "--no-browser" in err and "--api codex" in err


def test_remote_join_no_browser_is_silent_where_it_does_apply(monkeypatch, tmp_path, capsys):
    """... and the note must NOT fire on the one command the flag is for. A warning that cries wolf
    on correct usage is worse than no warning."""
    from remote import api_keys

    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    api_keys.store_codex_bundle(_bundle())  # stored seat: no sign-in, no prompt needed

    with pytest.raises(SystemExit):  # the model table is issue 05's
        cli.main(["join", "--api", "codex", "--no-browser"])

    assert "--no-browser" not in capsys.readouterr().err


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


def test_remote_join_bare_never_auto_joins_api_engine(monkeypatch, tmp_path):
    """A bare `grid join` (auto-detect) must NEVER include an API engine just because a key file
    exists on disk — API engines join only when --api is explicit (no surprise upstream spend)."""
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    from remote import api_keys
    api_keys.store_key("openai", "sk-on-disk")
    monkeypatch.setattr(cli.provider, "_detect", lambda host: [
        detect.DetectedEngine(label="ollama", endpoint_url="http://h:11434/v1", models=["llama3"]),
    ])

    assert cli.main(["join"]) == 0

    record = cli.provider._read_records("n1")["remote"]
    assert all(not spec.get("api_kind") for spec in record["engines"])  # hardware only
    assert record["models"] == ["llama3"]


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


def test_remote_join_api_append_onto_live_hardware_hot_reloads(monkeypatch, tmp_path):
    """Appending an API engine onto a live hardware identity is ZERO-DROP: the vendor key now lives in
    the durable key store, so the reload re-reads it and swaps the bearer in place — SIGHUP, no respawn,
    no dropped in-flight requests (issue 05; closes issue 02's respawn caveat). Rotation and the
    concurrency-flip reverse order still respawn (separate tests)."""
    import signal as _sig

    _seed_running_remote_grid(monkeypatch, tmp_path)
    spawned = _mock_remote_spawn(monkeypatch)
    terminated = []
    monkeypatch.setattr(cli.remote_provider.run_records, "terminate_pid", lambda pid: terminated.append(pid) or True)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    _mock_vendor(monkeypatch, lambda request: httpx.Response(200, json={"data": [{"id": "gpt-5.5"}]}))

    assert cli.main(["join", "--at", "http://h:11434/v1", "-m", "llama3"]) == 0  # first join spawns
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)
    assert cli.main(["join", "--api", "openai", "-m", "openai:gpt-5.5"]) == 0  # api-append hot-reloads

    rec = cli.provider._read_records("n1")["remote"]
    assert [e["endpoint_url"] for e in rec["engines"]] == ["http://h:11434/v1", "https://api.openai.com/v1"]
    assert rec["models"] == ["llama3", "openai:gpt-5.5"]
    assert spawned["signals"] == [(4242, _sig.SIGHUP)]  # hot-reloaded the live singleton in place
    assert terminated == []                             # never stopped the live process (zero-drop)


def test_remote_join_api_rotated_env_key_overwrites_store_and_respawns(monkeypatch, tmp_path, capsys):
    """Rotation is one command: a re-join with a NEW env key overwrites the stored key and RESPAWNS
    the live identity (never a silent no-op, never SIGHUP). Not because the mechanism can't hot-swap
    the bearer — it now can (issue 05) — but because rotation is a deliberate policy: a fresh process
    gives the operator certainty the new key is live, never a silent in-place swap."""
    import signal as _sig

    _seed_running_remote_grid(monkeypatch, tmp_path)
    spawned = _mock_remote_spawn(monkeypatch)
    terminated = []
    monkeypatch.setattr(cli.remote_provider.run_records, "terminate_pid", lambda pid: terminated.append(pid) or True)
    _mock_vendor(monkeypatch, lambda request: httpx.Response(200, json={"data": [{"id": "gpt-5.5"}]}))

    monkeypatch.setenv("OPENAI_API_KEY", "sk-old-111")
    assert cli.main(["join", "--api", "openai", "-m", "openai:gpt-5.5"]) == 0
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)

    monkeypatch.setenv("OPENAI_API_KEY", "sk-new-222")
    # Same models — without rotation this re-join would be the idempotent no-op.
    assert cli.main(["join", "--api", "openai", "-m", "openai:gpt-5.5"]) == 0

    from remote import api_keys
    assert api_keys.load_key("openai") == "sk-new-222"    # rotated on disk
    assert terminated == [4242]                           # stopped the stale-bearer process...
    assert (4242, _sig.SIGHUP) not in spawned["signals"]  # ...instead of hot-reloading it
    out_err = capsys.readouterr()
    assert "rotat" in out_err.out.lower()                 # the operator sees why it restarted
    assert "sk-new-222" not in out_err.out + out_err.err and "sk-old-111" not in out_err.out + out_err.err


def test_remote_join_api_rejoin_with_same_stored_key_is_noop(monkeypatch, tmp_path, capsys):
    """A re-join whose key resolves to the SAME stored value stays the idempotent no-op — rotation
    must not turn every repeat join into a restart."""
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    terminated = []
    monkeypatch.setattr(cli.remote_provider.run_records, "terminate_pid", lambda pid: terminated.append(pid) or True)
    _mock_vendor(monkeypatch, lambda request: httpx.Response(200, json={"data": [{"id": "gpt-5.5"}]}))

    monkeypatch.setenv("OPENAI_API_KEY", "sk-same-333")
    assert cli.main(["join", "--api", "openai", "-m", "openai:gpt-5.5"]) == 0
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)
    assert cli.main(["join", "--api", "openai", "-m", "openai:gpt-5.5"]) == 0

    assert "nothing to append" in capsys.readouterr().out  # the no-op message
    assert terminated == []


def test_remote_join_noop_surfaces_last_reload_error(monkeypatch, tmp_path, capsys):
    """The idempotent no-op re-join WARNS when the engine's last hot-reload failed inside the detached
    process (`last_reload_error` on the record): the operator's earlier join printed success while the
    old union kept serving, so a bare 'nothing to append' would compound the false success
    (issue 05 follow-up — reload failures were signaled only in the engine's log)."""
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)

    assert cli.main(["join", "--at", "http://h:11434/v1", "-m", "llama3"]) == 0
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)
    # The detached process recorded a reload failure (e.g. its key store was tampered with mid-flight).
    cli.remote_provider.run_records.update_record("n1", "remote", last_reload_error="no key is stored for openai")

    assert cli.main(["join", "--at", "http://h:11434/v1", "-m", "llama3"]) == 0  # identical → no-op
    out_err = capsys.readouterr()
    assert "nothing to append" in out_err.out
    assert "no key is stored for openai" in out_err.err  # the failure is surfaced, not silent


def test_remote_join_hardware_onto_api_only_respawns_for_concurrency_flip(monkeypatch, tmp_path):
    """Adding a hardware engine to an API-only identity flips the concurrency default (8 → 1). The
    pool is sized once at spawn, so this join must RESPAWN — a SIGHUP would leave 8 workers
    hammering a hardware engine sized for 1."""
    import signal as _sig

    _seed_running_remote_grid(monkeypatch, tmp_path)
    spawned = _mock_remote_spawn(monkeypatch)
    terminated = []
    monkeypatch.setattr(cli.remote_provider.run_records, "terminate_pid", lambda pid: terminated.append(pid) or True)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    _mock_vendor(monkeypatch, lambda request: httpx.Response(200, json={"data": [{"id": "gpt-5.5"}]}))

    assert cli.main(["join", "--api", "openai", "-m", "openai:gpt-5.5"]) == 0  # api-only: default 8
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)
    assert cli.main(["join", "--at", "http://h:11434/v1", "-m", "llama3"]) == 0  # union gains hardware

    assert terminated == [4242]                           # respawned to resize the pool...
    assert (4242, _sig.SIGHUP) not in spawned["signals"]  # ...not hot-reloaded at the old size


def test_remote_leave_shrink_to_api_only_respawns_for_concurrency_flip(monkeypatch, tmp_path):
    """The reverse flip: dropping the last hardware engine leaves an API-only union whose default
    is 8 — the shrink must respawn so the pool can grow (a SIGHUP keeps the live pool of 1)."""
    import signal as _sig

    _seed_remote_identity(monkeypatch, tmp_path, [
        {"endpoint_url": "http://h:11434/v1", "models": ["llama3"], "engine_label": "ollama"},
        {"endpoint_url": "https://api.openai.com/v1", "models": ["openai:gpt-5.5"],
         "engine_label": "openai", "api_kind": "openai"},
    ])
    spawned = _mock_remote_spawn(monkeypatch)
    terminated = []
    monkeypatch.setattr(cli.remote_provider.run_records, "terminate_pid", lambda pid: terminated.append(pid) or True)
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)

    assert cli.main(["leave", "--engine", "http://h:11434/v1"]) == 0

    rec = cli.provider._read_records("n1")["remote"]
    assert [e["endpoint_url"] for e in rec["engines"]] == ["https://api.openai.com/v1"]
    assert terminated == [4242]                           # respawned to grow the pool to 8...
    assert (4242, _sig.SIGHUP) not in spawned["signals"]  # ...not left at 1 by a hot-reload


def test_remote_join_api_additive_preserves_explicit_max_concurrency(monkeypatch, tmp_path):
    """An explicit --max-concurrency on an API identity survives a re-join that omits it — exactly
    the hardware-engine preserve rule (and no concurrency-flip respawn fires: 2 == 2)."""
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_remote_spawn(monkeypatch)
    monkeypatch.setattr(cli.remote_provider.run_records, "terminate_pid", lambda pid: True)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    _mock_vendor(monkeypatch, lambda request: httpx.Response(
        200, json={"data": [{"id": "gpt-5.5"}, {"id": "gpt-5.4"}]}
    ))

    assert cli.main(["join", "--api", "openai", "-m", "openai:gpt-5.5", "--max-concurrency", "2"]) == 0
    assert cli.provider._read_records("n1")["remote"]["max_concurrency"] == 2
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)
    assert cli.main(["join", "--api", "openai", "-m", "openai:gpt-5.4"]) == 0  # no flag re-passed

    rec = cli.provider._read_records("n1")["remote"]
    assert rec["max_concurrency"] == 2  # preserved, not reset
    assert rec["models"] == ["openai:gpt-5.5", "openai:gpt-5.4"]


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


def test_remote_leave_engine_reports_respawn_fallback(monkeypatch, tmp_path, capsys):
    """If the SIGHUP finds the singleton's pid gone (the TOCTOU window), the shrink falls back to a
    respawn — and the message says so instead of claiming zero-drop, mirroring cmd_remote_join's
    honest reporting (issue 05 follow-up: the return of _hot_reload_identity was discarded here)."""
    _seed_remote_identity(monkeypatch, tmp_path, [
        {"endpoint_url": "http://h:11434/v1", "models": ["llama3"], "engine_label": "ollama"},
        {"endpoint_url": "http://h:8000/v1", "models": ["mistral"], "engine_label": "vllm"},
    ])
    _mock_remote_spawn(monkeypatch)
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)
    monkeypatch.setattr(cli.remote_provider.run_records, "terminate_pid", lambda pid: True)

    def gone(pid):  # the singleton died between the liveness check and the signal
        raise ProcessLookupError(pid)

    monkeypatch.setattr(cli.remote_provider, "_signal_reload", gone)

    assert cli.main(["leave", "--engine", "http://h:11434/v1"]) == 0
    out = capsys.readouterr().out
    assert "Dropped 'http://h:11434/v1'" in out and "re-serving 1 engine(s)" in out
    assert "restarted" in out          # honest: it fell back to a respawn...
    assert "hot-reload" not in out     # ...and never claims a zero-drop reload


def test_remote_leave_engine_openai_drops_only_api_reserves_survivors(monkeypatch, tmp_path):
    """`grid leave --engine openai` drops ONLY the API engine (matched by its `openai` kind label) and
    hot-reloads the surviving hardware engine in place — the hardware keeps serving, zero-drop (issue 05)."""
    import signal as _sig

    _seed_remote_identity(monkeypatch, tmp_path, [
        {"endpoint_url": "http://h:11434/v1", "models": ["llama3"], "engine_label": "ollama"},
        {"endpoint_url": "https://api.openai.com/v1", "models": ["openai:gpt-5.5"],
         "engine_label": "openai", "api_kind": "openai"},
    ])
    spawned = _mock_remote_spawn(monkeypatch)
    terminated = []
    monkeypatch.setattr(cli.remote_provider.run_records, "terminate_pid", lambda pid: terminated.append(pid) or True)
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)

    assert cli.main(["leave", "--engine", "openai"]) == 0  # drop the API engine by its kind label
    rec = cli.provider._read_records("n1")["remote"]
    assert [e["endpoint_url"] for e in rec["engines"]] == ["http://h:11434/v1"]  # only the hardware survivor
    assert rec["models"] == ["llama3"] and rec["endpoint_url"] == "http://h:11434/v1"
    assert spawned["signals"] == [(4242, _sig.SIGHUP)]  # hot-reloaded the reduced union in place
    assert terminated == []                             # never stopped the live process (zero-drop)


def test_remote_leave_engine_openai_last_tears_down(monkeypatch, tmp_path, capsys):
    """Leaving the API engine when it is the ONLY engine tears the identity down (never reload-to-empty)."""
    _seed_remote_identity(monkeypatch, tmp_path, [
        {"endpoint_url": "https://api.openai.com/v1", "models": ["openai:gpt-5.5"],
         "engine_label": "openai", "api_kind": "openai"}], pid=0)

    assert cli.main(["leave", "--engine", "openai"]) == 0
    assert cli.provider._read_records("n1") == {}  # last engine removed → the whole identity is torn down
    assert "last engine" in capsys.readouterr().out.lower()


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
# codex refresh CAS (remote/api_keys.rotate_codex_bundle — ADR 0015 D-d, issue 06)
# Real store + real flock on GRID_HOME=tmp_path; the vendor is always MockTransport.
# ---------------------------------------------------------------------------

def _raw_codex_entry():
    """The codex TOML table as written — for asserting journal presence, which
    `load_codex_bundle` deliberately does not surface."""
    from remote import credentials

    return credentials.load_toml(paths.api_keys_file()).get("codex") or {}


def test_codex_rotate_journals_before_the_exchange_and_persist_clears_it(monkeypatch, tmp_path):
    """ADR 0015 D-d's crash-window discipline: the in-flight exchange is journaled BEFORE the
    vendor call (a kill after the vendor rotates but before we persist must be detectable), and
    the new bundle is persisted the moment it returns — a wholesale replace, so the journal
    field vanishes with no cleanup code to forget."""
    from remote import api_keys

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    api_keys.store_codex_bundle(_codex_bundle())
    new_access = _codex_jwt({"chatgpt_account_id": "acct-1"}, exp=2_000_000_000)

    def vendor(request):
        assert "refresh_pending_since" in _raw_codex_entry()  # journaled before the network call
        return httpx.Response(200, json={"access_token": new_access, "refresh_token": "rt-2"})

    _mock_vendor(monkeypatch, vendor)

    fresh = api_keys.rotate_codex_bundle("tok-access")

    assert fresh.access_token == new_access and fresh.refresh_token == "rt-2"
    assert api_keys.load_codex_bundle() == fresh  # persisted immediately, not held in memory
    assert "refresh_pending_since" not in _raw_codex_entry()  # the wholesale replace cleared it


def test_codex_rotate_adopts_a_fresher_store_without_spending(monkeypatch, tmp_path):
    """The loser's half of the CAS: the store already holds a token fresher than the one that just
    401ed, so another process rotated first — adopt it and spend NOTHING (the refresh token is
    single-use; a second exchange would revoke the seat)."""
    from remote import api_keys, codex_oauth

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    winner = codex_oauth.CodexBundle(
        access_token="tok-access-2", refresh_token="tok-refresh-2", account_id="acct-1",
        plan_type="free", last_refresh=1_700_000_000,
    )
    api_keys.store_codex_bundle(winner)
    _mock_vendor(monkeypatch, lambda request: pytest.fail("the loser must never reach the vendor"))

    adopted = api_keys.rotate_codex_bundle("tok-access-STALE")

    assert adopted == winner
    assert "refresh_pending_since" not in _raw_codex_entry()  # adopt writes nothing


def test_codex_rotate_two_concurrent_refreshers_spend_one_exchange(monkeypatch, tmp_path):
    """AC 4, on the REAL file lock: each `file_lock` acquisition opens its own fd, so two threads
    genuinely contend like two processes do. The winner journals + exchanges + persists under the
    lock; the loser blocks, re-reads, sees the fresher token, and adopts — exactly ONE vendor
    exchange for N racers."""
    from remote import api_keys

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    api_keys.store_codex_bundle(_codex_bundle())
    new_access = _codex_jwt({"chatgpt_account_id": "acct-1"}, exp=2_000_000_000)
    exchanges = []

    def vendor(request):
        exchanges.append(1)
        time.sleep(0.2)  # hold the winner in the exchange so the loser really blocks on the flock
        return httpx.Response(200, json={"access_token": new_access, "refresh_token": "rt-2"})

    _mock_vendor(monkeypatch, vendor)
    results, errors = [], []

    def racer():
        try:
            results.append(api_keys.rotate_codex_bundle("tok-access"))
        except Exception as exc:  # surfaced below — a hang would be worse than a raise
            errors.append(exc)

    threads = [threading.Thread(target=racer, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert errors == []
    assert len(exchanges) == 1  # one vendor exchange; the loser adopted
    assert [r.access_token for r in results] == [new_access, new_access]
    assert api_keys.load_codex_bundle().access_token == new_access


def test_codex_rotate_reports_an_interrupted_rotation_when_a_journal_was_left_behind(monkeypatch, tmp_path):
    """AC 6: a kill between the journal write and the persist leaves `refresh_pending_since` on
    disk. The NEXT attempt that the vendor refuses must say so — `interrupted=True` is what turns
    "the vendor said no" into "a previous rotation was lost; sign in again" instead of a silent
    zombie. The journal stays (only a persisted bundle resolves the doubt) and the stored bundle
    is untouched."""
    from remote import api_keys, credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    api_keys.store_codex_bundle(_codex_bundle())
    # Simulate the kill: a journal from a dead attempt, planted exactly as the CAS writes it.
    data = credentials.load_toml(paths.api_keys_file())
    credentials.atomic_write_toml(
        paths.api_keys_file(),
        {**data, "codex": {**data["codex"], "refresh_pending_since": 1_700_000_000}},
    )
    _mock_vendor(monkeypatch, lambda request: httpx.Response(400, json={"error": "invalid_grant"}))

    with pytest.raises(api_keys.CodexRotationRefused) as caught:
        api_keys.rotate_codex_bundle("tok-access")

    assert caught.value.interrupted is True
    assert caught.value.status_code == 400
    assert "refresh_pending_since" in _raw_codex_entry()  # still unresolved — keeps re-diagnosing
    assert api_keys.load_codex_bundle() == _codex_bundle()  # the stored bundle is untouched


def test_codex_rotate_refusal_without_a_journal_is_not_interrupted(monkeypatch, tmp_path):
    """The plain dead-seat case (revoked, signed out elsewhere): the vendor refuses but no prior
    exchange died mid-flight — `interrupted` must be False, or every refusal would carry the
    scarier lost-rotation diagnosis and the operator could not tell the two apart.

    And it must STAY False on every subsequent refusal: a definitive refusal is a completed
    attempt, so it withdraws its OWN journal — left behind, attempt 1's pre-call journal write
    would become attempt 2's "a prior exchange died" evidence, and a stably-dead seat would be
    misdiagnosed as a lost rotation from the second refusal onward, forever (silent-failure
    review). `interrupted` means exactly "an exchange died between the vendor call and the
    persist" — nothing here died."""
    from remote import api_keys

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    api_keys.store_codex_bundle(_codex_bundle())
    _mock_vendor(monkeypatch, lambda request: httpx.Response(401, json={}))

    with pytest.raises(api_keys.CodexRotationRefused) as caught:
        api_keys.rotate_codex_bundle("tok-access")
    assert caught.value.interrupted is False
    assert "refresh_pending_since" not in _raw_codex_entry()  # a completed attempt cleans up

    with pytest.raises(api_keys.CodexRotationRefused) as caught:
        api_keys.rotate_codex_bundle("tok-access")  # the same dead seat, one cooldown later
    assert caught.value.interrupted is False  # still a plain refusal — not self-poisoned


def test_codex_journal_is_cleared_by_any_persisted_bundle_and_never_affects_loading(monkeypatch, tmp_path):
    """The journal's whole lifecycle rides the wholesale replace: a successful rotation clears it
    (pinned in the tracer test), a fresh sign-in (`store_codex_bundle`) clears it, and
    `load_codex_bundle` never even sees it — a journaled entry still loads as a valid bundle, so
    a leftover journal can never lock an operator out of their own seat."""
    from remote import api_keys, credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    api_keys.store_codex_bundle(_codex_bundle())
    data = credentials.load_toml(paths.api_keys_file())
    credentials.atomic_write_toml(
        paths.api_keys_file(),
        {**data, "codex": {**data["codex"], "refresh_pending_since": 1_700_000_000}},
    )

    assert api_keys.load_codex_bundle() == _codex_bundle()  # journaled entry loads fine

    api_keys.store_codex_bundle(_codex_bundle(plan_type="plus"))  # the re-sign-in path

    assert "refresh_pending_since" not in _raw_codex_entry()  # replace=True dropped it
    assert api_keys.load_codex_bundle().plan_type == "plus"


def test_codex_rotate_clears_its_own_journal_when_the_request_never_left(monkeypatch, tmp_path):
    """A connect failure means the exchange provably never left this machine — nothing was spent,
    so OUR journal is withdrawn before re-raising. Left behind, it would sharpen a WRONG
    "rotation was lost" diagnosis out of an ordinary offline blip. An AMBIGUOUS failure
    (`ReadTimeout` — the grant may have reached the vendor) keeps the journal, truthfully."""
    from remote import api_keys, codex_oauth

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    api_keys.store_codex_bundle(_codex_bundle())

    def connect_error(request):
        raise httpx.ConnectError("offline", request=request)

    _mock_vendor(monkeypatch, connect_error)
    with pytest.raises(codex_oauth.RefreshUnavailable):
        api_keys.rotate_codex_bundle("tok-access")
    assert "refresh_pending_since" not in _raw_codex_entry()  # withdrawn — nothing was spent

    def read_timeout(request):
        raise httpx.ReadTimeout("mid-flight", request=request)

    _mock_vendor(monkeypatch, read_timeout)
    with pytest.raises(codex_oauth.RefreshUnavailable):
        api_keys.rotate_codex_bundle("tok-access")
    assert "refresh_pending_since" in _raw_codex_entry()  # ambiguous — the doubt is real, keep it


def test_codex_rotate_never_withdraws_a_journal_it_did_not_write(monkeypatch, tmp_path):
    """Withdrawal is scoped to the attempt's OWN journal: a journal left by an EARLIER crash is
    someone's unresolved doubt, and neither a connect failure (spent nothing, resolved nothing)
    nor a clean refusal may erase it — that would downgrade the next refusal's diagnosis from
    "a rotation was lost" to a plain revocation (python review). Only a persisted bundle — a
    rotation or a re-sign-in — resolves it."""
    from remote import api_keys, codex_oauth, credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    api_keys.store_codex_bundle(_codex_bundle())
    data = credentials.load_toml(paths.api_keys_file())
    credentials.atomic_write_toml(
        paths.api_keys_file(),
        {**data, "codex": {**data["codex"], "refresh_pending_since": 1_700_000_000}},
    )

    def connect_error(request):
        raise httpx.ConnectError("offline", request=request)

    _mock_vendor(monkeypatch, connect_error)
    with pytest.raises(codex_oauth.RefreshUnavailable):
        api_keys.rotate_codex_bundle("tok-access")
    assert "refresh_pending_since" in _raw_codex_entry()  # the earlier crash's doubt survives

    _mock_vendor(monkeypatch, lambda request: httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(api_keys.CodexRotationRefused) as caught:
        api_keys.rotate_codex_bundle("tok-access")
    assert caught.value.interrupted is True  # the crash's diagnosis, intact through the blip
    assert "refresh_pending_since" in _raw_codex_entry()  # and it keeps resurfacing


def test_codex_rotate_guards_the_empty_store_and_the_shutdown_race(monkeypatch, tmp_path):
    """Two refusals that must spend NOTHING: a store with no seat (signed out mid-serve) raises
    `CodexNotSignedIn`; a shutdown that wins the race to the lock raises `RotationAbandoned` with
    no journal written — the drain invariant is 'flag unset ⇒ nothing was spent AND nothing will
    be', and a journal written after stop would break its second half."""
    from remote import api_keys

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _mock_vendor(monkeypatch, lambda request: pytest.fail("neither guard may reach the vendor"))

    with pytest.raises(api_keys.CodexNotSignedIn):
        api_keys.rotate_codex_bundle("tok-access")  # empty store

    api_keys.store_codex_bundle(_codex_bundle())
    stopping = threading.Event()
    stopping.set()
    flag = threading.Event()

    with pytest.raises(api_keys.RotationAbandoned):
        api_keys.rotate_codex_bundle("tok-access", exchange_in_flight=flag, abandon=stopping)

    assert not flag.is_set()  # cleared in the finally, abandon path included
    assert "refresh_pending_since" not in _raw_codex_entry()  # no journal after stop, ever


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
    from remote import api_keys, serve
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
    # A record with an api spec makes the reload read the key store (issue 05); seed a deterministic
    # key per kind so `_api_bearers` resolves instead of raising, and the bearer is assertable.
    for kind in dict.fromkeys(s.get("api_kind") for s in engines if s.get("api_kind")):
        api_keys.store_key(kind, f"sk-test-{kind}")
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
    startup, so the two can't drift. `grid join --api` onto a live identity reaches this path now
    that the bearer is rebuilt from the key store on reload (issue 05); the bearer itself is covered
    by `test_serve_reload_new_api_spec_attaches_vendor_bearer`."""
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
    # The endpoint-gating map follows the reload: the vendor route is marked as kind `openai`,
    # the hardware route stays unmarked.
    assert state.route_and_kind("openai:gpt-5.5") == ("https://api.openai.com/v1", "openai")
    assert state.route_and_kind("a") == ("http://e1/v1", None)


def test_serve_reload_new_api_spec_attaches_vendor_bearer(monkeypatch, tmp_path):
    """A hot-reload that gains an API spec must attach that engine's vendor bearer to its forwards:
    the key is re-read from the machine-local store on reload (not fixed at spawn), so `grid join
    --api` onto a live identity hot-reloads a WORKING engine instead of respawning (issue 05 — the
    key store closes issue 02's respawn caveat). Hardware forwards stay bearer-free."""
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
    monkeypatch.setattr(relay, "register_node", lambda *a, **kw: None)

    serve._reload_once(state, "remote")

    # The appended vendor engine now forwards WITH the bearer (seeded by the harness as sk-test-openai);
    # the hardware engine stays bearer-free — the API key rides only its own vendor URL.
    assert serve._forward_headers(state, "https://api.openai.com/v1").get("Authorization") == "Bearer sk-test-openai"
    assert "Authorization" not in serve._forward_headers(state, "http://e1/v1")


def test_serve_reload_dropping_api_spec_removes_its_bearer(monkeypatch, tmp_path):
    """The vendor bearer follows the union: a `leave --engine openai` shrink drops the bearer on the same
    swap that drops the route, so the departed vendor URL is bearer-free afterwards — no in-flight job can
    forward to a vendor this identity no longer serves (issue 05, the leave-direction of the bind-once seam)."""
    from remote import probe, relay, serve
    from shared import run_records

    state = _seed_reload_state(monkeypatch, tmp_path, retained=[
        ("http://e1/v1", ["a"], ["a"], {"schema_version": 1, "models": {"a": {}}}),
    ], engines=[
        {"endpoint_url": "http://e1/v1", "models": ["a"], "engine_label": None},
        {"endpoint_url": "https://api.openai.com/v1", "models": ["openai:gpt-5.5"],
         "engine_label": "openai", "api_kind": "openai"},
    ])
    monkeypatch.setattr(probe, "capabilities", lambda *a, **k: {"schema_version": 1, "models": {}})
    monkeypatch.setattr(relay, "register_node", lambda *a, **kw: None)

    serve._reload_once(state, "remote")  # append the api engine → its vendor bearer is now live
    assert serve._forward_headers(state, "https://api.openai.com/v1").get("Authorization") == "Bearer sk-test-openai"

    run_records.write_record("n1", "remote", {  # `leave --engine openai` rewrites the record to hardware-only
        "engine_id": "remote", "grid_id": "n1",
        "engines": [{"endpoint_url": "http://e1/v1", "models": ["a"], "engine_label": None}]})
    serve._reload_once(state, "remote")

    assert "openai:gpt-5.5" not in state.models                         # the vendor engine left the advertised union...
    assert "Authorization" not in serve._forward_headers(state, "https://api.openai.com/v1")  # ...and its bearer with it


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


def test_serve_reload_loop_persists_failure_on_the_record(monkeypatch, tmp_path):
    """A failed reload leaves a trace the CLI can surface: `last_reload_error` on the run record. The
    CLI already printed success when it delivered the SIGHUP, so without this the only signal that the
    union was NOT re-advertised is this process's log (issue 05 follow-up)."""
    from remote import serve
    from shared import run_records

    state = _seed_reload_state(monkeypatch, tmp_path, retained=[], engines=[
        {"endpoint_url": "http://e1/v1", "models": ["a"], "engine_label": None},
    ])

    def failing_reload(s, eid):
        s.stop.set()  # one iteration is enough
        raise SystemExit("This engine serves --api openai models but no key is stored")

    monkeypatch.setattr(serve, "_reload_once", failing_reload)
    state.reload_requested.set()
    serve._reload_loop(state, "remote")

    rec = run_records.read_record("n1", "remote")
    assert "no key is stored" in rec["last_reload_error"]  # the failure survived the process's log


def test_serve_reload_success_clears_last_reload_error(monkeypatch, tmp_path):
    """A successful reload clears a previous failure's `last_reload_error`, so the CLI stops warning
    about a condition that healed."""
    from remote import relay, serve
    from shared import run_records

    state = _seed_reload_state(monkeypatch, tmp_path, retained=[
        ("http://e1/v1", ["a"], ["a"], {"schema_version": 1, "models": {"a": {}}}),
    ], engines=[
        {"endpoint_url": "http://e1/v1", "models": ["a"], "engine_label": None},
    ])
    run_records.update_record("n1", "remote", last_reload_error="previous failure")
    monkeypatch.setattr(relay, "register_node", lambda *a, **k: None)

    serve._reload_once(state, "remote")

    assert "last_reload_error" not in (run_records.read_record("n1", "remote") or {})


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


def test_serve_reload_missing_api_key_keeps_old_routing(monkeypatch, tmp_path):
    """If an appended API spec's key is in neither the store nor the env, the reload aborts BEFORE the
    swap: `_api_bearers` raises, the old snapshot keeps serving, and nothing is registered — the loop's
    guard then logs and survives (issue 05, mirrors the failed-probe abort). Never a partial union with a
    bearer-less openai:* engine advertised to the relay."""
    from remote import api_keys, probe, relay, serve

    state = _seed_reload_state(monkeypatch, tmp_path, retained=[
        ("http://e1/v1", ["a"], ["a"], {"schema_version": 1, "models": {"a": {}}}),
    ], engines=[
        {"endpoint_url": "http://e1/v1", "models": ["a"], "engine_label": None},
        {"endpoint_url": "https://api.openai.com/v1", "models": ["openai:gpt-5.5"],
         "engine_label": "openai", "api_kind": "openai"},
    ])
    # The key the harness seeded is gone (revoked/deleted out of band) and there is no env fallback.
    monkeypatch.setattr(api_keys, "load_key", lambda kind: None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(probe, "capabilities", lambda *a, **k: pytest.fail("api engines are never probed"))
    monkeypatch.setattr(relay, "register_node", lambda *a, **k: pytest.fail("must not register a partial union"))
    before = state.snapshot()

    with pytest.raises(SystemExit, match="OPENAI_API_KEY"):
        serve._reload_once(state, "remote")     # propagates; _reload_loop's guard catches + logs in production
    assert state.snapshot() is before           # no partial apply — the old routing keeps serving untouched


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


def test_serve_handle_job_pairs_route_and_bearer_from_one_snapshot(monkeypatch, tmp_path):
    """Bind-once (issue 05): a job reads its route AND its vendor bearer from ONE snapshot, so a reload
    swapping mid-job can never pair a route from one union with a bearer from another. Two generations
    give the SAME model different vendor URLs+keys; a torn pair would surface as a target URL whose key
    isn't in the bound snapshot (no Authorization) — asserted absent under a concurrent swapper."""
    import threading as _t

    from remote import serve

    state = _serve_state(monkeypatch, tmp_path, models=["m"], routes={"m": "http://a/v1"}, upstream={},
                         bearer_by_url={"http://a/v1": "KEY-A"})
    snap_a = state.snapshot()
    snap_b = serve._Snapshot.build(routes={"m": "http://b/v1"}, upstream={}, models=["m"], capabilities={},
                                   meta={}, pricing={}, max_concurrency=1,
                                   bearer_by_url={"http://b/v1": "KEY-B"})
    expected = {"http://a/v1": "Bearer KEY-A", "http://b/v1": "Bearer KEY-B"}
    errors = []
    iters = 5000  # fixed count → deterministic, can't hang

    def swapper():
        for i in range(iters):
            state.apply(snap_b if i % 2 else snap_a, [])

    def reader():
        for _ in range(iters):
            snap = state.snapshot()                       # bind once, exactly as handle_job does
            target = state.route_and_kind("m", snap)[0]
            auth = serve._forward_headers(state, target, snap).get("Authorization")
            if auth != expected.get(target):              # a torn (route, bearer) pair → wrong/absent key
                errors.append((target, auth))
                return

    threads = [_t.Thread(target=swapper, daemon=True)] + [_t.Thread(target=reader, daemon=True) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert errors == []   # route and bearer always came from the same snapshot generation


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


def test_remote_engine_startup_missing_api_key_everywhere_exits_naming_fix(monkeypatch, tmp_path, capsys):
    """A respawned serve process whose record carries an API spec but whose key is gone from BOTH
    the key store and the environment must exit non-zero naming the re-join fix (in the engine log)
    — not come up advertising models whose every job would 401 upstream."""
    from remote import serve

    monkeypatch.setenv("GRID_HOME", str(tmp_path))  # empty tmp home ⇒ empty key store
    record = {"grid_id": "n1", "signaling_url": "https://relay.example", "media": False,
              "engines": [{"endpoint_url": "https://api.openai.com/v1", "models": ["openai:gpt-5.5"],
                           "engine_label": "openai", "api_kind": "openai"}]}
    monkeypatch.setattr(serve.run_records, "read_record", lambda g, e: record)
    monkeypatch.setattr(serve, "_load_tokens", lambda net: ("AT", "RT"))
    monkeypatch.setattr(serve, "_node_id_from_token", lambda t: "node-1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert serve.run_remote_engine_from_record("n1", "remote") == 1
    err = capsys.readouterr().err
    assert "OPENAI_API_KEY" in err
    assert "grid join --api openai" in err


def test_api_key_store_preserves_other_kinds(monkeypatch, tmp_path):
    """Storing one kind's key must never clobber another kind's entry — the store is a single file
    holding every service kind (the read-merge-write is serialized under a file lock)."""
    from remote import api_keys

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    api_keys.store_key("openai", "sk-openai")
    api_keys.store_key("other", "sk-other")
    api_keys.store_key("openai", "sk-openai-2")  # rotation of one kind...

    assert api_keys.load_key("openai") == "sk-openai-2"
    assert api_keys.load_key("other") == "sk-other"  # ...leaves the sibling intact


def _bundle(**overrides):
    from remote.codex_oauth import CodexBundle

    return CodexBundle(**{
        "access_token": "at-1", "refresh_token": "rt-1", "account_id": "acct-1",
        "plan_type": "free", "last_refresh": 1_700_000_000, **overrides,
    })


def test_codex_bundle_round_trips_through_the_store_beside_an_openai_key(monkeypatch, tmp_path):
    """ADR 0015 D-c: the OAuth bundle lives under the `codex` kind in the SAME store as vendor API
    keys — one file, 0o600, the one hardened atomic writer. The two kinds' shapes differ (one string
    vs a rotating bundle) but their entries stay independent, in both directions."""
    from remote import api_keys

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    api_keys.store_key("openai", "sk-openai")
    api_keys.store_codex_bundle(_bundle())

    assert api_keys.load_codex_bundle() == _bundle()
    assert api_keys.load_key("openai") == "sk-openai"  # writing codex left the sibling alone

    key_file = paths.api_keys_file()
    assert (key_file.stat().st_mode & 0o777) == 0o600
    data = tomllib.loads(key_file.read_text())
    assert data["openai"] == {"key": "sk-openai"}  # ... byte-for-byte, not merely present
    assert data["codex"]["account_id"] == "acct-1"

    # ... and the reverse: rotating openai's key leaves the whole codex bundle intact.
    api_keys.store_key("openai", "sk-openai-2")
    assert api_keys.load_codex_bundle() == _bundle()


def test_codex_bundle_survives_a_tier_the_token_never_stated(monkeypatch, tmp_path):
    """`plan_type` is None when the token doesn't say, and TOML has no null — so a naive write either
    raises or drops the key. It must round-trip as None, because ADR 0015 D-f reads exactly that to
    decide "unknown tier ⇒ the minimal whitelist"; a None that came back as the string "None" would
    look like a tier and advertise a seat's worth of models it cannot serve."""
    from remote import api_keys

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    api_keys.store_codex_bundle(_bundle(plan_type=None))

    assert api_keys.load_codex_bundle() == _bundle(plan_type=None)
    assert "plan_type" not in tomllib.loads(paths.api_keys_file().read_text())["codex"]


def test_codex_bundle_load_is_none_when_the_store_has_no_codex_entry(monkeypatch, tmp_path):
    """No file, and a file holding only another kind, both mean "not signed in" — the join's
    signal to run the OAuth flow. Never a half-built bundle."""
    from remote import api_keys

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    assert api_keys.load_codex_bundle() is None

    api_keys.store_key("openai", "sk-openai")
    assert api_keys.load_codex_bundle() is None


def _codex_record():
    return {"engines": [{
        "endpoint_url": "https://chatgpt.com/backend-api/codex",
        "models": ["codex:gpt-5.4-mini"], "engine_label": "codex", "api_kind": "codex",
    }]}


def test_codex_seat_resolution_moved_to_the_holder_with_the_same_guarantees(monkeypatch, tmp_path):
    """Issue 06 (ADR 0015 D-d) moved the codex credential OUT of `_api_bearers` — a snapshot copy
    would go stale at the first rotation — and into the seat holder, primed at startup/reload by
    `_prime_codex_seat`. The guarantees the old resolution carried move WITH it, re-pinned here at
    the new seam (superseding issue 05's `_api_bearers`-level pins):

    * a stored seat resolves — at startup and on every hot-reload (the prime path);
    * ADR 0015 D-c: NO env-var input path. A stray `CODEX_API_KEY` is never adopted as a
      subscription — not signed in means not signed in, terminal, naming the only real fix and
      never advertising the env var as a way out. Branch order still makes this true for
      `require_bearer` too: the codex kind returns before any env var is consulted.
    """
    from remote import api_keys, serve

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_API_KEY", "sk-not-a-seat")

    state = _codex_serve_state(monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as exc:
        serve._prime_codex_seat(state, _codex_record())  # env var set, store empty → still refused

    msg = str(exc.value)
    assert "sk-not-a-seat" not in msg
    assert "CODEX_API_KEY" not in msg
    assert "grid join --api codex" in msg  # the only way to get a seat

    with pytest.raises(SystemExit):
        api_keys.require_bearer("codex")  # the shape-blind resolver refuses identically

    api_keys.store_codex_bundle(_bundle(access_token="at-codex"))
    serve._prime_codex_seat(state, _codex_record())
    assert state.codex_seat.bundle().access_token == "at-codex"  # a stored seat resolves
    assert api_keys.require_bearer("codex") == "at-codex"


def test_remote_engine_startup_missing_api_key_reaps_record(monkeypatch, tmp_path):
    """A startup that dies before registering (key gone from store AND env) must reap its on-disk
    run record like any other died-before-registering engine — not leave a stale singleton that
    forces a `grid leave --all`."""
    from remote import serve
    from shared import run_records as rr

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    rr.write_record("n1", "remote", {
        "engine_id": "remote", "grid_id": "n1", "signaling_url": "https://relay.example",
        "media": False,
        "engines": [{"endpoint_url": "https://api.openai.com/v1", "models": ["openai:gpt-5.5"],
                     "engine_label": "openai", "api_kind": "openai"}],
    })
    monkeypatch.setattr(serve, "_load_tokens", lambda net: ("AT", "RT"))
    monkeypatch.setattr(serve, "_node_id_from_token", lambda t: "node-1")

    assert serve.run_remote_engine_from_record("n1", "remote") == 1  # died pre-register, non-zero
    assert not rr.record_path("n1", "remote").exists()  # reaped, not stranded


def test_remote_engine_startup_reads_api_key_from_env_when_store_empty(monkeypatch, tmp_path):
    """Env fallback: a pre-store record respawned in a key-bearing environment still comes up (the
    store is authoritative but its absence must not brick an upgraded install)."""
    from remote import serve

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-999")
    record = {"grid_id": "n1", "signaling_url": "https://relay.example", "media": False,
              "engines": [{"endpoint_url": "https://api.openai.com/v1", "models": ["openai:gpt-5.5"],
                           "engine_label": "openai", "api_kind": "openai"}]}
    assert serve._api_bearers(record) == {"https://api.openai.com/v1": "sk-env-999"}


def test_remote_engine_api_record_registers_static_caps_and_kind(monkeypatch, tmp_path):
    """Startup-seam proof for the API-engine tracer bullet: a record with one api spec comes up with
    whitelist caps (no probe), advertises the namespaced models, reports kind `openai` on the grid
    page, and holds the STORED key ready for forwards (env no longer required at serve time) —
    while the record itself stays key-free."""
    from remote import api_keys, probe, relay, serve

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    api_keys.store_key("openai", "sk-test-123")
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
    # Honest advertisement: openai serves both dialects now (issue 03) but never the legacy
    # completions endpoint, so the relay is told exactly that (the serve-side gate stays as defense
    # in depth). Sourced from the kind's catalog row, not a hardcoded list.
    assert entry["endpoints"] == ["chat/completions", "responses"]
    assert seen["meta"]["engine"] == "openai"  # the grid page shows the API engine's kind
    assert state_seen["bearer_by_url"] == {"https://api.openai.com/v1": "sk-test-123"}


def test_remote_engine_startup_wires_api_endpoint_gating(monkeypatch, tmp_path):
    """Startup derives the API-kind map from the record, so a legacy `completions` job is gated
    from the very first poll — not only after a reload."""
    from remote import api_keys, relay, serve

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    api_keys.store_key("openai", "sk-test-123")
    record = {"grid_id": "n1", "signaling_url": "https://relay.example", "media": False,
              "engines": [{"endpoint_url": "https://api.openai.com/v1", "models": ["openai:gpt-5.5"],
                           "engine_label": "openai", "api_kind": "openai"}]}
    monkeypatch.setattr(serve.run_records, "read_record", lambda g, e: record)
    monkeypatch.setattr(serve, "_load_tokens", lambda net: ("AT", "RT"))
    monkeypatch.setattr(serve, "_node_id_from_token", lambda t: "node-1")
    monkeypatch.setattr(relay, "register_node", lambda *a, **k: None)
    monkeypatch.setattr(relay, "unregister_node", lambda *a, **k: None)
    monkeypatch.setattr(serve, "_heartbeat_loop", lambda s: None)
    captured = {}
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))

    def fake_poll(s):
        serve.handle_job(s, {"transaction_id": "t1", "endpoint_path": "completions",
                             "body": {"model": "openai:gpt-5.5"}})
        s.stop.set()

    monkeypatch.setattr(serve, "_poll_loop", fake_poll)

    assert serve.run_remote_engine_from_record("n1", "remote") == 0
    assert "chat/completions" in captured["error"]  # gated at the startup seam, no upstream call


def test_effective_max_concurrency_default_rules():
    """API-only union → 8; any hardware engine or media → 1; explicit value always wins; a legacy
    flat record (no engines field) → 1. One shared rule for the CLI and the serve loop."""
    from shared import run_records

    api = {"endpoint_url": "https://api.openai.com/v1", "models": ["openai:gpt-5.5"], "api_kind": "openai"}
    hw = {"endpoint_url": "http://h:11434/v1", "models": ["llama3"]}
    assert run_records.effective_max_concurrency({"engines": [api]}) == 8
    assert run_records.effective_max_concurrency({"engines": [api, hw]}) == 1
    assert run_records.effective_max_concurrency({"engines": [hw]}) == 1
    assert run_records.effective_max_concurrency({"engines": [api], "media": True}) == 1
    assert run_records.effective_max_concurrency({"engines": [], "media": True}) == 1
    assert run_records.effective_max_concurrency({}) == 1
    assert run_records.effective_max_concurrency({"engines": [api], "max_concurrency": 3}) == 3
    assert run_records.effective_max_concurrency({"engines": [hw], "max_concurrency": 8}) == 8


def test_effective_max_concurrency_codex_union_pins_one():
    """ADR 0015 D-f: a codex seat is flat-rate — ANY codex engine in the union pins the default
    to 1, overriding the API-only 8 (a seat must not be hammered eight-wide by default). An
    explicit --max-concurrency still wins: the operator asked. Lives in the ONE shared
    derivation, so the CLI's reload-vs-respawn gate and serve startup cannot desync on it."""
    from shared import run_records

    codex = {"endpoint_url": "https://chatgpt.com/backend-api/codex",
             "models": ["codex:gpt-5.5"], "api_kind": "codex"}
    openai_spec = {"endpoint_url": "https://api.openai.com/v1",
                   "models": ["openai:gpt-5.5"], "api_kind": "openai"}
    hw = {"endpoint_url": "http://h:11434/v1", "models": ["llama3"]}

    assert run_records.effective_max_concurrency({"engines": [codex]}) == 1
    assert run_records.effective_max_concurrency({"engines": [openai_spec, codex]}) == 1
    assert run_records.effective_max_concurrency({"engines": [codex, hw]}) == 1
    assert run_records.effective_max_concurrency({"engines": [codex], "max_concurrency": 4}) == 4
    assert run_records.effective_max_concurrency({"engines": [openai_spec]}) == 8  # openai-only keeps 8


def test_remote_engine_codex_union_defaults_to_one_worker(monkeypatch, tmp_path):
    """The serve-side half of the codex concurrency rule: a codex-only identity advertises AND
    pools exactly 1 worker by default (the `api_only_defaults_to_eight_workers` skeleton, flipped);
    an explicit --max-concurrency still wins."""
    from remote import api_keys, relay, serve

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    api_keys.store_codex_bundle(_codex_bundle())
    record = {"grid_id": "n1", "signaling_url": "https://relay.example", "media": False,
              "engines": [{"endpoint_url": "https://chatgpt.com/backend-api/codex",
                           "models": ["codex:gpt-5.5"],
                           "engine_label": "codex", "api_kind": "codex"}]}
    monkeypatch.setattr(serve.run_records, "read_record", lambda g, e: record)
    monkeypatch.setattr(serve, "_load_tokens", lambda net: ("AT", "RT"))
    monkeypatch.setattr(serve, "_node_id_from_token", lambda t: "node-1")
    seen = {}
    monkeypatch.setattr(relay, "register_node", lambda url, tok, node, **kw: seen.update(kw))
    monkeypatch.setattr(relay, "unregister_node", lambda *a, **k: None)
    monkeypatch.setattr(serve, "_heartbeat_loop", lambda s: None)
    state_seen = {}

    def fake_poll(s):
        state_seen["max_concurrency"] = s.max_concurrency
        s.stop.set()

    monkeypatch.setattr(serve, "_poll_loop", fake_poll)

    assert serve.run_remote_engine_from_record("n1", "remote") == 0
    assert seen["max_concurrency"] == 1          # advertised to the relay
    assert state_seen["max_concurrency"] == 1    # ... and sizing the real pool

    record = {**record, "max_concurrency": 3}
    assert serve.run_remote_engine_from_record("n1", "remote") == 0
    assert seen["max_concurrency"] == 3 and state_seen["max_concurrency"] == 3


def test_remote_join_codex_onto_api_only_respawns_for_concurrency_flip(monkeypatch, tmp_path):
    """Appending codex to an API-only identity flips the concurrency default (8 → 1). The pool
    is sized once at spawn, so this join must RESPAWN — a SIGHUP would leave 8 workers hammering
    a flat-rate seat the default exists to protect."""
    import signal as _sig

    from remote import api_keys

    _seed_running_remote_grid(monkeypatch, tmp_path)
    spawned = _mock_remote_spawn(monkeypatch)
    terminated = []
    monkeypatch.setattr(cli.remote_provider.run_records, "terminate_pid", lambda pid: terminated.append(pid) or True)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    api_keys.store_codex_bundle(_codex_bundle())

    def vendor(request):
        if "api.openai.com" in str(request.url):
            return httpx.Response(200, json={"data": [{"id": "gpt-5.5"}]})
        return httpx.Response(200, json={"models": [{"slug": "gpt-5.5", "visibility": "list"}]})

    _mock_vendor(monkeypatch, vendor)

    assert cli.main(["join", "--api", "openai", "-m", "openai:gpt-5.5"]) == 0  # 8-worker identity
    monkeypatch.setattr(cli.remote_provider.run_records, "pid_alive", lambda pid: True)
    assert cli.main(["join", "--api", "codex"]) == 0  # default flips 8 → 1

    rec = cli.provider._read_records("n1")["remote"]
    assert {e["api_kind"] for e in rec["engines"]} == {"openai", "codex"}
    assert terminated == [4242]                           # stopped the 8-worker process...
    assert (4242, _sig.SIGHUP) not in spawned["signals"]  # ...instead of hot-reloading it


def _codex_serve_skeleton(monkeypatch, tmp_path, models, *, plan_type=None):
    """The register-capture skeleton (:api_only_defaults_to_eight_workers pattern) for a
    codex-only record serving ``models``. ``plan_type`` (when given) rides the engine spec exactly
    as the CLI writes it — the tier row serve reads vendor_rank from. Returns the kwargs
    register_node saw."""
    from remote import api_keys, relay, serve

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    api_keys.store_codex_bundle(_codex_bundle())
    spec = {"endpoint_url": "https://chatgpt.com/backend-api/codex",
            "models": list(models), "engine_label": "codex", "api_kind": "codex"}
    if plan_type is not None:
        spec["plan_type"] = plan_type
    record = {"grid_id": "n1", "signaling_url": "https://relay.example", "media": False,
              "engines": [spec]}
    monkeypatch.setattr(serve.run_records, "read_record", lambda g, e: record)
    monkeypatch.setattr(serve, "_load_tokens", lambda net: ("AT", "RT"))
    monkeypatch.setattr(serve, "_node_id_from_token", lambda t: "node-1")
    seen = {}
    monkeypatch.setattr(relay, "register_node", lambda url, tok, node, **kw: seen.update(kw))
    monkeypatch.setattr(relay, "unregister_node", lambda *a, **k: None)
    monkeypatch.setattr(serve, "_heartbeat_loop", lambda s: None)
    monkeypatch.setattr(serve, "_poll_loop", lambda s: s.stop.set())

    assert serve.run_remote_engine_from_record("n1", "remote") == 0
    return seen


def test_remote_engine_codex_record_registers_honest_responses_caps(monkeypatch, tmp_path):
    """The codex capability envelope carries ONLY what passthrough can honestly claim (issue 05):
    `endpoints: ["responses"]` (the wire literal grid-src's per-model filter reads — absent means
    chat-only there, so old CLIs fail closed), the verified context window, features
    {vision, tools, parallel_tool_calls}, and the join-time `vendor_rank` (issue 03 / ADR 0016 —
    a top-level int sibling of context_window, the seat's tier-row position, 1 = most capable). It
    OMITS — not False — the chat-dialect flags (json_object/json_schema), `max_output_tokens` and
    the `limits` block (facts #1: the backend has no output cap under any name; a fabricated 64000
    would be a provider-written limit the relay might act on), and audio/logprobs."""
    seen = _codex_serve_skeleton(monkeypatch, tmp_path, ["codex:gpt-5.5"])

    caps = seen["capabilities"]
    assert caps["schema_version"] == 1
    entry = caps["models"]["codex:gpt-5.5"]
    assert entry["endpoints"] == ["responses"]
    assert entry["input_modalities"] == ["text", "image"]
    assert entry["output_modalities"] == ["text"]
    assert entry["context_window"] == 272_000
    # vendor_rank rides top-level (NOT inside features — the grid-src reader takes it there). gpt-5.5
    # is index 2 in the free tier row [terra, luna, gpt-5.5, gpt-5.4-mini] → rank 3.
    assert entry["vendor_rank"] == 3
    assert entry["features"] == {"vision": True, "tools": True, "parallel_tool_calls": True}
    assert "max_output_tokens" not in entry
    assert "limits" not in entry
    assert "json_object" not in entry["features"] and "json_schema" not in entry["features"]


def test_remote_engine_codex_model_gone_from_whitelist_degrades_honestly(monkeypatch, tmp_path, capsys):
    """The stale-catalog degrade (catalog edited between join and respawn) stays honest for
    codex: a warn plus an entry that still says `responses`-only with NO feature claims — never
    the chat-dialect all-False shape, and never a fabricated output cap. A model gone from the
    whitelist has no tier-row position either, so `vendor_rank` is omitted (issue 03)."""
    seen = _codex_serve_skeleton(monkeypatch, tmp_path, ["codex:ghost"])

    entry = seen["capabilities"]["models"]["codex:ghost"]
    assert entry["endpoints"] == ["responses"]
    assert entry["features"] == {}
    assert "max_output_tokens" not in entry and "limits" not in entry
    assert "context_window" not in entry  # unknown is omitted, never invented
    assert "vendor_rank" not in entry  # absent from the row → omit the fact, never rank 0/None
    assert "no longer in the codex whitelist" in capsys.readouterr().err


def test_remote_engine_codex_vendor_rank_follows_the_seats_tier_row(monkeypatch, tmp_path, capsys):
    """vendor_rank is sourced from the SEAT'S tier row, not the flat union (issue 03 / ADR 0016).
    A synthetic `plus` tier (the real table has only `free`) REVERSES the free order and drops one
    model; a seat whose stored plan_type is `plus` must advertise ranks in the plus order. A model
    advertised but OUTSIDE the seat's row carries no rank — the frozen union resolves its entry, but
    the row has no position for it, so the fact is omitted (graceful drift) AND an operator warn
    fires (silent-failure review: a silent no-rank is indistinguishable from tier-table drift).
    Rows are built from REAL entries so `find_advertised` (frozen union) still resolves every name."""
    free = api_catalog.CODEX_TIER_MODELS["free"]
    terra, luna, big, mini = free  # curated free order: terra 1, luna 2, gpt-5.5 3, gpt-5.4-mini 4
    # plus row: reversed + gpt-5.5 dropped → mini 1, luna 2, terra 3; gpt-5.5 is off-row.
    monkeypatch.setattr(api_catalog, "CODEX_TIER_MODELS", {"free": free, "plus": (mini, luna, terra)})

    advertised = [f"codex:{e.vendor_name}" for e in (mini, luna, terra, big)]
    models = _codex_serve_skeleton(monkeypatch, tmp_path, advertised, plan_type="plus")["capabilities"]["models"]

    assert models[f"codex:{mini.vendor_name}"]["vendor_rank"] == 1
    assert models[f"codex:{luna.vendor_name}"]["vendor_rank"] == 2
    assert models[f"codex:{terra.vendor_name}"]["vendor_rank"] == 3
    # gpt-5.5 resolves in the frozen union but has no position in the plus row → no rank, and a warn.
    assert "vendor_rank" not in models[f"codex:{big.vendor_name}"]
    err = capsys.readouterr().err
    assert f"codex:{big.vendor_name}" in err and "rank" in err  # off-row model flagged (not silent)
    assert f"codex:{mini.vendor_name}" not in err               # a ranked model is not flagged


def test_remote_engine_api_only_defaults_to_eight_workers(monkeypatch, tmp_path):
    """An identity whose union is API-only defaults to 8 poll workers, advertised AND held by the
    live state (the pool is sized from it) — several consumers must not queue behind one worker
    while the vendor sits idle. An explicit --max-concurrency still wins."""
    from remote import api_keys, relay, serve

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    api_keys.store_key("openai", "sk-test-123")
    record = {"grid_id": "n1", "signaling_url": "https://relay.example", "media": False,
              "engines": [{"endpoint_url": "https://api.openai.com/v1", "models": ["openai:gpt-5.5"],
                           "engine_label": "openai", "api_kind": "openai"}]}
    monkeypatch.setattr(serve.run_records, "read_record", lambda g, e: record)
    monkeypatch.setattr(serve, "_load_tokens", lambda net: ("AT", "RT"))
    monkeypatch.setattr(serve, "_node_id_from_token", lambda t: "node-1")
    seen = {}
    monkeypatch.setattr(relay, "register_node", lambda url, tok, node, **kw: seen.update(kw))
    monkeypatch.setattr(relay, "unregister_node", lambda *a, **k: None)
    monkeypatch.setattr(serve, "_heartbeat_loop", lambda s: None)
    state_seen = {}

    def fake_poll(s):
        state_seen["max_concurrency"] = s.max_concurrency
        s.stop.set()

    monkeypatch.setattr(serve, "_poll_loop", fake_poll)

    assert serve.run_remote_engine_from_record("n1", "remote") == 0
    assert seen["max_concurrency"] == 8          # advertised to the relay
    assert state_seen["max_concurrency"] == 8    # ... and sizing the real pool

    # Explicit flag wins over the API-only default.
    record = {**record, "max_concurrency": 3}
    assert serve.run_remote_engine_from_record("n1", "remote") == 0
    assert seen["max_concurrency"] == 3 and state_seen["max_concurrency"] == 3


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
    base URL, vendor-name rewrite, the key attached per target URL, and the target marked as an
    API engine (endpoint gating)."""
    kwargs = dict(
        models=["openai:gpt-5.5"],
        routes={"openai:gpt-5.5": "https://api.openai.com/v1"},
        upstream={"openai:gpt-5.5": "gpt-5.5"},
        bearer_by_url={"https://api.openai.com/v1": "sk-test-123"},
        api_kind_by_url={"https://api.openai.com/v1": "openai"},
    )
    kwargs.update(overrides)
    return _serve_state(monkeypatch, tmp_path, **kwargs)


def test_serve_handle_job_api_completions_gated_without_upstream_call(monkeypatch, tmp_path):
    """An API engine serves chat/completions ONLY: a legacy `completions` job routed to it submits
    a structured 'not served' error to the relay and never forwards upstream."""
    from remote import relay, serve

    state = _api_serve_state(monkeypatch, tmp_path)
    captured = {}
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))
    monkeypatch.setattr(relay, "submit_response", lambda *a, **k: pytest.fail("a gated job has no response"))
    _mock_serve_engine(monkeypatch, lambda request: pytest.fail("a gated job must never reach the vendor"))

    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "completions",
                             "body": {"model": "openai:gpt-5.5"}})

    assert "chat/completions" in captured["error"]  # names what IS served
    assert "openai" in captured["error"]            # ... and which engine refused


def test_serve_handle_job_api_completions_gated_on_single_url_fallback_too(monkeypatch, tmp_path):
    """route() falls back to the single distinct URL for an unknown model — on an API-only identity
    a `completions` job with ANY model name must still be gated, not blind-forwarded upstream."""
    from remote import relay, serve

    state = _api_serve_state(monkeypatch, tmp_path)
    captured = {}
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))
    _mock_serve_engine(monkeypatch, lambda request: pytest.fail("a gated job must never reach the vendor"))

    serve.handle_job(state, {"transaction_id": "t2", "endpoint_path": "completions",
                             "body": {"model": "some-unknown-model"}})

    assert "chat/completions" in captured["error"]


def test_serve_handle_job_api_translates_max_tokens_to_vendor_param(monkeypatch, tmp_path):
    """The whole OpenAI whitelist is GPT-5.x, which refuses `max_tokens` ("Use
    'max_completion_tokens' instead"). The master normalises every request the other way — to
    `max_tokens`, the only name hardware engines know — so forwarding to an API engine must
    translate to the vendor's name AND drop `max_tokens`, the name the vendor rejects."""
    from remote import relay, serve

    state = _api_serve_state(monkeypatch, tmp_path)
    seen = {}
    monkeypatch.setattr(relay, "submit_response", lambda url, tok, txn, *, content, stream: None)
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: pytest.fail(message))

    def vendor(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    _mock_serve_engine(monkeypatch, vendor)
    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "chat/completions",
                             "body": {"model": "openai:gpt-5.5", "max_tokens": 24}, "is_stream": False})

    assert seen["max_completion_tokens"] == 24
    assert "max_tokens" not in seen        # the exact name the vendor 400s on
    assert seen["model"] == "gpt-5.5"      # vendor-name rewrite still applies


def test_serve_handle_job_api_max_tokens_wins_over_stale_completion_tokens(monkeypatch, tmp_path):
    """A consumer that sent `max_completion_tokens` reaches the provider carrying BOTH names: the
    master copies it into `max_tokens` without removing the original. `max_tokens` is the value the
    master validated against its cap, so it is the authoritative one — and only one name may go
    upstream."""
    from remote import relay, serve

    state = _api_serve_state(monkeypatch, tmp_path)
    seen = {}
    monkeypatch.setattr(relay, "submit_response", lambda url, tok, txn, *, content, stream: None)
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: pytest.fail(message))

    def vendor(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    _mock_serve_engine(monkeypatch, vendor)
    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "chat/completions",
                             "body": {"model": "openai:gpt-5.5", "max_tokens": 24,
                                      "max_completion_tokens": 999}, "is_stream": False})

    assert seen["max_completion_tokens"] == 24
    assert "max_tokens" not in seen


def test_serve_handle_job_api_refuses_unsupported_param_before_vendor_call(monkeypatch, tmp_path):
    """The whole OpenAI whitelist (GPT-5.x) rejects `stop` — verified against the live API on
    2026-07-14, all four models. Forwarding it burns a vendor round-trip to learn a static fact
    the catalog already knows, so the provider refuses up front, wearing the vendor's own error
    shape ("engine error 400: {openai-style json}") — the same bytes the vendor would have sent,
    and the same format the relay's terminal-error mapper parses."""
    from remote import relay, serve

    state = _api_serve_state(monkeypatch, tmp_path)
    captured = {}
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))
    monkeypatch.setattr(relay, "submit_response", lambda *a, **k: pytest.fail("a refused job has no response"))
    _mock_serve_engine(monkeypatch, lambda request: pytest.fail("a refused job must never reach the vendor"))

    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "chat/completions",
                             "body": {"model": "openai:gpt-5.5", "max_tokens": 24,
                                      "stop": ["\n"]}, "is_stream": False})

    assert captured["error"].startswith("engine error 400: ")
    inner = json.loads(captured["error"][len("engine error 400: "):])["error"]
    assert inner["param"] == "stop"
    assert inner["code"] == "unsupported_parameter"


def test_serve_handle_job_api_null_unsupported_param_still_forwards(monkeypatch, tmp_path):
    """`"stop": null` is accepted by the vendor (verified live) — only a real value is refused."""
    from remote import relay, serve

    state = _api_serve_state(monkeypatch, tmp_path)
    seen = {}
    monkeypatch.setattr(relay, "submit_response", lambda url, tok, txn, *, content, stream: None)
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: pytest.fail(message))

    def vendor(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    _mock_serve_engine(monkeypatch, vendor)
    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "chat/completions",
                             "body": {"model": "openai:gpt-5.5", "max_tokens": 24,
                                      "stop": None}, "is_stream": False})

    assert seen["model"] == "gpt-5.5"  # reached the vendor


def test_adapt_output_token_param_leaves_a_kind_with_no_cap_parameter_alone():
    """A vendor with NO output-cap parameter under any name (`max_output_param=None` — facts.md #1:
    codex 400s `max_tokens`, `max_output_tokens` and `max_completion_tokens` alike) has nothing to
    rename *to*. `None` is not a parameter name, so the body must come back untouched.

    The bug this pins is a shared blind spot between two functions, and the null is the hinge:
    `unsupported_params` deliberately lets a null through (api_catalog: "Null values are NOT refused
    (the vendor accepts them)"), and the old guard here tested key PRESENCE — `"max_tokens" not in
    body` — not truthiness. So `max_tokens: null` slipped past both and became `adapted[None] = None`,
    serialising as a literal `{"null": null}` key on the wire. One function's deliberate exception
    was the other's unguarded path.
    """
    from remote.serve import _adapt_output_token_param

    for value in (None, 16):  # the null is what slipped through; a real value must not re-key either
        body = {"model": "codex:gpt-5.4-mini", "max_tokens": value, "messages": []}

        adapted = _adapt_output_token_param(body, "codex", "chat/completions")

        assert adapted == body
        assert None not in adapted  # `adapted[None] = ...` — not a JSON key
        assert '"null"' not in json.dumps(adapted)


def test_adapt_output_token_param_leaves_the_responses_dialect_cap_alone():
    """Defense-in-depth (issue 04): the adapter is a CHAT-dialect translator. On `chat/completions`
    the grid-internal `max_tokens` is renamed to the vendor's chat spelling (openai →
    `max_completion_tokens`); on `responses` the dialect's OWN cap `max_output_tokens` is already the
    name the vendor wants — the relay passes it through byte-for-byte — so nothing is renamed.

    The relay refuses `max_tokens` on `responses` (its wrong-dialect spelling), so a responses body
    can never really carry it — but the guard must not DEPEND on that cross-repo invariant: a stray
    `max_tokens` on a responses job is left alone, NOT mis-renamed to the chat spelling the vendor's
    responses endpoint would reject."""
    from remote.serve import _adapt_output_token_param

    # the real path: the responses-dialect cap reaches openai untouched
    body = {"model": "openai:gpt-5.5", "input": [], "max_output_tokens": 256}
    assert _adapt_output_token_param(body, "openai", "responses") == body

    # defense-in-depth: a stray chat-spelling on a responses job is left alone, not rewritten
    stray = {"model": "openai:gpt-5.5", "input": [], "max_tokens": 256}
    adapted = _adapt_output_token_param(stray, "openai", "responses")
    assert adapted == stray
    assert "max_completion_tokens" not in adapted


def test_serve_handle_job_hardware_keeps_stop(monkeypatch, tmp_path):
    """The refusal is per API kind: hardware engines support `stop` and must keep receiving it."""
    from remote import relay, serve

    state = _serve_state(monkeypatch, tmp_path)
    seen = {}
    monkeypatch.setattr(relay, "submit_response", lambda url, tok, txn, *, content, stream: None)
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: pytest.fail(message))

    def engine(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    _mock_serve_engine(monkeypatch, engine)
    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "chat/completions",
                             "body": {"model": "m", "max_tokens": 24, "stop": ["\n"]},
                             "is_stream": False})

    assert seen["stop"] == ["\n"]


def test_serve_handle_job_hardware_keeps_max_tokens(monkeypatch, tmp_path):
    """The translation is per API kind, not a blanket rename: a hardware engine (llama.cpp/ollama)
    only understands `max_tokens` and must keep receiving it."""
    from remote import relay, serve

    state = _serve_state(monkeypatch, tmp_path)
    seen = {}
    monkeypatch.setattr(relay, "submit_response", lambda url, tok, txn, *, content, stream: None)
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: pytest.fail(message))

    def engine(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    _mock_serve_engine(monkeypatch, engine)
    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "chat/completions",
                             "body": {"model": "m", "max_tokens": 24}, "is_stream": False})

    assert seen["max_tokens"] == 24
    assert "max_completion_tokens" not in seen


def test_serve_handle_job_hardware_completions_still_forwards(monkeypatch, tmp_path):
    """The legacy `completions` endpoint stays served by hardware engines — the gate is per API
    kind, not a blanket endpoint removal."""
    from remote import relay, serve

    state = _serve_state(monkeypatch, tmp_path)
    captured = {}
    monkeypatch.setattr(relay, "submit_response",
                        lambda url, tok, txn, *, content, stream: captured.update(content=content))
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: pytest.fail(f"unexpected error: {message}"))
    _mock_serve_engine(monkeypatch, lambda request: httpx.Response(200, json={"choices": []}))

    serve.handle_job(state, {"transaction_id": "t3", "endpoint_path": "completions", "body": {"model": "m"}})

    assert captured["content"]  # forwarded and answered


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


_CODEX_BASE = "https://chatgpt.com/backend-api/codex"


def _codex_serve_state(monkeypatch, tmp_path, **overrides):
    """A `_ServeState` serving one codex seat: advertised `codex:gpt-5.4-mini` routed to the
    vendor base URL, vendor-name rewrite, the target marked `codex` for the endpoint matrix —
    and NO bearer in the snapshot (ADR 0015 D-d: the seat lives in the holder, not the routing)."""
    kwargs = dict(
        models=["codex:gpt-5.4-mini"],
        routes={"codex:gpt-5.4-mini": _CODEX_BASE},
        upstream={"codex:gpt-5.4-mini": "gpt-5.4-mini"},
        bearer_by_url={},
        api_kind_by_url={_CODEX_BASE: "codex"},
    )
    kwargs.update(overrides)
    return _serve_state(monkeypatch, tmp_path, **kwargs)


def test_serve_handle_job_codex_refuses_a_chat_job_with_a_structured_error(monkeypatch, tmp_path):
    """ADR 0015 D-b: a codex seat serves `responses` ONLY. A chat/completions job routed to it —
    the relay should never send one, but the matrix is the provider's own wall — is refused with
    a structured error naming the matrix, never forwarded to the vendor and never translated."""
    from remote import relay, serve

    state = _codex_serve_state(monkeypatch, tmp_path)
    captured = {}
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))
    _mock_serve_engine(monkeypatch, lambda request: pytest.fail("a mismatched job must never reach the vendor"))

    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "chat/completions",
                             "body": {"model": "codex:gpt-5.4-mini"}, "is_stream": True})

    assert "serves responses only" in captured["error"]
    assert "'chat/completions'" in captured["error"]


def test_serve_handle_job_codex_refuses_chat_via_the_single_url_fallback_too(monkeypatch, tmp_path):
    """The fallback route (unknown model on a single-engine union) lands on the codex engine like
    any other — ADR 0015 D-b's rejected alternative is exactly a job that slips past a
    model-keyed gate this way. The matrix is kind-keyed, so the fallback changes nothing."""
    from remote import relay, serve

    state = _codex_serve_state(monkeypatch, tmp_path)
    captured = {}
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))
    _mock_serve_engine(monkeypatch, lambda request: pytest.fail("the fallback must not blind-forward"))

    serve.handle_job(state, {"transaction_id": "t2", "endpoint_path": "chat/completions",
                             "body": {"model": "some-unknown-model"}, "is_stream": False})

    assert "serves responses only" in captured["error"]


def test_serve_handle_job_responses_never_reaches_a_hardware_engine(monkeypatch, tmp_path):
    """The other half of D-b: the global allow-list is NOT widened, so a responses job that routes
    to a hardware engine — direct or via the fallback — is refused before any URL interpolation,
    with the pre-matrix wording."""
    from remote import relay, serve

    state = _serve_state(monkeypatch, tmp_path)  # one hardware engine serving "m"
    captured = {}
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))
    _mock_serve_engine(monkeypatch, lambda request: pytest.fail("responses must never reach a hardware engine"))

    serve.handle_job(state, {"transaction_id": "t3", "endpoint_path": "responses",
                             "body": {"model": "m"}, "is_stream": True})

    assert "unsupported endpoint" in captured["error"].lower()
    assert "'responses'" in captured["error"]


def test_serve_handle_job_openai_responses_streams_through(monkeypatch, tmp_path):
    """The tracer bullet (issue 03): an app streams a Responses request naming an ``openai:*`` model
    and gets the vendor's reply back through the grid. ``openai`` now serves the dialect (its catalog
    row lists ``responses``), so the per-kind gate admits the job and it forwards through the SHARED
    block-aligned responses path — the same one the seat uses — with the vendor key attached and the
    advertised name rewritten to the vendor name.

    This is the inverse of the deleted ``responses_never_reaches_an_openai_engine``: that openai is
    chat-only is exactly the invariant this slice overturns. The 7-byte vendor chunking proves the
    grouper realigns whole ``event:``+``data:`` blocks (a chat raw-passthrough forward would leak the
    socket's 7-byte chunks instead), so the usage-bearing terminal event is never torn."""
    from remote import relay, serve

    state = _api_serve_state(monkeypatch, tmp_path)
    captured = {}

    def cap_submit(url, tok, txn, *, content, stream):
        captured.update(stream=stream, chunks=list(content))  # keep the block boundaries

    monkeypatch.setattr(relay, "submit_response", cap_submit)
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))
    sse = (b'event: response.created\ndata: {}\n\n'
           b'event: response.completed\ndata: {"usage":{"total_tokens":3}}\n\n')

    def vendor(request):
        assert str(request.url) == "https://api.openai.com/v1/responses"  # {base}/{endpoint}
        assert request.headers["authorization"] == "Bearer sk-test-123"   # the stored vendor key
        assert json.loads(request.content)["model"] == "gpt-5.5"          # advertised → vendor rewrite
        return httpx.Response(200, content=iter([sse[i:i + 7] for i in range(0, len(sse), 7)]))

    _mock_serve_engine(monkeypatch, vendor)

    serve.handle_job(state, {"transaction_id": "t4", "endpoint_path": "responses",
                             "body": {"model": "openai:gpt-5.5"}, "is_stream": True})

    assert "error" not in captured
    assert captured["stream"] is True
    assert captured["chunks"] == [
        b'event: response.created\ndata: {}\n\n',
        b'event: response.completed\ndata: {"usage":{"total_tokens":3}}\n\n',
    ]  # two whole blocks, realigned from the 7-byte socket chunking
    assert b"".join(captured["chunks"]) == sse


def test_serve_handle_job_openai_responses_non_stream_forwards_whole(monkeypatch, tmp_path):
    """Issue 05 (AC1): a NON-stream Responses job to an ``openai:*`` engine takes the whole-body
    forward (``_forward_whole``), not the block-aligned stream. The vendor is called once,
    non-streaming, and its whole JSON response object is submitted with ``stream=False`` — the same
    non-stream arm chat has always had, reused because it is dialect-agnostic. Streaming (is_stream=True)
    still goes through ``_forward_responses_stream`` (test_serve_handle_job_openai_responses_streams_through)."""
    from remote import relay, serve

    state = _api_serve_state(monkeypatch, tmp_path)
    captured = {}
    monkeypatch.setattr(relay, "submit_response",
                        lambda url, tok, txn, *, content, stream: captured.update(content=content, stream=stream))
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))

    vendor_obj = (b'{"id":"resp_x","object":"response","status":"completed","model":"gpt-5.5",'
                  b'"output":[],"usage":{"input_tokens":3,"output_tokens":5}}')

    def vendor(request):
        assert str(request.url) == "https://api.openai.com/v1/responses"   # {base}/{endpoint}
        assert request.headers["authorization"] == "Bearer sk-test-123"    # the stored vendor key
        sent = json.loads(request.content)
        assert sent["model"] == "gpt-5.5"                                  # advertised → vendor rewrite
        assert sent.get("stream") in (None, False)                        # NOT forced to stream
        return httpx.Response(200, content=vendor_obj)

    _mock_serve_engine(monkeypatch, vendor)

    serve.handle_job(state, {"transaction_id": "t5", "endpoint_path": "responses",
                             "body": {"model": "openai:gpt-5.5"}, "is_stream": False})

    assert "error" not in captured
    assert captured["stream"] is False        # the whole-body arm, not the block-aligned stream
    assert captured["content"] == vendor_obj  # submitted verbatim — no block realignment on this path


def test_serve_handle_job_openai_responses_honours_output_cap(monkeypatch, tmp_path):
    """AC1 (issue 04): an app sets an output ceiling on a Responses request to an ``openai`` engine
    and the value REACHES the vendor. The relay's responses contract is a passthrough that lifted its
    blanket cap refusal, so the dialect's own ``max_output_tokens`` arrives here byte-for-byte; the
    per-kind gate admits it (not in openai's ``unsupported_params``) and the chat-only cap adapter
    leaves it untouched — so the vendor sees the ceiling the app asked for, under the dialect's own
    name, never mis-translated to the chat spelling."""
    from remote import relay, serve

    state = _api_serve_state(monkeypatch, tmp_path)
    captured = {}
    monkeypatch.setattr(relay, "submit_response", lambda url, tok, txn, *, content, stream: list(content))
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))

    def vendor(request):
        captured.update(sent=json.loads(request.content))
        return httpx.Response(200, content=b'event: response.completed\ndata: {"usage":{"total_tokens":3}}\n\n')

    _mock_serve_engine(monkeypatch, vendor)

    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "responses",
                             "body": {"model": "openai:gpt-5.5",
                                      "input": [{"role": "user", "content": "hi"}],
                                      "stream": True, "max_output_tokens": 256}, "is_stream": True})

    assert "error" not in captured
    assert captured["sent"]["max_output_tokens"] == 256      # the ceiling reached the vendor, dialect-named
    assert "max_completion_tokens" not in captured["sent"]   # not mis-translated to the chat spelling
    assert captured["sent"]["model"] == "gpt-5.5"            # advertised → vendor rewrite still applies


def test_serve_handle_job_openai_responses_refuses_stop_before_the_vendor(monkeypatch, tmp_path):
    """A consequence of the per-kind gate now running on the responses dialect (issue 04): openai's
    `unsupported_params` (`stop`, verified against the CHAT API on 2026-07-14) is refused up-front on
    `/responses` too, before any vendor call. This is a fail-closed EXTRAPOLATION — the responses
    endpoint does not document `stop`, so the vendor would reject it, and refusing here saves the
    round-trip (the gate's whole purpose). Pinned so the behavior is deliberate, not accidental: if
    openai is ever found to accept `stop` on `/responses`, that is a catalog-data fix, not a code
    change."""
    from remote import relay, serve

    state = _api_serve_state(monkeypatch, tmp_path)
    captured = {}
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))
    monkeypatch.setattr(relay, "submit_response", lambda *a, **k: pytest.fail("a refused job has no response"))
    _mock_serve_engine(monkeypatch, lambda request: pytest.fail("a refused job must never reach the vendor"))

    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "responses",
                             "body": {"model": "openai:gpt-5.5",
                                      "input": [{"role": "user", "content": "hi"}],
                                      "stream": True, "stop": ["\n"]}, "is_stream": True})

    assert captured["error"].startswith("engine error 400: ")
    inner = json.loads(captured["error"][len("engine error 400: "):])["error"]
    assert inner["param"] == "stop"                          # names the param the vendor rejects


def test_serve_handle_job_openai_responses_non200_submits_terminal_error(monkeypatch, tmp_path):
    """The caller obligation issue 02 handed to issue 03: ``_forward_responses_stream`` RETURNS a
    non-200 (``_UpstreamFailure``) rather than reporting it, so this second caller — unlike the seat's
    D-d refresh — must answer it with a terminal signal and does NOT retry (ADR 0012 job-error-only).
    NOTHING in the toolchain catches a dropped return (issue 02's as-built: not even ``ruff --select
    ALL``), so this test is what pins it: a vendor non-200 reaches the consumer as a ``submit_error``,
    nothing is streamed, and the vendor is hit exactly once. Modelled on
    ``test_forward_responses_stream_returns_the_drained_failure_without_submitting``, one layer up."""
    from remote import relay, serve

    state = _api_serve_state(monkeypatch, tmp_path)
    submissions = []
    monkeypatch.setattr(relay, "submit_response", lambda *a, **k: submissions.append("response"))
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: submissions.append(("error", message)))
    calls = []

    def vendor(request):
        calls.append(str(request.url))
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    _mock_serve_engine(monkeypatch, vendor)

    serve.handle_job(state, {"transaction_id": "t5", "endpoint_path": "responses",
                             "body": {"model": "openai:gpt-5.5"}, "is_stream": True})

    assert calls == ["https://api.openai.com/v1/responses"]   # hit once — no retry (unlike the seat)
    assert "response" not in submissions                      # nothing was streamed back
    errors = [s for s in submissions if isinstance(s, tuple)]
    assert len(errors) == 1 and "429" in errors[0][1]         # the terminal signal carries the status


def test_static_api_caps_openai_advertises_both_endpoints():
    """Change #2 in isolation: the advertised caps envelope sources its ``endpoints`` from the kind's
    catalog row rather than a hardcoded chat-only list. This list is exactly what grid-src's per-model
    ``provider_supports`` filter reads to decide the openai engine can serve ``responses`` — so if it
    stayed hardcoded, the row change would never reach the relay and nothing would route. The
    startup-path cousin ``test_remote_engine_api_record_registers_static_caps_and_kind`` proves the
    same value through the full registration path; this pins the source at the unit layer."""
    from remote import serve

    entry = serve._static_api_caps("openai", ["openai:gpt-5.5"])["models"]["openai:gpt-5.5"]
    assert entry["endpoints"] == ["chat/completions", "responses"]


def test_serve_codex_seat_holder_primes_from_the_store_and_self_heals(monkeypatch, tmp_path):
    """ADR 0015 D-d: the codex credential lives OUTSIDE the routing snapshot, in a per-kind holder
    on the serve state — a rotation must not rebuild routing or race a hot-reload swap. The holder
    exists unconditionally (no None state to branch on): unprimed on an empty store it refuses
    with the typed error the forward path turns into a job error; unprimed on a box WITH a seat it
    lazily self-heals from the store; primed, it answers from memory."""
    from remote import api_keys

    state = _codex_serve_state(monkeypatch, tmp_path)

    with pytest.raises(api_keys.CodexNotSignedIn):
        state.codex_seat.bundle()  # unprimed, empty store

    api_keys.store_codex_bundle(_codex_bundle())
    assert state.codex_seat.bundle() == _codex_bundle()  # the lazy backstop self-heals

    primed = _codex_serve_state(monkeypatch, tmp_path)
    primed.codex_seat.prime_from_store()
    assert primed.codex_seat.bundle() == _codex_bundle()


def test_serve_codex_seat_holder_refresh_rotates_once_and_collapses_racers(monkeypatch, tmp_path):
    """`refresh(stale)` is the ONE entry for reactive (upstream 401) and proactive (heartbeat)
    rotation. Success adopts the rotated bundle in memory — the very next job forwards the new
    bearer with no reload — and persists it; a second caller holding the same stale token
    short-circuits on the in-memory compare, so N workers 401ing together collapse to one
    exchange."""
    from remote import api_keys

    state = _codex_serve_state(monkeypatch, tmp_path)
    api_keys.store_codex_bundle(_codex_bundle())
    state.codex_seat.prime_from_store()
    new_access = _codex_jwt({"chatgpt_account_id": "acct-1"}, exp=2_000_000_000)
    exchanges = []

    def vendor(request):
        exchanges.append(1)
        return httpx.Response(200, json={"access_token": new_access, "refresh_token": "rt-2"})

    _mock_serve_engine(monkeypatch, vendor)

    assert state.codex_seat.refresh("tok-access") is True
    assert state.codex_seat.bundle().access_token == new_access
    assert api_keys.load_codex_bundle().access_token == new_access  # persisted immediately

    assert state.codex_seat.refresh("tok-access") is True  # a racer with the same stale token
    assert len(exchanges) == 1  # collapsed on the in-memory compare — no second exchange


def test_serve_codex_seat_holder_gates_failures_but_adopts_through_the_gate(monkeypatch, tmp_path, capsys):
    """A refused seat earns ONE vendor 4xx per cooldown window — the next refresh inside the gate
    makes no vendor call (a dead seat must not be hammered by every 401ing job plus every 30s
    tick). The gate never blocks the free heal, though: a bundle another PROCESS wrote (a
    re-sign-in from a second grid on this box) is adopted lock-free straight through it. And once
    stop is set, refresh never starts spending at all."""
    from remote import api_keys

    state = _codex_serve_state(monkeypatch, tmp_path)
    api_keys.store_codex_bundle(_codex_bundle())
    state.codex_seat.prime_from_store()
    exchanges = []

    def vendor(request):
        exchanges.append(1)
        return httpx.Response(400, json={"error": "invalid_grant"})

    _mock_serve_engine(monkeypatch, vendor)

    assert state.codex_seat.refresh("tok-access") is False
    assert len(exchanges) == 1
    assert "sign in again" in capsys.readouterr().err

    assert state.codex_seat.refresh("tok-access") is False  # inside the gate
    assert len(exchanges) == 1  # no second vendor call

    rotated_elsewhere = _codex_bundle()
    rotated_elsewhere = type(rotated_elsewhere)(
        access_token="tok-access-2", refresh_token="tok-refresh-2", account_id="acct-1",
        plan_type="free", last_refresh=1_700_000_000,
    )
    api_keys.store_codex_bundle(rotated_elsewhere)  # another process re-signed-in

    assert state.codex_seat.refresh("tok-access") is True  # adopted THROUGH the gate, no exchange
    assert state.codex_seat.bundle() == rotated_elsewhere
    assert len(exchanges) == 1

    state.stop.set()
    _mock_serve_engine(monkeypatch, lambda request: pytest.fail("nothing may be spent after stop"))
    assert state.codex_seat.refresh("tok-access-2") is False


def test_serve_api_bearers_skip_the_codex_seat(monkeypatch, tmp_path):
    """ADR 0015 D-d: the codex bearer never enters the routing snapshot — a snapshot copy would go
    stale at the first rotation (the holder is the live source, resolved at forward time). openai
    keys stay snapshot-resident exactly as before."""
    from remote import api_keys, serve

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    api_keys.store_key("openai", "sk-test-openai")
    api_keys.store_codex_bundle(_codex_bundle())

    bearers = serve._api_bearers({"engines": [
        {"endpoint_url": "https://api.openai.com/v1", "models": ["openai:gpt-5.5"], "api_kind": "openai"},
        {"endpoint_url": _CODEX_BASE, "models": ["codex:gpt-5.4-mini"], "api_kind": "codex"},
    ]})

    assert bearers == {"https://api.openai.com/v1": "sk-test-openai"}  # codex absent by design


def test_serve_prime_codex_seat_gates_startup_and_ignores_non_codex_records(monkeypatch, tmp_path):
    """The die-before-advertise gate, moved with the credential (D-d): a record serving a codex
    engine primes the holder — or dies naming the fix when the box is not signed in — while a
    record with no codex spec never touches the store at all (an unprimed holder is inert; a
    hardware-only engine must not go anywhere near another grid's seat). ONE derivation used by
    startup and reload, so the two can't drift."""
    from remote import api_keys, serve

    state = _codex_serve_state(monkeypatch, tmp_path)

    serve._prime_codex_seat(state, {"engines": [{"endpoint_url": "http://e1/v1", "models": ["m"]}]})
    with pytest.raises(api_keys.CodexNotSignedIn):
        state.codex_seat.bundle()  # no codex spec → untouched, still unprimed

    codex_record = {"engines": [
        {"endpoint_url": _CODEX_BASE, "models": ["codex:gpt-5.4-mini"], "api_kind": "codex"},
    ]}
    with pytest.raises(SystemExit) as exc:
        serve._prime_codex_seat(state, codex_record)  # empty store → terminal, names the fix
    assert "grid join --api codex" in str(exc.value)

    api_keys.store_codex_bundle(_codex_bundle())
    serve._prime_codex_seat(state, codex_record)
    assert state.codex_seat.bundle() == _codex_bundle()


def test_serve_reload_refuses_a_codex_append_with_no_stored_seat(monkeypatch, tmp_path):
    """Hot-appending a codex engine re-reads the seat BEFORE the routing swap: no stored seat →
    the reload raises (absorbed by `_reload_loop`'s catch into a warn + `last_reload_error` — that
    path is pinned elsewhere), the old routing keeps serving, and nothing re-registers. The reload
    analogue of the startup die-before-advertise: a union must never advertise codex models whose
    every job would error."""
    from remote import relay, serve

    state = _seed_reload_state(monkeypatch, tmp_path, retained=[
        ("http://e1/v1", ["a"], ["a"], {"schema_version": 1, "models": {"a": {}}}),
    ], engines=[
        {"endpoint_url": "http://e1/v1", "models": ["a"], "engine_label": None},
        {"endpoint_url": _CODEX_BASE, "models": ["codex:gpt-5.4-mini"], "api_kind": "codex"},
    ])
    monkeypatch.setattr(relay, "register_node",
                        lambda *a, **k: pytest.fail("a refused reload must not re-register"))

    with pytest.raises(SystemExit) as exc:
        serve._reload_once(state, "remote")

    assert "grid join --api codex" in str(exc.value)
    assert state.models == ["a"]  # the swap never happened — old routing intact


def test_serve_reload_appends_and_drops_codex_with_the_bundle_intact(monkeypatch, tmp_path):
    """AC 7: hot-append advertises the union with the codex engine (whitelist caps, no probe, no
    vendor call) and primes the seat; `grid leave --engine codex` re-advertises the survivors and
    leaves the STORED bundle untouched — re-joining later is one command, no re-auth."""
    from remote import api_keys, relay, serve

    state = _seed_reload_state(monkeypatch, tmp_path, retained=[
        ("http://e1/v1", ["a"], ["a"], {"schema_version": 1, "models": {"a": {}}}),
    ], engines=[
        {"endpoint_url": "http://e1/v1", "models": ["a"], "engine_label": None},
        {"endpoint_url": _CODEX_BASE, "models": ["codex:gpt-5.4-mini"], "api_kind": "codex"},
    ])
    api_keys.store_codex_bundle(_codex_bundle())
    seen = {}
    monkeypatch.setattr(relay, "register_node", lambda url, tok, node, **kw: seen.update(kw))

    serve._reload_once(state, "remote")  # the append

    assert seen["models"] == ["a", "codex:gpt-5.4-mini"]
    assert seen["capabilities"]["models"]["codex:gpt-5.4-mini"]["endpoints"] == ["responses"]
    assert state.route_and_kind("codex:gpt-5.4-mini")[1] == "codex"
    assert state.codex_seat.bundle() == _codex_bundle()  # primed by the reload

    from shared import run_records
    run_records.write_record("n1", "remote", {
        "engine_id": "remote", "grid_id": "n1", "media": False, "media_bundles": [],
        "engines": [{"endpoint_url": "http://e1/v1", "models": ["a"], "engine_label": None}],
    })

    serve._reload_once(state, "remote")  # the leave --engine codex

    assert seen["models"] == ["a"]  # survivors re-advertised
    assert api_keys.load_codex_bundle() == _codex_bundle()  # the credential survives the leave


def _codex_fixture_bytes():
    """The shared wire fixture (grid-src issue 03, copied verbatim — see tests/fixtures/README.md):
    47 LF-framed event blocks, each terminated by a blank line, no [DONE]."""
    return (Path(__file__).parent / "fixtures" / "codex_stream.sse").read_bytes()


def test_iter_event_blocks_regroups_any_chunking_into_whole_blocks(monkeypatch):
    """ADR 0015 D-e: the streaming unit for a responses job is the whole SSE event block. However
    the vendor's bytes arrive chunked, what leaves is one block per chunk — terminator included,
    byte-concatenation identical (no strip, no decode, no [DONE], CR untouched: the relay is the
    enforcement point for CR smuggling and refuses it; a provider that 'repaired' CR would mask
    exactly what the relay's sanitiser refuses)."""
    from remote import serve

    fixture = _codex_fixture_bytes()
    for size in (1, 7, 1024, len(fixture)):
        chunks = [fixture[i:i + size] for i in range(0, len(fixture), size)]
        blocks = list(serve._iter_event_blocks(iter(chunks)))
        assert len(blocks) == 47
        assert all(block.endswith(b"\n\n") for block in blocks)
        assert b"".join(blocks) == fixture  # byte-for-byte, whatever the input chunking

    # A `\n\n` straddling a chunk boundary is still one block.
    assert list(serve._iter_event_blocks(iter([b"event: a\ndata: {}\n", b"\nevent: b\ndata: {}\n\n"]))) \
        == [b"event: a\ndata: {}\n\n", b"event: b\ndata: {}\n\n"]

    # A tail with no trailing blank line is flushed verbatim — swallowing it would eat exactly the
    # `response.completed` that carries usage (the relay flushes its own final block the same way).
    assert list(serve._iter_event_blocks(iter([b"event: a\ndata: {}"]))) == [b"event: a\ndata: {}"]

    # CRLF input passes through byte-identical: the relay refuses bare-CR; we never re-frame.
    crlf = b"event: a\r\ndata: {}\r\n\r\n"
    assert b"".join(serve._iter_event_blocks(iter([crlf]))) == crlf

    # An unbounded block cannot buffer unboundedly: past the cap it degrades to passthrough —
    # alignment lost for one seam, bytes never lost (the relay re-splits on \n itself).
    monkeypatch.setattr(serve, "_MAX_EVENT_BLOCK", 8)
    giant = b"data: 0123456789ABCDEF"  # no blank line anywhere
    assert b"".join(serve._iter_event_blocks(iter([giant[:12], giant[12:]]))) == giant


def test_iter_event_blocks_never_flushes_a_partial_block_on_error():
    """The crash-atomicity half of D-e: a vendor stream that dies mid-block must leave only WHOLE
    events at the relay. The buffered partial block is dropped and the error propagates — a future
    'flush buf in a finally' refactor would silently reverse this design (review F6)."""
    from remote import serve

    def dies_mid_block():
        yield b"event: a\ndata: {}\n\nevent: b\ndata: {\"half\":"
        raise httpx.ReadError("vendor died")

    out = []
    with pytest.raises(httpx.ReadError):
        for block in serve._iter_event_blocks(dies_mid_block()):
            out.append(block)

    assert out == [b"event: a\ndata: {}\n\n"]  # exactly the complete block; the torn half never left


# A plain bearer header set — what a non-seat kind's `_forward_headers` produces. Deliberately NOT
# the seat's five (`_codex_headers`): the shared responses forward must not know a seat exists.
_NON_SEAT_HEADERS = {"Content-Type": "application/json", "Authorization": "Bearer k-plain"}


def test_forward_responses_stream_submits_whole_event_blocks_for_any_kind(monkeypatch, tmp_path):
    """PRD §5 / ADR 0018: block alignment is a property of the DIALECT, not of the subscription
    seat — so the streaming submission is a path any engine kind can take.

    Exercised here with no seat anywhere in the call: a plain `_serve_state` (whose codex holder is
    created but never primed) and a plain bearer header set. It still submits one whole
    `event:`+`data:` block per chunk. `test_serve_handle_job_codex_submits_the_fixture_as_whole_
    event_blocks` asserts the same invariants THROUGH the seat; that overlap is deliberate, and
    what is unique here is the seat's ABSENCE — if this path ever reached for `state.codex_seat`,
    the unprimed holder would raise `CodexNotSignedIn` and this test would fail loudly rather than
    let the two concerns quietly re-couple."""
    from remote import relay, serve

    state = _serve_state(monkeypatch, tmp_path)
    fixture = _codex_fixture_bytes()
    captured = {}

    def cap_submit(url, tok, txn, *, content, stream):
        captured.update(stream=stream, chunks=list(content))  # keep the chunk boundaries

    monkeypatch.setattr(relay, "submit_response", cap_submit)

    def engine(request):
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        # Awkward 7-byte chunking: block boundaries land mid-line, mid-JSON, everywhere.
        return httpx.Response(200, content=iter([fixture[i:i + 7] for i in range(0, len(fixture), 7)]))

    _mock_serve_engine(monkeypatch, engine)

    failure = serve._forward_responses_stream(
        state, "t1", "responses", {"model": "m"}, 600.0, "http://engine.example/v1",
        headers=dict(_NON_SEAT_HEADERS),
    )

    assert failure is None  # a submitted 200 reports nothing back to the caller
    assert captured["url"] == "http://engine.example/v1/responses"  # {target}/{endpoint}
    assert captured["headers"]["authorization"] == "Bearer k-plain"  # the CALLER's headers, verbatim
    assert "chatgpt-account-id" not in captured["headers"]  # nothing seat-shaped was added
    assert captured["stream"] is True
    assert len(captured["chunks"]) == 47
    assert all(chunk.endswith(b"\n\n") for chunk in captured["chunks"])
    assert b"".join(captured["chunks"]) == fixture  # byte-for-byte, whatever the socket chunking
    assert b"[DONE]" not in b"".join(captured["chunks"])


def test_forward_responses_stream_returns_the_drained_failure_without_submitting(monkeypatch, tmp_path):
    """A non-200 is reported back to the CALLER rather than answered here, because the right answer
    differs by kind: the seat refreshes a rotated bearer and retries once (ADR 0015 D-d), an API
    engine does not (ADR 0012 keeps it job-error-only). Two properties make that split safe.

    The body is drained INSIDE the response context and bound before both contexts close — so a
    caller that then runs a token exchange holds no vendor connection open through it, and the
    `Cf-Mitigated` header that the CF-403-vs-auth-403 taxonomy keys on (`_warn_codex_upstream`,
    D-f) is still readable afterwards, not just the status int. And nothing is submitted: the job
    has had NO terminal signal yet, which is precisely why a non-None return OBLIGES its caller to
    send one — dropping it would hang the consumer."""
    from remote import relay, serve

    state = _serve_state(monkeypatch, tmp_path)
    submissions = []
    monkeypatch.setattr(relay, "submit_response",
                        lambda *a, **k: submissions.append("response"))
    monkeypatch.setattr(relay, "submit_error",
                        lambda *a, **k: submissions.append("error"))
    _mock_serve_engine(monkeypatch, lambda request: httpx.Response(
        403, headers={"Cf-Mitigated": "challenge"}, json={"detail": "blocked"}))

    failure = serve._forward_responses_stream(
        state, "t1", "responses", {"model": "m"}, 600.0, "http://engine.example/v1",
        headers=dict(_NON_SEAT_HEADERS),
    )

    assert submissions == []  # the caller owns the terminal signal — nothing was sent for it
    assert failure is not None
    assert failure.status == 403
    assert failure.headers.get("cf-mitigated") == "challenge"  # readable after the contexts closed
    assert "blocked" in failure.text


def test_forward_responses_stream_flushes_a_final_block_with_no_trailing_blank_line(monkeypatch, tmp_path):
    """An engine that ends its stream without a trailing blank line must still have its last block
    submitted — that block is the `response.completed` carrying the usage, so swallowing it would
    under-bill whoever served the request while looking exactly like a clean stream.

    `_iter_event_blocks` already guarantees this and is tested directly above; what this pins is the
    WIRING — that the grouper is genuinely in the shared submission path, so a later change that
    forwarded raw bytes here (as the chat path legitimately does) could not silently drop the tail."""
    from remote import relay, serve

    state = _serve_state(monkeypatch, tmp_path)
    tail = b'event: response.completed\ndata: {"usage":{"total_tokens":7}}'  # no trailing blank line
    stream = b"event: response.created\ndata: {}\n\n" + tail
    captured = {}
    monkeypatch.setattr(relay, "submit_response",
                        lambda url, tok, txn, *, content, stream: captured.update(chunks=list(content)))
    _mock_serve_engine(monkeypatch, lambda request: httpx.Response(200, content=iter([stream])))

    failure = serve._forward_responses_stream(
        state, "t1", "responses", {"model": "m"}, 600.0, "http://engine.example/v1",
        headers=dict(_NON_SEAT_HEADERS),
    )

    assert failure is None
    assert captured["chunks"][-1] == tail  # flushed verbatim, terminator or not
    assert b"".join(captured["chunks"]) == stream


def test_serve_handle_job_codex_forwards_verbatim_with_the_seat_headers(monkeypatch, tmp_path):
    """AC 1 (issue 03): URL = the kind's base URL + the job's endpoint path; headers are the real
    client's set, built fresh from the live bundle (bearer + account id + originator/user-agent + SSE
    accept + json content-type — and NO OpenAI-Beta); the body goes verbatim except the
    advertised→vendor model rewrite (the one existing alias mechanism). A null `max_tokens` passes
    through untouched: the per-kind gate refuses only a REAL value (issue 04), so a null is neither
    refused nor renamed. (A real cap spelling or `temperature` IS now refused up-front on this path —
    see ``test_serve_handle_job_codex_responses_refuses_unsupported_param_before_the_seat``.)"""
    from remote import api_keys, relay, serve

    state = _codex_serve_state(monkeypatch, tmp_path)
    api_keys.store_codex_bundle(_codex_bundle())
    state.codex_seat.prime_from_store()
    captured = {}

    def cap_submit(url, tok, txn, *, content, stream):
        captured.update(stream=stream, body=b"".join(content))  # materialise while the stream is open

    monkeypatch.setattr(relay, "submit_response", cap_submit)
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))
    job_body = {"model": "codex:gpt-5.4-mini", "input": [{"role": "user", "content": "hi"}],
                "stream": True, "max_tokens": None}

    def vendor(request):
        assert str(request.url) == f"{_CODEX_BASE}/responses"
        assert request.headers["authorization"] == "Bearer tok-access"
        assert request.headers["chatgpt-account-id"] == "acct-1"
        assert request.headers["originator"] == "codex_cli_rs"
        assert request.headers["user-agent"] == "codex_cli_rs"
        assert request.headers["accept"] == "text/event-stream"
        assert request.headers["content-type"] == "application/json"
        assert "openai-beta" not in request.headers
        assert json.loads(request.content) == {**job_body, "model": "gpt-5.4-mini"}
        return httpx.Response(200, content=b"event: response.created\ndata: {}\n\n")

    _mock_serve_engine(monkeypatch, vendor)

    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "responses",
                             "body": job_body, "is_stream": True})

    assert "error" not in captured
    assert captured["stream"] is True
    assert captured["body"] == b"event: response.created\ndata: {}\n\n"


@pytest.mark.parametrize("param,value", [
    ("max_output_tokens", 256),   # the responses-dialect cap the relay now lets through (issue 04)
    ("temperature", 0.5),         # a chat-era knob the seat's allowlist backend denies (facts.md #7)
])
def test_serve_handle_job_codex_responses_refuses_unsupported_param_before_the_seat(
        monkeypatch, tmp_path, param, value):
    """AC2/AC7 (issue 04): the codex row's ``unsupported_params`` is now an EXECUTABLE per-kind gate
    on the responses dialect, not advisory catalog data. The relay lifted its blanket
    ``max_output_tokens`` refusal precisely so the kind-aware engine — the only layer that knows the
    seat cannot set a cap under any name — answers it; and the seat genuinely 400s both of these
    (facts.md #7). So a responses job carrying one is refused BEFORE any seat call, riding the same
    ``engine error 400: {…}`` string the chat gate uses. The relay re-renders that into the responses
    ``response.failed`` envelope, so the app's error handling does not fork on which layer caught it."""
    from remote import api_keys, relay, serve

    state = _codex_serve_state(monkeypatch, tmp_path)
    api_keys.store_codex_bundle(_codex_bundle())
    state.codex_seat.prime_from_store()
    captured = {}
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))
    monkeypatch.setattr(relay, "submit_response", lambda *a, **k: pytest.fail("a refused job has no response"))
    _mock_serve_engine(monkeypatch, lambda request: pytest.fail("a refused job must never reach the seat"))

    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "responses",
                             "body": {"model": "codex:gpt-5.4-mini",
                                      "input": [{"role": "user", "content": "hi"}],
                                      "stream": True, param: value}, "is_stream": True})

    assert captured["error"].startswith("engine error 400: ")
    inner = json.loads(captured["error"][len("engine error 400: "):])["error"]
    assert inner["param"] == param                    # names the parameter (AC6)
    assert inner["code"] == "unsupported_parameter"


def test_serve_handle_job_codex_refuses_a_non_stream_responses_job(monkeypatch, tmp_path):
    """AC7 (issue 05): the subscription seat is SSE-only, so a NON-stream responses job is refused by
    its per-kind ENGINE gate. The relay lifted its global stream-mandatory rule for every other engine,
    so the kind-aware layer — the only one that knows this backend cannot stream off — is where the
    seat's refusal now lives. Refused BEFORE any seat call, riding the same ``engine error 400: {…}``
    string the other per-kind gates use; the relay re-renders it into the responses ``{"detail": …}``
    envelope, byte-identical to the old pre-queue ``Stream must be set to true`` (user story 16)."""
    from remote import api_keys, relay, serve

    state = _codex_serve_state(monkeypatch, tmp_path)
    api_keys.store_codex_bundle(_codex_bundle())
    state.codex_seat.prime_from_store()
    captured = {}
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))
    monkeypatch.setattr(relay, "submit_response", lambda *a, **k: pytest.fail("a refused job has no response"))
    _mock_serve_engine(monkeypatch, lambda request: pytest.fail("a refused job must never reach the seat"))

    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "responses",
                             "body": {"model": "codex:gpt-5.4-mini",
                                      "input": [{"role": "user", "content": "hi"}], "stream": False},
                             "is_stream": False})

    assert captured["error"].startswith("engine error 400: ")
    inner = json.loads(captured["error"][len("engine error 400: "):])["error"]
    assert inner["param"] == "stream"
    assert inner["code"] == "unsupported_parameter"
    assert "Stream must be set to true" in inner["message"]


def test_serve_handle_job_codex_streams_regardless_of_the_job_flag(monkeypatch, tmp_path):
    """D-e: the upstream only speaks SSE, so a codex job takes the streaming forward whatever a
    STREAMING job's transport says. A NON-stream codex responses job is a different case — the seat
    refuses it at its per-kind gate (test_serve_handle_job_codex_refuses_a_non_stream_responses_job) —
    so this pins that once a job IS streaming, the forward stays streaming regardless of drift."""
    from remote import api_keys, relay, serve

    state = _codex_serve_state(monkeypatch, tmp_path)
    api_keys.store_codex_bundle(_codex_bundle())
    state.codex_seat.prime_from_store()
    captured = {}

    def cap_submit(url, tok, txn, *, content, stream):
        captured.update(stream=stream, body=b"".join(content))

    monkeypatch.setattr(relay, "submit_response", cap_submit)
    _mock_serve_engine(monkeypatch, lambda request: httpx.Response(
        200, content=b"event: response.created\ndata: {}\n\n"))

    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "responses",
                             "body": {"model": "codex:gpt-5.4-mini"}, "is_stream": True})

    assert captured["stream"] is True  # forced — the flag cannot demote a responses job


def test_serve_handle_job_codex_submits_the_fixture_as_whole_event_blocks(monkeypatch, tmp_path):
    """AC 2, end to end through handle_job on the shared wire fixture: whatever chunking the
    vendor's socket produces, every chunk submitted to the relay is one whole event block —
    47 blocks, byte-concatenation identical, no [DONE] appended."""
    from remote import api_keys, relay, serve

    state = _codex_serve_state(monkeypatch, tmp_path)
    api_keys.store_codex_bundle(_codex_bundle())
    state.codex_seat.prime_from_store()
    fixture = _codex_fixture_bytes()
    captured = {}

    def cap_submit(url, tok, txn, *, content, stream):
        captured.update(stream=stream, chunks=list(content))  # keep the chunk boundaries

    monkeypatch.setattr(relay, "submit_response", cap_submit)
    # Awkward 7-byte chunking: block boundaries land mid-line, mid-JSON, everywhere.
    _mock_serve_engine(monkeypatch, lambda request: httpx.Response(
        200, content=iter([fixture[i:i + 7] for i in range(0, len(fixture), 7)])))

    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "responses",
                             "body": {"model": "codex:gpt-5.4-mini"}, "is_stream": True})

    assert captured["stream"] is True
    assert len(captured["chunks"]) == 47
    assert all(chunk.endswith(b"\n\n") for chunk in captured["chunks"])
    assert b"".join(captured["chunks"]) == fixture
    assert b"[DONE]" not in b"".join(captured["chunks"])


def test_serve_handle_job_codex_401_refreshes_and_retries_once_with_the_new_bearer(monkeypatch, tmp_path):
    """AC 3, reactive half: an upstream 401 rotates the seat and retries EXACTLY once, and the
    retry carries the ROTATED bearer — resolved from the holder at forward time, no reload, no
    snapshot rebuild (D-d). The rotation is persisted, so the next process adopts it for free."""
    from remote import api_keys, relay, serve

    state = _codex_serve_state(monkeypatch, tmp_path)
    api_keys.store_codex_bundle(_codex_bundle())
    state.codex_seat.prime_from_store()
    new_access = _codex_jwt({"chatgpt_account_id": "acct-1"}, exp=2_000_000_000)
    inference, exchanges, captured = [], [], {}

    def cap_submit(url, tok, txn, *, content, stream):
        captured.update(stream=stream, body=b"".join(content))

    monkeypatch.setattr(relay, "submit_response", cap_submit)

    def vendor(request):
        if request.url.host == "auth.openai.com":  # the token endpoint (one MockTransport, two hosts)
            exchanges.append(1)
            return httpx.Response(200, json={"access_token": new_access, "refresh_token": "rt-2"})
        inference.append(request.headers["authorization"])
        if len(inference) == 1:
            return httpx.Response(401, json={"detail": "token expired"})
        return httpx.Response(200, content=b"event: response.created\ndata: {}\n\n")

    _mock_serve_engine(monkeypatch, vendor)

    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "responses",
                             "body": {"model": "codex:gpt-5.4-mini"}, "is_stream": True})

    assert inference == ["Bearer tok-access", f"Bearer {new_access}"]  # retry wore the rotation
    assert exchanges == [1]
    assert captured["stream"] is True  # the job succeeded on the retry
    assert api_keys.load_codex_bundle().access_token == new_access  # persisted for the next process


def test_serve_handle_job_codex_second_401_is_a_job_error_not_a_second_refresh(monkeypatch, tmp_path, capsys):
    """Retry ONCE means once: a 401 on the rotated bearer is a job error carrying the upstream
    status (byte-compatible `engine error 401:` for the relay's terminal mapper) plus the
    check-your-seat warning — never a second exchange, never a loop."""
    from remote import api_keys, relay, serve

    state = _codex_serve_state(monkeypatch, tmp_path)
    api_keys.store_codex_bundle(_codex_bundle())
    state.codex_seat.prime_from_store()
    new_access = _codex_jwt({"chatgpt_account_id": "acct-1"}, exp=2_000_000_000)
    inference, exchanges, captured = [], [], {}
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))

    def vendor(request):
        if request.url.host == "auth.openai.com":
            exchanges.append(1)
            return httpx.Response(200, json={"access_token": new_access, "refresh_token": "rt-2"})
        inference.append(1)
        return httpx.Response(401, json={"detail": "seat revoked"})

    _mock_serve_engine(monkeypatch, vendor)

    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "responses",
                             "body": {"model": "codex:gpt-5.4-mini"}, "is_stream": True})

    assert len(inference) == 2 and exchanges == [1]  # one retry, one exchange, then stop
    assert captured["error"].startswith("engine error 401:")
    assert "sign in again" in capsys.readouterr().err


def test_serve_handle_job_openai_401_never_touches_the_codex_token_endpoint(monkeypatch, tmp_path):
    """AC 3, the scoping half: 401→refresh→retry-once exists for codex ONLY — openai keeps ADR
    0012's job-error-without-retry. An openai vendor 401 must reach the consumer as a job error
    after exactly one vendor call, with the OAuth token endpoint never contacted (its sibling pin,
    `..._is_job_error_not_token_refresh`, covers the relay-token domain)."""
    from remote import relay, serve

    state = _api_serve_state(monkeypatch, tmp_path)
    calls, captured = [], {}
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))

    def vendor(request):
        if request.url.host == "auth.openai.com":
            pytest.fail("an openai 401 must never reach the codex token endpoint")
        calls.append(1)
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    _mock_serve_engine(monkeypatch, vendor)

    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "chat/completions",
                             "body": {"model": "openai:gpt-5.5"}, "is_stream": False})

    assert len(calls) == 1  # no retry of any kind
    assert captured["error"].startswith("engine error 401:")


def test_serve_codex_upstream_warn_taxonomy_is_four_way(monkeypatch, tmp_path, capsys):
    """AC 7 / ADR 0015 D-f: the two 403s demand OPPOSITE operator actions so their wordings must
    not overlap — CF-challenge (403 + Cf-Mitigated) names the egress IP and says sign-in won't
    help; a bare 403 says check your seat. 429 keeps the existing quota warning; a 5xx warns
    nothing (a vendor outage says nothing about the seat). Every one still submits the
    byte-compatible `engine error {status}:` job error."""
    from remote import api_keys, relay, serve

    state = _codex_serve_state(monkeypatch, tmp_path)
    api_keys.store_codex_bundle(_codex_bundle())
    state.codex_seat.prime_from_store()
    errors = []
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: errors.append(message))

    def run(status, headers=None):
        _mock_serve_engine(monkeypatch, lambda request: httpx.Response(
            status, json={"detail": "x"}, headers=headers or {}))
        serve.handle_job(state, {"transaction_id": "t", "endpoint_path": "responses",
                                 "body": {"model": "codex:gpt-5.4-mini"}, "is_stream": True})
        return capsys.readouterr().err

    cf = run(403, {"Cf-Mitigated": "challenge"})
    assert "egress IP" in cf and "Cloudflare" in cf
    assert "sign in again" not in cf.replace("signing in again will not help", "")  # opposite advice

    auth = run(403)
    assert "check your seat" in auth and "sign in again" in auth
    assert "egress" not in auth

    quota = run(429)
    assert "quota" in quota  # the existing API-engine quota warning, reused

    outage = run(502)
    assert outage == ""  # a vendor outage earns no seat/quota warn — same as every other kind

    assert [e.split(":")[0] for e in errors] == ["engine error 403", "engine error 403",
                                                 "engine error 429", "engine error 502"]


def test_serve_handle_job_codex_without_a_seat_is_a_job_error_naming_the_fix(monkeypatch, tmp_path):
    """The defensive floor under the wiring: routing says codex but the holder is unprimed AND the
    store has no seat (a reload race, or wiring drift). The job errors naming the one real fix —
    it never blind-forwards bearer-less and never kills the loop."""
    from remote import relay, serve

    state = _codex_serve_state(monkeypatch, tmp_path)  # nothing seeded, nothing primed
    captured = {}
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))
    _mock_serve_engine(monkeypatch, lambda request: pytest.fail("no seat, no forward"))

    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "responses",
                             "body": {"model": "codex:gpt-5.4-mini"}, "is_stream": True})

    assert "grid join --api codex" in captured["error"]


def test_serve_codex_store_corruption_is_a_job_error_never_an_engine_stop(monkeypatch, tmp_path, capsys):
    """The store's TOML loader raises SystemExit for a corrupt file — which skips every `except
    Exception` guard, so unguarded it would sail through handle_job to `_supervise` and take the
    WHOLE engine down (every model in the union, plus the waiting consumer gets no terminal error
    at all) over one kind's transient store hiccup. Unlike a corrupt credentials.toml — which is
    fatal by documented design, the engine cannot outlive its relay tokens — the codex store only
    feeds the codex forward, so the blast radius must be one job (silent-failure + python
    reviews). Both touchpoints are covered: a rotation mid-job, and the unprimed holder's
    self-heal read."""
    from remote import api_keys, credentials, relay, serve

    # Touchpoint 1: the reactive refresh hits a store that turns unreadable mid-serve.
    state = _codex_serve_state(monkeypatch, tmp_path)
    api_keys.store_codex_bundle(_codex_bundle())
    state.codex_seat.prime_from_store()
    captured = {}
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: captured.update(error=message))
    _mock_serve_engine(monkeypatch, lambda request: httpx.Response(401, json={}))
    real_load_toml = credentials.load_toml
    monkeypatch.setattr(credentials, "load_toml",
                        lambda path: (_ for _ in ()).throw(SystemExit("api_keys.toml is corrupt")))

    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "responses",
                             "body": {"model": "codex:gpt-5.4-mini"}, "is_stream": True})  # must not raise

    assert not state.stop.is_set()  # the engine lives
    assert captured["error"].startswith("engine error 401:")  # the consumer got a terminal signal
    assert "unexpectedly" in capsys.readouterr().err  # and the operator got the real cause

    # Touchpoint 2: an unprimed holder self-heals from a store that is garbage on disk.
    monkeypatch.setattr(credentials, "load_toml", real_load_toml)
    unprimed = _codex_serve_state(monkeypatch, tmp_path)
    paths.api_keys_file().write_bytes(b"\x00 this is not TOML [")
    captured.clear()

    unprimed.codex_seat  # noqa: B018 — the holder exists; the job below reads through it
    serve.handle_job(unprimed, {"transaction_id": "t2", "endpoint_path": "responses",
                                "body": {"model": "codex:gpt-5.4-mini"}, "is_stream": True})

    assert not unprimed.stop.is_set()
    assert "unreadable" in captured["error"]  # a job error naming the state, not a hang


def test_serve_codex_refusal_wording_distinguishes_interrupted_from_revoked(monkeypatch, tmp_path, capsys):
    """AC 6's one observable artifact, pinned end to end through the holder (code review): the
    journal left by a killed exchange must turn the NEXT refusal's warning into the
    lost-rotation diagnosis, and a plain dead seat must NOT get that crash-sounding story. The
    two wordings share the remedy but must never share the diagnosis — a regression collapsing
    the `interrupted` branch would otherwise pass the whole suite."""
    from remote import api_keys, credentials

    # A prior exchange died between the vendor call and the persist (the planted journal).
    state = _codex_serve_state(monkeypatch, tmp_path)
    api_keys.store_codex_bundle(_codex_bundle())
    state.codex_seat.prime_from_store()
    data = credentials.load_toml(paths.api_keys_file())
    credentials.atomic_write_toml(
        paths.api_keys_file(),
        {**data, "codex": {**data["codex"], "refresh_pending_since": 1_700_000_000}},
    )
    _mock_vendor(monkeypatch, lambda request: httpx.Response(400, json={"error": "invalid_grant"}))

    assert state.codex_seat.refresh("tok-access") is False
    interrupted = capsys.readouterr().err
    assert "interrupted before it could be saved" in interrupted  # the lost-rotation diagnosis
    assert "grid join --api codex" in interrupted

    # The plain dead seat: same refusal, no journal — no crash-sounding story.
    plain = _codex_serve_state(monkeypatch, tmp_path)
    api_keys.store_codex_bundle(_codex_bundle())
    plain.codex_seat.prime_from_store()

    assert plain.codex_seat.refresh("tok-access") is False
    revoked = capsys.readouterr().err
    assert "revoked" in revoked
    assert "interrupted before it could be saved" not in revoked  # the diagnoses never blur


def test_serve_codex_maybe_refresh_fires_on_expiry_or_window_only(monkeypatch, tmp_path):
    """AC 5's due-conditions (ADR 0015 D-d, proactive): rotate when the token's own expiry is
    inside the margin, OR when the last rotation is older than the window (the vendor's real
    rotation window is UNVERIFIED — facts #6 — so an idle grid errs toward rotating). A healthy,
    recently-rotated seat is left alone (every rotation is one more crash window), and an
    UNPRIMED holder never fires at all — an identity that serves no codex engine must not rotate
    a seat another grid on this box may own, even though the store has one."""
    from remote import api_keys, codex_oauth

    now = int(time.time())

    def holder_with(access_token, last_refresh):
        state = _codex_serve_state(monkeypatch, tmp_path)
        api_keys.store_codex_bundle(codex_oauth.CodexBundle(
            access_token=access_token, refresh_token="rt-1", account_id="acct-1",
            plan_type="free", last_refresh=last_refresh,
        ))
        state.codex_seat.prime_from_store()
        calls = []
        monkeypatch.setattr(state.codex_seat, "refresh", lambda stale: calls.append(stale) or True)
        return state, calls

    expiring = _codex_jwt({"chatgpt_account_id": "acct-1"}, exp=now + 60)
    state, calls = holder_with(expiring, last_refresh=now - 10)
    state.codex_seat.maybe_refresh(now)
    assert calls == [expiring]  # expiry inside the margin → rotate, stale-compare on this token

    no_exp = _codex_jwt({"chatgpt_account_id": "acct-1"})
    state, calls = holder_with(no_exp, last_refresh=now - 86_401)
    state.codex_seat.maybe_refresh(now)
    assert calls == [no_exp]  # no readable expiry → the rotation window rules

    healthy = _codex_jwt({"chatgpt_account_id": "acct-1"}, exp=now + 7_200)
    state, calls = holder_with(healthy, last_refresh=now - 10)
    state.codex_seat.maybe_refresh(now)
    assert calls == []  # not due — no gratuitous rotation

    unprimed = _codex_serve_state(monkeypatch, tmp_path)  # store still holds the last bundle
    _mock_serve_engine(monkeypatch, lambda request: pytest.fail("an unprimed holder must not rotate"))
    unprimed.codex_seat.maybe_refresh(now)  # no-op, no store read adopted, nothing spent


def test_serve_heartbeat_tick_rotates_a_due_seat_even_when_the_relay_errors(monkeypatch, tmp_path):
    """AC 5, the wiring: the proactive check runs on the heartbeat TICK — a job-less loop still
    rotates a near-expiry seat (one exchange, persisted), and a tick whose relay call failed still
    checks (the relay being unreachable says nothing about the vendor). The rotated seat is not
    due on the next tick, so exactly one exchange."""
    from remote import api_keys, codex_oauth, relay, serve

    def run_loop(heartbeat_behaviour):
        state = _codex_serve_state(monkeypatch, tmp_path)
        now = int(time.time())
        api_keys.store_codex_bundle(codex_oauth.CodexBundle(
            access_token=_codex_jwt({"chatgpt_account_id": "acct-1"}, exp=now + 60),
            refresh_token="rt-1", account_id="acct-1", plan_type="free", last_refresh=now - 10,
        ))
        state.codex_seat.prime_from_store()
        new_access = _codex_jwt({"chatgpt_account_id": "acct-1"}, exp=now + 999_999)
        exchanges = []

        def vendor(request):
            assert request.url.host == "auth.openai.com"  # job-less: only the token endpoint
            exchanges.append(1)
            return httpx.Response(200, json={"access_token": new_access, "refresh_token": "rt-2"})

        _mock_serve_engine(monkeypatch, vendor)
        ticks = []

        def fake_heartbeat(url, tok, *, load):
            ticks.append(1)
            if len(ticks) >= 2:
                state.stop.set()  # end the loop AFTER tick 2's wait
            return heartbeat_behaviour()

        monkeypatch.setattr(relay, "heartbeat", fake_heartbeat)
        monkeypatch.setattr(relay, "HEARTBEAT_INTERVAL", 0.01)
        serve._heartbeat_loop(state)
        return exchanges, state

    exchanges, state = run_loop(lambda: "ok")
    assert exchanges == [1]  # tick 1 rotated; tick 2 saw a fresh seat and left it alone
    assert api_keys.load_codex_bundle().refresh_token == "rt-2"

    exchanges, _state = run_loop(lambda: (_ for _ in ()).throw(relay.RelayError("relay down")))
    assert exchanges == [1]  # a failed heartbeat still runs the proactive check


def test_serve_heartbeat_refresh_failure_never_stops_the_engine(monkeypatch, tmp_path, capsys):
    """A refresh failure is a WARN, never an engine stop (no auto-eject — ADR 0015): the loop
    survives the failed tick, the failure gate stops the next tick from hammering the vendor,
    and even a maybe_refresh that RAISES is swallowed by the hook — `_heartbeat_loop` runs under
    `_supervise`, where anything escaping stops the whole engine."""
    from remote import api_keys, codex_oauth, relay, serve

    state = _codex_serve_state(monkeypatch, tmp_path)
    now = int(time.time())
    api_keys.store_codex_bundle(codex_oauth.CodexBundle(
        access_token=_codex_jwt({"chatgpt_account_id": "acct-1"}, exp=now + 60),
        refresh_token="rt-1", account_id="acct-1", plan_type="free", last_refresh=now - 10,
    ))
    state.codex_seat.prime_from_store()
    exchanges = []

    def vendor(request):
        exchanges.append(1)
        return httpx.Response(500, json={})

    _mock_serve_engine(monkeypatch, vendor)
    ticks = []

    def fake_heartbeat(url, tok, *, load):
        ticks.append(1)
        if len(ticks) >= 3:
            state.stop.set()
        return "ok"

    monkeypatch.setattr(relay, "heartbeat", fake_heartbeat)
    monkeypatch.setattr(relay, "HEARTBEAT_INTERVAL", 0.01)

    serve._heartbeat_loop(state)

    assert len(ticks) == 3  # the loop OUTLIVED the failure — only the fake heartbeat ended it
    assert exchanges == [1]  # the failure gate held ticks 2-3 back from the vendor
    assert "will retry" in capsys.readouterr().err  # transient wording, not sign-in-again

    # The belt under the braces: a maybe_refresh that raises (even SystemExit — the TOML loader's
    # corrupt-file idiom) is swallowed into a warn, so _supervise never sees it.
    monkeypatch.setattr(state.codex_seat, "maybe_refresh",
                        lambda now: (_ for _ in ()).throw(SystemExit("corrupt store")))
    serve._maybe_refresh_codex(state)  # must not raise
    assert "engine unaffected" in capsys.readouterr().err


def test_serve_drain_waits_out_an_inflight_codex_exchange(monkeypatch, tmp_path, capsys):
    """The shutdown clause of ADR 0015 D-d: a worker mid-exchange has a journal on disk and a
    vendor call in flight — abandoning it at the drain loses a rotation the journal can then only
    DIAGNOSE. `_serve_loop`'s teardown waits on the exchange FLAG (not the thread): it returns
    only after the persist, the rotation lands, and a teardown with NO exchange in flight pays
    nothing."""
    from remote import api_keys, relay, serve

    state = _codex_serve_state(monkeypatch, tmp_path)
    now = int(time.time())
    api_keys.store_codex_bundle(_codex_bundle())
    state.codex_seat.prime_from_store()
    monkeypatch.setattr(serve, "_DRAIN_TIMEOUT", 0.05)
    monkeypatch.setattr(serve, "_poll_loop", lambda s: None)
    monkeypatch.setattr(serve, "_heartbeat_loop", lambda s: None)
    monkeypatch.setattr(relay, "unregister_node", lambda *a, **k: None)
    new_access = _codex_jwt({"chatgpt_account_id": "acct-1"}, exp=now + 999_999)
    release = threading.Event()

    def vendor(request):
        release.wait(5)  # hold the exchange mid-flight until the test releases it
        return httpx.Response(200, json={"access_token": new_access, "refresh_token": "rt-2"})

    _mock_serve_engine(monkeypatch, vendor)
    refresher = threading.Thread(target=lambda: state.codex_seat.refresh("tok-access"), daemon=True)
    refresher.start()
    deadline = time.monotonic() + 5
    while not state.codex_seat.exchange_in_flight() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert state.codex_seat.exchange_in_flight()

    state.stop.set()  # a SIGTERM landed; the exchange is already past its abandon-check
    threading.Timer(0.3, release.set).start()
    started = time.monotonic()
    serve._serve_loop(state)
    elapsed = time.monotonic() - started

    assert elapsed >= 0.25  # the teardown WAITED for the persist, well past the 0.05s drain
    refresher.join(5)
    assert api_keys.load_codex_bundle().access_token == new_access  # the rotation landed
    assert "waiting for an in-flight codex token exchange" in capsys.readouterr().err

    started = time.monotonic()
    serve._serve_loop(state)  # nothing in flight now
    assert time.monotonic() - started < 0.2  # no gratuitous wait
    assert "waiting for an in-flight" not in capsys.readouterr().err


def test_serve_drain_reports_an_exchange_it_could_not_wait_out(monkeypatch, tmp_path, capsys):
    """The bounded half: an exchange that outlives even the exchange-drain budget is reported —
    the journal it leaves makes the possible loss diagnosable at the next refresh, and the
    operator hears it now rather than discovering a zombie later."""
    from remote import api_keys, relay, serve

    state = _codex_serve_state(monkeypatch, tmp_path)
    api_keys.store_codex_bundle(_codex_bundle())
    state.codex_seat.prime_from_store()
    monkeypatch.setattr(serve, "_DRAIN_TIMEOUT", 0.05)
    monkeypatch.setattr(serve, "_CODEX_EXCHANGE_DRAIN", 0.2)
    monkeypatch.setattr(serve, "_poll_loop", lambda s: None)
    monkeypatch.setattr(serve, "_heartbeat_loop", lambda s: None)
    monkeypatch.setattr(relay, "unregister_node", lambda *a, **k: None)
    stuck = threading.Event()

    def vendor(request):
        stuck.wait(10)  # never released within the drain budget
        return httpx.Response(500, json={})

    _mock_serve_engine(monkeypatch, vendor)
    refresher = threading.Thread(target=lambda: state.codex_seat.refresh("tok-access"), daemon=True)
    refresher.start()
    deadline = time.monotonic() + 5
    while not state.codex_seat.exchange_in_flight() and time.monotonic() < deadline:
        time.sleep(0.005)

    state.stop.set()
    serve._serve_loop(state)

    err = capsys.readouterr().err
    assert "still unfinished" in err and "grid join --api codex" in err
    stuck.set()  # unblock the daemon thread before the test ends
    refresher.join(5)


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


def test_serve_handle_job_api_auth_quota_failures_warn_on_stderr(monkeypatch, tmp_path, capsys):
    """A vendor auth/quota failure (401/403/429) on an API engine additionally warns on the
    engine's stderr log — the per-job error alone is invisible to the operator. The engine stays
    registered and the loop stays alive (handle_job returns normally). No key in the warning."""
    from remote import relay, serve

    state = _api_serve_state(monkeypatch, tmp_path)
    errors = []
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: errors.append(message))
    _mock_serve_engine(monkeypatch, lambda r: httpx.Response(429, text="rate limited"))

    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "chat/completions",
                             "body": {"model": "openai:gpt-5.5"}, "is_stream": False})

    assert errors and "429" in errors[0]          # the consumer sees status + snippet
    err = capsys.readouterr().err
    assert "openai" in err and "429" in err       # ... and the operator sees the warn
    assert "sk-test-123" not in err               # never the key


def test_serve_handle_job_api_5xx_is_job_error_without_auth_warn(monkeypatch, tmp_path, capsys):
    """A vendor 5xx is an ordinary upstream failure: job error passes through, but no auth/quota
    warning — 500s say nothing about the key."""
    from remote import relay, serve

    state = _api_serve_state(monkeypatch, tmp_path)
    errors = []
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: errors.append(message))
    _mock_serve_engine(monkeypatch, lambda r: httpx.Response(500, text="server error"))

    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "chat/completions",
                             "body": {"model": "openai:gpt-5.5"}, "is_stream": False})

    assert errors and "500" in errors[0]
    assert "quota" not in capsys.readouterr().err.lower()


def test_serve_handle_job_hardware_401_has_no_api_warn(monkeypatch, tmp_path, capsys):
    """A hardware engine's 401 is not a vendor-key event: job error only, no API warn."""
    from remote import relay, serve

    state = _serve_state(monkeypatch, tmp_path)
    errors = []
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: errors.append(message))
    _mock_serve_engine(monkeypatch, lambda r: httpx.Response(401, text="denied"))

    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "chat/completions",
                             "body": {"model": "m"}, "is_stream": False})

    assert errors and "401" in errors[0]
    assert "quota" not in capsys.readouterr().err.lower()


def test_serve_handle_job_api_stream_429_warns_too(monkeypatch, tmp_path, capsys):
    """The streamed forward path shares the auth/quota warn — a streaming consumer's 429 must not
    be quieter than a whole-body one."""
    from remote import relay, serve

    state = _api_serve_state(monkeypatch, tmp_path)
    errors = []
    monkeypatch.setattr(relay, "submit_error",
                        lambda url, tok, txn, *, message, tokens_delivered=0: errors.append(message))
    _mock_serve_engine(monkeypatch, lambda r: httpx.Response(429, text="rate limited"))

    serve.handle_job(state, {"transaction_id": "t1", "endpoint_path": "chat/completions",
                             "body": {"model": "openai:gpt-5.5"}, "is_stream": True})

    assert errors and "429" in errors[0]
    assert "429" in capsys.readouterr().err


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


def test_logout_leaves_api_key_store_intact(monkeypatch, tmp_path, capsys):
    """`grid logout` deletes the sign-in credential store but NEVER the vendor key store — the API
    key belongs to the provider's vendor account, not to the autonomous session (ADR 0012)."""
    from remote import api_keys, credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    credentials.save_credentials({"session_token": "S"})
    api_keys.store_key("openai", "sk-keep-me")

    assert cli.cmd_logout(cli.build_parser().parse_args(["logout"])) == 0
    assert not paths.credentials_file().exists()
    assert api_keys.load_key("openai") == "sk-keep-me"


def test_logout_leaves_the_codex_seat_intact(monkeypatch, tmp_path, capsys):
    """Same rule for the OAuth seat (ADR 0015 D-c, user story 5): signing out of the grid must not
    disconnect the provider's ChatGPT subscription. The bundle is a rotating credential rather than
    a static key, so a logout that took it would cost a full re-authorization, not just a re-read —
    and a later join must reuse it with no browser."""
    from remote import api_keys, credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    credentials.save_credentials({"session_token": "S"})
    api_keys.store_codex_bundle(_bundle())

    assert cli.cmd_logout(cli.build_parser().parse_args(["logout"])) == 0
    assert not paths.credentials_file().exists()
    assert api_keys.load_codex_bundle() == _bundle()


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


def test_remote_engines_renders_api_engine_kind_openai(monkeypatch, tmp_path, capsys):
    """An API engine surfaces on the grid page with kind `openai`: the overview renderer prints whatever
    `node.engine` the relay returns, and an API join advertises engine `openai` (issue 05 / AC#4)."""
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_overview(monkeypatch, {"nodes": [
        {"name": "keybox", "device": "Linux x86_64", "engine": "openai",
         "models": ["openai:gpt-5.5"], "online": True},
    ]})
    assert cli.main(["engines"]) == 0
    out = capsys.readouterr().out
    assert "openai" in out and "openai:gpt-5.5" in out  # the API engine's kind label + its namespaced model


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


def test_remote_models_prepends_auto_when_router_enabled(monkeypatch, tmp_path, capsys):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_overview(monkeypatch, {**_OVERVIEW_2NODES, "router_enabled": True})
    assert cli.main(["models"]) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert lines == ["auto", "glm-5.2", "qwen-3"]  # auto first (mirrors /relay/v1/models), then engine models


def test_remote_models_omits_auto_when_router_disabled(monkeypatch, tmp_path, capsys):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_overview(monkeypatch, {**_OVERVIEW_2NODES, "router_enabled": False})
    assert cli.main(["models"]) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert lines == ["glm-5.2", "qwen-3"] and "auto" not in lines


def test_remote_models_omits_auto_when_field_absent(monkeypatch, tmp_path, capsys):
    # An older master's overview lacks router_enabled → no auto row (graceful degradation).
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_overview(monkeypatch, _OVERVIEW_2NODES)
    assert cli.main(["models"]) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert "auto" not in lines


def test_remote_models_verbose_shows_auto_row_when_router_enabled(monkeypatch, tmp_path, capsys):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_overview(monkeypatch, {**_OVERVIEW_2NODES, "router_enabled": True})
    assert cli.main(["models", "--verbose"]) == 0
    out = capsys.readouterr().out
    assert "auto" in out and "grid-router" in out  # the reserved row, owner grid-router


def test_remote_models_json_includes_auto_first_when_router_enabled(monkeypatch, tmp_path, capsys):
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_overview(monkeypatch, {**_OVERVIEW_2NODES, "router_enabled": True})
    assert cli.main(["models", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0] == {"model": "auto", "engine": "grid-router", "node": ""}


def test_remote_models_shows_auto_even_with_zero_nodes_when_enabled(monkeypatch, tmp_path, capsys):
    # Mirrors /relay/v1/models: auto is advertised whenever routing is on, independent of engines.
    _seed_running_remote_grid(monkeypatch, tmp_path)
    _mock_overview(monkeypatch, {"nodes": [], "router_enabled": True})
    assert cli.main(["models"]) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert lines == ["auto"]


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


# ---------------------------------------------------------------------------
# Remote router — `grid router` command group (cli/remote_router.py, ADR 0013)
# ---------------------------------------------------------------------------

def _mock_router(monkeypatch, *, config=None, enable=None, disable=None,
                 set_advisors=None, remove_advisor=None, catalog=None):
    """Stub the six control-plane router calls; record what each was invoked with. Replies use the v2
    ``advisors`` shape (``{provider, model}`` pairs) — never a key or base URL."""
    from remote import control_plane

    calls = {}

    def _get(session_token, network_id, api_url=None):
        calls["status"] = {"session": session_token, "network_id": network_id}
        return config if config is not None else {"enabled": False, "advisors": []}

    def _enable(session_token, network_id, api_url=None):
        calls["enable"] = {"session": session_token, "network_id": network_id}
        return enable if enable is not None else {"enabled": True, "advisors": [], "synced": True}

    def _disable(session_token, network_id, api_url=None):
        calls["disable"] = {"session": session_token, "network_id": network_id}
        return disable if disable is not None else {"enabled": False, "advisors": [], "synced": True}

    def _set(session_token, network_id, advisors, api_url=None):
        calls["set_advisors"] = {
            "session": session_token, "network_id": network_id, "advisors": advisors}
        return set_advisors if set_advisors is not None else {
            "enabled": False,
            "advisors": [{"provider": p, "model": m} for p, m in advisors],
            "synced": True,
        }

    def _remove(session_token, network_id, provider, model=None, api_url=None):
        calls["remove_advisor"] = {
            "session": session_token, "network_id": network_id, "provider": provider, "model": model}
        return remove_advisor if remove_advisor is not None else {
            "enabled": False, "advisors": [], "synced": True}

    def _catalog(session_token, api_url=None):
        calls["catalog"] = {"session": session_token}
        return catalog if catalog is not None else {"providers": []}

    monkeypatch.setattr(control_plane, "get_router_config", _get)
    monkeypatch.setattr(control_plane, "enable_router", _enable)
    monkeypatch.setattr(control_plane, "disable_router", _disable)
    monkeypatch.setattr(control_plane, "set_advisors", _set)
    monkeypatch.setattr(control_plane, "remove_advisor", _remove)
    monkeypatch.setattr(control_plane, "get_router_catalog", _catalog)
    return calls


# -- enable / disable / status ----------------------------------------------

def test_router_enable_calls_control_plane_and_confirms(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                 networks=[{"network_id": "n1", "name": "team"}], active="team")
    calls = _mock_router(monkeypatch, enable={"enabled": True, "advisors": [], "synced": True})
    assert cli.main(["router", "enable"]) == 0
    out = capsys.readouterr().out
    assert calls["enable"] == {"session": "sess-tok", "network_id": "n1"}
    assert "enabled" in out.lower()


def test_router_disable_calls_control_plane_and_confirms(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                 networks=[{"network_id": "n1", "name": "team"}], active="team")
    calls = _mock_router(monkeypatch, disable={"enabled": False, "advisors": [], "synced": True})
    assert cli.main(["router", "disable"]) == 0
    out = capsys.readouterr().out
    assert calls["disable"] == {"session": "sess-tok", "network_id": "n1"}
    assert "disabled" in out.lower()


def test_router_status_shows_state_and_advisors_as_tokens(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                 networks=[{"network_id": "n1", "name": "team"}], active="team")
    config = {"enabled": True, "advisors": [
        {"provider": "openai", "model": "gpt-5-mini"},
        {"provider": "openai", "model": "gpt-4o-mini"}]}
    calls = _mock_router(monkeypatch, config=config)
    assert cli.main(["router", "status"]) == 0
    out = capsys.readouterr().out
    assert calls["status"] == {"session": "sess-tok", "network_id": "n1"}
    assert "enabled" in out.lower()
    assert "openai:gpt-5-mini" in out and "openai:gpt-4o-mini" in out
    # order = priority: position 1 (gpt-5-mini) prints before position 2 (gpt-4o-mini)
    assert out.index("gpt-5-mini") < out.index("gpt-4o-mini")
    # no base_url / position leakage from the old shape
    assert "https://" not in out


def test_router_status_empty_reports_no_advisors(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                 networks=[{"network_id": "n1", "name": "team"}], active="team")
    _mock_router(monkeypatch, config={"enabled": False, "advisors": []})
    assert cli.main(["router", "status"]) == 0
    out = capsys.readouterr().out
    assert "disabled" in out.lower()
    assert "no advisors" in out.lower()


def test_router_status_json_echoes_masked_config(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                 networks=[{"network_id": "n1", "name": "team"}], active="team")
    config = {"enabled": True, "advisors": [{"provider": "openai", "model": "gpt-5-mini"}]}
    _mock_router(monkeypatch, config=config)
    assert cli.main(["router", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == config


def test_router_status_selects_grid_with_flag(monkeypatch, tmp_path):
    _seed_remote(monkeypatch, tmp_path,
                 networks=[{"network_id": "n1", "name": "team"},
                           {"network_id": "n2", "name": "other"}], active="team")
    calls = _mock_router(monkeypatch, config={"enabled": False, "advisors": []})
    assert cli.main(["router", "status", "--grid", "other"]) == 0
    assert calls["status"]["network_id"] == "n2"  # --grid overrides the active grid


def test_router_status_rejects_positional_grid():
    # Uniform selection: after the v2 reshape a grid is named ONLY via --grid; a positional grid name is no
    # longer accepted (guards the whole group from a mixed-idiom regression).
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["router", "status", "team"])


# -- set-advisors -----------------------------------------------------------

def test_router_set_advisors_replaces_chain_and_confirms(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                 networks=[{"network_id": "n1", "name": "team"}], active="team")
    calls = _mock_router(monkeypatch)
    assert cli.main(["router", "set-advisors", "openai:gpt-5-mini", "openai:gpt-4o-mini"]) == 0
    out = capsys.readouterr().out
    assert calls["set_advisors"] == {
        "session": "sess-tok", "network_id": "n1",
        "advisors": [("openai", "gpt-5-mini"), ("openai", "gpt-4o-mini")],  # order = priority, forwarded
    }
    assert "openai:gpt-5-mini" in out and "openai:gpt-4o-mini" in out


def test_router_set_advisors_single_token(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                 networks=[{"network_id": "n1", "name": "team"}], active="team")
    calls = _mock_router(monkeypatch)
    assert cli.main(["router", "set-advisors", "openai:gpt-5-mini"]) == 0
    assert calls["set_advisors"]["advisors"] == [("openai", "gpt-5-mini")]


def test_router_set_advisors_bare_provider_forwards_none(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                 networks=[{"network_id": "n1", "name": "team"}], active="team")
    calls = _mock_router(monkeypatch)
    assert cli.main(["router", "set-advisors", "openai"]) == 0
    # bare provider → model None; the control-plane client sends it provider-only (server resolves default)
    assert calls["set_advisors"]["advisors"] == [("openai", None)]


def test_router_set_advisors_uses_grid_flag_not_active(monkeypatch, tmp_path):
    _seed_remote(monkeypatch, tmp_path,
                 networks=[{"network_id": "n1", "name": "team"},
                           {"network_id": "n2", "name": "other"}], active="team")
    calls = _mock_router(monkeypatch)
    assert cli.main(["router", "set-advisors", "openai:gpt-5-mini", "--grid", "other"]) == 0
    assert calls["set_advisors"]["network_id"] == "n2"  # --grid overrides the active grid


def test_router_set_advisors_rejects_fourth_token():
    parser = cli.build_parser()
    with pytest.raises(SystemExit):  # a 4th token is a clean parser rejection (AdvisorsAction cap)
        parser.parse_args(["router", "set-advisors",
                           "openai", "openai:gpt-5-nano", "openai:gpt-4o-mini", "openai:gpt-4.1-mini"])


def test_router_set_advisors_accepts_exactly_three_max():
    # The boundary: exactly MAX_ADVISORS (3) tokens must PARSE — guards a `>`→`>=` off-by-one regression in
    # AdvisorsAction that the over-limit test alone wouldn't catch.
    parser = cli.build_parser()
    ns = parser.parse_args(["router", "set-advisors",
                            "openai:gpt-5-mini", "openai:gpt-5-nano", "openai:gpt-4o-mini"])
    assert len(ns.advisors) == 3


@pytest.mark.parametrize("bad", [":m", "openai:", "a:b:c", ""])
def test_router_set_advisors_rejects_malformed_token(bad):
    parser = cli.build_parser()
    with pytest.raises(SystemExit):  # malformed provider[:model] → parse_advisor_token ArgumentTypeError
        parser.parse_args(["router", "set-advisors", bad])


def test_router_set_advisors_json_scrubs_any_key(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                 networks=[{"network_id": "n1", "name": "team"}], active="team")
    _mock_router(monkeypatch, set_advisors={"enabled": False, "synced": True, "advisors": [
        {"provider": "openai", "model": "gpt-5-mini", "api_key": "sk-should-not-appear"}]})
    assert cli.main(["router", "set-advisors", "openai:gpt-5-mini", "--json"]) == 0
    out = capsys.readouterr().out
    assert "sk-should-not-appear" not in out and "api_key" not in out


def test_router_set_advisors_surfaces_off_whitelist_400(monkeypatch, tmp_path):
    # A real control-plane call (transport stubbed): an off-whitelist model is a 400 whose detail lists the
    # valid names — the CLI surfaces that message verbatim, never a traceback.
    _seed_remote(monkeypatch, tmp_path,
                 networks=[{"network_id": "n1", "name": "team"}], active="team")
    _mock_control_plane(monkeypatch, lambda r: httpx.Response(400, json={
        "detail": "Invalid model 'gpt-9' for advisor 'openai'. Valid models: gpt-5-mini, gpt-5-nano"}))
    with pytest.raises(SystemExit) as exc:
        cli.main(["router", "set-advisors", "openai:gpt-9"])
    msg = str(exc.value)
    assert "400" in msg and "Valid models" in msg


# -- remove-advisor ---------------------------------------------------------

def test_router_remove_advisor_exact_pair(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                 networks=[{"network_id": "n1", "name": "team"}], active="team")
    calls = _mock_router(monkeypatch, remove_advisor={"enabled": True, "advisors": [], "synced": True})
    assert cli.main(["router", "remove-advisor", "openai:gpt-4o-mini"]) == 0
    out = capsys.readouterr().out
    assert calls["remove_advisor"] == {
        "session": "sess-tok", "network_id": "n1", "provider": "openai", "model": "gpt-4o-mini"}
    assert "openai:gpt-4o-mini" in out


def test_router_remove_advisor_bare_provider_removes_all(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                 networks=[{"network_id": "n1", "name": "team"}], active="team")
    calls = _mock_router(monkeypatch)
    assert cli.main(["router", "remove-advisor", "openai"]) == 0
    assert calls["remove_advisor"] == {
        "session": "sess-tok", "network_id": "n1", "provider": "openai", "model": None}


def test_router_remove_advisor_empty_reply_is_clean(monkeypatch, tmp_path, capsys):
    # Defensive: the real DELETE returns a full 200 body, but `_json_or_empty` coerces a 204/empty body to
    # `{}`. That path must still print a clean confirmation and exit 0 — never crash on `.get()`.
    _seed_remote(monkeypatch, tmp_path,
                 networks=[{"network_id": "n1", "name": "team"}], active="team")
    _mock_router(monkeypatch, remove_advisor={})  # 204-equivalent empty reply
    assert cli.main(["router", "remove-advisor", "openai"]) == 0
    assert "openai" in capsys.readouterr().out  # a confirmation still prints, no traceback


# -- models (catalog) -------------------------------------------------------

def test_router_models_renders_catalog_with_default_marked(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                 networks=[{"network_id": "n1", "name": "team"}], active="team")
    catalog = {"providers": [{"provider": "openai",
                              "models": ["gpt-5-mini", "gpt-5-nano", "gpt-4o-mini"],
                              "default_model": "gpt-5-mini"}]}
    calls = _mock_router(monkeypatch, catalog=catalog)
    assert cli.main(["router", "models"]) == 0
    out = capsys.readouterr().out
    assert calls["catalog"] == {"session": "sess-tok"}
    assert "openai:gpt-5-mini" in out and "openai:gpt-5-nano" in out and "openai:gpt-4o-mini" in out
    default_line = next(ln for ln in out.splitlines() if "gpt-5-mini" in ln)
    assert "default" in default_line.lower()  # the default model is marked
    nano_line = next(ln for ln in out.splitlines() if "gpt-5-nano" in ln)
    assert "default" not in nano_line.lower()  # a non-default is not marked


def test_router_models_json_echoes_reply(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                 networks=[{"network_id": "n1", "name": "team"}], active="team")
    catalog = {"providers": [
        {"provider": "openai", "models": ["gpt-5-mini"], "default_model": "gpt-5-mini"}]}
    _mock_router(monkeypatch, catalog=catalog)
    assert cli.main(["router", "models", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == catalog


def test_router_models_works_without_grid_resolved(monkeypatch, tmp_path, capsys):
    # No grids at all: `status` would error ("name a grid"), but `models` reads the account-level catalog
    # and must still work — it never resolves a grid.
    _seed_remote(monkeypatch, tmp_path, networks=[], active=None)
    calls = _mock_router(monkeypatch, catalog={"providers": [
        {"provider": "openai", "models": ["gpt-5-mini"], "default_model": "gpt-5-mini"}]})
    assert cli.main(["router", "models"]) == 0
    assert calls["catalog"] == {"session": "sess-tok"}
    assert "openai:gpt-5-mini" in capsys.readouterr().out


# -- no key path anywhere ---------------------------------------------------

def test_router_module_has_no_key_path():
    # The v2 reshape deletes every key path — env var, hidden prompt, and the resolver helpers.
    import cli.remote_router as rr
    for gone in ("RANKER_KEY_ENV", "_resolve_ranker_key", "_prompt_ranker_key"):
        assert not hasattr(rr, gone)


def test_router_set_advisors_has_no_key_or_url_flags():
    parser = cli.build_parser()
    for flag in (["--api-key", "sk-x"], ["--base-url", "https://x/v1"], ["--model", "m"]):
        with pytest.raises(SystemExit):  # none of these flags exist on the v2 surface
            parser.parse_args(["router", "set-advisors", "openai:gpt-5-mini", *flag])


# -- secret scrub (belt-and-suspenders) -------------------------------------

def test_router_status_json_scrubs_leaked_key(monkeypatch, tmp_path, capsys):
    # Defence in depth: even if grid-apis ever regressed its masking and returned a key, the CLI must not
    # echo it. The control plane is the source of truth for masking; this pins the client scrub.
    _seed_remote(monkeypatch, tmp_path,
                 networks=[{"network_id": "n1", "name": "team"}], active="team")
    leaked = {"enabled": True, "advisors": [
        {"provider": "openai", "model": "gpt-5-mini", "api_key": "sk-leaked-999"}]}
    _mock_router(monkeypatch, config=leaked)
    assert cli.main(["router", "status", "--json"]) == 0
    out = capsys.readouterr().out
    assert "sk-leaked-999" not in out and "api_key" not in out
    assert "gpt-5-mini" in out  # the non-secret fields still print


def test_router_status_json_scrubs_leaked_base_url(monkeypatch, tmp_path, capsys):
    # The invariant is "never a key OR URL" (ADR 0013). The master-facing snapshot already materializes
    # `{base_url, api_key, model}` triples; if a server regression ever leaked that shape onto the owner REST
    # reply, the client scrub must drop the base_url too — not just the key.
    _seed_remote(monkeypatch, tmp_path,
                 networks=[{"network_id": "n1", "name": "team"}], active="team")
    leaked = {"enabled": True, "advisors": [
        {"provider": "openai", "model": "gpt-5-mini", "base_url": "https://internal-proxy.example/v1"}]}
    _mock_router(monkeypatch, config=leaked)
    assert cli.main(["router", "status", "--json"]) == 0
    out = capsys.readouterr().out
    assert "internal-proxy" not in out and "base_url" not in out
    assert "gpt-5-mini" in out  # the non-secret fields still print


def test_router_status_never_shows_key_material(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                 networks=[{"network_id": "n1", "name": "team"}], active="team")
    config = {"enabled": True, "advisors": [{"provider": "openai", "model": "gpt-5-mini"}]}
    _mock_router(monkeypatch, config=config)
    for argv in (["router", "status"], ["router", "status", "--json"]):
        assert cli.main(argv) == 0
        out = capsys.readouterr().out
        assert "api_key" not in out and "sk-" not in out


# -- mutation sync caveat ---------------------------------------------------

def test_router_mutation_no_caveat_when_synced_absent(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                 networks=[{"network_id": "n1", "name": "team"}], active="team")
    _mock_router(monkeypatch, enable={"enabled": True, "advisors": []})  # reply omits `synced`
    assert cli.main(["router", "enable"]) == 0
    assert "shortly" not in capsys.readouterr().out.lower()


def test_router_mutation_warns_when_not_synced(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                 networks=[{"network_id": "n1", "name": "team"}], active="team")
    _mock_router(monkeypatch, enable={"enabled": True, "advisors": [], "synced": False})
    assert cli.main(["router", "enable"]) == 0
    out = capsys.readouterr().out.lower()
    assert "enabled" in out and "shortly" in out  # not-yet-synced caveat surfaced on the human line


def test_router_mutation_no_caveat_when_synced(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                 networks=[{"network_id": "n1", "name": "team"}], active="team")
    _mock_router(monkeypatch, enable={"enabled": True, "advisors": [], "synced": True})
    assert cli.main(["router", "enable"]) == 0
    assert "shortly" not in capsys.readouterr().out.lower()


def test_router_mutation_json_keeps_synced_verbatim(monkeypatch, tmp_path, capsys):
    _seed_remote(monkeypatch, tmp_path,
                 networks=[{"network_id": "n1", "name": "team"}], active="team")
    reply = {"enabled": True, "advisors": [], "synced": False}
    _mock_router(monkeypatch, enable=reply)
    assert cli.main(["router", "enable", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == reply


# -- gating / wiring --------------------------------------------------------

def test_router_classified_remote_only():
    assert "router" in dispatch.REMOTE_ONLY
    assert not (set(dispatch.AGNOSTIC) & set(dispatch.REMOTE_ONLY))
    assert not (set(dispatch.REMOTE_HANDLERS) & set(dispatch.REMOTE_ONLY))


def test_router_gated_in_local_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))  # default local mode
    with pytest.raises(SystemExit) as exc:
        cli.main(["router", "status"])
    assert "remote" in str(exc.value).lower()


def test_router_parser_wires_subcommands_to_handler():
    parser = cli.build_parser()
    argvs = (
        ["router", "status"], ["router", "enable"], ["router", "disable"], ["router", "models"],
        ["router", "set-advisors", "openai:gpt-5-mini"],
        ["router", "remove-advisor", "openai"],
    )
    for argv in argvs:
        assert parser.parse_args(argv).handler is cli.cmd_remote_router


# -- error passthrough ------------------------------------------------------

def test_router_requires_sign_in(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    state.set_mode("remote")  # remote, but no credentials on disk
    with pytest.raises(SystemExit) as exc:
        cli.main(["router", "status"])
    assert "signed in" in str(exc.value).lower()


def test_router_status_requires_grid_resolution(monkeypatch, tmp_path):
    _seed_remote(monkeypatch, tmp_path, networks=[], active=None)  # signed in, no grids
    with pytest.raises(SystemExit) as exc:
        cli.main(["router", "status"])
    assert "name a grid" in str(exc.value).lower()


def test_router_unknown_subcommand_errors(monkeypatch, tmp_path):
    _seed_remote(monkeypatch, tmp_path,
                 networks=[{"network_id": "n1", "name": "team"}], active="team")
    _mock_router(monkeypatch)
    # `grid=None` must be present: a bogus subcommand falls through the models short-circuit, resolves the
    # active grid, then hits the final guard. The parser blocks this path; the guard catches direct misuse.
    with pytest.raises(SystemExit) as exc:
        cli.cmd_remote_router(SimpleNamespace(subcommand="bogus", grid=None, json=False))
    assert "subcommand" in str(exc.value).lower()


def test_router_surfaces_control_plane_rejection(monkeypatch, tmp_path):
    _seed_remote(monkeypatch, tmp_path,
                 networks=[{"network_id": "n1", "name": "team"}], active="team")
    _mock_control_plane(monkeypatch,
                        lambda r: httpx.Response(403, json={"detail": "Network admin role required"}))
    with pytest.raises(SystemExit) as exc:
        cli.main(["router", "enable"])
    assert "403" in str(exc.value)


def test_router_surfaces_validation_error(monkeypatch, tmp_path):
    _seed_remote(monkeypatch, tmp_path,
                 networks=[{"network_id": "n1", "name": "team"}], active="team")
    _mock_control_plane(monkeypatch,
                        lambda r: httpx.Response(400, json={"detail": "configure an advisor first"}))
    with pytest.raises(SystemExit) as exc:
        cli.main(["router", "enable"])
    assert "400" in str(exc.value)


# -- control-plane wire contract (pins the /networks/ prefix, not /managed-networks/) -----------

def test_control_plane_get_router_config_gets_with_bearer(monkeypatch, tmp_path):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def handler(request):
        seen["method"], seen["path"] = request.method, request.url.path
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"enabled": False, "advisors": []})

    _mock_control_plane(monkeypatch, handler)
    assert control_plane.get_router_config("sess-tok", "n1") == {"enabled": False, "advisors": []}
    assert (seen["method"], seen["path"]) == ("GET", "/v1/grid/networks/n1/router")
    assert seen["auth"] == "Bearer sess-tok"


def test_control_plane_enable_router_posts(monkeypatch, tmp_path):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def handler(request):
        seen["method"], seen["path"] = request.method, request.url.path
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"enabled": True, "advisors": [], "synced": True})

    _mock_control_plane(monkeypatch, handler)
    control_plane.enable_router("sess-tok", "n1")
    assert (seen["method"], seen["path"]) == ("POST", "/v1/grid/networks/n1/router/enable")
    assert seen["auth"] == "Bearer sess-tok"


def test_control_plane_disable_router_posts(monkeypatch, tmp_path):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def handler(request):
        seen["method"], seen["path"] = request.method, request.url.path
        return httpx.Response(200, json={"enabled": False, "advisors": [], "synced": True})

    _mock_control_plane(monkeypatch, handler)
    control_plane.disable_router("sess-tok", "n1")
    assert (seen["method"], seen["path"]) == ("POST", "/v1/grid/networks/n1/router/disable")


def test_control_plane_set_advisors_puts_with_body_and_bearer(monkeypatch, tmp_path):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def handler(request):
        seen["method"], seen["path"] = request.method, request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"enabled": False, "advisors": [], "synced": True})

    _mock_control_plane(monkeypatch, handler)
    # A full pair AND a bare provider in one replace-all chain (order = priority).
    control_plane.set_advisors("sess-tok", "n1", [("openai", "gpt-5-mini"), ("openai", None)])
    assert (seen["method"], seen["path"]) == ("PUT", "/v1/grid/networks/n1/router/advisors")
    assert seen["auth"] == "Bearer sess-tok"
    assert seen["body"] == {"advisors": [
        {"provider": "openai", "model": "gpt-5-mini"},
        {"provider": "openai"},  # bare provider → model omitted; the server resolves the catalog default
    ]}


def test_control_plane_remove_advisor_deletes_with_query(monkeypatch, tmp_path):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def handler(request):
        seen["method"], seen["path"] = request.method, request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"enabled": False, "advisors": [], "synced": True})

    _mock_control_plane(monkeypatch, handler)
    # Exact pair → both query params; the path stays clean (no position segment).
    control_plane.remove_advisor("sess-tok", "n1", "openai", "gpt-5-mini")
    assert (seen["method"], seen["path"]) == ("DELETE", "/v1/grid/networks/n1/router/advisors")
    assert seen["params"] == {"provider": "openai", "model": "gpt-5-mini"}


def test_control_plane_remove_advisor_bare_provider_omits_model_param(monkeypatch, tmp_path):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(204)  # a successful DELETE may answer 204

    _mock_control_plane(monkeypatch, handler)
    # Bare provider → only `provider`; 204 must not crash `.json()`.
    assert control_plane.remove_advisor("sess-tok", "n1", "openai") == {}
    assert seen["params"] == {"provider": "openai"}


def test_control_plane_get_router_catalog_gets_with_bearer(monkeypatch, tmp_path):
    from remote import control_plane

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}
    catalog = {"providers": [
        {"provider": "openai", "models": ["gpt-5-mini", "gpt-5-nano"], "default_model": "gpt-5-mini"}]}

    def handler(request):
        seen["method"], seen["path"] = request.method, request.url.path
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=catalog)

    _mock_control_plane(monkeypatch, handler)
    # No network id in the path — the catalog is account-level (session token only).
    assert control_plane.get_router_catalog("sess-tok") == catalog
    assert (seen["method"], seen["path"]) == ("GET", "/v1/grid/router/catalog")
    assert seen["auth"] == "Bearer sess-tok"


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


def test_relay_model_price_roundtrips_namespaced_openai_name(monkeypatch, tmp_path):
    """A namespaced `openai:*` price rides the wire correctly: the colon is verbatim in the PUT body and
    percent-encodes to ONE safe path segment on DELETE — API-engine models are priced like any other
    (issue 05 / AC#5; the colon is treated exactly like the slash case above)."""
    from remote import relay

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    seen = {}

    def handler(request):
        seen.setdefault("urls", []).append(str(request.url))
        if request.method == "PUT":
            seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"model": "openai:gpt-5.5"})

    _mock_relay(monkeypatch, handler)
    relay.set_model_price("https://relay.example", "AT", model="openai:gpt-5.5", modality="chat",
                          input_rate=2.0, output_rate=8.0, cache_rate=0.5)
    relay.delete_model_price("https://relay.example", "AT", "openai:gpt-5.5")

    assert seen["body"]["model"] == "openai:gpt-5.5"                                # verbatim in the PUT body
    assert any("/relay/v1/grid/models/openai%3Agpt-5.5" in u for u in seen["urls"])  # one encoded path segment


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


def test_price_set_accepts_namespaced_openai_model_through_cli_main(monkeypatch, tmp_path):
    """`grid price set -m openai:gpt-5.5` is accepted verbatim through the whole CLI path — the per-model
    price command puts the namespaced API-engine name on the wire with no colon rejection (issue 05 / AC#5)."""
    _seed_running_remote_grid(monkeypatch, tmp_path)
    state.set_mode("remote")
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"model": "openai:gpt-5.5"})

    _mock_relay(monkeypatch, handler)
    rc = cli.main(["price", "set", "-m", "openai:gpt-5.5", "--input", "2.0", "--output", "8.0", "--cache", "0.5"])
    assert rc == 0
    assert seen["body"]["model"] == "openai:gpt-5.5"  # the colon-namespaced name rides through unchanged


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


def test_capability_entry_uses_ctx_size_and_omits_when_unknown():
    from remote import probe

    probed = {"vision": False, "tools": False, "parallel_tool_calls": False,
              "json_object": False, "json_schema": False}
    entry = probe.capability_entry(probed, 200000)
    assert entry["context_window"] == 200000
    assert entry["limits"]["max_context_tokens"] == 200000
    # An unknown window is OMITTED, never defaulted — the master treats absence as "unknown" rather
    # than being handed a fabricated 128000 that would mislead the auto-router's Advisor.
    default = probe.capability_entry(probed)
    assert "context_window" not in default
    assert "max_context_tokens" not in default["limits"]
    assert default["limits"]["max_output_tokens"] == 64000  # the other limits still present


def test_capabilities_threads_ctx_size_into_envelope(monkeypatch):
    from remote import probe

    monkeypatch.setattr(probe, "probe_llama_capabilities", lambda url, model: {
        "vision": False, "tools": False, "parallel_tool_calls": False,
        "json_object": False, "json_schema": False})
    env = probe.capabilities("http://h:8081/v1", "qwen3.5:0.8b", context_window=200000)
    assert env["models"]["qwen3.5:0.8b"]["context_window"] == 200000


def test_capabilities_envelope_omits_ctx_when_unknown(monkeypatch):
    from remote import probe

    monkeypatch.setattr(probe, "probe_llama_capabilities", lambda url, model: {
        "vision": False, "tools": False, "parallel_tool_calls": False,
        "json_object": False, "json_schema": False})
    env = probe.capabilities("http://h:8081/v1", "qwen3.5:0.8b")  # no context_window known
    assert "context_window" not in env["models"]["qwen3.5:0.8b"]


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
