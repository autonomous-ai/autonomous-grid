"""Typed shapes for the doggi media-generation gateway.

These are :class:`typing.TypedDict` definitions used purely for editor
autocompletion and documentation — at runtime the API returns plain ``dict``
objects, which the library passes through unchanged.
"""

from __future__ import annotations

from typing import List, Literal, Optional, TypedDict

#: Generation status reported by ``GET /media/generations/{id}``.
GenerationStatus = Literal[
    "queued",
    "processing",
    "running",
    "completed",
    "failed",
]

#: Statuses that mean the generation is over and won't change again.
TERMINAL_STATUSES = frozenset({"completed", "failed"})


class ResultFile(TypedDict, total=False):
    """One output file in a finished generation's ``result_files``.

    The gateway is not perfectly uniform: most models use ``file_url``, some
    fall back to ``url``; mask files may carry ``type``/``kind``/``is_mask``.
    """

    file_url: str
    url: str
    content_type: str
    width: Optional[int]
    height: Optional[int]
    type: str
    kind: str
    is_mask: bool


class Generation(TypedDict, total=False):
    """The task object returned when you submit or poll a generation."""

    request_id: str
    status: GenerationStatus
    progress: Optional[float]
    result_files: List[ResultFile]
    # legacy/per-model fallbacks for the file list:
    images: List[ResultFile]
    files: List[ResultFile]
    seed: Optional[int]
    error: Optional[str]


class NormalizedFile(TypedDict):
    """A file flattened to a stable shape by :meth:`Receipt.files`."""

    url: str
    content_type: Optional[str]
    width: Optional[int]
    height: Optional[int]
    is_mask: bool


def normalize_files(task: dict) -> List[NormalizedFile]:
    """Flatten a finished task's output files to a uniform ``NormalizedFile`` list.

    Looks at ``result_files`` first, then the per-model ``images`` / ``files``
    fallbacks, and resolves ``file_url`` vs ``url``. Mirrors the normalization
    each actor does internally so callers don't have to.
    """

    raw = task.get("result_files") or task.get("images") or task.get("files") or []
    out: List[NormalizedFile] = []

    for f in raw:
        url = f.get("file_url") or f.get("url")

        if not url:
            continue

        is_mask = bool(
            f.get("is_mask")
            or f.get("type") == "mask"
            or f.get("kind") == "mask"
            or ("mask" in url.lower())
        )

        out.append(
            {
                "url": url,
                "content_type": f.get("content_type"),
                "width": f.get("width"),
                "height": f.get("height"),
                "is_mask": is_mask,
            }
        )

    return out


ImageSize = Literal[
    "square_hd",
    "square",
    "portrait_4_3",
    "portrait_16_9",
    "landscape_4_3",
    "landscape_16_9",
    "auto",
]

ImageAspectRatio = Literal[
    "auto", "21:9", "16:9", "3:2", "4:3", "5:4", "1:1",
    "4:5", "3:4", "2:3", "9:16", "4:1", "1:4", "8:1", "1:8",
]

VideoResolution = Literal[
    "480p", "580p", "720p"
]

VideoAspectRatio = Literal[
    "auto", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"
]