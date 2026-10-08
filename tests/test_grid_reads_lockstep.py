"""What `grid-reads-without-waking` makes load-bearing across four repositories, pinned (issue 01).

The harness reads every grid of an account in the background, and it must never wake one by doing so.
It relies on a chain of hand-duplicated values, none of which any import path connects:

* the proxy (grid-apis `wake_routes`) wakes a sleeping grid for a read of its overview or its provider
  discovery ONLY when the request carries a credential — presence, not validity;
* the master (grid-src) serves both reads with no authentication at all, so a credential-less read of an
  AWAKE grid answers exactly as a signed-in one;
* the field names and path literals of those two reads, which grid-apis' sleep record, this CLI's
  exact-case map and the harness reader all parse;
* ``grid_asleep``, the ``--no-wake`` literal, and three timing values: a model is dropped only after
  150s (a node TTL plus one heartbeat), a record needs 180s of uptime, and a record older than 29 days is
  ignored because the master forgets node rows at 30.
* how few requests one poll costs: `grid stats --json` hands the viewer `grid engines --json` and `grid
  models --json` under ``listings`` (one overview read, not three), and a token without the creator's
  ``admin`` role — which grid-apis never takes from a creator — skips the creator-only status, which
  could only refuse it.

⚠️ **Every drift here fails toward a stale list or a wake, and the wake is SILENT**: reclassify a route
so an anonymous GET wakes, or put a credential check on one, and every open harness keeps every grid it
can see awake again while its lists look perfect. That is why the proxy half also has a CI pin in
grid-apis (`test_read_polls_never_record_nor_wake`).

⚠️ Per this repository's rule for every cross-repo assertion, the grid-apis and grid-src cases **skip
unless that worktree sits beside this one** — they skip in CI, and a green CI proves nothing about them.
The harness half landed (issue 02), so a harness pin that finds nothing FAILS rather than skips.

The canonical values come from `grid-protocol` (grid-platform ticket 12), the third party every side is pinned
against, never from either side: a pin that reads one side's constant and compares it to itself checks nothing.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import pathlib
import re
from typing import NamedTuple

import pytest

import cli
from cli import remote_grid, remote_overview
from remote import relay
from shared import paths, run_records, user_agent
from tests._remote_seed import seed_remote_grid
from tests.grid_src_repo import grid_apis_root, grid_src_private_server, harness_root
from tests.protocol_ast import protocol_value

from grid_protocol import _codegen
from grid_protocol import constants as protocol_constants

OVERVIEW_ROUTE = protocol_constants.OVERVIEW_PATH.lstrip("/")
DISCOVER_ROUTE = protocol_constants.DISCOVER_PATH.lstrip("/")
ASLEEP_CODE = protocol_constants.GRID_ASLEEP_CODE
ASLEEP_STATE = "asleep"
NO_WAKE = protocol_constants.NO_WAKE_FLAG
LAST_KNOWN_KEY = "last_known"
LAST_KNOWN_FIELDS = ["age_seconds", "nodes", "ids"]
OVERVIEW_NODE_FIELDS = ["name", "engine", "models"]
DISCOVERY_FIELDS = ["providers", "models", "capabilities", "raw_model_id"]
ROUTE_ID_PREFIX = protocol_constants.PROVIDER_ROUTE_PREFIX
RETENTION_SECONDS = protocol_constants.MODEL_RETENTION_SECONDS
CAPTURE_MIN_UPTIME_SECONDS = protocol_constants.SLEEP_RECORD_MIN_UPTIME_SECONDS
RECORD_MAX_AGE_DAYS = protocol_constants.LAST_KNOWN_MAX_AGE_SECONDS / 86400
ACCESS_LOSS_SETTLE_SECONDS = protocol_constants.ACCESS_LOSS_SETTLE_SECONDS
GRID_INFO_FIELDS = ["status", "grid_url"]
#: What `grid stats --json` carries beside its rollup, so the viewer reads a grid once per poll.
STATS_LISTINGS_KEY = "listings"
STATS_LISTINGS_FIELDS = ["engines", "models"]
#: The role grid-apis grants a grid's creator and keeps; a readable token without it is a member's.
ADMIN_ROLE = "admin"
#: The run-record keys the harness reads today (`cli/src/lib/localModels.ts`), and issue 02's `servedHere`.
RUN_RECORD_FIELDS = ["engines", "models", "advertise_as", "node_id", "meta_name", "ctx_size", "media", "pid"]
MASTER_NODE_PRUNE_DAYS = protocol_constants.MASTER_NODE_PRUNE_SECONDS / 86400

_SKIP_APIS = "the grid-apis worktree is not beside this one; the lockstep cannot be checked here"
_SKIP_SRC = "the grid-src worktree is not beside this one; the lockstep cannot be checked here"
_SKIP_HARNESS = "the autonomous-harness worktree is not beside this one; the lockstep cannot be checked here"
_GONE = ("the harness no longer has it — issue 02 landed, so an absence is a rename or a move: teach this pin "
         "where it went rather than letting it pass")


# --- reading the siblings --------------------------------------------------------------------------


def _source(root: pathlib.Path | None, relative: str, skip: str) -> pathlib.Path:
    """A sibling's file. Only "no such repository" skips: a checkout without the file means it moved,
    which is drift, and skipping would turn the pin off blaming the wrong thing."""
    if root is None:
        pytest.skip(skip)
    path = root / relative
    if not path.exists():
        raise AssertionError(f"{root} has no {relative} — teach this check where it went rather than skipping")
    return path


def _tree(root: pathlib.Path | None, relative: str, skip: str) -> ast.Module:
    return ast.parse(_source(root, relative, skip).read_text())


def _number(node: ast.AST) -> float:
    """A constant, or a product/sum of constants (`29 * 86400`) — nothing that has to be run."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Add)):
        left, right = _number(node.left), _number(node.right)
        return left * right if isinstance(node.op, ast.Mult) else left + right
    raise AssertionError(f"not a plain number: {ast.unparse(node)}")


