# Grid topologies

**Topology** = the shape of a grid: which kinds of node it has, and what each one is allowed to
do. Grid has two node kinds, so there are three useful shapes — and the shape decides what
serving a request costs and where it happens.

**Scope: this is a document about inference.** Training is not a topology choice — it runs on the
local nodes, in every shape. Renting compute by the hour to train is the expensive way to do the
one job idle office hardware is ideally suited to, and API nodes cannot train at any price (see
below). So: *route inference however the economics favour; train locally, always.*

| Node kind | What it is | Joined with |
|---|---|---|
| **local node** | a machine you own running an inference engine (llama.cpp, Ollama, vLLM, MLX, ComfyUI) serving models you host | `grid join` (auto-detected) |
| **API node** | a credential of yours fronting a frontier vendor (GPT, Codex seat), serving that vendor's models to the grid | `grid join --api openai` / `--api codex` |

## The three topologies

### Pure local — everything on machines you own

Every node is a local engine. Nothing leaves the network: not a prompt, not a document, not a
gradient. This is the only topology where **training** is fully available, and it is the one the
privacy story is about.

- Inference: yes, on your models.
- Training: yes — rollouts, trainer, judges, evals, all local.
- Cost: electricity and the hardware you already bought.
- Ceiling: your models. A 4–8B model that has been trained on your task is often past frontier
  quality *on that task*, and nowhere near it in general.

### Pure API — a private gateway in front of frontier models

Every node is an API node. Grid is a gateway here: one endpoint, your keys, your members, spend
visible in one place, no vendor lock-in in your app code.

- Inference: yes, frontier quality.
- **Training: no. Not "slow" — impossible.** Two reasons, in order of finality: the vendors
  return neither the token ids they sampled nor their logprobs (Anthropic returns no logprobs at
  all), so there is no behavior policy to compute a gradient against; and there are no weights to
  update — you cannot own an improvement to a model you rent. Grid enforces the first at the
  endpoint level: API kinds serve `chat/completions` and `responses`, never `completions`.
- Cost: per token, forever, rising with use.

### Hybrid — local for the many, frontier for the few

Both kinds in one grid. `model: "auto"` classifies each request and routes it: simple work to the
smallest adequate local model, demanding work to a frontier API node. This is the practical
shape for most businesses, and the one the roadmap is built around.

- Inference: routed by difficulty. Most volume lands local; the hard tail goes out.
- Training: runs on the **local half** — API nodes cannot serve rollouts (above). They can serve
  as *teachers* (frontier answers become SFT data for your local model) or as *judges* — but both
  send your data to a vendor, so treat that as an explicit egress decision, not a default. The
  privacy-preserving version keeps the judge on a local node.
- Cost: falls as the local half gets better.

## Why hybrid is the interesting one: the ratio is a dial, and training turns it

In a hybrid grid, `auto` sends a task to a frontier node because the local model can't do it
*yet*. Every one of those requests is evidence of a gap — and a gap with a task attached is
exactly what a training run consumes.

So the loop is: run the grid → note which tasks went out → train the local model on those tasks
overnight → `auto` starts keeping them local → repeat. The frontier share shrinks toward the work
that genuinely needs frontier reasoning, and it shrinks **because you used the grid**, not
because anyone tuned a router by hand.

That is the same play Microsoft described in July 2026 for MAI ("route traffic to the small model
wherever it matches frontier quality; frontier only for true frontier needs") — with one
difference that matters: their loop runs on their compute, on their models. This one runs on
yours.

## Choosing

- Have machines and sensitive data → **pure local**. Cheapest to serve, and trains.
- Want one governed endpoint over vendor keys, no hardware → **pure API**. Nothing to train on
  until at least one local node joins.
- Have machines *and* real hard problems → **hybrid**: serve from both, train on the local half,
  and let training move the line.

In all three, the training answer is the same — **local**. The topology decides your inference
bill; it does not decide where learning happens.

Training details: [two-node-training.md](two-node-training.md) ·
[ADR 0019](adr/0019-rl-training-plane.md). Routing details:
[ADR 0013](adr/0013-auto-routing.md) (`auto`), [ADR 0012](adr/0012-api-engines.md) (API nodes).
