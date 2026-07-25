"""MLX rollout engine: serve the *training* rollout contract from a Mac.

    python -m train.mlx.rollout_server --model mlx-community/SmolLM2-135M-Instruct --port 8080

This is the piece that removes the vLLM dependency for Apple-Silicon fleets. It serves an
OpenAI-style `POST /v1/completions` that honours the two fields RL training needs and chat
servers don't provide: `return_tokens_as_token_ids` (tokens come back as "token_id:<int>" —
the ids the engine actually sampled) and `logprobs` (the sampling-time logprob of each token —
the behavior policy in the loss). The response shape is exactly what `train/rollout.py`
parses, so a trainer pointed at this server or at vLLM sees no difference.

Also serves `GET /v1/models` (so `grid join` detects it — MLX probe, port 8080) and
`POST /reload_adapter` (hot-reload LoRA weights from a directory `mlx_lm` saved — same-format
adapters only; a torch/peft adapter does not load here).

Temperature contract: `temperature: 0` means greedy (argmax) — matching vLLM — which is how
a remote trainer runs its held-out eval.
"""
from __future__ import annotations

import argparse
import threading


def _import_mlx():
    try:
        import mlx.core as mx
        from mlx_lm import load
        from mlx_lm.models.cache import make_prompt_cache
    except ImportError as exc:
        raise SystemExit(
            f"rollout_server: missing dependency ({exc.name}) — needs an Apple Silicon Mac "
            "with `pip install mlx-lm`."
        ) from exc
    return mx, load, make_prompt_cache


class MlxEngine:
    """The MLX-backed sampler. One lock: MLX evaluation is not reentrant across threads."""

    def __init__(self, model_id: str, adapter_path: str | None = None) -> None:
        mx, load, make_prompt_cache = _import_mlx()
        self._mx = mx
        self._make_cache = make_prompt_cache
        self.model_id = model_id
        self.model, self.tokenizer = load(model_id, adapter_path=adapter_path)
        self._adapter_applied = adapter_path is not None
        self._lock = threading.Lock()

    def generate(self, prompt: str, n: int, max_tokens: int, temperature: float):
        """n samples -> [(token_ids, logprobs, text)]; temperature 0 = greedy."""
        mx = self._mx
        eos_ids = set(self.tokenizer.eos_token_ids)
        prompt_ids = self.tokenizer.encode(prompt)
        results = []
        with self._lock:
            for _ in range(n):
                cache = self._make_cache(self.model)
                logits = self.model(mx.array(prompt_ids)[None], cache=cache)[:, -1, :]
                ids: list[int] = []
                lps: list[float] = []
                for _ in range(max_tokens):
                    logp = logits - mx.logsumexp(logits, axis=-1, keepdims=True)
                    if temperature <= 0:
                        token = mx.argmax(logp, axis=-1)
                    else:
                        token = mx.random.categorical(logits / temperature)
                    token_id = token.item()
                    ids.append(token_id)
                    lps.append(logp[0, token_id].item())
                    if token_id in eos_ids:
                        break
                    logits = self.model(token[None], cache=cache)[:, -1, :]
                results.append((ids, lps, self.tokenizer.decode(ids, skip_special_tokens=True)))
        return results

    def reload_adapter(self, adapter_dir: str) -> str:
        """Apply (first call) or refresh (subsequent calls) LoRA adapter weights in place."""
        from pathlib import Path

        weights = Path(adapter_dir).expanduser() / "adapters.safetensors"
        if not weights.is_file():
            raise FileNotFoundError(f"no adapters.safetensors in {adapter_dir}")
        with self._lock:
            if not self._adapter_applied:
                from mlx_lm.tuner.utils import load_adapters

                load_adapters(self.model, adapter_dir)
                self._adapter_applied = True
            else:
                self.model.load_weights(str(weights), strict=False)
        return "applied"


def build_app(engine):
    """FastAPI app over any engine exposing generate()/reload_adapter()/model_id/tokenizer.

    Split from MlxEngine so the contract (request validation + response shaping) is testable
    on machines without mlx — including the round-trip test against GridRolloutClient.
    """
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import JSONResponse

    app = FastAPI(title="grid mlx rollout engine", docs_url=None, redoc_url=None)

    @app.get("/v1/models")
    def models() -> JSONResponse:
        return JSONResponse({"object": "list", "data": [{"id": engine.model_id, "object": "model"}]})

    @app.post("/v1/completions")
    def completions(body: dict) -> JSONResponse:
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            raise HTTPException(400, "prompt (string) is required")
        if not body.get("logprobs") or not body.get("return_tokens_as_token_ids"):
            # Fail loud, not degraded: a trainer that forgot the flags must find out now.
            raise HTTPException(
                400,
                "this engine serves TRAINING rollouts: set logprobs and "
                "return_tokens_as_token_ids in the request",
            )
        n = int(body.get("n", 1))
        max_tokens = int(body.get("max_tokens", 256))
        temperature = float(body.get("temperature", 1.0))
        if not (1 <= n <= 64):
            raise HTTPException(400, "n must be 1..64")
        choices = [
            {
                "index": i,
                "text": text,
                "finish_reason": "stop",
                "logprobs": {
                    "tokens": [f"token_id:{t}" for t in ids],
                    "token_logprobs": lps,
                },
            }
            for i, (ids, lps, text) in enumerate(
                engine.generate(prompt, n, max_tokens, temperature)
            )
        ]
        return JSONResponse({"object": "text_completion", "model": engine.model_id, "choices": choices})

    @app.post("/reload_adapter")
    def reload_adapter(body: dict) -> JSONResponse:
        adapter_dir = body.get("adapter_dir")
        if not adapter_dir:
            raise HTTPException(400, "adapter_dir is required")
        try:
            status = engine.reload_adapter(adapter_dir)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        return JSONResponse({"status": status})

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="MLX rollout engine (training contract)")
    parser.add_argument("--model", default="mlx-community/SmolLM2-135M-Instruct")
    parser.add_argument("--adapter-path", default=None, help="LoRA adapter dir (mlx_lm format)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    import uvicorn

    print(f"Loading {args.model} …")
    engine = MlxEngine(args.model, adapter_path=args.adapter_path)
    print(f"rollout engine -> http://{args.host}:{args.port}/v1  (model: {args.model})")
    uvicorn.run(build_app(engine), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
