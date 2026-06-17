"""NVIDIA GPU discovery using nvidia-smi."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


@dataclass
class GpuInfo:
    index: int
    name: str
    driver_version: str
    compute_cap: str
    memory_total_mb: float
    memory_used_mb: float
    utilization_pct: float

    @property
    def compute_cap_sm(self) -> str:
        return "sm_" + self.compute_cap.replace(".", "")


def nvidia_smi_available() -> bool:
    return shutil.which("nvidia-smi") is not None


def enumerate_gpus(timeout: float = 5.0) -> list[GpuInfo]:
    if not nvidia_smi_available():
        return []
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=index,name,driver_version,compute_cap,memory.total,memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            timeout=timeout,
        ).decode().strip()
    except (subprocess.SubprocessError, OSError):
        return []
    gpus: list[GpuInfo] = []
    for line in out.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 7:
            continue
        try:
            gpus.append(
                GpuInfo(
                    index=int(parts[0]),
                    name=parts[1],
                    driver_version=parts[2],
                    compute_cap=parts[3],
                    memory_total_mb=float(parts[4]),
                    memory_used_mb=float(parts[5]),
                    utilization_pct=float(parts[6]),
                )
            )
        except ValueError:
            continue
    return gpus

