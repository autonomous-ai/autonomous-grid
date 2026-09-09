"""Apple silicon under Linux (Asahi / omarchy-mac): the hardware is recognised, the backends are not.

The bug these tests exist for was a real node: an M2 under Asahi + Arch + Hyprland joined the grid,
`platform.system()` said "Linux", every Darwin-conjoined hardware predicate went false, and the
machine appeared as an anonymous "server" with no chip, no memory, and 16 GB of unified RAM labelled
VRAM. The fix splits two meanings that had been written as one condition:

- what the machine **IS** (chip name, unified pool, RAM-vs-VRAM label) follows the silicon onto Linux;
- what the OS can **RUN** (Metal, MLX, MPS) keeps requiring Darwin, because a backend the loader
  cannot create is worse than no backend.

Nothing here touches a real device tree: the devicetree is injected, and `/proc/meminfo` is stubbed.
The upstream grounding is `arch/arm64/boot/dts/apple/*.dts` in torvalds/linux — e.g. `t8103-j293.dts`
carries `compatible = "apple,j293", "apple,t8103", "apple,arm-platform"` and
`model = "Apple MacBook Pro (13-inch, M1, 2020)"`.
"""
from __future__ import annotations

import pytest

from shared.media import media_gating
from shared.system import apple, arch, asahi, device, gpu, host, node_hardware

# An M2 Pro MacBook Pro, verbatim in the shapes the devicetree really uses: NUL-separated
# `compatible`, NUL-terminated `model`.
_DT_M2_PRO = {
    "compatible": b"apple,j414s\x00apple,t6020\x00apple,arm-platform\x00",
    "model": b"Apple MacBook Pro (14-inch, M2 Pro, 2023)\x00",
}


@pytest.fixture(autouse=True)
def _clear_probes():
    """Both probes memoise for the life of the process (they sit on the heartbeat path). Every test
    drives a different machine, so reset around each — including after, so a stubbed Apple tree never
    leaks into the next test or onto a real box."""
    asahi._probed = None
    node_hardware._cached = None
    gpu._MAC_VRAM_MB = None
    yield
    asahi._probed = None
    node_hardware._cached = None
    gpu._MAC_VRAM_MB = None


def _asahi(monkeypatch, tree=None):
    """Become an Asahi box: Linux on aarch64, Apple devicetree injected.

    `_DT_PATHS` is not repointed at a temp directory because the real read is a plain `open()` on an
    absolute path per property — injecting `_read_property` keeps the parse (`_probe`) and every
    consumer under test while skipping the kernel's filesystem."""
    monkeypatch.setattr(host.platform, "system", lambda: "Linux")
    monkeypatch.setattr(asahi.platform, "system", lambda: "Linux")
    monkeypatch.setattr(arch, "normalized_machine", lambda: "aarch64")
    tree = _DT_M2_PRO if tree is None else tree
    monkeypatch.setattr(asahi, "_read_property", lambda name: tree.get(name, b""))


# ── the devicetree reading itself ──────────────────────────────────────────────

def test_the_devicetree_names_the_soc_and_the_product(monkeypatch):
    _asahi(monkeypatch)

    assert asahi.info() == {
        "soc": "t6020",
        "board": "j414s",
        "model": "Apple MacBook Pro (14-inch, M2 Pro, 2023)",
    }
    assert asahi.is_apple_linux() is True
    assert asahi.chip_name() == "Apple M2 Pro"


def test_the_soc_is_found_wherever_it_sits_in_compatible(monkeypatch):
    # Upstream orders the list board-first, but the SoC entry is the one on every machine of a
    # generation, so a vendor tree that reorders the rest must still read.
    _asahi(monkeypatch, tree={
        "compatible": b"apple,arm-platform\x00apple,t8103\x00apple,j293\x00",
        "model": b"Apple MacBook Pro (13-inch, M1, 2020)\x00",
    })

    assert asahi.chip_name() == "Apple M1"
    assert asahi.board_identifier() == "j293"


def test_a_soc_newer_than_the_table_reads_as_blank(monkeypatch):
    # An unpublished chip is a blank line; a guessed one routes work to the wrong machine.
    _asahi(monkeypatch, tree={"compatible": b"apple,j9999\x00apple,t9999\x00", "model": b"X\x00"})

    assert asahi.is_apple_linux() is True   # the hardware is Apple regardless of the table
    assert asahi.chip_name() == ""


