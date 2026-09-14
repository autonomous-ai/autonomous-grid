"""An M-series Mac booted into Linux (Omarchy M) is an Apple Silicon machine, not an "aarch64" box.

Every test here drives the machine through the files Linux actually exposes on that hardware — the
device tree and the Vulkan ICD directory — redirected to a temp dir, because no CI box is one and the
machine running the suite is whatever it is. The probe is the single source for three decisions
(`grid engine install`, the node's name on the grid page, `grid catalog`), and each is pinned to it.

The negatives carry as much weight as the positives: the machine most people meet Omarchy on is
Try Omarchy, a QEMU VM whose device tree says `linux,dummy-virt` — and inside it the Apple GPU does
not exist, so claiming Apple Silicon there would install a Vulkan engine onto software rasterisation.
"""

from __future__ import annotations

import struct
from types import SimpleNamespace

import pytest

from cli import engine as engine_cmd
from shared.models import catalog
from shared.system import apple_linux, node_hardware

ASAHI_COMPATIBLE = b"apple,j314s\x00apple,t6000\x00apple,arm-platform\x00"
QEMU_COMPATIBLE = b"linux,dummy-virt\x00"
MODEL_M1_PRO = b"Apple MacBook Pro (14-inch, M1 Pro, 2021)\x00"


def _machine(
    monkeypatch, tmp_path, *, system="Linux", machine="aarch64", compatible=ASAHI_COMPATIBLE,
    model=MODEL_M1_PRO, icd=True, loader=True, memory_gb=16,
):
    """Drive one machine: what its device tree says, whether the Vulkan driver is installed."""
    tree = tmp_path / "device-tree"
    tree.mkdir(parents=True)
    (tree / "compatible").write_bytes(compatible)
    (tree / "model").write_bytes(model)
    mem = tree / "memory@800000000"
    mem.mkdir()
    (mem / "reg").write_bytes(struct.pack(">QQ", 0x8_0000_0000, memory_gb * 1024 ** 3))
    icd_path = tmp_path / "icd.d" / "asahi_icd.aarch64.json"
    if icd:
        icd_path.parent.mkdir()
        icd_path.write_text("{}")
    monkeypatch.setattr(apple_linux.platform, "system", lambda: system)
    monkeypatch.setattr(apple_linux.arch, "normalized_machine", lambda: machine)
    monkeypatch.setattr(apple_linux, "_COMPATIBLE", tree / "compatible")
    monkeypatch.setattr(apple_linux, "_MODEL", tree / "model")
    monkeypatch.setattr(apple_linux, "_DEVICE_TREE", tree)
    monkeypatch.setattr(apple_linux, "_MEMINFO", tmp_path / "meminfo")
    monkeypatch.setattr(apple_linux, "_ASAHI_ICD", icd_path)
    monkeypatch.setattr(
        apple_linux.ctypes.util, "find_library",
        lambda name: "libvulkan.so.1" if (loader and name == "vulkan") else None,
    )


# ── the probe ──────────────────────────────────────────────────────────────────────────────────


def test_an_m_series_mac_booted_into_linux_is_recognised(monkeypatch, tmp_path):
    _machine(monkeypatch, tmp_path)
    assert apple_linux.is_apple_silicon_linux()


def test_the_try_omarchy_vm_is_not_one(monkeypatch, tmp_path):
    # QEMU's `virt` machine: aarch64 Linux on a Mac, but the Apple GPU is on the far side of the VM
    # boundary. The device tree is what tells them apart, and it must — the VM is where most people
    # first run Omarchy on a Mac.
    _machine(monkeypatch, tmp_path, compatible=QEMU_COMPATIBLE)
    assert not apple_linux.is_apple_silicon_linux()


@pytest.mark.parametrize("system, machine", [("Darwin", "aarch64"), ("Linux", "x86_64")])
def test_the_device_tree_is_never_read_off_aarch64_linux(monkeypatch, tmp_path, system, machine):
    # A Mac on macOS has no device tree; an x86 box with a stray file could not be made one.
    _machine(monkeypatch, tmp_path, system=system, machine=machine)
    assert not apple_linux.is_apple_silicon_linux()


