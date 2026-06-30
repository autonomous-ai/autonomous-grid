"""Curated GGUF model catalog for local providers."""

from __future__ import annotations

import platform
import shutil
from dataclasses import dataclass


TARGET_ANY = "any"
TARGET_APPLE_SILICON = "apple-silicon"
TARGET_NVIDIA = "nvidia"


@dataclass(frozen=True)
class CatalogEntry:
    label: str
    hf_repo: str
    quantized_file: str
    min_vram_gb: int
    kind: str
    notes: str = ""
    target: str = TARGET_ANY


CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        label="qwen36-35b-a3b-mtp",
        hf_repo="unsloth/Qwen3.6-35B-A3B-MTP-GGUF",
        quantized_file="Qwen3.6-35B-A3B-UD-IQ3_S.gguf",
        min_vram_gb=32,
        kind="language",
        notes="Recommended Qwen 3.6 MTP model for Apple Silicon unified memory.",
        target=TARGET_APPLE_SILICON,
    ),
    CatalogEntry(
        label="qwen36-27b-mtp",
        hf_repo="unsloth/Qwen3.6-27B-MTP-GGUF",
        quantized_file="Qwen3.6-27B-UD-Q5_K_XL.gguf",
        min_vram_gb=24,
        kind="language",
        notes="Recommended Qwen 3.6 MTP model for NVIDIA CUDA hosts.",
        target=TARGET_NVIDIA,
    ),
)


def find(label: str) -> CatalogEntry | None:
    for entry in CATALOG:
        if entry.label == label:
            return entry
    return None


def current_target() -> str | None:
    if platform.system() == "Darwin" and platform.machine() in ("arm64", "aarch64"):
        return TARGET_APPLE_SILICON
    if shutil.which("nvidia-smi"):
        return TARGET_NVIDIA
    return None


def recommended_entries(target: str | None = None) -> tuple[CatalogEntry, ...]:
    resolved = current_target() if target is None else target
    if resolved is None:
        # Unrecognized hardware (CPU-only Linux, Intel Mac, Windows, …): we can't
        # narrow by target, so surface the whole catalog rather than nothing.
        return CATALOG
    return tuple(
        entry
        for entry in CATALOG
        if entry.target == TARGET_ANY or entry.target == resolved
    )


def target_label(target: str) -> str:
    if target == TARGET_APPLE_SILICON:
        return "Apple Silicon"
    if target == TARGET_NVIDIA:
        return "NVIDIA"
    return target


def format_catalog_entry(entry: CatalogEntry) -> str:
    target = "" if entry.target == TARGET_ANY else f"{target_label(entry.target)}, "
    return (
        f"  {entry.label:<32} {entry.hf_repo}/{entry.quantized_file} "
        f"({target}min {entry.min_vram_gb} GB, {entry.kind})"
    )

