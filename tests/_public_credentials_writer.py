"""One half of the two-writer race in `tests/test_public_credentials_lock.py` (issue 12 follow-up).

A real child process, never a thread: `flock` is tied to the open file description, so a pair of
threads sharing one interpreter proves the wrong thing about the mechanism even when it goes green.

argv: ``<network_id> <role: slow|fast> <rendezvous dir> <mode: locked|unlocked>``
"""

from __future__ import annotations

import contextlib
import pathlib
import sys
import time

from remote import credentials

#: How long the slow writer holds still after reading. Locked, the fast writer is parked in `flock`
#: for the whole of it; unlocked it finishes in milliseconds and the pause ends early.
PAUSE_SECONDS = 2.0
HANDSHAKE_TIMEOUT_SECONDS = 20.0
_POLL_SECONDS = 0.01


def _wait_for(marker: pathlib.Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            return True
        time.sleep(_POLL_SECONDS)
    return False


def record(network_id: str) -> dict[str, object]:
    """One grid's entry — the per-grid row a lost update drops whole."""
    return {
        "network_id": network_id,
        "name": network_id,
        "network_type": "permissioned",
        "access_token": f"access-{network_id}",
        "refresh_token": f"refresh-{network_id}",
    }


def main(argv: list[str]) -> int:
    network_id, role, rendezvous, mode = argv
    rv = pathlib.Path(rendezvous)
    slow_has_read, fast_has_saved = rv / "slow-has-read", rv / "fast-has-saved"

    if mode == "unlocked":
        credentials.credentials_lock = contextlib.nullcontext  # the negative control

    if role == "slow":
        read = credentials.load_credentials

        def read_then_hold_still() -> dict:
            data = read()
            slow_has_read.touch()
            _wait_for(fast_has_saved, PAUSE_SECONDS)
            return data

        credentials.load_credentials = read_then_hold_still
    elif not _wait_for(slow_has_read, HANDSHAKE_TIMEOUT_SECONDS):
        raise SystemExit("the slow writer never reported reading the file")

    credentials.add_network(record(network_id))

    if role == "fast":
        fast_has_saved.touch()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
