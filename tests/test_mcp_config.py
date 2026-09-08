"""`grid mcp config` — the harness-facing half of ADR 0041.

The command reads local state, renews the credential when it is about to die, and prints. What is
worth pinning is therefore not "does it run" but the things that are wrong in a way nobody would
notice:

- **the URL keeps its trailing slash.** Without it the control-plane mount answers 307 (measured).
- **the base is the signed-in control plane**, not a hardcoded production host — a home logged in
  against dev must print a dev URL, or the config silently points at the wrong fleet.
- **each harness's spelling is its own, and one of them takes no command at all.** Measured
  2026-09-08 against Claude Code 2.1.263, Codex 0.153.4, Copilot 1.0.83, opencode 1.18.29 and
  Hermes 0.21.1, by running each binary against a throwaway home and reading the file it wrote,
  then proving the header arrives with a header-logging listener.
- **the listing view prints no credential**, so the command people run with no arguments does not
  put a live 365-day token on their screen.
- **a token inside the expiry margin is renewed before it is printed**, and a config that has just
  been pasted into a harness is not one that dies tomorrow.

⚠️ Every case here goes through the REAL parser (`_invoke`), never a hand-built ``Namespace``. The
previous version of this suite asserted a `--json` payload by setting ``json=True`` on a namespace
the `mcp config` parser never produced — the flag existed only as the *global* `grid --json`, which
is not the spelling the docs give, and the test would have passed just the same had the branch been
unreachable altogether.
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from cli import mcp_config

API_URL = "https://api-grid.example.invalid"
NETWORK_ID = "ag-0000000000000001"
REFRESH = "a-refresh-credential"

_YEAR = 365 * 24 * 3600


def _jwt(claims: dict) -> str:
    """A JWT-shaped token whose payload carries ``claims``. No signature is checked anywhere in this
    CLI (``credentials.claims_from_token`` says why), so an unsigned one is a faithful double."""
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{body}.sig"


def _token(*, expires_in: float) -> str:
    return _jwt({"exp": int(time.time() + expires_in)})


FRESH = _token(expires_in=_YEAR)


def _sign_in(monkeypatch, tmp_path, *, access: str = FRESH, refresh: str | None = REFRESH,
             name: str = "team") -> None:
    """One grid in the local store, signed in against a non-default control plane."""
    from remote import credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    record: dict[str, object] = {
        "network_id": NETWORK_ID,
        "name": name,
        "access_token": access,
        "lan_signaling_url": "https://relay.example.invalid",
    }
    if refresh is not None:
        record["refresh_token"] = refresh
    credentials.save_credentials(
        {"session_token": "session", "api_url": API_URL, "networks": [record]}
    )


def _invoke(*argv: str) -> int:
    """Run `grid mcp …` through the real parser, so a flag that does not exist fails here."""
    from cli.parser import build_parser

    args = build_parser().parse_args(["mcp", *argv])
    return args.handler(args)


@pytest.fixture
def signed_in(monkeypatch, tmp_path):
    _sign_in(monkeypatch, tmp_path)


@pytest.fixture
def no_refresh_calls(monkeypatch):
    """Fail loudly if anything reaches the control plane. A test that means "this made no network
    call" has to be able to tell that from "the call was made and quietly succeeded"."""
    from remote import control_plane

    def _refuse(**kwargs):
        raise AssertionError(f"the control plane was called: {kwargs}")

    monkeypatch.setattr(control_plane, "refresh_network_token", _refuse)


# ---------------------------------------------------------------------------
# The listing view
# ---------------------------------------------------------------------------

def test_no_harness_lists_them_all_and_prints_no_credential(signed_in, capsys, no_refresh_calls):
    """`grid mcp config` with no target is the command people run first. It names the URL and every
    harness it can configure — and deliberately no token, so the arg-less invocation cannot put a
    live 365-day credential on a screen somebody is sharing."""
    assert _invoke("config") == 0
    out = capsys.readouterr().out

    assert FRESH not in out
    for harness in ("claude", "codex", "copilot", "hermes", "opencode"):
        assert f"--harness {harness}" in out
    assert f"{API_URL}{mcp_config.MOUNT_PATH}/" in out


