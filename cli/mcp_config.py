"""`grid mcp config` — pointing a coding agent's harness at the grid's web tools.

ADR 0041. The server is the control plane's (`/v1/grid/web-mcp`); this half exists so nobody has to
open `~/.grid/credentials.toml` and pick a token out of it by hand. That file holds one bundle per
grid, so "by hand" means choosing between them correctly, and teaching people to open their own
credential store is a habit worth not starting.

**Six harnesses, six spellings, one credential.** Measured 2026-09-08 against Claude Code 2.1.263,
Codex 0.153.4, GitHub Copilot CLI 1.0.83, opencode 1.18.29, Hermes 0.21.1 and pi 0.84.1 — each
binary run against a throwaway home so the file it writes could be read back, then the header proven
to arrive with a header-logging listener (positive control included). Not from vendor documentation:
the flag set moves per release and does not cover every supported field.

- **Claude Code**, **Copilot** and **opencode** take a command *and* a config file, so both are
  printed — a person who keeps their config in git wants the file, not a command that edits it.
- ⚠️ **Codex has no `--header` flag** — still true on 0.153.4, which offers only
  `--bearer-token-env-var` and the OAuth options. It nonetheless honours `http_headers` in
  `config.toml` (proven on the wire; `codex mcp get` shows the field and masks its value, which it
  never does for a key it does not know), so Codex gets a **block to paste**, not a command. An
  environment variable was rejected: a 365-day credential in a shell is readable by every child
  process of that shell.
- ⚠️ **opencode's `--header` takes `KEY=VALUE`**, not the `Key: value` every other harness takes.
- ⚠️ **Hermes's block is YAML, so its indentation is load-bearing** — it is printed at column zero
  while the shell commands are indented. Its own `mcp mcp add` is interactive (it asks for the token
  and probes the server), which is why the block leads.
- ⚠️ **pi ships no MCP client at all**, by design. It is a listed target anyway: argparse's "invalid
  choice" would read as an oversight in this command, and the refusal is decided before a grid is
  resolved so it never turns into "run `grid login`".

⚠️ **The URL carries its trailing slash.** Without it the mount answers **307**; Claude Code follows
it, and nothing here should depend on every client doing the same.

⚠️ `MOUNT_PATH` is hand-duplicated from grid-apis `web_mcp.MOUNT_PATH`. There is deliberately no
discovery route — its own path would be the same constant one level down. Pinned by
`tests/test_web_mcp_lockstep.py`, which skips unless grid-apis sits beside this worktree.
"""
from __future__ import annotations

import argparse
import json
import shlex
import time

# ↔ grid-apis `grid_networks/web_mcp.MOUNT_PATH`. Roll the CONTROL PLANE out before this CLI: a
# `grid mcp config` against an older control plane prints a URL that answers a bare 404, which the
# harness reports as a server it cannot reach.
MOUNT_PATH = "/v1/grid/web-mcp"

# What the tools are called in the harness's own listing — an agent sees `mcp__grid-web__web_search`.
# The hyphen survives every harness, Codex included: `codex mcp add` writes `[mcp_servers.grid-web]`
# itself (measured), so spelling it `grid_web` there would rename the tools for that one harness.
SERVER_NAME = "grid-web"

# How close to `exp` still counts as "renew it before printing". Deliberately not
# `grid_credential.LAUNCH_EXPIRY_MARGIN_SECONDS`' one day: a launch protects a work session that
# starts now, while what this command prints is pasted into a config file and left there for months.
# A token with 25 hours left passes a one-day margin and gives somebody a harness that dies tomorrow.
# Thirty days is 8% of a grid token's 365-day life, so it cannot cause a renewal that would not have
# happened within the month anyway.
EXPIRY_MARGIN_SECONDS = 30 * 24 * 3600

#: Harness key → what to print for it. The listing view and argparse's `choices` both read this, so
#: a new harness is one entry rather than three places to keep in step.
_HARNESS_ORDER = ("claude", "codex", "copilot", "hermes", "opencode", "pi")

#: Harnesses with no MCP client at all, and the sentence that says so. Keyed like the renderers so
#: `--harness` accepts them and answers in words rather than argparse's "invalid choice".
NO_MCP_CLIENT = {
    "pi": (
        "pi ships no MCP client — by design (\"No MCP.\" in its own README, 0.84.1).\n"
        "The grid's web tools are an MCP server, so there is nothing here to configure for it.\n"
        "A third-party pi extension can add MCP support; pi itself has none to point at."
    ),
}

HARNESS_CHOICES = _HARNESS_ORDER


