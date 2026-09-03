from __future__ import annotations

import argparse
import base64
import json
import threading
import tomllib
from types import SimpleNamespace

import pytest

from cli.train import cmd_train_submit_sft, cmd_train_verify_result
from remote import task_agent, tasks, train_worker
from train.config import SAMPLE


def test_train_worker_command_is_shell_free_and_confined(tmp_path):
    config = tmp_path / "grid-train-input" / "grid-train.toml"
    config.parent.mkdir()
    config.write_text("[model]\nname='tiny'\n", encoding="utf-8")
    job = {
        "kind": "train.sft", "agent_kind": "train-mlx",
        "prompt": json.dumps({"version": 1, "backend": "mlx", "iters": 12}),
    }
    argv = train_worker.command(job, tmp_path)
    assert argv[:5] == [train_worker.sys.executable, "-I", "-m", "cli", "train"]
    assert argv[-2:] == ["--iters", "12"]
    assert not any(token in argv for token in ("sh", "bash", "-c"))
    assert train_worker.timeout(job, tmp_path) == 24 * 3600

    job["prompt"] = json.dumps({
        "version": 1, "backend": "mlx", "run_dir": "../../outside"})
    with pytest.raises(ValueError, match="inside|relative"):
        train_worker.command(job, tmp_path)


def test_training_profiles_are_opt_in_and_dependency_gated(monkeypatch):
    monkeypatch.setenv("GRID_TASK_AGENT_KINDS", "train-mlx,train-torch")
    monkeypatch.setattr(train_worker, "available", lambda kind: kind == "train-torch")
    assert tasks._agent_profiles() == (
        {"kind": "train-torch", "capabilities": ["sft"]},)
    assert tasks.has_non_claude_claim_capacity()


def test_claude_rate_limit_does_not_pause_training_worker(monkeypatch):
    monkeypatch.setenv("GRID_TASK_AGENT_KINDS", "claude,train-torch")
    monkeypatch.setattr(tasks, "_agent_profiles", lambda: (
        {"kind": "claude", "capabilities": []},
        {"kind": "train-torch", "capabilities": ["sft"]},
    ))
    state = SimpleNamespace(
        stop=threading.Event(), tasks_stop=threading.Event(),
        signaling_url="https://relay", token=lambda: "token",
        refresh=lambda stale_token=None: False)
    claims = []

    def claim(_state, *, excluded_agent_kinds=()):
        claims.append(excluded_agent_kinds)
        state.tasks_stop.set()
        return None

    monkeypatch.setattr(tasks, "claim_once", claim)
    capacity = SimpleNamespace(pause_seconds=lambda: 3600.0)
    tasks.task_loop(state, capacity=capacity)
    assert claims == [("claude",)]


