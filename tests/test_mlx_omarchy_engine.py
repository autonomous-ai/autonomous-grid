"""The mlx-omarchy engine: a second built-in text engine, chosen with `--engine`, gated to one machine.

Three contracts, each pinned from the side that would break silently:

- **The flag.** `--engine` already meant "detection filter" (`--kind ollama`); with `--serve` it now
  names the built-in. The two readings must not leak into each other — a built-in name with nothing
  to serve is refused rather than falling through to "no detected engine of kind mlx-omarchy".
- **The record.** The choice rides the engine spec as `"engine"`, absent for llama.cpp, so every
  record written before the key existed still launches what it always launched.
- **The launch.** The serve loops start `mlx_lm.server` instead of llama-server, and hand the relay
  the REPO ID as the upstream name — that server takes any unknown `model` for a Hugging Face repo
  to download, so an `--advertise-as` alias reaching it would start a fetch of a repo called
  `qwen7b`. The alias→upstream rewrite the loop already does for Ollama is what keeps it out.

The install itself runs only on an Apple Silicon Mac booted into Linux with the Vulkan driver, and
no CI box is one; the gate is tested through the same fake device tree `test_apple_linux.py` uses,
and the network/venv steps are stubbed.
"""

from __future__ import annotations

import argparse
import struct
from types import SimpleNamespace

import pytest

from cli import engine as engine_cmd
from cli import provider, remote_provider
from shared.engine import mlx_omarchy
from shared.models import catalog
from shared.system import apple_linux

REPO = "mlx-community/Qwen2.5-7B-Instruct-4bit"


def _omarchy_m(monkeypatch, tmp_path, *, icd=True):
    """Pretend to be an M1 Pro on Omarchy M: the same fake device tree as test_apple_linux."""
    tree = tmp_path / "device-tree"
    tree.mkdir(parents=True)
    (tree / "compatible").write_bytes(b"apple,j314s\x00apple,t6000\x00apple,arm-platform\x00")
    (tree / "model").write_bytes(b"Apple MacBook Pro (14-inch, M1 Pro, 2021)\x00")
    mem = tree / "memory@800000000"
    mem.mkdir()
    (mem / "reg").write_bytes(struct.pack(">QQ", 0x8_0000_0000, 32 * 1024 ** 3))
    icd_path = tmp_path / "icd.d" / "asahi_icd.aarch64.json"
    if icd:
        icd_path.parent.mkdir()
        icd_path.write_text("{}")
    monkeypatch.setattr(apple_linux.platform, "system", lambda: "Linux")
    monkeypatch.setattr(apple_linux.arch, "normalized_machine", lambda: "aarch64")
    monkeypatch.setattr(apple_linux, "_COMPATIBLE", tree / "compatible")
    monkeypatch.setattr(apple_linux, "_MODEL", tree / "model")
    monkeypatch.setattr(apple_linux, "_DEVICE_TREE", tree)
    monkeypatch.setattr(apple_linux, "_ASAHI_ICD", icd_path)
    monkeypatch.setattr(apple_linux.ctypes.util, "find_library",
                        lambda name: "libvulkan.so.1" if name == "vulkan" else None)


def _not_that_machine(monkeypatch):
    monkeypatch.setattr(apple_linux, "is_apple_silicon_linux", lambda: False)


def _join_args(**overrides) -> argparse.Namespace:
    base = dict(serve=None, models=[], at=None, kind=None, media=False, advertise_as=[], all=False)
    base.update(overrides)
    return argparse.Namespace(**base)


# ── the flag ───────────────────────────────────────────────────────────────────────────────────


def test_serve_without_engine_is_llama_cpp():
    assert provider.builtin_engine(_join_args(serve="model.gguf")) == "llama.cpp"


def test_serve_with_engine_mlx_omarchy_picks_it():
    assert provider.builtin_engine(_join_args(serve=REPO, kind="mlx-omarchy")) == "mlx-omarchy"


