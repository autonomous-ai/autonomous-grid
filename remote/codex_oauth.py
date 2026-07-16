"""The vendor OAuth protocol behind `grid join --api codex` (ADR 0015 D-c).

A codex API engine is a ChatGPT/Codex subscription seat, so its credential is an OAuth bundle
rather than an ``sk-...`` key. The grid runs the authorization itself — PKCE, public client, no
client secret — against the vendor's auth service, and never reads or writes ``~/.codex/auth.json``
(adopting the real Codex CLI's bundle would double-spend its single-use refresh token and revoke the
operator's seat).

**Protocol only: nothing here prompts, opens a browser, or waits on a human.** The browser/paste
choice, the deadline, and every operator-facing message live in ``cli/codex_signin.py``; the one-shot
callback listener lives in ``remote/codex_callback.py``. That separation is what lets the sign-in
flow be tested without a terminal and this module without a browser.

The boundary is about *interaction*, not output — one deliberate exception proves the distinction.
``exchange_code`` writes a single diagnostic line to stderr when a freshly-exchanged token cannot be
decoded: its ``.reason``, a closed-vocabulary constant that provably cannot carry a token value out.
It stays here rather than moving to the CLI for a reason that outlives this issue — ADR 0015 D-d's
refresh will hit the identical case from the **serve loop**, which never imports ``cli/``, so a CLI-
side log would have to be duplicated there or lost. A ``remote/`` module logging to stderr is also
this package's own habit (``remote/serve.py`` does it throughout). Nothing waits on that line and no
test needs a terminal to read it, so it costs the module none of its testability.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from . import codex_auth

# The exchange is one round-trip on a human-driven path: the operator is watching a terminal, so a
# hung socket must surface, not hang. Matches `_VENDOR_LIST_TIMEOUT`, the other vendor call at join.
_EXCHANGE_TIMEOUT = 15.0

# --- Vendor constants -------------------------------------------------------------------------
# Verified 2026-07-15 by an offline scan of the real client
# (`/Applications/Codex.app/Contents/Resources/codex`, codex-cli 0.144.2) cross-checked against
# `l2aas-be/docs/codex-responses-impl.md`. None of them is discoverable at runtime — the vendor
# publishes no metadata document for this client — so they are pinned here and re-verified by hand.

# The vendor's public client id for the Codex CLI. Public by design: a PKCE client has no secret,
# which is why `generate_pkce` exists. Two independent sources agree (the binary's
# `CODEX_APP_SERVER_LOGIN_CLIENT_ID` and the vendor doc's refresh-grant body).
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"

# The port the one-shot callback listener binds, and the redirect uri's port.
# **UNVERIFIED — deferred by decision.** The value is not in the client binary (a scan for `1455`
# found only crate line numbers), and settling it would need a live authorization against the
# vendor, which this feature does not spend. It is mocked in every test and only has to be right at
# issue 07's live E2E. It matters because it must match the redirect uri the vendor has registered
# for CLIENT_ID: a wrong port fails the sign-in at the vendor's own consent screen, loudly, before
# anything is stored. That port is also the real Codex CLI's — see `remote/codex_callback.py` for
# why the listener binds-and-catches rather than probing first.
CALLBACK_PORT = 1455

# `binary:` a literal `http://localhost:{}/auth/callback` format string in the client's login path.
CALLBACK_PATH = "/auth/callback"

# `offline_access` is the load-bearing scope: it is what earns the refresh token, without which the
# seat dies at the first access-token expiry and ADR 0015 D-d's rotation has nothing to rotate. The
# real client also asks for `api.connectors.read api.connectors.invoke`; we don't call connectors,
# so we don't ask — a narrower grant for the same seat.
SCOPE = "openid profile email offline_access"

# Which client the vendor thinks it is talking to. `binary:` the string `codex_cli_rs` lives in the
# client's own `src/auth/default_client.rs`, ~10KB from the authorize-parameter table; `doc:` the
# vendor doc hardcodes `Originator: codex_cli_rs` on the inference call. We send the vendor's own
# client id, so claiming a different originator would describe a client that does not exist.
ORIGINATOR = "codex_cli_rs"


def redirect_uri() -> str:
    """Where the vendor sends the operator's browser after they approve.

    Registered against CLIENT_ID at the vendor, so the string must match theirs exactly — it is not
    ours to choose (`binary:` `http://localhost:{}/auth/callback`). `localhost`, not `127.0.0.1`.
    """
    return f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"


def build_authorize_url(*, state: str, challenge: str) -> str:
    """The URL the operator approves in a browser.

    ``state`` is the anti-injection control (ADR 0015 D-c): it round-trips through the vendor and
    `parse_redirect` refuses any redirect that doesn't carry back the exact value generated here, so
    a redirect URL from an attacker's own authorize session cannot be pasted into this sign-in.
    """
    return f"{AUTHORIZE_URL}?" + urlencode(
        {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": redirect_uri(),
            "scope": SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",  # never "plain": the challenge must not be the secret
            # The vendor keys its simplified Codex consent screen off these two; without them the
            # same client id gets the generic OAuth flow.
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "originator": ORIGINATOR,
            "state": state,
        }
    )


@dataclass(frozen=True)
class CodexBundle:
    """The stored credential for a codex seat — the OAuth analogue of openai's one ``sk-...`` string.

    ``account_id`` and ``plan_type`` are *derived* from ``access_token``, not returned beside it: the
    exchange response carries only ``id_token``/``access_token``/``refresh_token``. They are stored
    rather than re-derived per use so the serve loop's forward path spends no decode per job.
    """

    # The Bearer on every forward. `repr=False` on both tokens: issue 04 bars a token from reaching
    # any log, terminal or run record, and a dataclass's default repr would put them in the first
    # `logger.debug(f"{bundle}")` anyone writes.
    access_token: str = field(repr=False)

    # Single-use and rotating (ADR 0015 D-d): spending it twice revokes the operator's whole seat,
    # including their real Codex CLI. The most dangerous string in this file.
    refresh_token: str = field(repr=False)

    # The `Chatgpt-Account-Id` header on every forward. Not a token, but it is the operator's
    # account identity — held to the same bar (see `codex_auth.CodexSeat.account_id`).
    account_id: str = field(repr=False)

    # The seat's subscription tier, verbatim from the token's claim; None when the token doesn't say,
    # which ADR 0015 D-f answers with the minimal whitelist. Not secret.
    plan_type: str | None

    # POSIX seconds at which this bundle was obtained — wall clock, not monotonic, because it
    # outlives the process. ADR 0015 D-d's proactive refresh fires on it when the token's own `exp`
    # is missing, and on the vendor's rotation window regardless.
    last_refresh: int


def exchange_code(code: str, verifier: str) -> CodexBundle:
    """Spend the one-time authorization ``code`` for a seat's token bundle.

    Form-encoded, per the vendor's authorization_code grant. **Issue 06's refresh grant is JSON** —
    the same endpoint, a different encoding; unifying them 400s. No ``client_secret``: this is a
    public client and ``verifier`` is the proof of possession (see `generate_pkce`).
    """
    try:
        with httpx.Client(timeout=_EXCHANGE_TIMEOUT) as client:
            resp = client.post(
                TOKEN_URL,
                # `data=` is form-encoded; `json=` would be the refresh grant's shape. Not a style
                # choice — the vendor accepts exactly one of them per grant type.
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri(),
                    "client_id": CLIENT_ID,
                    "code_verifier": verifier,
                },
            )
    except httpx.HTTPError as exc:
        raise SystemExit(f"Could not reach the sign-in service at {TOKEN_URL}: {exc}") from None

    payload = _token_payload(resp)
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    if not isinstance(access_token, str) or not isinstance(refresh_token, str):
        raise SystemExit(_INCOMPLETE_GRANT)

    # The account id and the tier exist ONLY inside the access token — the exchange returns neither.
    # A token we cannot read is a seat we cannot serve, so this is terminal rather than degraded.
    try:
        seat = codex_auth.decode_seat(access_token)
    except codex_auth.CodexTokenError as exc:
        # `CodexTokenError`'s own message ends "…sign in again", which is right for a token read
        # back off the disk and WRONG here: this token is seconds old, so the operator just did sign
        # in and doing it again reproduces this exactly. A fresh token we can't read means the
        # vendor changed the token's shape (issue 04's amendment). It is also a ValueError, and
        # `cli/_main.py`'s `main` has no handler for one — letting it out is a traceback.
        # `.reason` is logged because it is the only signal separating a vendor rename from a
        # corrupt token, and it is safe to log: `REASONS` is a closed vocabulary of constants, so it
        # cannot carry a value out of a claim set that provably holds the operator's email.
        print(f"codex sign-in: unreadable access token ({exc.reason})", file=sys.stderr)
        raise SystemExit(
            "The sign-in worked, but the token the vendor returned carries no seat identity this "
            "version of grid can read. Nothing was saved. Signing in again will not help — check "
            "for a newer grid release."
        ) from None
    return CodexBundle(
        access_token=access_token,
        refresh_token=refresh_token,
        account_id=seat.account_id,
        plan_type=seat.plan_type,
        last_refresh=int(time.time()),
    )


# A 200 whose body isn't a usable grant is a vendor contract change, not an operator mistake — say
# so, rather than sending them round a sign-in loop that cannot fix it.
_INCOMPLETE_GRANT = (
    "The sign-in service returned a token response this version of grid can't read (no access or "
    "refresh token). Nothing was saved. This usually means the vendor changed the sign-in contract "
    "— check for a newer grid release."
)


def _token_payload(resp: httpx.Response) -> dict[str, object]:
    """The token endpoint's JSON object, or a terminal error naming what went wrong.

    Never echoes the body: a 200's body holds the tokens themselves, and even an error body on this
    endpoint is vendor text of unbounded shape.
    """
    if resp.status_code != 200:
        raise SystemExit(
            f"The sign-in service rejected this sign-in (HTTP {resp.status_code}). Nothing was "
            "saved. Re-run `grid join --api codex` to try again — an authorization code is "
            "single-use and expires within minutes, so a slow paste is the usual cause."
        )
    try:
        payload = resp.json()
    except ValueError:
        raise SystemExit("The sign-in service returned a malformed token response (not JSON).") from None
    if not isinstance(payload, dict):
        raise SystemExit(_INCOMPLETE_GRANT)
    return payload


# --- The refresh grant (ADR 0015 D-d, issue 06) --------------------------------------------------

# One vendor round-trip on the serve loop's refresh path; a hung socket must surface as an error the
# loop can classify, not hang a poll worker. Mirrors `_EXCHANGE_TIMEOUT`, the sign-in's own bound.
_REFRESH_TIMEOUT = 15.0


# Why a refresh grant was definitively refused. A CLOSED vocabulary (the `CodexTokenError`
# pattern): "grant-rejected" = a definitive 4xx; "unusable-grant" = a 200 whose body carries no
# usable tokens (the vendor *processed* the grant, so the old refresh token is spent either way).
_REFUSED_REASONS = frozenset({"grant-rejected", "unusable-grant"})


class RefreshRefused(Exception):
    """The token service answered the refresh grant and said no — the stored rotation is dead and
    only a fresh sign-in can mint a new one.

    ``reason`` comes from ``_REFUSED_REASONS`` and is ENFORCED at construction, not trusted (the
    `CodexTokenError` pattern): the whole point of a closed vocabulary is that ``str(exc)`` is
    provably safe to log, and a future raise site passing vendor-derived text would silently
    defeat that. Never carries the body: a 200's body IS the tokens, and an error body is vendor
    text of unbounded shape.
    """

    def __init__(self, status_code: int, reason: str) -> None:
        if reason not in _REFUSED_REASONS:
            # Report the SHAPE, never the value — if this fires for its real purpose (a
            # regression passing vendor-derived text), interpolating it would leak what it caught.
            raise ValueError(
                f"RefreshRefused reason must be one of _REFUSED_REASONS; got a "
                f"{type(reason).__name__}"
                + (f" of length {len(reason)}" if isinstance(reason, str) else "")
            )
        super().__init__(f"HTTP {status_code} ({reason})")
        self.status_code = status_code
        self.reason = reason


class RefreshUnavailable(Exception):
    """The refresh grant could not be CONCLUDED — transport failure, vendor 5xx, or rate limiting.

    Says nothing about the seat: the grant may or may not have been spent. ``request_sent=False``
    means the request provably never left this machine (connect failed), so the caller may clear
    any journal it wrote — nothing was spent. Never carries the body.
    """

    def __init__(
        self, detail: str, *, status_code: int | None = None, request_sent: bool = True
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.request_sent = request_sent


def refresh_bundle(old: CodexBundle) -> CodexBundle:
    """Spend ``old``'s single-use refresh token for a rotated bundle (ADR 0015 D-d).

    **JSON-encoded, unlike ``exchange_code``'s form encoding** — the same endpoint takes two grants
    in two encodings (facts.md #9); unifying them 400s one of the two. One attempt, no transport
    retry: a retry after an ambiguous failure would re-present a possibly-spent single-use token
    (facts.md #6 — ``refresh_token_reused`` is a permanent vendor failure).
    """
    try:
        with httpx.Client(timeout=_REFRESH_TIMEOUT) as client:
            resp = client.post(
                TOKEN_URL,
                # `json=` is the refresh grant's shape; `data=` would be the exchange's. Not a
                # style choice — the vendor accepts exactly one encoding per grant type.
                json={
                    "client_id": CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": old.refresh_token,
                },
            )
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        # The request never left this machine — nothing was spent (`request_sent=False`).
        raise RefreshUnavailable(
            f"could not reach the sign-in service ({type(exc).__name__})", request_sent=False
        ) from None
    except httpx.HTTPError as exc:
        raise RefreshUnavailable(f"transport failure mid-grant ({type(exc).__name__})") from None

    status = resp.status_code
    if status != 200:
        # A definitive 4xx is the vendor processing the grant and saying no. 408/429 are excluded
        # on purpose — timeout/rate noise says nothing about the grant — and everything else
        # (5xx, 3xx: httpx follows no redirects here) lands in the retry-later bucket, so no
        # status can fall outside the taxonomy.
        if 400 <= status < 500 and status not in (408, 429):
            raise RefreshRefused(status, "grant-rejected")
        raise RefreshUnavailable(
            f"the sign-in service is unavailable (HTTP {status})", status_code=status
        )
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    access_token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(access_token, str) or not access_token:
        # The vendor said 200 — it PROCESSED the grant — but returned nothing we can use. The
        # single-use token is spent either way, so this is a refusal (sign-in-again), not a
        # retry-later: retrying would re-present a spent grant (facts.md #6).
        raise RefreshRefused(200, "unusable-grant")
    # No new refresh token beside the rotated access token means the old grant is still the live
    # one — carry it forward (the `_ServeState.refresh` pattern). Dropping it would leave nothing
    # to rotate; refusing the 200 would discard a rotation the vendor already performed. Guarded
    # for TYPE, not just truth: a non-string here (vendor drift) would persist silently and
    # surface as a baffling refusal on the NEXT rotation, far from where it appeared.
    new_refresh = payload.get("refresh_token")
    refresh_token = new_refresh if isinstance(new_refresh, str) and new_refresh else old.refresh_token
    try:
        seat = codex_auth.decode_seat(access_token)
        account_id, plan_type = seat.account_id, seat.plan_type
    except codex_auth.CodexTokenError as exc:
        # The deliberate asymmetry with `exchange_code` (terminal on the same failure): by now the
        # OLD refresh token is SPENT, so discarding the new tokens bricks the seat, and a fallback
        # identity EXISTS — the account id is stable per seat and the vendor honours the token
        # whether or not we can read it. Stale plan_type heals at the next decodable rotation.
        # `.reason` is the closed vocabulary — loggable; the token and its claims are not.
        print(
            f"codex refresh: unreadable rotated access token ({exc.reason}) — keeping the seat's "
            "stored identity",
            file=sys.stderr,
        )
        account_id, plan_type = old.account_id, old.plan_type
    return CodexBundle(
        access_token=access_token,
        refresh_token=refresh_token,
        account_id=account_id,
        plan_type=plan_type,
        last_refresh=int(time.time()),
    )


def parse_redirect(url: str, *, expected_state: str) -> str:
    """The authorization code carried by the vendor's redirect ``url``.

    ``url`` is untrusted input in both flows — the paste flow takes it from the operator's
    clipboard, and the browser flow from an unauthenticated request to a localhost port anything on
    the box can reach. Every failure is a ``SystemExit`` carrying an operator message, never a
    traceback, and no failure quotes the URL back.
    """
    # Enforced rather than trusted, because the failure would be SILENT and total:
    # `compare_digest("", "")` is True, so an empty expected_state turns the check below into
    # "accept any redirect that omits `state`" while still reading like a verification. A bug in a
    # caller, so a ValueError — no operator can act on it, and it must never be mistaken for the
    # operator-facing refusal. (`codex_auth` guards its own closed vocabulary the same way.)
    if not expected_state:
        raise ValueError("parse_redirect needs the state this sign-in generated; got an empty one.")

    query = parse_qs(urlsplit(url).query)

    # Checked BEFORE the code is even looked at: an injected redirect's code is real and its token
    # would be genuinely signed, so this comparison is the only thing standing between a foreign
    # authorize session and the operator's store.
    if not _state_matches(query, expected_state):
        raise SystemExit(
            "That redirect URL is from a different sign-in (its `state` isn't the one this command "
            "generated). Nothing was saved. Re-run `grid join --api codex` and use the URL it "
            "prints — never one from another source."
        )

    # A refusal (the operator clicked Deny, or the vendor rejected the grant) redirects with `error`
    # and no code. Reported before the missing-code check so it never degrades into the vaguer
    # "no code in that URL" — the causes differ and so does the operator's next move.
    error = _one(query, "error")
    if error:
        raise SystemExit(
            f"The sign-in was refused by the vendor ({_safe(error)}). Nothing was saved. "
            "Re-run `grid join --api codex` and approve the request to continue."
        )

    code = _one(query, "code")
    if not code:
        raise SystemExit(
            "That URL carries no authorization code. Nothing was saved. Paste the URL your browser "
            "ended up on after you approved (it looks like "
            f"`{redirect_uri()}?code=...`) — not the sign-in URL this command printed."
        )
    return code


def redirect_is_ours(url: str, expected_state: str) -> bool:
    """Whether ``url`` is the redirect for the sign-in that generated ``expected_state``.

    **Comparison only — never raises, never interprets, never decides.** It exists for the callback
    listener, which runs on a handler thread where a raise would be swallowed and surface to the
    operator as a hang. It answers one question: is this request worth ending the wait for?

    A filter, not the control. `parse_redirect` still makes the authoritative refusal on the main
    thread, and shares `_state_matches` with this — so the duplicate-parameter and non-ASCII guards
    cannot drift between the two readings of the same URL.
    """
    if not expected_state:
        return False  # cannot raise here; `listen()` rejects an empty state up front instead
    return _state_matches(parse_qs(urlsplit(url).query), expected_state)


def _state_matches(query: dict[str, list[str]], expected_state: str) -> bool:
    """Constant-time compare of the redirect's ``state`` against ours.

    Compared as **bytes**: `compare_digest` raises TypeError on a non-ASCII `str`, and this value is
    attacker-supplied. A missing state compares against `b""` and fails, which is why the empty
    `expected_state` guard at every entry point matters — `compare_digest(b"", b"")` is True.
    """
    received = _one(query, "state")
    return secrets.compare_digest((received or "").encode("utf-8"), expected_state.encode("utf-8"))


def _safe(value: str) -> str:
    """``value`` bounded for a terminal. It reaches here from a URL, so it is vendor text at best and
    operator-pasted text at worst; an unbounded echo would let it carry ANSI escapes or newlines into
    the operator's terminal and forge lines around our own message. OAuth error codes are short and
    printable (RFC 6749 §4.1.2.1), so anything else is not one."""
    return value if value.isprintable() and len(value) <= 64 else "unreadable error code"


def _one(query: dict[str, list[str]], name: str) -> str | None:
    """The single value of ``name``, or None when absent — or repeated.

    A repeated parameter is refused rather than resolved: `parse_qs` would hand back both values and
    picking one (first? last?) is precisely the ambiguity a smuggled `?state=ours&state=theirs`
    redirect would exploit, since our reading need not match the vendor's.
    """
    values = query.get(name) or []
    return values[0] if len(values) == 1 else None


def generate_pkce() -> Pkce:
    """A fresh PKCE verifier/challenge pair (RFC 7636, S256).

    32 random bytes is the RFC's own recommendation and base64url-encodes to 43 chars — the minimum
    legal verifier length, and every character is in the required unreserved set.
    """
    verifier = _b64url(secrets.token_bytes(32))
    return Pkce(verifier=verifier, challenge=_b64url(hashlib.sha256(verifier.encode("ascii")).digest()))


@dataclass(frozen=True)
class Pkce:
    """One sign-in's proof-of-possession pair. Single-use: a fresh pair per authorization."""

    # The secret half. Held in memory only, sent once at exchange, never stored or printed.
    # `repr=False` because PKCE's entire guarantee is that an attacker holding the authorization
    # code still cannot exchange it — and the code travels through a URL the `--no-browser` flow
    # puts on a terminal. A verifier in a log alongside that URL hands over both halves, so it is
    # kept out of the repr rather than trusted to every future caller (as `CodexSeat.account_id` is).
    verifier: str = field(repr=False)

    # The public half: base64url(SHA256(verifier)), padding stripped. Rides the authorize URL.
    challenge: str


def _b64url(raw: bytes) -> str:
    """base64url with the padding stripped — the encoding every OAuth/JWT field on this path uses."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
