"""`grid login` / `grid logout` / `grid sync` — remote-mode sign-in and credential refresh.

Remote-only: dispatch gates these to remote mode, so the handlers assume remote. `cmd_login`
mirrors grid-src's browser device flow (start → poll → fetch tokens → persist); `cmd_logout`
clears the local credential store; `cmd_sync` reuses the saved session to re-fetch the grid list
+ tokens with no browser (ADR 0002 §11), never touching the active pointer. Remote deps import
lazily inside the handlers (repo convention). Tokens are never printed or logged — not on the human
path, not in ``--json``. Login does not pick an active grid; selection is always an explicit
``grid use <name>``.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import webbrowser
from typing import Any
from urllib.parse import urlencode


# Cap the server-supplied poll interval so a misbehaving/misconfigured control plane can't
# make the CLI appear frozen for far longer than the (already capped) sign-in deadline.
_MAX_POLL_INTERVAL_S = 30

# control_plane._raise formats failures as "<METHOD> <URL> failed (<status>): <body>". Match only the
# method-anchored prefix (re.match) so a 5xx whose body merely contains "failed (401):" can't be
# misclassified as an expired session.
_SESSION_EXPIRED_RE = re.compile(r"[A-Z]+ \S+ failed \((?:401|403)\):")


def cmd_login(args: argparse.Namespace) -> int:
    from remote import control_plane, credentials

    as_json = getattr(args, "json", False)
    api_url = credentials.api_url()
    device_id = credentials.device_id()

    started = control_plane.start_device_login(api_url)
    url = _device_login_url(started)
    _print_signin_prompt(url, started.get("user_code", ""), to_stderr=as_json)
    if not getattr(args, "no_browser", False):
        try:
            webbrowser.open(url)
        except (OSError, webbrowser.Error):
            pass  # headless box / no browser available — the URL + code are already printed

    approved = _await_approval(started, api_url)
    session_token = approved.get("session_token")
    if not session_token:
        # The user already approved in the browser; a token-less "approved" is a server
        # regression — fail clearly rather than KeyError after a successful sign-in.
        raise SystemExit("Sign-in was approved but the control plane returned no session token. "
                         "Run `grid login` to try again.")
    user = approved.get("user") or {}
    networks = _validated(control_plane.fetch_tokens(session_token, device_id, api_url))

    credentials.save_credentials({
        "session_token": session_token,
        "api_url": api_url,
        "user": user,
        "networks": networks,
    })
    # Deliberately no `state.set_active("remote", …)` here — login never auto-selects a grid.
    return _report_login(user.get("email", ""), networks, as_json=as_json)


def _await_approval(started: dict[str, Any], api_url: str) -> dict[str, Any]:
    """Poll until the user approves in the browser, or the device code expires."""
    from remote import control_plane

    device_code = started.get("device_code")
    if not device_code:
        raise SystemExit("Control plane returned no device code; cannot poll for sign-in approval.")

    interval = max(1, min(int(started.get("interval") or 2), _MAX_POLL_INTERVAL_S))
    deadline = time.monotonic() + min(int(started.get("expires_in") or 600), 600)
    while True:
        now = time.monotonic()  # read once per iteration: the deadline check and sleep clamp share it
        if now >= deadline:
            raise SystemExit("Sign-in timed out. Run `grid login` to try again.")
        result = control_plane.poll_device_login(device_code, api_url)
        status = result.get("status")
        if status == "approved":
            return result
        if status in {"expired", "consumed", "denied"}:
            raise SystemExit(f"Sign-in {status}. Run `grid login` to try again.")
        time.sleep(min(interval, max(0.1, deadline - now)))  # pending / slow_down — never past the deadline


def _device_login_url(started: dict[str, Any]) -> str:
    """The page where the user signs in with Google. Built from the configured website URL;
    falls back to the server's value when ``GRID_WEBSITE_URL`` is set empty (grid-src parity)."""
    from remote import credentials

    website = credentials.default_website_url()
    if website:
        query = urlencode({"user_code": started.get("user_code", "")})
        return f"{website}{credentials.GRID_LOGIN_PATH}?{query}"
    # Server-supplied fallback (operator opted out via GRID_WEBSITE_URL=""). It's opened in a
    # browser, so require HTTPS and a real value rather than trusting the response blindly.
    uri = started.get("verification_uri_complete") or ""
    if not uri.lower().startswith("https://"):
        raise SystemExit(
            "GRID_WEBSITE_URL is empty and the control plane did not return a usable "
            "https verification URL; set GRID_WEBSITE_URL to sign in."
        )
    return uri