def test_a_built_in_name_with_nothing_to_serve_is_refused_not_detected():
    # Without --serve, --engine is the detection filter; letting a built-in's name through would
    # report "no detected engine of kind mlx-omarchy" — true, and useless.
    with pytest.raises(SystemExit, match="pair it with --serve"):
        provider.builtin_engine(_join_args(kind="mlx-omarchy"))


def test_the_detection_filter_reading_is_untouched():
    # `--kind ollama` with no --serve still means "only the detected ollama" — not a built-in.
    assert provider.builtin_engine(_join_args(kind="ollama")) == "llama.cpp"


def test_an_unknown_engine_with_serve_lists_the_built_ins():
    with pytest.raises(SystemExit, match="llama.cpp, mlx-omarchy"):
        provider.builtin_engine(_join_args(serve=REPO, kind="vllm"))


# ── the record (remote) ────────────────────────────────────────────────────────────────────────


def test_the_remote_spec_carries_the_engine_only_when_it_is_not_the_default():
    specs, media = remote_provider._resolve_serve_targets(_join_args(serve=REPO, kind="mlx-omarchy"))
    assert media is False
    assert specs == [{"endpoint_url": None, "models": [REPO], "engine_label": None, "engine": "mlx-omarchy"}]

    specs, _ = remote_provider._resolve_serve_targets(_join_args(serve="model.gguf"))
    # No key at all — so a record written before the key existed and one written today agree.
    assert "engine" not in specs[0]


def test_two_built_ins_are_two_engines_in_the_union():
    # Same shape (no endpoint URL) but different models, so the merge keeps both rather than
    # folding the MLX join into a llama.cpp one that happens to be live.
    base = [{"endpoint_url": None, "models": ["model.gguf"], "engine_label": None}]
    incoming = [{"endpoint_url": None, "models": [REPO], "engine_label": None, "engine": "mlx-omarchy"}]
    merged, changed = remote_provider._merge_engines(base, incoming)
    assert changed
    assert [spec.get("engine") for spec in merged] == [None, "mlx-omarchy"]


# ── the launch (remote serve loop) ─────────────────────────────────────────────────────────────


class _Proc:
    def __init__(self, pid=4242):
        self.pid = pid
        self._rc = None

    def poll(self):
        return self._rc

    def terminate(self):
        self._rc = 0

    def wait(self, timeout=None):
        return self._rc


def test_the_remote_loop_launches_mlx_and_hands_the_relay_the_repo_id(monkeypatch, tmp_path):
    from remote import serve

    started = []
    monkeypatch.setattr(mlx_omarchy, "start", lambda model, *, port: started.append((model, port)) or
                        mlx_omarchy.LlamaProcess(proc=_Proc(), port=port, log=tmp_path / "log"))
    monkeypatch.setattr(mlx_omarchy, "wait_ready", lambda proc, timeout=None: None)
    from local import runtime
    monkeypatch.setattr(runtime, "port_in_use", lambda port: False)

    spec = {"endpoint_url": None, "models": [REPO], "engine_label": None, "engine": "mlx-omarchy"}
    record = {"endpoint_port": 8081}
    url, launched, mod, advertised, upstream = serve._bring_up_one(spec, record, ["qwen7b"])

    assert started == [(REPO, 8081)]
    assert url == "http://127.0.0.1:8081/v1"
    assert mod is mlx_omarchy and launched.proc.pid == 4242
    # The alias is what the grid sees; the repo id is what the server is asked for.
    assert advertised == ["qwen7b"]
    assert upstream == [REPO]


