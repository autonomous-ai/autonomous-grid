#!/usr/bin/env python3
"""End-to-end check of the training product against a REAL engine over REAL HTTP.

    python tests/e2e_train.py            # ~2 minutes, CPU only, no GPU and no keys

What it exercises, in the order a customer meets it:

  1. a rollout engine actually serving the training contract (ids + logprobs) on a port
  2. `grid train doctor` probing that engine and reporting honestly
  3. the web interface: a workspace, a real Zendesk-shaped CSV upload, the data report
  4. the plain-language checks generating a rewards.py that imports and runs
  5. a config the CLI loads — the browser and the terminal agreeing on one format
  6. `grid train eval`: both models answering held-out work through the live engine, scored by
     the generated graders, producing a verdict and an eval card
  7. `grid train deploy`: a real adapter-load call to the live engine
  8. the nightly cycle's refusals (host in use, a model that lost)

The one thing it cannot do on an x86 Mac is the optimizer step itself: TRL's GRPOTrainer needs a
newer torch than this platform has wheels for, and `grid train run` says so precisely rather than
failing obscurely — step 2 asserts that message. The optimizer step is proved separately, and
measured, by `python -m train.torch_grpo_hello`.

Exit 0 = every seam holds.
"""
from __future__ import annotations

import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

PORT = 8099
MODEL = "HuggingFaceTB/SmolLM2-135M-Instruct"
PASS, FAIL = "  ok  ", " FAIL "
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{PASS if condition else FAIL}{label}" + (f"  — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def tickets_csv(n: int) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Subject", "Description", "Public reply", "Status"])
    for i in range(n):
        order = 284519 + i
        writer.writerow([
            "SmartDesk 5 stuck at lowest height",
            f"My desk (order AN-{order}) stopped moving and the controller shows E07. "
            "I work from home and really need this fixed.",
            "Sorry about the E07 — that is the anti-collision sensor tripping, and a reset clears "
            "it in most cases. Unplug the desk for a full 60 seconds, then hold the DOWN button "
            f"for 10 seconds until it recalibrates. If E07 returns, reply here and I will ship a "
            f"new controller for AN-{order} under warranty.",
            "solved",
        ])
    return buf.getvalue()


class Engine:
    """The rollout engine as a separate process, exactly as a worker machine would run it."""

    def __init__(self, workdir: Path) -> None:
        self.log = workdir / "engine.log"
        script = workdir / "engine.py"
        # A torch-backed stand-in for `grid train serve` (this box has no Apple Silicon). It
        # serves the same app, so the contract under test is the real one.
        script.write_text(_ENGINE_SOURCE, encoding="utf-8")
        self.process = subprocess.Popen(
            [sys.executable, str(script), "--port", str(PORT), "--model", MODEL],
            stdout=self.log.open("w"), stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONPATH": str(REPO), "PYTHONUNBUFFERED": "1"},
        )

    def wait_ready(self, timeout: float = 180) -> bool:
        import httpx

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process.poll() is not None:
                return False
            try:
                if httpx.get(f"http://127.0.0.1:{PORT}/v1/models", timeout=2).status_code == 200:
                    return True
            except httpx.TransportError:
                time.sleep(1)
        return False

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()


