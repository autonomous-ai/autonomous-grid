"""What this office can actually do — offered as choices, not as a URL to paste.

The browser flow used to ask for "the grid's address" and a number of "attempts". Both are
questions only an engineer can answer, and either one is where a support manager stops. This
module answers them instead: it finds the machines that are serving, works out which kind of
training can really run here, and hands the page a short list of options with a preselected
default.

The second job is being honest about the ladder. A Mac with mlx-lm can imitate the answers a team
already wrote (stage one) but cannot yet run the feedback loop, which needs a rollout engine and
graders. Offering the feedback loop on a machine that can't finish it is how you lose a night —
so the page offers what the hardware can complete and says so in one sentence.
"""
from __future__ import annotations

import contextlib
import dataclasses
import importlib.util
import platform


@dataclasses.dataclass(frozen=True)
class Machine:
    """One place answers can come from, described the way its owner would describe it."""

    url: str
    label: str
    detail: str
    kind: str                   # ollama | lm-studio | vllm | mlx | llama.cpp | grid
    serves_training: bool       # can it return the sampled tokens a feedback run needs?
    models: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        return self.url


_FRIENDLY = {
    "ollama": "Ollama",
    "lm-studio": "LM Studio",
    "vllm": "vLLM",
    "mlx": "Grid's own engine (MLX)",
    "llama.cpp": "llama.cpp",
    "comfyui": "ComfyUI",
}
# Only these return the sampled token ids and their probabilities that a feedback run needs.
_TRAINING_CAPABLE = ("vllm", "mlx")


def find_machines() -> list[Machine]:
    """Everything serving on this computer, plus any grid this computer belongs to."""
    machines: list[Machine] = []
    try:
        from shared.system.detect import detect_engines

        found = detect_engines(timeout=0.4)
    except Exception:  # noqa: BLE001 — a probe failure means "found nothing", never a 500
        found = []
    for engine in found:
        if engine.media:
            continue                            # image/video engines can't answer text work
        models = tuple(engine.models[:6])
        machines.append(Machine(
            url=engine.endpoint_url,
            label=f"{_FRIENDLY.get(engine.label, engine.label)} on this computer",
            # Short names: a repository path like "HuggingFaceTB/SmolLM2-135M-Instruct" is noise to
            # the person choosing a machine, and the part after the slash is the recognisable bit.
            detail=(", ".join(m.rsplit("/", 1)[-1] for m in models[:3]) if models
                    else "no models loaded yet"),
            kind=engine.label,
            serves_training=engine.label in _TRAINING_CAPABLE,
            models=models,
        ))

    # "No grid configured" is an ordinary answer here, so a failure just means we offer whatever
    # the local probe already found.
    with contextlib.suppress(Exception):
        from train.endpoints import resolve

        for endpoint in resolve():
            if any(m.url == endpoint.base_url for m in machines):
                continue
            machines.append(Machine(
                url=endpoint.base_url,
                label=(f"Your whole grid — {endpoint.label}" if endpoint.mode == "local"
                       else f"Your hosted grid — {endpoint.label}"),
                detail=("every machine on this network"
                        if endpoint.mode == "local" else "machines in any office"),
                kind="grid",
                # A grid may route to a capable engine; the probe at run time decides for real.
                serves_training=True,
            ))
    # Training-capable first, then by name: the default should be the one that can do most.
    machines.sort(key=lambda m: (not m.serves_training, m.label))
    return machines


def _installed(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


@dataclasses.dataclass(frozen=True)
class Capability:
    """Which rung of the ladder this computer can finish, and how to say it."""

    can_imitate: bool          # stage one — learn from answers the team already wrote
    can_feedback: bool         # the feedback loop — needs a trainer AND a capable engine
    recommended: str           # "sft" | "rl" | "none"
    headline: str
    detail: str
    backend: str = ""          # mlx | torch | ""

    @property
    def ready(self) -> bool:
        return self.recommended != "none"


def capability(machines: list[Machine] | None = None) -> Capability:
    """What can actually run here, decided before she presses anything."""
    machines = machines if machines is not None else find_machines()
    apple = platform.system() == "Darwin" and platform.machine() == "arm64"
    has_mlx = apple and _installed("mlx_lm")
    has_torch = _installed("torch") and _installed("trl") and _installed("peft")
    trainable_engine = any(m.serves_training for m in machines)

    # The feedback loop needs both halves: something to sample from, and something to learn with.
    can_feedback = has_torch and trainable_engine and _has_feedback_trainer()
    can_imitate = has_mlx or has_torch

    if can_feedback:
        return Capability(
            True, True, "rl",
            "This computer can do both stages.",
            "It will learn from your examples first, then improve by trying and being scored.",
            backend="mlx" if has_mlx else "torch",
        )
    if can_imitate:
        return Capability(
            True, False, "sft",
            "This computer can learn from your examples.",
            ("That is the stage that matters most and it runs right here. The second stage — "
             "improving by trial and feedback — needs a machine with a bigger graphics card, and "
             "you can add one later without redoing this."),
            backend="mlx" if has_mlx else "torch",
        )
    if apple:
        return Capability(
            False, False, "none",
            "One thing to install first.",
            "Run `pip install mlx-lm` in a terminal, then reload this page. It is Apple's own "
            "training library and takes about a minute.",
        )
    return Capability(
        False, False, "none",
        "One thing to install first.",
        "Run `pip install 'grid[train]'` in a terminal, then reload this page.",
    )


def _has_feedback_trainer() -> bool:
    """Is the installed TRL one with the hook the feedback loop needs?"""
    try:
        import inspect

        import trl

        trainer = getattr(trl, "GRPOTrainer", None)
        return bool(trainer) and "rollout_func" in inspect.signature(trainer.__init__).parameters
    except Exception:  # noqa: BLE001 — no TRL, or a version that explodes on introspection
        return False


# How long she wants to give it, in her units. Steps are ours, not hers.
EFFORT_CHOICES = (
    {"id": "quick", "label": "A quick look — about 20 minutes",
     "detail": "Enough to see whether it is learning at all.", "steps": 40, "iters": 150},
    {"id": "evening", "label": "A couple of hours", "detail": "The usual choice.",
     "steps": 120, "iters": 600, "default": True},
    {"id": "overnight", "label": "Overnight — leave it running",
     "detail": "Best result; the machine is free anyway.", "steps": 400, "iters": 2000},
)


def effort(choice_id: str) -> dict:
    for choice in EFFORT_CHOICES:
        if choice["id"] == choice_id:
            return choice
    return next(c for c in EFFORT_CHOICES if c.get("default"))