def test_a_gguf_spec_still_takes_the_llama_cpp_path(monkeypatch, tmp_path):
    from remote import serve
    from shared.engine import launcher

    started = []
    monkeypatch.setattr(launcher, "assert_supported_build", lambda: None)
    monkeypatch.setattr(launcher, "start_llm", lambda model, **kw: started.append(model) or
                        launcher.LlamaProcess(proc=_Proc(), port=kw["port"], log=tmp_path / "log"))
    monkeypatch.setattr(launcher, "wait_for_models", lambda proc, timeout=120.0: None)
    monkeypatch.setattr(mlx_omarchy, "start", lambda *a, **k: pytest.fail("mlx must not launch"))
    from local import runtime
    monkeypatch.setattr(runtime, "port_in_use", lambda port: False)
    from shared import run_records
    monkeypatch.setattr(run_records, "effective_parallel", lambda record: 1)

    spec = {"endpoint_url": None, "models": ["model.gguf"], "engine_label": None}
    _, _, mod, advertised, upstream = serve._bring_up_one(spec, {"endpoint_port": 8081}, ["alias"])

    assert started == ["model.gguf"]
    assert mod is launcher
    assert advertised == upstream == ["alias"]  # llama-server is launched with --alias, so it IS the alias


def test_a_server_that_never_comes_up_is_not_orphaned(monkeypatch, tmp_path):
    from remote import serve

    proc = _Proc()
    stopped = []
    monkeypatch.setattr(mlx_omarchy, "start", lambda model, *, port:
                        mlx_omarchy.LlamaProcess(proc=proc, port=port, log=tmp_path / "log"))
    monkeypatch.setattr(mlx_omarchy, "wait_ready", lambda p, timeout=None: (_ for _ in ()).throw(SystemExit("boom")))
    monkeypatch.setattr(mlx_omarchy, "stop", lambda p, **k: stopped.append(p.proc))
    from local import runtime
    monkeypatch.setattr(runtime, "port_in_use", lambda port: False)

    spec = {"endpoint_url": None, "models": [REPO], "engine_label": None, "engine": "mlx-omarchy"}
    with pytest.raises(SystemExit, match="boom"):
        serve._bring_up_one(spec, {"endpoint_port": 8081}, [])
    assert stopped == [proc]


# ── the record (local) ─────────────────────────────────────────────────────────────────────────


def test_the_local_record_carries_the_engine(monkeypatch, tmp_path):
    written = {}
    monkeypatch.setattr(provider, "_record_path", lambda grid_id, engine_id: tmp_path / "missing")
    monkeypatch.setattr(provider, "_write_record", lambda grid_id, engine_id, record: written.update(record))
    # Stop before the detached child is spawned — the record is what this test is about.
    monkeypatch.setattr(provider.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("halt")))
    monkeypatch.setattr(provider.logging_setup, "cap_and_open_append", lambda *a, **k: open(tmp_path / "l", "a"))
    monkeypatch.setattr(provider.paths, "engines_dir", lambda grid_id: tmp_path)
    monkeypatch.setattr(provider.runtime, "utc_now", lambda: "now")

    args = _join_args(serve=REPO, kind="mlx-omarchy", name="m1", endpoint_port=8081)
    with pytest.raises(RuntimeError, match="halt"):
        provider._spawn_engine({"grid_id": "g", "name": "home"}, args, endpoint_url=None, models=[REPO],
                               engine=provider.builtin_engine(args))
    assert written["engine"] == "mlx-omarchy"
    assert written["models"] == [REPO]


def test_the_local_record_omits_the_engine_for_llama_cpp(monkeypatch, tmp_path):
    written = {}
    monkeypatch.setattr(provider, "_record_path", lambda grid_id, engine_id: tmp_path / "missing")
    monkeypatch.setattr(provider, "_write_record", lambda grid_id, engine_id, record: written.update(record))
    monkeypatch.setattr(provider.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("halt")))
    monkeypatch.setattr(provider.logging_setup, "cap_and_open_append", lambda *a, **k: open(tmp_path / "l", "a"))
    monkeypatch.setattr(provider.paths, "engines_dir", lambda grid_id: tmp_path)
    monkeypatch.setattr(provider.runtime, "utc_now", lambda: "now")

    args = _join_args(serve="model.gguf", name="box", endpoint_port=8081)
    with pytest.raises(RuntimeError, match="halt"):
        provider._spawn_engine({"grid_id": "g", "name": "home"}, args, endpoint_url=None, models=["model.gguf"])
    assert "engine" not in written


