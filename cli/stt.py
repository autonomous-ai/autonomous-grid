"""`grid stt transcribe`: speech-to-text for the app's voice-input feature.

Uploads to the account-level Grid control plane with the saved session token. There's no
"this grid" for it to route through, so it behaves identically in local and remote mode
and is classified AGNOSTIC in cli/dispatch.py, like `catalog`/`train`.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

from remote import credentials


_ENDPOINT_PATH = "/v1/audio/transcriptions"


def cmd_stt_transcribe(args: argparse.Namespace) -> int:
    session_token = credentials.require_session()
    path = Path(args.audio).expanduser()
    if not path.is_file():
        raise SystemExit(f"Audio file not found: {path}")

    try:
        with path.open("rb") as fh:
            resp = httpx.post(
                f"{credentials.api_url()}{_ENDPOINT_PATH}",
                params={"lang": args.lang},
                headers={"Authorization": f"Bearer {session_token}"},
                files={"file": (path.name, fh, "audio/wav")},
                timeout=args.timeout,
            )
    except httpx.RequestError as exc:
        print(f"Couldn't reach the transcription service: {exc}", file=sys.stderr)
        return 1

    if resp.status_code >= 400:
        # Prefixed with the status so the Dart caller can tell an expired session (401) from
        # a too-long clip (413) apart without re-parsing JSON — mirrors cmd_chat's convention
        # of printing the raw body on error, plus the status code.
        print(f"HTTP {resp.status_code}: {resp.text}")
        return 1
    if getattr(args, "json", False):
        print(resp.text)
        return 0
    try:
        print(resp.json()["data"]["transcript"])
    except (KeyError, TypeError, ValueError):
        print(resp.text)
    return 0
