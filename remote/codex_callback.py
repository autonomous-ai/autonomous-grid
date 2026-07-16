"""The one-shot localhost listener that catches the codex OAuth redirect (ADR 0015 D-c).

The browser half of `grid join --api codex`: bind the callback port, open the operator's browser at
the authorize URL, and catch the single redirect the vendor sends back. The port is not ours to
choose — it is baked into the redirect uri registered against the vendor's client id
(``codex_oauth.CALLBACK_PORT``), **and it is the real Codex CLI's port too**, so a collision is an
ordinary Tuesday rather than an exotic failure: an operator signing into Codex proper in another
terminal holds it.

**Bind, then handle the failure — never probe first.** The nearest neighbours in this repo
(``shared/engine/launcher.py``'s and ``local/runtime.py``'s ``connect_ex`` checks) ask "is this port
free?" and then act on the answer, which is a TOCTOU race against exactly the process this listener
collides with: the real Codex CLI can bind in the window between our probe and our bind, and we would
then fail *anyway*, with a traceback instead of guidance. The bind IS the check.

**This module filters; it never decides.** It compares ``state`` only to answer "is this request the
redirect we are waiting for, or noise?" — because anything on the box can reach a listening loopback
port, and any webpage can make a browser reach it (`<img src="http://localhost:1455/auth/callback">`
is a plain cross-origin GET). Without that filter the first request to hit the path won the race and
threw away the operator's real approval.

Whether the redirect carries a usable code, and whether it is genuinely ours, remains
``codex_oauth.parse_redirect``'s call, made on the main thread with the authority to refuse. The split
is deliberate: a rejection raised inside a handler thread would be swallowed (``SystemExit`` in a
non-main thread kills the thread silently, a footgun this repo has met before) and the operator would
see a hang rather than an error. So the two share ``_state_matches`` and cannot drift, but only one of
them can say no.
"""

from __future__ import annotations

import contextlib
import errno
import http.server
import time
from collections.abc import Iterator

from . import codex_oauth


# Ceiling on how long one accepted connection may hold the listener while it dawdles over its
# request. A real browser sends its request line immediately; this only has to be generous enough
# to survive a loopback hiccup. `_Listener.wait` clamps it to the remaining deadline, so it is a
# ceiling, never an extension.
_HANDLER_TIMEOUT_S = 5.0


class PortInUse(OSError):
    """The callback port is already bound — most likely the operator's real Codex CLI.

    Its own type because it is the one bind failure with an operator-actionable answer (finish or
    quit the other sign-in, or use ``--no-browser``); every other OSError is a genuine fault.
    """


@contextlib.contextmanager
def listen(port: int, *, expected_state: str) -> Iterator[_Listener]:
    """Hold the callback port open for the body of the ``with`` block.

    Binds on entry, so the caller can open the browser knowing the redirect has somewhere to land —
    the reverse order would race the browser against our own startup. ``port=0`` lets the OS pick a
    free one (tests; never production, where the vendor pins the number).

    ``expected_state`` is what tells our redirect apart from every other request that can reach a
    listening loopback port; only a request carrying it ends the wait.
    """
    # Checked on the MAIN thread, at bind time, because the handler cannot raise. An empty state
    # would make `redirect_is_ours` reject everything and the sign-in would look like a timeout —
    # and it is the same footgun `parse_redirect` guards (`compare_digest("", "")` is True).
    if not expected_state:
        raise ValueError("listen needs the state this sign-in generated; got an empty one.")
    try:
        server = _CallbackServer(("127.0.0.1", port), _CallbackHandler)
    except OSError as exc:
        # EADDRINUSE is the expected collision, and the only one with a fallback: it gets its own
        # type so the join can retry by paste. `errno.EADDRINUSE` resolves per-platform (48 on
        # macOS/BSD, 98 on Linux) — never hardcode it.
        if exc.errno == errno.EADDRINUSE:
            raise PortInUse(exc.errno, str(exc)) from None
        # Anything else (an exhausted fd table, a sandbox refusing loopback) is a real fault and must
        # NOT be dressed up as "someone else has the port" — but it must not escape as a bare OSError
        # either: nothing between here and `cli/_main.py`'s `main` catches one, so re-raising it is a
        # traceback in the operator's face. The OS's own reason is carried through, since that is the
        # only diagnosable part; a port we could not bind for an unknown reason is terminal.
        raise SystemExit(
            f"Could not open the sign-in callback on port {port}: {exc}. Nothing was saved. "
            "Re-run with `grid join --api codex --no-browser` to sign in without a callback port."
        ) from None
    server.expected_state = expected_state
    try:
        yield _Listener(server)
    finally:
        server.server_close()