# ── the engine module ──────────────────────────────────────────────────────────────────────────


def test_install_refuses_every_machine_that_is_not_omarchy_m(monkeypatch):
    _not_that_machine(monkeypatch)
    with pytest.raises(SystemExit, match="Apple Silicon Mac booted into Linux"):
        mlx_omarchy.install()


def test_install_refuses_omarchy_m_without_the_vulkan_driver_and_names_the_fix(monkeypatch, tmp_path):
    _omarchy_m(monkeypatch, tmp_path, icd=False)
    with pytest.raises(SystemExit, match="pacman -S vulkan-asahi"):
        mlx_omarchy.install()


SUMS = (
    "c59e06037861be4e6fa0c0e3ded5c831f35765092572ed71ac105f012cf3bbc1  "
    "mlx_omarchy-0.32.2.dev202609100353+b3e977b-cp314-cp314-linux_aarch64.whl\n"
    "a9f64520c195d88a147913c1251423d09d691e69d21ca937f5734a7e404f3145  "
    "mlx_omarchy-0.32.2.dev202609100356+b3e977b4-cp311-cp311-linux_x86_64.whl\n"
)
INSTALL_SH = 'VERSION="${MLX_OMARCHY_VERSION:-v0.5.0}"\nMLX_LM_VERSION=0.32.0\nTRANSFORMERS_VERSION=5.17.0\n'


def _github(monkeypatch, *, latest="v0.5.0", sums=SUMS, install_sh=INSTALL_SH):
    """Answer the three URLs the resolver may fetch; anything else is a miss."""
    def get_text(url, timeout=20.0):
        if url == mlx_omarchy._LATEST_API:
            return None if latest is None else '{"tag_name": "%s"}' % latest
        if url.endswith("/SHA256SUMS"):
            return sums
        if url.endswith("/install.sh"):
            return install_sh
        return None
    monkeypatch.setattr(mlx_omarchy, "_get_text", get_text)


def test_the_latest_release_is_resolved_from_github_and_its_own_sums(monkeypatch):
    _github(monkeypatch)
    release = mlx_omarchy.resolve_release()

    assert release.tag == "v0.5.0"
    assert release.wheel.endswith("cp314-cp314-linux_aarch64.whl")  # never the x86_64 one
    assert release.sha256 == "c59e06037861be4e6fa0c0e3ded5c831f35765092572ed71ac105f012cf3bbc1"
    assert release.wheel_url == f"https://github.com/joshuaswarren/mlx-omarchy/releases/download/v0.5.0/{release.wheel}"


def test_the_mlx_lm_pins_follow_the_release_being_installed(monkeypatch):
    # What its author tested that release with, read from its install.sh — not what this repo
    # happened to copy in last.
    _github(monkeypatch)
    release = mlx_omarchy.resolve_release()
    assert release.mlx_lm == "mlx-lm==0.32.0"
    assert release.deps[0] == "transformers[sentencepiece]==5.17.0"
    assert release.deps[1:] == mlx_omarchy.MLX_LM_DEPS[1:]


def test_an_unreadable_installer_falls_back_to_the_known_pins(monkeypatch):
    _github(monkeypatch, install_sh=None)
    release = mlx_omarchy.resolve_release()
    assert release.mlx_lm == mlx_omarchy.MLX_LM and release.deps == mlx_omarchy.MLX_LM_DEPS


def test_version_pins_one_release(monkeypatch):
    _github(monkeypatch, latest="v0.9.9")
    assert mlx_omarchy.resolve_release("v0.4.2").tag == "v0.4.2"


