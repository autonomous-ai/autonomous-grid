"""Talk to the model you just trained.

Every other screen in this product is *about* a model. This is the one where you meet it: paste a
ticket, press a button, read the reply — and read what the old model would have said next to it.
That side-by-side is the whole argument for the product, made in five seconds with the user's own
words instead of a number.

Deliberately the same request shape training used (`/completions` with the sampled-token fields,
greedy decoding), so what she sees here is what her team will get, not a differently-configured
approximation. Greedy also means pressing the button twice gives the same answer, which matters
when someone is deciding whether to trust it.
"""
from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path

from ..config import RolloutConfig, TrainRunConfig
from ..rollout import GridRolloutClient


@dataclasses.dataclass
class Answer:
    """One model's reply to one piece of work."""

    label: str          # what to call this model on screen
    model: str          # the name the endpoint serves it under
    text: str
    seconds: float
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def _ask(cfg: TrainRunConfig, model: str, label: str, prompt: str, *,
         max_tokens: int, transport=None) -> Answer:
    started = time.time()
    rollout_cfg = dataclasses.replace(cfg.rollout, max_tokens=max_tokens)
    client = GridRolloutClient(rollout_cfg, model, transport=transport)
    try:
        rollouts = client.generate_group(prompt, 1, temperature=0.0)
        return Answer(label, model, rollouts[0].text.strip(), round(time.time() - started, 1))
    except SystemExit as exc:
        # A playground must never crash the page: a machine that is asleep or a model that was
        # never loaded is an ordinary thing to say out loud.
        return Answer(label, model, "", round(time.time() - started, 1), detail_of(exc))
    finally:
        client.close()


def detail_of(exc: SystemExit) -> str:
    """Turn our own CLI-shaped errors into one sentence a non-engineer can act on."""
    text = str(exc).replace("grid train: ", "")
    if "unreachable" in text or "Connection" in text:
        return ("The machine that answers isn't reachable right now — it may be asleep. "
                "Wake it and try again.")
    if "404" in text or "not found" in text.lower():
        return ("This model isn't loaded on the machine yet. Press “Start using this model” on the "
                "result page first.")
    if "logprobs" in text or "token ids" in text:
        return ("The machine is serving answers in a form this page can't read — it needs to be "
                "started by Grid (`grid train serve`).")
    return text[:240]


def compare(cfg: TrainRunConfig, prompt: str, trained_model: str, *,
            base_label: str = "What you use today", trained_label: str = "Your trained model",
            max_tokens: int = 240, transport=None) -> list[Answer]:
    """Ask both models the same thing. Order is fixed: today's model first, then hers."""
    prompt = prompt.strip()
    if not prompt:
        return []
    answers = [
        _ask(cfg, cfg.rollout_model, base_label, prompt, max_tokens=max_tokens, transport=transport),
    ]
    if trained_model and trained_model != cfg.rollout_model:
        answers.append(
            _ask(cfg, trained_model, trained_label, prompt, max_tokens=max_tokens,
                 transport=transport)
        )
    return answers


def sample_prompts(run_dir: Path, limit: int = 3) -> list[str]:
    """A few of her own held-out items, so the first thing she tries is real work.

    Held-out on purpose: these are pieces the model never trained on, which makes the demo honest
    rather than a recital.
    """
    path = Path(run_dir) / "eval_prompts.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            prompt = json.loads(line).get("prompt")
        except json.JSONDecodeError:
            continue
        if isinstance(prompt, str) and prompt.strip():
            out.append(prompt.strip())
        if len(out) >= limit:
            break
    return out


def rollout_config_for(endpoint: str, api_key_env: str = "GRID_TRAIN_API_KEY") -> RolloutConfig:
    """For asking a model outside a workspace (the home page's quick try)."""
    return RolloutConfig(base_url=endpoint, api_key_env=api_key_env, max_tokens=240)
