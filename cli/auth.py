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
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from shared import run_records

if TYPE_CHECKING:  # annotations only (PEP 563) — the runtime import stays lazy, in the handlers
    from . import os_grid_notice, signout


# Cap the server-supplied poll interval so a misbehaving/misconfigured control plane can't
# make the CLI appear frozen for far longer than the (already capped) sign-in deadline.
_MAX_POLL_INTERVAL_S = 30

# control_plane._raise formats failures as "<METHOD> <URL> failed (<status>): <body>". Match only the
# method-anchored prefix (re.match) so a 5xx whose body merely contains "failed (401):" can't be
# misclassified as an expired session.
_SESSION_EXPIRED_RE = re.compile(r"[A-Z]+ \S+ failed \((?:401|403)\):")


def cmd_login(args: argparse.Namespace) -> int:
    from remote import control_plane, credentials

    from . import os_grid_notice, signout

    as_json = getattr(args, "json", False)
    api_url = credentials.api_url()
    device_id = credentials.device_id()
    # Read BEFORE the save below replaces `[[networks]]` wholesale: signing in as a second account
    # drops every prior grid's bundle while its serve child keeps polling, and afterwards there is
    # nothing left to diff against (ADR 0023).
    previous_networks = list(credentials.load_credentials().get("networks") or [])

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
    fetched = control_plane.fetch_tokens(session_token, device_id, api_url)
    networks = _validated(fetched.networks)

    credentials.save_credentials({
        "session_token": session_token,
        "api_url": api_url,
        "user": user,
        "networks": networks,
    })
    signout.warn_stranded(previous_networks, networks)
    # Deliberately no `state.set_active("remote", …)` here — login never auto-selects a grid.
    return _report_login(user.get("email", ""), networks,
                         absence=os_grid_notice.absence(fetched.os_served), as_json=as_json)


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


def _report_login(email: str, networks: list[dict[str, Any]], *,
                  absence: os_grid_notice.OsGridAbsence | None = None, as_json: bool) -> int:
    if as_json:
        grids = [{"name": n["name"], "type": n.get("network_type")} for n in networks]
        print(json.dumps({"signed_in": True, "email": email, "grids": grids, "active": None,
                          "os_grid": absence.as_json() if absence else None}))
        return 0
    if networks:
        listed = ", ".join(n["name"] for n in networks)
        print(f"Signed in as {email}. {len(networks)} grid(s) available: {listed}.")
        print("Run `grid use <name>` to pick one.")
    else:
        print(f"Signed in as {email}. You don't belong to any grids yet.")
    _print_os_grid_absence(absence)
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    """Stop serving, then sign out — in that order (ADR 0023).

    Signing out used to be a pure credential delete, which stranded every detached serve child on this
    box: they hold their per-grid access token in memory from spawn (TTL ≈ 1 year), so they kept
    registering and heartbeating as a provider, while the deleted store was simultaneously the only
    index of which grids this box knows *and* the gate on ``grid leave``. So the teardown runs while the
    token that makes its deregister authoritative still exists, and the delete happens after.
    """
    from remote import credentials
    from shared import state

    from . import signout

    data = credentials.load_credentials()
    result = signout.stop_serving(list(data.get("networks") or []),
                                  session=str(data.get("session_token") or ""))
    outcomes = result.outcomes
    # A grid we still hold a token for and could not prove is stopped keeps its credentials: they are
    # the handle a retried `grid leave` needs to deregister it authoritatively. A grid whose bundle is
    # already gone has no such handle to preserve, so it never blocks — `grid leave <id>` works without
    # one. Neither the store NOR the active pointer is touched on this path: a cleared pointer over live
    # credentials is a half-state that no command reports.
    forcing = getattr(args, "force", False)
    blocked = [outcome for outcome in outcomes if outcome.bundled and not outcome.ok]
    # Everything the sign-out is *walking away from* is named BEFORE the refusal below, never after it.
    # An unbundled grid never blocks, so raising first would hide its survivor entirely on this attempt:
    # the operator would clear the block, retry, and meet a second failure nobody had mentioned. The
    # blocked grids themselves are excluded here — their credentials are about to be **kept**, so
    # `_warn_unreaped`'s "signing out removed the credentials" would be false for them; they are named
    # by the refusal instead, and only join this list when `--force` really does walk away from them.
    _warn_unreaped([o for o in outcomes if not o.ok and not o.bundled] + (blocked if forcing else []))
    _warn_unscanned(result.unscanned)
    # Every outcome, not only the failed ones: a grid can be torn down perfectly and still have a
    # sibling child this account may not touch, which is precisely the case that used to go unsaid.
    _warn_unstoppable(outcomes)
    if blocked and not forcing:
        raise SystemExit(_signout_blocked_message(blocked))
    existed = credentials.clear_credentials()
    state.set_active("remote", None)  # a cleared session has no active grid
    return _report_logout(existed, outcomes, as_json=getattr(args, "json", False))


