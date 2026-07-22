"""The async-flow receipt: a handle to an in-flight generation.

A :class:`Receipt` is returned by the non-blocking ``submit`` calls. It carries
the gateway ``request_id`` plus enough state to check on and collect the result
later — including from a *different* process, via :meth:`Receipt.to_dict` /
:meth:`Receipt.from_dict`.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .errors import GenerationFailedError, GenerationTimeoutError, ResultNotReadyError
from .types import TERMINAL_STATUSES, NormalizedFile, normalize_files

if TYPE_CHECKING:  # avoid a runtime import cycle
    from .client import DoggiClient


class Receipt:
    """A handle to a submitted generation.

    You normally get one from ``client.submit(...)`` or a typed ``.submit()``
    helper. Use :meth:`ready` / :meth:`result` to poll yourself, or
    :meth:`wait` to block until the generation finishes.
    """

    def __init__(
        self,
        client: "DoggiClient",
        request_id: str,
        *,
        model: str = "",
        status: Optional[str] = None,
        task: Optional[Dict[str, Any]] = None,
    ):
        self._client = client
        #: The gateway request id for this generation.
        self.request_id = request_id
        #: The model this generation was submitted to (best-effort).
        self.model = model
        self.status: Optional[str] = status
        #: Last raw task object seen from the gateway.
        self.task: Dict[str, Any] = task or {}

    # -- polling ----------------------------------------------------------

    def refresh(self) -> "Receipt":
        """Re-fetch the task object and update cached status in place."""
        task = self._client.get_generation(self.request_id)
        self.task = task
        self.status = task.get("status", self.status)
        return self

    @property
    def progress(self) -> Optional[float]:
        """Last known progress in ``0.0``–``1.0`` (``None`` if unreported)."""
        return self.task.get("progress")

    def is_terminal(self) -> bool:
        """True if the generation is over (completed or failed), no re-fetch."""
        return self.status in TERMINAL_STATUSES

    def succeeded(self) -> bool:
        """True if the last known status is ``completed`` (no re-fetch)."""
        return self.status == "completed"

    def ready(self) -> bool:
        """Poll once; return True if the generation reached a terminal status."""

        if self.is_terminal():
            return True

        self.refresh()

        return self.is_terminal()

    # -- collecting results ----------------------------------------------

    def result(self, *, refresh: bool = True) -> Dict[str, Any]:
        """Return the finished task object.

        Raises :class:`ResultNotReadyError` if still running and
        :class:`GenerationFailedError` if it ended in ``failed``.
        """

        if refresh and not self.is_terminal():
            self.refresh()

        if not self.is_terminal():
            raise ResultNotReadyError(
                f"Generation {self.request_id} is not finished "
                f"(status: {self.status})"
            )

        if self.status != "completed":
            raise GenerationFailedError(
                self.request_id, self.status or "UNKNOWN", task=self.task
            )

        return self.task

    def files(self, *, refresh: bool = False) -> List[NormalizedFile]:
        """Return the finished generation's output files, normalized.

        Convenience over :meth:`result` — resolves ``result_files`` /
        ``images`` / ``files`` and ``file_url`` vs ``url`` into a uniform list.
        """

        task = self.result(refresh=refresh)
        return normalize_files(task)

    def wait(
        self,
        *,
        timeout: float = 600.0,
        poll_interval: float = 2.0,
    ) -> Dict[str, Any]:
        """Block until the generation finishes, then return the task object.

        Args:
            timeout: Max seconds to wait before raising
                :class:`GenerationTimeoutError`.
            poll_interval: Seconds between status checks.

        Raises:
            GenerationFailedError: The generation ended in ``failed``.
            GenerationTimeoutError: ``timeout`` elapsed first.
        """

        deadline = time.monotonic() + timeout

        while True:
            self.refresh()

            if self.is_terminal():
                if self.status != "completed":
                    raise GenerationFailedError(
                        self.request_id, self.status or "UNKNOWN", task=self.task
                    )
                return self.task

            remaining = deadline - time.monotonic()

            if remaining <= 0:
                raise GenerationTimeoutError(
                    self.request_id, self.status or "UNKNOWN", timeout
                )

            time.sleep(min(poll_interval, remaining))

    # -- serialization (cross-process handoff) ---------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict you can store and rehydrate elsewhere."""
        return {
            "request_id": self.request_id,
            "model": self.model,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, client: "DoggiClient", data: Dict[str, Any]) -> "Receipt":
        """Rebuild a receipt from :meth:`to_dict` output, bound to ``client``."""
        return cls(
            client,
            request_id=data["request_id"],
            model=data.get("model", ""),
            status=data.get("status"),
        )

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"Receipt(request_id={self.request_id!r}, model={self.model!r}, "
            f"status={self.status!r})"
        )
