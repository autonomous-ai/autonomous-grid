"""Best-effort accelerator topology and transfer-capacity discovery.

The wire data is deliberately vendor-neutral. NVIDIA discovery uses only shipped driver tools and
Linux sysfs; unsupported or malformed probes return partial/empty topology rather than inventing a
fast link. Unknown topology is therefore safe for profiles that require explicit guarantees.
"""

from __future__ import annotations

import math
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from statistics import median
from typing import Any


_GPU_ROW = re.compile(r"^GPU(\d+)\s+")
_NVLINK = re.compile(r"^NV(\d+)$")
_GPU_LINE = re.compile(r"^GPU\s+(\d+):.*\(UUID:\s*(GPU-[^)]+)\)")
_MIG_LINE = re.compile(
    r"^\s*MIG\s+([^\s]+).*\(UUID:\s*(MIG-[^)]+)\)", re.IGNORECASE
)
_MIG_MEMORY = re.compile(r"(\d+(?:\.\d+)?)gb", re.IGNORECASE)
_NVLINK_RATE = re.compile(r"Link\s+\d+:\s*([0-9.]+)\s*GB/s", re.IGNORECASE)
_NVLINK_GPU = re.compile(r"GPU\s+(\d+):")
_PCIE_GBPS_PER_LANE = {1: 0.25, 2: 0.50, 3: 0.985, 4: 1.969, 5: 3.938, 6: 7.563}
_PCIE_FACTOR = {"PIX": 1.0, "PXB": 0.85, "PHB": 0.75, "NODE": 0.65, "SYS": 0.45}


def collect(gpus: list[dict[str, Any]], backend: str) -> dict[str, Any]:
    """Return ``gpu_devices``, ``gpu_links``, and host transfer capacity."""

    transfer = network_capacity_mbps()
    if backend == "metal":
        devices = []
        if gpus and float(gpus[0].get("memory_total_mb") or 0) > 0:
            devices.append(
                {
                    "device_id": "metal:0",
                    "index": 0,
                    "memory_mb": int(float(gpus[0]["memory_total_mb"])),
                    "numa_node": 0,
                    "pci_bus_id": "",
                    "mig_parent_id": "",
                    "mig_profile": "",
                }
            )
        return {
            "gpu_devices": devices,
            "gpu_links": [],
            "transfer_bandwidth_mbps": transfer,
        }
    if backend != "cuda":
        return {
            "gpu_devices": [],
            "gpu_links": [],
            "transfer_bandwidth_mbps": transfer,
        }
    inventory = _nvidia_inventory(gpus)
    mig = _mig_instances()
    devices: list[dict[str, Any]] = []
    physical_ids: dict[int, str] = {}
    pcie_capacity: dict[int, float] = {}
    for item in inventory:
        index = int(item["index"])
        parent = str(item["uuid"])
        physical_ids[index] = parent
        pcie_capacity[index] = _pcie_capacity_gbps(item["pcie_gen"], item["pcie_width"])
        instances = mig.get(parent, ()) if item["mig_enabled"] else ()
        if item["mig_enabled"]:
            for profile, instance_id, memory_mb in instances:
                devices.append(
                    {
                        "device_id": instance_id,
                        "index": index,
                        "memory_mb": memory_mb,
                        "numa_node": item["numa_node"],
                        "pci_bus_id": item["pci_bus_id"],
                        "mig_parent_id": parent,
                        "mig_profile": profile,
                    }
                )
            continue
        devices.append(
            {
                "device_id": parent,
                "index": index,
                "memory_mb": item["memory_mb"],
                "numa_node": item["numa_node"],
                "pci_bus_id": item["pci_bus_id"],
                "mig_parent_id": "",
                "mig_profile": "",
            }
        )
    schedulable_physical = {
        int(item["index"]): str(item["device_id"])
        for item in devices
        if not item["mig_parent_id"]
    }
    links = _nvidia_links(schedulable_physical, pcie_capacity)
    return {
        "gpu_devices": devices,
        "gpu_links": links,
        "transfer_bandwidth_mbps": transfer,
    }


def network_capacity_mbps(root: Path = Path("/sys/class/net")) -> float:
    """Fastest live NIC line rate; zero means unknown and disables transfer estimation."""

    rates: list[float] = []
    try:
        interfaces = tuple(root.iterdir())
    except OSError:
        return 0.0
    for interface in interfaces:
        if interface.name == "lo":
            continue
        try:
            state = (interface / "operstate").read_text().strip()
            speed = float((interface / "speed").read_text().strip())
        except (OSError, ValueError):
            continue
        if state == "up" and math.isfinite(speed) and speed > 0:
            rates.append(speed)
    return max(rates, default=0.0)


