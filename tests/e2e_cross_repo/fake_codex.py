"""A deterministic Codex app-server for the distributed Goal cross-repo E2E.

It implements only the JSON-RPC methods the Grid Goal runner uses. The behavior is deliberately
node-specific so the test can kill A and B mid-turn and prove C receives only relay-published Git
state, never a shared directory or process object.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import uuid
from pathlib import Path


def emit(value: dict) -> None:
    print(json.dumps(value, separators=(",", ":")), flush=True)


def reply(request: dict, result: dict | None = None) -> None:
    emit({"id": request["id"], "result": result or {}})


def history_path() -> Path:
    home = Path(os.environ["CODEX_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    return home / "fake-history.json"


def load_history() -> list[dict]:
    path = history_path()
    if not path.is_file():
        return []
    value = json.loads(path.read_text())
    if not isinstance(value, list):
        raise TypeError("distributed history is not a list")
    return value


def save_history(value: list[dict]) -> None:
    history_path().write_text(json.dumps(value, separators=(",", ":")) + "\n")


def run_turn(node: str, call_tool=None) -> tuple[str, str, int]:
    cwd = Path.cwd()
    history = load_history()
    scenario = os.environ.get("GRID_E2E_GOAL_SCENARIO")
    mixed = scenario == "mixed"

    if scenario == "image":
        if node != "A" or history:
            raise RuntimeError(f"image Goal reached the wrong worker: {node}, {history!r}")
        # A valid 1x1 PNG. The scenario measures capability-aware placement and durable artifact
        # transport; image quality belongs to a model/tool eval, not this protocol fixture.
        (cwd / "poster.png").write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "/x8AAusB9Wl2nCEAAAAASUVORK5CYII="))
        history.append({"node": "A", "capability": "image_generation"})
        save_history(history)
        return "complete", "A generated the requested PNG", 100

    if scenario == "optional_subgoal":
        if node == "A" and not history:
            if call_tool is None:
                raise RuntimeError("optional subgoal scenario received no dynamic tool bridge")
            result = call_tool("grid_spawn_subgoal", {
                "objective": "Explore a disposable game mechanic",
                "done_when": "Report whether the experiment worked",
                "agents": ["codex"],
                "required_capabilities": ["optional_worker"],
                "required": False,
                "token_budget": 2_000,
            })
            content = ((result.get("contentItems") or [{}])[0]).get("text")
            envelope = json.loads(content or "{}")
            child_id = ((envelope.get("body") or {}).get("id"))
            if not result.get("success") or not child_id:
                raise RuntimeError(f"optional subgoal creation failed: {result!r}")
            history.append({"node": "A", "spawned_optional_child": child_id})
            save_history(history)
            return "active", f"A spawned optional child Goal {child_id}", 100
        if node == "B" and not history:
            raise RuntimeError("the optional experiment failed as intended")
        if node == "C" and len(history) == 1 and history[0].get("node") == "A":
            (cwd / "FINAL.md").write_text(
                "# Parent complete\n\nThe optional experiment failed without blocking delivery.\n")
            history.append({"node": "C", "ignored_optional_failure": True})
            save_history(history)
            return "complete", "C completed the parent after optional child failure", 200
        raise RuntimeError(f"unexpected optional subgoal turn on {node}: {history!r}")

    if scenario == "subgoal":
        if node == "A" and not history:
            if call_tool is None:
                raise RuntimeError("subgoal scenario received no dynamic tool bridge")
            result = call_tool("grid_spawn_subgoal", {
                "objective": "Write the child instructions",
                "done_when": "README.md exists",
                "agents": ["claude"],
                "evals": [{"type": "file", "name": "instructions", "path": "README.md"}],
                "token_budget": 2_000,
            })
            content = ((result.get("contentItems") or [{}])[0]).get("text")
            envelope = json.loads(content or "{}")
            child_id = ((envelope.get("body") or {}).get("id"))
            if not result.get("success") or not child_id:
                raise RuntimeError(f"subgoal creation failed: {result!r}")
            history.append({"node": "A", "spawned_child": child_id})
            save_history(history)
            return "active", f"A spawned child Goal {child_id}", 100
        if node == "C" and len(history) == 1 and history[0].get("node") == "A":
            if not (cwd / "README.md").exists():
                raise RuntimeError("parent C resumed without the child README fan-in")
            (cwd / "FINAL.md").write_text("# Parent complete\n\nChild instructions were merged.\n")
            history.append({"node": "C", "fan_in": "README.md"})
            save_history(history)
            return "complete", "C verified child fan-in and completed parent", 200
        raise RuntimeError(f"unexpected subgoal parent turn on {node}: {history!r}")

    if not (cwd / "index.html").exists():
        if node != "A" or history:
            raise RuntimeError(f"feature 1 reached {node} with unexpected history {history!r}")
        (cwd / "index.html").write_text("""<!doctype html>
<html><head><meta charset=\"utf-8\"><title>Grid Click</title><link rel=\"stylesheet\" href=\"style.css\"></head>
<body><main><h1>Grid Click</h1><p>Score: <strong id=\"score\">0</strong></p><button id=\"target\">Click me</button></main><script src=\"game.js\"></script></body></html>
""")
        history.append({"node": "A", "feature": 1})
        save_history(history)
        return "active", "A completed feature 1", 100

    if not (cwd / "game.js").exists():
        if node == "A":
            # This file exists only in A's uncommitted worktree. B must not see it after reclaim.
            (cwd / "partial-feature-2.tmp").write_text("A died here\n")
            time.sleep(90)
            raise RuntimeError("node A was expected to be killed")
        if node != "B" or history != [{"node": "A", "feature": 1}]:
            raise RuntimeError(f"B did not receive A's exact checkpoint: {history!r}")
        assert not (cwd / "partial-feature-2.tmp").exists(), "A's uncommitted file crossed nodes"
        (cwd / "game.js").write_text("""const score = document.querySelector('#score');
