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
    hf_repo: str
    quantized_file: str
    min_vram_gb: int
    kind: str
    notes: str = ""
    target: str = TARGET_ANY


CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        hf_repo="unsloth/Qwen3.6-35B-A3B-MTP-GGUF",
        quantized_file="Qwen3.6-35B-A3B-UD-IQ3_S.gguf",
        min_vram_gb=32,
        kind="language",
        notes="Recommended Qwen 3.6 MTP model for Apple Silicon unified memory.",
        target=TARGET_APPLE_SILICON,
    ),
    CatalogEntry(
        hf_repo="unsloth/Qwen3.6-27B-MTP-GGUF",
        quantized_file="Qwen3.6-27B-UD-Q5_K_XL.gguf",
        min_vram_gb=24,
        kind="language",
        notes="Recommended Qwen 3.6 MTP model for NVIDIA CUDA hosts.",
        target=TARGET_NVIDIA,
    ),
)


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


def pull_spec(entry: CatalogEntry) -> str:
    """The exact argument `grid pull` takes for [entry] — `<repo>:<file>`, nothing invented.

    A short nickname used to live here instead, and `grid pull` accepted it. It was dropped
    because a name that exists only inside Grid reads like a real model id and is not one. What
    replaces it is the spec itself: still one whitespace-free token a caller can hand straight to
    `grid pull`, but every character of it is checkable against Hugging Face.
    """
    return f"{entry.hf_repo}:{entry.quantized_file}"


def format_catalog_entry(entry: CatalogEntry) -> str:
    """One catalog row: the pull spec, then the same repo and file as a browsable path.

    Two columns, and the repetition is deliberate rather than sloppy. The first is a command
    argument (`repo:file`); the second is the path you would open on huggingface.co (`repo/file`).
    One colon apart, but only one of them works in each place — and this row is parsed
    POSITIONALLY by tools that read it, first token as what to pull and second as what to show.
    Changing the column count breaks them silently rather than loudly: dropping the first column
    made one parser read `(Apple` as the repository name (measured, not feared).
    """
    target = "" if entry.target == TARGET_ANY else f"{target_label(entry.target)}, "
    return (
        f"  {pull_spec(entry)}  {entry.hf_repo}/{entry.quantized_file} "
        f"({target}min {entry.min_vram_gb} GB, {entry.kind})"
    )