def test_the_listing_keeps_the_grid_you_named(monkeypatch, tmp_path, capsys, no_refresh_calls):
    """The commands it suggests must configure the grid the user asked about, not the active one."""
    _sign_in(monkeypatch, tmp_path, name="other-team")

    assert _invoke("config", "other-team") == 0
    assert "--harness claude other-team" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# One harness at a time — every shape measured against the binary, not its documentation
# ---------------------------------------------------------------------------

def test_claude_gets_the_command_and_the_file_that_command_writes(signed_in, capsys):
    """Measured on 2.1.263: `--scope user` writes `mcpServers.<n>` into `~/.claude.json` with
    `type: "http"`. Both spellings are printed because a person who keeps their config in git wants
    the file, not a command that edits it behind them."""
    assert _invoke("config", "--harness", "claude") == 0
    out = capsys.readouterr().out

    assert "claude mcp add --transport http --scope user" in out
    assert f"--header 'Authorization: Bearer {FRESH}'" in out
    assert '"type": "http"' in out
    assert '"mcpServers"' in out


def test_codex_gets_a_block_and_never_a_command(signed_in, capsys):
    """⚠️ Codex has no `--header` flag — still true on 0.153.4 — so a printed `codex mcp add …
    --header …` would be an invalid-argument error in the user's terminal. This is the assertion
    that catches somebody "tidying" the harnesses into one shape."""
    assert _invoke("config", "--harness", "codex") == 0
    out = capsys.readouterr().out

    assert "codex mcp add" not in out
    assert f'http_headers = {{ Authorization = "Bearer {FRESH}" }}' in out


def test_codex_keeps_the_hyphen_in_the_server_name(signed_in, capsys):
    """`codex mcp add` writes `[mcp_servers.grid-web]` itself — measured — so a hyphen is a legal
    TOML bare key here. Spelling it `grid_web` would rename the tools this one harness sees
    (`grid_web__web_search` against everyone else's `grid-web`)."""
    assert _invoke("config", "--harness", "codex") == 0
    assert f"[mcp_servers.{mcp_config.SERVER_NAME}]" in capsys.readouterr().out


def test_copilot_gets_the_command_and_the_file(signed_in, capsys):
    """Measured on 1.0.83: `copilot mcp add --transport http --header "K: V"` writes
    `mcpServers.<n>` into `~/.copilot/mcp-config.json`, and the header reaches the server."""
    assert _invoke("config", "--harness", "copilot") == 0
    out = capsys.readouterr().out

    assert "copilot mcp add --transport http" in out
    assert f"--header 'Authorization: Bearer {FRESH}'" in out
    assert "~/.copilot/mcp-config.json" in out


def test_opencode_headers_are_key_equals_value_on_the_command_line(signed_in, capsys):
    """⚠️ opencode's `--header` takes `KEY=VALUE`, not the `Key: value` every other harness takes.
    Printed with a colon it is accepted and the header is silently wrong."""
    assert _invoke("config", "--harness", "opencode") == 0
    out = capsys.readouterr().out

    assert f"--header 'Authorization=Bearer {FRESH}'" in out
    assert '"type": "remote"' in out


def test_hermes_yaml_starts_at_column_zero(signed_in, capsys):
    """⚠️ The one block whose indentation is load-bearing. TOML and JSON ignore leading whitespace;
    YAML does not, so a `mcp_servers:` printed indented is a block that cannot be pasted."""
    assert _invoke("config", "--harness", "hermes") == 0
    out = capsys.readouterr().out

    assert "\nmcp_servers:\n" in out
    assert f'Authorization: "Bearer {FRESH}"' in out


def test_every_harness_that_takes_one_is_told_the_credential_is_live(signed_in, capsys):
    """A 365-day token on a screen should have been named as one before somebody pastes it into an
    issue."""
    assert _invoke("config", "--harness", "claude") == 0
    assert "access token" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# pi — a harness with no MCP client at all
# ---------------------------------------------------------------------------

