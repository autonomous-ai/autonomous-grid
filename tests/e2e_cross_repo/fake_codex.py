"""A deterministic Codex app-server for the distributed Goal cross-repo E2E.

It implements only the JSON-RPC methods the Grid Goal runner uses. The behavior is deliberately
node-specific so the test can kill A and B mid-turn and prove C receives only relay-published Git
state, never a shared directory or process object.
"""
from __future__ import annotations

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


def run_turn(node: str) -> tuple[str, str, int]:
    cwd = Path.cwd()
    history = load_history()

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
    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        if "id" not in request:
            continue
        if method == "initialize":
            reply(request, {"serverInfo": {"name": "fake-codex"}})
        elif method == "thread/start":
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
                    native_status, output, tokens = run_turn(node)
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