def test_github_unreachable_falls_back_to_the_known_tag(monkeypatch, capsys):
    _github(monkeypatch, latest=None)
    release = mlx_omarchy.resolve_release()
    assert release.tag == mlx_omarchy.FALLBACK_RELEASE
    assert "Could not ask GitHub" in capsys.readouterr().out


def test_a_release_with_no_aarch64_wheel_is_refused(monkeypatch):
    _github(monkeypatch, sums=SUMS.splitlines()[1] + "\n")  # only the x86_64 line
    with pytest.raises(SystemExit, match="no cp314-cp314-linux_aarch64.whl wheel"):
        mlx_omarchy.resolve_release()


def test_a_missing_sums_file_is_refused_with_the_releases_page(monkeypatch):
    _github(monkeypatch, sums=None)
    with pytest.raises(SystemExit, match="releases"):
        mlx_omarchy.resolve_release("v0.0.0")


def _stub_install_steps(monkeypatch, tmp_path):
    monkeypatch.setattr(mlx_omarchy, "_require_system_libs", lambda: None)
    monkeypatch.setattr(mlx_omarchy.paths, "ensure_all", lambda: None)
    monkeypatch.setattr(mlx_omarchy, "_create_venv", lambda: None)
    monkeypatch.setattr(mlx_omarchy, "engine_dir", lambda: tmp_path / "engine")
    (tmp_path / "engine").mkdir()
    monkeypatch.setattr(mlx_omarchy, "version_file", lambda: tmp_path / "engine" / "VERSION")


def test_install_checks_the_wheel_against_the_release_digest(monkeypatch, tmp_path):
    _omarchy_m(monkeypatch, tmp_path)
    _github(monkeypatch)
    _stub_install_steps(monkeypatch, tmp_path)
    monkeypatch.setattr(mlx_omarchy, "is_installed", lambda: False)
    monkeypatch.setattr(mlx_omarchy.installer, "_download", lambda url, dest: dest.write_bytes(b"not the wheel"))
    monkeypatch.setattr(mlx_omarchy, "_pip", lambda *a: pytest.fail("pip must not run on a bad digest"))

    with pytest.raises(SystemExit, match="SHA-256 mismatch"):
        mlx_omarchy.install()


def test_install_orders_the_steps_and_keeps_mlx_lm_off_its_own_mlx(monkeypatch, tmp_path):
    # `mlx-lm` depends on upstream `mlx`, which ships the same module and would replace the Vulkan
    # build with one that fails at import — so it is installed --no-deps and its deps explicitly.
    _omarchy_m(monkeypatch, tmp_path)
    _github(monkeypatch)
    _stub_install_steps(monkeypatch, tmp_path)
    monkeypatch.setattr(mlx_omarchy, "is_installed", lambda: False)
    monkeypatch.setattr(mlx_omarchy.installer, "_download", lambda url, dest: dest.write_bytes(b"wheel"))
    monkeypatch.setattr(mlx_omarchy.installer, "_sha256", lambda path: SUMS.split()[0])
    pips = []
    monkeypatch.setattr(mlx_omarchy, "_pip", lambda *a: pips.append(a))
    monkeypatch.setattr(mlx_omarchy, "_smoke_test", lambda: "Apple M1 Pro (G13S B1)")

    assert mlx_omarchy.install() == "Apple M1 Pro (G13S B1)"
    assert pips[0] == ("--upgrade", "pip")
    assert pips[1][1].endswith("cp314-cp314-linux_aarch64.whl")
    assert pips[2] == ("--upgrade", "--no-deps", "mlx-lm==0.32.0")
    assert pips[3][0] == "--upgrade" and pips[3][1] == "transformers[sentencepiece]==5.17.0"
    assert mlx_omarchy.installed_version() == "v0.5.0"


