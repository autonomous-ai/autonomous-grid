"""Who is allowed to open this interface.

On loopback the answer is "whoever is using this computer", which is the whole security model and
is the right one — the pages are a local tool, like the dashboard.

The moment someone runs `--host 0.0.0.0` to let a colleague use it, that stops being true. The
pages show samples of the uploaded data (real tickets, real leads, real customer names), can start
jobs on the machine, and can put a model in front of customers. Serving that to an office LAN with
no check at all would make "your data never leaves your network" technically true and practically
worthless — the network is where the other people are.

So: a shared link with a token in it. One secret, generated per run and printed once, exchanged for
a cookie on first visit. That is deliberately the weakest thing that still means something — it is
not accounts, not TLS, and not a defence against someone who can read your terminal. It is the
difference between "anyone on the office wifi" and "the person you sent the link to".
"""
from __future__ import annotations

import hmac
import os
import secrets

COOKIE = "grid_train_access"
LOOPBACK = {"127.0.0.1", "::1", "localhost", ""}


def is_loopback(host: str) -> bool:
    return (host or "").strip() in LOOPBACK


def resolve_token(host: str) -> str:
    """The token this run requires, or "" when none is needed.

    `GRID_TRAIN_WEB_TOKEN` lets someone pin a stable value (a saved bookmark, a kiosk). Otherwise a
    fresh one per run, which is the safer default: closing the terminal invalidates the link.
    """
    if is_loopback(host):
        return ""
    return (os.environ.get("GRID_TRAIN_WEB_TOKEN") or "").strip() or secrets.token_urlsafe(24)


def matches(expected: str, given: str | None) -> bool:
    return bool(given) and hmac.compare_digest(expected, given)


def install(app, token: str) -> None:
    """Require the token on every request except the health check. No token, no middleware."""
    if not token:
        return

    from fastapi.responses import HTMLResponse, RedirectResponse
    from starlette.middleware.base import BaseHTTPMiddleware

    class Gate(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.url.path == "/healthz":
                return await call_next(request)
            if matches(token, request.cookies.get(COOKIE)):
                return await call_next(request)
            query = request.query_params.get("token")
            if matches(token, query):
                # Swap the secret out of the address bar so it does not end up in a screenshot,
                # a shared URL, or the next person's browser history.
                clean = request.url.remove_query_params("token")
                response = RedirectResponse(str(clean), status_code=303)
                response.set_cookie(COOKIE, token, httponly=True, samesite="lax", path="/")
                return response
            return HTMLResponse(_denied(), status_code=403)

    app.add_middleware(Gate)


def _denied() -> str:
    return """<!doctype html><meta charset="utf-8"><title>Not your link</title>
<style>body{font:16px/1.6 -apple-system,sans-serif;max-width:32rem;margin:18vh auto;padding:0 1.2rem;
color:#14181f}h1{font-size:1.3rem;margin:0 0 .4rem}p{color:#5c6672}
@media(prefers-color-scheme:dark){body{background:#101419;color:#e9edf3}p{color:#98a2af}}</style>
<h1>This link needs the code</h1>
<p>Whoever started this interface has a link with a code at the end of it. Ask them for that link —
it is what proves you are meant to see this team's data.</p>"""


def share_lines(host: str, port: int, token: str) -> list[str]:
    """What to print at startup: never the bare address when a token is required."""
    if not token:
        return [
            f"grid train web -> http://127.0.0.1:{port}  (Ctrl-C to stop)",
            ("Only this computer can reach it. To share it with a colleague, add --host 0.0.0.0 "
             "and send them the link it prints."),
        ]
    where = "<this-computer>" if host in ("0.0.0.0", "::") else host
    return [
        f"grid train web -> http://{where}:{port}/?token={token}",
        ("Send that whole link, including the code, to whoever should use it. Anyone with it can "
         "see this team's examples and start training runs on this machine."),
        "The code changes every time you start it. Set GRID_TRAIN_WEB_TOKEN to keep one.",
    ]