def test_run_task_executes_typed_trainer_without_agent(monkeypatch, tmp_path):
    config = tmp_path / "grid-train-input" / "grid-train.toml"
    config.parent.mkdir()
    config.write_text("[model]\nname='tiny'\n", encoding="utf-8")
    monkeypatch.setattr(task_agent, "workspace_for", lambda *_: tmp_path)
    monkeypatch.setattr(task_agent, "ensure_workspace", lambda path: path)
    monkeypatch.setattr(task_agent, "ensure_cache", lambda _path: None)
    monkeypatch.setattr(tasks.task_evict, "touch", lambda _path: None)
    monkeypatch.setattr(tasks.task_evict, "sweep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(train_worker, "preflight", lambda _kind: None)
    monkeypatch.setattr(task_agent, "child_env", lambda **_kwargs: {})
    spawned = []

    def run(argv, **kwargs):
        spawned.append((argv, kwargs))
        adapter = tmp_path / "grid-train-result" / "adapter"
        adapter.mkdir(parents=True)
        (adapter / "adapter.safetensors").write_bytes(b"trained")
        (adapter.parent / "run.json").write_text(json.dumps({
            "stage": "sft", "backend": "torch", "model": "tiny",
            "adapter": str(adapter),
        }), encoding="utf-8")
        return 0, "Adapter saved: grid-train-result/adapter\n"

    monkeypatch.setattr(tasks, "_run_child", run)
    outcome = tasks.run_task({
        "task_id": "T1", "project_id": "P1", "member_key": "member",
        "conversation_id": "C1", "kind": "train.sft", "agent_kind": "train-torch",
        "prompt": json.dumps({"version": 1, "backend": "torch"}),
    })
    assert outcome.state == "completed"
    assert "Adapter saved" in outcome.output
    assert spawned[0][0][1:5] == ["-I", "-m", "cli", "train"]
    assert spawned[0][1]["cwd"] == str(tmp_path)
    manifest = json.loads(
        (tmp_path / "grid-train-result" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["total_bytes"] == len(b"trained")
    run = json.loads(
        (tmp_path / "grid-train-result" / "run.json").read_text(encoding="utf-8"))
    assert run["adapter"] == "adapter"

    assert cmd_train_verify_result(argparse.Namespace(
        path=str(tmp_path / "grid-train-result"), json=False)) == 0


def test_verify_result_refuses_a_tampered_adapter(tmp_path):
    config = tmp_path / "grid-train-input" / "grid-train.toml"
    config.parent.mkdir()
    config.write_text("[model]\nname='tiny'\n", encoding="utf-8")
    result = tmp_path / "grid-train-result"
    adapter = result / "adapter"
    adapter.mkdir(parents=True)
    artifact = adapter / "adapter.safetensors"
    artifact.write_bytes(b"original")
    (result / "run.json").write_text(json.dumps({
        "stage": "sft", "backend": "torch", "model": "tiny", "adapter": str(adapter),
    }), encoding="utf-8")
    job = {
        "kind": "train.sft", "agent_kind": "train-torch",
        "prompt": json.dumps({"version": 1, "backend": "torch"}),
    }
    train_worker.finalize_result(job, tmp_path)
    artifact.write_bytes(b"tampered")
    with pytest.raises(SystemExit, match="failed verification"):
        cmd_train_verify_result(argparse.Namespace(path=str(result), json=False))

    artifact.write_bytes(b"original")
    run = json.loads((result / "run.json").read_text(encoding="utf-8"))
    run["model"] = "different-model"
    (result / "run.json").write_text(json.dumps(run), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        train_worker.verify_result(result)


def test_run_task_refuses_a_zero_exit_without_an_adapter(monkeypatch, tmp_path):
    config = tmp_path / "grid-train-input" / "grid-train.toml"
    config.parent.mkdir()
    config.write_text("[model]\nname='tiny'\n", encoding="utf-8")
    monkeypatch.setattr(task_agent, "workspace_for", lambda *_: tmp_path)
    monkeypatch.setattr(task_agent, "ensure_workspace", lambda path: path)
    monkeypatch.setattr(task_agent, "ensure_cache", lambda _path: None)
    monkeypatch.setattr(tasks.task_evict, "touch", lambda _path: None)
    monkeypatch.setattr(tasks.task_evict, "sweep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(train_worker, "preflight", lambda _kind: None)
    monkeypatch.setattr(task_agent, "child_env", lambda **_kwargs: {})
    monkeypatch.setattr(tasks, "_run_child", lambda *_args, **_kwargs: (0, "done"))

    outcome = tasks.run_task({
        "task_id": "T1", "project_id": "P1", "member_key": "member",
        "conversation_id": "C1", "kind": "train.sft", "agent_kind": "train-torch",
        "prompt": json.dumps({"version": 1, "backend": "torch"}),
    })
    assert outcome.state == "failed"
    assert "no usable adapter" in outcome.error


def test_submit_sft_uploads_portable_config_and_data(monkeypatch, tmp_path, capsys):
    config = tmp_path / "grid-train.toml"
    config.write_text(SAMPLE.replace(
        "# prompts_jsonl = \"tasks.jsonl\"", 'prompts_jsonl = "tasks.jsonl"').replace(
            '# python_file = "rewards.py"', 'python_file = "rewards.py"'),
        encoding="utf-8")
    data = tmp_path / "dataset" / "train" / "sft.jsonl"
    data.parent.mkdir(parents=True)
    rows = [
        {"messages": [{"role": "user", "content": f"question {i}"},
                      {"role": "assistant", "content": f"answer {i}"}]}
        for i in range(20)
    ]
    data.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    from cli import remote_task
    from remote import relay

    monkeypatch.setattr(remote_task, "_resolve", lambda _args: ("https://relay", "token", "forge"))
    monkeypatch.setattr(remote_task, "_resolve_project", lambda *_args: "project-1")
    sent = {}

    def create(*args, **kwargs):
        sent.update(kwargs)
        return {"id": "job-1", "project_id": "project-1", "state": "queued"}

    monkeypatch.setattr(relay, "create_train_job", create)
    args = argparse.Namespace(
        config=str(config), data=str(tmp_path / "dataset"), backend="torch", iters=None,
        timeout_hours=36, queue_timeout_hours=240,
        grid="forge", project="project-1", json=False)
    assert cmd_train_submit_sft(args) == 0
    assert sent["spec"]["backend"] == "torch"
    assert sent["spec"]["run_timeout_seconds"] == 36 * 3600
    assert sent["spec"]["queue_timeout_seconds"] == 240 * 3600
    by_path = {
        item["path"]: base64.b64decode(item["content_b64"])
        for item in sent["files"]
    }
    uploaded = tomllib.loads(by_path["grid-train-input/grid-train.toml"].decode())
    assert uploaded["data"]["prompts_jsonl"] == "grid-train-input/prompts.jsonl"
    assert len(by_path["grid-train-input/sft.jsonl"].splitlines()) == 20
    output = capsys.readouterr().out
    assert "job-1" in output and "grid task fetch" in output
    assert "--into grid-train-result" in output
