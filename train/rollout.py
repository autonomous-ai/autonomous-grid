"""Rollout generation over a grid: the bridge between TRL's trainer and Grid's inference plane.

The contract (ADR 0019): the trainer never generates text itself. Each GRPO step asks the grid
for completions through the ordinary OpenAI-compatible `/completions` endpoint, with two
requirements a stock chat client doesn't have — the engine's *own* sampled token ids and their
logprobs must come back with the text, because they are the behavior-policy term of the RL loss
(re-tokenizing client-side can silently disagree with what the engine sampled). vLLM serves both
via `logprobs` + `return_tokens_as_token_ids`; `probe_endpoint` verifies a node actually honours
them and fails closed, mirroring the ADR 0018 capability-probe pattern.

Everything here runs on the core CLI deps (httpx + stdlib) so it is unit-testable without torch.
"""
from __future__ import annotations

import dataclasses
import os

import httpx

from .config import RolloutConfig

# vLLM's `return_tokens_as_token_ids` marks each logprob token as "token_id:<int>" so the ids
# survive the OpenAI response shape, which only has string fields for tokens.
_TOKEN_ID_PREFIX = "token_id:"


class RolloutError(SystemExit):
    def __init__(self, message: str) -> None:
        super().__init__(f"grid train: {message}")


@dataclasses.dataclass(frozen=True)
class Rollout:
    """One completion as the engine sampled it."""

    text: str
    token_ids: tuple[int, ...]
    logprobs: tuple[float, ...]


def _parse_token(token: str) -> int:
    if not token.startswith(_TOKEN_ID_PREFIX):
        raise RolloutError(
            "rollout engine returned plain-text tokens, not token ids — it ignored "
            "`return_tokens_as_token_ids`. Serve rollouts from a vLLM engine "
            "(see `grid train doctor`)."
        )
    try:
        return int(token[len(_TOKEN_ID_PREFIX):])
    except ValueError as exc:
        raise RolloutError(f"malformed token id from rollout engine: {token!r}") from exc


def parse_completion_choice(choice: dict) -> Rollout:
    """One OpenAI `choices[i]` -> Rollout, insisting on the ids+logprobs the loss needs."""
    lp = choice.get("logprobs") or {}
    tokens = lp.get("tokens")
    token_logprobs = lp.get("token_logprobs")
    if not tokens or token_logprobs is None:
        raise RolloutError(
            "rollout engine returned no logprobs — the RL loss needs the engine's sampled "
            "logprobs. Enable logprobs on the serving engine (vLLM does this natively)."
        )
    if len(tokens) != len(token_logprobs):
        raise RolloutError("rollout engine returned misaligned tokens/logprobs")
    return Rollout(
        text=choice.get("text", ""),
        token_ids=tuple(_parse_token(t) for t in tokens),
        logprobs=tuple(float(x) for x in token_logprobs),
    )