def test_a_second_install_at_the_same_release_only_re_runs_the_smoke_test(monkeypatch, tmp_path):
    _omarchy_m(monkeypatch, tmp_path)
    _github(monkeypatch)
    _stub_install_steps(monkeypatch, tmp_path)
    (tmp_path / "engine" / "VERSION").write_text("v0.5.0 whatever.whl\n")
    monkeypatch.setattr(mlx_omarchy, "is_installed", lambda: True)
    monkeypatch.setattr(mlx_omarchy.installer, "_download", lambda url, dest: pytest.fail("no download"))
    monkeypatch.setattr(mlx_omarchy, "_smoke_test", lambda: "Apple M1 Pro (G13S B1)")

    assert mlx_omarchy.install() == "Apple M1 Pro (G13S B1)"


def test_a_newer_release_upgrades_an_existing_install(monkeypatch, tmp_path):
    _omarchy_m(monkeypatch, tmp_path)
    _github(monkeypatch, latest="v0.6.0")
    _stub_install_steps(monkeypatch, tmp_path)
    (tmp_path / "engine" / "VERSION").write_text("v0.5.0 old.whl\n")
    monkeypatch.setattr(mlx_omarchy, "is_installed", lambda: True)
    monkeypatch.setattr(mlx_omarchy.installer, "_download", lambda url, dest: dest.write_bytes(b"wheel"))
    monkeypatch.setattr(mlx_omarchy.installer, "_sha256", lambda path: SUMS.split()[0])
    pips = []
    monkeypatch.setattr(mlx_omarchy, "_pip", lambda *a: pips.append(a))
    monkeypatch.setattr(mlx_omarchy, "_smoke_test", lambda: "Apple M1 Pro (G13S B1)")

    mlx_omarchy.install()
    assert any(a[0] == "--upgrade" and a[1].endswith(".whl") for a in pips)
    assert mlx_omarchy.installed_version() == "v0.6.0"


def test_a_software_vulkan_device_fails_the_smoke_test(monkeypatch):
    # llvmpipe answering the Vulkan call means the Apple driver was not the one loaded: the engine
    # would "work" at CPU speed and nobody would know why.
    monkeypatch.setattr(mlx_omarchy.subprocess, "check_output", lambda *a, **k: "llvmpipe (LLVM 19.1.7, 128 bits)\n")
    with pytest.raises(SystemExit, match="software device"):
        mlx_omarchy._smoke_test()


def _generic_aarch64_linux(monkeypatch, *, compatible=b"linux,dummy-virt\x00"):
    """Any non-Apple aarch64 Linux box (what GRID_MLX_OMARCHY_DEV is for) — no device-tree fixture
    on disk, just the two cheap calls `is_aarch64_linux` reads."""
    monkeypatch.setattr(apple_linux.platform, "system", lambda: "Linux")
    monkeypatch.setattr(apple_linux.arch, "normalized_machine", lambda: "aarch64")
    monkeypatch.setattr(apple_linux, "_COMPATIBLE", None)  # unread by is_aarch64_linux
    monkeypatch.setattr(apple_linux, "is_apple_silicon_linux", lambda: False)
    monkeypatch.setattr(apple_linux, "describe_chip", lambda: ("", ""))


# ── the dev override (GRID_MLX_OMARCHY_DEV) ───────────────────────────────────────────────────


def test_the_dev_override_is_off_by_default(monkeypatch):
    _generic_aarch64_linux(monkeypatch)
    monkeypatch.delenv(mlx_omarchy._DEV_ENV, raising=False)
    with pytest.raises(SystemExit, match="Apple Silicon Mac booted into Linux"):
        mlx_omarchy.require_supported_machine()


def test_the_dev_override_accepts_any_aarch64_linux_box(monkeypatch, capsys):
    _generic_aarch64_linux(monkeypatch)
    monkeypatch.setenv(mlx_omarchy._DEV_ENV, "1")
    chip = mlx_omarchy.require_supported_machine()
    assert chip == "development device (non-Apple GPU)"
    assert "skipping the Apple Silicon" in capsys.readouterr().out