class _Listener:
    """One bound callback port, waiting for one redirect."""

    def __init__(self, server: _CallbackServer) -> None:
        self._server = server

    @property
    def port(self) -> int:
        """The port actually bound — the OS's choice when ``listen(0)`` was used."""
        return int(self._server.server_address[1])

    def wait(self, *, deadline: float) -> str | None:
        """The redirect URL the browser landed on, or None once ``deadline`` (a ``time.monotonic``
        value) passes with nothing usable.

        Requests that are not the callback (a browser's speculative ``/favicon.ico``, or anything
        else on the box poking at a listening port) are served and ignored rather than ending the
        wait — otherwise a favicon fetch would consume the operator's sign-in.
        """
        while self._server.redirect_url is None:
            now = time.monotonic()  # read once per iteration: the deadline check and both socket
            if now >= deadline:  # timeouts share it, as `cli/auth.py`'s device-login wait does
                return None
            remaining = max(0.05, deadline - now)
            # TWO timeouts, because they bound different sockets and only one of them is obvious.
            # `server.timeout` bounds the select() waiting for a NEW connection; once one is
            # accepted, control passes to the handler and this one no longer applies.
            self._server.timeout = remaining
            # ... so the ACCEPTED socket needs its own bound. Without it a client that connects and
            # sends nothing (`nc 127.0.0.1 1455`) parks `rfile.readline()` forever:
            # `StreamRequestHandler.timeout` defaults to None, so `setup()` never calls
            # `settimeout()`. That silently defeats the whole deadline — no error, the CLI just
            # stops — and, this server being single-threaded, also blocks the real browser's
            # redirect from being accepted at all. Clamped to the remaining budget so a handler can
            # never outlive the deadline it is supposed to serve.
            self._server.handler_timeout = min(_HANDLER_TIMEOUT_S, remaining)
            self._server.handle_request()  # returns on a request OR on the timeout above
        return self._server.redirect_url


class _CallbackServer(http.server.HTTPServer):
    # `allow_reuse_address` stays TRUE (the HTTPServer default), and that is load-bearing rather than
    # inherited by accident. It does NOT let us bind a port another process is LISTENing on — that
    # would need SO_REUSEPORT — so the collision this module cares about is still detected. What it
    # does prevent is the false positive: the sockets accepted by a PREVIOUS `grid join --api codex`
    # linger in TIME_WAIT on this same port, so without it a second run moments later would report
    # "the Codex CLI has the port" when the only thing holding it is our own last run.
    allow_reuse_address = True

    redirect_url: str | None = None

    # The state this sign-in generated; set by `listen`, read by the handler to tell our redirect
    # from every other request that can reach a listening loopback port.
    expected_state: str = ""

    # How long an accepted connection may take over its request, refreshed per wait iteration by
    # `_Listener.wait`. Lives on the server because the handler is constructed per connection and
    # has no other way to see the wait's remaining budget. The default only covers a handler
    # constructed before `wait` ever ran, which the `with` block makes impossible.
    handler_timeout: float = _HANDLER_TIMEOUT_S


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Serves exactly one thing: a page telling the operator to go back to their terminal."""

    server: _CallbackServer

    def setup(self) -> None:
        # `StreamRequestHandler.setup()` calls `settimeout()` only when `self.timeout` is not None,
        # and the class default IS None — so this assignment is the entire difference between a
        # bounded read and one that blocks forever. Set per instance from the server, which knows
        # the wait's remaining budget; `self.server` is assigned before `setup()` runs.
        self.timeout = self.server.handler_timeout
        super().setup()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
        if self.path.split("?")[0] != "/auth/callback":
            self.send_error(404)
            return
        url = f"http://localhost:{self.server.server_address[1]}{self.path}"
        # A path match is NOT enough. Anything on the box can reach this port, and any webpage can
        # make a browser do it — `<img src="http://localhost:1455/auth/callback">` is a plain
        # cross-origin GET. Ending the wait on the first path match let such a hit preempt the
        # operator's real approval, which then arrived to a closed socket and was thrown away.
        # So: only OUR redirect ends the wait; everything else is served and ignored, exactly like
        # the favicon fetch below.
        #
        # Comparison only — this is a handler thread, where a raise would be swallowed and the
        # operator would see a hang instead of an error. It is a filter, not the control:
        # `parse_redirect` still makes the authoritative refusal on the main thread.
        #
        # A vendor `?error=` redirect echoes our state too (RFC 6749 §4.1.2.1 requires it), so a
        # refusal reaches the operator through this same gate rather than needing a hole in it.
        if not codex_oauth.redirect_is_ours(url, self.server.expected_state):
            self._ignore()
            return
        # Captured BEFORE the reply is written. Writing first would mean a browser that hung up mid-
        # response (a closed tab, a client that stops reading once redirected) loses an approval the
        # operator actually gave: the write raises, `handle_one_request` swallows it, and `wait`
        # would sit on the port until the deadline with the answer already in hand.
        self.server.redirect_url = url
        # The vendor sends the operator's browser here, so the reply is read by a human. It says
        # nothing about the outcome: `parse_redirect` has not run yet (it runs on the main thread,
        # by which point this socket is gone), so the page points at the terminal, which is where
        # the real answer — including a refusal — is printed.
        body = (
            b"<!doctype html><meta charset=utf-8><title>grid</title>"
            b"<p>Sign-in received. You can close this tab and return to your terminal.</p>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ignore(self) -> None:
        """Answer a request that isn't our redirect, without ending the wait.

        404 rather than a hint: this reply is readable by whatever sent it, which by definition is
        not the sign-in we started. It learns that something is listening — which it already knew,
        having connected — and nothing else.
        """
        self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:
        """Silence. The base class logs every request line to stderr, and on this port the request
        line IS the authorization code — the default would print the operator's live credential
        (issue 04: no token reaches a log, terminal, or run record)."""

    def handle_one_request(self) -> None:
        # A half-open or hostile connection must not take the sign-in down: anything on the box can
        # reach a listening loopback port. Swallowing loses nothing — `wait` simply keeps waiting for
        # a request that does carry a redirect, until the deadline says otherwise.
        try:
            super().handle_one_request()
        except (TimeoutError, OSError):
            # `close_connection` must be set, not just the error swallowed: `handle()` loops on it
            # (`while not self.close_connection: self.handle_one_request()`), so returning from a
            # dead socket without it spins reads at full tilt until the deadline.
            self.close_connection = True
