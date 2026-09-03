# Allocator resilience qualification

`grid allocator resilience` exercises the production allocator controller, planner, reconciler,
durable command queue, receipts, and single-writer fencing under four conditions:

- changing replica requirements that force warm, drain, and unload operations;
- missing heartbeats from one logical node (a node-side partition);
- a missing relay observation, which must preserve serving state instead of fabricating an empty
  fleet and unloading models;
- controller replacement from durable state, with a higher lease term fencing the old leader.

The nodes and relay observation boundary are modeled. This command does **not** claim to perform
physical inference; use `grid allocator qualify` for real Ollama, ComfyUI, and vLLM lifecycle
qualification. Together, the two tests separate control-plane fault safety from engine correctness.

Run an accelerated three-day qualification during development:

```console
uv run grid allocator resilience --duration 3d
```

Run a real elapsed-time three-day soak in a dedicated terminal or service:

```console
uv run grid allocator resilience --duration 3d --interval 300 --wall-clock \
  --state-dir ~/.grid/allocator-resilience/three-day
```

If the process or machine restarts, repeat the same command with `--resume`. The command validates
that every configuration field matches the checkpoint before continuing. `checkpoint.json` and
`report.json` are written owner-only. A partial checkpoint is never reported as a pass.

Fault frequencies are configurable by cycle. Set a frequency to zero to disable that fault:

```console
uv run grid allocator resilience --duration 24h --node-partition-every 11 \
  --relay-outage-every 19 --controller-failover-every 37
```

This harness intentionally does not mutate firewall rules, stop a production relay, or disconnect a
real provider. Perform physical network fault injection only on a dedicated test deployment with an
approved infrastructure runbook.