const target = document.querySelector('#target');
let points = 0;
target.addEventListener('click', () => {
  points += 1; score.textContent = String(points);
  target.style.transform = `translate(${Math.random()*220-110}px, ${Math.random()*120-60}px)`;
});
""")
        history.append({"node": "B", "feature": 2})
        save_history(history)
        return "active", "B completed feature 2", 200

    if not (cwd / "style.css").exists():
        if mixed:
            if node != "C" or history != [{"node": "A", "feature": 1}]:
                raise RuntimeError(
                    f"Codex C did not resume its own A checkpoint after Claude B: {history!r}")
            if "addEventListener('click'" not in (cwd / "game.js").read_text():
                raise RuntimeError("Codex C did not receive Claude B's committed feature 2")
            assert not (cwd / "partial-feature-34.tmp").exists(), (
                "Claude B's uncommitted file crossed machines")
            (cwd / "style.css").write_text(
                "body{font:18px system-ui;background:#111827;color:#f9fafb;text-align:center}\n")
            (cwd / "README.md").write_text(
                "# Grid Click\n\nCodex, Claude, and Codex completed this game across Grid nodes.\n")
            history.append({"node": "C", "features": [3, 4], "after": "claude-B"})
            save_history(history)
            return "complete", "C completed features 3 and 4 after Claude B", 300
        if node == "B":
            (cwd / "partial-feature-34.tmp").write_text("B died here\n")
            time.sleep(90)
            raise RuntimeError("node B was expected to be killed")
        expected = [{"node": "A", "feature": 1}, {"node": "B", "feature": 2}]
        if node != "C" or history != expected:
            raise RuntimeError(f"C did not receive A+B's exact checkpoint: {history!r}")
        assert not (cwd / "partial-feature-34.tmp").exists(), "B's uncommitted file crossed nodes"
        (cwd / "style.css").write_text("""body{font:18px system-ui;background:#111827;color:#f9fafb;text-align:center}main{margin:12vh auto;max-width:32rem}button{padding:1rem 2rem;border:0;border-radius:999px;background:#34d399;font-weight:700;cursor:pointer;transition:transform .15s} 
""")
        (cwd / "README.md").write_text("""# Grid Click

Open `index.html`, then click the moving button to increase your score.
""")
        history.append({"node": "C", "features": [3, 4]})
        save_history(history)
        return "complete", "C completed features 3 and 4", 300

    return "complete", "already complete", 300


def main() -> int:
    node = os.environ.get("GRID_E2E_GOAL_NODE", "?")
    thread_id = "grid-e2e-" + str(uuid.uuid4())
    native_status = "paused"
    tokens = 0
    dynamic_tools: list[dict] = []
    early_pause = False

    def call_tool(name: str, arguments: dict) -> dict:
        nonlocal early_pause
        request_id = 90_000
        emit({"id": request_id, "method": "item/tool/call", "params": {
            "tool": name, "arguments": arguments, "callId": "subgoal-spawn"}})
        while True:
            response_line = sys.stdin.readline()
            if not response_line:
                raise RuntimeError("Grid closed before answering the subgoal tool call")
            response = json.loads(response_line)
            # Grid pauses native continuation as soon as it sees turn/started. A tool request can
            # make that client request arrive before the tool response; service it without losing
            # the in-flight call.
            if (response.get("method") == "thread/goal/set"
                    and response.get("params", {}).get("status") == "paused"):
                early_pause = True
                reply(response, {"goal": {"status": "paused"}})
                continue
            if response.get("id") != request_id or "result" not in response:
                raise RuntimeError(f"unexpected subgoal tool response: {response!r}")
            return response["result"]
    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        if "id" not in request:
            continue
        if method == "initialize":
            reply(request, {"serverInfo": {"name": "fake-codex"}})
        elif method == "thread/start":
            dynamic_tools = request.get("params", {}).get("dynamicTools") or []
            reply(request, {"thread": {"id": thread_id}})
        elif method == "thread/resume":
            thread_id = request.get("params", {}).get("threadId") or thread_id
            reply(request, {"thread": {"id": thread_id}})
        elif method == "thread/goal/set":
            desired = request.get("params", {}).get("status")
            # Completion wins the runner's pause request, as it does in the native Goal state.
            if native_status != "complete":
                native_status = desired or native_status
            reply(request, {"goal": {"status": native_status}})
            if desired == "active":
                emit({"method": "turn/started", "params": {"threadId": thread_id,
                                                               "turn": {"id": str(uuid.uuid4())}}})
                try:
                    bridge = call_tool if any(
                        tool.get("name") == "grid_spawn_subgoal" for tool in dynamic_tools) else None
                    native_status, output, tokens = run_turn(node, bridge)
                    if early_pause and native_status != "complete":
                        native_status = "paused"
                    emit({"method": "thread/tokenUsage/updated", "params": {
                        "tokenUsage": {"total": {"totalTokens": tokens}}}})
                    emit({"method": "turn/completed", "params": {
                        "threadId": thread_id,
                        "turn": {"status": "completed", "output": output}}})
                except Exception as exc:  # noqa: BLE001 - emulate an app-server terminal event
                    emit({"method": "turn/completed", "params": {
                        "threadId": thread_id,
                        "turn": {"status": "failed", "error": str(exc)}}})
        elif method == "thread/goal/get":
            reply(request, {"goal": {"status": native_status, "tokensUsed": tokens,
                                      "timeUsedSeconds": 1}})
        else:
            emit({"id": request["id"], "error": {"code": -32601,
                                                   "message": f"unsupported {method}"}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
