# Physical runtime qualification

Grid does not treat adapter unit tests or an open TCP port as proof that it can manage an engine on
real hardware. `grid --local allocator qualify` executes the same native lifecycle boundary used by
the allocator and writes an owner-only JSON report below
`~/.grid/allocator-qualification/reports/`.

Every successful report proves, in order:

1. Native cached-model inventory and exact artifact identity.
2. Artifact fetch with a digest and size bound when it was not already cached.
3. Warm/load through the engine's native API.
4. Process/model ownership and native readiness.
5. A real generated response or completed ComfyUI workflow.
6. The runtime activity probe used during drain.
7. Native drain/stop or model-memory unload.

Teardown is attempted even after a failed inference. `--cleanup-artifact` removes only an artifact
fetched by that same qualification run; a model already present at the inventory step is never
deleted. Without that flag all artifacts remain cached.

## Commands

Ollama uses its registry digest and does not stop the shared daemon:

```bash
grid --local allocator qualify ollama smollm2:135m \
  --artifact-sha256 <digest> --timeout 300
```

ComfyUI qualification requires an installed image-generation bundle. It submits the real bundled
workflow at 256×256 and one step by default, waits for an output, checks the queue, then calls the
native `/free` memory-unload API:

```bash
grid --local allocator qualify comfyui comfyui:image_generation \
  --endpoint http://127.0.0.1:8188 --timeout 900
```

vLLM qualification pins a full Hugging Face commit. Its `artifact_sha256` is the allocator's
deterministic identity for that exact repository snapshot, not a mutable branch name:

```bash
grid --local allocator qualify vllm Qwen/example \
  --artifact-source hf://Qwen/example@<40-hex-commit> \
  --artifact-sha256 <snapshot-identity> \
  --artifact-size-mb <maximum-download-mb> \
  --tensor-parallel-size 2 --timeout 1800
```

On a shared GPU, bound the canary and shorten its compilation window with
`--gpu-memory-utilization`, `--max-model-len`, and `--enforce-eager`. If the installed vLLM and
FlashInfer wheels have incompatible JIT toolchains, `--disable-flashinfer-sampler` selects vLLM's
native sampler. Grid activates sibling vLLM build tools and a wheel-packaged CUDA compiler when the
host has only an NVIDIA driver; a source-build path still requires Python development headers.

The command downloads into Grid's isolated qualification cache, starts a Grid-owned vLLM child,
proves it through `/v1/models`, runs an OpenAI-compatible chat completion, and reaps the child.

## Forge evidence, 2026-09-02

Machine A physically passed the full Ollama lifecycle with `smollm2:135m`: exact digest inventory,
warm, ownership, readiness, a real `/api/generate` response, activity probe, and unload. The warm
took 0.81 seconds and inference 2.13 seconds after the artifact was cached. The disposable 270 MB
canary was removed after the report; only the pre-existing model remained.

The same run found a model-specific failure for the pre-existing `gpt-oss:20b`: Ollama returned
`tensor "blk.0.ffn_down_exps.weight" size overflow` during warm. Grid correctly withheld readiness
and did not attempt inference or delete the artifact. That is a failed artifact/runtime
qualification, not evidence that the Ollama lifecycle adapter is healthy for that model.

Machine D physically passed vLLM 0.28 with Qwen2.5-Coder-0.5B-Instruct at an immutable commit. The
run used 35% GPU utilization, a 2,048-token context, eager mode, and the native sampler. It fetched
the bounded snapshot, proved process ownership and `/v1/models` readiness, returned `GRID` through
the OpenAI-compatible chat API, observed zero active requests, stopped the child, and cleaned the
canary artifact. The run also established that fresh Ubuntu needs Python development headers for
Triton.

ComfyUI must be run on a host where its canary assets are installed. A report is not transferable
between machines: the engine version, accelerator, driver, and artifacts are part of what the
physical run is testing.
