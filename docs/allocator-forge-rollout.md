# Forge allocator rollout and model cutover

This runbook rolls the stacked allocator work onto Forge after its prerequisite pull requests merge.
It is intentionally staged: no command infers permission to stop a manually operated engine.

## Preconditions

1. Deploy the relay/controller before providers. Keep the controller in `recommend` mode.
2. Update Machines A, C, and D to the same Grid release and re-run their existing provider joins.
3. Run `grid --remote allocator join forge --dedicated` on each allocator-managed provider.
4. Confirm every host heartbeat, runtime, capacity, disk figure, GPU count, and existing residency in
   `grid --local allocator status --grid allocator-control --json`.
5. Run `grid --local allocator audit --grid allocator-control`. Any row marked `EXTERNAL` remains
   routable inventory, but the allocator will not stop or replace it.

Do not enable `automatic` while a required host is absent or reports incorrect capacity.

## Qualify each physical runtime

Run the lifecycle qualifier locally on the host that owns the engine. Use a disposable, pinned
artifact where possible and retain the generated report.

```console
grid allocator qualify ollama <small-model> --cleanup-artifact
grid allocator qualify vllm <pinned-hugging-face-model> \
  --artifact-source hf://<repo>@<full-commit> --artifact-sha256 <snapshot-sha256>
grid allocator qualify comfyui comfyui:image_generation --endpoint http://127.0.0.1:8188
```

Only a successful report qualifies that runtime on that physical host. Qualification never removes
an artifact that existed before the run.

## Introduce a replacement model on Machine C

Use `grid allocator scout run` and benchmark the proposed coding model first. The selected proposal
must identify an immutable upstream revision and a measured memory footprint that fits Machine C.
Do not encode a marketing model name or a mutable `main` revision directly into the rollout.

1. Add the replacement as a new profile with `min_replicas=0`, `max_replicas=1`, runtime `vllm`,
   backend `cuda`, Machine C's required tag, and the exact artifact identity.
2. Keep the allocator in `recommend`; inspect the plan and ensure the old external Qwen route is not
   presented as allocator-owned.
3. Benchmark/qualify the replacement. If both models cannot coexist in VRAM, schedule a maintenance
   window: drain the old provider route, stop it with the operator's original service manager, then
   let the allocator warm the replacement. The allocator must never kill an unowned PID.
4. Send real coding requests through Grid and verify response correctness, latency, and errors.
5. Run the cutover gate:

```console
grid --local allocator audit --grid allocator-control \
  --require-managed <replacement-model> \
  --forbid-external <legacy-model>
```

The gate passes only when at least one allocator-owned replacement route is ready, no external
route with that replacement identity remains, and the legacy external route is absent. Always use
the two gates together so an empty/offline fleet cannot look like a completed cutover. Keep the old
artifact until the observation window and rollback deadline have passed.

## Bring Machine A's Ollama model under allocation

First resolve the existing `gpt-oss:20b` load failure reported by physical qualification; do not
delete its pre-existing artifact. Once the model can complete a native Ollama inference, create an
exact-digest Ollama profile with `min_replicas=0`, validate in `recommend`, and allow the managed
Ollama adapter to warm/unload residency through the shared daemon. The daemon itself remains
operator-owned; model residency becomes allocator-owned.

## Enable and prove automatic mode

```console
grid --local allocator mode automatic --grid allocator-control
grid --local allocator tick --grid allocator-control
grid --local allocator status --grid allocator-control
grid --local allocator audit --grid allocator-control \
  --require-managed <replacement-model>
```

Then run a dedicated physical fault window. Pause one provider at a time, verify make-before-break
where spare capacity exists, restore it, and wait for convergence before the next fault. A relay or
controller interruption must use a test deployment or an approved maintenance window; the
development resilience harness deliberately does not stop the live Forge relay.

Rollback is `allocator mode recommend` first. Restore the prior external service only with its
original service manager, verify it is ready in Grid, and then retire the replacement profile.