def test_an_ordinary_linux_box_is_not_apple(monkeypatch):
    # Aarch64 Linux without an Apple tree (a Pi, an ARM cloud VM) must answer False on every
    # question, or a Raspberry Pi starts advertising unified memory it does not have.
    monkeypatch.setattr(asahi.platform, "system", lambda: "Linux")
    monkeypatch.setattr(arch, "normalized_machine", lambda: "aarch64")
    monkeypatch.setattr(asahi, "_read_property", lambda name: b"")

    assert asahi.is_apple_linux() is False
    assert asahi.chip_name() == ""


def test_an_x86_linux_box_never_opens_the_devicetree(monkeypatch):
    monkeypatch.setattr(asahi.platform, "system", lambda: "Linux")
    monkeypatch.setattr(arch, "normalized_machine", lambda: "x86_64")

    def _boom(name):
        raise AssertionError("the devicetree must not be read on x86")

    monkeypatch.setattr(asahi, "_read_property", _boom)
    assert asahi.is_apple_linux() is False


def test_a_mac_is_not_apple_linux(monkeypatch):
    # `is_apple_linux` feeds hardware predicates that macOS answers through other tools; claiming it
    # on macOS would route the naming path to a devicetree that is not there.
    monkeypatch.setattr(asahi.platform, "system", lambda: "Darwin")

    def _boom(name):
        raise AssertionError("macOS must not read the Linux devicetree")

    monkeypatch.setattr(asahi, "_read_property", _boom)
    assert asahi.is_apple_linux() is False


# ── the node's identity on the grid page ───────────────────────────────────────

def test_an_asahi_node_is_named_by_its_chip(monkeypatch):
    """The regression: this box used to reach `_cpu_only()` and report `device_class: "server"`,
    `chip: None`, `memory_gb: None` — an M2 shown as an unnamed server."""
    _asahi(monkeypatch)
    monkeypatch.setattr(host, "_meminfo", lambda: (16 * 1024 ** 3, 9 * 1024 ** 3))

    got = node_hardware.describe()

    assert got["chip"] == "Apple M2 Pro"
    assert got["device"] == "Apple MacBook Pro (14-inch, M2 Pro, 2023)"
    assert got["memory_gb"] == 16
    assert got["memory_kind"] == "unified"


def test_the_cpu_brand_is_the_chip_not_the_architecture(monkeypatch):
    # An ARM `/proc/cpuinfo` has no `model name` line, so without the devicetree reading the brand
    # came back as "aarch64" — the architecture in a field meant to name the machine.
    _asahi(monkeypatch)

    assert host.cpu_brand() == "Apple M2 Pro"


def test_apple_describe_chip_works_without_system_profiler(monkeypatch):
    """`system_profiler` does not exist on Linux; the devicetree carries both readings."""
    _asahi(monkeypatch)
    monkeypatch.setattr(apple, "_run", lambda *a, **k: "")

    assert apple.describe_chip() == (
        "Apple MacBook Pro (14-inch, M2 Pro, 2023)", "Apple M2 Pro",
    )


# ── the unified pool: one question, two OS answers ─────────────────────────────

def test_unified_memory_reads_mectotal_under_linux(monkeypatch):
    _asahi(monkeypatch)
    monkeypatch.setattr(host, "_meminfo", lambda: (32 * 1024 ** 3, 30 * 1024 ** 3))

    assert host.unified_memory_mb() == 32 * 1024


def test_a_linux_box_with_discrete_memory_advertises_no_shared_pool(monkeypatch):
    """The guard rail on the previous test. `load_snapshot` folds this figure into ONE advertised bar,
    so an x86 box with a real card must get 0.0 — otherwise its 512 GB of system RAM is offered to the
    grid as VRAM the GPU cannot touch."""
    monkeypatch.setattr(host.platform, "system", lambda: "Linux")
    monkeypatch.setattr(asahi.platform, "system", lambda: "Linux")
    monkeypatch.setattr(arch, "normalized_machine", lambda: "x86_64")

    assert host.unified_memory_mb() == 0.0
    assert gpu._integrated_pool_mb() == 0.0


