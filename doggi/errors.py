"""Exception hierarchy for the doggi client."""

from __future__ import annotations

from typing import Any, Optional


class DoggiError(Exception):
    """Base class for every error raised by this library."""


class AuthError(DoggiError):
    """No API key could be resolved, or the gateway rejected it."""


class GatewayError(DoggiError):
    """The gateway returned a non-2xx HTTP response.

    Attributes:
        status: HTTP status code.
        body: Decoded response body (parsed JSON when possible, else text).
        message: Best-effort human-readable message pulled from the body.
    """

    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        self.message = _extract_message(body) or f"HTTP {status}"
        super().__init__(f"Gateway error {status}: {self.message}")


class GenerationFailedError(DoggiError):
    """The generation reached a terminal ``failed`` status.

    Attributes:
        request_id: The gateway request id.
        status: Terminal status (normally ``failed``).
        task: The full task object, if available.
    """

    def __init__(
        self,
        request_id: str,
        status: str,
        message: Optional[str] = None,
        task: Optional[dict] = None,
    ):
        self.request_id = request_id
        self.status = status
        self.task = task
        #: The gateway's error/traceback string, if the API returned one.
        self.error: Optional[str] = _task_error(task)
        if message is None:
            message = f"Generation {request_id} ended with status {status}"
            if self.error:
                message += f": {self.error.strip().splitlines()[-1][:300]}"
        super().__init__(message)


def _task_error(task: Optional[dict]) -> Optional[str]:
    """Pull a failure reason out of a task object, if the API included one.

    The gateway stores a full traceback in ``error`` server-side; today's read
    API strips it, but this surfaces it automatically once it's exposed (and
    works if you hand the raw DB row to the exception).
    """
    if not isinstance(task, dict):
        return None
    for key in ("error", "error_message", "detail", "message"):
        val = task.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return None


class GenerationTimeoutError(DoggiError):
    """Internal polling gave up before the generation reached a terminal status.

    Attributes:
        request_id: The gateway request id (may still be running server-side).
        status: Last observed status.
    """

    def __init__(self, request_id: str, status: str, waited: float):
        self.request_id = request_id
        self.status = status
        self.waited = waited
        super().__init__(
            f"Generation {request_id} did not finish within {waited:.0f}s "
            f"(last status: {status})"
        )


class ResultNotReadyError(DoggiError):
    """``Receipt.result()`` was called before the generation finished."""


def _extract_message(body: Any) -> Optional[str]:
    if isinstance(body, dict):
        for key in ("message", "detail", "error"):
            val = body.get(key)

            if isinstance(val, str) and val:
                return val

            if isinstance(val, dict):
                msg = val.get("message") or val.get("type")
                if msg:
                    return msg

    if isinstance(body, str) and body:
        return body[:300]

    return None