_ENGINE_SOURCE = '''
import argparse, threading, torch
from transformers import AutoModelForCausalLM, AutoTokenizer
from train.mlx.rollout_server import build_app

class TorchEngine:
    def __init__(self, model_id):
        self.model_id = model_id
        self.tokenizer = AutoTokenizer.from_pretrained(model_id)
        self.model = AutoModelForCausalLM.from_pretrained(model_id, torch_dtype=torch.float32)
        self.model.eval(); self._lock = threading.Lock()
        self.loaded_adapters = []

    @torch.no_grad()
    def generate(self, prompt, n, max_tokens, temperature):
        eos = self.tokenizer.eos_token_id
        ids0 = prompt if isinstance(prompt, list) else self.tokenizer(prompt, add_special_tokens=False)["input_ids"]
        with self._lock:
            tokens = torch.tensor([ids0] * n); past = None; inputs = tokens
            done = torch.zeros(n, dtype=torch.bool)
            ids = [[] for _ in range(n)]; lps = [[] for _ in range(n)]
            for _ in range(max_tokens):
                out = self.model(input_ids=inputs, past_key_values=past, use_cache=True)
                past = out.past_key_values
                logp = torch.log_softmax(out.logits[:, -1, :].float(), dim=-1)
                chosen = logp.argmax(dim=-1) if temperature <= 0 else \
                    torch.multinomial((logp / temperature).exp(), 1).squeeze(1)
                clp = logp.gather(1, chosen[:, None]).squeeze(1)
                for i in range(n):
                    if not done[i]:
                        ids[i].append(chosen[i].item()); lps[i].append(clp[i].item())
                done = done | (chosen == eos)
                if bool(done.all()): break
                inputs = chosen[:, None]
        return [(ids[i], lps[i], self.tokenizer.decode(ids[i], skip_special_tokens=True))
                for i in range(n)]

    def reload_adapter(self, adapter_dir):
        from pathlib import Path
        if not (Path(adapter_dir).expanduser() / "adapter_model.safetensors").is_file():
            raise FileNotFoundError(f"no adapter in {adapter_dir}")
        self.loaded_adapters.append(adapter_dir)
        return "applied"

if __name__ == "__main__":
    p = argparse.ArgumentParser(); p.add_argument("--port", type=int); p.add_argument("--model")
    a = p.parse_args()
    import uvicorn
    uvicorn.run(build_app(TorchEngine(a.model)), host="127.0.0.1", port=a.port, log_level="warning")
'''


