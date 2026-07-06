# ADR 0010 — CLI vocabulary & UX for `join` / `leave` / `engine`

Status: accepted (2026-07-06). Scope: the public `grid` CLI surface (v0.1.6 shipped). All changes are
additive / alias-preserving except Decision D-f (`grid up` no longer auto-creates an id-shaped arg).

## Context

The released CLI accumulated avoidable friction, each verified against the tree on `feat/cli-ux`:

- **`--engine` is overloaded.** On `grid join` it *filters auto-detected engines by kind*
  (`cli/provider.py:82-85`, `cli/remote_provider.py:367-370`); on `grid leave` it *names the joined
  instance to drop* (`cli/provider.py:231`). Two meanings, one flag — the reader can't tell "type" from
  "instance".
- **Model aliasing needs two positionally-paired flags.** `-m real --advertise-as pub` must be given in
  matching order and equal count (`_advertised_text_models`, `cli/provider.py:525-539`). Easy to
  mis-pair; nothing signals the pairing at the call site.
- **`grid engines` sits one keystroke from `grid engine`.** `engines` (plural) lists live joined engines
  (`cli/parser.py:175`); `engine` (singular) is the built-in-engine setup namespace
  (`install`/`pull`/`status`/`start`/`stop`, `cli/parser.py:359-398`). Typo-adjacent, and inconsistent
  with the rest of the surface.
- **`leave --engine` help lies.** It says "endpoint URL (or unique label)" (`cli/parser.py:163-165`) but
  the remote matcher already accepts URL / label / served-model / URL-fragment (`_drop_spec`,
  `cli/remote_provider.py:569-596`); the *local* handler accepts only the exact engine-id.
- **`grid up <unknown>` silently creates.** Any unknown positional becomes a brand-new grid named after
  the string (`cli/grid.py:23`). Paste a grid *id* you haven't synced locally and you get a junk grid
  named after the id, not an error.

Design constraints: the CLI is mode-aware (local/remote) and dispatch is classified **per top-level
command only** (`cli/dispatch.py`; a test enforces every command is classified). Grid ids are
`ag-<slug>-<hex8>` locally (`local/runtime.py:65`) and opaque `[A-Za-z0-9_-]+` remotely
(`cli/remote_grid.py:32`). There is no `DECISIONS.md`; design is recorded in these ADRs.

## Decisions

**D-a (join `--kind`).** The pre-join engine-*type* filter is `--kind` (e.g. `--kind ollama`).
`--engine` is retained as a compatibility alias on the same dest (two option strings, one `dest="kind"`
— the repo precedent is `--endpoint-port`/`--llama-port`, `cli/parser.py:135`). Vocabulary is now
consistent: **kind** = the type you filter on *before* joining; **engine** = the instance you name
*after* joining (and `leave --engine`). Consequence: `grid join --engine ollama` keeps working; help and
error text move to `--kind`.

**D-b (inline alias `-m real=pub`).** Model aliasing gains an inline spelling that desugars into today's
flat `models` / `advertise_as` lists before any handler reads them (`_apply_inline_aliases`). It is
**minimal**: same all-or-nothing rule as `--advertise-as` (alias every text model, or none), mutually
exclusive with `--advertise-as`, and rejected on `--serve` (which bypasses the models list — alias a
built-in via `--advertise-as`). Split on the first `=`. Consequence: **zero** change to
`_advertised_text_models`, `_reject_unserveable_union`, the run record, or the SIGHUP reload path;
`--advertise-as` remains as the escape hatch for exotic model names.

**D-c (deprecate `--engine-label`).** The grid page derives an engine's kind automatically
(`remote/serve.py` `_meta`), so `--engine-label` no longer changes any display. It is deprecated:
accepted (no hard error), marked deprecated in help, warns on stderr when used (mirroring the
`--pricing-*` deprecation, `cli/remote_provider.py:53-58`), and is still stored in the record so
`grid leave --engine <label>` keeps matching an engine a prior join labelled.

**D-d (local fuzzy `leave --engine`).** Local `leave --engine` gains the same match order as remote —
exact engine-id → endpoint URL → served model → URL fragment — via one shared, pure matcher
`shared.run_records.match_engine`, which the remote `_drop_spec` also delegates to (byte-identical
remote messages). The exact engine-id (== local `--name`) short-circuits first, before fuzzy matching.
The ambiguity guidance is parameterized per mode: remote says "pass the exact endpoint URL instead",
local says "pass the exact engine id instead" (a local built-in `--serve` engine has no URL, so the
always-unique engine-id is the actionable disambiguator).

**D-e (`grid engine ls`).** Live-engine listing gains a canonical `grid engine ls` (+ `list` alias);
`grid engines` is kept as a legacy alias. The handler branches on mode internally (the `cmd_overview`
pattern) so `engine` stays AGNOSTIC and needs no dispatch change. The `grid engine` group help is
reworded from "Set up the built-in engines" to "Set up built-in engines and list live ones", so the
namespace coherently covers setup **and** listing. Chosen over a new top-level `grid ps` (which would
cost a `dispatch.py` bucket + a `REMOTE_HANDLERS` entry + a third spelling for "list engines").

**D-f (`grid up` id guard).** `grid up <arg>` no longer auto-creates when `<arg>` matches the local
grid-id shape `ag-<slug>-<hex8>` (anchored `fullmatch`) but isn't found locally; it exits with guidance
to `grid ls` (and, if the grid is remote, `grid mode remote` then `grid sync`). An ordinary name still
creates as before. This is the one non-additive change. Accepted trade-off: a chosen grid *name* shaped
exactly like an id (`ag-…-<8 hex>`) is refused — acceptable, since that collides with the reserved id
form. No remote mirror: remote `network_id`s are opaque and indistinguishable from a chosen name, so any
heuristic would false-positive; remote keeps create-on-unknown-name (the correct lever there would be an
explicit `--create`, out of scope).

**D-g (`leave --all` stays required for multi-engine).** Local bare `grid leave` on a grid with several
engines keeps its safety guard — it errors asking for `--engine`/`--all` (`cli/provider.py:240`) rather
than silently tearing down everything. (A one-engine grid still leaves that engine on a bare `leave`;
remote tears the single identity down.) Only the help wording changes, to state this accurately —
correcting the handoff's assumption that bare-leave already meant "all".

## Consequences

- Back-compat: `grid join --engine ollama`, `grid engines`, and `-m x --advertise-as y` all keep working.
- No new top-level command → the dispatch classification test is untouched by every decision here.
- The only behavior a v0.1.6 user could notice as *removed* is `grid up <an-id-you-didn't-sync>`
  auto-creating a junk grid — now a clean error (D-f).
