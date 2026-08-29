from __future__ import annotations

import hashlib

import pytest

import cli
from cli import dispatch
from cli.logical_test import (
    _RealChatResult,
    _RealUser,
    _assert_ports_available,
    _distinct_affinity_user_ids,
    _live_replicas,
    _print_status,
    _real_chat_request,
    _real_users,
    _wait_for_competition_choice,
    workload_model_binding,
)
from shared.allocator.intelligence import anonymous_tenant_cohort


def test_logical_test_parser_defaults_to_four_machines():
    args = cli.build_parser().parse_args(["test", "start"])

    assert args.machines == 4
    assert args.model == "SmolLM2-135M-Instruct-Q3_K_M.gguf"
    assert args.portfolio_model == "SmolLM2-135M-Instruct-Q3_K_S.gguf"
    assert args.candidate_models == []
    assert args.workload_models == []
    assert args.port == 22_100
    assert args.engine_port_base == 22_110
    assert args.include_comfyui is False
    assert args.media_bundle == "z_image"
    assert args.comfyui_port == 22_200
    assert args.media_port == 22_201
    assert args.text_capacities_gib == ()
    assert args.timeout == 600.0
    assert args.handler is cli.cmd_test_start


def test_logical_status_explains_fleet_and_capacity(capsys):
    _print_status(
        {
            "running": True,
            "machines": 1,
            "endpoint": "http://127.0.0.1:22100",
            "model": "baseline",
            "portfolio_models": [],
            "include_comfyui": False,
            "log": "/tmp/grid.log",
            "mode": "automatic",
            "nodes": [
                {
                    "node_id": "node-1",
                    "state": "accepting",
                    "runtimes": ["llama.cpp"],
                    "backends": ["metal"],
                    "capacity_mb": 8_192,
                    "residencies": [],
                    "actuator_capabilities": ["load"],
                }
            ],
            "portfolio_policy": {
                "joint": True,
                "workloads": 2,
                "selected_models": ["generalist"],
            },
            "capacity_recommendations": [
                {
                    "model_id": "video",
                    "missing_replicas": 1,
                    "minimum_shape": {
                        "memory_mb": 24_576,
                        "runtimes": ["comfyui"],
                        "backends": ["cuda"],
                    },
                    "reason": "no eligible accelerator",
                }
            ],
        },
        as_json=False,
    )

    output = capsys.readouterr().out
    assert "llama.cpp/metal · 8.0 GiB · managed" in output
    assert "2 workloads jointly · models generalist" in output
    assert "video missing 1 · >=24.0 GiB · comfyui/cuda" in output


def test_competition_wait_uses_typed_choice_not_explanation_prose(monkeypatch):
    status = {
        "portfolio_projections": [
            {
                "workload": "coding",
                "chosen_model": "coder",
                "reason": "confidence-aware canary; 8.0 effective samples",
            }
        ],
        "nodes": [
            {
                "residencies": [
                    {"model_id": "coder", "state": "ready"}
                ]
            }
        ],
        "pending_commands": [],
        "history": [],
    }
    monkeypatch.setattr("cli.logical_test._status_payload", lambda _record: status)

    chosen, observed = _wait_for_competition_choice(
        {},
        candidates=("coder", "general"),
        timeout=1,
        initial={"history": []},
    )

    assert chosen == "coder"
    assert observed is status


def test_demo_fixture_users_occupy_distinct_bounded_cohorts():
    user_ids = _distinct_affinity_user_ids(4)
    cohorts = {
        anonymous_tenant_cohort(hashlib.sha256(user_id.encode()).digest())
        for user_id in user_ids
    }

    assert len(user_ids) == 4
    assert len(cohorts) == 4


def test_logical_test_parser_accepts_machine_count_and_lifecycle_commands():
    start = cli.build_parser().parse_args(["test", "start", "--machines", "7"])
    status = cli.build_parser().parse_args(["test", "status", "--json"])
    demo = cli.build_parser().parse_args(["test", "demo"])
    compete = cli.build_parser().parse_args(["test", "compete"])
    watch = cli.build_parser().parse_args(["test", "watch", "--interval", "0.1"])
    stop = cli.build_parser().parse_args(["test", "stop"])

    assert start.machines == 7
    assert status.handler is cli.cmd_test_status
    assert status.json is True
    assert demo.handler is cli.cmd_test_demo
    assert demo.requests == 12
    assert demo.users == 6
    assert demo.max_tokens == 32
    assert demo.timeout == 600.0
    assert compete.handler is cli.cmd_test_compete
    assert compete.max_tokens == 16
    assert compete.timeout == 600.0
    assert watch.handler is cli.cmd_test_watch
    assert watch.interval == 0.1
    assert stop.handler is cli.cmd_test_stop
    assert "test" in dispatch.AGNOSTIC


@pytest.mark.parametrize("value", ["0", "-1", "33", "nope"])
def test_logical_test_parser_rejects_invalid_machine_counts(value):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["test", "start", "--machines", value])