def _assigned(tree: ast.Module, name: str) -> ast.AST:
    found = [
        node.value for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None
        and any(getattr(target, "id", None) == name
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target]))
    ]
    assert len(found) == 1, f"expected exactly one module-level `{name} = …`, found {len(found)}"
    return found[0]


def _function(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    found = [node for node in ast.walk(tree)
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name]
    assert len(found) == 1, f"expected exactly one `def {name}`, found {len(found)}"
    return found[0]


def _strings(node: ast.AST) -> set[str]:
    return {sub.value for sub in ast.walk(node) if isinstance(sub, ast.Constant) and isinstance(sub.value, str)}


def _apis_wake_routes():
    """grid-apis' route rules, loaded in isolation: the module is stdlib-only, so its OWN functions can
    answer the question rather than a re-reading of its table that could drift from how it is used."""
    path = _source(grid_apis_root(), "grid_networks/wake_routes.py", _SKIP_APIS)
    spec = importlib.util.spec_from_file_location("_grid_apis_wake_routes", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:  # it grew an import of its own package: load it another way
        raise AssertionError(f"grid-apis' wake_routes.py no longer loads on its own ({exc}) — teach this "
                             f"check to read it") from exc
    return module


def _node_ttl_seconds() -> int:
    tree = _tree(grid_src_private_server(), "config.py", _SKIP_SRC)
    # The default is a literal, or (since grid-platform ticket 12) `str(protocol.NODE_TTL_SECONDS)`.
    defaults = [
        protocol_value(node.args[1]) for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "getenv"
        and len(node.args) == 2 and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "NODE_TTL_SECONDS"
    ]
    assert len(defaults) == 1, "grid-src's config no longer reads NODE_TTL_SECONDS with one default"
    return int(defaults[0])


def _master_node_prune_days() -> float:
    prune = _function(_tree(grid_src_private_server(), "registry.py", _SKIP_SRC), "prune_stale_nodes")
    # `timedelta(days=30)`, or (since grid-platform ticket 12) `timedelta(seconds=protocol.MASTER_NODE_PRUNE_SECONDS)`.
    per_unit = {"days": 1.0, "seconds": 1 / 86400}
    days = [
        float(protocol_value(keyword.value)) * per_unit[keyword.arg] for node in ast.walk(prune)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "timedelta"
        for keyword in node.keywords if keyword.arg in per_unit
    ]
    assert len(days) == 1, "grid-src's prune_stale_nodes no longer names its window as one timedelta(days|seconds=…)"
    return days[0]


# --- this CLI's halves: need no sibling, so they are the ones that run in CI ------------------------


def test_this_cli_reads_the_canonical_paths():
    assert remote_overview.OVERVIEW_PATH == f"/{OVERVIEW_ROUTE}"
    assert remote_overview.DISCOVER_PATH == f"/{DISCOVER_ROUTE}"


@pytest.mark.parametrize("command", ["models", "engines", "stats"])
def test_the_three_reads_take_the_canonical_no_wake_flag(command):
    assert remote_overview.NO_WAKE_FLAG == NO_WAKE
    assert cli.build_parser().parse_args([command, NO_WAKE]).no_wake is True


def test_this_cli_reads_the_canonical_asleep_code_and_state():
    assert relay.GRID_ASLEEP_CODE == ASLEEP_CODE
    assert remote_grid.ASLEEP_STATE == ASLEEP_STATE


def test_this_clis_user_agent_says_when_it_sends_no_credential():
    """Read by people counting journal lines (the zero-baseline alarm keys on it), not by a program."""
    assert user_agent.relay_user_agent(has_credential=True).startswith("grid-cli/")
    assert user_agent.relay_user_agent(has_credential=False) == (
        user_agent.relay_user_agent(has_credential=True) + " (no-wake)")


def test_grid_info_json_names_the_status_and_address_the_harness_reads(monkeypatch, tmp_path, capsys):
    """The harness resolves each grid's address, and its OWN grid's owner status, from `grid info --json`
    (issue 02). The word `asleep` passes through unchanged."""
    seed_remote_grid(monkeypatch, tmp_path, state_word=ASLEEP_STATE)

    assert cli.main(["info", "--json"]) == 0

    view = json.loads(capsys.readouterr().out)
    assert set(GRID_INFO_FIELDS) <= set(view), view
    assert view["status"] == ASLEEP_STATE
    assert view["grid_url"] == "https://relay.example"


def test_grid_stats_json_hands_out_the_listings_the_harness_viewer_reads(monkeypatch, tmp_path, capsys):
    """The viewer reads a grid ONCE per poll, from `grid stats --json`'s ``listings``. Renamed here, it
    silently goes back to three commands — three overview reads and three `grid` processes a poll."""
    seed_remote_grid(monkeypatch, tmp_path)
    monkeypatch.setattr(remote_overview, "fetch_overview",
                        lambda *_args, **_kwargs: {"grid": {"state": "running"}, "nodes": []})

    assert cli.main(["stats", NO_WAKE, "--json"]) == 0

    listings = json.loads(capsys.readouterr().out)[STATS_LISTINGS_KEY]
    assert sorted(listings) == sorted(STATS_LISTINGS_FIELDS)


def test_this_cli_reads_a_token_without_the_creators_role_as_a_members():
    assert remote_grid.ADMIN_ROLE == ADMIN_ROLE


def _record_keys() -> set[str]:
    """The keys `grid join` writes into a remote run record (`cli/remote_provider._build_record`)."""
    source = pathlib.Path(cli.__file__).parent / "remote_provider.py"
    build = _function(ast.parse(source.read_text()), "_build_record")
    returned = [node.value for node in ast.walk(build) if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)]
    assert len(returned) == 1, "`_build_record` no longer returns one dict literal — teach this check its shape"
    return {key.value for key in returned[0].keys if isinstance(key, ast.Constant)}


def test_a_run_record_carries_every_field_the_harness_reads(tmp_path, monkeypatch):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    missing = sorted(set(RUN_RECORD_FIELDS) - _record_keys())
    assert not missing, f"`grid join` no longer writes {missing} into a run record"
    assert paths.engines_dir("n1") == tmp_path / "run" / "engines" / "n1"
    assert run_records.heartbeat_path("n1", "remote").name == "remote.heartbeat"


# --- grid-apis: the proxy's route rules, and the sleep record --------------------------------------


@pytest.mark.parametrize("route", [OVERVIEW_ROUTE, DISCOVER_ROUTE])
def test_the_proxy_wakes_neither_read_without_a_credential(route):
    rules = _apis_wake_routes()
    assert rules.route_class("GET", route) == rules.WAKE_SIGNED_IN
    assert rules.may_wake("GET", route, {}) is False, "an anonymous read must never wake a grid"


@pytest.mark.parametrize("headers", [{"authorization": "Bearer anything"}, {"x-api-key": "anything"}])
@pytest.mark.parametrize("route", [OVERVIEW_ROUTE, DISCOVER_ROUTE])
def test_the_credential_rule_is_presence_only(route, headers):
    """The proxy verifies nothing — the master does. So `--no-wake` has only to send NOTHING."""
    assert _apis_wake_routes().may_wake("GET", route, headers) is True


def test_the_sleep_record_reads_the_canonical_paths_and_fields():
    tree = _tree(grid_apis_root(), "grid_networks/sleep_record.py", _SKIP_APIS)
    assert ast.literal_eval(_assigned(tree, "OVERVIEW_PATH")) == OVERVIEW_ROUTE
    assert ast.literal_eval(_assigned(tree, "DISCOVER_PATH")) == DISCOVER_ROUTE
    assert ast.literal_eval(_assigned(tree, "_ROUTE_ID_PREFIX")) == ROUTE_ID_PREFIX
    read = _strings(tree)
    missing = [field for field in [*OVERVIEW_NODE_FIELDS, "id", *DISCOVERY_FIELDS] if field not in read]
    assert not missing, f"grid-apis' sleep record no longer reads {missing}"


def test_the_control_plane_grants_the_creator_the_admin_role_and_keeps_it():
    """A creator whose token lacked ``admin`` would be read as a member: its live status — the
    authoritative address, and `asleep`/`stopped` answered with no request — would never be asked."""
    source = _source(grid_apis_root(), "grid_networks/store.py", _SKIP_APIS).read_text()
    assert re.search(rf"owner_roles\s*=\s*\[[^\]]*[\"']{ADMIN_ROLE}[\"']", source), (
        "grid-apis no longer grants the creator `admin` when it creates a grid")
    assert re.search(rf"==\s*normalize_email\(network\.owner_email\)[\s\S]{{0,200}}\|\s*\{{\s*[\"']{ADMIN_ROLE}[\"']\s*\}}",
                     source), "grid-apis no longer re-adds the creator's `admin` on a membership write"


def test_the_asleep_answer_carries_last_known_in_the_canonical_shape():
    proxy = _tree(grid_apis_root(), "grid_proxy.py", _SKIP_APIS)
    assert LAST_KNOWN_KEY in _strings(proxy), "the proxy no longer puts `last_known` on the asleep answer"
    answer = _function(_tree(grid_apis_root(), "grid_networks/sleep_record.py", _SKIP_APIS), "answer_field")
    shapes = [[key.value for key in node.keys] for node in ast.walk(answer) if isinstance(node, ast.Dict)]
    assert LAST_KNOWN_FIELDS in shapes, f"`answer_field` no longer builds {LAST_KNOWN_FIELDS}: {shapes}"


def test_a_record_is_ignored_before_the_master_forgets_its_nodes():
    tree = _tree(grid_apis_root(), "grid_networks/sleep_record.py", _SKIP_APIS)
    max_age = _number(_assigned(tree, "MAX_AGE_SECONDS"))
    assert max_age == RECORD_MAX_AGE_DAYS * 86400
    assert _master_node_prune_days() == MASTER_NODE_PRUNE_DAYS
    assert max_age < MASTER_NODE_PRUNE_DAYS * 86400


def test_a_capture_waits_out_a_node_ttl_and_a_beat():
    """A just-woken master has not heard from its providers: it must be up past one TTL and one beat."""
    tree = _tree(grid_apis_root(), "grid_networks/sleep_record.py", _SKIP_APIS)
    uptime = _number(_assigned(tree, "MIN_UPTIME_SECONDS"))
    assert uptime == CAPTURE_MIN_UPTIME_SECONDS
    assert uptime >= _node_ttl_seconds() + relay.HEARTBEAT_INTERVAL


def test_no_record_is_written_while_a_removed_machine_can_still_be_listed():
    """A removed person's machine stays in the master's node list until its last heartbeat ages out, so the
    record's access-loss fence must outlast a node TTL and a beat."""
    tree = _tree(grid_apis_root(), "grid_networks/sleep_record.py", _SKIP_APIS)
    settle = _number(_assigned(tree, "ACCESS_LOSS_SETTLE_SECONDS"))
    assert settle == ACCESS_LOSS_SETTLE_SECONDS
    assert settle >= _node_ttl_seconds() + relay.HEARTBEAT_INTERVAL


# --- grid-src: both reads public, and the fields they publish --------------------------------------


def _handler_for(tree: ast.Module, method: str, path: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    found = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call) and getattr(decorator.func, "attr", None) == method
        and decorator.args and isinstance(decorator.args[0], ast.Constant) and decorator.args[0].value == path
    ]
    assert len(found) == 1, f"expected exactly one handler for {method.upper()} {path}, found {len(found)}"
    return found[0]


