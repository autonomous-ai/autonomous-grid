"""Grid lifecycle + overview: `grid`, `grid version`, `grid start/stop/ls/info`."""
from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import sys
from typing import Any
from urllib.parse import urlparse

import httpx

from local import config
from local import runtime
from shared import paths, run_records, shell, state
from shared._version import __version__


def cmd_version(args: argparse.Namespace) -> int:
    print(f"grid {__version__}")
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    name = args.name or "home"
    cfg = _grid_by_name(name)
    if cfg is None:
        _reject_foreign_grid(name)  # a known remote grid or an id-shaped arg → don't auto-create junk
        cfg = runtime.init_grid_config(
            name=name,
            port=args.port if args.port is not None else runtime.DEFAULT_PORT,
            host=args.host if args.host is not None else runtime.DEFAULT_HOST,
            advertise_host=args.advertise_host,
        )
    else:
        cfg, _ = _apply_up_overrides(cfg, args)
    # Both paths, first run included — a busy port must never be something the reader has to
    # resolve before they can get started.
    cfg, _ = _resolve_port(cfg)
    config.save_grid_config(cfg["grid_id"], cfg)
    runtime.start_grid(cfg)
    cfg, local_only = _resolve_address(cfg)
    _report_up(cfg, local_only)
    return 0