def cmd_mcp_config(args: argparse.Namespace) -> int:
    """Print how to point one harness — or list the harnesses — at this grid's web tools."""
    harness = getattr(args, "harness", None)
    if harness in NO_MCP_CLIENT:
        # Before any credential is touched: this refusal is about the harness, not about this
        # machine's sign-in, and resolving a grid first would let it fail with the wrong sentence.
        raise SystemExit(NO_MCP_CLIENT[harness])

    wants_credential = bool(harness) or bool(getattr(args, "json", False))
    url, token, label = _session(args, renew=wants_credential)

    if getattr(args, "json", False):
        # The same three values, for something assembling a config itself — and the replacement for
        # the `grid mcp token` this command used to sit beside. No postcondition to guard: the
        # command reports what is already true locally.
        print(json.dumps({"server": SERVER_NAME, "url": url, "authorization": f"Bearer {token}"},
                         indent=2))
        return 0

    if not harness:
        _print_listing(url, label, getattr(args, "grid", None))
        return 0

    print(f"Web tools for grid {label}.\n")
    print(_RENDERERS[harness](url, token))
    print(
        "\nThis prints your grid's access token. Anyone who has it can search the web on your "
        "account's allowance."
    )
    return 0


# ---------------------------------------------------------------------------
# The grid, and the credential it is about to hand over
# ---------------------------------------------------------------------------

def _session(args: argparse.Namespace, *, renew: bool) -> tuple[str, str, str]:
    """`(url, token, label)` for the grid this command names, or a clean `SystemExit`.

    The control-plane base comes from the same `credentials.api_url()` every other command uses, so
    a home signed in against dev prints a dev URL without being told.

    ``renew`` is false for the listing view, and that is the whole of this command's network
    behaviour: it reaches the control plane **only when it is about to print the credential**. An
    arg-less `grid mcp config` prints no token, so it needs no fresh one — and must not rotate this
    machine's refresh credential to draw a menu.
    """
    from remote import credentials

    from . import remote_grid

    rec = remote_grid._select(getattr(args, "grid", None))
    label = str(rec.get("name") or rec.get("network_id") or "?")
    token = remote_grid.require_access_token(rec, label)
    if renew:
        token = _renewed(rec, label, token)
    url = credentials.api_url().rstrip("/") + MOUNT_PATH + "/"
    return url, token, label


def _renewed(rec: dict, label: str, token: str) -> str:
    """The token to print: renewed when it is inside the margin, refused when it is already dead.

    ⚠️ The refusal is this command's own, and it is the one place it must be **less** forgiving than
    `grid launch`. That command warns about an unrepairable token and lets its relay probe decide,
    because the probe is authoritative and a wrong local clock must not cost somebody a launch that
    works. There is no probe here — the web-tools server is the control plane's, not the grid's — so
    the alternative is printing a credential that is certainly dead into a file, where the 401 that
    follows tells the user to re-run the very command that gave it to them.

    It runs **before** the shared repair rather than checking its result, so this state is described
    once: the repair's own warning ("no refresh credential is stored for it") would otherwise be
    printed first and then contradicted by a refusal saying the same thing in different words. Every
    other unrepairable state still belongs to the repair, whose refusals name the remedy the control
    plane's status implies — `grid login` for a 401, `grid sync` for a 403.
    """
    from remote import credentials

    from . import grid_credential

    exp = credentials.token_expiry(token)
    if exp is not None and exp <= time.time() and not str(rec.get("refresh_token") or ""):
        raise SystemExit(
            f"Grid {label}'s access token has expired and there is no refresh credential stored "
            f"for it.\nRun `grid login` to renew it — a harness configured with this token would "
            f"be refused."
        )
    return grid_credential.refresh_if_stale(
        rec, label, token,
        margin_seconds=EXPIRY_MARGIN_SECONDS,
        # Not the default "launching anyway": this command launches nothing.
        proceeding="printing it anyway",
    )


# ---------------------------------------------------------------------------
# The listing view — the arg-less command, which prints no credential
# ---------------------------------------------------------------------------