def _signout_blocked_message(blocked: list[signout.SignoutOutcome]) -> str:
    """Why the sign-out stopped, naming what is still running and how to get past it.

    Mirrors ``grid leave``'s honest teardown rather than inventing a second dialect: never a success
    line over a live process, always the pid, always a command the operator can actually run.
    """
    parts: list[str] = []
    for outcome in blocked:
        # `unchecked` is read rather than inferred from an empty `survivors`. The two coincide only
        # while a grid has exactly one run record; a legacy directory with several could name a
        # confirmed survivor AND fail to verify another, and inferring would drop the second caveat.
        if outcome.survivors:
            named = ", ".join(run_records.describe_target(value) for value in outcome.survivors)
            parts.append(f"{outcome.label}: still serving on {named}")
        if outcome.unchecked:
            parts.append(f"{outcome.label}: couldn't verify what this box is serving")
    listed = "; ".join(parts)
    return (
        f"grid logout: kept your credentials because a serve child is still running — {listed}. "
        "Your grid tokens are the only handle that can deregister it, so they were not deleted. "
        "Run `grid leave` to retry the teardown, or `grid logout --force` to sign out anyway."
    )


def _warn_unreaped(unreaped: list[signout.SignoutOutcome]) -> None:
    """Name every serve child the sign-out is walking away from, and how to reap it afterwards.

    Reached two ways — a forced sign-out, and a grid whose bundle an earlier overwrite already
    dropped — and the remedy has to survive both: ``grid leave <grid-id>`` works with no credentials
    for that grid at all, which is precisely why it may be printed after the store is deleted.
    """
    for outcome in unreaped:
        named = ", ".join(run_records.describe_target(value) for value in outcome.survivors)
        still = f"still serving on {named}" if named else "may still be serving"
        print(
            f"Warning: {outcome.label} {still} on this box, and signing out removed the credentials "
            f"that could deregister it. Run `grid leave {outcome.network_id}` to stop it.",
            file=sys.stderr,
        )


def _warn_unscanned(unscanned: tuple[signout.UncheckedGrid, ...]) -> None:
    """Say which grids the sign-out could establish **nothing** about, and what it did anyway.

    Reached when the process table was unreadable and a grid had no live record: a record-less orphan
    lives only in that table, so this is "no evidence", not "no child". Deliberately not a refusal —
    a box with several stale record directories would then be unable to sign out at all, for a
    condition signing out cannot fix.

    Split by whether the deregister landed, because the two leave the operator in different places. A
    grid that was told stops listing this box immediately even if a child is still running; one that
    could not be told (no bundle, or a rejected token) is waiting on the ~120s node TTL. The remedy is
    real in both cases only because the rescue path shipped alongside this: `grid leave <grid-id>` runs
    with no credential store, so it still works after the delete below.
    """
    for grid in unscanned:
        told = (
            "it has been deregistered, so that grid already stopped listing this box"
            if grid.deregistered
            else "nothing could tell that grid, so anything left drops after the node TTL (~120s)"
        )
        # Three ways to have no evidence, and none of them shares a remedy. An unreadable table clears
        # on its own once processes are listable; a table read but mostly hidden looks identical on
        # every retry from this account, so the only useful instruction is to elevate; and an
        # unparseable record names a FILE the operator can act on — the one case with a fix in their
        # hands, so it says which file rather than which permission.
        if grid.unreadable:
            blind = f"couldn't read this grid's run record ({grid.unreadable})"
            remedy = "Once that file is removed or repaired"
        elif grid.partial:
            blind = ("couldn't see all of the process table (most command lines were hidden from this "
                     "account)")
            remedy = "From an elevated shell"
        else:
            blind = "couldn't read the process table"
            remedy = "Once processes are listable"
        print(
            f"Warning: {blind}, so an untracked serve child could not be ruled out for "
            f"{grid.label} — {told}. {remedy}, `grid leave {grid.network_id}` reaps one; "
            "it needs no sign-in.",
            file=sys.stderr,
        )


