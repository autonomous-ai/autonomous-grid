"""How a node names itself on the grid page.

The rule the page depends on: Apple Silicon is named by its CHIP, everything else by its CARD. The
tests below drive each branch with the probes stubbed, because the real answer is whatever machine
happens to run the suite — and the branch that matters most (Apple Silicon) is one no CI box has.

The blank cases are as load-bearing as the named ones. The relay MERGES this over what it already
holds, so a field that comes back empty must be dropped rather than sent: sending it blank would
erase a name a previous run got right, on a page where nobody could see why.
"""

from __future__ import annotations

import pytest

from shared.system import node_hardware


@pytest.fixture(autouse=True)
def _clear_cache():
    """The module probes once and caches. Every test drives a different machine, so reset around
    each — including after, so a stubbed answer never leaks into the next test or the real one."""
    node_hardware._cached = None
    yield
    node_hardware._cached = None


def _stub(
    monkeypatch, *, system="Linux", apple_silicon=False, chip=("", ""), gpus=(),
    profiler="", cpu="",
):
    """Drive one machine. `system` matters as much as the probes: the Intel-Mac branch is chosen by
    the OS, so a test that leaves it alone silently exercises whatever box runs the suite."""
    monkeypatch.setattr(node_hardware.platform, "system", lambda: system)
    monkeypatch.setattr(node_hardware, "_is_apple_silicon", lambda: apple_silicon)
    monkeypatch.setattr(node_hardware.apple, "describe_chip", lambda: chip)
    monkeypatch.setattr(node_hardware.gpu, "enumerate_gpus", lambda *a, **k: list(gpus))
    monkeypatch.setattr(node_hardware.gpu, "_sysctl_memsize_mb", lambda *a, **k: 196608.0)
    monkeypatch.setattr(node_hardware.gpu, "_macos_profiler_vram_mb", lambda *a, **k: 4096.0)
    monkeypatch.setattr(node_hardware.host, "cpu_brand", lambda: cpu)
    monkeypatch.setattr(
        node_hardware.subprocess, "check_output", lambda *a, **k: profiler.encode()
    )


class _Card:
    def __init__(self, name, memory_total_mb=24576.0):
        self.name = name
        self.memory_total_mb = memory_total_mb


def test_apple_silicon_is_named_by_its_chip(monkeypatch):
    # The GPU is part of the SoC and has no name of its own — "Apple GPU" tells a reader nothing,
    # while "Apple M4 Pro" is what they would say out loud about the machine.
    _stub(monkeypatch, apple_silicon=True, chip=("Mac Studio", "Apple M4 Pro"))
    got = node_hardware.describe()

    assert got["chip"] == "Apple M4 Pro"
    assert got["device"] == "Mac Studio"
    assert got["memory_gb"] == 192


def test_a_gpu_box_is_named_by_its_card(monkeypatch):
    # The card decides what the box can run; its CPU brand is noise beside it.
    _stub(monkeypatch, gpus=[_Card("NVIDIA GeForce RTX 4090")], cpu="AMD EPYC 7402P")
    got = node_hardware.describe()

    assert got["device"] == "NVIDIA GeForce RTX 4090"
    assert got["chip"] is None
    assert got["memory_gb"] == 24


def test_several_identical_cards_read_as_one_name_with_a_count(monkeypatch):
    # The name is a label, not an inventory — the memory figures the page shows come from telemetry,
    # which counts them properly.
    _stub(monkeypatch, gpus=[_Card("NVIDIA GeForce RTX 4090"), _Card("NVIDIA GeForce RTX 4090")])
    assert node_hardware.describe()["device"] == "NVIDIA GeForce RTX 4090 ×2"


def test_an_intel_mac_reports_its_discrete_card_over_the_integrated_one(monkeypatch):
    # `enumerate_gpus` only knows nvidia-smi and no Intel Mac has an NVIDIA card, so this branch is
    # the only thing standing between a MacBook Pro and advertising itself as "i386". The integrated
    # chipset on a Mac is always the Intel-branded one; the discrete card is what matters.
    _stub(
        monkeypatch,
        system="Darwin",
        profiler=(
            "Graphics/Displays:\n"
            "      Chipset Model: Intel UHD Graphics 630\n"
            "      Chipset Model: Radeon Pro 560X\n"
        ),
    )
    got = node_hardware.describe()

    assert got["device"] == "Radeon Pro 560X"
    assert got["memory_gb"] == 4


def test_an_intel_mac_with_only_integrated_graphics_is_still_named(monkeypatch):
    _stub(monkeypatch, system="Darwin", profiler="      Chipset Model: Intel Iris Plus Graphics\n")
    assert node_hardware.describe()["device"] == "Intel Iris Plus Graphics"


def test_a_cpu_only_box_reports_its_processor_not_its_architecture(monkeypatch):
    # `platform.processor()` answers "i386" on macOS and "x86_64" on Linux — the architecture, not
    # the processor, and useless on a page whose job is to say what the machine is.
    _stub(monkeypatch, cpu="Intel(R) Xeon(R) Gold 6248")
    got = node_hardware.describe()

    assert got["device"] == "Intel(R) Xeon(R) Gold 6248"
    assert got["device_class"] == "server"


def test_empty_fields_never_go_on_the_wire(monkeypatch):
    # The relay merges this over what it holds, so a blank would erase a good name rather than
    # leave it — a regression nobody could see from either side.
    _stub(monkeypatch, apple_silicon=True, chip=("", ""))
    monkeypatch.setattr(node_hardware.gpu, "_sysctl_memsize_mb", lambda *a, **k: 0.0)

    assert node_hardware.describe() == {
        "device": "",
        "chip": None,
        "memory_gb": None,
        "device_class": "gpu",
    }
    assert node_hardware.meta_fields() == {"device_class": "gpu"}


def test_a_probe_that_throws_leaves_the_node_serving(monkeypatch):
    # An unnamed machine serves models perfectly well; taking the node down over a cosmetic field
    # would trade a blank line for an outage.
    monkeypatch.setattr(node_hardware, "_probe", lambda: (_ for _ in ()).throw(OSError("boom")))

    assert node_hardware.describe() == {}
    assert node_hardware.meta_fields() == {}


def test_the_probe_runs_once_however_often_it_is_read(monkeypatch):
    # Read on every heartbeat, and `system_profiler` takes seconds to answer.
    calls = []
    monkeypatch.setattr(
        node_hardware, "_probe", lambda: calls.append(1) or {"device": "RTX 4090"}
    )
    for _ in range(5):
        node_hardware.describe()

    assert len(calls) == 1


def test_a_caller_cannot_mutate_the_cache(monkeypatch):
    # Both call sites merge this into a meta dict; a shared reference would let one of them change
    # what every later heartbeat reports.
    _stub(monkeypatch, gpus=[_Card("NVIDIA GeForce RTX 4090")])
    node_hardware.describe()["device"] = "tampered"

    assert node_hardware.describe()["device"] == "NVIDIA GeForce RTX 4090"
