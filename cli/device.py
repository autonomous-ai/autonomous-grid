"""`grid device-info`: this machine's hardware profile.

One flat inventory — CPU, memory, disk, GPU, and the memory budget a local model may
claim — so you can see at a glance what this box can actually run.
"""
from __future__ import annotations

import argparse
import json


def cmd_device_info(args: argparse.Namespace) -> int:
    from shared.system.device_info import collect_device_info

    info = collect_device_info()
    if getattr(args, "json", False):
        print(json.dumps(info, indent=2))
        return 0
    _print_human(info)
    return 0


def _gb(n_bytes: int) -> str:
    return f"{n_bytes / (1024 ** 3):.1f} GB"


def _print_human(info: dict) -> None:
    machine = info.get("machine") or {}
    cpu = info.get("cpu") or {}
    mem = info.get("memory") or {}
    disk = info.get("disk") or {}
    gpus = info.get("gpus") or []

    # The model line prefers the friendly machine name (Apple only); everywhere else the
    # CPU/GPU brand is the most recognisable label, so fall back to that.
    name = machine.get("model") or cpu.get("brand") or "This machine"
    print(name)
    print(f"  Class    {info.get('device_class')}  ·  backend {info.get('backend')}")

    chip = cpu.get("brand")
    if chip and chip != name:
        print(f"  Chip     {chip}")

    cores = cpu.get("physical_cores")
    threads = cpu.get("logical_threads")
    if cores or threads:
        print(f"  CPU      {cores} cores / {threads} threads")

    print(
        f"  Memory   {mem.get('total_gb')} GB total  ·  "
        f"{mem.get('available_gb')} GB available"
    )
    print(f"  Disk     {disk.get('total_gb')} GB total  ·  {disk.get('free_gb')} GB free")

    # usable_bytes is the number that actually decides which models fit — surface it plainly.
    print(f"  Usable   {_gb(info.get('usable_bytes', 0))} for models  ({info.get('detected')})")

    if not gpus:
        print("  GPU      none")
    for g in gpus:
        cores_part = f" · {g['core_count']} cores" if g.get("core_count") else ""
        vram = g.get("memory_total_mb") or 0
        vram_part = f" · {vram / 1024:.0f} GB" if vram else ""
        print(f"  GPU      {g.get('name')}{cores_part}{vram_part}")