class GridRolloutClient:
    """Blocking client for grouped completions against one grid endpoint.

    Grouping matters twice: one request with `n=group_size` lets the engine reuse the prompt
    prefix across the whole GRPO group, and it keeps the group on one engine — the same
    prefix-locality reasoning as prime-rl's group pinning, but enforced by request shape
    instead of a scheduler.
    """

    def __init__(self, cfg: RolloutConfig, model: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self._cfg = cfg
        self._model = model
        headers = {}
        key = os.environ.get(cfg.api_key_env, "") if cfg.api_key_env else ""
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if cfg.target_provider:
            # Day-one training-pool pinning: route every rollout to one named engine.
            headers["X-Target-Provider"] = cfg.target_provider
        self._client = httpx.Client(
            base_url=cfg.base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(cfg.timeout_seconds, connect=10.0),
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def _post_completions(self, body: dict) -> dict:
        try:
            response = self._client.post("/completions", json=body)
        except httpx.TransportError:
            # One retry: a LAN grid's transient node hiccup shouldn't kill a training step.
            try:
                response = self._client.post("/completions", json=body)
            except httpx.TransportError as exc:
                raise RolloutError(f"rollout endpoint unreachable: {exc}") from exc
        if response.status_code != 200:
            detail = response.text[:300]
            raise RolloutError(f"rollout request failed: HTTP {response.status_code}: {detail}")
        return response.json()

    def generate_group(self, prompt: str, n: int, temperature: float | None = None) -> list[Rollout]:
        body = {
            "model": self._model,
            "prompt": prompt,
            "n": n,
            "max_tokens": self._cfg.max_tokens,
            # temperature 0 = greedy on both vLLM and the MLX rollout server — the remote
            # trainer's held-out eval path.
            "temperature": self._cfg.temperature if temperature is None else temperature,
            "logprobs": 1,
            "return_tokens_as_token_ids": True,
        }
        data = self._post_completions(body)
        choices = data.get("choices") or []
        if len(choices) != n:
            raise RolloutError(f"rollout engine returned {len(choices)} choices, expected {n}")
        # OpenAI choices carry an `index`; keep group order deterministic regardless of arrival.
        ordered = sorted(choices, key=lambda c: c.get("index", 0))
        return [parse_completion_choice(c) for c in ordered]


def probe_endpoint(cfg: RolloutConfig, model: str, *, transport: httpx.BaseTransport | None = None) -> dict:
    """Can this endpoint serve *training* rollouts? Fail closed, report precisely.

    Returns {"ok": bool, "detail": str, "model": str}. A truthful "no" here is the difference
    between a config error at minute one and silently untrainable rollouts at step one
    (a chat-only engine yields zero trainable tokens — the prime-rl teardown's core finding).
    """
    client = GridRolloutClient(cfg, model, transport=transport)
    try:
        rollouts = client.generate_group("grid train probe: reply with one word.", 1)
    except SystemExit as exc:
        return {"ok": False, "detail": str(exc), "model": model}
    finally:
        client.close()
    rollout = rollouts[0]
    if not rollout.token_ids:
        return {"ok": False, "detail": "engine returned an empty completion", "model": model}
    return {
        "ok": True,
        "detail": f"token ids + logprobs verified ({len(rollout.token_ids)} tokens sampled)",
        "model": model,
    }


def build_rollout_func(cfg: RolloutConfig, model: str, tokenizer):
    """TRL `rollout_func` adapter: prompts in, {prompt_ids, completion_ids, logprobs} out.

    TRL repeats each prompt `num_generations` times in the list it passes; identical adjacent
    prompts are coalesced into one `n=k` request so the engine sees the group whole. The
    returned `completion_ids`/`logprobs` are exactly what the engine sampled; `prompt_ids` are
    tokenized here with the trainer's tokenizer (same model family as the engine — enforced by
    config, checked by doctor).

    Keyword-tolerant signature: the hook is experimental in TRL and has grown arguments across
    releases; we accept and ignore extras rather than pin to one call shape.
    """

    client = GridRolloutClient(cfg, model)

    def rollout_func(prompts: list[str], *args, **kwargs) -> dict:
        groups: list[tuple[str, int]] = []
        for prompt in prompts:
            if groups and groups[-1][0] == prompt:
                groups[-1] = (prompt, groups[-1][1] + 1)
            else:
                groups.append((prompt, 1))

        prompt_ids: list[list[int]] = []
        completion_ids: list[list[int]] = []
        logprobs: list[list[float]] = []
        completions_text: list[str] = []
        for prompt, count in groups:
            ids = tokenizer(prompt, add_special_tokens=False)["input_ids"]
            for rollout in client.generate_group(prompt, count):
                prompt_ids.append(list(ids))
                completion_ids.append(list(rollout.token_ids))
                logprobs.append(list(rollout.logprobs))
                completions_text.append(rollout.text)

        return {
            "prompt_ids": prompt_ids,
            "completion_ids": completion_ids,
            "logprobs": logprobs,
            # Extra keys are forwarded to reward functions; the graded text is the one the
            # engine actually produced, so rewards never re-decode token ids.
            "completions_text": completions_text,
        }

    rollout_func.close = client.close  # type: ignore[attr-defined]
    return rollout_func
