from __future__ import annotations

from dataclasses import replace

import pytest

from shared.allocator.models import GpuDevice, GpuLink, ModelProfile, NodeSnapshot
from shared.allocator.planner import PlacementPlanner, PlannerPolicy
from shared.system import topology


def _device(device_id: str, index: int, *, numa: int = 0, memory: int = 24_000, mig=""):
    return GpuDevice(
        device_id=device_id,
        index=index,
        memory_mb=memory,
        numa_node=numa,
        mig_parent_id=("GPU-parent" if mig else ""),
        mig_profile=mig,
    )


def _node(node_id: str, *, devices=(), links=(), transfer=0.0, active_transfers=0):
    memories = tuple(item.memory_mb for item in devices)
    return NodeSnapshot(
        node_id=node_id,
        capacity_mb=96_000,
        backends=("cuda",),
        runtimes=("vllm",),
        gpu_count=max(2, len(devices)),
        gpu_memory_mb=memories or (48_000, 48_000),
        gpu_devices=devices,
        gpu_links=links,
        transfer_bandwidth_mbps=transfer,
        active_transfers=active_transfers,
        last_heartbeat=10,
    )


def _model(**changes):
    profile = ModelProfile(
        model_id="sharded",
        memory_mb=40_000,
        runtimes=("vllm",),
        backends=("cuda",),
        min_gpu_count=2,
        min_gpu_memory_mb=20_000,
        min_gpu_interconnect_gbps=40,
        require_single_numa_node=True,
    )
    return replace(profile, **changes)


def test_node_topology_round_trips_and_rejects_dangling_links():
    devices = (_device("GPU-a", 0), _device("GPU-b", 1))
    link = GpuLink("GPU-b", "GPU-a", "NVLINK", 50)
    node = _node("gpu", devices=devices, links=(link,), transfer=1_000)
    restored = NodeSnapshot.from_dict(node.to_dict())
    assert restored == node
    assert restored.gpu_links[0].device_a == "GPU-a"
    with pytest.raises(ValueError, match="unknown GPU"):
        _node("bad", devices=devices, links=(GpuLink("GPU-a", "missing", "pcie", 10),))


def test_planner_selects_only_an_all_peer_single_numa_shard_set():
    devices = (_device("GPU-a", 0), _device("GPU-b", 1), _device("GPU-c", 2, numa=1))
    good = _node(
        "good",
        devices=devices,
        links=(
            GpuLink("GPU-a", "GPU-b", "nvlink", 50),
            GpuLink("GPU-a", "GPU-c", "pcie", 16),
            GpuLink("GPU-b", "GPU-c", "pcie", 16),
        ),
    )
    wrong_numa = _node(
        "wrong-numa",
        devices=(_device("GPU-d", 0, numa=0), _device("GPU-e", 1, numa=1)),
        links=(GpuLink("GPU-d", "GPU-e", "nvlink", 100),),
    )
    slow_fabric = _node(
        "slow-fabric",
        devices=(_device("GPU-f", 0), _device("GPU-g", 1)),
        links=(GpuLink("GPU-f", "GPU-g", "pcie", 31.5),),
    )
    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        (wrong_numa, slow_fabric, good), (_model(),), now=10
    )
    assert plan.nodes_for("sharded") == ("good",)


def test_advanced_constraints_fail_closed_on_unknown_topology_and_mig_policy():
    unknown = _node("unknown")
    mig = _node(
        "mig",
        devices=(_device("MIG-a", 0, memory=24_000, mig="1g.24gb"),),
    )
    profile = _model(min_gpu_count=1, min_gpu_interconnect_gbps=0, allow_mig=False)
    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        (unknown, mig), (profile,), now=10
    )
    assert not plan.assignments
    assert plan.unsatisfied[0].code == "no_eligible_nodes"


def test_cold_load_ranking_accounts_for_link_rate_and_current_transfer_contention():
    profile = _model(
        min_gpu_count=0,
        min_gpu_memory_mb=0,
        min_gpu_interconnect_gbps=0,
        require_single_numa_node=False,
        artifact_sha256="a" * 64,
        artifact_source="hf://owner/repo/model",
        artifact_size_mb=8_000,
        load_seconds=1,
    )
    fast_busy = _node("fast-busy", transfer=10_000, active_transfers=2)
    medium_idle = _node("medium-idle", transfer=4_000)
    slow = _node("slow", transfer=100)
    plan = PlacementPlanner(PlannerPolicy(memory_headroom_fraction=0)).plan(
        (slow, fast_busy, medium_idle), (profile,), now=10
    )
    assert plan.nodes_for("sharded") == ("medium-idle",)
    assert "transfer-bandwidth cold-load estimate" in plan.assignments[0].reasons


def test_nvidia_probe_reports_nvlink_numa_and_pcie_capacity(monkeypatch):
    gpus = [
        {"index": 0, "memory_total_mb": 24_576},
        {"index": 1, "memory_total_mb": 24_576},
    ]

    def fake_run(command, timeout=5.0):
        del timeout
        joined = " ".join(command)
        if "--query-gpu" in joined:
            assert "pcie.link.gen.max" in joined
            assert "pcie.link.gen.current" not in joined
            return (
                "0, GPU-a, 0000:01:00.0, Disabled, 4, 16\n"
                "1, GPU-b, 0000:02:00.0, Disabled, 4, 16"
            )
        if command[-1] == "-L":
            return "GPU 0: RTX (UUID: GPU-a)\nGPU 1: RTX (UUID: GPU-b)"
        if "topo" in command:
            return "        GPU0 GPU1\nGPU0   X    NV2\nGPU1   NV2  X"
        if "nvlink" in command:
            return "GPU 0:\n Link 0: 25 GB/s\nGPU 1:\n Link 0: 25 GB/s"
        return ""

    monkeypatch.setattr(topology, "_run", fake_run)
    monkeypatch.setattr(topology, "_numa_node", lambda _bus: 0)
    monkeypatch.setattr(topology, "network_capacity_mbps", lambda: 10_000)
    result = topology.collect(gpus, "cuda")
    assert [item["device_id"] for item in result["gpu_devices"]] == ["GPU-a", "GPU-b"]
    assert result["gpu_links"] == [
        {
            "device_a": "GPU-a",
            "device_b": "GPU-b",
            "kind": "nvlink",
            "bandwidth_gbps": 50.0,
        }
    ]
    assert result["transfer_bandwidth_mbps"] == 10_000


def test_mig_enabled_gpu_advertises_instances_not_the_physical_parent(monkeypatch):
    monkeypatch.setattr(
        topology,
        "_run",
        lambda command, timeout=5.0: (
            "0, GPU-parent, 0000:01:00.0, Enabled, 4, 16"
            if "--query-gpu" in " ".join(command)
            else "GPU 0: A100 (UUID: GPU-parent)\n  MIG 1g.10gb Device 0: (UUID: MIG-child)"
            if command[-1] == "-L"
            else ""
        ),
    )
    monkeypatch.setattr(topology, "_numa_node", lambda _bus: 1)
    monkeypatch.setattr(topology, "network_capacity_mbps", lambda: 0)
    result = topology.collect([{"index": 0, "memory_total_mb": 80_000}], "cuda")
    assert result["gpu_devices"] == [
        {
            "device_id": "MIG-child",
            "index": 0,
            "memory_mb": 10_240,
            "numa_node": 1,
            "pci_bus_id": "0000:01:00.0",
            "mig_parent_id": "GPU-parent",
            "mig_profile": "1g.10gb",
        }
    ]