def test_the_dev_override_does_not_touch_an_x86_box(monkeypatch):
    # It widens WHICH aarch64 Linux box, not the architecture gate.
    monkeypatch.setattr(apple_linux.platform, "system", lambda: "Linux")
    monkeypatch.setattr(apple_linux.arch, "normalized_machine", lambda: "x86_64")
    monkeypatch.setattr(apple_linux, "is_apple_silicon_linux", lambda: False)
    monkeypatch.setenv(mlx_omarchy._DEV_ENV, "1")
    with pytest.raises(SystemExit, match="Apple Silicon Mac booted into Linux"):
        mlx_omarchy.require_supported_machine()


def test_the_dev_override_still_rejects_a_non_finite_matmul(monkeypatch):
    # The override widens the HARDWARE gate; it must never widen correctness.
    monkeypatch.setenv(mlx_omarchy._DEV_ENV, "1")
    monkeypatch.setattr(mlx_omarchy.subprocess, "check_output",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("matmul produced a non-finite result")))
    with pytest.raises(AssertionError):
        mlx_omarchy._smoke_test()


def test_the_dev_override_accepts_llvmpipe_with_a_warning(monkeypatch, capsys):
    monkeypatch.setenv(mlx_omarchy._DEV_ENV, "1")
    monkeypatch.setattr(mlx_omarchy.subprocess, "check_output", lambda *a, **k: "llvmpipe (LLVM 19.1.7, 128 bits)\n")
    assert mlx_omarchy._smoke_test() == "llvmpipe (LLVM 19.1.7, 128 bits)"
    assert "mean nothing" in capsys.readouterr().out


def test_without_the_override_llvmpipe_still_refuses(monkeypatch):
    monkeypatch.delenv(mlx_omarchy._DEV_ENV, raising=False)
    monkeypatch.setattr(mlx_omarchy.subprocess, "check_output", lambda *a, **k: "llvmpipe (LLVM 19.1.7, 128 bits)\n")
    with pytest.raises(SystemExit, match="software device"):
        mlx_omarchy._smoke_test()


def test_start_refuses_when_not_installed(monkeypatch, tmp_path):
    monkeypatch.setattr(mlx_omarchy, "python_bin", lambda: tmp_path / "absent")
    with pytest.raises(SystemExit, match="grid engine install mlx-omarchy"):
        mlx_omarchy.start(REPO, port=8081)


def test_start_spawns_mlx_lm_server_on_loopback(monkeypatch, tmp_path):
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("")
    monkeypatch.setattr(mlx_omarchy, "python_bin", lambda: python)
    monkeypatch.setattr(mlx_omarchy.paths, "ensure_all", lambda: None)
    monkeypatch.setattr(mlx_omarchy.paths, "run_dir", lambda: tmp_path)
    monkeypatch.setattr(mlx_omarchy.logging_setup, "cap_and_open_append", lambda *a, **k: open(tmp_path / "l", "a"))
    spawned = []
    monkeypatch.setattr(mlx_omarchy.subprocess, "Popen", lambda cmd, **k: spawned.append(cmd) or _Proc())

    launched = mlx_omarchy.start(REPO, port=8099)

    # `-m mlx_lm server`, never `-m mlx_lm.server` — the dotted form is deprecated as of the
    # pinned mlx-lm release (verified against its own __main__.py / cli.py / server.py).
    assert spawned == [[str(python), "-m", "mlx_lm", "server", "--model", REPO, "--host", "127.0.0.1", "--port", "8099"]]
    assert launched.port == 8099 and launched.log == tmp_path / "mlx-omarchy-8099.log"


def test_wait_ready_reports_an_early_exit_with_the_log_tail(tmp_path):
    proc = _Proc()
    proc._rc = 1
    log = tmp_path / "log"
    log.write_text("Traceback\nValueError: no such repo\n")
    with pytest.raises(SystemExit, match="no such repo"):
        mlx_omarchy.wait_ready(mlx_omarchy.LlamaProcess(proc=proc, port=8081, log=log), timeout=5)


