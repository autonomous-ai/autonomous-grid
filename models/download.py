"""Resumable Hugging Face GGUF downloads."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

import paths


HF_BASE = "https://huggingface.co"
CHUNK = 1024 * 1024


def hf_url(repo: str, quantized_file: str) -> str:
    return f"{HF_BASE}/{repo}/resolve/main/{quantized_file}"


def parse_spec(spec: str) -> tuple[str, str]:
    if ":" in spec:
        repo, _, filename = spec.partition(":")
        repo = repo.strip()
        filename = filename.strip()
        if repo and filename:
            return repo, filename
    parts = spec.rsplit("/", 1)
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0], parts[1]
    raise SystemExit(
        f"Unrecognized model spec: {spec!r}. Use '<hf-id>:<filename>' "
        "(for example unsloth/Qwen3.6-35B-A3B-MTP-GGUF:Qwen3.6-35B-A3B-UD-IQ3_S.gguf)."
    )


def local_path(quantized_file: str) -> Path:
    paths.ensure_all()
    return paths.models_dir() / Path(quantized_file).name


def download(repo: str, quantized_file: str, *, out: Path | None = None, on_progress=None) -> Path:
    target = out or local_path(quantized_file)
    part = target.with_suffix(target.suffix + ".part")
    target.parent.mkdir(parents=True, exist_ok=True)
    url = hf_url(repo, quantized_file)

    headers: dict[str, str] = {}
    have = part.stat().st_size if part.exists() else 0
    if have > 0:
        headers["Range"] = f"bytes={have}-"

    mode = "ab" if have > 0 else "wb"
    with httpx.stream("GET", url, headers=headers, timeout=httpx.Timeout(30, read=None), follow_redirects=True) as resp:
        if resp.status_code not in (200, 206):
            raise SystemExit(f"Download failed ({resp.status_code}): {resp.text[:300]}")
        total = have + int(resp.headers.get("Content-Length") or 0)
        with part.open(mode) as fh:
            for chunk in resp.iter_bytes(CHUNK):
                if not chunk:
                    continue
                fh.write(chunk)
                have += len(chunk)
                if on_progress:
                    on_progress(have, total)

    part.replace(target)
    return target


def stderr_progress(done: int, total: int) -> None:
    if total <= 0:
        sys.stderr.write(f"\r{done / 1e6:.1f} MB")
        sys.stderr.flush()
        return
    pct = done / total
    width = 30
    filled = int(width * pct)
    bar = "#" * filled + "." * (width - filled)
    sys.stderr.write(f"\r[{bar}] {done / 1e6:8.1f} / {total / 1e6:.1f} MB ({pct * 100:5.1f}%)")
    sys.stderr.flush()
    if done >= total:
        sys.stderr.write("\n")

