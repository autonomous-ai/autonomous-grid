"""`grid mcp config` / `grid mcp token` — pointing a coding agent's harness at the grid's web tools.

ADR 0041. The server is the control plane's (`/v1/grid/web-mcp`); this half exists so nobody has to
open `~/.grid/credentials.toml` and pick a token out of it by hand. That file holds one bundle per
grid, so "by hand" means choosing between them correctly, and teaching people to open their own
credential store is a habit worth not starting.

**Three harnesses, three spellings, one credential.** Measured 2026-09-07 against Claude Code 2.1.263
and Codex 0.144.6, with a header-logging listener rather than vendor documentation:

- Claude Code takes `--header` on `claude mcp add`, so it gets a command.
- ⚠️ **Codex has no `--header` flag** — `codex mcp add` offers only `--bearer-token-env-var` and the
  OAuth options. It nonetheless honours `http_headers` in `config.toml` (proven on the wire; `codex
  mcp get` shows the field and masks its value, which it never does for a key it does not know), so
  Codex gets a **block to paste**, not a command. An environment variable was rejected: a 365-day
  credential in a shell is readable by every child process of that shell.
- opencode takes `headers` in its JSON config.

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

# ↔ grid-apis `grid_networks/web_mcp.MOUNT_PATH`. Roll the CONTROL PLANE out before this CLI: a
# `grid mcp config` against an older control plane prints a URL that answers a bare 404, which the
# harness reports as a server it cannot reach.
MOUNT_PATH = "/v1/grid/web-mcp"

# What the tools are called in the harness's own listing — an agent sees `mcp__grid-web__web_search`.
SERVER_NAME = "grid-web"


def _session(args: argparse.Namespace) -> tuple[str, str, str]:
    """`(url, token, label)` for the grid this command names, or a clean `SystemExit`.

    Nothing here is a network call. The control-plane base comes from the same `credentials.api_url`
    every other command uses, so a home signed in against dev prints a dev URL without being told.
    """
    from remote import credentials

    from . import remote_grid

    rec = remote_grid._select(getattr(args, "grid", None))
    label = str(rec.get("name") or rec.get("network_id") or "?")
    token = remote_grid.require_access_token(rec, label)
    url = credentials.api_url().rstrip("/") + MOUNT_PATH + "/"
    return url, token, label


def cmd_mcp_token(args: argparse.Namespace) -> int:
    """Print the grid's access token and nothing else, for a script that builds its own config."""
    _url, token, _label = _session(args)
    print(token)
    return 0


def cmd_mcp_config(args: argparse.Namespace) -> int:
    """Print how to point each harness at this grid's web tools."""
    url, token, label = _session(args)

    if getattr(args, "json", False):
        # The same three values, for something assembling a config itself. No postcondition to
        # guard: this command reports what is already true locally and writes nothing.
        print(json.dumps({"server": SERVER_NAME, "url": url, "authorization": f"Bearer {token}"},
                         indent=2))
        return 0

    header = f"Authorization: Bearer {token}"
    print(f"Web tools for grid {label}.\n")
    print("Claude Code — run this:")
    print(
        f"  claude mcp add --transport http --scope user {SERVER_NAME} {shlex.quote(url)} "
        f"--header {shlex.quote(header)}\n"
    )
    # `--scope user` on purpose: `--scope project` writes `.mcp.json`, which Claude Code holds for
    # interactive approval — measured. Web search is not a per-repository capability anyway.
    print("Codex — add this to ~/.codex/config.toml (it has no --header flag):")
    print(f"  [mcp_servers.{SERVER_NAME.replace('-', '_')}]")
    print(f'  url = "{url}"')
    print(f'  http_headers = {{ Authorization = "Bearer {token}" }}\n')
    print("opencode — add this to your opencode.json:")
    print(json.dumps(
        {"mcp": {SERVER_NAME: {"type": "remote", "url": url, "enabled": True,
                               "headers": {"Authorization": f"Bearer {token}"}}}},
        indent=2,
    ))
    print(
        "\nThis prints your grid's access token. Anyone who has it can search the web on your "
        "account's allowance."
    )
    return 0