def test_wait_ready_posts_a_real_generation_not_get_models(monkeypatch, tmp_path):
    # server.py's /v1/models answers 200 the instant the HTTP server binds — it only lists what
    # is already in the local HF cache, and says nothing about whether THIS process's --model has
    # finished loading (that happens on a separate background thread). A GET here would report
    # "ready" while a multi-gigabyte download is still in flight.
    proc = _Proc()
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append((url, json))
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(mlx_omarchy.httpx, "post", fake_post)
    monkeypatch.setattr(mlx_omarchy.httpx, "get", lambda *a, **k: pytest.fail("must not GET /v1/models"))

    mlx_omarchy.wait_ready(mlx_omarchy.LlamaProcess(proc=proc, port=8099, log=tmp_path / "l"), timeout=5)

    assert len(calls) == 1
    url, body = calls[0]
    assert url == "http://127.0.0.1:8099/v1/chat/completions"
    assert body["messages"] and body["max_tokens"] == 1


def test_wait_ready_retries_while_the_model_is_still_loading(monkeypatch, tmp_path):
    # A non-200 (or a connection error, before the port is even bound) must be retried, not
    # treated as failure — the background load can legitimately take minutes on a big model.
    proc = _Proc()
    attempts = {"n": 0}

    def flaky_post(url, json=None, timeout=None):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise mlx_omarchy.httpx.RequestError("connection refused")
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(mlx_omarchy.httpx, "post", flaky_post)
    monkeypatch.setattr(mlx_omarchy.time, "sleep", lambda s: None)

    mlx_omarchy.wait_ready(mlx_omarchy.LlamaProcess(proc=proc, port=8099, log=tmp_path / "l"), timeout=5)
    assert attempts["n"] == 3


# ── grid engine install / grid catalog ─────────────────────────────────────────────────────────


def test_engine_install_mlx_omarchy_runs_the_installer(monkeypatch, capsys):
    asked = []
    monkeypatch.setattr(mlx_omarchy, "install", lambda version=None: asked.append(version) or "Apple M1 Pro (G13S B1)")
    monkeypatch.setattr(mlx_omarchy, "installed_version", lambda: "v0.5.0")
    rc = engine_cmd.cmd_engine_install(argparse.Namespace(
        name="mlx-omarchy", target_sm=None, from_source=False, engine_version="v0.4.2"))
    assert rc == 0
    assert asked == ["v0.4.2"]
    out = capsys.readouterr().out
    assert "Apple M1 Pro" in out and "v0.5.0" in out and "--engine mlx-omarchy" in out


def test_version_is_refused_for_the_pinned_engines():
    with pytest.raises(SystemExit, match="mlx-omarchy` only"):
        engine_cmd.cmd_engine_install(argparse.Namespace(
            name="llama.cpp", target_sm=None, from_source=False, engine_version="b1"))


def test_the_catalog_offers_nothing_off_omarchy_m(monkeypatch):
    _not_that_machine(monkeypatch)
    assert catalog.mlx_entries() == ()


def test_the_catalog_offers_mlx_models_on_omarchy_m(monkeypatch, tmp_path):
    # A fresh monkeypatch, not the same one _not_that_machine used above: that helper stubs
    # `is_apple_silicon_linux` directly, and a later `_omarchy_m` in the SAME test would only
    # patch the device-tree files it reads — never undoing the direct stub — so the two cases
    # need their own monkeypatch fixture instance, exactly as pytest hands out per test function.
    _omarchy_m(monkeypatch, tmp_path)
    assert catalog.mlx_entries() == catalog.MLX_CATALOG
    assert all(entry.hf_repo.startswith("mlx-community/") for entry in catalog.MLX_CATALOG)