def test_a_machine_with_no_device_tree_is_an_ordinary_linux_box(monkeypatch, tmp_path):
    _machine(monkeypatch, tmp_path)
    monkeypatch.setattr(apple_linux, "_COMPATIBLE", tmp_path / "missing")
    assert not apple_linux.is_apple_silicon_linux()


def test_vulkan_needs_both_the_driver_manifest_and_the_loader(monkeypatch, tmp_path):
    # llama.cpp's Vulkan build dlopens libvulkan.so.1 and asks it for devices: with either half
    # missing the engine does not start, so neither alone may read as "ready".
    _machine(monkeypatch, tmp_path, icd=True, loader=True)
    assert apple_linux.vulkan_ready()
    _machine(monkeypatch, tmp_path / "a", icd=False, loader=True)
    assert not apple_linux.vulkan_ready()
    _machine(monkeypatch, tmp_path / "b", icd=True, loader=False)
    assert not apple_linux.vulkan_ready()


@pytest.mark.parametrize("model, chip", [
    (b"Apple MacBook Pro (14-inch, M1 Pro, 2021)\x00", "Apple M1 Pro"),
    (b"Apple MacBook Air (M2, 2022)\x00", "Apple M2"),
    (b"Apple Mac Studio (M1 Ultra, 2022)\x00", "Apple M1 Ultra"),
    (b"Apple Mac mini (M1, 2020)\x00", "Apple M1"),
    (b"Apple MacBook Pro (16-inch, M2 Max, 2023)\x00", "Apple M2 Max"),
])
def test_the_chip_is_read_out_of_the_model_string(monkeypatch, tmp_path, model, chip):
    # The same "Apple M1 Pro" a Mac reports on macOS, so the grid page names the machine the same
    # way whichever OS it booted.
    _machine(monkeypatch, tmp_path, model=model)
    got_model, got_chip = apple_linux.describe_chip()
    assert got_chip == chip
    assert got_model == model.decode().rstrip("\x00")


def test_a_model_naming_no_chip_still_names_the_machine(monkeypatch, tmp_path):
    _machine(monkeypatch, tmp_path, model=b"Apple Neo\x00")
    assert apple_linux.describe_chip() == ("Apple Neo", "")


def test_memory_is_the_physical_size_from_the_device_tree(monkeypatch, tmp_path):
    # `/proc/meminfo` is what is left after firmware and kernel reservations — 15.5 GB on a 16 GB
    # Mac — and would round to "15 GB" beside every other Mac's "16 GB".
    _machine(monkeypatch, tmp_path, memory_gb=32)
    (tmp_path / "meminfo").write_text("MemTotal:       32410000 kB\n")
    assert apple_linux.memory_total_mb() == 32 * 1024


def test_memory_falls_back_to_meminfo_when_the_tree_has_no_memory_node(monkeypatch, tmp_path):
    _machine(monkeypatch, tmp_path)
    (tmp_path / "device-tree" / "memory@800000000" / "reg").unlink()
    (tmp_path / "meminfo").write_text("MemTotal:       16252928 kB\n")
    assert apple_linux.memory_total_mb() == pytest.approx(15872.0)


def test_a_reg_this_parser_does_not_understand_is_not_guessed_at(monkeypatch, tmp_path):
    _machine(monkeypatch, tmp_path)
    (tmp_path / "device-tree" / "memory@800000000" / "reg").write_bytes(b"\x00" * 12)
    monkeypatch.setattr(apple_linux, "_MEMINFO", tmp_path / "no-meminfo")
    assert apple_linux.memory_total_mb() == 0.0


# ── grid engine install ────────────────────────────────────────────────────────────────────────


