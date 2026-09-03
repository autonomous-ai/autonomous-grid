"""Is the credential we are about to hand an app going to be accepted — and can we fix it if not?

ADR 0029. `grid launch` hands its access token to a *different, long-lived process* whose error
surface this CLI does not own: a rejected token surfaces at the app's first prompt, minutes later,
dressed as the vendor's own `authentication_error`, with nothing in it naming `grid`. So the token is
checked here, before hand-over, and repaired in place when it can be.

Two checks, because neither subsumes the other. The offline one reads the JWT's `exp` and is the only
thing that can refuse a token which is valid *now* and dies mid-session. The relay probe is the only
thing that can see an epoch bump, a revoked membership, a missing scope — or this machine's clock
being wrong. Between them sits **one** refresh, shared: whichever check asks for it first spends it.

Deliberately not shared with ``remote/serve._ServeState.refresh``, which does the same exchange for
the serve loop. That one must never die, is thread-locked against concurrent workers, and adopts a
token another process stored; this one is a one-shot that raises CLI-shaped ``SystemExit`` and speaks
to a person. Merging them would make each carry the other's concerns.
"""
from __future__ import annotations

import json
import sys
import time
from typing import Any

# How close to `exp` still counts as "refresh it now". Not `remote/serve._CODEX_EXPIRY_MARGIN`'s ten
# minutes: what is being protected here is a human work session, not a poll iteration, and a token
# with four minutes left passes every check and then dies at the user's third prompt. A day is 0.27%
# of a grid token's 365-day life, so it cannot cause a refresh that would not have happened anyway.
LAUNCH_EXPIRY_MARGIN_SECONDS = 24 * 3600

# The scope `POST /messages` requires. Read off the token only to *explain* a 403 — never to gate,
# which is the relay's job. The control plane grants the inference scopes as one bundle, so this is
# also what the probe's own `inference:models` implies.
_INFERENCE_SCOPE = "inference:create"


def ensure_usable(rec: dict[str, Any], label: str, token: str, base: str) -> str:
    """The access token to hand over — refreshed first if it needed it — or a clean ``SystemExit``.

    ``rec`` is a snapshot of the stored grid record, so the refreshed token is **returned**; a caller
    that re-reads ``rec`` afterwards would use the stale one.
    """
    token, refreshed, expiry_note = _repair_if_stale(rec, label, token)
    return _prove(rec, label, token, base, refreshed=refreshed, expiry_note=expiry_note)


# ---------------------------------------------------------------------------
# Layer 1 + 2: what the token says about itself, and the repair
# ---------------------------------------------------------------------------

def _repair_if_stale(rec: dict[str, Any], label: str, token: str) -> tuple[str, bool, str | None]:
    """``(token, refreshed, expiry_note)`` after the offline check has had its say.

    ``expiry_note`` is what we learned and could not act on — carried forward so that if the probe
    then refuses, the refusal can say *why* the token was already suspect instead of reporting a bare
    rejection.

    A token we cannot repair is **not** refused here even when `exp` has passed. The clock is the one
    input this check cannot verify (a VM resumed from a snapshot, a dead CMOS battery), and the probe
    is one round-trip away and authoritative — refusing on local evidence alone would take away a
    launch that works, which is precisely what ADR 0029 forbids.
    """
    exp = _token_expiry(token)
    now = time.time()
    if exp is None or exp - now > LAUNCH_EXPIRY_MARGIN_SECONDS:
        return token, False, None

    phrase = _expiry_phrase(exp, now)
    expired = exp <= now
    if not str(rec.get("refresh_token") or ""):
        # Nothing to exchange. Say so once — a token inside the margin with no way to renew it is a
        # launch that will stop working soon — and let the probe decide whether it works right now.
        _warn(f"grid {label}'s access token {phrase} and no refresh credential is stored for it; "
              f"run `grid login` to renew it.")
        return token, False, phrase

    try:
        fresh = _refresh(rec, label)
    except _RefreshFailed as failure:
        if not expired:
            # It still works. A control-plane hiccup must not cost a launch that would have succeeded.
            _warn(f"couldn't renew grid {label}'s access token ({failure.reason}); it {phrase}, "
                  f"launching anyway.")
            return token, False, phrase
        raise SystemExit(failure.refusal(label, phrase)) from None
    _note(f"Refreshed grid {label}'s access token (it {phrase}).")
    return fresh, True, None


