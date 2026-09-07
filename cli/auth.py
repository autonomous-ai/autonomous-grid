"""`grid login` / `grid logout` / `grid sync` — remote-mode sign-in and credential refresh.

Remote-only: dispatch gates these to remote mode, so the handlers assume remote. `cmd_login` has
two doors and one tail: grid-src's browser device flow (start → poll), or `--harness`, which reads an
Autonomous account token off stdin and trades it for the same session in one call (ADR 0040). What
follows either — fetch tokens → validate → persist → warn about stranded grids — is one path, because
the only thing the flag changes is where the session token came from. `cmd_logout`
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
import shlex
import sys
import time
import webbrowser
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from shared import run_records

from .next_steps import print_next_steps

if TYPE_CHECKING:  # annotations only (PEP 563) — the runtime import stays lazy, in the handlers
    from . import os_grid_notice, signout


# Cap the server-supplied poll interval so a misbehaving/misconfigured control plane can't
# make the CLI appear frozen for far longer than the (already capped) sign-in deadline.
_MAX_POLL_INTERVAL_S = 30

# control_plane._raise formats failures as "<METHOD> <URL> failed (<status>): <body>". Match only the
# method-anchored prefix (re.match) so a 5xx whose body merely contains "failed (401):" can't be
# misclassified as an expired session.
_SESSION_EXPIRED_RE = re.compile(r"[A-Z]+ \S+ failed \((?:401|403)\):")

# The Autonomous account token arrives on a pipe this process does not control, so the read is
# bounded. Not a tuning knob — a real token is a couple of kilobytes — but a bound on a wedged or
# hostile writer, and the same one `cli.credential` puts on git's own request for the same reason.
_MAX_HARNESS_TOKEN_BYTES = 64 * 1024

# Reaching the bound is its OWN refusal, never a silent truncation. A prefix of a token is still a
# well-formed request, so sending one answers with a 401 about the person's account — a sentence
# blaming a credential that is in fact fine, for something this process decided locally.
_HARNESS_TOKEN_TOO_LONG = (
    f"grid login --harness: more than {_MAX_HARNESS_TOKEN_BYTES} bytes arrived on standard input "
    "with no end to the token, so nothing was sent. Check that `harness grid login` is what is "
    "writing to it."
)

# Every way the pipe can carry no usable token collapses to this one sentence. It names the command
# that produces the token because somebody who typed `grid login --harness` by hand has no other way
# to find out where the input was supposed to come from.
_NO_HARNESS_TOKEN = (
    "grid login --harness: no Autonomous account token on standard input. "
    "Run `harness grid login`, which signs in and hands the token to `grid` for you."
)

# The one refusal on that route that is not about the caller: the control plane predates it, which
# the rollout order makes a deployment window rather than a fault (the control plane ships first).
# The credential the person is holding is fine, so the sentence names the other door instead.
_OLD_CONTROL_PLANE = (
    "This control plane cannot sign you in with an Autonomous account token yet, so the hand-off "
    "from the harness cannot complete. Run `grid login` to sign in with a browser instead."
)


# The refusal when the control plane predates the revoke route — the one status this path reads,
# and the rollout order (control plane first) makes it a deployment window rather than a fault.
#
# ⚠️ The sentence says what is true of the CREDENTIALS and nothing wider. By the time this is
# raised the serve-child teardown has already run, so "nothing on this machine changed" would be
# false — somebody who read it that way would retry later and find their grids no longer served.
_OLD_CONTROL_PLANE_NO_REVOKE = (
    "This control plane cannot sign your other machines out yet, so nothing was signed out anywhere "
    "and your credentials on this machine were kept. Anything this box was serving has already been "
    "stopped; run `grid logout` to finish signing out here."
)

# The other half of the taxonomy, and the reason it is not a refusal: a 401 means this machine's own
# session was refused — already signed out from elsewhere, or expired — so it can revoke nothing
# and never will. Raising would leave `--everywhere` unable to sign this machine out at all, with a
# credential on disk that no retry can ever spend. The local sign-out is what is left, and it runs.
_SESSION_ALREADY_GONE = (
    "Warning: this machine's sign-in was refused, so nothing could be signed out elsewhere — it had "
    "already been signed out, or it expired. Signing out on this machine anyway."
)


def cmd_login(args: argparse.Namespace) -> int:
    from remote import control_plane, credentials
    from shared import state

    from . import os_grid_notice, signout

    as_json = getattr(args, "json", False)
    api_url = credentials.api_url()
    device_id = credentials.device_id()
    # Read BEFORE the save below replaces `[[networks]]` wholesale: signing in as a second account
    # drops every prior grid's bundle while its serve child keeps polling, and afterwards there is
    # nothing left to diff against (ADR 0023). The email is read from the same snapshot and for a
    # neighbouring reason — on the `--harness` path nobody types one, so a token from a different
    # Autonomous account would take this machine's grids away with nothing on screen naming the swap.
    stored = credentials.load_credentials()
    previous_networks = list(stored.get("networks") or [])
    previous_email = str((stored.get("user") or {}).get("email") or "")

    if getattr(args, "harness", False):
        approved = _harness_sign_in(api_url)
    else:
        approved = _browser_sign_in(args, api_url, as_json=as_json)
    session_token = approved.get("session_token")
    if not session_token:
        # Reached through either door, and a server regression through both: the browser sign-in was
        # approved, or the account token was accepted, and a token-less success is not something to
        # KeyError over after the person has already done their part.
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
    # Signing in is what "I want the hosted grids" looks like, so the mode follows the credentials
    # rather than being demanded up front (dispatch.SELF_SWITCHING). Done here, after the save: a
    # sign-in that times out or is denied never reaches this line and leaves the mode alone.
    switched = state.get_mode() != "remote"
    if switched:
        state.set_mode("remote")
    # Deliberately no `state.set_active("remote", …)` here — login never auto-selects a grid.
    return _report_login(user.get("email", ""), networks,
                         absence=os_grid_notice.absence(fetched.os_served),
                         replaced=previous_email, as_json=as_json, switched=switched)


def _browser_sign_in(args: argparse.Namespace, api_url: str, *, as_json: bool) -> dict[str, Any]:
    """The device-code flow: show the URL and code, open a browser, poll until the person approves."""
    from remote import control_plane

    started = control_plane.start_device_login(api_url)
    url = _device_login_url(started)
    _print_signin_prompt(url, started.get("user_code", ""), to_stderr=as_json)
    if not getattr(args, "no_browser", False):
        try:
            webbrowser.open(url)
        except (OSError, webbrowser.Error):
            pass  # headless box / no browser available — the URL + code are already printed
    return _await_approval(started, api_url)


def _harness_sign_in(api_url: str) -> dict[str, Any]:
    """Trade the Autonomous account token on stdin for a grid session — no URL, no wait (ADR 0040).

    Only the **404** is translated. It is the one refusal on that route that says nothing about the
    caller: the control plane predates the route, which the rollout order makes an ordinary
    deployment window, and `POST … failed (404): Not Found` reads as a verdict on a credential that
    is in fact fine. Every other refusal already carries the control plane's own remedy sentence, so
    it is re-raised untouched and unparsed — `ControlPlaneError` is a `SystemExit`, so it reaches the
    person exactly as written, and a fourth machine-read refusal code across these repositories
    would be a fourth thing a rewording could break in silence.
    """
    from remote import control_plane

    token = _read_harness_token()
    try:
        return control_plane.sign_in_with_harness_token(token, api_url)
    except control_plane.ControlPlaneError as exc:
        if exc.status == 404:
            raise SystemExit(_OLD_CONTROL_PLANE) from None
        raise


def _read_harness_token() -> str:
    """The Autonomous account token the harness piped in, or a `SystemExit` saying why there is none.

    **Stdin, never argv and never the environment.** An argument would put a live account credential
    into the process listing for every user on the machine, where it stays for the life of the call.

    Every unusable input becomes one sentence rather than a traceback, and there are two of them
    because the two say opposite things: nothing arrived, or too much did. Reaching the bound is
    **not** silently truncated — a prefix of a token is a well-formed request, and sending one buys a
    401 about the person's account in place of a local sentence about their pipe.

    Decoded **strictly** on purpose. `errors="replace"` would assemble a token out of U+FFFD and send
    it, trading a sentence about this machine for a 401 about their account; and the length is checked
    **before** the decode, so a bound reached mid-UTF-8-sequence is reported as the overrun it is
    rather than as "nothing arrived", which would be false.
    """
    raw = _piped_bytes()
    if raw is None:
        raise SystemExit(_NO_HARNESS_TOKEN)
    if len(raw) >= _MAX_HARNESS_TOKEN_BYTES:
        raise SystemExit(_HARNESS_TOKEN_TOO_LONG)
    try:
        token = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise SystemExit(_NO_HARNESS_TOKEN) from None
    if not token:
        raise SystemExit(_NO_HARNESS_TOKEN)
    return token


def _piped_bytes() -> bytes | None:
    """One bounded LINE off stdin, or `None` when there is nothing there to read. Never raises.

    A line, not the stream. `read()` on a pipe returns only at the cap or at **EOF**, so a writer
    that sends the token and then waits for this process — `spawn`, `stdin.write(token)`, await exit,
    which is the obvious shape for the harness half — would deadlock both sides forever, with no
    output and no timeout on either. `readline` returns on the newline as well, so the hand-off
    survives a writer that forgets to close, and the bound is unchanged.

    Read through `.buffer` when there is one — the same shape `cli.credential._read_request` reads
    git's request through, and for the same two reasons: a pipe carries bytes, and a stdin without a
    `.buffer` is a test double or an unusual embedding rather than an error.

    Three ways there is nothing to read, and all three are the caller's one sentence rather than a
    hang or a traceback: **no stdin at all** (`None`, which some embeddings hand a process — the
    fallback branch would otherwise `AttributeError` on it), a **terminal** (nobody is piping
    anything; a person typed the flag by hand, and the sentence naming `harness grid login` exists
    for exactly them — reached by a hang they have to guess Ctrl-D out of, it may as well not),
    and a **severed pipe** (`OSError` — the harness died mid-hand-off). `ValueError` covers
    `isatty()` on a stdin somebody already closed.
    """
    stdin = sys.stdin
    if stdin is None:
        return None
    isatty = getattr(stdin, "isatty", None)
    try:
        if isatty is not None and isatty():
            return None
        stream = getattr(stdin, "buffer", None)
        if stream is None:
            return (stdin.readline(_MAX_HARNESS_TOKEN_BYTES) or "").encode("utf-8", "surrogatepass")
        return stream.readline(_MAX_HARNESS_TOKEN_BYTES) or b""
    except (OSError, ValueError):
        return None


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
                  absence: os_grid_notice.OsGridAbsence | None = None, replaced: str = "",
                  as_json: bool, switched: bool = False) -> int:
    """Who is signed in, which grids they got, and — when it changed — whose account this replaced.

    The replacement is a line, never a refusal: `grid login` has never compared accounts, and making
    the hand-off the one door that argues about identity would be a divergence between two ways in
    that are meant to differ in nothing but where the token came from. The consequence is already
    carried by `signout.warn_stranded`, which names every grid the swap left a serve child polling.

    Said only when it actually changed — a line on every ordinary re-sign-in is a line people learn
    to skip past, and then it is not there on the day it matters.
    """
    swapped = f"Signed in as {email} (was {replaced})." if replaced and replaced != email else ""
    if as_json:
        # stdout is the JSON contract and gains no key for this, so the one thing a script cannot
        # re-derive — that this sign-in swapped the account under it — goes to stderr, beside the
        # sign-in prompt and the stranded-grid warnings that are already there.
        if swapped:
            print(swapped, file=sys.stderr)
        grids = [{"name": n["name"], "type": n.get("network_type")} for n in networks]
        print(json.dumps({"signed_in": True, "email": email, "grids": grids, "active": None,
                          "os_grid": absence.as_json() if absence else None}))
        return 0
    signed_in = swapped or f"Signed in as {email}."
    if switched:
        # Named, never silent: the mode is persisted state that changes what every later command
        # talks to, so a reader who did not ask for it is told it happened and how to undo it.
        print("This computer is now in remote mode (`grid mode local` switches back).")
    if networks:
        print(f"{signed_in} {len(networks)} grid(s) available:")
        _print_grid_list(networks)
        # Login deliberately selects nothing (see cmd_login), so the next steps are spelled out:
        # pick a grid, optionally contribute this computer to it, then use it.
        _print_next_steps(networks)
        print("Missing a grid you were just added to? Run `grid sync` to refresh the list.")
    else:
        print(f"{signed_in} You don't belong to any grids yet.")
        print_next_steps([
            ("grid sync", "refresh after someone adds you to a grid"),
            ("grid start <name>", "or create your own, then `grid use <name>`"),
        ])
    _print_os_grid_absence(absence)
    return 0


def _print_grid_list(networks: list[dict[str, Any]]) -> None:
    """The grids this account can reach, one per line — `grid ls` columns, aligned and marked the
    same way (``*`` on the active grid) so the list you get for free after login/sync reads like the
    list you get on demand."""
    from shared import state

    active = state.get_active("remote")
    names = [str(n.get("name") or "") for n in networks]
    width = max(len("GRID"), *(len(n) for n in names))
    print("")
    print(f"    {'GRID'.ljust(width)}   TYPE")
    marked = False
    for net, name in zip(networks, names):
        is_active = bool(active) and active in {net.get("network_id"), name}
        marked = marked or is_active
        print(f"  {'*' if is_active else ' '} {name.ljust(width)}   {net.get('network_type') or ''}")
    if marked:
        print("  * = active grid")


def _print_next_steps(networks: list[dict[str, Any]]) -> None:
    """The verbs that follow a grid list. The examples name a real grid — the active one when the
    pointer still resolves, else the first — so they can be pasted as printed."""
    from shared import state

    active = state.get_active("remote")
    names = [str(n.get("name") or "") for n in networks]
    example = shlex.quote(active if active in names else names[0])
    print_next_steps([
        (f"grid use {example}", "pick the grid to work with"),
        ('grid chat -m <model> "hello"', "start using it"),
        # How a coding agent reaches this grid: the exports point any OpenAI-dialect client
        # (opencode, codex, Cursor) at it. `grid info --env` itself says what to do with them.
        (f"grid info --env {example}", "point coding agents at it (opencode, codex, …)"),
        (f"grid join {example} --serve <model>", "optional: serve a model to it"),
    ])


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
    # AFTER the teardown and BEFORE the delete, and both halves of that are load-bearing. The
    # teardown's deregisters are authoritative only while the credentials exist, and the revoke is
    # authorized by the session token the delete is about to destroy — so this is the one point in
    # the sequence where both are still true. A failure that could succeed on a retry raises here,
    # leaving the credentials in place; one that never could does not (see `_revoke_everywhere`).
    session_token = str(data.get("session_token") or "")
    revoked = False
    if getattr(args, "everywhere", False) and session_token:
        revoked = _revoke_everywhere(session_token)
    existed = credentials.clear_credentials()
    state.set_active("remote", None)  # a cleared session has no active grid
    return _report_logout(existed, outcomes, as_json=getattr(args, "json", False), revoked=revoked)


def _revoke_everywhere(session_token: str) -> bool:
    """Take back every session this account holds, this machine's included (ADR 0040 D-e).

    Returns whether the revocation actually landed, because the caller has to report what happened
    rather than what was asked. Two statuses are read and nothing else is:

    - **404** — this control plane predates the route. The one refusal that says nothing about the
      caller, and the rollout order makes it an ordinary deployment window. It raises: the session
      is still good, and it is the handle that will work once the control plane catches up.
    - **401** — this machine's own session was refused: signed out from another machine already, or
      expired. It does NOT raise. Raising would mean `--everywhere` could never sign this machine
      out at all — the very credential it kept "so a retry can reach the route" is the one thing
      that will never be accepted again — so the person would have to guess to drop the flag. The
      local sign-out is all that is left to do, and it is still worth doing.

    Everything else already carries the control plane's own remedy sentence and is re-raised
    untouched and unparsed — `ControlPlaneError` is a `SystemExit`, so it reaches the person as
    written, and a fourth machine-read refusal code across these repositories would be a fourth
    thing a rewording could break in silence.
    """
    from remote import control_plane, credentials

    try:
        control_plane.revoke_sessions(session_token, credentials.api_url())
    except control_plane.ControlPlaneError as exc:
        if exc.status == 404:
            raise SystemExit(_OLD_CONTROL_PLANE_NO_REVOKE) from None
        if exc.status == 401:
            print(_SESSION_ALREADY_GONE, file=sys.stderr)
            return False
        raise
    return True


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


def _report_logout(
    existed: bool,
    outcomes: list[signout.SignoutOutcome],
    *,
    as_json: bool,
    revoked: bool,
) -> int:
    """The sign-out's own line, plus what it stopped on the way out.

    A teardown that could not deregister has already printed its own stderr caveat (the ~120s node TTL
    is the fallback), so this never claims the grid was told — it says what was stopped here.

    ``revoked`` is what `--everywhere` actually achieved rather than what was asked for: a machine
    that was not signed in has no session to revoke with, and reporting the flag instead of the
    outcome would tell somebody their other machines were signed out when nothing was sent. It rides
    the JSON on **every** logout, present and false, for the reason `stopped` does — a stable shape
    beats a key a reader has to tell "absent" from "false".
    """
    stopped = [outcome for outcome in outcomes if outcome.ok]
    if as_json:
        print(json.dumps({
            "signed_out": existed,
            "stopped": [
                {"grid": outcome.label, "deregistered": outcome.sent} for outcome in stopped
            ],
            "revoked_everywhere": revoked,
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
    if revoked:
        print("Every other machine signed in to this account was signed out too.")
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
        print(f"Synced {len(networks)} grid(s):")
        _print_grid_list(networks)
        _print_next_steps(networks)
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
