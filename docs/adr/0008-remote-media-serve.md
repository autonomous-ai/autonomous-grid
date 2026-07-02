# ADR 0008 — Remote provider media serve (ComfyUI over the relay)

Status: accepted (2026-07-02)

## Context

ADR 0004 landed `grid join` in remote mode for **text** engines and deferred media: `--media` was
rejected at the join boundary, the serve loop's endpoint allowlist was text-only, and `handle_job`
dropped any `media/*` job with "isn't available in remote mode yet". ADR 0005 (remote consume) and
`cli/remote_request.py` already ship the **consumer** half — `grid image` / `edit` / `video` POST
`media/*` to `{relay}/relay/v1/...` and consume the SSE via the shared `cli/media_io.py` — and the
relay wire layer (`remote/relay.py`) already streams SSE both ways (`submit_response(stream=True)`).
So the only missing half was the **provider**: bringing ComfyUI up on a remote-joined box and
forwarding relay `media/*` jobs to it. This slice fills that half. It mirrors local media serving
(`cli/provider._run_engine` + `local/server._proxy_media`); the consumer side and relay contract are
unchanged.

Hard invariant (unchanged from 0004): local `grid join` behaviour and the whole existing suite stay
green; no `remote → cli` back-dependency; the relay's poll response stays untrusted input.

## Decisions

1. **ComfyUI bring-up moves to `local/media_engine.py`.** The bring-up (memory-gate the bundles,
   verify files, start ComfyUI + the media server) was inline in `cli/provider._prepare_media_engine`.
   It moves verbatim into `local/media_engine.prepare_media_engine(...)`; `cli/provider` keeps
   `_prepare_media_engine(args)` as a thin adapter (so the local test/monkeypatch surface is
   byte-identical). The serve loop (`remote/serve.py`) calls the shared function directly — it lives in
   `local/` **not** `cli/` precisely so remote reuses it without a `remote → cli` import (ADR 0004 §2);
   the serve loop already imports `local.runtime`.

2. **One serve loop, media brought up alongside text.** `run_remote_engine_from_record` brings up text
   engines only when the record names them (`has_text`), so a media-only join (empty text spec) never
   hits `_bring_up_engines`. When `record["media"]`, it calls `prepare_media_engine`, merges the
   `comfyui:*` models into the identity's `union_models` and their `media` caps
   (`shared.media.media_gating.capability_entry`) into the register envelope, and passes the media
   server's **loopback** URL (`http://127.0.0.1:<media_port>`) to `_ServeState`. Media therefore
   coexists with a text engine under one identity (DECISIONS D9): `grid join --serve <m> --media`
   registers `[<m>, comfyui:image_generation, …]`. Teardown stops the media server we launched and
   only stops ComfyUI if **we** started it (never one the operator was already running).

3. **`media/*` jobs forward to the media server, always streamed.** `handle_job` routes an endpoint on
   the fixed media allowlist (`media/image/generate`, `media/image/edit`, `media/video/i2v`) to
   `state.media_url` via the existing `_forward_stream` — a media response is always SSE, so `is_stream`
   is ignored. The media allowlist is **separate** from `_ALLOWED_ENDPOINTS` (text) because the two
   route to different local URLs; a `media/*` path outside the allowlist is refused (never blind-
   forwarded), preserving the 0004 §6 guarantee that the relay's untrusted `endpoint_path` can't reach
   an arbitrary local path. A job for a media endpoint on a text-only identity (no `media_url`) is
   reported as an error, not crashed.

4. **The record carries media, mirroring local.** `cli/remote_provider._build_record` gains
   `media` / `media_bundles` / `comfyui_port` / `media_port` (same fields, same defaults as the local
   record in `cli/provider._spawn_engine`). `_reject_local_only_flags` no longer rejects `--media`
   (only `--advertise-host` stays local-only — a remote engine polls outbound, so a media server needs
   no advertised address; the loop reaches it on loopback). `_resolve_serve_targets` returns
   `(text_specs, media_detected)`: an explicit `--media` with no text engine is media-only, and a
   detected ComfyUI engine flips `media_detected` so a bare `grid join` picks it up.

## Consequences

- Remote media serving works end-to-end **on the provider side**: `grid join --media --bundle
  image_generation` now registers `comfyui:*` and serves relay `media/*` jobs. Whether the hosted
  relay actually enqueues media jobs and accepts the streamed media response is server-side and must
  be confirmed in a 2-host E2E — the provider now does exactly what the consumer + relay expect.
- The media read budget reuses the job's `inference_timeout_seconds` (per-chunk read timeout); media
  handlers emit progress frequently, so a long video generation won't idle it out. If the hosted relay
  tunes that value only for text, a future slice can carry a media-specific budget.
- `--all` mixing several text engines **and** a detected media engine under one identity falls out of
  the design (text specs + one media server), but the tested paths are media-only and media + a single
  text engine; the broad union is exercised less and may need follow-up polish.
- The detached seam stays the one plug-in point; the classification + flag-gating tests still keep a
  command from running the wrong mode's code.