def _run(command: list[str], timeout: float = 5.0) -> str:
    try:
        return subprocess.check_output(
            command, timeout=timeout, stderr=subprocess.DEVNULL, text=True
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _nvidia_inventory(gpus: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # PCIe generation downshifts while an idle GPU is in a low-power state. Using the current
    # generation made a Gen4 x16 host look like Gen1 during scheduling. Width remains the negotiated
    # physical lane constraint, so combine maximum generation with current width.
    fields = "index,uuid,pci.bus_id,mig.mode.current,pcie.link.gen.max,pcie.link.width.current"
    output = _run(["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"])
    by_index = {int(item.get("index") or 0): item for item in gpus if isinstance(item, Mapping)}
    inventory: list[dict[str, Any]] = []
    for line in output.splitlines():
        parts = [item.strip() for item in line.split(",")]
        if len(parts) != 6:
            continue
        try:
            index = int(parts[0])
            fallback = by_index.get(index, {})
            memory_mb = int(float(fallback.get("memory_total_mb") or 0))
            if memory_mb <= 0:
                continue
            bus_id = parts[2].lower()
            inventory.append(
                {
                    "index": index,
                    "uuid": parts[1],
                    "pci_bus_id": bus_id,
                    "mig_enabled": parts[3].lower().startswith("enabled"),
                    "pcie_gen": int(parts[4]) if parts[4].isdigit() else 0,
                    "pcie_width": int(parts[5]) if parts[5].isdigit() else 0,
                    "memory_mb": memory_mb,
                    "numa_node": _numa_node(bus_id),
                }
            )
        except (TypeError, ValueError, OverflowError):
            continue
    if inventory:
        return inventory
    # Older drivers may reject one query field. Preserve device truth but leave topology unknown.
    return [
        {
            "index": int(item.get("index") or 0),
            "uuid": f"cuda:{int(item.get('index') or 0)}",
            "pci_bus_id": "",
            "mig_enabled": False,
            "pcie_gen": 0,
            "pcie_width": 0,
            "memory_mb": int(float(item.get("memory_total_mb") or 0)),
            "numa_node": -1,
        }
        for item in gpus
        if isinstance(item, Mapping) and float(item.get("memory_total_mb") or 0) > 0
    ]


def _numa_node(bus_id: str) -> int:
    path = Path("/sys/bus/pci/devices") / bus_id / "numa_node"
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return -1


def _mig_instances() -> dict[str, tuple[tuple[str, str, int], ...]]:
    current_parent = ""
    found: dict[str, list[tuple[str, str, int]]] = {}
    for line in _run(["nvidia-smi", "-L"]).splitlines():
        if match := _GPU_LINE.match(line):
            current_parent = match.group(2)
            found.setdefault(current_parent, [])
            continue
        match = _MIG_LINE.match(line)
        if not match or not current_parent:
            continue
        profile, instance_id = match.groups()
        memory_match = _MIG_MEMORY.search(profile)
        if memory_match:
            memory_mb = max(1, round(float(memory_match.group(1)) * 1024))
            found[current_parent].append((profile, instance_id, memory_mb))
    return {key: tuple(values) for key, values in found.items()}


def _nvlink_rates() -> dict[int, float]:
    rates: dict[int, list[float]] = {}
    current = -1
    for line in _run(["nvidia-smi", "nvlink", "--status"]).splitlines():
        if match := _NVLINK_GPU.search(line):
            current = int(match.group(1))
            continue
        if current >= 0 and (match := _NVLINK_RATE.search(line)):
            rates.setdefault(current, []).append(float(match.group(1)))
    return {index: median(values) for index, values in rates.items() if values}


def _nvidia_links(
    devices: Mapping[int, str], pcie_capacity: Mapping[int, float]
) -> list[dict[str, Any]]:
    output = _run(["nvidia-smi", "topo", "-m"])
    rates = _nvlink_rates()
    links: list[dict[str, Any]] = []
    for line in output.splitlines():
        match = _GPU_ROW.match(line.strip())
        if not match:
            continue
        source = int(match.group(1))
        cells = line.split()
        if source not in devices:
            continue
        for target in sorted(devices):
            if target <= source or target + 1 >= len(cells):
                continue
            code = cells[target + 1].upper()
            if code in ("X", "N/A"):
                continue
            kind = "pcie"
            bandwidth = min(pcie_capacity.get(source, 0.0), pcie_capacity.get(target, 0.0))
            if nvlink := _NVLINK.match(code):
                kind = "nvlink"
                per_link = min(rates.get(source, 0.0), rates.get(target, 0.0))
                bandwidth = per_link * int(nvlink.group(1))
            else:
                bandwidth *= _PCIE_FACTOR.get(code, 0.0)
            links.append(
                {
                    "device_a": devices[source],
                    "device_b": devices[target],
                    "kind": kind,
                    "bandwidth_gbps": round(max(0.0, bandwidth), 3),
                }
            )
    return links


def _pcie_capacity_gbps(generation: int, width: int) -> float:
    return _PCIE_GBPS_PER_LANE.get(generation, 0.0) * max(0, width)
