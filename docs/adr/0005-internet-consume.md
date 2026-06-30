# ADR 0005 — Internet consume path (`grid chat`/`image`/`edit`/`video` + `info --env`)

Status: accepted (2026-06-28)

## Context

ADR 0001 set up modes + dispatch; 0002 sign-in + the per-grid `access_token`; 0003 the internet grid
lifecycle (`grid up/down/ls/info`) and the `signaling_url` relay base; 0004 the provider serve loop
(`grid join`). This slice fills the last everyday surface: **consuming** an internet grid. `grid chat` /
`image` / `edit` / `video` route through the active grid's hosted relay with the per-grid token, and
`grid info --env` prints the relay base URL + token for any OpenAI SDK — the same verbs as LAN, routed
to the relay instead of the local proxy.

This **fulfils ADR 0003 §6's deferral** of the internet `info --env` token-printing form (that ADR, written
before the issues were renumbered, calls it "issue 05"; the use-path is issue 06). The relay consumer
wire contract is ported from the reference client `grid-src/grid_cli/cli.py:cmd_request_*`; the
proprietary backend stays out.

Hard invariant: LAN `chat`/`image`/`edit`/`video` behaviour and the whole existing suite stay green;
no off-LAN calls leak into `shared/`/`lan/`. Tokens are never printed except the one carve-out below.

## Decisions

1. **Separate `cli/internet_request.py`, wired into `dispatch.INTERNET_HANDLERS`.** It mirrors the internet
   handler modules from the prior slices (`cli/internet_grid.py`, `cli/internet_provider.py`) rather than
   making `cli/request.py` mode-aware — the dispatch design already routes by mode to a per-mode
   handler, so each handler stays thin and single-purpose. `chat`/`image`/`edit`/`video` move from the
   internet stub to real handlers; they stay `INTERNET_HANDLERS` *keys*, so the classification/partition test
   is unchanged.

2. **Shared media IO extracted to `cli/media_io.py`.** The SSE consumption and media-file encode/write
   are transport-agnostic and were identical for both modes, so they moved out of `cli/request.py`
   (LAN) into a leaf module imported by both LAN and internet. Pure refactor, behaviour byte-identical;
   the relay/proxy difference is only how the request is built.

3. **The relay wire contract lives in `internet/relay.py`** (one place, beside the provider contract):
   `open_consumer_client(signaling_url, access_token, *, timeout)` reuses the provider `_client`
   (`signaling_url` base + `Authorization: Bearer <access_token>`), and `consumer_headers(...)` builds
   the optional routing headers `X-Target-Provider: <id>` / `X-Allow-Self-Provider: "true"`. The
   per-modality paths (`/relay/v1/chat/completions`, `/relay/v1/media/{image/generate,image/edit,
   video/i2v}`) stay inline in the handlers, mirroring LAN `request.py`. Reusing `_client` means the
   existing `_mock_relay` test seam (which swaps `relay.httpx.Client` for a `MockTransport`) covers the
   consumer — including `.stream()` for media — with no new seam.

4. **`--target-provider` / `--allow-self-provider` are internet-only, transmitted as headers.** They are
   the DECISIONS D16-sanctioned exception to the vocabulary discipline (the only `provider` tokens on
   the surface); declared once on the unified `chat`/`image`/`edit`/`video` parser and **rejected in
   LAN mode** via a local guard that mirrors `cli/provider.py:_reject_internet_only_flags`. The boolean
   `--allow-self-provider` (`store_true`, default `False`) needs a truthiness check, not the `is not
   None` test the value-flags use. Routing is via headers (port-source contract), never the body.

5. **The relay address is resolved live, not from the bundle.** Sign-in persists the per-grid
   `access_token` but **not** the `signaling_url` (`cmd_login` stores `/tokens` verbatim;
   `auth._validated` requires only `network_id`+`name`). So each consume command — and `info --env` —
   takes `access_token` from the bundle and reads `signaling_url` from
   `control_plane.get_managed_network_status`, exactly as `grid join`/`up`/`info` already do, and emits
   a clean "run `grid up`" if the grid isn't running. A pure consumer (`login → use → chat`, never
   `grid up`) would otherwise have no address. This is account-level (member-readable, like `grid
   join`'s status read); it is the only deviation from the reference client, which read the address from
   a self-host bundle field that the hosted model no longer provides.

6. **Chat mirrors LAN exactly; "streaming honoured" is about the relay, not token-by-token output.**
   `cmd_internet_chat` is a single non-streaming POST that prints `choices[0].message.content` (raw
   fallback; `--json` prints the raw body) — identical to LAN `cmd_chat`. Neither mode streams chat
   tokens to the terminal; media *does* stream SSE (progress → result), and the relay relays a
   provider's stream end-to-end. The acceptance criterion's "streaming honoured" means that relay
   streaming isn't broken, not that `grid chat` renders tokens incrementally.

7. **A 401 on this one-shot path is a clear error, not an auto-refresh.** The handler checks 401
   *before* the `--json`/`≥400` branch and prints "Your access token has expired. Run `grid login`…"
   to stderr (exit 1), never echoing the token. Refresh-on-401 stays scoped to the long-running serve
   loop (ADR 0004 §4); a one-shot command refreshing + persisting tokens is unwarranted coupling
   (KISS/YAGNI). Resolution gates in order: signed in (`require_session`) → a grid resolves
   (`internet_grid._select`) → it has an access token → it is up.

8. **`info --env` is the one deliberate token-printing carve-out (ADR 0003 §6).** It prints
   `OPENAI_BASE_URL="{signaling_url}/relay/v1"` + `OPENAI_API_KEY="{access_token}"` — an explicit,
   user-requested disclosure of the caller's own token to their own shell, like `gh auth token` /
   `gcloud auth print-access-token`. Every other path (`ls`, `info` without `--env`, all `--json`,
   error messages) stays token-free, and the consume client closes both the HTTP client and the
   streamed response (a double-context) so no connection leaks.

## Consequences

- LAN is untouched: the media-IO extraction is a pure refactor, the internet-only flags are rejected in
  LAN, and no new code runs for LAN users. The existing suite stays green (the obsolete
  `info --env`-deferred placeholder test is replaced by real `info --env` coverage).
- The relay layer is the one place the consumer wire contract lives; tests mock it via
  `httpx.MockTransport`, adjustable if the hosted relay's consumer API diverges.
- Every consume command and `info --env` makes one control-plane status call to learn the live relay
  address — the documented cost of not persisting `signaling_url` at sign-in, and consistent with how
  `grid join`/`up`/`info` already behave. A grid created/started elsewhere works without re-login.
- Tokens live only in `credentials.toml`; the consume path reads, never writes them, and prints one
  only through `info --env`. A future multi-engine / streaming-chat enhancement extends the same
  handlers without touching the wire boundary.