def test_a_mac_still_answers_from_hwmemsize(monkeypatch):
    monkeypatch.setattr(host.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(gpu.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(arch, "native_machine", lambda: "arm64")
    monkeypatch.setattr(host, "_sysctl", lambda name: "137438953472")  # 128 GiB

    assert host.unified_memory_mb() == 131072.0
    assert gpu._integrated_pool_mb() == 131072.0


def test_an_asahi_node_advertises_the_pool_it_shares(monkeypatch):
    """`memory_total_mb` is what the grid page renders as the memory bar, so an Asahi node has to
    populate it — from the same `MemTotal` reading `memory_gb` uses, or the two figures disagree."""
    _asahi(monkeypatch)
    monkeypatch.setattr(gpu.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gpu, "enumerate_gpus", lambda *a, **k: [])
    monkeypatch.setattr(host, "_meminfo", lambda: (16 * 1024 ** 3, 8 * 1024 ** 3))

    assert gpu.load_snapshot()["memory_total_mb"] == 16 * 1024


def test_occupancy_pairs_with_the_pool_and_never_invents_a_zero(monkeypatch):
    """`used` and `total` are divided and drawn as one bar. With no reading available the key must be
    absent, not 0.0 — a fabricated zero renders a busy machine as an idle one holding all its RAM."""
    _asahi(monkeypatch)
    import sys

    monkeypatch.setattr(gpu.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gpu, "enumerate_gpus", lambda *a, **k: [])
    monkeypatch.setattr(host.platform, "system", lambda: "Linux")
    monkeypatch.setattr(host, "_meminfo", lambda: (16 * 1024 ** 3, 4 * 1024 ** 3))
    monkeypatch.setitem(sys.modules, "psutil", None)  # psutil is optional and often absent

    snap = gpu.load_snapshot()
    assert snap["memory_used_mb"] == 12 * 1024  # MemTotal - MemAvailable, the kernel's own figure

    monkeypatch.setattr(host, "_meminfo", lambda: None)
    assert "memory_used_mb" not in gpu.load_snapshot()


def test_no_mac_only_gauges_leak_onto_linux(monkeypatch):
    """IORegistry utilisation, temperature and power have no Linux equivalent. A CPU temperature
    borrowed for the `gpu_temp_c` gauge would be a confidently wrong number, so those keys stay off."""
    _asahi(monkeypatch)
    monkeypatch.setattr(gpu.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gpu, "enumerate_gpus", lambda *a, **k: [])
    monkeypatch.setattr(host, "_meminfo", lambda: (16 * 1024 ** 3, 4 * 1024 ** 3))

    snap = gpu.load_snapshot()

    assert not {"gpu_util", "gpu_temp_c", "gpu_power_w"} & set(snap)


def test_a_mac_keeps_its_registry_gauges(monkeypatch):
    """The other direction: the Linux half must not have cost macOS anything it already read."""
    _asahi(monkeypatch)
    monkeypatch.setattr(gpu.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(gpu, "enumerate_gpus", lambda *a, **k: [])
    monkeypatch.setattr(host.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(arch, "native_machine", lambda: "arm64")
    monkeypatch.setattr(host, "_sysctl", lambda name: "34359738368")  # 32 GiB
    monkeypatch.setattr(host, "memory_used_mb", lambda: 12_000.0)
    monkeypatch.setattr(
        apple, "_run",
        lambda *a, **k: '    "PerformanceStatistics" = {"Device Utilization %"=31,'
                        '"Temperature(C)"=54,"Total Power(W)"=9}\n',
    )

    snap = gpu.load_snapshot()

    assert snap["memory_total_mb"] == 32 * 1024
    assert snap["memory_used_mb"] == 12_000.0
    assert snap["gpu_util"] == 31.0 and snap["gpu_temp_c"] == 54.0 and snap["gpu_power_w"] == 9.0


# ── the budget: the pool is real, the backend is not ───────────────────────────

def test_an_asahi_budget_is_the_pool_minus_a_reserve(monkeypatch):
    """Unified memory is also system RAM: without the reserve, recommendations sized to the whole
    machine swap the OS — the same failure the macOS branch already guards."""
    _asahi(monkeypatch)
    monkeypatch.setattr(gpu, "enumerate_gpus", lambda **k: [])
    monkeypatch.setattr(gpu, "load_snapshot", lambda **k: {"memory_total_mb": 16.0 * 1024})

    budget = device.resolve_budget()

    assert budget.source == "vram"
    assert "usable of 16 GB unified memory" in budget.detected
    assert budget.total_bytes < 14 * 1024 ** 3


# ── what the OS still cannot run ───────────────────────────────────────────────

def test_the_backend_selectors_stay_macos_only(monkeypatch):
    """The half of the split that must NOT move. Metal, MPS and MLX do not exist on Linux for Apple
    silicon, so every predicate that picks an inference/training backend keeps asking for Darwin.
    Widening them to the hardware would advertise a backend the loader cannot create — a node that
    accepts work it is guaranteed to fail, which is worse than the anonymous node this change fixes.

    The media gate is included on purpose: it is the one predicate whose *memory* reading is already
    unified, so it is the easiest one to widen by mistake."""
    from shared.engine import comfyui, launcher

    _asahi(monkeypatch)
    monkeypatch.setattr(launcher.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(media_gating.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(comfyui.platform, "machine", lambda: "arm64")

    assert launcher.is_apple_silicon() is False
    assert media_gating.is_apple_silicon() is False
    assert comfyui._is_apple_silicon() is False
    assert device._is_apple_silicon() is False


def test_the_mac_media_gate_still_reads_unified_memory(monkeypatch):
    monkeypatch.setattr(media_gating.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(media_gating.platform, "machine", lambda: "arm64")

    assert media_gating.is_apple_silicon() is True


def test_an_asahi_budget_never_claims_metal(monkeypatch):
    """The line the whole split protects. There is no Metal/MPS llama.cpp build for Linux on Apple
    silicon, and `device_info.consistency_ok` derives device_class from the backend, so `metal` here
    would both advertise an unloadable graph and desync the inventory invariant."""
    _asahi(monkeypatch)
    monkeypatch.setattr(gpu, "enumerate_gpus", lambda **k: [])
    monkeypatch.setattr(gpu, "load_snapshot", lambda **k: {"memory_total_mb": 16.0 * 1024})

    budget = device.resolve_budget()

    assert budget.backend == "cpu"
    assert budget.is_cuda is False
    assert device._is_apple_silicon() is False


def test_a_mac_still_gets_metal(monkeypatch):
    monkeypatch.setattr(host.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(gpu, "enumerate_gpus", lambda **k: [])
    monkeypatch.setattr(gpu, "load_snapshot", lambda **k: {"memory_total_mb": 36.0 * 1024})
    monkeypatch.setattr(device.arch, "native_machine", lambda: "arm64")

    assert device.resolve_budget().backend == "metal"


# ── the label the reader sees ──────────────────────────────────────────────────

def test_the_relay_carries_the_node_s_word_for_its_memory():
    """`platform` answers "which binaries run here" (ADR 0039 D-c), so an Asahi node says "linux"
    truthfully. Inferring the memory label from it is what produced a RAM bar titled VRAM."""
    from cli import remote_stats

    asahi_node = {"platform": "linux", "memory_kind": "unified", "vram_gb": 16.0}
    mac_node = {"platform": "macos-arm64", "vram_gb": 192.0}  # provider too old to send the field
    gpu_box = {"platform": "linux", "vram_gb": 24.0}

    assert remote_stats.node_memory_kind(asahi_node) == "RAM"
    assert remote_stats.node_memory_kind(mac_node) == "RAM"
    assert remote_stats.node_memory_kind(gpu_box) == "VRAM"


def test_an_asahi_chip_still_gets_its_measured_bandwidth():
    """device_class is `cpu` on Asahi, which used to route bandwidth to the Intel-MacBook fragment
    table and miss. The rate belongs to the silicon, not the driver — and the Apple lookup is gated
    on the brand being an Apple chip string, so "Intel Core m3-7Y32" stays a 17 GB/s laptop."""
    from shared.system import bandwidth

    assert bandwidth.estimate("cpu", "Apple M2 Pro", []) == 200
    assert bandwidth.estimate("cpu", "Intel(R) Core(TM) m3-7Y32", []) is None