def _print_listing(url: str, label: str, grid: str | None) -> None:
    """Name the server and every harness that can be pointed at it — and no token.

    The token is left out on purpose: this is the invocation people run first and run on a shared
    screen, and it would otherwise put a live 365-day credential there to answer a question about
    which harnesses are supported.
    """
    suffix = f" {shlex.quote(grid)}" if grid else ""
    print(f"Web tools for grid {label}.")
    print(f"  server: {SERVER_NAME}")
    print(f"  url:    {url}\n")
    print("Pick your harness:")
    for key in _HARNESS_ORDER:
        if key in NO_MCP_CLIENT:
            continue
        print(f"  grid mcp config --harness {key}{suffix}")
    print(
        "\nEach of those prints your grid's access token — a live credential, so treat the output "
        "like a password."
    )
    # Why a harness somebody has installed is missing from the list above. Its own first line names
    # it, so this generalises to any later addition without a second sentence to keep in step.
    for sentence in NO_MCP_CLIENT.values():
        print(sentence.splitlines()[0])


# ---------------------------------------------------------------------------
# One renderer per harness. Commands are indented by two; file blocks start at column zero, because
# they exist to be pasted and YAML's leading whitespace is part of the document.
# ---------------------------------------------------------------------------

def _header(token: str) -> str:
    return f"Authorization: Bearer {token}"


def _claude(url: str, token: str) -> str:
    # `--scope user` on purpose: `--scope project` writes `.mcp.json`, which Claude Code holds for
    # interactive approval — measured. Web search is not a per-repository capability anyway.
    command = (
        f"  claude mcp add --transport http --scope user {SERVER_NAME} {shlex.quote(url)} "
        f"--header {shlex.quote(_header(token))}"
    )
    block = json.dumps(
        {"mcpServers": {SERVER_NAME: {"type": "http", "url": url,
                                      "headers": {"Authorization": f"Bearer {token}"}}}},
        indent=2,
    )
    return (
        f"Claude Code — run this:\n{command}\n\n"
        f"…or merge this into ~/.claude.json yourself (what that command writes):\n{block}"
    )


def _codex(url: str, token: str) -> str:
    return (
        "Codex — add this to ~/.codex/config.toml (it has no --header flag):\n"
        f"[mcp_servers.{SERVER_NAME}]\n"
        f'url = "{url}"\n'
        f'http_headers = {{ Authorization = "Bearer {token}" }}'
    )


def _copilot(url: str, token: str) -> str:
    # Flag order as measured: `copilot mcp add --transport http --header … <name> <url>`.
    command = (
        f"  copilot mcp add --transport http --header {shlex.quote(_header(token))} "
        f"{SERVER_NAME} {shlex.quote(url)}"
    )
    # `"tools": ["*"]` is what its own `mcp add` writes. Omitting it defaults to all tools today
    # (measured), but printing what the harness itself writes leaves nothing to a default that moves.
    block = json.dumps(
        {"mcpServers": {SERVER_NAME: {"tools": ["*"], "type": "http", "url": url,
                                      "headers": {"Authorization": f"Bearer {token}"}}}},
        indent=2,
    )
    return (
        f"GitHub Copilot CLI — run this:\n{command}\n\n"
        f"…or merge this into ~/.copilot/mcp-config.json:\n{block}"
    )


def _opencode(url: str, token: str) -> str:
    # ⚠️ `KEY=VALUE`, not `Key: value`. Spelled with a colon the header is accepted and wrong.
    command = (
        f"  opencode mcp add {SERVER_NAME} --url {shlex.quote(url)} "
        f"--header {shlex.quote(f'Authorization=Bearer {token}')}"
    )
    block = json.dumps(
        {"mcp": {SERVER_NAME: {"type": "remote", "url": url,
                               "headers": {"Authorization": f"Bearer {token}"}}}},
        indent=2,
    )
    return (
        f"opencode — run this:\n{command}\n\n"
        f"…or merge this into ~/.config/opencode/opencode.json:\n{block}"
    )


def _hermes(url: str, token: str) -> str:
    # The block leads because `hermes mcp add` is interactive: it asks for the token at a prompt and
    # probes the server before it will save anything. That prompt is the better path for somebody at
    # a terminal — the token never reaches their shell history — so it is offered second.
    block = (
        "mcp_servers:\n"
        f"  {SERVER_NAME}:\n"
        f'    url: "{url}"\n'
        "    headers:\n"
        f'      Authorization: "Bearer {token}"\n'
        "    enabled: true"
    )
    command = (
        f"  hermes mcp add {SERVER_NAME} --url {shlex.quote(url)} --auth header"
    )
    return (
        f"Hermes — add this at the top level of ~/.hermes/config.yaml:\n{block}\n\n"
        f"…or run this, which asks for the token instead of putting it in your shell history:\n"
        f"{command}"
    )


_RENDERERS = {
    "claude": _claude,
    "codex": _codex,
    "copilot": _copilot,
    "hermes": _hermes,
    "opencode": _opencode,
}
