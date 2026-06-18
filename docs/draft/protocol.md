# Grid Protocol — AI MapReduce

MapReduce (2004) parallelized **deterministic functions over records in a file**. The records
were lines of text, the operators were code, and you distributed because the *data* was too big —
so you moved compute *to the data*.

Keep the structure (`map → shuffle → reduce`); upgrade two of the three primitives for the AI age:

| | MapReduce, 2004 | AI MapReduce |
|---|---|---|
| **operator** | a deterministic function | an **agent** — model + prompt, reasons, may use tools |
| **record / value** | a line in a file | a doc/task — and every intermediate is an **LLM response** |
| **why distribute** | data too big for one box | the **model (VRAM)** is too big, or you want many agents at once |
| **locality** | move compute → the data | move the task → the node holding the model |
| **map** | `f(record)` in parallel | `agent(item)` in parallel across the grid |
| **shuffle** | group by key | group by a key the **agent emits** (semantic shuffle) |
| **reduce** | combine values per key | a reducer **agent** synthesizes/judges, or a fn votes/counts |
| **failure** | re-run a dead task | re-run **+ verify** — agents fail *semantically*, not just by crashing |
| **output** | a file | one synthesized answer — which can be mapped again |

**The inversion.** Hadoop: data is heavy and immobile → move compute to it. AI age: the **model**
is heavy and immobile (VRAM-resident) → move the work to the model. Grid already routes each call
to the node holding the model — **that is data-locality for the AI age**. `map` and `reduce` are
the two verbs that turn that router into a parallel agent engine over hardware you already own.

## Primitives

- **`map(agent, items)`** — apply one agent to each item of a collection, in parallel; the grid
  schedules each task onto a free model-worker (the node that holds the model). Items are
  documents, rows, files, tasks, or prompts. *Running one prompt across many models — an
  ensemble — is the special case where the collection is the model list.*
- **`shuffle` (optional)** — group map outputs by a **key the agent emits** (e.g. an extracted
  topic or label). Group-by, but the key is semantic.
- **`reduce(reducer, group)`** — fold a group to one: a reducer **agent** (synthesize, judge,
  rank) or a deterministic **fn** (count, vote, sum — the word-count case).

Every stage is `text → agent → text`, so stages nest: a `reduce` output is a valid `map` input.

## Agents are flaky workers

MapReduce assumed deterministic, idempotent tasks and just re-ran stragglers. Agent tasks add
**semantic** failure — wrong, partial, or hallucinated output at variable cost and latency. The
runtime treats map tasks as best-effort and adds:

- per-task **retries + idempotency keys** (re-run a branch without re-running the job),
- **budget caps** — max fan-out, max tokens, wall-clock, per-job spend,
- a `reduce` that can **verify** — `judge`/quorum/validate, not just blind-merge.

That is what makes this a runtime rather than a `for` loop.

## Surface

**Single call — stays OpenAI-compatible.** Fan one prompt across models with a comma-model and an
optional `grid` block (plain OpenAI clients still work); the grid reduces to one completion:

```json
{ "model": "qwen3-coder,deepseek-v3,glm-4.6", "messages": [...],
  "grid": { "reduce": "synthesize", "reducer": "qwen3-coder" } }
```

**Batch job — the new shape.** A dataset map isn't one completion, so it's async:

```
POST /v1/jobs
{ "map":    { "agent": "qwen3-coder", "prompt": "Review {item} for bugs", "over": "<dataset>" },
  "shuffle":{ "by": "severity" },                       // optional, semantic group-by
  "reduce": { "strategy": "synthesize", "reducer": "qwen3-coder" },
  "limits": { "max_fanout": 64, "retries": 2, "budget_tokens": 2_000_000 } }
→ { "job_id": "..." }     # stream progress; collect the reduced result
```

**CLI**
```
grid map -a <agent> --over <items.jsonl> --prompt "..." [--key <field>]
grid reduce --strategy synthesize|judge|vote|count [--reducer <model>]
grid merge -m <a,b,c> "<prompt>"        # ensemble = the single-call special case
grid split <model> --across <nodes>     # model plane (below)
```

**SDK**
```python
from grid import dataset, prompt

# map an agent over a collection, then reduce
dataset(files).map("Review {item} for bugs", agent="qwen3-coder").reduce("Synthesize one report")

# keyed: classify, shuffle by the emitted label, reduce per group
dataset(tickets).map("Label intent of {item}", agent="qwen3").groupby("intent").reduce(count)

# ensemble shortcut (special case)
prompt("Tagline?").map(["qwen3", "deepseek-v3", "glm-4.6"]).merge()
```

## Scope

- **Request plane — near-term, ships on the router:** `map(agent, items)` + `reduce`, the
  `/v1/jobs` async surface, retries/caps/verify, and the ensemble shortcut. Pure orchestration;
  "Grid runs no models" still holds.
- **Advanced:** semantic `shuffle` / keyed `reduce`.
- **Model plane — moonshot:** `split` (one model across nodes via a distributed engine), and
  large-corpus data locality. Kept separate so the request plane never depends on it.
- **Non-goals:** Grid is not storage and runs no inference. It schedules agents over the models
  you joined — the engines do the thinking.
