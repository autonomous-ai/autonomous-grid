"""`grid engine install llama.cpp` on Windows (`shared.engine.installer`).

There was no Windows build at all, the pinned path was a symlink (which needs admin rights or Developer
Mode on Windows), the locator looked for `llama-server` without `.exe`, and `has_gpu_backend` knew only
the `libggml-*` names — Windows ships `ggml-*.dll`, so a CPU-only Windows box read as a GPU box.
The Windows branches run here on any OS by flipping the modules' platform flags.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli
from shared import paths
from shared.engine import installer, launcher
from shared.system import arch, gpu


@pytest.fixture
def windows(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    monkeypatch.setattr(installer, "_IS_WINDOWS", True)
    monkeypatch.setattr(paths, "_IS_WINDOWS", True)
    monkeypatch.setattr(arch, "normalized_machine", lambda: "x86_64")
    return tmp_path


def test_windows_builds_pin_the_release_and_a_sha256():
    assert installer.WINDOWS_BUILDS
    for build in installer.WINDOWS_BUILDS.values():
        for url, sha in ((build.url, build.sha256), *build.extra):
            assert url.startswith("https://github.com/ggml-org/llama.cpp/releases/download/")
            assert installer.LLAMA_RELEASE in url
            assert url.endswith(".zip")
            assert re.fullmatch(r"[0-9a-f]{64}", sha)


def test_pick_windows_build_cuda_x64_carries_the_cuda_runtime():
    build = installer.pick_windows_build("cuda", "x86_64")
    assert build.url.endswith(f"llama-{installer.LLAMA_RELEASE}-bin-win-cuda-12.4-x64.zip")
    assert [Path(url).name for url, _sha in build.extra] == ["cudart-llama-bin-win-cuda-12.4-x64.zip"]


def test_pick_windows_build_cpu_needs_no_runtime():
    assert installer.pick_windows_build("cpu", "x86_64").extra == ()
    assert installer.pick_windows_build("cpu", "aarch64").url.endswith("-bin-win-cpu-arm64.zip")


def test_pick_windows_build_unknown_machine_says_so():
    with pytest.raises(SystemExit, match="aarch64"):
        installer.pick_windows_build("cuda", "aarch64")


def _fake_release(monkeypatch) -> list[str]:
    fetched: list[str] = []

    def fake_fetch(label, url, sha256, tmp):
        fetched.append(Path(url).name)
        root = tmp / f"extract-{len(fetched)}"
        root.mkdir(parents=True)
        if "cudart" in url:
            (root / "cudart64_12.dll").write_bytes(b"rt")
        else:
            (root / "llama-server.exe").write_bytes(b"MZ")
            (root / "llama.dll").write_bytes(b"lib")
            (root / "ggml-cpu-haswell.dll").write_bytes(b"cpu")
            (root / "ggml-cuda.dll").write_bytes(b"cuda")
            (root / "llama-cli.exe").write_bytes(b"MZ")
        return root

    monkeypatch.setattr(installer, "fetch_and_extract", fake_fetch)
    return fetched


def test_install_windows_prebuilt_copies_exe_and_dlls_no_symlink(windows, monkeypatch):
    fetched = _fake_release(monkeypatch)
    server = installer.install_windows_prebuilt("cuda")
    prefix = paths.llama_prefix_dir()
    assert fetched == [f"llama-{installer.LLAMA_RELEASE}-bin-win-cuda-12.4-x64.zip",
                       "cudart-llama-bin-win-cuda-12.4-x64.zip"]
    assert server == prefix / "llama-server.exe" == paths.llama_server_bin()
    names = sorted(p.name for p in prefix.iterdir())
    assert names == ["cudart64_12.dll", "ggml-cpu-haswell.dll", "ggml-cuda.dll", "llama-cli.exe",
                     "llama-server.exe", "llama.dll"]
    assert not any(p.is_symlink() for p in paths.grid_home().rglob("*"))


def test_llama_server_bin_is_the_exe_on_windows(windows):
    assert paths.llama_server_bin() == paths.llama_prefix_dir() / "llama-server.exe"


def test_llama_server_bin_is_unchanged_off_windows(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    monkeypatch.setattr(paths, "_IS_WINDOWS", False)
    assert paths.llama_server_bin() == paths.bin_dir() / "llama-server"


def test_has_gpu_backend_reads_windows_dll_names(windows):
    prefix = paths.llama_prefix_dir()
    prefix.mkdir(parents=True)
    (prefix / "llama-server.exe").write_bytes(b"MZ")
    (prefix / "ggml-cpu-haswell.dll").write_bytes(b"cpu")
    assert launcher.has_gpu_backend() is False, "the CPU zip: no GPU backend beside the server"
    (prefix / "ggml-cuda.dll").write_bytes(b"cuda")
    assert launcher.has_gpu_backend() is True


def _engine_install(monkeypatch, gpus) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(installer, "is_macos", lambda: False)
    monkeypatch.setattr(installer, "is_windows", lambda: True)
    monkeypatch.setattr(gpu, "enumerate_gpus", lambda: gpus)
    monkeypatch.setattr(installer, "install_windows_prebuilt", lambda kind: calls.append(kind) or Path("x"))

    def not_linux(kind):
        raise AssertionError("Windows must not take the Linux tarball")

    monkeypatch.setattr(installer, "install_linux_prebuilt", not_linux)
    return calls


def test_engine_install_on_windows_picks_cuda_for_nvidia_else_cpu(monkeypatch, capsys):
    calls = _engine_install(monkeypatch, [SimpleNamespace(compute_cap_sm="sm_86")])
    assert cli.cmd_engine_install(argparse.Namespace(name="llama.cpp", target_sm=None, from_source=False)) == 0
    assert calls == ["cuda"]
    assert "CUDA" in capsys.readouterr().out

    calls = _engine_install(monkeypatch, [])
    assert cli.cmd_engine_install(argparse.Namespace(name="llama.cpp", target_sm=None, from_source=False)) == 0
    assert calls == ["cpu"]
    assert "CPU" in capsys.readouterr().out


def test_engine_install_on_windows_refuses_from_source(monkeypatch):
    _engine_install(monkeypatch, [])
    with pytest.raises(SystemExit, match="Windows"):
        cli.cmd_engine_install(argparse.Namespace(name="llama.cpp", target_sm=None, from_source=True))
