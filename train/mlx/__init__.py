"""The Apple-Silicon trainer lane: clean-room GRPO on MLX (ADR 0019, MLX slice).

First slice is the RL "hello world" — `python -m train.mlx.grpo_hello` — a complete
sample→grade→update climb on one M-series Mac, no CUDA anywhere. It exists to prove the loop
on Apple Silicon and to seed the lane with correct GRPO semantics (notably: completion
logprobs are computed conditioned on the prompt — the exact defect that made us pass on the
existing community MLX trainer).

Layout mirrors the TRL lane's split: `hello_task.py` is pure Python (task, reward, advantage
math — unit-tested on any machine, including x86 CI); `grpo_hello.py` touches `mlx` and only
runs on Apple Silicon. Phase 2 replaces in-process generation with the Grid rollout contract
from `train/rollout.py`, which is why sampling returns (token_ids, logprobs) in exactly that
shape.
"""