def _assert_unauthenticated(handler: ast.AST, allowed_args: list[str]) -> None:
    args = [arg.arg for arg in handler.args.args]
    assert args == allowed_args, (
        f"`{handler.name}` now takes {args} — a `request` or a dependency is where authentication goes, "
        f"and an authenticated read here makes every credential-less read fail while the grid is awake")
    called = {getattr(node.func, "id", None) or getattr(node.func, "attr", None)
              for node in ast.walk(handler) if isinstance(node, ast.Call)}
    assert not any("auth" in (name or "").lower() for name in called), called


def test_the_overview_is_public_at_the_master():
    relay_tree = _tree(grid_src_private_server(), "relay.py", _SKIP_SRC)
    prefixes = [
        keyword.value.value for node in ast.walk(relay_tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "APIRouter"
        for keyword in node.keywords if keyword.arg == "prefix"
    ]
    assert prefixes == ["/relay/v1"]
    _assert_unauthenticated(_handler_for(relay_tree, "get", "/grid/overview"), [])


def test_provider_discovery_is_public_at_the_master():
    server = _tree(grid_src_private_server(), "server.py", _SKIP_SRC)
    _assert_unauthenticated(_handler_for(server, "get", f"/{DISCOVER_ROUTE}"), ["model"])


def test_the_master_publishes_the_fields_the_readers_read():
    root = grid_src_private_server()
    overview = _function(_tree(root, "overview.py", _SKIP_SRC), "build_grid_overview")
    published = _strings(overview)
    missing = [field for field in ["models", "nodes", *OVERVIEW_NODE_FIELDS] if field not in published]
    assert not missing, f"grid-src's overview no longer publishes {missing}"
    assert "id" in _strings(_function(_tree(root, "overview.py", _SKIP_SRC), "_curated_payload"))

    info = next(node for node in _tree(root, "models.py", _SKIP_SRC).body
                if isinstance(node, ast.ClassDef) and node.name == "ProviderInfo")
    fields = {node.target.id for node in info.body if isinstance(node, ast.AnnAssign)}
    assert {"models", "capabilities"} <= fields, fields

    model_ids = _tree(root, "model_ids.py", _SKIP_SRC)
    assert "raw_model_id" in _strings(model_ids)
    assert protocol_value(_assigned(model_ids, "PROVIDER_PREFIX")) == ROUTE_ID_PREFIX
    assert ".gguf" in _strings(_function(model_ids, "display_model_name")), (
        "the display rule every reader applies is a trailing `.gguf` removed, case kept")


def test_a_model_outlives_a_node_ttl_and_a_beat():
    assert RETENTION_SECONDS >= _node_ttl_seconds() + relay.HEARTBEAT_INTERVAL


# --- the harness: fail on any other spelling, and on an absence -------------------------------------
#
# ⚠️ **The names below are the contract for issue 02**: the two literals are found wherever the harness
# QUOTES them (since grid-platform ticket 12 that includes its copy of `gridProtocol.ts`), and the two timings
# by these exact constant names (either unit), written as digits or as a `grid-protocol` constant times 1000.
# Issue 02 has landed, so a pin that finds nothing FAILS: skipping would let a rename pass for ever. Test
# files are not read — a harness test proving that `'GRID_ASLEEP'` is NOT the code spells it
# wrong on purpose. A constant that merely looks similar (`pasteDropFiles.RETENTION_MS`, 24h, measured on
# the harness at the time of writing) is exactly what a looser match would have read instead.

_HARNESS_TREES = ("cli/src", "store/agents/autonomous-grid")
_HARNESS_SUFFIXES = (".ts", ".mjs", ".js")
_HARNESS_RETENTION = re.compile(r"\bGRID_MODEL_RETENTION_(SECONDS|MS)\s*(?::\s*\w+\s*)?=\s*([0-9A-Z_*\s]+)[;\n]")
_HARNESS_MAX_AGE = re.compile(r"\bGRID_LAST_KNOWN_MAX_AGE_(SECONDS|MS)\s*(?::\s*\w+\s*)?=\s*([0-9A-Z_*\s]+)[;\n]")
#: The harness's copy of the generated contract, which must equal what `protocol/` generates.
_HARNESS_PROTOCOL = "cli/src/lib/gridProtocol.ts"
#: The harness's copies of this repository's recordings, which its reader is tested against.
_HARNESS_FIXTURES = "cli/src/lib/__fixtures__/protocol"


def _harness_files() -> list[pathlib.Path]:
    root = harness_root()
    if root is None:
        pytest.skip(_SKIP_HARNESS)
    return [
        path for tree in _HARNESS_TREES for path in (root / tree).rglob("*")
        if path.suffix in _HARNESS_SUFFIXES and path.is_file() and "node_modules" not in path.parts
        and not re.search(r"\.(spec|test)\.", path.name)
    ]


def _harness_spellings(pattern: str) -> dict[str, list[str]]:
    """Every QUOTED spelling ``pattern`` finds in the harness's source, with the files it is in."""
    quoted = re.compile(rf"(['\"`])({pattern})\1", flags=re.IGNORECASE)
    found: dict[str, list[str]] = {}
    for path in _harness_files():
        for match in quoted.finditer(path.read_text(errors="replace")):
            found.setdefault(match.group(2), []).append(str(path))
    return found


@pytest.mark.parametrize(("pattern", "canonical"), [
    (r"--no[-_ ]?wake", NO_WAKE),
    (r"grid[-_ ]?asleep", ASLEEP_CODE),
])
def test_the_harness_spells_each_literal_as_this_side_does(pattern, canonical):
    spellings = _harness_spellings(pattern)
    if not spellings:
        raise AssertionError(_GONE)
    assert set(spellings) == {canonical}, (
        f"the harness spells it {sorted(spellings)} — the CLI and the proxy say {canonical!r}. Renamed on "
        f"one side, the viewer is refused with exit 2 (the flag) or reads 'not answering' (the code)")


def _harness_timing(pattern: re.Pattern[str]) -> list[float]:
    """Every value the harness assigns the named constant, in seconds (``_MS`` → milliseconds)."""
    values: list[float] = []
    for path in _harness_files():
        for unit, expression in pattern.findall(path.read_text(errors="replace")):
            # Digits, `*`, and grid-protocol's UPPER_CASE constants — nothing else can reach eval.
            names = {name: getattr(protocol_constants, name, None)
                     for name in re.findall(r"[A-Z][A-Z0-9_]*", expression)}
            missing = [name for name, value in names.items() if not isinstance(value, (int, float))]
            assert not missing, f"{path}: {missing} are not grid-protocol numbers"
            literal = re.sub(r"(?<=\d)_(?=\d)", "", expression)
            value = float(eval(literal, {"__builtins__": {}}, names))
            values.append(value / 1000 if unit == "MS" else value)
    return values


def test_the_harness_keeps_a_model_past_a_node_ttl_and_a_beat():
    values = _harness_timing(_HARNESS_RETENTION)
    if not values:
        raise AssertionError(_GONE)
    assert all(seconds >= RETENTION_SECONDS for seconds in values), (
        f"the harness's GRID_MODEL_RETENTION is {values}s — under a node TTL plus a beat, a cold wake "
        f"blanks a list")


def test_the_harness_ignores_a_record_before_the_master_forgets_its_nodes():
    values = _harness_timing(_HARNESS_MAX_AGE)
    if not values:
        raise AssertionError(_GONE)
    assert all(seconds <= RECORD_MAX_AGE_DAYS * 86400 < MASTER_NODE_PRUNE_DAYS * 86400 for seconds in values), (
        values)


#: How the harness spells a run record's folder (`join(home, 'run', 'engines', gridId)`). A function that
#: builds it is a function that reads grid's run records — which is what scopes the pin below.
_HARNESS_RUN_RECORD_PATH = "'run', 'engines'"
#: `localModels.owned` (the Model Manager's own engines) and `readRunRecords` (`servedHere`), measured
#: 2026-10-07. One more or one fewer is a move to teach this check, never a pass on what is left in scope.
_HARNESS_RUN_RECORD_READERS = 2
#: The harness's OWN record of an engine it started in another app (`AppEngineRecord`, `app-engines.json`,
#: written by the harness itself — PR #569), read in the same file through a variable also named `record`.
#: A block that names it reads that record, not grid's.
_HARNESS_APP_RECORD = re.compile(r"\b(?:AppEngineRecord|readAppRecords)\b")
#: A read of a record's field. A block's keys are matched in its RAW source — a `${record.x}` in a template
#: literal stays visible, and a comment that mentions one can only err toward failing.
_RECORD_READ = re.compile(r"\brecord\.(\w+)")
#: Comments, then string and template literals: blanked before braces are matched, so none can unbalance
#: them. Whichever starts first wins, which is how a `//` inside a URL string stays a string.
_TS_NOT_CODE = re.compile(r"//[^\n]*|/\*[\s\S]*?\*/|'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|`(?:\\.|[^`\\])*`")
#: A function's name, from its header — everything since the last `;`, `{` or `}` at its own level, which
#: spans several statements in a file written without semicolons. The `$` is what picks the function's own
#: name out of that: the identifier right before the parameter list that ends the header (parentheses nested
#: two deep inside it, as a callback parameter's own callback is).
_TS_NAMED = re.compile(
    r"(\w+)\s*(?:=\s*(?:async\s*)?)?(?:<[^()]*>)?\s*\((?:[^()]|\((?:[^()]|\([^()]*\))*\))*\)[^()]*$")


class _TsBlock(NamedTuple):
    name: str  # the function's, or "(unnamed)" for an object or type literal
    line: int
    start: int  # the offset of its `{` in the file
    source: str  # raw, comments and strings included


class _TsOpen(NamedTuple):
    start: int
    kind: str  # "class" (a class body), "signature" (a type literal in a header) or "block"
    nested: bool  # inside a function body, an object literal or a signature: never reported on its own
    header: str


def _ts_code_only(source: str) -> str:
    """``source`` with the same offsets, comments blanked to spaces and strings to `_` — so a header ending in
    a string-literal type (`): 'a' | 'b'`) still ends in a word, not in the `|` that opens a type literal."""
    return _TS_NOT_CODE.sub(
        lambda match: re.sub(r"[^\n]", " " if match.group().startswith("/") else "_", match.group()), source)


def _ts_signature_literal(header: str) -> bool:
    """Whether the `{` after ``header`` opens a type literal in it — `Promise<{ … }>`, `(record: { … })`,
    `): { … } | null` — rather than a body. It belongs to the header, so the body after it keeps its name.
    An escaped paren (`/\\(build/`) is not a paren, and only a `<` written against a name opens a generic:
    `t < 5` is a comparison."""
    unclosed = re.sub(r"\\.", "", header.replace("=>", ""))
    return (unclosed.count("(") > unclosed.count(")") or len(re.findall(r"\w<", unclosed)) > unclosed.count(">")
            or unclosed.endswith((":", "|", "&")))


def _ts_top_level_blocks(source: str) -> list[_TsBlock]:
    """Every block at the top of a TypeScript file or of a class in it — each function body, and the object
    and type literals beside them, which nothing here asks for by name.

    A misreading that unbalances the braces is refused here. One that does not — a header misjudged as a
    type literal, which folds a body into the next header — drops a block instead, and that is what
    `_harness_run_record_scope` refuses: a read of a record that sits in no block this found.
    """
    code = _ts_code_only(source)

    def line(offset: int) -> int:
        return code.count("\n", 0, offset) + 1

    found: list[_TsBlock] = []
    opened: list[_TsOpen] = []
    header_from = 0
    for offset, char in enumerate(code):
        top = not opened or opened[-1].kind == "class"
        if char == ";" and top:
            header_from = offset + 1
        elif char == "{":
            header = " ".join(code[header_from:offset].split())
            kind = ("block" if not top else "signature" if _ts_signature_literal(header)
                    else "class" if re.search(r"\bclass\b(?!\s*[:?])", header) else "block")
            opened.append(_TsOpen(offset, kind, not top, header))
            if top and kind != "signature":
                header_from = offset + 1
        elif char == "}":
            assert opened, f"an unmatched `}}` on line {line(offset)} — teach this check the harness's syntax"
            block = opened.pop()
            if block.kind == "block" and not block.nested:
                named = _TS_NAMED.search(block.header)
                found.append(_TsBlock(named.group(1) if named else "(unnamed)", line(block.start), block.start,
                                      source[block.start:offset + 1]))
            if block.kind != "signature" and (not opened or opened[-1].kind == "class"):
                header_from = offset + 1
    assert not opened, (f"an unmatched `{{` on line {line(opened[-1].start)} (after `{opened[-1].header[-80:]}`) "
                        f"— teach this check the harness's syntax")
    return found


def _harness_run_record_scope(source: str) -> list[_TsBlock]:
    """The blocks of the harness's `localModels.ts` that read grid's run records, each move refused loudly."""
    blocks = _ts_top_level_blocks(source)
    code = _ts_code_only(source)
    spans = [(block.start, block.start + len(block.source)) for block in blocks]
    dropped = sorted({code.count("\n", 0, read.start()) + 1 for read in _RECORD_READ.finditer(code)
                      if not any(start <= read.start() < end for start, end in spans)})
    assert not dropped, (
        f"`record.<field>` on line(s) {dropped} sits outside every block this check found — in an "
        f"expression-bodied arrow, a class field or a default value, or in a function it misread and dropped: "
        f"teach it where that read is rather than letting it go unchecked")
    readers = [block for block in blocks if _HARNESS_RUN_RECORD_PATH in block.source]
    assert len(readers) == _HARNESS_RUN_RECORD_READERS, (
        f"expected {_HARNESS_RUN_RECORD_READERS} functions that find run records under run/engines/<grid id> "
        f"(`owned`, `readRunRecords`), found {[f'{b.name} (line {b.line})' for b in readers]} — the reading "
        f"moved: teach this check where it went rather than letting it pass on what is left")
    for reader in readers:
        assert _RECORD_READ.search(reader.source), (
            f"`{reader.name}` (line {reader.line}) builds the run-record path but reads no `record.<field>` — "
            f"the reads moved out of this check's reach: teach it where they went")
    handed = {called for reader in readers for called in re.findall(r"\b(\w+)\(\s*record\s*\)", reader.source)}
    scope = readers + [block for block in blocks if block.name in handed]
    unclassified = [f"{block.name} (line {block.line})" for block in blocks
                    if block not in scope and _RECORD_READ.search(block.source)
                    and not _HARNESS_APP_RECORD.search(block.source)]
    assert not unclassified, (
        f"{unclassified} read `record.<field>`, but none of them finds run records under run/engines/<grid "
        f"id>, is handed one by a function that does, or reads the harness's own `AppEngineRecord` — teach "
        f"this check which record that is rather than leaving its keys unchecked")
    return scope


def test_the_harness_reads_only_run_record_fields_grid_join_writes():
    """Not skipped: the harness reads run records TODAY, from the same layout — in the functions that build
    their folder's path (`localModels.owned`, `readRunRecords`) and in the helpers those hand a record to
    (`recordPid`). A key they read that `grid join` stopped writing is a model that silently stops being
    recognised as this computer's own.

    ⚠️ Those functions, never the whole file: since harness PR #569 (2026-10-02) `localModels.ts` also
    reads the harness's OWN app-engine records (`AppEngineRecord` in `app-engines.json`, which the harness
    writes itself) through a variable that is also named `record`, and a file-wide match reported their
    `alias`, `engine`, `grid`, `modelId` and `name` as keys `grid join` had stopped writing. A reader that
    moves is reported as a move, never a pass on whatever is left in scope: exactly
    `_HARNESS_RUN_RECORD_READERS` readers are demanded, each reading the record itself; any other block
    reading a `record.<field>` must be one about the harness's own record, or it fails as unclassified; and
    a read in no block at all fails too — it is outside every function body, or in one this check's reading
    of TypeScript dropped. Only that spelling is seen — `record?.x`, `record['x']` and destructuring are
    not, as before."""
    root = harness_root()
    if root is None:
        pytest.skip(_SKIP_HARNESS)
    source = _source(root, "cli/src/lib/localModels.ts", _SKIP_HARNESS).read_text()
    read = {field for block in _harness_run_record_scope(source) for field in _RECORD_READ.findall(block.source)}
    unknown = sorted(read - _record_keys())
    assert not unknown, f"the harness reads {unknown}, which `grid join` does not write into a run record"
    assert "'.heartbeat'" in source, "the harness no longer reads the heartbeat sidecar beside a record"


def test_the_harness_viewer_reads_the_listings_this_cli_prints():
    root = harness_root()
    if root is None:
        pytest.skip(_SKIP_HARNESS)
    source = _source(root, "store/agents/autonomous-grid/lib/telemetry.mjs", _SKIP_HARNESS).read_text()
    if not re.search(rf"\b{STATS_LISTINGS_KEY}\b", source):  # not `listing`: the viewer already names `grid ls` that
        pytest.skip("the harness viewer does not read `grid stats --json`'s listings yet — this pin turns "
                    "itself on the moment any spelling of it appears")
    for field in STATS_LISTINGS_FIELDS:
        assert re.search(rf"\b{STATS_LISTINGS_KEY}\??\.{field}\b", source), (
            f"the viewer no longer reads `{STATS_LISTINGS_KEY}.{field}` — this CLI prints it there")


def test_the_harness_carries_the_contract_this_repository_generates():
    """The harness reads every path, code and field name off its copy of `gridProtocol.ts`. A copy that is
    stale or edited by hand is a harness reading a contract nobody else speaks — so it must equal, byte for
    byte, what `protocol/` generates today (grid-platform ticket 12)."""
    root = harness_root()
    if root is None:
        pytest.skip(_SKIP_HARNESS)
    copy = _source(root, _HARNESS_PROTOCOL, _SKIP_HARNESS).read_text(encoding="utf-8")
    assert copy == _codegen.TYPESCRIPT_FILE.read_text(encoding="utf-8"), (
        f"the harness's {_HARNESS_PROTOCOL} is not this repository's protocol/typescript/gridProtocol.ts — "
        f"copy the generated file over it, whole")
    # Its tests read real answers: copies of this repository's recordings, which must not drift from them.
    fixtures = sorted((root / _HARNESS_FIXTURES).glob("*.json"))
    assert fixtures, f"the harness has no recordings under {_HARNESS_FIXTURES} — teach this check where they went"
    recordings = _codegen.PACKAGE_DIR.parent / "recordings"
    for fixture in fixtures:
        assert fixture.read_bytes() == (recordings / fixture.name).read_bytes(), (
            f"the harness's {fixture.name} is not protocol/recordings/{fixture.name} — copy it over, whole")
