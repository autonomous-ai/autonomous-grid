# Grid Protocol — AI MapReduce

Grid routes one request to one engine today. This spec adds two verbs that make it a compute
layer: **map** (fan a job across your grid) and **reduce** (fold the results into one answer).
Same OpenAI endpoint, same LAN, no new runtime.

> `map` fans work across the models and machines you already joined; `reduce` brings it back to
> one answer. `split` (last section) is the separate model-plane verb.

## Model

A request is a **job** = a prompt + a **map** (where it runs) + a **reduce** (how results fold).
The grid resolves the map to N branches, runs them in parallel across joined nodes, and reduces
them to a single OpenAI-shaped response. The reducer is itself a model on the grid — **map and
reduce run on the same fabric**, so they compose and nest.

## Map — target resolution

The `model` field is the program:

| `model` | branches |
|---|---|
| `qwen3-coder` | 1 — today's behavior |
| `qwen3-coder,deepseek-v3,glm-4.6` | 3 — one branch per model (ensemble) |
| `qwen3-coder` + `grid.n: 5` | 5 — samples of one model |

Comma = map. Each branch is scheduled onto whatever node serves that model; spreading one model
across many nodes for throughput is the existing router's job, not a new verb.

## Reduce — fold N → 1

Strategy goes in the `grid.reduce` field:

| strategy | result |
|---|---|
| `synthesize` *(default when branches > 1)* | a reducer model fuses every branch — Mixture-of-Agents |
| `judge` | a judge model picks the single best branch |
| `vote` | majority over normalized branch outputs |
| `first` | the first branch to finish; the rest are cancelled — race |
| `concat` | every branch, labeled |
| `none` | return the array of branches; the client reduces |

`synthesize` and `judge` take an optional `reducer` model and `instruction`.

## Wire — OpenAI-compatible

`POST /v1/chat/completions`. A plain OpenAI client gets ensembling just by passing a comma-model
plus an optional `grid` block (clients that don't read it still work):

```json
{
  "model": "qwen3-coder,deepseek-v3,glm-4.6",
  "messages": [{ "role": "user", "content": "Review this diff for bugs: ..." }],
  "grid": {
    "reduce": "synthesize",
    "reducer": "qwen3-coder",
    "instruction": "Merge into one review; flag where the models disagree",
    "n": 1,
    "max_fanout": 8,
    "timeout_ms": 60000
  }
}
```

The response is a normal chat completion — the reduced answer, which **streams** as the reducer
writes it. A `grid` object is attached with which models ran, per-branch latency, and (when
`reduce: none`) the raw branches. Partial failures reduce over the survivors, down to an optional
`min_quorum`.

## CLI

```
grid merge -m <a,b,c> "<prompt>" [--strategy synthesize|judge|vote|first|concat] [--reducer <model>] [-n <k>]
grid split <model> --across <node,node,...>
```

`grid merge` is map + reduce in one line (default `synthesize`). `grid up` / `join` / `models` /
`chat` are unchanged.

## SDK

A thin client over the wire — the AI-MapReduce DSL:

```python
from grid import prompt

prompt("Review this diff:\n" + diff).map(["qwen3-coder", "deepseek-v3", "glm-4.6"]).merge()
prompt("Spam? yes/no\n" + email).map(["llama3", "qwen3", "gemma3"]).reduce(majority)   # python fn
prompt("Tagline?").map("qwen3", n=5).reduce(judge="pick the best")
prompt(q).map(models).race()
```

Mapping: `.merge()` → `synthesize`; `.reduce(fn)` → `none` + client fold; `.reduce(text, model=)`
→ `synthesize` + instruction; `.race()` → `first`; `.vote()` → `vote`; `.filter(fn)` → `none` +
filter, then reduce.

## split — the model plane

`map`/`reduce` are the **request plane**: many requests across many models. `split` is the
**model plane**: run *one* model too big for any single box across several nodes. Grid does not
shard models itself — it stands up and coordinates a distributed-inference engine (vLLM
tensor/pipeline parallel, Exo, or Petals) across the named nodes and advertises the result as a
single model. That model then behaves like any other, so `split` composes with `map`/`reduce`.

## Scope

- **Request plane — near-term, ships on the router:** `map`, `reduce`/`merge`, `race`, `vote`,
  `filter`, and the SDK. Pure orchestration; no new runtime; "Grid runs no models" still holds.
- **Model plane — moonshot:** `split`, a control plane over a distributed engine. Bigger bet,
  kept cleanly separate so the request plane never depends on it.
- **Non-goals:** Grid still implements no inference and stores no model. It maps, reduces, and
  coordinates — the engines do the thinking.