def test_pi_is_refused_with_the_reason_and_never_reaches_the_credential_store(monkeypatch, tmp_path):
    """pi ships no MCP client, by design ("No MCP." in its own README, 0.84.1). Answering argparse's
    "invalid choice" would read as an oversight in this command; answering "run `grid login`" would
    be worse still, so the refusal is decided **before** a grid is resolved — this case runs with no
    credential store at all and must still say the same thing."""
    monkeypatch.setenv("GRID_HOME", str(tmp_path))  # signed out: no grids, no tokens

    with pytest.raises(SystemExit) as refused:
        _invoke("config", "--harness", "pi")

    message = str(refused.value)
    assert "pi" in message
    assert "grid login" not in message


# ---------------------------------------------------------------------------
# The machine-readable triple, and the flag that replaced `grid mcp token`
# ---------------------------------------------------------------------------

def test_json_is_a_machine_readable_triple(signed_in, capsys):
    assert _invoke("config", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "server": mcp_config.SERVER_NAME,
        "url": f"{API_URL}{mcp_config.MOUNT_PATH}/",
        "authorization": f"Bearer {FRESH}",
    }


def test_json_and_harness_cannot_be_combined(signed_in):
    """One asks for a harness's own spelling, the other for the values with no spelling at all.
    Silently ignoring either would hand a script the wrong thing."""
    from cli.parser import build_parser

    with pytest.raises(SystemExit) as refused:
        build_parser().parse_args(["mcp", "config", "--json", "--harness", "codex"])
    assert refused.value.code == 2


def test_the_token_subcommand_is_gone(signed_in):
    """`grid mcp token` was removed: `--json` carries the same value in a shape a script can read
    without a second command to keep in step."""
    from cli.parser import build_parser

    with pytest.raises(SystemExit) as refused:
        build_parser().parse_args(["mcp", "token"])
    assert refused.value.code == 2
    assert not hasattr(mcp_config, "cmd_mcp_token")


# ---------------------------------------------------------------------------
# The URL and the control plane behind it
# ---------------------------------------------------------------------------

def test_url_is_the_signed_in_control_plane_with_a_trailing_slash(signed_in, capsys):
    assert _invoke("config", "--harness", "claude") == 0
    out = capsys.readouterr().out

    assert f"{API_URL}{mcp_config.MOUNT_PATH}/" in out
    # The default production host must not appear: a home signed in against dev prints dev.
    assert "api-grid.autonomous.ai" not in out


def test_a_grid_with_no_token_says_to_sign_in(monkeypatch, tmp_path):
    """The familiar sentence, not an empty bearer pasted into a config that then 401s forever."""
    from remote import credentials

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    credentials.save_credentials(
        {
            "session_token": "session",
            "api_url": API_URL,
            "networks": [{"network_id": "ag-0000000000000002", "name": "tokenless"}],
        }
    )
    with pytest.raises(SystemExit) as refused:
        _invoke("config", "--harness", "claude")
    assert "grid login" in str(refused.value)


def test_an_unknown_grid_name_is_refused(signed_in):
    with pytest.raises(SystemExit) as refused:
        _invoke("config", "--harness", "claude", "no-such-grid")
    assert "no-such-grid" in str(refused.value)


# ---------------------------------------------------------------------------
# Renewing the credential before it is printed
# ---------------------------------------------------------------------------

def _capture_refresh(monkeypatch, *, access: str, refresh: str | None = "rotated-refresh") -> list:
    """Stand in for the control plane's token exchange, recording what it was asked for."""
    from remote import control_plane

    calls: list[dict] = []

    def _exchange(**kwargs):
        calls.append(kwargs)
        bundle = {"access_token": access}
        if refresh is not None:
            bundle["refresh_token"] = refresh
        return bundle

    monkeypatch.setattr(control_plane, "refresh_network_token", _exchange)
    return calls


def test_a_token_inside_the_margin_is_renewed_before_it_is_printed(monkeypatch, tmp_path, capsys):
    """A config is pasted once and lives in a file for months, so a token with a fortnight left is
    a harness that stops working in a fortnight. Renewing mints a fresh 365-day one."""
    stale = _token(expires_in=14 * 24 * 3600)
    renewed = _token(expires_in=_YEAR)
    _sign_in(monkeypatch, tmp_path, access=stale)
    calls = _capture_refresh(monkeypatch, access=renewed)

    assert _invoke("config", "--harness", "codex") == 0

    out = capsys.readouterr().out
    assert renewed in out
    assert stale not in out
    assert calls == [{"network_id": NETWORK_ID, "refresh_token": REFRESH}]


