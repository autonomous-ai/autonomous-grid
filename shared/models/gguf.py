"""Minimal read-only GGUF metadata reader (no external dependencies).

Parses just enough of the GGUF header to recover a model's trained context
length (``<arch>.context_length``). Everything is best-effort: any malformed
or unexpected file returns ``None`` rather than raising.
"""

from __future__ import annotations

import struct
from pathlib import Path

# GGUF scalar value types -> struct format (all little-endian).
_SCALAR = {
    0: "<B",   # uint8
    1: "<b",   # int8
    2: "<H",   # uint16
    3: "<h",   # int16
    4: "<I",   # uint32
    5: "<i",   # int32
    6: "<f",   # float32
    7: "<?",   # bool (1 byte)
    10: "<Q",  # uint64
    11: "<q",  # int64
    12: "<d",  # float64
}
_TYPE_STRING = 8
_TYPE_ARRAY = 9


def _read_str(f) -> str:
    (n,) = struct.unpack("<Q", f.read(8))
    return f.read(n).decode("utf-8", "replace")


def _read_value(f, vtype: int):
    fmt = _SCALAR.get(vtype)
    if fmt is not None:
        return struct.unpack(fmt, f.read(struct.calcsize(fmt)))[0]
    if vtype == _TYPE_STRING:
        return _read_str(f)
    if vtype == _TYPE_ARRAY:
        (elem_type,) = struct.unpack("<I", f.read(4))
        (count,) = struct.unpack("<Q", f.read(8))
        for _ in range(count):
            _read_value(f, elem_type)  # consume; array values are unused here
        return None
    raise ValueError(f"unknown gguf value type {vtype}")


def _iter_kv(path: str | Path):
    """Yield every ``(key, value)`` in a GGUF's header, or nothing at all if it isn't readable.

    Every reader below walks the same header the same way; sharing the walk keeps them from
    drifting on the parts that are easy to get subtly wrong (the version gate, array skipping).
    Array values are yielded as ``None`` — their bytes are consumed, but no reader wants them.
    """
    with open(path, "rb") as f:
        if f.read(4) != b"GGUF":
            return
        (version,) = struct.unpack("<I", f.read(4))
        if version < 2:  # v1 used 32-bit lengths; not emitted by modern tooling
            return
        struct.unpack("<Q", f.read(8))  # tensor count (unused)
        (kv_count,) = struct.unpack("<Q", f.read(8))
        for _ in range(kv_count):
            key = _read_str(f)
            (vtype,) = struct.unpack("<I", f.read(4))
            yield key, _read_value(f, vtype)


def read_context_length(path: str | Path) -> int | None:
    """Return the model's trained context length, or ``None`` if unreadable.

    For split GGUFs, pass the first shard (``*-00001-of-*.gguf``) — it carries
    the metadata.
    """
    try:
        arch: str | None = None
        ctx: dict[str, int] = {}
        for key, val in _iter_kv(path):
            if key == "general.architecture":
                arch = val
            elif key.endswith(".context_length"):
                ctx[key] = int(val)

        if arch and f"{arch}.context_length" in ctx:
            return ctx[f"{arch}.context_length"]
        return next(iter(ctx.values())) if ctx else None
    except Exception:
        return None


def is_projector(path: str | Path) -> bool:
    """True when the file is a multimodal projector (an ``mmproj``), not a language model.

    Read from the file's own header, because the name proves nothing: every vision repo on Hugging
    Face ships its projector as some spelling of ``mmproj-F16.gguf``, so the name is neither unique
    across models nor guaranteed on any one of them. ``clip.has_vision_encoder`` /
    ``clip.has_audio_encoder`` are what llama.cpp itself keys on. Unreadable or malformed → False;
    a caller must never hand llama-server a ``--mmproj`` it is not sure about.
    """
    try:
        for key, val in _iter_kv(path):
            if key in ("clip.has_vision_encoder", "clip.has_audio_encoder") and val:
                return True
        return False
    except Exception:
        return False


def has_mtp_head(path: str | Path) -> bool:
    """True when the file's own header declares a fused MTP draft head.

    Checks `<arch>.nextn_predict_layers` in the file, not the filename or the HF repo it
    came from — two unsloth repos ship a file with the identical name
    (`Qwen3.6-35B-A3B-Q8_0.gguf`) where only one actually has the layer. Any unreadable or
    malformed file returns False; callers should not add MTP flags on doubt.
    """
    try:
        for key, val in _iter_kv(path):
            if key.endswith(".nextn_predict_layers"):
                return bool(val)
        return False
    except Exception:
        return False


PROJECTOR_SUFFIX = ".mmproj.gguf"


def projector_beside(model_path: str | Path) -> Path | None:
    """The projector paired with this model file, or ``None`` for a text-only model.

    Pairing is by NAME, not by scanning the directory: `grid pull` saves a repo's projector as
    ``<model-stem>.mmproj.gguf`` beside the model precisely so this lookup can be exact. A
    directory scan cannot work — models live in one flat folder and every vision repo names its
    projector the same handful of ways, so two vision models would each match the other's file.

    The header is still checked before returning: the sidecar name is our own convention, and a
    file that does not declare a vision or audio encoder must never reach ``--mmproj``.
    """
    model_path = Path(model_path)
    # Built by hand, not `with_suffix`: model names carry dots inside the stem
    # (`Qwen3.6-35B-A3B-UD-IQ3_S.gguf`), and `with_suffix` would treat `.6-35B-A3B-UD-IQ3_S`
    # as the suffix and eat most of the name.
    candidate = model_path.parent / (model_path.stem + PROJECTOR_SUFFIX)
    if candidate.is_file() and is_projector(candidate):
        return candidate
    return None