def _warn_unstoppable(outcomes: list[signout.SignoutOutcome]) -> None:
    """Name what the teardown found and was **not permitted** to finish — a serve child of that grid
    owned by another user, or a process table it was shown only part of (grid-leave issue 15).

    The concrete shape is mundane, not adversarial: `sudo grid join` followed by an unprivileged
    `grid logout` over a shared `GRID_HOME` is the SAME node_id. The backstop's consumer flip stands
    (the master's heartbeat never writes `role`), so the grid stops listing the box — but the
    root-owned child keeps the engine, the port and the token it loaded at startup, and the
    credentials that could address it again have just gone. If the backstop *degraded* instead of
    landing, the node was never flipped and that child's heartbeats keep it advertised indefinitely.

    A warning and never a refusal, for the same two reasons `_warn_unscanned` is: another operator's
    node is not ours to kill, and a box that hosts one would otherwise be unable to sign out at all.
    Neither condition reaches `ok`, so neither can block.

    Worded so it stays true whichever way the sign-out went — a blocked grid keeps its credentials,
    so "signing out removed them" (which `_warn_unreaped` may say) would be false here. `grid leave
    <grid-id>` is the remedy because it needs no sign-in and therefore still works after the store is
    deleted; elevated, because that is the thing this run lacked.
    """
    for outcome in outcomes:
        if outcome.foreign:
            pids = ", ".join(str(pid) for pid in outcome.foreign)
            print(
                f"Warning: {outcome.label} still has a serve process on this box owned by another "
                f"user (pid {pids}), which signing out could not stop. Run "
                f"`grid leave {outcome.network_id}` from an elevated shell to stop it.",
                file=sys.stderr,
            )
        if outcome.partial:
            print(
                f"Warning: couldn't see all of the process table, so a serve child for "
                f"{outcome.label} owned by another user could not be ruled out. Run "
                f"`grid leave {outcome.network_id}` from an elevated shell to be sure.",
                file=sys.stderr,
            )


def _report_logout(existed: bool, outcomes: list[signout.SignoutOutcome], *, as_json: bool) -> int:
    """The sign-out's own line, plus what it stopped on the way out.

    A teardown that could not deregister has already printed its own stderr caveat (the ~120s node TTL
    is the fallback), so this never claims the grid was told — it says what was stopped here.
    """
    stopped = [outcome for outcome in outcomes if outcome.ok]
    if as_json:
        print(json.dumps({
            "signed_out": existed,
            "stopped": [
                {"grid": outcome.label, "deregistered": outcome.sent} for outcome in stopped
            ],
        }))
        return 0
    for outcome in stopped:
        if outcome.bundled:
            print(f"Stopped serving {outcome.label}.")
        else:
            # No bundle: the reap landed but nothing could address the relay, so say which fallback
            # the grid is relying on instead of implying the model has already dropped.
            print(f"Stopped an untracked serve child for {outcome.label}; "
                  "that grid drops it after the node TTL (~120s).")
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

    from . import os_grid_notice, signout

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
    networks = _validated(raw.networks)
    # Authoritative overwrite of the stored grid list; session_token / api_url / user are preserved.
    # Immutable update — a fresh dict, never the loaded one mutated in place. state.json is untouched.
    credentials.save_credentials({**data, "networks": networks})
    # A grid that just dropped out takes its token with it while its serve child keeps polling — the
    # same unreachable state a logout produces, one grid at a time (ADR 0023). Sync does not tear it
    # down; it names it and the verb that still reaches it.
    signout.warn_stranded(list(data.get("networks") or []), networks)
    if prev_count and not networks:
        # The overwrite just cleared every grid. Make the wipe visible so a transient backend hiccup
        # isn't mistaken for a silent loss of all credentials.
        print(
            f"Warning: the control plane returned 0 grids; {prev_count} previously synced grid(s) "
            "were cleared locally. Re-run `grid sync` if this may be transient.",
            file=sys.stderr,
        )
    return _report_sync(networks, absence=os_grid_notice.absence(raw.os_served), as_json=as_json)


def _report_sync(networks: list[dict[str, Any]], *,
                 absence: os_grid_notice.OsGridAbsence | None = None, as_json: bool) -> int:
    if as_json:
        grids = [{"name": n["name"], "type": n.get("network_type")} for n in networks]
        print(json.dumps({"synced": True, "grids": grids,
                          "os_grid": absence.as_json() if absence else None}))
        return 0
    if networks:
        listed = ", ".join(n["name"] for n in networks)
        print(f"Synced {len(networks)} grid(s): {listed}.")
    else:
        print("Synced 0 grids.")
    _print_os_grid_absence(absence)
    return 0


def _print_os_grid_absence(absence: os_grid_notice.OsGridAbsence | None) -> None:
    """Say why there is no OS grid in the list, on the human path only (ADR 0039 D-k).

    Stdout beside the command's own report, not stderr: this is part of the answer to what was asked,
    not a warning that something went wrong — and it never is. ``--json`` never reaches here; the same
    fact rides the ``os_grid`` field instead, so stdout stays parseable and a script sees what a
    person sees. ``None`` — an OS grid you have, or a control plane too old to have said — prints
    nothing at all, which is this whole feature's ordinary case.
    """
    if absence is not None:
        print(absence.line())