class _RefreshFailed(Exception):
    """A refresh that did not produce a token, and enough about why to say the right thing.

    ``status`` is the control plane's, or ``None`` when it never answered — the difference decides
    whether the user is told to sign in again or told to try again later, and telling them the wrong
    one sends them to a browser for an outage.
    """

    def __init__(self, reason: str, *, status: int | None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status

    def refusal(self, label: str, phrase: str) -> str:
        lead = f"Grid {label}'s access token {phrase}, and it couldn't be renewed: {self.reason}"
        if self.status == 401:
            return f"{lead}\nRun `grid login` to sign in again."
        if self.status == 403:
            # The refresh credential is fine; the *membership* it names is not. What re-establishes a
            # membership is **not the same on every grid type**, so this may not promise an outcome:
            # on the types that predate OS grids it is a row somebody else controls and re-signing in
            # is a circle, while on an `os-community` grid (ADR 0039 D-e) there is no row at all —
            # membership is re-derived from the `os=` claim that only the token fetch sends, so
            # `grid sync` is the entire repair. Naming the cheap thing to try and what it means when
            # it does not work is true of both.
            #
            # ⚠️ **Deliberately not branched on the grid's type.** The stored record does carry a
            # `network_type`, but it is a snapshot from the last login/sync that nothing refreshes on
            # a token exchange — and keying the advice on it would put a fourth copy of the
            # `os-community` literal in this repository, which by decision holds none (see
            # `tests/test_os_grid_type_lockstep.py`). That copy would degrade silently: renamed at
            # the far end the branch simply stops firing and the inverted sentence is back, with
            # nothing red. A refusal `code` on the wire is worse still — exactly three are parsed
            # across these seams and keeping that count low is the contract.
            return (f"{lead}\nThat is about your membership of this grid, not your sign-in. Run "
                    f"`grid sync` to re-check it; if the grid still refuses afterwards the "
                    f"membership itself is gone, and signing in again will not bring it back.")
        return (f"{lead}\nNothing is wrong with your sign-in; the control plane could not mint a "
                f"token. Try again shortly.")


def _refresh(rec: dict[str, Any], label: str) -> str:
    """Exchange the stored refresh token for a fresh access token, persisting **both**.

    Persisting both is not tidiness. The control plane rotates the refresh credential on *every*
    exchange and the old one stops matching immediately, while ``update_network_tokens`` takes
    ``refresh_token`` as an **optional** argument — so omitting it destroys this machine's refresh
    credential permanently and silently, sending every later launch *and* the serve loop back to
    `grid login`. ``remote/serve._ServeState.refresh`` carries the same line for the same reason.
    """
    from remote import control_plane, credentials

    from . import remote_grid

    network_id = remote_grid._network_id(rec)  # the validated id, before it reaches a request path
    stored_refresh = str(rec.get("refresh_token") or "")
    try:
        bundle = control_plane.refresh_network_token(
            network_id=network_id, refresh_token=stored_refresh
        )
    except control_plane.ControlPlaneError as exc:
        raise _RefreshFailed(_reason(exc), status=exc.status) from None
    access = str(bundle.get("access_token") or "") if isinstance(bundle, dict) else ""
    if not access:
        # A 2xx that carried no token. Treated as a failure rather than trusted, because the
        # alternative is handing the app an empty bearer and calling it a repair.
        raise _RefreshFailed("the control plane returned no access token", status=None)
    credentials.update_network_tokens(
        network_id,
        access_token=access,
        # `or stored_refresh` for a control plane that chose not to rotate: never store an empty one.
        refresh_token=str(bundle.get("refresh_token") or stored_refresh),
    )
    return access


# ---------------------------------------------------------------------------
# Layer 3: what the grid says about the token
# ---------------------------------------------------------------------------

def _prove(
    rec: dict[str, Any],
    label: str,
    token: str,
    base: str,
    *,
    refreshed: bool,
    expiry_note: str | None,
) -> str:
    """Ask the relay whether it will accept this token, repairing once if the budget is unspent."""
    from remote import relay

    try:
        relay.check_credential(base, token)
        return token
    except relay.RelayUnauthorized:
        pass
    except relay.RelayError as exc:
        if exc.status == 403:
            raise SystemExit(_forbidden(label, token, exc)) from None
        # 429, 5xx, a timeout, no answer at all: nothing was learned about the token, so nothing is
        # taken away from the user (ADR 0029). Named, not swallowed — if the app then fails the same
        # way, this line is what makes that make sense.
        _warn(f"couldn't check grid {label}'s access token ({_reason(exc)}); launching anyway.")
        return token

    if refreshed:
        # The refresh already ran and the grid still says no. A second exchange would mint another
        # token carrying the same rejected membership, so it would only be a slower way to fail.
        raise SystemExit(
            f"Grid {label} rejected this machine's access token, including a freshly renewed one.\n"
            f"Run `grid login` to sign in again."
        )
    try:
        token = _refresh(rec, label)
    except _RefreshFailed as failure:
        raise SystemExit(failure.refusal(label, expiry_note or "was rejected by the grid")) from None
    _note(f"Refreshed grid {label}'s access token (the grid rejected the stored one).")

    try:
        relay.check_credential(base, token)
    except relay.RelayUnauthorized:
        raise SystemExit(
            f"Grid {label} rejected this machine's access token, and the renewed one too.\n"
            f"Run `grid login` to sign in again."
        ) from None
    except relay.RelayError as exc:
        if exc.status == 403:
            raise SystemExit(_forbidden(label, token, exc)) from None
        _warn(f"couldn't re-check grid {label}'s access token ({_reason(exc)}); launching anyway.")
    return token


def _forbidden(label: str, token: str, exc: Exception) -> str:
    """The refusal for a 403 — which of the two it is, decided from the token, not from the message.

    The relay answers 403 both for a token that lacks the inference scope and for a member who may no
    longer use the grid, and the remedies are different. Which one applies is read off the token's own
    ``scopes`` claim rather than by matching words in the relay's reply: a message is a response body,
    and keying user-facing advice on its wording is the failure this whole feature exists to remove.
    """
    from remote import credentials

    reason = _reason(exc)
    scopes = credentials.claims_from_token(token).get("scopes")
    if isinstance(scopes, list) and _INFERENCE_SCOPE not in scopes:
        return (
            f"Grid {label} accepted your sign-in, but this machine's token cannot run inference on "
            f"it: {reason}\n"
            # Named as roles, never as "ask the owner": the control plane's owner fallback synthesises
            # roles=["admin"], whose scopes carry no inference — so this can land in front of the very
            # person "the owner" would point at.
            f"Inference is granted by the `consumer` and `both` roles; this token carries neither."
        )
    return (
        f"Grid {label} refused this machine's access token: {reason}\n"
        f"You are signed in, but not as a member who may use this grid — `grid login` will not "
        f"change that."
    )


# ---------------------------------------------------------------------------
# Small shared pieces
# ---------------------------------------------------------------------------

def _token_expiry(token: str) -> int | None:
    from remote import credentials

    return credentials.token_expiry(token)


_UNITS = ((86400, "day"), (3600, "hour"), (60, "minute"))


def _expiry_phrase(exp: int, now: float) -> str:
    """"expired 3 days ago" / "expires in 6 hours" — the half of the message that makes it actionable.

    Without it the user is told a token is stale but not whether it went stale a year ago (a machine
    left alone) or is about to (a session they should not start yet), which are different problems.
    """
    delta = int(abs(exp - now))
    for size, unit in _UNITS:
        if delta >= size:
            count = delta // size
            amount = f"{count} {unit}{'s' if count > 1 else ''}"
            break
    else:
        amount = "less than a minute"
    return f"expired {amount} ago" if exp <= now else f"expires in {amount}"


def _reason(exc: Exception) -> str:
    """The readable half of a relay/control-plane failure.

    Both render as ``… failed (<status>): <body>``, and the body is usually FastAPI's
    ``{"detail": "…"}`` — the only part a person can act on. Unwrapped when it is exactly that, and
    otherwise handed over whole: a body we do not recognise is still evidence, and hiding it would
    leave the user with a refusal that names nothing.
    """
    text = str(exc)
    _, separator, body = text.partition("): ")
    if not separator:
        return text
    try:
        payload = json.loads(body)
    except ValueError:
        return text
    detail = payload.get("detail") if isinstance(payload, dict) else None
    return detail if isinstance(detail, str) and detail else text


def _note(message: str) -> None:
    """A line about something this command did. stderr, so `--print-env`'s stdout stays evaluable."""
    print(message, file=sys.stderr, flush=True)


def _warn(message: str) -> None:
    _note(f"Warning: {message}")
