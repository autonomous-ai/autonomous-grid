"""The five billing values that cross a repository boundary, pinned (ADR 0042 D-l).

A grid's money is counted in **credits** — a thousandth of a dollar — and four wire values plus one
sentence carry that unit between three repositories that share no code. Each is hand-duplicated and
kept in step by editing both sides; this file is what says so out loud.

⚠️ **Not one of the five has a half in THIS repository**, which is why they are pinned here rather
than beside an implementation. Two run grid-src ↔ grid-apis, one is a bare number nobody sends, and
the last runs grid-src ↔ the Flutter app. This repository is the hub; the register
(`docs/agents/lockstep-register.md`) is the prose and this is the executable half of it.

⚠️ **Both sides are compared to a literal written HERE**, never to each other and never to one
side's own constant. A pin that reads grid-src's spelling and asserts grid-apis matches it is green
for any pair that agrees — including a pair that agreed on the wrong name, which is the only failure
this feature can actually produce. The canonical strings below are the third party.

⚠️ **A green continuous-integration run proves nothing about any of this.** Every check here skips
unless the sibling checkout sits beside this worktree, and in CI none does. Run it locally on a
machine that has them, and read `CLAUDE.md` § *Cross-repo lockstep* before changing any value.

What each failure costs, because they differ and the differences are the whole design:

* `cost_credits` — a loud 422 from the control plane, and loud **only** because ADR 0042 D-h taught
  the relay to read the reply's status. Revert that and this becomes a grid serving free in silence.
* `balance_credits` — the relay's `.get(…, 0.0)` default, and zero is below every threshold, so the
  grid refuses **every** request. Fail closed on the rename, deliberately.
* `min_balance_credits` — the relay falls back to its own figure, which must be the credit one.
* `CREDITS_PER_USD` — nothing degrades. No route is missing, no body refused, no postcondition to
  check: charges simply run on one scale and top-ups on the other, both answering 200.
* the refusal sentence — a **disclosure**, not an outage: the app's substring match misses and it
  renders the relay's raw text, which prints the viewer's exact balance and the grid's threshold
  into their chat window.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

from tests.grid_src_repo import app_root, grid_apis_root, grid_src_private_server

# ── the canonical values, written here so neither side can be compared to itself ────────────────
_COST_FIELD = "cost_credits"
_BALANCE_FIELD = "balance_credits"
_MIN_BALANCE_FIELD = "min_balance_credits"
_CREDITS_PER_USD = 1000
#: The app lowercases before matching, so the phrase is pinned in the relay's own capitalisation and
#: lowered for the app's half. Only the sentence's NUMBERS became credits; these bytes did not move.
_REFUSAL_PHRASE = "Insufficient balance"

#: Where the app's half lives, and the shape of it. The regex accepts either quote style — Dart
#: formatters move between them freely, and reporting that as drift is a false red on a seam whose
#: real failure is a disclosure. Applied per LINE, skipping comments, so a `//` note quoting the old
#: call cannot stand in for a live one.
_APP_CHAT_SENDER = ("lib", "features", "playground", "logic", "chat_sender.dart")
_APP_MATCH = re.compile(r"""contains\(\s*(['"])""" + re.escape(_REFUSAL_PHRASE.lower()) + r"""\1\s*\)""")

#: The names the rename replaced. Asserting the new spelling is present is half a pin — a side that
#: added the new key while keeping the old refuses nothing, and that is exactly the shape ADR 0042
#: D-c rules out ("accepting both names would make a thousand-fold unit error answer 200").
_RETIRED = {_COST_FIELD: "cost_usd",
            _BALANCE_FIELD: "balance",
            _MIN_BALANCE_FIELD: "min_balance_usd"}


_SKIP_NO_SIBLING = "the sibling checkout is not beside this worktree; this pin cannot run here"


def _sibling_file(path: pathlib.Path | None, *parts: str) -> pathlib.Path:
    """A file inside a sibling checkout, skipping only when that REPOSITORY is absent.

    ⚠️ **A resolved checkout missing the file RAISES**, following `tests/grid_apis_routes.py`. The
    resolver has already proved the marker directory is there, so a file absent from it was renamed
    or moved — which is drift, and drift is what this file exists to catch. Reporting it as "the
    sibling is not beside this one" would turn every pin here off with a message blaming a checkout
    that is sitting right there, and the credit rename would then have no executable proof at all.
    """
    if path is None:
        pytest.skip(_SKIP_NO_SIBLING)
    target = path.joinpath(*parts)
    if not target.exists():
        raise AssertionError(
            f"the checkout at {path} has no {'/'.join(parts)} — it was renamed or moved, so teach "
            f"this pin where it went rather than letting it skip")
    return target


def _source(path: pathlib.Path | None, *parts: str) -> ast.Module:
    """Parse a sibling repository's file, or skip when that repository is not on this machine."""
    return ast.parse(_sibling_file(path, *parts).read_text())


def _named(tree: ast.Module, name: str) -> ast.AST:
    """The top-level function or class called ``name``, or an assertion naming where to look."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            return node
    raise AssertionError(
        f"{name} is no longer defined at module level in the sibling repository — it was renamed or "
        f"moved, so teach this pin where it went rather than deleting the pin")


def _wire_keys(node: ast.AST) -> set[str]:
    """The names ``node`` actually uses AS WIRE KEYS: dict-literal keys, and `.get()` lookups.

    ⚠️ **Every other string in the function is deliberately invisible here, and that is the whole
    difference between this pin and a grep.** Both halves of this seam name their own field in
    prose — grid-apis' balance endpoint spells `balance_credits` inside its docstring — and the
    relay names it in log lines and URL fragments. Collecting every literal would let a payload key
    quietly become a *third* spelling (not the retired one, so the negative check misses it either)
    while a surviving comment or log message kept the pin green and grid-apis 422'd every report.

    Two shapes cover all five sites because they are the only two ways a JSON field is named in
    Python: `{"cost_credits": …}` on the way out and `r.json().get("balance_credits", …)` on the
    way back.
    """
    keys: set[str] = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.Dict):
            keys.update(k.value for k in inner.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str))
        elif (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "get" and inner.args
                and isinstance(inner.args[0], ast.Constant)
                and isinstance(inner.args[0].value, str)):
            keys.add(inner.args[0].value)
    return keys


def _returned_text(node: ast.AST) -> set[str]:
    """The string literals ``node`` RETURNS, f-string fragments included and docstring excluded.

    Its own reader rather than `_wire_keys`, because the refusal is not a wire key: it is prose the
    relay hands back in a body. Narrowed to `return` statements so a comment quoting the old phrase
    cannot stand in for the sentence itself.
    """
    return {inner.value
            for statement in ast.walk(node) if isinstance(statement, ast.Return)
            for inner in ast.walk(statement.value) if statement.value is not None
            if isinstance(inner, ast.Constant) and isinstance(inner.value, str)}


def _module_number(tree: ast.Module, name: str) -> float:
    """A module-level numeric constant, asserted to still BE a literal.

    A value computed at import — read from the environment, derived from another constant — is not
    something a cross-repository pin can compare, and silently reading `None` for it would turn this
    check off.
    """
    for node in tree.body:
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) and node.value is not None
                   else [])
        if not any(getattr(t, "id", None) == name for t in targets):
            continue
        assert isinstance(node.value, ast.Constant) and isinstance(node.value.value, (int, float)) \
            and not isinstance(node.value.value, bool), (
            f"{name} is no longer a plain numeric literal, so the two repositories' copies cannot "
            f"be compared — ADR 0042 D-a says this is a constant and not a setting, on purpose")
        return node.value.value
    raise AssertionError(
        f"{name} is no longer defined in the sibling repository — it was renamed or moved, so teach "
        f"this pin where it went rather than deleting it")


def _is_required(field: ast.AnnAssign) -> bool:
    """Whether a pydantic model field has no default — the only shape that REFUSES a body missing it.

    ⚠️ **Declared is not required, and the whole loudness argument rests on the difference.** With
    pydantic's default `extra='ignore'`, an old relay's surplus `cost_usd` is dropped in silence; it
    is the *missing required* `cost_credits` that raises. Give the field a default "to be lenient
    during the rollout" and that same body answers **200** with the cost read as the default — a
    grid serving free, with D-h's status check seeing nothing wrong.

    Required means either a bare annotation, or a `Field(...)` carrying neither a positional default
    nor `default=` / `default_factory=`. Any other value is a default.
    """
    if field.value is None:
        return True
    if isinstance(field.value, ast.Call) and getattr(field.value.func, "id", "") == "Field":
        return not field.value.args and not any(
            kw.arg in ("default", "default_factory") for kw in field.value.keywords)
    return False


def _relay() -> ast.Module:
    return _source(grid_src_private_server(), "relay.py")


def _control_plane(module: str) -> ast.Module:
    return _source(grid_apis_root(), "grid_networks", module)


def _speaks(function: str, tree: ast.Module, field: str) -> None:
    """``function`` names ``field`` and no longer names the value it replaced."""
    spoken = _wire_keys(_named(tree, function))
    assert field in spoken, (
        f"{function} does not name {field!r}; ADR 0042 D-c renames every wire field whose UNIT "
        f"changed, and the two halves of this seam are hand-duplicated")
    retired = _RETIRED[field]
    assert retired not in spoken, (
        f"{function} still names the retired {retired!r}. Keeping both spellings is the one outcome "
        f"D-c rules out: it makes a thousand-fold unit error answer 200 instead of refusing")


# ── the four wire values ─────────────────────────────────────────────────────────────────────────

def test_the_report_and_the_control_plane_agree_on_the_cost_field():
    """`POST /v1/grid/internal/usage` — the relay's payload key against the schema's required field.

    ⚠️ Loud only because the relay reads the reply's status (D-h). This pin says the two spellings
    match; it says nothing about the grid noticing when they do not.
    """
    _speaks("_report_usage", _relay(), _COST_FIELD)

    schema = _named(_control_plane("handler.py"), "UsageReportRequest")
    fields = {t.target.id: t for t in schema.body
              if isinstance(t, ast.AnnAssign) and isinstance(t.target, ast.Name)}
    assert _COST_FIELD in fields, (
        f"grid-apis' UsageReportRequest does not declare {_COST_FIELD!r}, so an old relay's body is "
        f"accepted rather than refused")
    assert _is_required(fields[_COST_FIELD]), (
        f"grid-apis' UsageReportRequest declares {_COST_FIELD!r} with a DEFAULT, so it is optional. "
        f"An old relay posting `cost_usd` then gets a 200 and is charged the default — the silent "
        f"free-serving this whole seam exists to close, and the sole reason the rollout is control "
        f"plane first. Only a REQUIRED field 422s (ADR 0042 D-c: the extra old key is ignored; it "
        f"is the missing required one that refuses)")
    assert _RETIRED[_COST_FIELD] not in fields, (
        "grid-apis' UsageReportRequest still declares the dollar field; accepting both is what D-c "
        "forbids")


def test_the_gate_and_the_control_plane_agree_on_the_balance_field():
    """`GET /v1/grid/internal/accounts/{sub}/balance` — the read the min-balance gate makes."""
    _speaks("_fetch_authoritative_balance", _relay(), _BALANCE_FIELD)
    _speaks("internal_account_balance", _control_plane("handler.py"), _BALANCE_FIELD)


def test_the_gate_and_the_control_plane_agree_on_the_minimum_field():
    """`GET /v1/grid/internal/min-balance` — the threshold that gate compares against."""
    _speaks("_fetch_min_balance", _relay(), _MIN_BALANCE_FIELD)
    _speaks("internal_min_balance", _control_plane("handler.py"), _MIN_BALANCE_FIELD)


def test_both_repositories_convert_at_the_same_ratio():
    """`CREDITS_PER_USD` — the only value here whose disagreement is pure arithmetic.

    Nothing on the wire carries it and nothing anywhere would report a mismatch: charges would run
    on one scale and top-ups on the other, both answering 200. This pin is the whole of the
    detection, which is why it compares each side to the number written above rather than to the
    other side.
    """
    assert _module_number(_relay(), "CREDITS_PER_USD") == _CREDITS_PER_USD, (
        "grid-src converts a cost to credits at a different ratio than ADR 0042 D-a states")
    assert _module_number(_control_plane("store.py"), "CREDITS_PER_USD") == _CREDITS_PER_USD, (
        "grid-apis turns a dollar top-up into credits at a different ratio than ADR 0042 D-a states")


# ── the sentence, which is a contract with a repository written in another language ──────────────

def test_the_refusal_the_app_matches_survives_byte_for_byte():
    """The 402's phrase — grid-src ↔ autonomous-grid-app, with nothing in this repository between.

    ⚠️ A reword costs a **disclosure**, not an outage: the app's match misses, it falls back to
    rendering the relay's raw sentence, and that sentence prints the viewer's exact balance and the
    grid's threshold into their chat window. Only the numbers in it became credits.
    """
    detail = _named(_relay(), "_insufficient_balance_detail")
    # `contains`, not `startsWith` — the app's own predicate. Pinning a prefix would report drift
    # for a reprefixed sentence the app still matches perfectly well.
    assert any(_REFUSAL_PHRASE.lower() in text.lower() for text in _returned_text(detail)), (
        f"grid-src's refusal no longer carries {_REFUSAL_PHRASE!r}; autonomous-grid-app's "
        f"chat_sender.dart substring-matches it and will render the raw sentence instead")

    dart = _sibling_file(app_root(), *_APP_CHAT_SENDER)
    matched = [line for line in dart.read_text().splitlines()
               if not line.lstrip().startswith("//") and _APP_MATCH.search(line)]
    assert matched, (
        f"autonomous-grid-app has no live call matching {_REFUSAL_PHRASE.lower()!r} in "
        f"{'/'.join(_APP_CHAT_SENDER)}. Both halves are outside this repository and no deploy order "
        f"brings the matched phrase back — see ADR 0042 D-l")


# ── the pin's own controls ───────────────────────────────────────────────────────────────────────

def test_an_absent_repository_skips_but_a_moved_file_goes_red():
    """Absent repository ⇒ skip; everything past that ⇒ red. The register's rule for every pin here.

    ⚠️ The two halves are one test on purpose. Written as "an absent sibling skips" alone, the
    natural way to satisfy it is to skip on the missing FILE too — which is how a renamed
    `relay.py` or `handler.py` turns every pin in this file off while reporting that a checkout
    sitting right there is missing. Both halves are reachable only through the helper, because the
    derivation finds all four repositories on the machine this was written on.
    """
    with pytest.raises(pytest.skip.Exception):
        _source(None, "relay.py")

    with pytest.raises(AssertionError, match="renamed or moved"):
        _source(pathlib.Path("/no-such-checkout"), "relay.py")


def test_a_field_named_anywhere_but_a_wire_key_does_not_count_as_spoken():
    """The control for `_wire_keys`, and the difference between this pin and a grep.

    Both halves of this seam name their own field in a docstring, and the relay names it in log
    lines and URLs too. If any of those counted, a payload key could quietly become a third spelling
    — not the retired one, so the negative check misses it as well — while the prose kept this file
    green and grid-apis 422'd every report.
    """
    tree = ast.parse(
        'def f(r):\n'
        '    """mentions balance_credits."""\n'
        '    log("balance_credits went out")\n'
        '    return {"other": r.get("fetched")}\n')

    assert _wire_keys(_named(tree, "f")) == {"other", "fetched"}
