"""git's credential-helper protocol, answered from the local credential store alone (ADR 0033 D-h).

`grid task fetch` never writes the token into a clone's `.git/config`, and the reason is at its call
site: a result directory is a thing people zip up and pass around, and a grid access token lives a
year. It gets away with it by handing the token to each git child through the environment. That does
not survive a member with a real clone running `git pull` from an IDE — nothing of ours is in that
call path. A credential helper is the only shape where git asks **us**, every time, which also means
a refreshed token is picked up instead of one written once expiring in place.

**This is not the textbook helper, and it cannot be.** A classic helper answers `username`/
`password`, which git turns into HTTP **Basic**. The relay's git front accepts only
`Authorization: Bearer` (grid-src `relay._bearer_or_api_key`), so Basic is refused — measured, git
sends `Basic eC1hY2Nlc3MtdG9rZW46…`, gets a 401, and reports `fatal: Authentication failed`. git's
`authtype`/`credential` pair is what lets a helper name the scheme: git concatenates the two with a
single space to build the header verbatim.

Nothing here reaches the network, and that is a requirement rather than a happy accident. git runs
the helper on **every** operation, so routing this through the CLI's usual `_resolve()` →
`resolve_relay_base` → `control_plane.get_managed_network_status` path would put a control-plane
round trip in front of every IDE fetch, break `git pull` against a healthy relay whenever the control
plane blips, and make every non-creator member take the 403 fallback each time.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


# git shows a helper's stderr to the member, so every refusal below says which one it is. A helper
# that declines in silence leaves `fatal: could not read Username` as the only evidence, which names
# neither the grid nor the host and reads as a git bug.
_NO_SUCH_GRID = (
    "grid: this clone is configured for grid {network_id!r}, which is not in your local "
    "credentials. Run `grid login` to refresh your grids."
)
_WRONG_HOST = (
    "grid: refusing to send {label}'s credential to {protocol}://{host} — that is not this grid's "
    "relay. Check this repository's `remote.origin.url`."
)
_UNREADABLE = "grid: could not read the credential request on stdin; it is not UTF-8 text."
_UNUSABLE_TOKEN = (
    "grid: the stored access token for {label} is not usable as a credential. "
    "Run `grid login` to refresh your grids."
)
_NO_AUTHTYPE = (
    "grid: this git cannot carry a `Bearer` credential — it did not announce the `authtype` "
    "capability. The grid relay accepts no other scheme, so please upgrade git. "
    "`git credential capability` on a git that can do this prints `capability authtype`."
)

# The capability git must announce before a helper may answer with `authtype`/`credential`.
# `git-credential(1)`: those values "should not be sent unless the appropriate capability is
# provided on input". Measured — git 2.54.0 sends `capability[]=authtype` and `capability[]=state`.
AUTHTYPE_CAPABILITY = "authtype"

# The one scheme the relay's git front accepts (grid-src `relay._bearer_or_api_key`). git joins this
# to the credential with a single space to build the `Authorization` header, so it is a wire value
# shared with the other repository — `tests/test_task_lease.py` checks it against grid-src's source.
AUTH_SCHEME = "Bearer"


@dataclass(frozen=True)
class Reply:
    """What the helper says. `stdout` is git's; `stderr` is the member's.

    Two fields rather than a bare string because "I have nothing for you" and "I have nothing for
    you *and here is why*" are different answers, and git reads only the first — `gitcredentials(7)`:
    a helper "may write to stderr" to notify the user of a potential issue. Collapsing them would
    make every refusal in this module silent.
    """

    stdout: str = ""
    stderr: str = ""


def parse_request(raw: bytes) -> dict[str, list[str]]:
    """git's `key=value` lines as a multimap. Raises `UnicodeDecodeError` on undecodable input.

    Every value is a list because git's own protocol has multi-valued keys (`capability[]`,
    `wwwauth[]`) and single-valued ones, and a mapping that guessed which was which from the first
    line it saw would answer differently depending on input order.

    `splitlines()` rather than `split("\\n")`: it takes CRLF too, and the difference is not
    cosmetic — a trailing `\\r` left on `host=relay.example` matches no grid, so the token would be
    withheld for a reason nobody could read off the message.

    Deliberately allowed to RAISE rather than swallowing a decode failure into an empty mapping:
    `respond` is the one place that has to turn "I could not read this" into an answer, and an
    empty mapping there is indistinguishable from "git asked me nothing", which is a different
    fault with a different message.
    """
    request: dict[str, list[str]] = {}
    for line in raw.decode("utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            request.setdefault(key, []).append(value)
    return request


def relay_endpoint(network: dict[str, Any]) -> tuple[str, str] | None:
    """A stored grid's relay as `(scheme, netloc)`, lower-cased, or `None` when it records none.

    Reads `signaling_url` then `lan_signaling_url`, the precedence `remote_grid._grid_url` already
    applies — a create reply carries the first, a login-fetched bundle the second.

    The netloc keeps its **port**, because git's `host` does: a `get` for a relay on
    `127.0.0.1:8123` arrives as `host=127.0.0.1:8123` (measured). Comparing bare hostnames would
    make every grid on one machine the same grid.
    """
    url = network.get("signaling_url") or network.get("lan_signaling_url")
    if not isinstance(url, str) or not url:
        return None
    split = urlsplit(url)
    if not split.scheme or not split.netloc:
        return None
    return split.scheme.lower(), split.netloc.lower()


def respond(operation: str, raw: bytes, *, networks: list[dict[str, Any]],
            network_id: str | None = None) -> Reply:
    """The whole protocol: what git asked, and what to say back.

    One entry point rather than a function per operation, so the CLI layer does I/O and nothing
    else — and so an operation this build has never heard of is answered here, once, rather than
    falling off the end of a dispatch table.
    """
    if operation == "capability":
        # git's own capability announcement format, which is deliberately NOT the credential format
        # so the two can never be confused for each other.
        return Reply(stdout=f"version 0\ncapability {AUTHTYPE_CAPABILITY}\n")
    if operation != "get":
        # `store`, `erase`, and anything git adds later. Silence and success, per
        # `gitcredentials(7)`: "If a helper receives any other operation, it should silently ignore
        # the request. This leaves room for future operations to be added."
        #
        # `store` is handed the credential BACK (measured), so this branch is the one that has to
        # not write — being a no-op is the feature, not an omission.
        return Reply()

    try:
        request = parse_request(raw)
    except UnicodeDecodeError:
        # Whatever wrote to this pipe, it was not git speaking the credential protocol. A traceback
        # here lands in the middle of somebody's `git pull`.
        return Reply(stderr=_UNREADABLE)
    protocol = (request.get("protocol") or [""])[0].lower()
    host = (request.get("host") or [""])[0].lower()

    # Checked BEFORE the store is read, so a git that could not use the answer never causes the
    # credential to be looked up at all.
    if AUTHTYPE_CAPABILITY not in request.get("capability[]", []):
        return Reply(stderr=_NO_AUTHTYPE)

    # `isinstance` is not belt-and-braces: `credentials.toml` is PARSED, not validated, so a
    # hand-edited `networks = ["…"]` — or a `[networks]` TABLE, which tomllib returns as a dict
    # whose iteration yields plain string KEYS — puts a non-record in this list. `n.get` on a string
    # raises `AttributeError`, caught by nothing between here and the process boundary, and the
    # member gets a traceback in the middle of a `git pull`. Skipping rather than raising also
    # contains the blast radius: one malformed entry no longer takes out the lookup for every other
    # grid in the store.
    network = next(
        (n for n in networks if isinstance(n, dict) and n.get("network_id") == network_id), None)
    if network is None:
        return Reply(stderr=_NO_SUCH_GRID.format(network_id=network_id))

    # The grid is PINNED by the clone's config and the host is checked against it anyway.
    # `.git/config` is a file people copy, and a repository shipped with a `credential.<url>.helper`
    # pointing at somebody else's host would otherwise be handed a year-long grid token.
    endpoint = relay_endpoint(network)
    if endpoint is None or endpoint != (protocol, host):
        return Reply(stderr=_WRONG_HOST.format(
            protocol=protocol, host=host, label=network.get("name") or network_id))

    token = network.get("access_token")
    # `splitlines()` is the same rule the parser above reads by, and it is deliberately WIDER than
    # git's own (it also breaks on VT, FF, NEL and the Unicode line separators). Being stricter than
    # git about what may become a credential is the safe direction.
    if (not isinstance(token, str) or not token
            or token.strip() != token or len(token.splitlines()) != 1):
        # The reply is line-oriented, so a value carrying a line break is not a value — it is more
        # lines, and the next one would be read as a credential DIRECTIVE (a `quit=0` re-enabling
        # the helper chain, say) out of a file this process only reads. `credentials.toml` is
        # parsed, not validated: a half-written bundle or a hand edit lands here.
        return Reply(stderr=_UNUSABLE_TOKEN.format(label=network.get("name") or network_id))

    return Reply(stdout="".join(line + "\n" for line in (
        # The capability directive comes FIRST: `git-credential(1)` requires a directive to precede
        # any value depending on it.
        f"capability[]={AUTHTYPE_CAPABILITY}",
        f"authtype={AUTH_SCHEME}",
        # `token`, never a second `network.get(...)`. The value that goes on the wire has to be the
        # one the guard above ran on: that guard is what keeps a line break out of a line-oriented
        # reply, and re-reading the mapping puts an unchecked value one refactor away from the
        # `quit=0` injection the comment above it describes. They agree today only because nothing
        # mutates `network` in between — a property of this function's body, enforced by nothing.
        f"credential={token}",
        # No other helper may be consulted, and no prompt may appear. Without it git keeps going —
        # `gitcredentials(7)` stops only "once both username and password have been provided", and
        # this answer provides neither.
        "quit=1",
    )))