def _resolve_address(cfg: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Fall back to a working address instead of warning about a broken one.

    ``detect_local_ip`` asks which interface reaches the internet, which on a machine holding a VPN
    is not the interface anything reaches *back* on. The grid is bound to every interface and is
    perfectly healthy; only the address it hands out is wrong, and the symptom is every later
    command failing with "Server disconnected without sending a response".

    Nothing needs restarting to fix it — the server already listens on 0.0.0.0, so pointing the
    advertised URL at loopback makes it work on this machine immediately. That is a real
    reduction in reach, so the caller says so — in three words, not a paragraph.
    """
    url = runtime.grid_url(cfg)
    if runtime.advertised_address_works(url):
        return cfg, False

    loopback = runtime.make_local_url(cfg["port"], "127.0.0.1")
    if not runtime.advertised_address_works(loopback):
        return cfg, True

    updated = dict(cfg)
    updated["lan_signaling_url"] = loopback
    config.save_grid_config(updated["grid_id"], updated)
    return updated, True


def _report_up(cfg: dict[str, Any], local_only: bool) -> None:
    """Two facts and one command. Nothing else.

    Everything worth explaining here — a port that moved, an address that had to fall back — has
    already been *handled*, so narrating it only buries the one line the reader needs, which is
    what to type next. The address printed is the truth; how it was arrived at is not their
    problem. `local_only` is the single exception, because it changes what they can do next.
    """
    scope = "  (this computer only)" if local_only else ""
    print(f"\n✓ Grid '{cfg['name']}' running — {runtime.grid_url(cfg)}{scope}")
    print("\nNext:  grid engine install llama.cpp")
    print("See:   grid info")


def _resolve_port(cfg: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Move to a free port rather than asking the reader to pick one.

    `grid start` is the first command anyone runs, and a busy 8090 used to end the story there: an
    error about a port they never chose, no clue what was holding it, and — until the flag was
    honoured — no escape even by choosing another. A port conflict is not a decision anyone needs
    to be consulted about; it just needs solving, out loud.

    This applies to an explicit `--port` too. Being told "that one is taken, run it again with a
    different number" is exactly the loop worth deleting — we name what is holding it and move on,
    so the reader ends up running, not retrying.
    """
    port = int(cfg["port"])
    if not runtime.port_in_use(port):
        return cfg, None

    holder = runtime.port_holder(port)
    by = f" by {holder}" if holder else ""
    replacement = runtime.free_port_from(port + 1)
    if replacement is None:
        raise SystemExit(
            f"Port {port} is already in use{by}, and no free port was found near it.\n"
            f"Name one yourself, for example:  grid start {cfg['name']} --port 9500"
        )
    updated = dict(cfg)
    updated["port"] = replacement
    updated["lan_signaling_url"] = runtime.make_local_url(replacement, _advertised_host(cfg))
    return updated, f"Port {port} is in use{by} — starting on {replacement} instead."


def _apply_up_overrides(cfg: dict[str, Any], args: argparse.Namespace) -> tuple[dict[str, Any], list[str]]:
    """Let `--port` / `--host` / `--advertise-host` change a grid that already exists.

    These used to be read only when creating a grid. Bringing an existing one up dropped them
    silently, which produced the worst error text in the CLI: `grid start --port 8099` answering
    "Port 8090 is already in use. Choose a different --port." — naming the stored port, telling
    you to change a flag you had just changed, and doing it again for every port you tried.

    A grid is down at this moment, so re-addressing it is safe; the URL it hands out is rebuilt
    from whatever the new values are.
    """
    changes = {
        key: value
        for key, value in (
            ("port", args.port),
            ("host", args.host),
            ("advertise_host", args.advertise_host),
        )
        if value is not None
    }
    if not changes:
        return cfg, []

    updated = dict(cfg)
    if "port" in changes:
        updated["port"] = int(changes["port"])
    if "host" in changes:
        updated["host"] = changes["host"]
    # Rebuilt from the new port whether or not `--advertise-host` was given, since the URL carries
    # the port too — otherwise a port change would leave the old one advertised.
    updated["lan_signaling_url"] = runtime.make_local_url(
        updated["port"], changes.get("advertise_host") or _advertised_host(cfg)
    )
    # Port is announced by the caller after the free-port check, so only host is reported here.
    notes = [
        f"host changed: {cfg.get('host')} -> {updated['host']}"
    ] if "host" in changes and cfg.get("host") != updated["host"] else []
    return updated, notes


def _advertised_host(cfg: dict[str, Any]) -> str | None:
    """The host previously handed out, so a port-only change keeps the address it was reached at."""
    previous = urlparse(cfg.get("lan_signaling_url") or "").hostname
    return previous or None


def cmd_down(args: argparse.Namespace) -> int:
    cfg = config.select_grid(args.name)
    if not cfg.get("managed_server", True):
        print(f"{cfg['name']} is hosted by another box; nothing to stop here.")
        return 0
    outcome = runtime.stop_grid(cfg)
    if not outcome.stopped():
        _refuse_false_stop(cfg, outcome)
    print(f"\n✓ Grid '{cfg['name']}' stopped. Its setup is kept.")
    # `shlex.quote` only — a grid name is freeform and can carry a space ("Hydrate Grid"), and a
    # hint printed without quoting it isn't actually copy-pasteable (grid-leave issue: this exact
    # `Next:` line, unquoted, is what argparse rejected as "unrecognized arguments").
    print(f"\nNext:  grid start {shlex.quote(cfg['name'])}")
    return 0


def _refuse_false_stop(cfg: dict[str, Any], outcome: runtime.StopOutcome) -> None:
    """Fail a `grid stop` that did not stop the grid, naming what is still running and a remedy that
    reaches it.

    Three ways to get here and each needs a different next step: a server that outlived SIGKILL, a
    grid still answering on its port, and a teardown that established nothing at all. What they share
    is that none may print "is down" — and that the recorded identity is **kept**, because unlike a
    run record there is no argv sweep behind it, so it is the only handle a retry has.
    """
    windows = sys.platform == "win32"
    survivor = outcome.teardown.survivor
    if survivor:
        remedy = (
            # `/T` as well as `/F`: the teardown that just failed was already a forced tree kill, so
            # suggesting a retry weaker than the thing that did not work would waste the operator's
            # next attempt.
            f"taskkill /F /T /PID {survivor}" if windows
            else f"kill -9 -{survivor}" if outcome.teardown.is_group
            else f"kill -9 {survivor}"
        )
        raise SystemExit(
            f"grid stop: could not stop the server for {cfg['name']} "
            f"({run_records.describe_survivor(outcome.teardown)}). It keeps serving this grid until "
            f"it stops, and the recorded pid is kept so a retried `grid stop` still reaches it "
            f"(e.g. `{remedy}`)."
        )

    port = cfg.get("port")
    if outcome.serving:
        # Reachable precisely when nothing signallable was recorded, so there is no pid to name — the
        # remedy has to start from the port instead.
        finder = f"netstat -ano | findstr :{port}" if windows else f"lsof -ti :{port}"
        raise SystemExit(
            f"grid stop: {cfg['name']} is still answering on port {port}, so this box is still "
            f"serving it — nothing was stopped that the config could prove was the grid's own "
            f"server. Find what is listening with `{finder}`, then retry `grid stop {cfg['name']}`."
        )

    probe = runtime.probe_url(cfg)
    check = (
        # The WHOLE url quoted, because it is built from the config's `host` — config-controlled text
        # reaching the terminal — so `repr` escapes any control byte, and the quotes also make the
        # suggested command safe to paste into a shell.
        f"the check by hand is `curl -s {probe + '/grid/info'!r}`" if probe
        else f"the config's port ({port!r}) is unusable, so there is nothing to check it with"
    )
    raise SystemExit(
        f"grid stop: could not confirm the server for {cfg['name']} stopped, and could not reach its "
        f"port to find out — so nothing about this box was established. The recorded pid is kept for "
        f"a retry; {check}."
    )


def cmd_delete(args: argparse.Namespace) -> int:
    """Remove a grid's local config for good — `grid stop` only pauses it.

    There was no way to make `grid ls` forget a grid short of deleting `~/.grid/grids/<id>` by
    hand, which meant knowing that path exists at all. This is the missing other half of `create,
    then throw away` — the flow `grid stop` deliberately does not cover, because config surviving a
    stop is what lets `grid start <name>` bring it straight back.
    """
    cfg = config.select_grid(args.name)
    if not cfg.get("managed_server", True):
        raise SystemExit(
            f"{cfg['name']!r} is a remote grid — nothing local to delete. "
            f"`grid ls` only forgets it here; the grid itself lives on the account that owns it."
        )
    if not args.yes:
        response = input(
            f"Delete grid {cfg['name']!r} ({cfg['grid_id']})? This removes its local config and "
            "cannot be undone. [y/N] "
        ).strip().lower()
        if response != "y":
            print("Aborted.")
            return 1

    runtime.stop_grid(cfg)  # idempotent — a no-op if it was already down
    shutil.rmtree(paths.grid_dir(cfg["grid_id"]), ignore_errors=True)
    if state.get_active("local") in (cfg["name"], cfg["grid_id"]):
        state.set_active("local", None)
    print(f"Deleted grid {cfg['name']!r}.")
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    grids = config.iter_grid_configs()
    if getattr(args, "json", False):
        print(json.dumps([
            {
                "grid": cfg["name"],
                "id": cfg["grid_id"],
                "grid_url": runtime.grid_url(cfg),
                "local": bool(cfg.get("managed_server", True)),
            }
            for cfg in grids
        ], indent=2))
        return 0
    if not grids:
        print("(no grids — run `grid start` to bring one online)")
        return 0
    for cfg in grids:
        where = "local" if cfg.get("managed_server", True) else "remote"
        print(f"{cfg['name']}\t{cfg['grid_id']}\t{where}\t{runtime.grid_url(cfg)}")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    cfg = config.select_grid(args.grid)
    grid_url = runtime.grid_url(cfg)

    if args.env:
        # Quoted the same way as the remote form, though the local token is a constant and the URL
        # is built from this machine's own config: two commands printing the same block in two
        # different quoting styles is how one of them stays wrong after the other is fixed.
        print(f"export OPENAI_BASE_URL={shell.quote(f'{grid_url}/v1')}")
        print(f"export OPENAI_API_KEY={shell.quote('local-grid')}")
        return 0

    engines, reachable = _live_engines(grid_url)
    models = _unique_models(engines)

    if args.json:
        print(json.dumps({
            "grid": cfg["name"],
            "grid_url": grid_url,
            "engines": [_engine_entry(engine) for engine in engines],
            "models": models,
        }, indent=2))
        return 0

    print(f"grid={cfg['name']}")
    print(f"grid_url={grid_url}")
    if not reachable:
        print("status=unreachable")
        return 0
    print(f"engines={len(engines)}")
    print(f"models={','.join(models) if models else '(none)'}")
    return 0


# ---------------------------------------------------------------------------
# overview (`grid` with no subcommand)
# ---------------------------------------------------------------------------

def cmd_overview(args: argparse.Namespace) -> int:
    # Mode is stamped by dispatch; fall back to the persisted mode for direct calls.
    mode = getattr(args, "mode", None) or state.get_mode()
    as_json = getattr(args, "json", False)
    if mode == "remote":
        return _overview_remote(as_json)
    return _overview_local(as_json)


def _overview_remote(as_json: bool) -> int:
    active = state.get_active("remote")
    if as_json:
        print(json.dumps({"mode": "remote", "grid": active}, indent=2))
        return 0
    print("mode: remote")
    print(f"active grid: {active}" if active else "active grid: (none)")
    print("\nSign in with `grid login`, then manage your remote grids with `grid start`/`ls`/`info`, "
          "serve models with `grid join`, and use them with `grid chat -m <model> \"…\"`.")
    # `remote` is the default for a new install (ADR 0001 D-2, amended), so this screen is the first
    # thing a new user sees — and without this line the local mode has no signpost anywhere.
    print("Or run a grid on this machine alone, no account needed: `grid mode local`.")
    return 0


def _overview_local(as_json: bool) -> int:
    grids = config.iter_grid_configs()
    if not grids:
        if as_json:
            print(json.dumps(
                {"mode": "local", "grid": None, "grid_url": None, "engines": [], "models": []},
                indent=2,
            ))
            return 0
        print("mode: local\n")
        print("No grid yet.\n")
        print("Start one:\n  grid start\n")
        print("Then join an engine:\n  grid join")
        return 0

    default = config.select_grid(None) if _has_default(grids) else grids[0]
    grid_url = runtime.grid_url(default)
    engines, reachable = _live_engines(grid_url)
    models = _unique_models(engines)

    if as_json:
        print(json.dumps({
            "mode": "local",
            "grid": default["name"],
            "grid_url": grid_url,
            "engines": [_engine_entry(engine) for engine in engines],
            "models": models,
        }, indent=2))
        return 0

    print("mode: local")
    print(f"Grid: {default['name']}")
    print(f"grid_url: {grid_url}")
    if not reachable:
        print("status: unreachable — start it with `grid start`")
    else:
        print(f"engines: {len(engines)} live")
        print(f"models: {', '.join(models) if models else '(none)'}")
    print("\nNext:")
    print("  grid join")
    if models:
        print(f'  grid chat -m {models[0]} "hello"')
    print("  grid info --env")
    return 0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

# Local grid ids are minted as ``ag-<slug>-<hex8>`` (local/runtime.init_grid_config). `grid start` uses this
# to refuse auto-creating a junk grid when the arg is an unsynced id, not a new name (ADR 0011 D-f).
# fullmatch (anchored) so a real name like ``ag-team`` still creates.
_GRID_ID_RE = re.compile(r"ag-.+-[0-9a-f]{8}")


def _looks_like_grid_id(name: str) -> bool:
    return bool(_GRID_ID_RE.fullmatch(name))


def _reject_foreign_grid(name: str) -> None:
    """Refuse to auto-create when `name` is really an existing grid the user hasn't synced here — one of
    their known remote grids (exact name/id match, zero false-positive), or a string shaped like a minted
    local grid id — instead of silently making a junk local grid named after it (ADR 0011 D-f). Runs
    before any create/start, so nothing is written or spawned."""
    from . import remote_grid

    if remote_grid._by_name(name) is not None:  # a grid from `grid login`, pasted in local mode
        raise SystemExit(
            f"{name!r} is one of your remote grids, not a new local grid. Switch to it with "
            f"`grid mode remote` (or `grid --remote start {name}`)."
        )
    if _looks_like_grid_id(name):
        raise SystemExit(
            f"No local grid with id {name!r}. That looks like a grid id, not a new grid's name — run "
            f"`grid ls` to see your grids (or `grid mode remote` + `grid sync` for a remote one)."
        )


def _grid_by_name(name: str) -> dict[str, Any] | None:
    for cfg in config.iter_grid_configs():
        if cfg.get("name") == name or cfg.get("grid_id") == name:
            return cfg
    return None


def _has_default(grids: list[dict[str, Any]]) -> bool:
    active = state.get_active("local")
    if active and any(cfg.get("grid_id") == active or cfg.get("name") == active for cfg in grids):
        return True
    return len(grids) == 1 or any(cfg.get("name") == "home" for cfg in grids)


def _live_engines(grid_url: str) -> tuple[list[dict[str, Any]], bool]:
    try:
        resp = httpx.get(f"{grid_url}/nodes/discover", timeout=3)
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError):
        return [], False
    if not isinstance(body, dict):
        return [], False
    return body.get("engines", []), True


def _engine_entry(engine: dict[str, Any]) -> dict[str, Any]:
    return {
        "engine": engine.get("name") or engine.get("node_id", "?"),
        "where": engine.get("endpoint_url") or engine.get("media_url") or "",
        "models": engine.get("models") or [],
    }


def _unique_models(engines: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for engine in engines:
        for model in engine.get("models") or []:
            if model not in seen:
                seen.append(model)
    return seen
