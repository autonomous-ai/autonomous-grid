"""Resumable Hugging Face GGUF downloads."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

from shared import paths


HF_BASE = "https://huggingface.co"
CHUNK = 1024 * 1024
DOWNLOAD_ATTEMPTS = 8
DOWNLOAD_READ_TIMEOUT_SECONDS = 30.0


def hf_url(repo: str, quantized_file: str) -> str:
    return f"{HF_BASE}/{repo}/resolve/main/{quantized_file}"


DEFAULT_QUANT = "Q4_K_M"


def parse_spec(spec: str) -> tuple[str, str | None]:
    """Split a pull spec into ``(repo, filename)``. ``filename`` is ``None`` for a bare
    ``owner/repo``, which means the caller must show the repo's file list and let the reader pick.

    Exactly two accepted forms, and no third: name the **exact** file (``repo:model-Q8_0.gguf``),
    or name **only the repo** and choose from the list. A partial quant (``repo:Q8_0``) is refused
    rather than silently substring-matched, because that match is a guess: `Q8_0` hits
    `model-Q8_0.gguf` and `model-UD-Q8_0.gguf` alike, and picking one for a reader who typed
    neither is how somebody ends up serving a file they never chose.
    """
    if ":" in spec:
        repo, _, tail = spec.partition(":")
        repo, tail = repo.strip(), tail.strip()
        if not repo or not tail:
            raise SystemExit(f"{spec!r} needs a repository before the colon.")
        if not tail.lower().endswith(".gguf"):
            raise SystemExit(
                f"{tail!r} isn't a filename. After the colon, name the exact file — it ends "
                f"'.gguf' (see the repo's Files and versions tab on huggingface.co). To choose "
                f"from a list instead, drop the colon and pull just the repo: 'grid pull {repo}'."
            )
        return repo, tail
    if "/" in spec:
        return spec, None  # bare `owner/repo` — the caller lists the files and asks
    raise SystemExit(
        f"{spec!r} isn't a Hugging Face repository. `grid pull` needs one, not a short name — "
        "run `grid catalog` and copy the repo/file shown there, or name any repo yourself: "
        "'unsloth/Qwen3.6-35B-A3B-MTP-GGUF' to pick from its files, or "
        "'unsloth/Qwen3.6-35B-A3B-MTP-GGUF:<exact-filename>.gguf' to skip the prompt."
    )


def list_model_files(repo: str, timeout: float = 15.0) -> list[str]:
    """Every top-level `.gguf` model file in [repo] (never `mmproj-*`), alphabetically.

    One list call, no download — the same repo listing `resolve_file` and `find_projector` each
    need, factored out so a caller can show the reader what else was available instead of a
    filename resolved in silence.
    """
    try:
        resp = httpx.get(f"{HF_BASE}/api/models/{repo}", timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
        siblings = resp.json().get("siblings")
    except (httpx.HTTPError, ValueError) as exc:
        raise SystemExit(f"Could not list files in {repo!r}: {exc}") from exc
    return sorted(
        s["rfilename"] for s in (siblings or [])
        if isinstance(s, dict) and isinstance(s.get("rfilename"), str)
        and "/" not in s["rfilename"]  # top-level only, never a nested variant dir
        and s["rfilename"].lower().endswith(".gguf")
        and "mmproj" not in s["rfilename"].lower()
    )


def resolve_file(repo: str, timeout: float = 15.0) -> tuple[str, list[str]]:
    """``(suggested default, every .gguf in [repo])``.

    The default is the ``DEFAULT_QUANT`` file, or the alphabetically first when the repo ships no
    such quant — the same fallback llama.cpp's `-hf` documents. It is only ever a *suggestion* the
    caller offers: choosing is the reader's, which is why the full list comes back with it.
    """
    names = list_model_files(repo, timeout=timeout)
    if not names:
        raise SystemExit(f"No .gguf files found in {repo!r}. Check the repo on huggingface.co.")
    wanted = DEFAULT_QUANT.lower()
    chosen = next((n for n in names if wanted in n.lower()), names[0])
    return chosen, names


def local_path(quantized_file: str) -> Path:
    paths.ensure_all()
    return paths.models_dir() / Path(quantized_file).name


# Preference order for a repo that ships several projector precisions. BF16 and F16 are the same
# size and either works; F32 is double for no practical gain, so it is the last resort rather than
# whichever happens to sort first.
_PROJECTOR_PREFERENCE = ("mmproj-f16", "mmproj-bf16", "mmproj")


def find_projector(repo: str, timeout: float = 15.0) -> str | None:
    """The name of the multimodal projector in [repo], or ``None`` if it ships none.

    Vision models on Hugging Face put the projector in the SAME repo as the weights (unsloth's
    `gemma-3-4b-it-GGUF` carries `mmproj-BF16.gguf`, `mmproj-F16.gguf` and `mmproj-F32.gguf`
    beside every quant), which is the same pairing llama.cpp's own `--mmproj-auto` relies on for
    `-hf`. Best-effort by design: this runs inside `grid pull`, and a rate-limited or offline API
    must cost the user a projector, never the model they actually asked for.
    """
    try:
        resp = httpx.get(f"{HF_BASE}/api/models/{repo}", timeout=timeout, follow_redirects=True)
        if resp.status_code != 200:
            return None
        siblings = resp.json().get("siblings")
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    if not isinstance(siblings, list):
        return None
    names = [
        s["rfilename"]
        for s in siblings
        if isinstance(s, dict)
        and isinstance(s.get("rfilename"), str)
        and s["rfilename"].endswith(".gguf")
        and "mmproj" in s["rfilename"].lower()
        and "/" not in s["rfilename"]  # top-level only; never a file from a nested variant dir
    ]
    for wanted in _PROJECTOR_PREFERENCE:
        for name in sorted(names):
            if name.lower().startswith(wanted):
                return name
    return None


def download(
    repo: str,
    quantized_file: str,
    *,
    out: Path | None = None,
    on_progress=None,
    max_bytes: int | None = None,
) -> Path:
    if max_bytes is not None and (
        isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0
    ):
        raise ValueError("max_bytes must be a positive integer or None")
    target = out or local_path(quantized_file)
    if target.is_file():
        if max_bytes is not None and target.stat().st_size > max_bytes:
            raise SystemExit(
                f"Downloaded artifact exceeds the configured {max_bytes} byte limit."
            )
        # `grid pull` must be safe to run twice — re-running it to double check a name, or
        # because a script always calls it before `grid join`, must not re-download a multi-
        # gigabyte file that is already sitting right there. Only a `.part` (a download that
        # never finished) is worth resuming; a complete `target` is worth nothing more to do.
        return target
    part = target.with_suffix(target.suffix + ".part")
    target.parent.mkdir(parents=True, exist_ok=True)
    url = hf_url(repo, quantized_file)

    have = part.stat().st_size if part.exists() else 0
    if max_bytes is not None and have > max_bytes:
        raise SystemExit(
            f"Partial download already exceeds the configured {max_bytes} byte limit."
        )
    try:
        last_error: httpx.TransportError | None = None
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            have = part.stat().st_size if part.exists() else 0
            headers = {"Range": f"bytes={have}-"} if have else {}
            try:
                with httpx.stream(
                    "GET",
                    url,
                    headers=headers,
                    timeout=httpx.Timeout(30, read=DOWNLOAD_READ_TIMEOUT_SECONDS),
                    follow_redirects=True,
                ) as resp:
                    if resp.status_code not in (200, 206):
                        try:
                            body = resp.read().decode(errors="replace")[:300]
                        except httpx.HTTPError:
                            body = "(could not read response body)"
                        raise SystemExit(f"Download failed ({resp.status_code}): {body}")

                    # A server may ignore Range and return the complete file with 200. Appending
                    # that response to a partial would silently create a corrupt oversized model;
                    # restart this one response from byte zero instead.
                    if have and resp.status_code == 200:
                        have = 0
                        mode = "wb"
                    else:
                        mode = "ab" if have else "wb"
                    remaining = int(resp.headers.get("Content-Length") or 0)
                    total = have + remaining if remaining else 0
                    if max_bytes is not None and total > max_bytes:
                        raise SystemExit(
                            f"Artifact is {total} bytes, above the configured "
                            f"{max_bytes} byte limit."
                        )
                    with part.open(mode) as fh:
                        for chunk in resp.iter_bytes(CHUNK):
                            if not chunk:
                                continue
                            if max_bytes is not None and have + len(chunk) > max_bytes:
                                raise SystemExit(
                                    "Artifact stream exceeded the configured "
                                    f"{max_bytes} byte limit."
                                )
                            fh.write(chunk)
                            have += len(chunk)
                            if on_progress:
                                on_progress(have, total)
                    if total and have < total:
                        raise httpx.ReadError(
                            f"download ended at {have} of {total} bytes"
                        )
                part.replace(target)
                return target
            except httpx.TransportError as exc:
                last_error = exc
                if attempt == DOWNLOAD_ATTEMPTS:
                    break
                kept = part.stat().st_size if part.exists() else 0
                print(
                    f"\nDownload connection interrupted ({exc}); reconnecting "
                    f"{attempt + 1}/{DOWNLOAD_ATTEMPTS} from {kept / 1e6:.0f} MB...",
                    file=sys.stderr,
                )
        kept = part.stat().st_size if part.exists() else 0
        raise SystemExit(
            f"Download stopped after {DOWNLOAD_ATTEMPTS} connection attempts: {last_error}. "
            f"{kept / 1e6:.0f} MB kept; rerun the same command to resume."
        )
    except KeyboardInterrupt:
        # `.part` is deliberately left in place, not deleted — it is what makes the `Range`
        # header above resume from `have` instead of restarting a multi-gigabyte download from
        # byte 0 next time. Ctrl-C here used to skip this entirely: httpx/httpcore/ssl raise
        # KeyboardInterrupt from three layers down mid-socket-read, and with nothing catching it
        # partway up the call stack, the reader got a 30-line stack trace instead of a cancellation.
        raise SystemExit(f"\nCancelled. Resume with the same command — {have / 1e6:.0f} MB kept.") from None

    raise AssertionError("download retry loop exited without an outcome")


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
