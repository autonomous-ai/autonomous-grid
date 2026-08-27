"""`grid catalog` / `grid pull` / `grid rm`: manage local GGUF model files,
plus the static API-engine whitelist (`grid catalog --api <kind>`)."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # runtime imports stay lazy inside the handlers
    from remote.codex_oauth import CodexBundle
    from remote.codex_probe import CodexModel
    from shared.models import api_catalog


def cmd_catalog(args: argparse.Namespace) -> int:
    # `is not None`, not truthiness: `--api ""` must reach the unknown-kind error,
    # not silently fall through to the GGUF catalog.
    if getattr(args, "api", None) is not None:
        return _catalog_api(args)

    from shared.models import catalog, store

    if getattr(args, "json", False):
        print(json.dumps([
            {
                "hf_repo": entry.hf_repo,
                "file": entry.quantized_file,
                "min_vram_gb": entry.min_vram_gb,
                "kind": entry.kind,
                "target": entry.target,
            }
            for entry in catalog.recommended_entries()
        ], indent=2))
        return 0

    stored = store.list_all()
    if stored:
        print("Local models:")
        for model in stored:
            print(f"  {model.name:<58} {model.size_bytes / 1e9:>7.2f} GB")
        print()
    print("Grid can pull:")
    for entry in catalog.recommended_entries():
        print(catalog.format_catalog_entry(entry))
    print()
    print("Any other GGUF on Hugging Face works too — search huggingface.co, then just pull the")
    print("repository. It grabs Q4_K_M by default, or asks you to pick if there's more than one file:")
    print("  grid pull unsloth/gemma-3-4b-it-GGUF")
    return 0


def _catalog_api(args: argparse.Namespace) -> int:
    from shared.models import api_catalog

    kind = args.api
    whitelist = api_catalog.WHITELISTS.get(kind)
    # "Is this kind known?" is dict membership, and ONLY dict membership — the same question
    # `_reject_api_conflicts` asks, answered the same way. This used to lean on "has entries" as a
    # proxy, which held only while every row had some; a credential-only row (ADR 0015 D-c: codex
    # ships its credential path before issue 05's per-tier model table) made the proxy disagree with
    # the real predicate and print "Unknown API kind 'codex'. Supported: codex, openai".
    if whitelist is None:
        supported = ", ".join(api_catalog.supported_kinds())
        raise SystemExit(f"Unknown API kind {kind!r}. Supported: {supported}")

    # A tier-keyed kind prints per tier — its flat `entries` (the tier union) exists for the
    # kind-generic helpers, not for display, and its `--json` contract speaks `tiers`.
    if kind == api_catalog.CODEX_KIND:
        return _catalog_codex(kind, whitelist, bool(getattr(args, "json", False)))

    if getattr(args, "json", False):
        # Explicit keys: this is the stable machine-readable contract (ADR 0012);
        # renaming an ApiModelEntry field must not silently rename a JSON key.
        print(json.dumps({
            "kind": kind,
            "last_verified": whitelist.last_verified,
            # The endpoint list every API kind serves — now emitted for the generic path too, not
            # only the codex seat (issue 06 AC5), so a script reads `"responses" in endpoints` the
            # same way for any kind. Mirrors the codex JSON's placement in `_catalog_codex`.
            "endpoints": list(whitelist.endpoints),
            "models": [
                {
                    "advertised": api_catalog.advertised_name(kind, entry),
                    "vendor_name": entry.vendor_name,
                    "context_window": entry.context_window,
                    "supports_tools": entry.supports_tools,
                    "supports_vision": entry.supports_vision,
                    "supports_json_mode": entry.supports_json_mode,
                    "supports_structured_outputs": entry.supports_structured_outputs,
                    "notes": entry.notes,
                }
                for entry in api_catalog.entries_for(kind)
            ],
        }, indent=2))
        return 0

    if not api_catalog.entries_for(kind):
        # A known kind with no models listed yet — not an error, and emphatically not "unknown".
        # `grid catalog` answered the question it was asked (ADR 0012 D-a: no credential, no network);
        # the answer happens to be "none". Exit 0, like `--json`'s `models: []`.
        print(f"No models are listed for `{kind}` in this version of grid yet.")
        print(
            f"`grid join --api {kind}` will still sign you in, but there is nothing for it to serve "
            "until a release carries the model list."
        )
        return 0

    print(
        f"Models a `grid join --api {kind}` would serve "
        f"(verified {whitelist.last_verified}):"
    )
    for entry in api_catalog.entries_for(kind):
        print(api_catalog.format_api_entry(kind, entry, whitelist.endpoints))
    print()
    print(f"No key needed to view. Requests to {kind}:* models leave the grid for the vendor.")
    return 0


@dataclass(frozen=True)
class _CodexPlanBlock:
    """The signed-in seat's current-plan slice for `grid catalog --api codex` (issue 10b), with the
    provenance needed to render + mark it. Invariant, guaranteed by construction in
    ``_codex_current_plan``: a fresh probe → ``live=True, fetched_at=None, warning=None``; a cache
    fallback → ``live=False`` with ``fetched_at`` and ``warning`` set; no data at all (offline+no
    cache, rejected+no cache, or no seat) → ``models=None`` + ``warning``. ``models=()`` (NOT ``None``)
    is a real, empty seat. Folding these five into one type keeps the two renderers at three params and
    makes the invariant type-checkable rather than doc-only."""

    plan_type: str | None
    models: tuple[CodexModel, ...] | None
    live: bool
    fetched_at: float | None
    warning: str | None


def _catalog_codex(kind: str, whitelist: api_catalog.ApiWhitelist, as_json: bool) -> int:
    """The seat-aware codex catalog (issue 10b): the current plan's REAL entitlement from a free
    `GET /models` when signed in + online (cached for offline), plus the illustrative cross-plan
    reference of what every plan can serve. No `--live` flag — one automatic path. It NEVER dies on a
    probe failure: offline / a rejected seat / no seat all fall back to the cache then the static
    reference, always with a warning (issue 10b Q2). This is the documented departure from ADR 0012
    D-a's "no network" posture for the codex kind — one free GET, spending nothing.
    """
    from remote import api_keys

    bundle = api_keys.load_codex_bundle()
    block = _codex_current_plan(bundle, whitelist)
    if as_json:
        return _catalog_codex_json(kind, whitelist, block)
    return _catalog_codex_human(kind, whitelist, block)


def _codex_current_plan(
    bundle: CodexBundle | None, whitelist: api_catalog.ApiWhitelist
) -> _CodexPlanBlock:
    """Resolve the current-plan block, NEVER raising (issue 10b Q2). A fresh probe also (re)writes the
    cache, scoped to this seat's account fingerprint; on any probe failure it falls back to that
    seat's cache (if present) then to the static reference, each with a warning.
    """
    from remote import codex_models_cache, codex_probe
    from shared.models import api_catalog

    plan_type = bundle.plan_type if bundle is not None else None
    if bundle is None:
        return _CodexPlanBlock(
            plan_type=None, models=None, live=False, fetched_at=None,
            warning=(
                "Not signed in to a codex seat — showing illustrative data. Run "
                "`grid join --api codex` to sign in and confirm your real entitlement."
            ),
        )

    client_version = api_catalog.CODEX_CLIENT_VERSION
    try:
        live = codex_probe.probe_seat(
            bundle, base_url=whitelist.base_url, client_version=client_version,
        )
    except codex_probe.SeatRejected as rejected:
        warning = (
            f"Your codex seat was rejected (HTTP {rejected.status_code}) — re-run "
            "`grid join --api codex` to sign in again. Showing your last cached set + the "
            "illustrative reference."
        )
    except SystemExit:
        # `probe_seat` raises SystemExit for offline / 429 / 5xx / contract-drift — its documented
        # terminal-error TAXONOMY (every `raise SystemExit` in remote/codex_probe.py is one of those
        # classes). The catalog is read-only and MUST render something (Q2), so we catch that taxonomy,
        # DISCARD the join-oriented message, and fall back with a generic warning — ALWAYS a visible
        # warning, never a silent swallow. NOTE: this catch is by-TYPE; if a future change adds a
        # `raise SystemExit` to probe_seat's call graph for an UNRELATED reason (e.g. a bug-guard), it
        # would be mislabeled "couldn't reach your seat" — re-check this catch against that taxonomy.
        warning = (
            "Couldn't reach your codex seat — showing cached/illustrative data; sign in online to "
            "confirm your entitlement."
        )
    else:
        codex_models_cache.write_cache(
            live, client_version=client_version, account_id=bundle.account_id,
        )
        return _CodexPlanBlock(plan_type=plan_type, models=live, live=True, fetched_at=None, warning=None)

    cached = codex_models_cache.read_cache(
        client_version=client_version, account_id=bundle.account_id,
    )
    if cached is not None:
        return _CodexPlanBlock(
            plan_type=plan_type, models=cached.models, live=False,
            fetched_at=cached.fetched_at, warning=warning,
        )
    return _CodexPlanBlock(
        plan_type=plan_type, models=None, live=False, fetched_at=None, warning=warning,
    )


def _entry_from_codex_model(model: CodexModel) -> api_catalog.ApiModelEntry:
    """Adapt a probe/cache ``CodexModel`` to an ``ApiModelEntry`` so live/cached rows reuse
    ``format_api_entry``. Lives here, not in shared/, which must not import remote/'s ``CodexModel``.
    Codex is a Responses passthrough, so json/structured are always False (the envelope omits them
    elsewhere); an unknown ``context_window`` (0) renders `—`."""
    from shared.models import api_catalog

    return api_catalog.ApiModelEntry(
        vendor_name=model.slug,
        context_window=model.context_window,
        supports_tools=model.supports_tools,
        supports_vision=model.supports_vision,
        supports_json_mode=False,
        supports_structured_outputs=False,
    )


def _codex_plan_heading(plan_type: str | None, suffix: str) -> str:
    label = f" ({plan_type})" if plan_type else ""
    return f"Your current plan{label} — {suffix}:"


def _catalog_codex_human(
    kind: str, whitelist: api_catalog.ApiWhitelist, block: _CodexPlanBlock
) -> int:
    import time

    from shared.models import api_catalog

    if block.warning:
        # Warnings go to STDERR so `grid catalog --api codex 2>/dev/null` yields a clean listing (every
        # sibling warning in this feature — write_cache's note, the probe recovery notice — is already
        # stderr). `--json` carries the warning as a field, so it is unaffected by this split.
        print(f"⚠ {block.warning}\n", file=sys.stderr)

    if block.models is not None:
        if block.live:
            print(_codex_plan_heading(block.plan_type, "live, from your seat"))
        else:
            when = (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(block.fetched_at))
                if block.fetched_at else ""
            )
            print(_codex_plan_heading(block.plan_type, f"cached {when}".rstrip()))
        if block.models:
            for model in block.models:
                print(api_catalog.format_api_entry(
                    kind, _entry_from_codex_model(model), whitelist.endpoints,
                ))
        else:
            print("  (your seat currently serves no models)")
        print()

    print(
        f"All codex plans (illustrative — pricing docs {api_catalog.CODEX_REFERENCE_LAST_VERIFIED}, "
        f"{api_catalog.CODEX_PRICING_DOCS_URL} — verify against a paid seat):"
    )
    _print_codex_reference(kind, whitelist)
    print()
    print(
        f"{kind} models serve the vendor's `responses` endpoint — point an external Codex app at "
        "your grid; `grid chat` cannot call them."
    )
    print(
        f"Requests to {kind}:* models leave the grid for the vendor and spend the seat's own monthly "
        "allowance."
    )
    return 0


def _print_codex_reference(kind: str, whitelist: api_catalog.ApiWhitelist) -> None:
    """The cross-plan reference in delta style: the free set under `free / go`, then each higher plan's
    ADDITIONS over the previous one, then the note that Business/Enterprise/Edu match Plus."""
    from shared.models import api_catalog

    shown: set[str] = set()
    for plan, entries in api_catalog.CODEX_TIER_MODELS.items():
        additions = [entry for entry in entries if entry.vendor_name not in shown]
        suffix = "" if not shown else " (added over the previous plan)"
        print(f"\n{plan}:{suffix}")
        for entry in additions:
            print(api_catalog.format_api_entry(kind, entry, whitelist.endpoints))
        shown.update(entry.vendor_name for entry in entries)
    print("\nBusiness / Enterprise / Edu serve the same set as Plus.")


def _codex_json_row(kind: str, entry: api_catalog.ApiModelEntry, live: bool) -> dict[str, object]:
    """One `--json` row. `context_window` is null when unknown (the `0` sentinel) — uniformly, for
    both the live `current_plan` rows and the illustrative `tiers` rows — never a fabricated 0.

    Deliberately leaner than the pre-10b codex row: `supports_parallel_tool_calls` is dropped (it was
    fully derived — always == supports_tools for codex, remote/probe.codex_features) and so is `notes`
    (illustrative-caveat prose now carried by the row's `live` flag + the envelope's source_url /
    last_verified). Don't reintroduce them: the caps ENVELOPE hand-duplicated with grid-src is a
    separate surface and stays unchanged."""
    from shared.models import api_catalog

    return {
        "advertised": api_catalog.advertised_name(kind, entry),
        "vendor_name": entry.vendor_name,
        "context_window": entry.context_window if entry.context_window > 0 else None,
        "supports_tools": entry.supports_tools,
        "supports_vision": entry.supports_vision,
        "live": live,
    }


def _catalog_codex_json(
    kind: str, whitelist: api_catalog.ApiWhitelist, block: _CodexPlanBlock
) -> int:
    from shared.models import api_catalog

    current_plan = None
    if block.models is not None:
        current_plan = {
            "plan_type": block.plan_type,
            "live": block.live,
            "fetched_at": block.fetched_at,
            "models": [
                _codex_json_row(kind, _entry_from_codex_model(m), block.live) for m in block.models
            ],
        }
    tiers = {
        plan: [_codex_json_row(kind, entry, False) for entry in entries]
        for plan, entries in api_catalog.CODEX_TIER_MODELS.items()
    }
    print(json.dumps({
        "kind": kind,
        "source_url": api_catalog.CODEX_PRICING_DOCS_URL,
        "last_verified": api_catalog.CODEX_REFERENCE_LAST_VERIFIED,
        "endpoints": list(whitelist.endpoints),
        "warning": block.warning,
        "current_plan": current_plan,
        "tiers": tiers,
    }, indent=2))
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    from shared.models import download

    repo, filename = download.parse_spec(args.model)
    if filename is None:
        # A bare repo — the only form that leaves the file unchosen. Show the repo's real file
        # list and let the reader pick; `parse_spec` has already refused the half-way spelling
        # (a partial quant like ':Q8_0') that would otherwise be resolved by guesswork here.
        filename = _pick_file(repo)
    existing = download.local_path(filename)
    if existing.is_file():
        # `download()` itself is safe to call again (it just returns `existing`), but printing
        # "Downloading ..." for a file that never moves a byte would tell the reader something
        # happened that did not.
        print(f"Already have it: {existing}")
        target = existing
    else:
        print(f"Downloading {repo}/{filename} ...")
        target = download.download(repo, filename, on_progress=download.stderr_progress)
        print(f"Saved {target}")
    _pull_projector(repo, target)
    # Only `join` — a downloaded model is not yet on any grid, so `grid models` and `grid chat`
    # would both come back empty. The rest of the path is suggested by `join` itself, once there
    # is something for them to find.
    alias = target.stem
    print(f"\nNext:  grid join <grid-url> --serve {target.name} --advertise-as {alias} --name this-computer")
    return 0


def _pick_file(repo: str) -> str:
    """Show every `.gguf` file in [repo] and let the reader choose, rather than silently
    downloading a guess. No arrow-key menu — that needs a UI dependency this package does not
    carry — a numbered list with `input()` gets the same outcome: see everything, pick one.

    Falls straight to the suggested default with no prompt when there is nothing to choose
    between, or when stdin/stdout are not a real terminal (a script piping `grid pull` must never
    block waiting on a keypress that will never come).
    """
    from shared.models import download

    default, names = download.resolve_file(repo)
    if len(names) == 1 or not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(f"Picked {default!r} ({download.DEFAULT_QUANT})")
        return default

    print(f"{repo} ships {len(names)} files:")
    for i, name in enumerate(names, 1):
        marker = "  <- default" if name == default else ""
        print(f"  {i}. {name}{marker}")
    choice = input(f"Pick a number [1-{len(names)}], or press Enter for the default: ").strip()
    if not choice:
        return default
    if choice.isdigit() and 1 <= int(choice) <= len(names):
        return names[int(choice) - 1]
    print(f"Not a valid choice — using the default {default!r}.")
    return default


def _pull_projector(repo: str, model_path) -> bool:
    """Fetch the repo's multimodal projector, if it has one, so vision needs no second command.
    Returns whether this model can see, so the caller can offer `grid chat --image` to a model
    that will actually answer it.

    Saved as ``<model-stem>.mmproj.gguf`` rather than under its own name: every vision repo calls
    its projector some spelling of ``mmproj-F16.gguf``, so keeping the original name would make two
    vision models in one flat models directory overwrite each other, and would leave nothing tying
    a projector to the weights it belongs to. `grid join --serve` reads this exact name back.
    """
    from shared.models import download, gguf

    projector = download.find_projector(repo)
    if not projector:
        return False
    target = model_path.parent / (model_path.stem + gguf.PROJECTOR_SUFFIX)
    if target.is_file():
        print(f"Vision projector already present: {target.name}")
        return True
    print(f"Supports vision. Downloading {repo}/{projector} ...")
    download.download(repo, projector, out=target, on_progress=download.stderr_progress)
    print(f"Saved {target}")
    return True


def cmd_ctx(args: argparse.Namespace) -> int:
    from pathlib import Path

    from shared.models import gguf, store

    candidate = Path(args.model).expanduser()
    if candidate.is_file():
        path = candidate
    else:
        model = store.find(args.model)
        if not model:
            raise SystemExit(
                f"No such model: {args.model} (not a file, and not under ~/.grid/models/)"
            )
        path = model.path

    ctx = gguf.read_context_length(path)
    if ctx is None:
        raise SystemExit(f"Could not read context length from GGUF metadata: {path}")

    if getattr(args, "json", False):
        print(json.dumps({"file": str(path), "context_length": ctx}))
    else:
        print(ctx)
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    from shared.models import store

    model = store.find(args.model)
    if not model:
        raise SystemExit(f"No such model: {args.model}")
    if not args.yes:
        response = input(f"Delete {model.path} ({model.size_bytes / 1e9:.2f} GB)? [y/N] ").strip().lower()
        if response != "y":
            print("Aborted.")
            return 1
    store.remove(args.model)
    print(f"Removed {model.path}")
    return 0