def test_the_renewed_pair_is_persisted_so_the_next_run_can_renew_too(monkeypatch, tmp_path, capsys):
    """⚠️ The control plane rotates the refresh credential on every exchange and the old one stops
    matching immediately. Storing the access token alone destroys this machine's ability to renew
    anything ever again — silently, and not until some later day."""
    from remote import credentials

    _sign_in(monkeypatch, tmp_path, access=_token(expires_in=3600))
    renewed = _token(expires_in=_YEAR)
    _capture_refresh(monkeypatch, access=renewed, refresh="rotated-refresh")

    assert _invoke("config", "--harness", "codex") == 0

    stored = credentials.load_credentials()["networks"][0]
    assert stored["access_token"] == renewed
    assert stored["refresh_token"] == "rotated-refresh"


def test_a_token_with_a_year_left_is_printed_without_a_network_call(signed_in, capsys,
                                                                    no_refresh_calls):
    """The margin is what keeps this command cheap: a healthy token is local work only."""
    assert _invoke("config", "--harness", "codex") == 0
    assert FRESH in capsys.readouterr().out


def test_the_listing_view_never_renews(monkeypatch, tmp_path, capsys, no_refresh_calls):
    """Nothing is handed out, so nothing needs renewing — and an arg-less command must not rotate a
    credential (or need the network at all) to print a menu."""
    _sign_in(monkeypatch, tmp_path, access=_token(expires_in=3600))

    assert _invoke("config") == 0
    assert "--harness claude" in capsys.readouterr().out


def test_a_failed_renewal_of_a_still_valid_token_warns_and_prints_it_anyway(monkeypatch, tmp_path,
                                                                           capsys):
    """A control-plane hiccup must not cost somebody a config they could have had: the token still
    works today. Named on stderr, never swallowed."""
    from remote import control_plane

    stale = _token(expires_in=2 * 24 * 3600)
    _sign_in(monkeypatch, tmp_path, access=stale)

    def _fails(**_kwargs):
        raise control_plane.ControlPlaneError("token refresh failed (503): busy", status=503)

    monkeypatch.setattr(control_plane, "refresh_network_token", _fails)

    assert _invoke("config", "--harness", "codex") == 0
    captured = capsys.readouterr()
    assert stale in captured.out
    assert "couldn't renew" in captured.err
    # ⚠️ Not "launching anyway": the sentence is shared with `grid launch`, and this command launches
    # nothing. A message about a command the user did not run is how a shared string goes wrong.
    assert "printing it anyway" in captured.err
    assert "launching" not in captured.err


def test_an_expired_token_with_nothing_to_renew_it_is_refused(monkeypatch, tmp_path, capsys):
    """⚠️ The one case that must NOT degrade into printing. `grid launch` prints a warning here and
    lets its relay probe decide, but this command has no probe — the web-tools server is the control
    plane's — so an expired token would be pasted into a harness that then 401s forever, with the
    server's own refusal telling the user to re-run the command that gave it to them."""
    _sign_in(monkeypatch, tmp_path, access=_token(expires_in=-3600), refresh=None)

    with pytest.raises(SystemExit) as refused:
        _invoke("config", "--harness", "codex")
    assert "grid login" in str(refused.value)
    # Said once. The shared repair warns about this same state in its own words for `grid launch`,
    # which goes on to ask its relay; printing both leaves the user reading one fact twice.
    assert "no refresh credential is stored" not in capsys.readouterr().err


def test_an_expired_token_that_can_be_renewed_is_renewed(monkeypatch, tmp_path, capsys):
    """Expiry is not a dead end while a refresh credential is stored — which is what makes the
    control plane's own 401 sentence ("run `grid mcp config`") true rather than a loop."""
    renewed = _token(expires_in=_YEAR)
    _sign_in(monkeypatch, tmp_path, access=_token(expires_in=-3600))
    _capture_refresh(monkeypatch, access=renewed)

    assert _invoke("config", "--harness", "codex") == 0
    assert renewed in capsys.readouterr().out