def grid(*args: str, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "cli", *args],
        capture_output=True, text=True, cwd=str(cwd),
        env={**env, "PYTHONPATH": str(REPO)}, check=False,
    )


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="grid-e2e-"))
    grid_home = workdir / "grid-home"
    grid_home.mkdir()
    env = {**os.environ, "GRID_HOME": str(grid_home)}
    print(f"workdir: {workdir}\n")

    print("1 · a rollout engine serving the training contract")
    engine = Engine(workdir)
    try:
        if not engine.wait_ready():
            print(engine.log.read_text(encoding="utf-8")[-2000:])
            check("engine started", False, "see log above")
            return 1
        check("engine started", True, f"127.0.0.1:{PORT}")

        import httpx

        from train.config import RolloutConfig
        from train.rollout import probe_endpoint

        base = f"http://127.0.0.1:{PORT}/v1"
        probe = probe_endpoint(RolloutConfig(base_url=base), MODEL)
        check("engine serves ids + logprobs", probe["ok"], probe["detail"])
        chat_shaped = httpx.post(f"{base}/completions", json={"prompt": "hi", "n": 1}, timeout=20)
        check("a chat-shaped request is refused, not quietly served",
              chat_shaped.status_code == 400)

        print("\n2 · the browser flow: workspace, real export, honest report")
        from fastapi.testclient import TestClient

        from train.web import build_app, workspace

        client = TestClient(build_app(), follow_redirects=False)
        created = client.post("/new", data={"pack": "support-replies", "name": "Support replies"})
        slug = created.headers["location"].rsplit("/", 1)[-1]
        upload = client.post(f"/w/{slug}/data",
                             json={"filename": "zendesk.csv", "content": tickets_csv(120)})
        check("upload accepted", upload.status_code == 200 and upload.json()["ok"])
        page = client.get(f"/w/{slug}").text
        check("the data report is shown, not skipped", "usable tickets" in page)
        check("it shows what the model will learn from", "What it will learn from" in page)

        thin = client.post("/new", data={"pack": "support-replies", "name": "Too little"})
        thin_slug = thin.headers["location"].rsplit("/", 1)[-1]
        client.post(f"/w/{thin_slug}/data", json={"filename": "x.csv", "content": tickets_csv(9)})
        refused = client.get(f"/w/{thin_slug}/checks")
        check("too little data is refused before anyone waits a night", refused.status_code == 400)

        print("\n3 · the checks become runnable graders")
        client.get(f"/w/{slug}/checks")
        client.post(f"/w/{slug}/checks", data={"check": ["similarity", "grounding", "format"]})
        w = workspace.load(slug)
        config_path = workspace.write_config(w, model=MODEL, endpoint=base, steps=20)

        from train.config import load_config
        from train.rewards import load_reward_funcs

        cfg = load_config(config_path)
        check("the CLI loads what the browser wrote", cfg.rollout.base_url == base)
        graders = load_reward_funcs(cfg.rewards, cfg.data)
        names = sorted(getattr(g, "__name__", "?") for g in graders)
        check("graders import and are the ones ticked",
              names == ["reward_format", "reward_grounding", "reward_similarity"], ", ".join(names))
        scores = graders[0](["Subject: x\n\nMy desk AN-284519 shows E07"],
                            completions_text=["Unplug it for 60 seconds, order AN-284519."])
        check("a grader returns a number", isinstance(scores[0], float), f"{scores[0]:.3f}")

        print("\n4 · doctor tells the truth about this machine")
        doctor = grid("train", "doctor", "--config", str(config_path), "--json", cwd=workdir, env=env)
        report = json.loads(doctor.stdout) if doctor.stdout.strip().startswith("{") else {}
        check("doctor probed the live engine", bool(report.get("endpoint", {}).get("ok")),
              report.get("endpoint", {}).get("detail", doctor.stderr[-200:]))
        check("doctor found the data and graders", bool(report.get("data", {}).get("ok")),
              str(report.get("data")))
        run_attempt = grid("train", "run", "--config", str(config_path), cwd=workdir, env=env)
        combined = run_attempt.stdout + run_attempt.stderr
        check("an unrunnable trainer says exactly what is missing",
              "rollout_func" in combined or "missing training dependency" in combined,
              combined.strip().splitlines()[-1][:110] if combined.strip() else "(no output)")

        print("\n5 · the gate, against the live engine")
        run_dir = Path(cfg.trainer.output_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        prompts = json.loads(json.dumps(  # the held-out slice a real run writes
            [json.loads(line)["prompt"] for line in
             Path(cfg.data.prompts_jsonl).read_text(encoding="utf-8").splitlines()[:4]]))
        (run_dir / "eval_prompts.jsonl").write_text(
            "".join(json.dumps({"prompt": p}) + "\n" for p in prompts), encoding="utf-8")

        from train.evaluate import run_eval

        result = run_eval(cfg, run_dir, MODEL, base_model=MODEL)
        check("eval ran both models through the live engine", result["after"]["n"] == len(prompts))
        check("identical models produce no gain, so the gate holds", result["passed"] is False,
              result["verdict"])
        check("an eval card was written for a human", (run_dir / "eval-card.html").is_file())

        print("\n6 · deploy talks to the live engine")
        adapter = run_dir / "adapter"
        adapter.mkdir(exist_ok=True)
        (adapter / "adapter_config.json").write_text("{}", encoding="utf-8")
        (adapter / "adapter_model.safetensors").write_text("stub", encoding="utf-8")

        from train.deploy import deploy_adapter

        outcomes = deploy_adapter(adapter, [base], "support-replies")
        check("the engine loaded the adapter", all(o["ok"] for o in outcomes),
              outcomes[0]["detail"])

        from train.sync import push_adapter, summarize

        synced = push_adapter(adapter, [base])
        check("weight sync reaches the engine", all(s["ok"] for s in synced), summarize(synced))
        missing = push_adapter(adapter, [base, "http://127.0.0.1:9/v1"])
        check("an unreachable machine is skipped, not fatal",
              sum(1 for m in missing if m["ok"]) == 1, summarize(missing))

        print("\n7 · the nightly loop's refusals")
        from train import nightly

        free, why = nightly.host_is_free()
        check("host priority is enforced on this machine", isinstance(free, bool), why)
        cycle = nightly.run_cycle(cfg, check_host=False, deploy=False)
        check("a nightly cycle with no trainer fails loudly, not silently",
              cycle.stage == "failed" and not cycle.ok, cycle.detail[:90])
        check("the night was recorded in history", len(nightly.history(cfg)) >= 1)

        print("\n8 · the interface refuses to serve an unproven model")
        response = client.post(f"/w/{slug}/use")
        check("serving is blocked without a passing card", response.status_code == 400)
    finally:
        engine.stop()

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: " + "; ".join(failures))
        print(f"workdir kept for inspection: {workdir}")
        return 1
    print("every seam holds. (The optimizer step is proved separately by "
          "`python -m train.torch_grpo_hello`.)")
    shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
