from __future__ import annotations

import json

from cli.allocator_ownership import cmd_allocator_audit
from cli.parser import build_parser
from shared.allocator.ownership import audit_ownership


def status_fixture():
    return {
        "models": [{"model_id": "coder"}, {"model_id": "assistant"}],
        "plan": {
            "assignments": [
                {"node_id": "gpu-c", "model_id": "coder"},
                {"node_id": "mac-a", "model_id": "assistant"},
            ]
        },
        "nodes": [
            {
                "node_id": "gpu-c",
                "manually_managed": False,
                "residencies": [
                    {
                        "model_id": "coder",
                        "state": "ready",
                        "managed": True,
                        "runtime": "vllm",
                        "artifact_sha256": "a" * 64,
                    },
                    {
                        "model_id": "old-qwen",
                        "state": "ready",
                        "managed": False,
                        "runtime": "vllm",
                    },
                ],
            },
            {
                "node_id": "mac-a",
                "manually_managed": False,
                "residencies": [
                    {
                        "model_id": "assistant",
                        "state": "ready",
                        "managed": True,
                        "runtime": "ollama",
                    }
                ],
            },
        ],
    }


def test_audit_keeps_per_model_ownership_on_a_mixed_host():
    audit = audit_ownership(status_fixture())
    rows = {(row["model_id"], row["owner"]) for row in audit.rows}
    assert ("coder", "allocator") in rows
    assert ("old-qwen", "external") in rows
    assert audit.passed
    assert "no allocator profile" in audit.warnings[0]


def test_cutover_gate_requires_managed_ready_and_no_external_ready_route():
    failed = audit_ownership(status_fixture(), require_managed=("old-qwen", "coder"))
    assert not failed.passed
    requirements = {item["model_id"]: item for item in failed.requirements}
    assert requirements["coder"]["passed"] is True
    assert requirements["old-qwen"]["passed"] is False

    payload = status_fixture()
    payload["nodes"][0]["residencies"][1]["managed"] = True
    passed = audit_ownership(payload, require_managed=("old-qwen",))
    assert passed.passed


def test_same_model_mixed_ownership_warns_and_fails_cutover():
    payload = status_fixture()
    payload["nodes"][1]["residencies"].append(
        {"model_id": "coder", "state": "ready", "managed": False, "runtime": "ollama"}
    )
    audit = audit_ownership(payload, require_managed=("coder",))
    assert not audit.passed
    assert any("both allocator-owned and external" in item for item in audit.warnings)


def test_cli_parser_and_json_exit_gate(monkeypatch, capsys):
    parser = build_parser()
    args = parser.parse_args(
        ["allocator", "audit", "--require-managed", "old-qwen", "--json"]
    )
    monkeypatch.setattr("cli.allocator_ownership.config.select_grid", lambda _grid: object())
    monkeypatch.setattr("cli.allocator_ownership._request", lambda *_args, **_kwargs: status_fixture())
    assert args.handler is cmd_allocator_audit
    assert args.handler(args) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["passed"] is False