def _install(monkeypatch, capsys):
    """Run the Linux branch of `grid engine install llama.cpp` with the download stubbed, and
    return (build kind installed, what was printed)."""
    installed = []
    from shared.engine import installer
    from shared.system import gpu

    monkeypatch.setattr(installer, "is_macos", lambda: False)
    monkeypatch.setattr(installer, "install_linux_prebuilt", lambda kind: installed.append(kind))
    monkeypatch.setattr(gpu, "enumerate_gpus", lambda *a, **k: [])
    args = SimpleNamespace(name="llama.cpp", target_sm=None, from_source=False)
    assert engine_cmd.cmd_engine_install(args) == 0
    return installed, capsys.readouterr().out


def test_an_omarchy_m_machine_gets_the_vulkan_engine(monkeypatch, tmp_path, capsys):
    # This is the whole point: without it the machine "had no GPU" and served on the CPU.
    _machine(monkeypatch, tmp_path)
    installed, out = _install(monkeypatch, capsys)

    assert installed == ["vulkan"]
    assert "Apple M1 Pro" in out and "Vulkan" in out
    assert "no GPU detected" not in out


def test_without_the_vulkan_driver_it_gets_cpu_and_the_one_command_that_fixes_it(
    monkeypatch, tmp_path, capsys,
):
    # Not the bare "no GPU detected" a box with no GPU gets — there IS one, one package away.
    _machine(monkeypatch, tmp_path, icd=False)
    installed, out = _install(monkeypatch, capsys)

    assert installed == ["cpu"]
    assert "pacman -S vulkan-asahi" in out
    assert "no GPU detected" not in out


def test_the_try_omarchy_vm_still_gets_the_cpu_engine(monkeypatch, tmp_path, capsys):
    _machine(monkeypatch, tmp_path, compatible=QEMU_COMPATIBLE)
    installed, out = _install(monkeypatch, capsys)

    assert installed == ["cpu"]
    assert "no GPU detected" in out


# ── the node's name on the grid page ───────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_node_cache():
    node_hardware._cached = None
    yield
    node_hardware._cached = None


def test_on_the_grid_page_it_is_the_same_mac_it_was_on_macos(monkeypatch, tmp_path):
    _machine(monkeypatch, tmp_path, memory_gb=32)
    monkeypatch.setattr(node_hardware.platform, "system", lambda: "Linux")
    monkeypatch.setattr(node_hardware, "_is_apple_silicon", lambda: False)

    assert node_hardware.describe() == {
        "device": "Apple MacBook Pro (14-inch, M1 Pro, 2021)",
        "chip": "Apple M1 Pro",
        "memory_gb": 32,
        "device_class": "gpu",
    }


def test_without_the_vulkan_driver_the_node_is_honestly_a_cpu_server(monkeypatch, tmp_path):
    _machine(monkeypatch, tmp_path, icd=False)
    monkeypatch.setattr(node_hardware.platform, "system", lambda: "Linux")
    monkeypatch.setattr(node_hardware, "_is_apple_silicon", lambda: False)

    got = node_hardware.describe()
    assert got["chip"] == "Apple M1 Pro"
    assert got["device_class"] == "server"


# ── grid catalog ───────────────────────────────────────────────────────────────────────────────


def test_the_catalog_narrows_to_the_apple_silicon_entries(monkeypatch, tmp_path):
    _machine(monkeypatch, tmp_path)
    monkeypatch.setattr(catalog.platform, "system", lambda: "Linux")
    monkeypatch.setattr(catalog.shutil, "which", lambda name: None)

    assert catalog.current_target() == catalog.TARGET_APPLE_SILICON


def test_the_catalog_is_unchanged_for_the_vm_and_every_other_linux_box(monkeypatch, tmp_path):
    _machine(monkeypatch, tmp_path, compatible=QEMU_COMPATIBLE)
    monkeypatch.setattr(catalog.platform, "system", lambda: "Linux")
    monkeypatch.setattr(catalog.shutil, "which", lambda name: None)

    assert catalog.current_target() is None
