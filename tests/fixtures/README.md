# `codex_stream.sse` — the canned codex Responses stream (shared wire contract)

A vendor SSE stream as the codex backend actually emits it: 15,715 bytes, 47 event blocks
(`sequence_number` 0–46), LF-only framing (zero CR bytes), each block `event:` + `data:`
terminated by a blank line, no `data: [DONE]`.

## This file is a cross-repo wire contract, shared BY COPY

It pins the event-block framing convention between the provider (this repo, `remote/serve.py`
`_iter_event_blocks`, issue 06) and the relay (grid-src, issue 03). Per `CLAUDE.local.md` the two
repos share no code — a change here must be made on both sides in lockstep, or the seam breaks
silently.

The source of truth for provenance (how the capture was taken, which parts are verbatim vs
derived, and why the derived middle is not guesswork) is the grid-src copy's README. Verify
lockstep with `cmp` against the **real source path** (the two paths differ between repos):

```
/Users/macbookpro/Projects/grid-src-feats/feat-codex-subs/grid_cli/private_server/tests/fixtures/codex_stream.sse
```

(grid-src repo, branch `feat/codex-subs`, landed with issue 03, commit `50d4f29`.)

Do not regenerate casually — the capture spent a live free-tier seat's allowance and cannot be
re-taken for free.
