"""`grid join --api codex` sign-in — the operator-facing half of the OAuth seat (ADR 0015 D-c).

Everything a human sees or is asked for during a codex sign-in: which flow to run (browser by
default, `--no-browser` to paste), what gets printed, how long we wait, and what a failure reads
like. The wire protocol underneath is `remote/codex_oauth.py` and the one-shot callback listener is
`remote/codex_callback.py`; neither knows a terminal exists.

The UX mirror is `grid login`'s device flow (`cli/auth.py`) — print a URL, open a browser unless
`--no-browser`, wait with a deadline, never print a token. The mechanism is unrelated: that flow
talks to autonomous's control plane and polls it; this one talks to the vendor and catches a
redirect. **This credential is not part of sign-in**: `grid logout` leaves it alone, because it is
the operator's ChatGPT subscription, not their grid session.

Import rule mirrors `cli/remote_provider.py`: only stdlib + `shared.*` at module top; `remote.*` is
imported lazily inside the functions.
"""

from __future__ import annotations

import secrets
import sys
import time
import webbrowser
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from remote.codex_oauth import CodexBundle

# How long the operator has to approve, in seconds. The vendor's authorization code lives ~10
# minutes; waiting past that only buys a guaranteed-doomed exchange, so this is a local ceiling on
# the vendor's own lifetime rather than a number of our own (the clamp `cli/auth.py` applies to the
# control plane's `expires_in`). It bounds both flows: the browser wait, and how stale a paste may be.
_SIGNIN_DEADLINE_S = 600


def resolve_seat(*, no_browser: bool) -> tuple[CodexBundle, bool]:
    """This machine's codex seat — the stored one, or a fresh sign-in — plus whether the sign-in
    ran. Fresh means the credential CHANGED: the join must probe it, and a live codex engine must
    respawn to pick the new bundle up (the openai key-rotation policy, ADR 0012).

    A stored bundle is reused with no browser and no prompt (the acceptance criterion behind user
    story 15: re-joining later is one command). Whether a stored seat can still serve from this
    machine is the join probe's question (ADR 0015 D-f, issue 05); whether it stays alive across
    idle weeks is D-d's, answered by the serve loop's refresh.
    """
    from remote import api_keys

    stored = api_keys.load_codex_bundle()
    if stored is not None:
        return stored, False
    return sign_in(no_browser=no_browser), True


def sign_in(*, no_browser: bool) -> CodexBundle:
    """Run the OAuth PKCE authorization and store the seat it yields."""
    from remote import api_keys, codex_oauth

    pkce = codex_oauth.generate_pkce()
    # The anti-injection nonce. Generated per sign-in and never reused: `parse_redirect` refuses any
    # redirect that doesn't carry this exact value back, which is what stops a code from someone
    # else's authorize session being pasted into this one.
    state = secrets.token_urlsafe(32)
    authorize_url = codex_oauth.build_authorize_url(state=state, challenge=pkce.challenge)

    redirect = _paste_flow(authorize_url) if no_browser else _browser_flow(authorize_url, state)
    code = codex_oauth.parse_redirect(redirect, expected_state=state)
    bundle = codex_oauth.exchange_code(code, pkce.verifier)
    # Stored only once the vendor has honoured the code — a bundle we never obtained is never
    # written, mirroring the openai key store's "validate, then persist" ordering.
    api_keys.store_codex_bundle(bundle)
    print(f"Signed in to your codex subscription (plan: {bundle.plan_type or 'unknown'}).")
    return bundle