def _validated(networks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trust-boundary check: refuse to persist a bundle we can't name or select later."""
    for net in networks:
        if not net.get("network_id") or not net.get("name"):
            raise SystemExit(
                "Control plane returned a malformed grid token; "
                "aborting to avoid corrupting local credentials."
            )
    return networks


def _print_signin_prompt(url: str, user_code: str, *, to_stderr: bool) -> None:
    # In --json mode the prompt goes to stderr so stdout stays clean JSON; the user still
    # needs the URL + code to act, so it is never suppressed.
    stream = sys.stderr if to_stderr else sys.stdout
    print("To sign in, open this URL and approve with Google:", file=stream)
    print(f"  {url}", file=stream)
    print(f"  Code: {user_code}", file=stream)


def _report_login(email: str, networks: list[dict[str, Any]], *, as_json: bool) -> int:
    if as_json:
        grids = [{"name": n["name"], "type": n.get("network_type")} for n in networks]
        print(json.dumps({"signed_in": True, "email": email, "grids": grids, "active": None}))
        return 0
    if networks:
        listed = ", ".join(n["name"] for n in networks)
        print(f"Signed in as {email}. {len(networks)} grid(s) available: {listed}.")
        print("Run `grid use <name>` to pick one.")
    else:
        print(f"Signed in as {email}. You don't belong to any grids yet.")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    from remote import credentials
    from shared import state

    existed = credentials.clear_credentials()
    state.set_active("remote", None)  # a cleared session has no active grid
    if getattr(args, "json", False):
        print(json.dumps({"signed_out": existed}))
        return 0
    print("Signed out." if existed else "You're not signed in.")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    """Refresh the stored grid list + per-grid tokens using the saved session — no browser.

    The no-re-auth complement to re-running ``grid login`` (ADR 0002 §11): reuse the session token to
    re-fetch ``GET /v1/grid/tokens`` and authoritatively overwrite the local grid list, so a grid
    created on the website (or one you were just added to) appears without signing in again. Never
    writes ``state.json`` — the active pointer is left untouched (a vanished active grid becomes a
    tolerated stale value), mirroring login's "never auto-select". The overwrite is last-writer-wins
    against a concurrent detached ``__remote-engine`` doing ``update_network_tokens`` — the same
    atomic-write model the rest of the credential store uses, so no new locking is needed.
    """
    from remote import control_plane, credentials

    as_json = getattr(args, "json", False)
    # One credentials snapshot for both the auth gate and the merge below. Reading the session token
    # and `data` from the same load closes a TOCTOU window: with two reads, a concurrent `grid logout`
    # in between would let the save recreate a partial file (networks but no session) — the same
    # concurrent-logout hazard credentials.update_network_tokens already guards. Same "not signed in"
    # wording as credentials.require_session (the gate every other remote command uses).
    data = credentials.load_credentials()
    session_token = data.get("session_token")
    if not session_token:
        raise SystemExit("You're not signed in. Run `grid login` to sign in.")
    prev_count = len(data.get("networks") or [])
    device_id = credentials.device_id()
    api_url = credentials.api_url()
    try:
        raw = control_plane.fetch_tokens(session_token, device_id, api_url)
    except SystemExit as exc:
        # An expired/invalid session is the expected failure here (you haven't re-logged in) — surface
        # it as an actionable message instead of control_plane._raise's raw request dump. Anything else
        # (a transport error, a 5xx) re-raises unchanged.
        if _SESSION_EXPIRED_RE.match(str(exc)):
            raise SystemExit(
                "Your grid session has expired. Run `grid login` to sign in again."
            ) from exc
        raise
    # Validate outside the try: a malformed bundle is a data error, not a session error, so it must
    # surface as-is (never rewritten to "session expired").
    networks = _validated(raw)
    # Authoritative overwrite of the stored grid list; session_token / api_url / user are preserved.
    # Immutable update — a fresh dict, never the loaded one mutated in place. state.json is untouched.
    credentials.save_credentials({**data, "networks": networks})
    if prev_count and not networks:
        # The overwrite just cleared every grid. Make the wipe visible so a transient backend hiccup
        # isn't mistaken for a silent loss of all credentials.
        print(
            f"Warning: the control plane returned 0 grids; {prev_count} previously synced grid(s) "
            "were cleared locally. Re-run `grid sync` if this may be transient.",
            file=sys.stderr,
        )
    return _report_sync(networks, as_json=as_json)


def _report_sync(networks: list[dict[str, Any]], *, as_json: bool) -> int:
    if as_json:
        grids = [{"name": n["name"], "type": n.get("network_type")} for n in networks]
        print(json.dumps({"synced": True, "grids": grids}))
        return 0
    if networks:
        listed = ", ".join(n["name"] for n in networks)
        print(f"Synced {len(networks)} grid(s): {listed}.")
    else:
        print("Synced 0 grids.")
    return 0
