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


def read_context_length(path: str | Path) -> int | None:
    """Return the model's trained context length, or ``None`` if unreadable.

    For split GGUFs, pass the first shard (``*-00001-of-*.gguf``) — it carries
    the metadata.
    """
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"GGUF":
                return None
            (version,) = struct.unpack("<I", f.read(4))
            if version < 2:  # v1 used 32-bit lengths; not emitted by modern tooling
                return None
            struct.unpack("<Q", f.read(8))  # tensor count (unused)
            (kv_count,) = struct.unpack("<Q", f.read(8))

            arch: str | None = None
            ctx: dict[str, int] = {}
            for _ in range(kv_count):
                key = _read_str(f)
                (vtype,) = struct.unpack("<I", f.read(4))
                val = _read_value(f, vtype)
                if key == "general.architecture":
                    arch = val
                elif key.endswith(".context_length"):
                    ctx[key] = int(val)

            if arch and f"{arch}.context_length" in ctx:
                return ctx[f"{arch}.context_length"]
            return next(iter(ctx.values())) if ctx else None
    except Exception:
        return None