def _browser_flow(authorize_url: str, state: str) -> str:
    """Open the operator's browser and catch the redirect on the callback port.

    ``state`` reaches the listener as well as the authorize URL: it is what lets the listener tell
    our redirect from any other request that can reach a loopback port, so a stray hit cannot
    preempt the operator's real approval. It does not replace `parse_redirect`'s check.
    """
    from remote import codex_callback, codex_oauth

    try:
        # Bound BEFORE the browser opens: the reverse order races the redirect against our own
        # startup. The bind is also the port check — see `remote/codex_callback.py`.
        with codex_callback.listen(codex_oauth.CALLBACK_PORT, expected_state=state) as listener:
            _open_browser(authorize_url)
            print("Approve the sign-in in your browser to connect your codex subscription.")
            print(f"  {authorize_url}")  # printed too: the browser may not have opened
            redirect = listener.wait(deadline=time.monotonic() + _SIGNIN_DEADLINE_S)
    except codex_callback.PortInUse:
        return _port_in_use_fallback(authorize_url)
    if redirect is None:
        raise SystemExit(
            "Timed out waiting for the sign-in to be approved in your browser. Nothing was saved. "
            "Re-run `grid join --api codex` to try again."
        )
    return redirect


def _port_in_use_fallback(authorize_url: str) -> str:
    """The callback port is taken — most likely by the operator's real Codex CLI (user story 4).

    A fallback rather than a refusal: the two tools have to coexist, and the paste flow needs
    nothing from that port. On a machine with no terminal to paste into there is nothing to fall
    back TO, so that ends the join with the flag that would have worked.
    """
    from remote import codex_oauth

    from . import provider

    print(
        f"Port {codex_oauth.CALLBACK_PORT} is already in use, so grid can't catch the sign-in "
        "redirect there. The Codex CLI and Codex Desktop use that port too — if one of them is "
        "signing in, finish or quit it and re-run this command.",
        file=sys.stderr,
    )
    if not provider._interactive():
        raise SystemExit(
            "Nothing was saved. Re-run with `grid join --api codex --no-browser` to sign in by "
            "pasting the redirect URL instead."
        )
    print("Falling back to the paste flow.", file=sys.stderr)
    return _paste_flow(authorize_url)


def _paste_flow(authorize_url: str) -> str:
    """Print the authorize URL and take the redirect URL back by hand (user story 2).

    For a headless box: the operator approves on whatever machine has a browser and brings the
    resulting URL back. Nothing is bound and no port is needed.
    """
    from . import provider

    if not provider._interactive():
        raise SystemExit(
            "Signing in to a codex subscription needs a terminal to paste the redirect URL into. "
            "Run `grid join --api codex` from an interactive shell."
        )
    print("To connect your codex subscription, open this URL on any machine and approve it:")
    print(f"  {authorize_url}")
    print(
        "Your browser will then land on a `localhost` URL that fails to load — that is expected. "
        "Copy it from the address bar and paste it here."
    )
    # Started AFTER the URL is printed: the operator's clock starts when they can act, and the
    # vendor's code is only minted once they approve. Checked after the paste rather than around it
    # — `input()` cannot be interrupted portably, and a doomed exchange is worth not spending.
    deadline = time.monotonic() + _SIGNIN_DEADLINE_S
    pasted = _prompt_redirect_url()
    if not pasted:
        raise SystemExit("No redirect URL entered. Nothing was saved.")
    if time.monotonic() >= deadline:
        raise SystemExit(
            "That sign-in took too long — the authorization code in that URL has expired (they "
            "last about 10 minutes). Nothing was saved. Re-run `grid join --api codex` for a "
            "fresh URL."
        )
    return pasted


def _prompt_redirect_url() -> str:
    """Read the pasted redirect URL. Split out so the CLI-seam tests can monkeypatch it.

    Echoed, unlike `_prompt_api_key`'s getpass: this URL is already on the operator's screen in
    their own browser's address bar, a paste this long needs to be visible to be checked, and its
    authorization code is single-use and dead within minutes — whereas an API key is neither.
    """
    return input("Paste the redirect URL here: ").strip()


def _open_browser(url: str) -> None:
    """Best-effort. A headless box has no browser, and the URL is printed either way — exactly as
    `cmd_login` treats it."""
    try:
        webbrowser.open(url)
    except (OSError, webbrowser.Error):
        pass
