"""The codex seat's identity, read from its OAuth access token (ADR 0015).

A codex API engine is a ChatGPT/Codex subscription seat rather than a metered key, so its
credential is an OAuth bundle instead of an ``sk-...`` string. The access token is a JWT, and two
things the join needs are already inside it — no network call, no cost:

* **the account id**, which becomes the ``Chatgpt-Account-Id`` header on every forward, and
* **the plan type**, which is the seat's subscription tier (ADR 0015 D-f).

Verified against a live seat on 2026-07-15 (spike 01, ``.scratch/codex-subs/facts.md``): both live
under the vendor's namespaced ``https://api.openai.com/auth`` claim.

**The token is the ONLY source of the account id — do not go looking for a second one.** An earlier
version of this docstring said the claim's account id "is byte-identical to the ``account_id`` the
token exchange returns alongside it", which motivated a planned cross-check of "two independent
sources" at sign-in. Both halves were false, and the check was deleted rather than deferred
(``facts.md`` fact 9, ADR 0015 D-c):

* The exchange returns **three** fields — ``id_token``/``access_token``/``refresh_token`` — and no
  account id at all (``binary:`` ``struct TokenResponse with 3 elements``). The *persisted* struct
  has four (``struct TokenData with 4 elements``) because the vendor's client derives the account id
  from this very claim before writing it to disk.
* The "byte-identical" reading had compared the claim against ``~/.codex/auth.json`` — that client's
  own copy of that same claim. One value, read twice.

So a comparison would have been ``x == x``: incapable of failing, while reading like defence in
depth. What actually defends this path is the OAuth ``state`` verification at sign-in
(``remote/codex_oauth.py``), not any cross-check here.

**This module decodes; it does NOT verify.** The signature is never checked, so nothing here is an
authorization decision — and three things keep it that way:

1. *Nothing to trust.* ``CodexSeat`` carries no ``is_valid``/``verified``/bool field, so no caller
   can branch on it as though the token had been validated.
2. *Nothing to gain.* The token arrives from our own OAuth exchange into our own ``0o600`` store,
   so forging any claim first means winning write access to that store — at which point reading the
   refresh token and using the seat directly is a strictly easier attack than editing a claim.
   Forging ``plan_type`` only widens the model set we advertise to our own grid — the vendor then
   rejects the job, so the damage is self-inflicted 400s (and a small availability spillover onto
   any consumer who targeted this seat's ``codex:`` model), not privilege escalation. Forging
   ``account_id`` pairs our real signed bearer with a different account's id in the header; whether
   the vendor rejects that (re-deriving the account from the signature) or ignores it is **not
   something we verified** — the spike confirmed only that the two agree for an honest token, so
   we do not lean on the vendor here, we lean on the store's ``0o600`` and on ``account_id`` never
   being attacker-chosen. Forging ``exp`` skips a proactive refresh, which D-d's reactive
   401→refresh→retry then heals: **exp is a hint, not a deadline.**
3. *Nothing verification would buy.* The realistic attack is OAuth code injection — tricking the
   operator into pasting a redirect URL from the attacker's authorize session. That token is
   *genuinely signed*, so a signature check passes it. What defends that is the ``state``
   verification at sign-in, not cryptography here. Verification would add a JWKS network fetch to
   a decoder whose entire value is being offline.

The technique (split → restore padding → urlsafe decode → guard the shape) mirrors
``remote/serve.py``'s ``_node_id_from_token``, which reads our own relay token the same way and for
the same reason. The contracts differ deliberately: that one is best-effort and returns ``""``;
this one raises, because a seat we cannot identify cannot serve.

The decoder never reads the clock — see ``decode_seat``.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any

# `json.loads` legitimately returns None for the payload "null", so failure needs its own sentinel.
_UNDECODABLE = object()

# The vendor namespaces the seat's facts under a URL-shaped claim; they are NOT at the top level.
CODEX_AUTH_CLAIM = "https://api.openai.com/auth"

# One message for every failure. The operator's action is identical in all of them, and a per-case
# message would be a menu of ways to describe a corrupt blob nobody can act on differently — plus a
# standing temptation to interpolate the offending token into the text.
_UNREADABLE = (
    "This codex access token carries no seat identity; re-run `grid join --api codex` to sign in "
    "again."
)

# Why a token was unreadable, for a caller that wants to log something. A CLOSED vocabulary of
# constants: the reason can never carry a value out of the token, which is what makes it safe to
# log while `_UNREADABLE` stays the only thing an operator sees. The operator's action is the same
# for all of them; these distinguish *our* diagnoses, not their instructions. Without this, a
# vendor claim-rename is indistinguishable from a corrupt store — both say "sign in again", and
# signing in again reproduces the same failure. That is not hypothetical: spike 01 found the
# vendor's own documented shapes were already wrong in three places.
REASONS = frozenset(
    {
        "not-a-string",  # the caller handed us something that isn't a token at all
        "bad-segment-count",  # not header.payload.signature
        "undecodable-payload",  # segment 1 isn't base64url-encoded JSON
        "payload-not-an-object",  # valid JSON, but not a claims object
        "claim-missing",  # no namespaced auth claim (a vendor rename lands here)
        "account-id-unusable",  # claim present, but its account id can't be a header value
    }
)


class CodexTokenError(ValueError):
    """The access token cannot identify a seat.

    ``str()`` is always the same constant operator message: the token, its claims, and its account
    id must never reach a log, a terminal, or a run record (issue 04's acceptance criteria), and an
    exception message is the easiest place for one to leak. ``.reason`` carries a constant from
    ``REASONS`` for callers that want to log *why* — it is drawn from a closed set, so it cannot
    smuggle a value out, and it is deliberately absent from ``args`` so ``repr()`` stays clean too.
    """

    def __init__(self, reason: str) -> None:
        # Enforce the closed vocabulary rather than trust it. The whole leak-safety argument for
        # `.reason` is "it can never carry a value out of the token because it is one of these six
        # constants" — a future raise site that passed a token-derived string would silently defeat
        # that. A plain `raise`, not `assert` (which `-O` strips, taking the guarantee with it).
        if reason not in REASONS:
            # Report the SHAPE, never the value. If this guard ever fires for its real purpose — a
            # regression passing token-derived content as `reason` — interpolating the value would
            # make the tripwire itself leak what it caught. Type + length debugs a typo'd literal.
            shape = (
                f"str of length {len(reason)}"
                if isinstance(reason, str)
                else type(reason).__name__
            )
            raise ValueError(
                f"CodexTokenError reason must be one of REASONS; got a {shape}"
            )
        super().__init__(_UNREADABLE)
        self.reason = reason


@dataclass(frozen=True)
class CodexSeat:
    """What a codex access token says about its seat. Not a verification verdict — see the module
    docstring.

    Every field is **verbatim** from the token: nothing is normalized, and no claim is checked for
    authenticity. ``account_id`` is the one field checked for *shape* — it is spent as an HTTP
    header value, so an unusable one is refused rather than carried (see ``decode_seat``). That is
    a parser guarding its own output, not an authorization decision.
    """

    # The `Chatgpt-Account-Id` header on every forward. Required: no id, no seat.
    # `repr=False` because this is the operator's account identity and issue 04 holds it to the same
    # bar as the token itself — it may not reach a log, a terminal, or a run record. Without it the
    # dataclass's own repr prints it in full, so the first `logger.debug(f"joined {seat}")` anyone
    # writes would ship it. Kept out of the repr rather than trusted to every future caller.
    account_id: str = field(repr=False)

    # The subscription tier, verbatim (e.g. "free"). None when the token doesn't say — ADR 0015 D-f
    # then falls back to the minimal whitelist. Mapping a tier to a model set is policy and lives
    # with the catalog data, not here; this reports only what the token claimed. Not secret.
    plan_type: str | None

    # POSIX seconds from the `exp` claim, verbatim. Feeds D-d's proactive refresh. None when the
    # token doesn't say, which D-d handles by falling back to the last-refresh age. Not secret.
    expires_at: int | None


def decode_seat(access_token: str) -> CodexSeat:
    """The seat identified by ``access_token``.

    Raises ``CodexTokenError`` when the token cannot identify a seat; degrades a claim to ``None``
    when it only tunes behaviour and ADR 0015 already gives it a safe default. The split matters: a
    missing tier must not fail an otherwise-working seat's join, but a missing account id must.

    **Never reads the clock.** An expired token still decodes, because D-d's refresh has to read
    the account id back OUT of an expired token in order to rotate it — rejecting expired tokens
    here would brick precisely the case refresh exists for. Whether ``expires_at`` is in the past is
    the caller's decision.
    """
    claims = _payload(access_token)

    auth = claims.get(CODEX_AUTH_CLAIM)
    if not isinstance(auth, dict):
        raise CodexTokenError("claim-missing")

    # `account_id` is spent as an HTTP header value, so it is checked for header-safety, not just
    # truthiness: blank and control-char ids are refused. This is shape validation at a trust
    # boundary (the token is file content) — NOT authenticity validation, which this module refuses
    # on purpose. It matters because the forward path uses httpx, and httpx (unlike urllib) will
    # happily send a header value containing CRLF. Reaching here needs a forged token, i.e. write
    # access to the 0o600 store — so this is defence in depth, not a live hole.
    account_id = auth.get("chatgpt_account_id")
    if (
        not isinstance(account_id, str)
        or not account_id.strip()
        or not account_id.isprintable()
    ):
        raise CodexTokenError("account-id-unusable")

    plan_type = auth.get("chatgpt_plan_type")
    exp = claims.get("exp")
    return CodexSeat(
        account_id=account_id,
        plan_type=plan_type if isinstance(plan_type, str) else None,
        # `isinstance(True, int)` is True in Python, so an unguarded check would turn `exp: true`
        # into expires_at=1 — epoch 1970 — and refresh eagerly forever.
        expires_at=exp if isinstance(exp, int) and not isinstance(exp, bool) else None,
    )


def _payload(access_token: str) -> dict[str, Any]:
    """The JWT's decoded payload claims, or ``CodexTokenError``.

    Guards three shapes that a naive split-and-decode passes silently or fatally: a value that
    isn't a string at all (``None.split`` is an AttributeError, escaping this module's whole error
    contract — and the store's loader is `str | None`-shaped, so a half-written bundle lands here);
    a token whose segment count isn't 3 (``split(".")[1]`` happily decodes segment 1 of a
    4-segment string); and a payload that is valid JSON but not an object (``json.loads`` returns
    an int/list/None and the caller's ``.get`` explodes far from here).
    """
    if not isinstance(access_token, str):
        raise CodexTokenError("not-a-string")

    segments = access_token.split(".")
    if len(segments) != 3:
        raise CodexTokenError("bad-segment-count")

    payload = segments[1]
    payload += "=" * (-len(payload) % 4)  # restore the base64 padding a JWT strips
    try:
        # Every realistic failure here is a ValueError (binascii.Error, json.JSONDecodeError and
        # UnicodeDecodeError all subclass it) or a RecursionError from a deeply-nested payload. A
        # TypeError would mean `payload` isn't a str, which the guard above already made impossible,
        # so it is left to propagate as the bug it is. Catching RecursionError keeps the decoder's
        # contract total: any unusable token becomes a CodexTokenError, never a raw crash.
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, RecursionError):
        claims = _UNDECODABLE

    # Raised OUTSIDE the except block on purpose, and this is subtler than it looks.
    # `raise ... from None` would have been the house idiom, but it only sets __suppress_context__
    # — it does NOT clear __context__. The chained JSONDecodeError stays reachable, and its `.doc`
    # attribute holds the DECODED PAYLOAD, which is exactly where the account id and the operator's
    # email live. No standard rendering path shows it (traceback, str, repr and logging.exception
    # are all clean — verified), but `exc.__context__.doc` is a plausible thing to reach for while
    # debugging an "unreadable token" report. Leaving the except block first clears the handled
    # exception, so the error we raise has no __context__ at all and there is nothing to reach for.
    if claims is _UNDECODABLE:
        raise CodexTokenError("undecodable-payload")

    if not isinstance(claims, dict):
        raise CodexTokenError("payload-not-an-object")
    return claims
