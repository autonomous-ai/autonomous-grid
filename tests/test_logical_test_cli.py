from __future__ import annotations

import pytest

import cli
from cli import dispatch
from cli.logical_test import _assert_ports_available


def test_logical_test_parser_defaults_to_four_machines():
    args = cli.build_parser().parse_args(["test", "start"])

    assert args.machines == 4
    assert args.model == "SmolLM2-135M-Instruct-Q3_K_M.gguf"
    assert args.port == 22_100
    assert args.engine_port_base == 22_110
    assert args.handler is cli.cmd_test_start


def test_logical_test_parser_accepts_machine_count_and_lifecycle_commands():
    start = cli.build_parser().parse_args(["test", "start", "--machines", "7"])
    status = cli.build_parser().parse_args(["test", "status", "--json"])
    watch = cli.build_parser().parse_args(["test", "watch", "--interval", "0.1"])
    stop = cli.build_parser().parse_args(["test", "stop"])

    assert start.machines == 7
    assert status.handler is cli.cmd_test_status
    assert status.json is True
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