@pytest.mark.parametrize("command,flag", [("start", "--timeout"), ("watch", "--interval")])
def test_logical_test_parser_rejects_nonpositive_durations(command, flag):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["test", command, flag, "0"])


def test_logical_test_port_plan_rejects_overlap():
    with pytest.raises(SystemExit, match="overlap"):
        _assert_ports_available(22_110, 22_110, 1)


def test_logical_test_port_plan_rejects_out_of_range():
    with pytest.raises(SystemExit, match="outside"):
        _assert_ports_available(22_100, 65_530, 2)


def test_logical_test_port_plan_includes_media_ports():
    with pytest.raises(SystemExit, match="overlap"):
        _assert_ports_available(22_100, 22_110, 2, extra_ports=(22_110, 22_201))


def test_mixed_framework_parser_exposes_real_media_and_user_controls():
    start = cli.build_parser().parse_args(
        [
            "test",
            "start",
            "--machines",
            "4",
            "--include-comfyui",
            "--media-bundle",
            "z_image",
            "--comfyui-port",
            "23000",
            "--media-port",
            "23001",
            "--candidate-model",
            "coder.gguf",
            "--candidate-model",
            "general.gguf",
            "--workload-model",
            "coding=coder.gguf",
            "--workload-model",
            "research=general.gguf",
            "--text-capacities-gib",
            "3.4,5,17",
        ]
    )
    demo = cli.build_parser().parse_args(
        [
            "test",
            "demo",
            "--users",
            "9",
            "--requests",
            "18",
            "--max-tokens",
            "48",
        ]
    )

    assert start.include_comfyui is True
    assert start.media_bundle == "z_image"
    assert start.comfyui_port == 23000
    assert start.media_port == 23001
    assert start.candidate_models == ["coder.gguf", "general.gguf"]
    assert start.workload_models == [
        ("coding", "coder.gguf"),
        ("research", "general.gguf"),
    ]
    assert start.text_capacities_gib == (3.4, 5.0, 17.0)
    assert demo.users == 9
    assert demo.requests == 18
    assert demo.max_tokens == 48


def test_workload_model_binding_rejects_unknown_or_missing_values():
    assert workload_model_binding("coding=coder.gguf") == ("coding", "coder.gguf")
    with pytest.raises(Exception, match="WORKLOAD=GGUF"):
        workload_model_binding("finance=model.gguf")
    with pytest.raises(Exception, match="WORKLOAD=GGUF"):
        workload_model_binding("coding=")


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--users", "0"),
        ("--users", "65"),
        ("--requests", "0"),
        ("--requests", "1001"),
        ("--max-tokens", "0"),
        ("--max-tokens", "4097"),
    ],
)
def test_real_demo_rejects_unsafe_workload_bounds(flag, value):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["test", "demo", flag, value])


def test_real_users_span_models_and_keep_stable_affinity_ids():
    users = _real_users(6, baseline="general", specialist="code")

    assert [item.user_id for item in users] == [f"user-{index:03d}" for index in range(1, 7)]
    assert {item.model for item in users} == {"general", "code"}
    assert {item.role for item in users} == {
        "software-engineer",
        "researcher",
        "marketer",
        "sales",
        "designer",
        "operations",
    }


def test_real_client_retries_only_transient_capacity_responses(monkeypatch):
    attempts = iter((503, 429, 200))
    clock = iter((10.0, 10.1, 10.3, 10.7))

    def request_once(endpoint, user, *, max_tokens, timeout, request_headers=None):
        assert request_headers == {"X-Test": "evaluation"}
        status = next(attempts)
        return _RealChatResult(
            user_id=user.user_id,
            role=user.role,
            model=user.model,
            status_code=status,
            elapsed_seconds=0.01,
            response_id="real-id" if status == 200 else "",
            completion_tokens=1 if status == 200 else 0,
            text="ok" if status == 200 else "",
            error="" if status == 200 else "busy",
        )

    monkeypatch.setattr("cli.logical_test._real_chat_request_once", request_once)
    monkeypatch.setattr("cli.logical_test.time.sleep", lambda _: None)
    monkeypatch.setattr("cli.logical_test.time.monotonic", lambda: next(clock))

    result = _real_chat_request(
        "http://127.0.0.1:1",
        _RealUser("user-1", "engineer", "model", "prompt"),
        max_tokens=1,
        timeout=1,
        retries=3,
        request_headers={"X-Test": "evaluation"},
    )

    assert result.status_code == 200
    assert result.attempts == 3
    assert result.response_id == "real-id"
    assert result.elapsed_seconds == pytest.approx(0.7)


def test_live_replica_count_includes_transitions_but_not_cached_weights():
    payload = {
        "nodes": [
            {
                "residencies": [
                    {"model_id": "model", "state": "ready"},
                    {"model_id": "model", "state": "draining"},
                    {"model_id": "model", "state": "cached"},
                    {"model_id": "other", "state": "warming"},
                ]
            }
        ]
    }

    assert _live_replicas(payload, "model") == 2
