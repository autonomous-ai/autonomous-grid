"""Builds the full `grid` argument parser.

The command tree lives here; each command's handler lives in the per-group
module this imports from. The surface mirrors docs/cli.md.
"""
from __future__ import annotations

import argparse

from local import runtime
from shared._version import __version__

from ._constants import (
    VALID_I2V_ASPECT_RATIOS,
    VALID_I2V_DURATIONS,
    VALID_MEDIA_BUNDLES,
)
from .agent import cmd_agent_install, cmd_agent_status
from .allocator import (
    cmd_allocator_join,
    cmd_allocator_mode,
    cmd_allocator_model_remove,
    cmd_allocator_model_set,
    cmd_allocator_node_override,
    cmd_allocator_node_resume,
    cmd_allocator_node_start,
    cmd_allocator_node_status,
    cmd_allocator_node_stop,
    cmd_allocator_status,
    cmd_allocator_tick,
    cmd_allocator_token_write,
)
from .allocator_scout import (
    cmd_allocator_scout_benchmark,
    cmd_allocator_scout_run,
    cmd_allocator_scout_status,
    cmd_allocator_scout_watch,
)
from .allocator_qualification import cmd_allocator_qualify
from .allocator_ownership import cmd_allocator_audit
from shared.allocator.scenario import SCENARIO_STRATEGIES

from .allocator_scenario import (
    bounded_scenario_machines,
    bounded_scenario_models,
    bounded_scenario_users,
    cmd_test_graduate,
    cmd_test_scenario,
    graduation_machine_counts,
    graduation_seeds,
    simulated_minutes,
    workload_trace_binding,
)
from .auth import cmd_login, cmd_logout, cmd_sync
from .device import cmd_device_info
from .engine import (
    cmd_engine_install,
    cmd_engine_list,
    cmd_engine_pull,
    cmd_engine_start,
    cmd_engine_status,
    cmd_engine_stop,
)
from .grid import (
    cmd_delete,
    cmd_down,
    cmd_info,
    cmd_ls,
    cmd_overview,
    cmd_up,
    cmd_version,
)
from .credential import cmd_credential
from .launch import cmd_launch
from .logical_test import (
    cmd_test_compete,
    cmd_test_start,
    cmd_test_demo,
    cmd_test_status,
    cmd_test_stop,
    cmd_test_watch,
    logical_machine_count,
    positive_seconds,
    positive_tokens,
    positive_gib_csv,
    workload_model_binding,
    real_request_count,
    real_user_count,
)
from .allocator_resilience import (
    cmd_allocator_resilience,
    nonnegative_frequency,
    positive_interval,
    resilience_hours,
)
from .mode import cmd_mode, cmd_use
from .models import cmd_catalog, cmd_ctx, cmd_pull, cmd_rm
from . import project_arg
from .provider import cmd_engines, cmd_join, cmd_leave, cmd_models
from .remote_grid import cmd_remote_members
from .remote_price import cmd_remote_price
from .remote_project import cmd_remote_project
from .remote_task import cmd_remote_task
from .remote_router import (
    MAX_ADVISORS,
    AdvisorsAction,
    cmd_remote_router,
    parse_advisor_token,
)
from .request import cmd_chat, cmd_edit, cmd_image, cmd_video
from .stt import cmd_stt_transcribe


def _positive_task_count(raw: str) -> int:
    """`--max-tasks`, refused rather than defaulted when it is not a positive whole number.

    ⚠️ **Deliberately NOT `GRID_MAX_TASKS`'s rule, and the difference is the point.** That variable
    falls back to 1 and says so, because refusing would take task serving down for the life of a
    running process — a far worse answer than running with the default. A flag is a different
    situation: the operator is at the terminal, they typed it a second ago, and being told costs
    them one retry. `argparse` turns this into exit 2, which is "ask again" rather than "done".
    """
    try:
        count = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a whole number of tasks") from None
    if count < 1:
        raise argparse.ArgumentTypeError(f"{raw!r} must be at least 1 task")
    return count


def _positive_concurrency(raw: str) -> int:
    """Bound one explicitly advertised engine width before it reaches either control plane."""

    try:
        count = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a whole concurrency value") from None
    if not 1 <= count <= 256:
        raise argparse.ArgumentTypeError(f"{raw!r} must be between 1 and 256")
    return count


def _positive_gpu_memory_mb(raw: str) -> int:
    try:
        memory_mb = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a whole MB value") from None
    if not 1 <= memory_mb <= 1_000_000_000:
        raise argparse.ArgumentTypeError(
            f"{raw!r} must be between 1 and 1000000000 MB"
        )
    return memory_mb


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="grid",
        description=(
            "Grid: one private OpenAI endpoint for the engines you already run. "
            "Use --local/--remote before any command to override the active mode for that one run."
        ),
    )
    # ⚠ Every global flag here must be **valueless** (`store_true`/`version`). `cli.dispatch.
    # split_forwarded` finds the subcommand as the first non-option token, so a global flag that took
    # a separate value would make that value look like the command word — `grid launch claude -- -p`
    # would stop being recognised as a passthrough and argparse would bind `-p` to the `grid`
    # positional again, which is the exact bug that function exists to prevent. A global flag that
    # needs a value must be spelled `--flag=value`, or `split_forwarded` must learn about it.
    parser.add_argument("--version", action="version", version=f"grid {__version__}")
    parser.add_argument(
        "--json",
        action="store_true",
        help="With no command, print the overview as JSON. (For subcommands, pass --json after the command.)",
    )
    parser.set_defaults(handler=cmd_overview)
    sub = parser.add_subparsers(dest="command", metavar="<command>", required=False)

    version = sub.add_parser("version", help="Print the grid version")
    version.set_defaults(handler=cmd_version)

    _add_grid_lifecycle(sub)
    _add_engines(sub)
    _add_models(sub)
    _add_use(sub)
    _add_state(sub)
    _add_auth(sub)
    _add_members(sub)
    _add_price(sub)
    _add_project(sub)
    _add_task(sub)
    _add_router(sub)
    _add_engine_setup(sub)
    _add_launch(sub)
    _add_allocator(sub)
    _add_logical_test(sub)
    _add_train(sub)
    _add_credential(sub)
    _add_stt(sub)

    return parser


def _add_logical_test(sub) -> None:
    test = sub.add_parser(
        "test",
        help="Run an isolated logical-machine Grid on this computer",
    )
    test_sub = test.add_subparsers(dest="test_command", required=True)

    start = test_sub.add_parser("start", help="Start a persistent logical test Grid")
    start.add_argument(
        "--machines",
        type=logical_machine_count,
        default=4,
        metavar="N",
        help=f"Number of logical machines (default 4; maximum {32}).",
    )
    start.add_argument(
        "--model",
        default="SmolLM2-135M-Instruct-Q3_K_M.gguf",
        help="Cached baseline GGUF filename to load on every logical machine.",
    )
    start.add_argument(
        "--portfolio-model",
        default="SmolLM2-135M-Instruct-Q3_K_S.gguf",
        help="Cached GGUF the autonomous workload demo may proactively load.",
    )
    start.add_argument(
        "--candidate-model",
        dest="candidate_models",
        action="append",
        default=[],
        metavar="GGUF",
        help="Additional cached GGUF candidate for real portfolio competition; repeatable.",
    )
    start.add_argument(
        "--workload-model",
        dest="workload_models",
        action="append",
        type=workload_model_binding,
        default=[],
        metavar="WORKLOAD=GGUF",
        help=(
            "Bind a real workload capability to a cached GGUF for autonomous portfolio "
            "selection; repeatable (for example coding=qwen-coder.gguf)."
        ),
    )
    start.add_argument("--port", type=int, default=22_100, help="Grid control/API port.")
    start.add_argument(
        "--engine-port-base",
        type=int,
        default=22_110,
        help="Beginning of the non-overlapping logical llama.cpp port ranges.",
    )
    start.add_argument(
        "--timeout",
        type=positive_seconds,
        default=600.0,
        help="Seconds to wait for every real text/media engine to become ready.",
    )
    start.add_argument(
        "--include-comfyui",
        action="store_true",
        help=(
            "Use one of the N logical machines for a real ComfyUI/PyTorch media engine; "
            "the remaining machines run llama.cpp."
        ),
    )
    start.add_argument(
        "--media-bundle",
        choices=("image_generation", "z_image"),
        default="z_image",
        help="Installed ComfyUI bundle used by the real media node (default z_image).",
    )
    start.add_argument(
        "--comfyui-port",
        type=int,
        default=22_200,
        help="Loopback ComfyUI port for the mixed-framework test Grid.",
    )
    start.add_argument(
        "--media-port",
        type=int,
        default=22_201,
        help="Loopback Grid media-adapter port for the mixed-framework test Grid.",
    )
    start.add_argument(
        "--text-capacities-gib",
        type=positive_gib_csv,
        default=(),
        metavar="GIB,...",
        help=(
            "Logical usable-memory totals for text nodes; count must equal the number of text "
            "nodes and the physical total remains enforced."
        ),
    )
    start.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    start.set_defaults(handler=cmd_test_start)

    status = test_sub.add_parser("status", help="Show logical hosts and ready replicas")
    status.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    status.set_defaults(handler=cmd_test_status)

    demo = test_sub.add_parser(
        "demo",
        help="Explain and exercise demand-driven scale-up and scale-down",
    )
    demo.add_argument(
        "--requests",
        type=real_request_count,
        default=12,
        metavar="N",
        help="Total real concurrent chat requests in the multi-user phase (default 12).",
    )
    demo.add_argument(
        "--users",
        type=real_user_count,
        default=6,
        metavar="N",
        help="Distinct real client personas issuing inference requests (default 6).",
    )
    demo.add_argument(
        "--max-tokens",
        type=positive_tokens,
        default=32,
        metavar="N",
        help="Maximum generated tokens for each real chat request (default 32).",
    )
    demo.add_argument(
        "--timeout",
        type=positive_seconds,
        default=600.0,
        help="Seconds allowed for each real placement or ComfyUI generation transition.",
    )
    demo.set_defaults(handler=cmd_test_demo)

    compete = test_sub.add_parser(
        "compete",
        help="Benchmark real candidate models and prove quality/cost-aware selection",
    )
    compete.add_argument(
        "--max-tokens",
        type=positive_tokens,
        default=16,
        metavar="N",
        help="Maximum generated tokens per deterministic benchmark task (default 16).",
    )
    compete.add_argument(
        "--timeout",
        type=positive_seconds,
        default=600.0,
        help="Seconds allowed for each real model lifecycle transition.",
    )
    compete.set_defaults(handler=cmd_test_compete)

    scenario = test_sub.add_parser(
        "scenario",
        help="Simulate heterogeneous machines, models, users, demand shifts, and failures",
    )
    scenario.add_argument(
        "--machines",
        type=bounded_scenario_machines,
        default=8,
        metavar="N",
        help="Modeled heterogeneous logical machines (default 8; maximum 64).",
    )
    scenario.add_argument(
        "--models",
        type=bounded_scenario_models,
        default=8,
        metavar="N",
        help="Configured model profiles (default 8; maximum 32).",
    )
    scenario.add_argument(
        "--users",
        type=bounded_scenario_users,
        default=50,
        metavar="N",
        help="Concurrent user personas (default 50; maximum 10000).",
    )
    scenario.add_argument(
        "--duration",
        type=simulated_minutes,
        default=30,
        metavar="TIME",
        help="Simulated time as minutes or hours, e.g. 30m or 2h (default 30m).",
    )
    scenario.add_argument("--seed", type=int, default=42, help="Deterministic random seed.")
    scenario.add_argument(
        "--strategy",
        choices=SCENARIO_STRATEGIES,
        default="smart",
        help=(
            "Allocation policy to evaluate: smart, reactive, greedy, or static "
            "(default smart)."
        ),
    )
    scenario.add_argument(
        "--workload-trace",
        action="append",
        default=[],
        type=workload_trace_binding,
        metavar="WORKLOAD=CSV",
        help=(
            "Replay a headerless timestamp,request-rate CSV for one workload; repeat for "
            "multiple workloads."
        ),
    )
    scenario.add_argument(
        "--timeline",
        action="store_true",
        help="Print every placement-changing tick instead of notable events only.",
    )
    scenario.add_argument(
        "--oracle",
        action="store_true",
        help=(
            "Exhaustively benchmark the observed trace on up to 4 machines and 9 models."
        ),
    )
    scenario.add_argument("--json", action="store_true", help="Emit the complete JSON report.")
    scenario.set_defaults(handler=cmd_test_scenario)

    graduate = test_sub.add_parser(
        "graduate",
        help="Compare the smart allocator with fixed and reactive baselines",
    )
    graduate.add_argument(
        "--machines",
        type=graduation_machine_counts,
        default=(2, 4, 8),
        metavar="N,N,...",
        help="Comma-separated logical fleet sizes (default 2,4,8).",
    )
    graduate.add_argument(
        "--seeds",
        type=graduation_seeds,
        default=(42, 144),
        metavar="N,N,...",
        help="Comma-separated deterministic trace seeds (default 42,144).",
    )
    graduate.add_argument(
        "--models",
        type=bounded_scenario_models,
        default=8,
        metavar="N",
        help="Configured model profiles (default 8; maximum 32).",
    )
    graduate.add_argument(
        "--users",
        type=bounded_scenario_users,
        default=50,
        metavar="N",
        help="Concurrent user personas (default 50; maximum 10000).",
    )
    graduate.add_argument(
        "--duration",
        type=simulated_minutes,
        default=120,
        metavar="TIME",
        help="Simulated duration per run (default 120m).",
    )
    graduate.add_argument("--json", action="store_true", help="Emit the complete JSON report.")
    graduate.set_defaults(handler=cmd_test_graduate)

    watch = test_sub.add_parser(
        "watch",
        help="Follow model placement and allocator action transitions",
    )
    watch.add_argument(
        "--interval",
        type=positive_seconds,
        default=0.5,
        help="Seconds between status polls (default 0.5).",
    )
    watch.set_defaults(handler=cmd_test_watch)

    stop = test_sub.add_parser("stop", help="Drain and stop the logical test Grid")
    stop.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    stop.set_defaults(handler=cmd_test_stop)


def _add_allocator(sub) -> None:
    allocator = sub.add_parser(
        "allocator",
        help="Place configured models dynamically across participating hosts",
    )
    allocator_sub = allocator.add_subparsers(dest="allocator_command", required=True)

    resilience = allocator_sub.add_parser(
        "resilience",
        help="Qualify controller failover, relay outages, partitions, and long-running safety",
    )
    resilience.add_argument(
        "--duration",
        type=resilience_hours,
        default=72.0,
        metavar="TIME",
        help="Soak duration such as 72h or 3d (default 72h).",
    )
    resilience.add_argument(
        "--interval",
        type=positive_interval,
        default=300.0,
        metavar="SECONDS",
        help="Logical observation interval (default 300 seconds).",
    )
    resilience.add_argument(
        "--wall-clock",
        action="store_true",
        help="Sleep between cycles for a real elapsed-time soak; default runs accelerated.",
    )
    resilience.add_argument("--seed", type=int, default=42)
    resilience.add_argument(
        "--node-partition-every",
        type=nonnegative_frequency,
        default=17,
        metavar="N",
        help="Omit one node heartbeat every N cycles; 0 disables (default 17).",
    )
    resilience.add_argument(
        "--relay-outage-every",
        type=nonnegative_frequency,
        default=29,
        metavar="N",
        help="Suppress the relay observation every N cycles; 0 disables (default 29).",
    )
    resilience.add_argument(
        "--controller-failover-every",
        type=nonnegative_frequency,
        default=43,
        metavar="N",
        help="Replace the controller leader every N cycles; 0 disables (default 43).",
    )
    resilience.add_argument("--state-dir", default="", metavar="PATH")
    resilience.add_argument("--resume", action="store_true")
    resilience.add_argument("--quiet", action="store_true")
    resilience.add_argument("--json", action="store_true")
    resilience.set_defaults(handler=cmd_allocator_resilience)

    audit = allocator_sub.add_parser(
        "audit",
        help="Show per-model lifecycle ownership and enforce migration cutover gates",
    )
    _add_allocator_grid(audit)
    audit.add_argument(
        "--require-managed",
        action="append",
        default=[],
        metavar="MODEL",
        help="Fail unless MODEL has managed ready routes and no external ready routes; repeatable.",
    )
    audit.add_argument("--json", action="store_true")
    audit.set_defaults(handler=cmd_allocator_audit)

    allocator_join = allocator_sub.add_parser(
        "join",
        help="Enroll this already-joined remote provider as allocator-managed capacity",
    )
    allocator_join.add_argument("grid", nargs="?", default=None)
    allocator_join.add_argument("--heartbeat-interval", type=float, default=15.0)
    allocator_join.add_argument(
        "--dedicated",
        action="store_true",
        help="Treat this host as dedicated Grid capacity while retaining hardware safety limits.",
    )
    allocator_join.add_argument(
        "--restart",
        action="store_true",
        help="Gracefully replace this provider's running allocator node with the current build.",
    )
    allocator_join.set_defaults(handler=cmd_allocator_join)

    status = allocator_sub.add_parser("status", help="Show demand, placement, and mutations")
    _add_allocator_grid(status)
    status.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    status.set_defaults(handler=cmd_allocator_status)

    model = allocator_sub.add_parser("model", help="Create or remove a model placement profile")
    model_sub = model.add_subparsers(dest="allocator_model_command", required=True)
    set_model = model_sub.add_parser("set", help="Create or replace a model placement profile")
    set_model.add_argument("model", help="Advertised model id; managed llama.cpp uses a cached GGUF filename.")
    set_model.add_argument("--memory-mb", type=int, required=True)
    set_model.add_argument(
        "--artifact-sha256",
        default="",
        metavar="HEX",
        help="Require this immutable GGUF SHA-256 on managed replicas.",
    )
    set_model.add_argument(
        "--artifact-source",
        default="",
        metavar="URI",
        help=(
            "Authenticated immutable source for autonomous loading; managed llama.cpp accepts "
            "an exact hf://owner/repo[@commit]/path.gguf URI."
        ),
    )
    set_model.add_argument(
        "--artifact-size-mb",
        type=int,
        default=0,
        metavar="MB",
        help="Maximum transfer size and required free disk for autonomous loading.",
    )
    set_model.add_argument(
        "--runtime-memory-mb",
        action="append",
        default=[],
        metavar="RUNTIME=MB",
        help="Runtime-specific memory estimate; repeat for multiple runtimes.",
    )
    set_model.add_argument(
        "--workload-score",
        action="append",
        default=[],
        metavar="WORKLOAD=SCORE",
        help=(
            "Capability score in (0, 1] for an allocator workload such as coding, research, "
            "design, image, or video; repeat for multiple workloads."
        ),
    )
    set_model.add_argument(
        "--runtime",
        action="append",
        dest="runtimes",
        default=None,
        metavar="NAME",
        help="Compatible runtime; repeat for multiple values (default: llama.cpp when omitted).",
    )
    set_model.add_argument("--backend", action="append", dest="backends", default=[])
    set_model.add_argument("--data-tier", default="internal")
    set_model.add_argument("--required-tag", action="append", dest="required_tags", default=[])
    set_model.add_argument("--forbidden-tag", action="append", dest="forbidden_tags", default=[])
    set_model.add_argument("--pin", action="append", dest="pinned_nodes", default=[])
    set_model.add_argument("--min-replicas", type=int, default=1)
    set_model.add_argument("--max-replicas", type=int, default=None)
    set_model.add_argument("--target-utilization", type=float, default=0.70)
    set_model.add_argument(
        "--replica-concurrency",
        type=int,
        default=1,
        help="Conservative request slots per newly placed replica (default: 1).",
    )
    set_model.add_argument("--service-seconds", type=float, default=5.0)
    set_model.add_argument("--latency-slo-ms", type=float, default=5_000.0)
    set_model.add_argument("--priority", type=int, default=100)
    set_model.add_argument("--load-seconds", type=float, default=30.0)
    set_model.add_argument("--warm-seconds", type=float, default=5.0)
    set_model.add_argument("--min-residency-seconds", type=float, default=300.0)
    set_model.add_argument("--scale-down-cooldown-seconds", type=float, default=900.0)
    set_model.add_argument("--min-failure-domains", type=int, default=1)
    set_model.add_argument(
        "--min-gpu-count",
        type=int,
        default=0,
        help="Require at least this many GPUs on a placement target.",
    )
    set_model.add_argument(
        "--min-gpu-memory-mb",
        type=int,
        default=0,
        help="Require each needed GPU to have at least this much physical VRAM.",
    )
    set_model.add_argument(
        "--min-gpu-interconnect-gbps",
        type=float,
        default=0.0,
        help="Require this all-peer GPU fabric bandwidth for a sharded placement.",
    )
    set_model.add_argument(
        "--single-numa-node",
        action="store_true",
        help="Require every GPU shard to share one known NUMA node.",
    )
    set_model.add_argument(
        "--disallow-mig",
        action="store_true",
        help="Reject MIG compute instances for this model.",
    )
    set_model.add_argument(
        "--max-colocated-models",
        type=int,
        default=0,
        metavar="N",
        help="Limit distinct live models per host; 1 requests exclusive serving (default: unlimited).",
    )
    set_model.add_argument(
        "--colocation-exclude",
        action="append",
        dest="colocation_excludes",
        default=[],
        metavar="MODEL",
        help="Forbid sharing a host with this model; repeat for multiple pairwise exclusions.",
    )
    _add_allocator_grid(set_model, token=True)
    set_model.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    set_model.set_defaults(handler=cmd_allocator_model_set)

    for verb in ("remove", "rm"):
        remove = model_sub.add_parser(
            verb,
            help="Retire a model and safely drain its managed replicas",
        )
        remove.add_argument("model")
        _add_allocator_grid(remove, token=True)
        remove.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
        remove.set_defaults(handler=cmd_allocator_model_remove)

    mode = allocator_sub.add_parser("mode", help="Select observe, recommend, or automatic")
    mode.add_argument("allocator_mode", choices=("observe", "recommend", "automatic"))
    _add_allocator_grid(mode, token=True)
    mode.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    mode.set_defaults(handler=cmd_allocator_mode)

    tick = allocator_sub.add_parser("tick", help="Run an immediate allocation pass")
    _add_allocator_grid(tick, token=True)
    tick.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    tick.set_defaults(handler=cmd_allocator_tick)

    scout = allocator_sub.add_parser(
        "scout",
        help="Discover immutable open-weight releases and qualify real canaries",
    )
    scout_sub = scout.add_subparsers(dest="allocator_scout_command", required=True)

    def add_scout_discovery_options(command) -> None:
        command.add_argument("--search", default="", help="Optional Hub model search text.")
        command.add_argument("--workload", action="append", dest="workloads", default=[])
        command.add_argument(
            "--runtime", action="append", dest="runtimes", choices=("llama.cpp", "vllm"), default=[]
        )
        command.add_argument("--author", action="append", dest="authors", default=[])
        command.add_argument("--license", action="append", dest="licenses", default=[])
        command.add_argument("--quantization", action="append", dest="quantizations", default=[])
        command.add_argument("--limit", type=int, default=30)
        command.add_argument("--inspect", type=int, default=12)
        command.add_argument("--max-artifact-size-mb", type=int, default=100_000)
        command.add_argument("--min-downloads", type=int, default=0)
        command.add_argument("--hub-url", default="https://huggingface.co")
        command.add_argument("--state-file", default=None)
        _add_allocator_grid(command)
        command.add_argument("--json", action="store_true")

    scout_run = scout_sub.add_parser(
        "run", help="Run one discovery and fleet-fit proposal cycle"
    )
    add_scout_discovery_options(scout_run)
    scout_run.set_defaults(handler=cmd_allocator_scout_run)

    scout_watch = scout_sub.add_parser(
        "watch", help="Continuously refresh discovery and proposals"
    )
    add_scout_discovery_options(scout_watch)
    scout_watch.add_argument("--interval", type=float, default=21_600.0)
    scout_watch.add_argument(
        "--max-cycles", type=int, default=0, help="Stop after N cycles; 0 runs until interrupted."
    )
    scout_watch.set_defaults(handler=cmd_allocator_scout_watch)

    scout_status = scout_sub.add_parser("status", help="Show persisted scout proposals")
    scout_status.add_argument("--state-file", default=None)
    _add_allocator_grid(scout_status)
    scout_status.add_argument("--json", action="store_true")
    scout_status.set_defaults(handler=cmd_allocator_scout_status)

    scout_benchmark = scout_sub.add_parser(
        "benchmark", help="Deploy one bounded canary and record real evaluation evidence"
    )
    scout_benchmark.add_argument("proposal")
    scout_benchmark.add_argument("--inference-grid", required=True)
    scout_benchmark.add_argument("--workload", action="append", dest="workloads", default=[])
    scout_benchmark.add_argument("--deploy-canary", action="store_true")
    scout_benchmark.add_argument("--startup-timeout", type=float, default=900.0)
    scout_benchmark.add_argument("--request-timeout", type=float, default=120.0)
    scout_benchmark.add_argument("--state-file", default=None)
    _add_allocator_grid(scout_benchmark, token=True)
    scout_benchmark.add_argument("--json", action="store_true")
    scout_benchmark.set_defaults(handler=cmd_allocator_scout_benchmark)

    qualify = allocator_sub.add_parser(
        "qualify",
        help="Prove a physical engine's full managed lifecycle with real inference",
    )
    qualify.add_argument("runtime", choices=("ollama", "comfyui", "vllm"))
    qualify.add_argument("model")
    qualify.add_argument(
        "--endpoint",
        default="",
        help="Native engine base URL (defaults by runtime).",
    )
    qualify.add_argument("--artifact-source", default="")
    qualify.add_argument("--artifact-sha256", default="")
    qualify.add_argument("--artifact-size-mb", type=int, default=0)
    qualify.add_argument("--port", type=int, default=28901)
    qualify.add_argument("--tensor-parallel-size", type=int, default=1)
    qualify.add_argument("--cache-dir", default=None)
    qualify.add_argument("--prompt", default="Reply with exactly GRID.")
    qualify.add_argument("--max-tokens", type=int, default=32)
    qualify.add_argument("--image-size", type=int, default=256)
    qualify.add_argument("--steps", type=int, default=1)
    qualify.add_argument("--timeout", type=float, default=900.0)
    qualify.add_argument("--cleanup-artifact", action="store_true")
    qualify.add_argument("--report", default=None)
    qualify.add_argument("--json", action="store_true")
    qualify.set_defaults(handler=cmd_allocator_qualify)

    token = allocator_sub.add_parser("token", help="Provision the node control capability")
    token_sub = token.add_subparsers(dest="allocator_token_command", required=True)
    token_write = token_sub.add_parser(
        "write",
        help="Write the control token to an owner-only file for secure node provisioning",
    )
    token_write.add_argument("path")
    token_write.add_argument("--force", action="store_true")
    token_write.add_argument(
        "--host-id",
        default=None,
        help="Stable host identity to authorize (a new identity is generated if omitted).",
    )
    token_write.add_argument(
        "--ttl-days",
        type=int,
        default=365,
        help="Credential lifetime in days (default: 365).",
    )
    _add_allocator_grid(token_write, token=True)
    token_write.set_defaults(handler=cmd_allocator_token_write)

    node = allocator_sub.add_parser("node", help="Manage this machine's allocator node loop")
    node_sub = node.add_subparsers(dest="allocator_node_command", required=True)
    node_start = node_sub.add_parser("start", help="Join this machine as managed capacity")
    _add_allocator_grid(node_start, node_token=True)
    node_start.add_argument("--heartbeat-interval", type=float, default=15.0)
    node_start.add_argument(
        "--provider-grid",
        default=None,
        help=(
            "Publish allocator-owned models through this already-joined remote Grid identity "
            "using zero-drop hot reload."
        ),
    )
    node_start.add_argument(
        "--dedicated",
        action="store_true",
        help=(
            "Treat this host as dedicated Grid capacity: ignore desktop activity and ordinary "
            "CPU-load throttling while retaining thermal, memory, battery, disk, and network safety."
        ),
    )
    node_start.add_argument("--advertise-host", default=None)
    node_start.add_argument(
        "--engine-tls-cert",
        default=None,
        help="PEM certificate served by managed llama.cpp endpoints.",
    )
    node_start.add_argument(
        "--engine-tls-key",
        default=None,
        help="Owner-only PEM private key paired with --engine-tls-cert.",
    )
    node_start.add_argument(
        "--engine-tls-ca",
        default=None,
        help="PEM CA bundle Grid should trust for the managed endpoint.",
    )
    node_start.add_argument(
        "--allow-insecure-http",
        action="store_true",
        help=(
            "Accepted for compatibility only; managed node and engine credentials still require "
            "loopback HTTP or HTTPS."
        ),
    )
    node_start.set_defaults(handler=cmd_allocator_node_start)

    node_stop = node_sub.add_parser("stop", help="Stop this machine's managed node loop")
    _add_allocator_grid(node_stop)
    node_stop.set_defaults(handler=cmd_allocator_node_stop)

    node_status = node_sub.add_parser("status", help="Show this machine's managed node loop")
    _add_allocator_grid(node_status)
    node_status.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    node_status.set_defaults(handler=cmd_allocator_node_status)

    for verb in ("drain", "pause", "quarantine"):
        override = node_sub.add_parser(
            verb,
            help=f"Apply a durable local {verb} override",
        )
        _add_allocator_grid(override)
        override.add_argument("--reason", default=f"local {verb}")
        override.add_argument(
            "--for-seconds",
            type=float,
            default=None,
            help="Expire the override automatically after this duration.",
        )
        override.set_defaults(
            handler=cmd_allocator_node_override,
            override_state=verb,
        )

    resume = node_sub.add_parser("resume", help="Clear the local override and resume normal policy")
    _add_allocator_grid(resume)
    resume.set_defaults(handler=cmd_allocator_node_resume)


def _add_allocator_grid(
    parser,
    *,
    token: bool = False,
    node_token: bool = False,
) -> None:
    parser.add_argument("--grid", default=None, help="Grid name, id, or local signaling URL.")
    if token or node_token:
        parser.add_argument(
            "--token-file",
            default=None,
            help=(
                "Read a host-scoped node credential from this file "
                "(or set GRID_ALLOCATOR_NODE_TOKEN)."
                if node_token
                else "Read the allocator operator token from this file "
                "(or set GRID_ALLOCATOR_CONTROL_TOKEN)."
            ),
        )
    if token:
        parser.add_argument(
            "--allow-insecure-http",
            action="store_true",
            help="Permit operator credentials over non-loopback HTTP (trusted LANs only).",
        )


def _add_credential(sub) -> None:
    """`grid credential <operation>` — git's credential-helper protocol (ADR 0033 D-h, issue 17).

    Not a command anybody types: `grid project clone` writes it into the clone's `.git/config` and
    git runs it on every operation against that relay.

    The operation is a free-form positional rather than a `choices=` list, and that is the protocol
    speaking, not laxity — `gitcredentials(7)` says a helper receiving an operation it does not
    know "should silently ignore the request. This leaves room for future operations to be added
    (older helpers will just ignore the new requests)." `choices=` would make a future git verb an
    argparse usage error with exit code 2, which git reports as a broken helper.
    """
    credential = sub.add_parser(
        "credential",
        help="git credential helper (used by `grid project clone`; not typed by hand)",
        description=(
            "Answer git's credential-helper protocol on stdin/stdout.\n\n"
            "`grid project clone` configures this in the clone's own `.git/config`, scoped to that\n"
            "grid's relay, so no token is ever written to disk and a refreshed one is picked up\n"
            "automatically. Reads only the local credential store — never the network."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    credential.add_argument("operation", help="get, store or erase — git supplies this.")
    credential.add_argument(
        "--grid", default=None,
        help="Grid whose credential this clone uses. Written by `grid project clone`.")
    credential.set_defaults(handler=cmd_credential)


def _add_grid_lifecycle(sub) -> None:
    # `start`/`stop` beside `up`/`down`. The command names are the vocabulary a reader learns, and
    # two metaphors is one too many: a grid went "up" while a computer "joined" it, so nothing
    # paired with `grid leave`. Now the grid **starts** and **stops**, computers **join** and
    # **leave**, and the docs can say one thing. `up`/`down` keep working — every existing script,
    # every older README and every muscle memory still resolves.
    for _verb, _summary in (("up", "Start a grid (creates it on first run; default: home)"),
                            ("start", "Start a grid — same as `grid up`")):
        _build_up_parser(sub, _verb, _summary)


    for _verb, _summary in (("down", "Stop a grid (its setup is kept)"),
                            ("stop", "Stop a grid — same as `grid down`")):
        _d = sub.add_parser(_verb, help=_summary)
        _d.add_argument("name", nargs="?", default=None,
                        help="Grid name or id (ag-…). Omit for the active grid.")
        _d.set_defaults(handler=cmd_down)

    delete = sub.add_parser(
        "delete", help="Delete a grid's local config for good (`grid stop` only pauses it)"
    )
    delete.add_argument("name", nargs="?", default=None,
                        help="Grid name or id (ag-…). Omit for the active grid.")
    delete.add_argument("--yes", action="store_true", help="Skip confirmation.")
    delete.set_defaults(handler=cmd_delete)

    ls = sub.add_parser("ls", help="List your grids")
    ls.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    ls.set_defaults(handler=cmd_ls)

    list_alias = sub.add_parser("list", help="Alias for `grid ls`")
    list_alias.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    list_alias.set_defaults(handler=cmd_ls)

    info = sub.add_parser("info", help="Endpoint, key, and live models for a grid")
    info.add_argument("grid", nargs="?", default=None,
                      help="Grid name or id (ag-…). Omit for the active grid.")
    info.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    info.add_argument("--env", action="store_true", help="Print OPENAI_* shell exports.")
    info.set_defaults(handler=cmd_info)


def _build_up_parser(sub, verb: str, summary: str):
    """One definition, two names — see the note at the call site."""
    parser = sub.add_parser(verb, help=summary)
    parser.add_argument("name", nargs="?", default=None,
                        help="Grid name or id (ag-…). Omit for 'home'.")
    # `default=None`, not the real defaults: these three are also settings of a grid that already
    # exists, and `cmd_up` can only apply the ones the operator actually typed if "not given" is
    # distinguishable from "given the default value". Falling back to DEFAULT_* happens at use.
    parser.add_argument("--port", type=int, default=None,
                        help=f"Port to listen on (default {runtime.DEFAULT_PORT}). On a grid that "
                             "already exists this changes its port.")
    parser.add_argument("--host", default=None,
                        help=f"Address to bind (default {runtime.DEFAULT_HOST}).")
    parser.add_argument("--advertise-host", default=None,
                        help="Address to hand out to other computers, when the one Grid picks is "
                             "not reachable. Use 127.0.0.1 to keep everything on this machine.")
    # Remote-only (local cmd_up ignores it): the network type set when `grid up` creates a remote
    # grid. default=None lets the remote handler tell an explicit value from this create default.
    parser.add_argument(
        "--type",
        choices=("permissioned-public", "permissioned-providers"),
        default=None,
        help="Remote grid network type, set on create (default permissioned-public).",
    )
    parser.set_defaults(handler=cmd_up)
    return parser


def _add_engines(sub) -> None:
    join = sub.add_parser("join", help="Join an engine to a grid")
    join.add_argument("grid", nargs="?", default=None,
                      help="Grid name or id (ag-…). Omit for the active grid.")

    choose = join.add_argument_group("Choose an engine")
    choose.add_argument("-m", "--model", action="append", dest="models", default=[],
                        help="A model an engine serves; pair with --at, or use --serve for the built-in.")
    choose.add_argument("--at", default=None, help="URL of an existing OpenAI-compatible engine.")
    choose.add_argument("--serve", default=None, help="Start the built-in engine for this model, then join.")
    choose.add_argument("--media", action="store_true", help="Join this box as a media (ComfyUI) engine.")
    choose.add_argument(
        "--bundle",
        action="append",
        dest="bundles",
        choices=VALID_MEDIA_BUNDLES,
        default=[],
        help="Media bundle to advertise; repeat for multiple bundles.",
    )
    choose.add_argument("--all", action="store_true", help="Join every detected engine.")
    choose.add_argument("--kind", "--engine", dest="kind", default=None,
                        help="Join only the detected engine of this kind (e.g. ollama, vllm); "
                             "with --at, record that runtime kind for placement.")
    choose.add_argument(
        "--max-concurrency",
        type=_positive_concurrency,
        default=None,
        metavar="N",
        help="Maximum simultaneous requests admitted to this engine (default: 1).",
    )
    choose.add_argument(
        "--gpu-count",
        type=_positive_concurrency,
        default=None,
        metavar="N",
        help="Physical GPU count reported as allocator topology evidence (local mode).",
    )
    choose.add_argument(
        "--gpu-memory-mb",
        action="append",
        type=_positive_gpu_memory_mb,
        default=[],
        metavar="MB",
        help=(
            "Physical VRAM per GPU; repeat for heterogeneous devices, or combine one value "
            "with --gpu-count for homogeneous GPUs (local mode)."
        ),
    )
    choose.add_argument(
        "--api",
        metavar="KIND",
        default=None,
        help="Join a third-party API engine of this service kind (e.g. openai, codex). "
             "Remote only; -m optionally narrows the whitelist (see `grid catalog --api`), "
             "omitted = every whitelisted model the credential can serve. "
             "A CLI-seat kind (e.g. claude) serves a coding CLI installed on this box "
             "and works in local mode too.",
    )
    choose.add_argument(
        "--no-browser",
        action="store_true",
        help="For `--api codex` on a headless machine: print the sign-in URL instead of opening a "
             "browser, and take the redirect URL back by paste.",
    )
    choose.add_argument(
        "--api-key",
        default=None,
        help="API key for the --api engine. Overrides env var and key store. "
             "Warning: visible in shell history; prefer exporting the env var.",
    )

    naming = join.add_argument_group("Name & display")
    naming.add_argument("--name", default=None,
                        help="Local: engine id. Remote: display name shown on the grid page.")
    naming.add_argument(
        "--advertise-as",
        action="append",
        dest="advertise_as",
        default=[],
        help="Model name advertised to the grid. Repeat once per -m/--model.",
    )

    tuning = join.add_argument_group("Built-in tuning (--serve)")
    tuning.add_argument("--endpoint-port", "--llama-port", type=int, default=8081)
    tuning.add_argument("--heartbeat-interval", type=float, default=15.0)
    tuning.add_argument("--ctx-size", type=int, default=None, metavar="N",
                        help="Pin the context window to N tokens. Left unset, the engine measures "
                             "free memory at load and takes the largest window that fits — pinning "
                             "turns that off, so an N this machine cannot hold fails to start "
                             "instead of shrinking. N=0 is not 'unset': it demands the model's "
                             "full trained window and spills weights into system RAM to get it.")
    tuning.add_argument("--n-predict", type=int, default=None)
    tuning.add_argument("--parallel", type=int, default=None)
    tuning.add_argument("--flash-attn", default=None, metavar="on|off|auto",
                        help="Left unset, the engine probes the backend and falls back on its own.")
    tuning.add_argument("--temp", type=float, default=None)
    tuning.add_argument("--reasoning-budget", type=int, default=None)
    tuning.add_argument("--mmproj", default=None, metavar="FILE",
                        help="Override the multimodal projector, naming a FILE in ~/.grid/models. "
                             "Not normally needed: `grid pull` fetches a vision model's projector "
                             "with it, and `--serve` finds it and enables vision on its own.")

    media = join.add_argument_group("Media")
    media.add_argument("--comfyui-port", type=int, default=8188)
    media.add_argument("--media-port", type=int, default=8190)

    seat = join.add_argument_group("CLI seat (--api claude, …)")
    seat.add_argument("--seat-port", type=int, default=None,
                      help="Loopback port for the seat server (default: the kind's own).")
    seat.add_argument("--seat-timeout", type=float, default=None,
                      help="Seconds one CLI run may take before it is abandoned (default 600).")
    seat.add_argument("--seat-concurrency", type=int, default=None,
                      help="Concurrent CLI processes (default 1 — each request is a whole process "
                           "racing one subscription's rate limit).")
    seat.add_argument("--seat-session-limit", type=int, default=None, metavar="PCT",
                      help="Stop serving once the short-window usage reaches this percent.")
    seat.add_argument("--seat-week-limit", type=int, default=None, metavar="PCT",
                      help="Stop serving once WEEKLY usage reaches this percent. Worth setting "
                           "lower than the session limit: a spent week costs days.")
    seat.add_argument("--seat-quota-ttl", type=float, default=None, metavar="SECONDS",
                      help="How long a quota reading is reused before re-probing (default 60). "
                           "0 = probe every request, which adds seconds to each one.")

    local_only = join.add_argument_group("Local only")
    # A remote engine polls the relay outbound, so it has no inbound endpoint to advertise.
    local_only.add_argument("--advertise-host", default=None,
                            help="Host/IP to advertise this engine at (local only).")

    remote_only = join.add_argument_group("Remote only")
    # Remote-only: billing + pull-based capacity + grid-page display (rejected in local). default=None
    # so a wrong-mode use is detectable.
    remote_only.add_argument("--engine-label", default=None,
                             help="Deprecated — the grid page derives the engine kind automatically; "
                                  "no longer changes display (remote only).")
    # Deprecated: pricing now lives in the authoritative per-provider table — set it with
    # `grid price set` instead. Kept so old invocations don't hard-error; they no longer advertise a price.
    remote_only.add_argument("--pricing-input", type=float, default=None,
                             help="Deprecated — use `grid price set`. (No longer advertises a price.)")
    remote_only.add_argument("--pricing-output", type=float, default=None,
                             help="Deprecated — use `grid price set`. (No longer advertises a price.)")
    # `default=None`, not the `store_true` default of False: `provider._reject_remote_only_flags`
    # decides "was this flag used" with `is not None`, so a False default would reject every LOCAL
    # `grid join`. Every flag in this group defaults to None for that reason.
    remote_only.add_argument("--respawn", action="store_true", default=None,
                             help="Stop the engine already serving this grid and start a fresh one, "
                                  "instead of no-opping an identical re-join (remote only).")
    remote_only.add_argument(
        "--allocator-provider",
        action="store_true",
        default=None,
        help=(
            "Create an empty provider identity for allocator-managed engines. The allocator can "
            "then load and unload models without a manually started bootstrap model (remote only)."
        ),
    )
    remote_only.add_argument(
        "--relay-at",
        default=None,
        metavar="URL",
        help=(
            "Use this URL for provider-to-relay traffic while keeping the grid's public URL "
            "canonical. Useful on the relay host (for example http://127.0.0.1:8090); "
            "non-loopback transports must use HTTPS. Changing it respawns only this provider."
        ),
    )
    # Task serving (ADR 0032). A task is claimed from the relay, and local mode has no relay.
    # `default=None` throughout, per
    # the group's comment above — `--tasks` in particular, because a `store_true` defaulting to
    # False would make `_reject_remote_only_flags` refuse every LOCAL join.
    remote_only.add_argument("--tasks", action="store_true", default=None,
                             help="Also claim distributed tasks for this grid, spending this box's "
                                  "own Claude subscription (remote only). Off unless you ask.")
    remote_only.add_argument("--max-tasks", type=_positive_task_count, default=None, metavar="N",
                             help="How many tasks this provider runs at once (default 1). "
                                  "Wins over GRID_MAX_TASKS.")
    remote_only.add_argument("--tasks-root", default=None, metavar="PATH",
                             help="Where task workspaces live. Keep it SHORT and outside your home "
                                  "directory. Wins over GRID_TASK_ROOT.")
    join.set_defaults(handler=cmd_join)

    leave = sub.add_parser("leave", help="Stop and unregister engines from a grid")
    leave.add_argument("grid", nargs="?", default=None,
                       help="Grid name or id (ag-…). Omit for the active grid.")
    leave.add_argument("--engine", default=None,
                       help="Engine to leave. Matches, in order: exact engine id, endpoint URL, the "
                            "--name given at join, a served model, or a URL fragment (e.g. :8000).")
    leave.add_argument("--all", action="store_true",
                       help="Leave every engine on this grid. Without --engine: a one-engine grid "
                            "leaves that one; a multi-engine grid requires --all.")
    leave.set_defaults(handler=cmd_leave)

    models = sub.add_parser("models", help="Live models the grid can run now")
    models.add_argument("grid", nargs="?", default=None,
                        help="Grid name or id (ag-…). Omit for the active grid.")
    models.add_argument("--verbose", action="store_true", help="Show the engine serving each model.")
    models.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    models.set_defaults(handler=cmd_models)

    engines = sub.add_parser("engines", help="Live engines joined to a grid")
    engines.add_argument("grid", nargs="?", default=None,
                         help="Grid name or id (ag-…). Omit for the active grid.")
    engines.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    engines.set_defaults(handler=cmd_engines)


def _add_models(sub) -> None:
    device_info = sub.add_parser(
        "device-info",
        help="This machine's hardware profile (CPU, memory, disk, GPU)",
    )
    device_info.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    device_info.set_defaults(handler=cmd_device_info)

    catalog = sub.add_parser("catalog", help="Models Grid can pull")
    catalog.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    catalog.add_argument(
        "--api",
        metavar="KIND",
        help="Show the API-engine whitelist for a service kind (e.g. openai, codex).",
    )
    catalog.set_defaults(handler=cmd_catalog)

    pull = sub.add_parser(
        "pull",
        help="Download a model. Takes a name from `grid catalog`, or any GGUF on Hugging Face "
             "as repository:file, e.g. unsloth/gemma-3-4b-it-GGUF:gemma-3-4b-it-Q4_K_M.gguf",
    )
    pull.add_argument("model")
    pull.set_defaults(handler=cmd_pull)

    for verb, help_text in (("rm", "Delete a local model file"), ("remove", "Alias for `grid rm`")):
        rm = sub.add_parser(verb, help=help_text)
        rm.add_argument("model", help="Filename under ~/.grid/models/")
        rm.add_argument("--yes", action="store_true", help="Skip confirmation.")
        rm.set_defaults(handler=cmd_rm)

    ctx = sub.add_parser("ctx", help="Show a model's max context length (from GGUF metadata)")
    ctx.add_argument("model", help="Filename under ~/.grid/models/ or a path to a .gguf")
    ctx.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    ctx.set_defaults(handler=cmd_ctx)


def _add_use(sub) -> None:
    chat = sub.add_parser("chat", help="Send one chat message")
    chat.add_argument("-m", "--model", required=True)
    chat.add_argument("message")
    # `append`, so several images go in one question ("what changed between these two?"). The model
    # has to be one `grid models --verbose` shows as vision-capable; a text-only one will either
    # ignore the image or refuse, and that is the engine's answer to give, not ours to pre-empt.
    chat.add_argument(
        "--image", action="append", metavar="PATH",
        help="Send an image with the message (vision models). Repeat for more than one.",
    )
    chat.add_argument("--grid", default=None)
    chat.add_argument("--json", action="store_true", help="Print the full JSON response.")
    chat.add_argument("--timeout", type=float, default=600.0)
    _add_remote_use_flags(chat)
    chat.set_defaults(handler=cmd_chat)

    image = sub.add_parser("image", help="Generate an image")
    _add_media_common(image)
    image.add_argument("prompt")
    image.add_argument(
        "-m", "--model",
        required=True,
        help="Model to use (e.g. doggi:hunyuan-image-3-t2i).",
    )
    image.add_argument("--width", type=int, default=720)
    image.add_argument("--height", type=int, default=720)
    # No default: the workflow the provider runs carries the step count its model was
    # distilled for. Sending one from here would override it on every call.
    image.add_argument("--steps", type=int, default=None)
    _add_remote_use_flags(image)
    image.set_defaults(handler=cmd_image)

    edit = sub.add_parser("edit", help="Edit one to three images")
    _add_media_common(edit)
    edit.add_argument("prompt")
    edit.add_argument(
        "-m", "--model",
        required=True,
        help="Model to use (e.g. doggi:hunyuan-image-3-i2i).",
    )
    edit.add_argument(
        "-i",
        "--image",
        action="append",
        dest="input_images",
        required=True,
        help="Input image path. Repeat up to three times.",
    )
    edit.add_argument("--steps", type=int, default=None)
    _add_remote_use_flags(edit)
    edit.set_defaults(handler=cmd_edit)

    video = sub.add_parser("video", help="Generate a short video from an image")
    _add_media_common(video)
    video.add_argument("prompt")
    video.add_argument(
        "-m", "--model",
        required=True,
        help="Model to use (e.g. doggi:Wan-AI/Wan2.2-I2V-A14B-Lightning).",
    )
    video.add_argument("-i", "--image", required=True, help="Input image path.")
    video.add_argument("--duration", choices=VALID_I2V_DURATIONS, default="5s")
    video.add_argument("--aspect-ratio", choices=VALID_I2V_ASPECT_RATIOS, default="2:3")
    _add_remote_use_flags(video)
    video.set_defaults(handler=cmd_video)


def _add_state(sub) -> None:
    mode = sub.add_parser("mode", help="Show or switch the active mode (local/remote)")
    mode.add_argument(
        "target",
        nargs="?",
        choices=("local", "remote"),
        default=None,
        help="Switch to this mode and persist it; omit to print the current mode.",
    )
    mode.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    mode.set_defaults(handler=cmd_mode)

    use = sub.add_parser("use", help="Set the active grid for the current mode")
    use.add_argument(
        "name",
        nargs="?",
        default=None,
        help="Grid to make active; omit to print the current active grid.",
    )
    use.add_argument("--none", action="store_true", help="Clear the active grid for the current mode.")
    use.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    use.set_defaults(handler=cmd_use)


def _add_auth(sub) -> None:
    login = sub.add_parser("login", help="Sign in to remote mode")
    login.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the sign-in URL and code instead of opening a browser (for headless machines).",
    )
    login.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    login.set_defaults(handler=cmd_login)

    logout = sub.add_parser("logout", help="Sign out of remote mode")
    logout.add_argument(
        "--force",
        action="store_true",
        help="Sign out even if a serve child on this box could not be stopped (it is still stopped "
             "first; `grid leave <grid-id>` reaps a survivor afterwards).",
    )
    logout.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    logout.set_defaults(handler=cmd_logout)

    sync = sub.add_parser("sync", help="Refresh your remote grids without signing in again")
    sync.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    sync.set_defaults(handler=cmd_sync)


def _add_members(sub) -> None:
    """Remote-only membership admin (DECISIONS D13): `grid members add|remove [grid] <email>` and
    `grid members list [grid]`. Gated in local mode by dispatch (`members` is in `REMOTE_ONLY`). On
    add/remove the `[grid]` positional is declared first so argparse binds a lone positional to the
    required `email`; omitting it falls back to the active grid."""
    members = sub.add_parser("members", help="Manage who may use or serve a remote grid")
    members_sub = members.add_subparsers(dest="subcommand", required=True)

    add = members_sub.add_parser("add", help="Add a member to a grid")
    add.add_argument("grid", nargs="?", default=None)
    add.add_argument("email")
    add.add_argument(
        "--role",
        choices=("consumer", "provider", "both"),
        default="both",
        help="Member role (default: both).",
    )
    add.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    add.set_defaults(handler=cmd_remote_members)

    remove = members_sub.add_parser("remove", help="Remove a member from a grid")
    remove.add_argument("grid", nargs="?", default=None)
    remove.add_argument("email")
    remove.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    remove.set_defaults(handler=cmd_remote_members)

    listing = members_sub.add_parser("list", help="List a grid's members and roles")
    listing.add_argument("grid", nargs="?", default=None)
    listing.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    listing.set_defaults(handler=cmd_remote_members)


def _add_project(sub) -> None:
    """Remote-only `grid project create|list` and `grid project member …` (ADR 0033 D-a).

    Gated in local mode by dispatch (`project` is in `REMOTE_ONLY`). A project is addressed by
    **id** everywhere downstream, so `create` printing one and `list` showing them is not a
    convenience — without it, `grid task create --project <id>` has no id to be given."""
    project = sub.add_parser("project", help="Create projects and manage who is in them (remote)")
    project_sub = project.add_subparsers(dest="subcommand", required=True)

    create = project_sub.add_parser("create", help="Create (or get) one of your projects by name")
    create.add_argument("--name", required=True, help="What to call it. Unique among your own.")
    # ADR 0034 D-o (issue 48). Without it a project is created with no trunk and the very next
    # thing anybody does is refused, which is the first wall a new user hits.
    create.add_argument(
        "--empty", action="store_true",
        # ⚠️ No git word here (ADR 0034 D-m, issue 46): this flag is among the first things a new
        # person meets, and "trunk" sends them to git's documentation for a product that has
        # deliberately hidden git from them. `tests/test_application_surface.py` keeps it that way.
        help=("Start it ready to work in, so a task can run in it immediately. IRREVERSIBLE: a "
              "project that already holds something can never take in an existing repository with "
              "`grid project import`."))
    create.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    create.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    create.set_defaults(handler=cmd_remote_project)

    # `init` — the OTHER way a project gets a trunk (ADR 0033 D-o, issue 25), and the one a new
    # piece of work needs. Registered next to `create` rather than beside `import`, because
    # `create`'s own next step names it and somebody who reads `grid project --help` on that advice
    # should find it where they were just sent.
    starter = project_sub.add_parser(
        "init",
        # ⚠️ No git word in the SUMMARY, and `test_project_inits_summary_speaks_the_same_words`
        # keeps it that way. `init` is GIT_PLANE — its long description below may say `trunk`, and
        # does — but this one line is different twice over: it is what `grid project --help` shows
        # everybody who is merely browsing, and `_no_trunk_message` sends a brand-new person here
        # BY NAME with the words "give it something to start from". Two registers for one step,
        # one sentence apart, and the person reading them has never heard of a trunk.
        help="Give an empty project a starting point, so tasks can run in it",
        description=(
            "Create the project's `main` at a single empty root commit.\n\n"
            "A project has no trunk when it is created, and a task cannot be cut from nothing. "
            "This is one of the two ways to give it one — `grid project import` is the other, for "
            "a repository that already exists. Every member's branch is then cut from this same "
            "commit, which is what lets their work be integrated and promoted later.\n\n"
            "The trunk it makes is EMPTY and holds nothing you send: files reach `main` by "
            "promoting a branch, never by a bootstrap. Put them on yours with `grid project "
            "commit` (or a task) and promote.\n\n"
            "Refused if the project already has a trunk, and it is not undoable — a project that "
            "has been initialized can no longer import a repository, because a second trunk would "
            "move `main` out from under every member's branch."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    project_arg.add_project(starter)
    starter.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    starter.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    starter.set_defaults(handler=cmd_remote_project)

    listing = project_sub.add_parser(
        "list", help="List the projects you can work in")
    listing.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    listing.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    # ADR 0033 D-p / issue 33. Archived projects are hidden by default because this listing is what
    # a member reads to find an id, and one they archived is one they have said they are not working
    # in. Nothing becomes unreachable — the id still resolves on every other route.
    listing.add_argument(
        "--all", action="store_true",
        help="Include archived projects, marked as archived. They are hidden by default.")
    listing.set_defaults(handler=cmd_remote_project)

    # ADR 0033 D-p / issue 33 — the two ways to stop looking at a project, and they are deliberately
    # not symmetrical. Registered next to `list`, because that is the command whose output somebody
    # is looking at when they decide they want one of these.
    archiver = project_sub.add_parser(
        "archive",
        help="Stop a project accepting new work and hide it from `grid project list`",
        description=(
            "Archive a project. It accepts no new work — no tasks, commits, integrates, promotes, "
            "imports or inits — and leaves `grid project list` unless you pass --all.\n\n"
            "NOTHING IS DESTROYED. The repository is kept exactly as it is, and every read still "
            "works: `grid project status`, `grid project clone`, `grid task list`, `grid task "
            "fetch`. Reverse it at any time with `grid project unarchive`.\n\n"
            "A task that is already queued or running is NOT cancelled — it is claimed, runs and "
            "settles normally. Archiving stops new work starting; use `grid task cancel` to stop "
            "work that has already started."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    project_arg.add_project(archiver)
    archiver.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    archiver.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    archiver.set_defaults(handler=cmd_remote_project)

    restorer = project_sub.add_parser(
        "unarchive", help="Put an archived project back, so it accepts work again")
    project_arg.add_project(restorer, help="Project id from `grid project list --all`.")
    restorer.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    restorer.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    restorer.set_defaults(handler=cmd_remote_project)

    remover = project_sub.add_parser(
        "delete",
        help="Permanently remove a project that has nothing in it",
        description=(
            "Delete a project, its members and its repository. THIS CANNOT BE UNDONE.\n\n"
            "Refused unless the project has never held anything and has never had a task — in that "
            "state there is provably nothing in it to lose. Anything else is refused, naming "
            "`grid project archive`, which takes a project out of the way and keeps every byte.\n\n"
            "For the project created by a typo, and for one you inited or imported into by "
            "mistake and want the id back for. Asks before it acts unless you pass --yes."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    project_arg.add_project(remover, help="Project id from `grid project list --all`.")
    remover.add_argument(
        "--yes", action="store_true",
        help="Do not ask for confirmation. Required when stdin is not a terminal.")
    remover.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    remover.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    remover.set_defaults(handler=cmd_remote_project)

    # ADR 0035 D-a / issue 55 — changing what a project is called. Registered next to the lifecycle
    # verbs above because it answers a neighbouring question about the same row: a person tidying up
    # is often renaming and archiving in the same sitting.
    renamer = project_sub.add_parser(
        "rename",
        help="Change what a project is called",
        description=(
            "Give a project a different name.\n\n"
            "Its id does not change, so every clone, script and saved link still reaches it — the "
            "new name appears in `grid project list` and nowhere else has to be updated.\n\n"
            "This is what to use instead of creating a project with the new name. `grid project "
            "create` finds a project BY name, so creating one with a new name gives you a second, "
            "empty project and leaves your work in the first.\n\n"
            "Owner only, and refused while the project is archived — unarchive it first. The old "
            "name becomes free the moment this succeeds."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    project_arg.add_project(renamer)
    renamer.add_argument(
        "--name", required=True,
        help="What to call it from now on.")
    renamer.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    renamer.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    renamer.set_defaults(handler=cmd_remote_project)

    # ADR 0034 D-k / issue 36 — who on the grid may reach a project. Registered next to the
    # lifecycle verbs above because they answer neighbouring questions about the same row, and a
    # person deciding to archive a project is often deciding who should see it.
    sharer = project_sub.add_parser(
        "share",
        help="Let anyone on this grid work in the project, without being added to it",
        description=(
            "Share a project with everyone on this grid. Anyone signed in can then work in it "
            "without being added as a member, and they become one the first time they do.\n\n"
            "This is the DEFAULT for a new project. Use `grid project private` to restrict one.\n\n"
            "Owner only. Nothing is destroyed and nothing is moved — this changes who may reach "
            "the project, not what is in it."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    project_arg.add_project(sharer)
    sharer.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    sharer.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    sharer.set_defaults(handler=cmd_remote_project)

    privater = project_sub.add_parser(
        "private",
        help="Restrict the project to its members",
        description=(
            "Make a project private: only its members can reach it — read it, clone it, list its "
            "tasks or work in it.\n\n"
            "Everyone who has already worked in the project is a member and KEEPS access. This "
            "stops anyone else joining, it does not remove anybody.\n\n"
            "Owner only. Reverse it at any time with `grid project share`."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    project_arg.add_project(privater)
    privater.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    privater.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    privater.set_defaults(handler=cmd_remote_project)

    # ADR 0035 D-b / issue 56 — the member's own way off a project. Registered immediately before
    # the `member` group because it is the counterpart to `member remove` down there: the two mean
    # the same thing to the project, and only who may ask differs. A person reading `member remove`
    # and finding it is the owner's should see this on the same screen.
    leaver = project_sub.add_parser(
        "leave",
        help="Take yourself off a project",
        description=(
            "Leave a project you are a member of. You do not need the owner to do it for you.\n\n"
            "Nothing of yours is stopped or thrown away: any task you have already asked for runs "
            "and finishes normally. What changes is that the project stops answering to you — you "
            "can no longer read it, send it work, or see its tasks.\n\n"
            "YOUR TASKS IN IT LEAVE `grid task list` WITH YOU, and only the project's owner can "
            "put you back. That is why --yes is required: this command never asks.\n\n"
            "The owner of a project cannot leave it — nobody could reach it afterwards, and there "
            "is no way to hand it over. Use `grid project archive` to stop it accepting work, or "
            "`grid project delete` to remove one that holds nothing."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    project_arg.add_project(leaver)
    # ⚠️ **Required, with no interactive fallback** (ADR 0035 D-g), unlike `delete` above. A confirm
    # somebody declines exits 0, and 0 on this plane means *done* — so a script would read a refused
    # departure as a completed one. The refusal without it is where the warning lives.
    leaver.add_argument(
        "--yes", action="store_true",
        help="Required. Says you meant it — this command never asks.")
    leaver.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    leaver.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    leaver.set_defaults(handler=cmd_remote_project)

    member = project_sub.add_parser("member", help="List, add and remove project members")
    member_sub = member.add_subparsers(dest="member_action", required=True)

    member_list = member_sub.add_parser("list", help="Show a project's members and their keys")
    project_arg.add_project(member_list)
    member_list.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    member_list.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    member_list.set_defaults(handler=cmd_remote_project)

    member_add = member_sub.add_parser("add", help="Admit a grid member to this project")
    project_arg.add_project(member_add)
    member_add.add_argument(
        "--email", required=True,
        help="Their address on this grid. They must have signed in to it at least once.")
    member_add.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    member_add.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    member_add.set_defaults(handler=cmd_remote_project)

    # By member key and not by email: the key is a path segment by construction, and a member's
    # `user_id` (`grid:<network>:<sub>`) is not. `grid project member list` prints it.
    member_remove = member_sub.add_parser("remove", help="Remove someone from this project")
    project_arg.add_project(member_remove)
    project_arg.add_member(member_remove)
    member_remove.add_argument("--grid", default=None,
                               help="Grid to act on (default: active grid).")
    member_remove.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    member_remove.set_defaults(handler=cmd_remote_project)

    # `wip reset` — the one way out of a conversation's branch left ahead of the turn that settled
    # onto it (ADR 0033 D-c, re-keyed by ADR 0034 D-e). It SURVIVES the clean break that deletes
    # promote and integrate (ADR 0034 D-m), because the relay's own apply can still leave a branch
    # ahead and this is the documented recovery. Nothing else moves one backwards: members never
    # push, the apply writes only `main`, and there is no revert — so without this the
    # conversation's NEXT turn is silently cut from a lost attempt's work.
    wip = project_sub.add_parser(
        "wip", help="Work on a conversation's branch — the ref its tasks are cut from")
    wip_sub = wip.add_subparsers(dest="wip_action", required=True)

    wip_reset = wip_sub.add_parser(
        "reset", help="Move a conversation's branch back to a commit (recovers a lost attempt)")
    project_arg.add_project(wip_reset)
    # Through `project_arg`, never a bare `add_argument`: every refusal in that module offers forms
    # DERIVED from what was registered, so a second positional added behind its back makes each of
    # them name a command that no longer parses. Caught by
    # `test_a_refusal_only_ever_offers_a_command_that_really_works`, which is what that test is for.
    project_arg.add_conversation(
        wip_reset,
        help="Which conversation's branch to move. `grid task get <task-id>` prints the "
             "conversation a task belongs to.")
    wip_reset.add_argument(
        "--commit", required=True,
        help="Where to put the branch. `grid task get <id>` prints the `base_commit` a task was "
             "cut from, which is usually the commit you want.")
    wip_reset.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    wip_reset.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    wip_reset.set_defaults(handler=cmd_remote_project)

    # `status` — the question that used to need a WRITE to answer (ADR 0033 D-l, issue 19a). A pure
    # read: it moves no ref and takes nobody's slot. Its `check` sibling went with the integrate it
    # previewed (ADR 0034 D-d, issue 41).
    status = project_sub.add_parser(
        "status",
        help="Where the project is: what it holds, what is running, and who can serve it",
        description=(
            "Where the project is.\n\n"
            "What is holding your tasks was answerable before only by attempting a create and "
            "reading the refusal. It is a read now, and it costs nothing.\n\n"
            "It is also how an application notices the project changed without running `git fetch`: "
            "the grid applies every finished task to the project itself, so what the project "
            "holds changes whenever anybody's work lands."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    project_arg.add_project(status)
    status.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    status.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    status.set_defaults(handler=cmd_remote_project)

    # `files` / `file` / `download` — seeing the project with no git on the machine (ADR 0034 D-m,
    # issue 45). Until these, the only read path was `clone`, which needs git AND a credential
    # helper git is new enough to describe — a dead end at the first step for the person this
    # product is for.
    files = project_sub.add_parser(
        "files",
        help="List what is in the project, one folder at a time",
        description=(
            "See what is in your project.\n\n"
            "No git needed on this machine. With no path you get the top of the project; give a "
            "folder's name to look inside it.\n\n"
            "What you see is the project as it stands now — everybody's finished work, applied by "
            "the grid as each task completes."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    project_arg.add_project(files)
    # Through `project_arg` rather than a bare `add_argument`: an optional positional BEHIND the
    # optional project id is the `clone`/`refresh` shape, and argparse fills the two left to right —
    # so `grid project files --project P src` parks `src` in the project id and lists the top of a
    # project called `src`. Measured, before `add_path` existed.
    project_arg.add_path(files, help="Folder to look inside (default: the top of the project).")
    files.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    files.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    files.set_defaults(handler=cmd_remote_project)

    one_file = project_sub.add_parser(
        "file",
        help="Read one file out of the project",
        description=(
            "Read one file, without git and without downloading the whole project.\n\n"
            "Text is printed. Anything that is not text needs `--output`, because writing it to a "
            "terminal stops that terminal rendering text at all.\n\n"
            "A very large file is refused with its size — `grid project download` gets it as part "
            "of the whole project."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    project_arg.add_project(one_file)
    one_file.add_argument("path", help="File to read, e.g. `src/app.py`.")
    one_file.add_argument("--output", default=None,
                          help="Write the file here instead of printing it.")
    one_file.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    one_file.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    one_file.set_defaults(handler=cmd_remote_project)

    download = project_sub.add_parser(
        "download",
        help="Download the whole project as a zip",
        description=(
            "Take the whole project away as one zip file.\n\n"
            "No git needed. This is the copy to hand to somebody else, or to open in an editor on "
            "a machine that has no developer tools.\n\n"
            "It is a snapshot of the project as it stands, not a repository: to keep working in it "
            "through the grid, send a task instead."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    project_arg.add_project(download)
    download.add_argument("--output", default=None,
                          help="Where to write the zip (default: <project-id>.zip here).")
    download.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    download.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    download.set_defaults(handler=cmd_remote_project)

    # `commit` — a change goes in without an agent (ADR 0033 D-j, issue 20), onto the branch of a
    # CONVERSATION named on the command line (ADR 0034 D-e, issue 41).
    committer = project_sub.add_parser(
        "commit",
        help="Put files into the project without running an agent",
        description=(
            "Commit files into a conversation, with no agent and no provider.\n\n"
            "This is the answer to 'the agent got it 90% right, let me fix the last line'. The "
            "alternative is `grid task create --file`, which spends a slot and then runs an agent "
            "that may change the very line you are fixing.\n\n"
            "It goes into a conversation you already started, so the next message you send there "
            "starts from it. You still cannot push to the project — the write goes through the "
            "grid, lands on exactly one ref, and holds that conversation's slot while it does. So "
            "it is refused while that conversation has a turn running, and the refusal names it.\n\n"
            "Executable bits look after themselves. Editing a file the project already has as "
            "executable keeps it executable, and a local file that is executable is committed that "
            "way. (Removing an executable bit is not expressible here.)\n\n"
            "--delete takes a path already in your branch. A path that is not there is REFUSED "
            "rather than quietly ignored, because git's own answer to deleting a file that does "
            "not exist is to report success and do nothing.\n\n"
            "It appears in the project by itself, exactly as a finished turn does — the grid "
            "applies it. There is nothing else to run afterwards."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    committer.add_argument(
        "conversation_id",
        help="Which conversation to commit into. `grid task create` prints one, and "
             "`grid task get <turn-id>` prints the conversation a turn belongs to.")
    committer.add_argument(
        "-m", "--message", required=True, metavar="MSG",
        help="What this commit did. Required, like git's own.")
    committer.add_argument(
        "--file", action="append", metavar="LOCAL[:DEST]", default=None,
        help="A file to write, repeatable. DEST defaults to the file's name. Same form as "
             "`grid task create --file`.")
    committer.add_argument(
        "--dir", action="append", metavar="LOCAL[:DEST]", default=None,
        help="A folder to write, repeatable. DEST defaults to the folder's name. Inside a git work "
             "tree your .gitignore is honoured; `.git/`, `.grid/`, `.claude/`, `.mcp.json` and "
             "symlinks are skipped and reported. Executable bits are kept, like --file.")
    committer.add_argument(
        "--delete", action="append", metavar="PATH", default=None,
        help="A path in the conversation's branch to remove, repeatable. Refused if it is not "
             "there.")
    committer.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    committer.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    committer.set_defaults(handler=cmd_remote_project)

    # `import` — how a project that has no commits gets a trunk from a repository that already
    # exists (ADR 0033 D-f, issue 16b). Since issue 16b a member cannot push `main` themselves, and
    # since issue 25 (D-o) `init` is the other way in — for work that starts from nothing.
    importer = project_sub.add_parser(
        "import",
        help="Import an existing repository into an empty project",
        description=(
            "Import an existing repository, with its history, into a project that has no `main` "
            "yet.\n\n"
            "One of the two ways a project gets a trunk — `grid project init` is the other, and "
            "makes an empty one for work that starts from nothing. The relay is `main`'s sole "
            "writer — those two create it and the grid moves it afterwards, applying every turn "
            "that succeeds, and nothing else does — so a first `git push` of `main` is refused."
            "\n\n"
            "What happens: the repository is pushed to a staging ref only you can see, the relay "
            "reads EVERY tree its history reaches, and only then does it become `main`. The "
            "reading is the slow part and it is why this command waits — on a 29,000-commit "
            "repository it is about twenty seconds.\n\n"
            "It is refused if the repository contains a submodule (a task's provider has no "
            "credential to fetch one), a path under `.grid/`, or a symlink pointing outside the "
            "repository. Symlinks that stay inside are fine, and so is `.claude/`. A repository "
            "using Git LFS imports with a warning: an agent will see pointer files, not content.\n\n"
            "A refused import leaves the project with NO trunk, on purpose — half a trunk would be "
            "worse. Fix what it names and import again, or import into a fresh project.\n\n"
            "Import brings a repository into an EMPTY project. A project that already has a `main` "
            "is refused, because a second import would move the trunk out from under every "
            "member's branch and nothing could integrate back."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    importer.add_argument("path", help="Path to the local git repository to import.")
    project_arg.add_project(importer)
    importer.add_argument(
        "--branch", default="HEAD",
        help="Which local ref to import (default: HEAD, i.e. whatever is checked out).")
    importer.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    importer.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    importer.set_defaults(handler=cmd_remote_project)

    # `clone` — a member's own working copy, with a credential helper instead of a stored token
    # (ADR 0033 D-h, issue 17). `grid task fetch` still exists and is still the right thing for one
    # task's result; this is for working in the project.
    cloner = project_sub.add_parser(
        "clone",
        help="Clone a project's repository, configured to ask grid for a credential each time",
        description=(
            "Clone a project into a directory you name.\n\n"
            "No token is written anywhere. The clone is configured to run `grid credential` when\n"
            "git needs one, scoped to this grid's relay and to this clone alone — your global git\n"
            "config is untouched. Because git asks each time, a refreshed token is picked up\n"
            "automatically, where a token written into `.git/config` would expire in place.\n\n"
            "You are put on the project's trunk, which holds everybody's finished work: the grid\n"
            "applies every turn that succeeds, so there is no separate branch of yours to be on.\n\n"
            "`git push` is REFUSED, and that is the design rather than a permission to request.\n"
            "The project is written by the grid alone, so a push could move the ground under work\n"
            "that is running right now.\n"
            "Land work with `grid task create`, or `grid project commit <conversation-id>` for\n"
            "files with no agent.\n\n"
            "Needs a git that reports `authtype` from `git credential capability`; the relay\n"
            "accepts no credential scheme an older git can send."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    project_arg.add_project(cloner)
    project_arg.add_directory(
        cloner, help="Where to put it (default: a directory named after the project id).")
    cloner.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    cloner.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    cloner.set_defaults(handler=cmd_remote_project)

    # `refresh` — what a clone does after somebody else's work lands. The counterpart to `clone`,
    # and deliberately the READ-ONLY half of it: re-cloning updates by resetting the branch, which
    # is refused outright the moment you have a local commit, and that is the ordinary state of
    # anyone checkpointing work.
    #
    # NO `--grid`. Every other project subcommand has one because it calls the relay; this one calls
    # nothing — the grid is already pinned inside the clone's own credential helper, so a flag
    # naming a different one could not change anything and would only look as though it did.
    refresher = project_sub.add_parser(
        "refresh",
        help="Fetch what the grid has for a clone, and report how far behind it is",
        description=(
            "Bring a clone's view of the grid up to date, and say what changed.\n\n"
            "This NEVER touches your working tree or your branch — it updates what your clone knows\n"
            "about the grid and reports the difference. So it works with local commits, with a\n"
            "dirty tree, and while a task of yours is running. Re-running `grid project clone` over\n"
            "the same directory also updates it, but by RESETTING your branch to the fetched tip,\n"
            "which it refuses to do when you have commits the grid has not seen.\n\n"
            "It reports on the branch you are standing on, whichever that is. Inside a clone that\n"
            "is usually your own WIP branch, but `grid task fetch`'s own advice puts you on\n"
            "`task/<id>`, and those move and are collected too.\n\n"
            "Nothing here reaches `main`, and nothing here is compared against it: how far your\n"
            "branch is from the trunk is `grid project status`, which asks the relay. This asks\n"
            "only your clone and the git plane.\n\n"
            "It needs the relay reachable. It does not need the control plane — the credential\n"
            "helper reads your local store, never the network."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    project_arg.add_project(refresher)
    project_arg.add_directory(
        refresher, help="The clone to refresh (default: the current directory).")
    refresher.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    refresher.set_defaults(handler=cmd_remote_project)


def _add_task(sub) -> None:
    """Remote-only `grid task` — hand the grid a coding task, watch it, read the result back, stop it.

    `create | get | list | follow | fetch | cancel` (ADR 0032; `list` and `cancel` are ADR 0033 D-l).

    Gated in local mode by dispatch (`task` is in `REMOTE_ONLY`). `--grid` is a FLAG, not a leading
    positional — the same call `router set-advisors` made. The reason used to be given as "the
    prompt that follows is free-form": it is not, `--prompt` is a flag, and since issue 28 there IS
    a leading optional positional here, the project id. The reason that survives is that a SECOND
    optional positional beside it could not be told from the first (measured — `cli/project_arg.py`
    documents the same limitation for `promote`)."""
    task = sub.add_parser("task", help="Create and read distributed tasks (remote)")
    task_sub = task.add_subparsers(dest="subcommand", required=True)

    create = task_sub.add_parser("create", help="Hand the grid a task and queue it for a provider")
    create.add_argument("--prompt", required=True, help="What the agent should do.")
    # The ONE project-taking command where omitting it is not a refusal: it resolves the caller's
    # own `default` project (ADR 0033 D-o, issue 26), so `required=False`.
    project_arg.add_project(
        create, required=False,
        help="Project id to run in, from `grid project list` (default: your own project named "
             "'default', when you already have one — it is never created for you). You may run "
             "as many conversations in a project as you like and they run alongside each other; "
             "the messages inside one conversation run in the order you sent them. A colleague's "
             "work never blocks yours.")
    create.add_argument(
        "--file", action="append", default=None, metavar="LOCAL[:DEST]",
        help="File to upload with the task; repeatable. Committed with the task before any "
             "provider can claim it, so the agent always finds it. Placed at the file's own name "
             "unless you give a destination (e.g. ./conf.toml:config/conf.toml).")
    # ADR 0033 D-j / issue 27. Client-only: it expands into the same `files` list `--file` produces,
    # so the relay sees the payload it saw before and there is no rollout order.
    create.add_argument(
        "--dir", action="append", default=None, metavar="LOCAL[:DEST]",
        help="Folder to upload with the task; repeatable. Placed under the folder's own name "
             "unless you give a destination. Inside a git work tree your .gitignore is honoured; "
             "`.git/`, `.grid/`, `.claude/`, `.mcp.json` and symlinks are skipped and reported. "
             "For a whole codebase use `grid project import` instead.")
    # ADR 0033 D-o / issue 26 — the opt-in convenience form D-o promised. Opt-IN because it is a
    # one-way door: `grid project import` refuses a project that already has a trunk, so a project
    # given an empty one can never bring an existing repository in, and nothing undoes it.
    create.add_argument(
        "--init-project", dest="init_project", action="store_true",
        # No git word (ADR 0034 D-m, issue 46) — see `grid project create --empty`, which this is
        # the convenience form of.
        help="Start the project empty first, if it holds nothing yet, then run the task. For work "
             "that starts from nothing. ONE-WAY: a project that already holds something can never "
             "take in an existing repository, so use `grid project import` instead if you have "
             "one. Your uploaded files start inside your own conversation, and reach the project "
             "when the task succeeds.")
    # ADR 0032 / issue 32. `create` printed an id and stopped, so every use was create → copy the id
    # → `grid task follow <id>`. This is that, with the id it already has and no window in between:
    # the stream is attached from `after_seq=-1`, so nothing is missed.
    create.add_argument(
        "--follow", action="store_true",
        help="Watch the task after creating it, and exit with its outcome — 0 completed, non-zero "
             "otherwise, exactly as `grid task follow` does. Ctrl-C stops the watching, not the "
             "task. With --json the create payload is one compact line and each event follows on "
             "its own line, so both are readable by a program.")
    create.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    create.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    create.set_defaults(handler=cmd_remote_task)

    # ADR 0034 D-n / issue 47 — the door the queue was built for. `create` opens a conversation and
    # sends its FIRST message; this sends the next one. Until it existed every `create` minted a
    # fresh conversation, so there was no way to continue one at all.
    #
    # A separate verb rather than `create --conversation <id>`, for the reason `follow` is separate
    # from `get --follow`: they differ in what they ADDRESS. `create` names a project and may resolve
    # one; this names a conversation, which already answers that — so `--project` and
    # `--init-project` are absent rather than ignored, and the relay refuses a `project_id` on the
    # wire for the same reason.
    send = task_sub.add_parser(
        "send", help="Send another message into a conversation you already started",
        description=(
            "Send a follow-up message into a conversation. It runs after whatever that "
            "conversation is already doing — your messages inside one conversation run in the "
            "order you sent them, and you can type ahead without waiting.\n\n"
            "The conversation id is printed by `grid task create`, and is on every task that "
            "`grid task get` and `grid task list` report.\n\n"
            "Only the person who started a conversation can send into it. A colleague can read "
            "its tasks; to work alongside them, start your own with `grid task create`."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    send.add_argument(
        "conversation_id",
        help="Conversation to send into, from `grid task create` or `grid task get`.")
    send.add_argument("--prompt", required=True, help="What the agent should do next.")
    send.add_argument(
        "--file", action="append", default=None, metavar="LOCAL[:DEST]",
        help="File to upload with the message; repeatable. Committed before any provider can claim "
             "the task, so the agent always finds it. Placed at the file's own name unless you give "
             "a destination (e.g. ./conf.toml:config/conf.toml).")
    send.add_argument(
        "--dir", action="append", default=None, metavar="LOCAL[:DEST]",
        help="Folder to upload with the message; repeatable. Placed under the folder's own name "
             "unless you give a destination. Inside a git work tree your .gitignore is honoured; "
             "`.git/`, `.grid/`, `.claude/`, `.mcp.json` and symlinks are skipped and reported.")
    send.add_argument(
        "--follow", action="store_true",
        help="Watch the task after sending it, and exit with its outcome — 0 completed, non-zero "
             "otherwise, exactly as `grid task follow` does. Ctrl-C stops the watching, not the "
             "task. A message waiting for an earlier one in the same conversation simply shows "
             "nothing until the earlier one finishes.")
    send.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    send.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    send.set_defaults(handler=cmd_remote_task)

    # `get` exits with the task's OUTCOME (ADR 0032, issue 32) — the same contract `follow` has
    # always had, on the command a script is far more likely to reach for, because it is the
    # non-blocking one. It returned 0 whatever the state until this slice, so `grid task get $id &&
    # deploy.sh` deployed a failed task and nothing anywhere said so.
    get = task_sub.add_parser(
        "get",
        help="Show a task's state and result; exit with its outcome",
        description=(
            "Show where a task got to, once. Exits with the task's own outcome, so a script can "
            "branch on it:\n\n"
            "  0   completed\n"
            "  1   failed or timed_out — and, as everywhere in this CLI, any refusal\n"
            "  2   not finished yet (preparing, queued, running)\n\n"
            "`2` is the code that makes waiting expressible: 0 would say \"fine\" about an outcome "
            "nobody has reached, and 1 would say it went wrong. It is also the ONLY code a poller "
            "may read as \"ask again\" — 1 covers both a failed task and a relay that could not be "
            "reached.\n\n"
            # `rc`, never `status`: in zsh — the default macOS shell — `status` is a READ-ONLY
            # special parameter aliased to `$?`, so the assignment fails, the comparison then reads
            # the assignment's own 1, and the loop exits reporting a FAILURE on the first poll of a
            # perfectly healthy running task. Measured in zsh, bash and sh; bash and sh accept
            # `status` happily, which is what makes the trap invisible to whoever writes it.
            "  until grid task get \"$id\"; do\n"
            "    rc=$?\n"
            "    [ \"$rc\" -eq 2 ] || exit \"$rc\"   # it finished, and not well\n"
            "    sleep 30\n"
            "  done\n\n"
            "Read the status: `until grid task get \"$id\"; do sleep 30; done` on its own ends only "
            "on success, so it waits forever on a task that failed.\n\n"
            "A state this build cannot place — one a newer relay invented — is reported as "
            "unfinished with a line on stderr saying so, never as a success or a failure.\n\n"
            "`--json` prints exactly what it always did; only the exit status is new. To watch a "
            "task instead of asking once, use `grid task follow`."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    get.add_argument(
        "task_id",
        help="Task id, from `grid task create` or `grid task list`. Not the conversation id "
             "— that one goes to `grid task send`.")
    get.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    get.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    get.set_defaults(handler=cmd_remote_task)

    # A separate verb rather than `get --follow`: `get` answers "where did it get to" in one shot,
    # `follow` holds a stream open and owns a cursor. One flag flipping between those two would
    # change the output shape wholesale.
    #
    # ONE verb for a turn and for a whole conversation (ADR 0034 D-m, issue 51), because the two are
    # the same watching: one cursor, one reconnect budget, one renderer. A second command would be a
    # synonym of this one, which `CONTEXT-MAP.md` bans, and two copies of the cursor to keep in step.
    follow = task_sub.add_parser("follow", help="Watch a task's output as it runs")
    what = follow.add_mutually_exclusive_group()
    what.add_argument("task_id", nargs="?", default=None,
                      help="Task id, from `grid task create` or `grid task list`. Not the "
                           "conversation id — that one goes to --conversation.")
    what.add_argument(
        "--conversation", default=None, metavar="<conversation-id>",
        help="Watch a whole conversation instead of one task — every task's output in order, "
             "including steps the grid added itself. Ends when the conversation goes quiet.")
    follow.add_argument(
        "--after-seq", type=int, default=-1, dest="after_seq",
        help="Resume after this event sequence number (default: -1, from the start). "
             "A conversation's sequence is its own and is not a task's.")
    follow.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    follow.add_argument("--json", action="store_true", help="Emit one JSON event per line.")
    follow.set_defaults(handler=cmd_remote_task)

    fetch = task_sub.add_parser("fetch", help="Fetch a finished task's result into a directory")
    fetch.add_argument(
        "task_id",
        help="Task id, from `grid task create` or `grid task list`. Not the conversation id "
             "— that one goes to `grid task send`.")
    fetch.add_argument(
        "--into", default=None, metavar="DIR",
        help="Where to put the result (default: ./<task-id>). Created if missing; an existing "
             "directory is added to, never reset.")
    fetch.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    fetch.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    fetch.set_defaults(handler=cmd_remote_task)

    # `list` — nothing listed tasks at all before ADR 0033 issue 19a. `grid task get` answers one id
    # at a time, and the id came from `grid task create`, so somebody who closed their terminal had
    # to clone the project and read `task/*` refs by hand.
    listing = task_sub.add_parser("list", help="List your conversations, or one project's")
    # OPTIONAL since ADR 0034 D-m (issue 46), and omitting it means something DIFFERENT from what it
    # means on `create`. There it resolves the caller's own `default` project; here it widens to
    # every project — an application's home screen is *your conversations*, which names none. Issue
    # 28's refusal still fires for a blank value, and both spellings still work when one is given.
    project_arg.add_project(
        listing, required=False,
        help="Project id to list, from `grid project list` (default: your conversations in every "
             "project you can reach, newest page last).")
    listing.add_argument(
        "--all", action="store_true",
        help="Every member's tasks, not only your own. A project is shared, so this is how a team "
             "sees what the team ran. Needs a project: the whole grid's work is listed one project "
             "at a time.")
    listing.add_argument(
        "--state", action="append", default=None, metavar="STATE",
        help="Only tasks in this state (queued, running, completed, failed, timed_out). "
             "Repeatable.")
    listing.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="How many to show (default 50, maximum 200).")
    listing.add_argument(
        "--after", default=None, metavar="TASK_ID",
        help="Continue from a previous page — the task id the last page printed.")
    listing.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    listing.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    listing.set_defaults(handler=cmd_remote_task)

    # `cancel` — the first verb here that ENDS somebody's work (ADR 0033 D-l, issue 19b). It gets a
    # long description for the reason `project check` does: what the verb costs, or in this case
    # frees and stops, belongs in the sentence that offers it.
    cancel = task_sub.add_parser(
        "cancel",
        help="Stop a queued or running task without ending its conversation",
        description=(
            "Stop a task that has not finished.\n\n"
            "Until this existed the only way out of a task nobody wanted any more was to wait for "
            "its deadline — an hour if it was running, and up to four if it was still waiting for "
            "a provider. A task the grid queued to resolve a collision — one nobody typed — is the "
            "usual reason to reach for it.\n\n"
            "The CONVERSATION survives. Cancelling stops this run and nothing else, so the next "
            "message you send continues where it left off — there is no way to end a conversation, "
            "because the grid holds no such state.\n\n"
            "A project is shared, so any member may cancel any task in it: the colleague whose "
            "combining step has been stuck all afternoon is often the person who needs to stop it. "
            "The event log records who did.\n\n"
            "The conversation is free to take its next message immediately. The agent itself stops "
            "within about half a minute, on the provider's next lease renewal — and on a provider "
            "that has not been updated yet it runs to completion, harmlessly, with nothing waiting "
            "on it.\n\n"
            "Nothing is undone. What the agent had reached is another matter: it is stopped "
            "part-way and may never have published anything, so `grid task fetch` gives you "
            "what the grid has recorded — which can be only what you sent in. It says which."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    cancel.add_argument(
        "task_id",
        help="Task id, from `grid task create` or `grid task list`. Not the conversation id "
             "— that one goes to `grid task send`.")
    cancel.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    cancel.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    cancel.set_defaults(handler=cmd_remote_task)

    # `diff` — what one task changed (ADR 0034 D-m, issue 45). The audit surface auto-apply created:
    # every finished task is now a release, and the promotion ledger that used to answer "what
    # landed" went with `grid project promote`.
    diff = task_sub.add_parser(
        "diff",
        help="See what one finished task changed",
        description=(
            "See exactly what one task changed in the project.\n\n"
            "Finished work reaches the project by itself, so this is how you check that what "
            "arrived is what you meant — including any files you sent with the message.\n\n"
            "A task that changed nothing says so. A task that ran long ago may no longer have its "
            "details kept, which is also an answer rather than a problem.\n\n"
            "To take a change back out, `grid task undo <task-id>`."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    diff.add_argument(
        "task_id",
        help="Task id, from `grid task create` or `grid task list`. Not the conversation id "
             "— that one goes to `grid task send`.")
    diff.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    diff.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    diff.set_defaults(handler=cmd_remote_task)

    undo = task_sub.add_parser(
        "undo",
        help="Take one finished task's change back out of the project",
        description=(
            "Undo a change you did not want.\n\n"
            "Finished work reaches the project by itself, with nobody asked to approve it — so this "
            "is how you decline afterwards. It takes out exactly what that one task changed, "
            "including any files you sent with it.\n\n"
            "Everything done since then stays. Your colleagues' work, and your own later tasks, are "
            "untouched — the project is not rewound to how it looked before, it simply no longer "
            "contains this one change.\n\n"
            "If somebody has since built on the same files, the grid cannot take the change out "
            "cleanly and will say so, naming them. Ask for what you want in a new message instead.\n\n"
            "Only the person who asked for the task, and whoever owns the project, can undo it. A "
            "task that failed, or one whose result has not appeared in the project yet, has nothing "
            "to undo.\n\n"
            "This is not the same as `grid task cancel`, which stops a task that is still running "
            "and changes nothing."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    undo.add_argument(
        "task_id",
        help="Task id, from `grid task create` or `grid task list`. Not the conversation id "
             "— that one goes to `grid task send`.")
    undo.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    undo.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    undo.set_defaults(handler=cmd_remote_task)


def _add_price(sub) -> None:
    """Remote-only `grid price set|rm|show` — this engine's authoritative model price (grid_chat_pricing).
    Gated in local mode by dispatch (`price` is in `REMOTE_ONLY`). `--type` defaults to chat; image/video are
    not priced yet (the handler rejects them). `--grid` selects the grid (active grid when omitted)."""
    price = sub.add_parser("price", help="Set or remove this engine's model price (remote)")
    price_sub = price.add_subparsers(dest="subcommand", required=True)

    pset = price_sub.add_parser("set", help="Set the price for a model this engine serves")
    pset.add_argument("-m", "--model", required=True, help="Model id (as advertised to the grid).")
    pset.add_argument(
        "--type",
        choices=("chat", "image", "video"),
        default="chat",
        help="Model type (default chat). image/video pricing isn't supported yet.",
    )
    pset.add_argument("--input", type=float, required=True, help="USD per 1M input tokens.")
    pset.add_argument("--output", type=float, required=True, help="USD per 1M output tokens.")
    pset.add_argument("--cache", type=float, default=0.0, help="USD per 1M cached input tokens (default 0).")
    # Optional model metadata recorded on the same relay endpoint; each is sent only when given.
    pset.add_argument("--name", default=None, help="Display name shown on the grid page (e.g. 'Ornith 1.0 397B').")
    pset.add_argument("--maker", default=None, help="Model maker/vendor (e.g. 'DeepReinforce AI').")
    pset.add_argument("--status", default=None, help="Model status on the grid (e.g. 'available').")
    pset.add_argument("--context-length", type=int, default=None, help="Max context length in tokens.")
    pset.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    pset.set_defaults(handler=cmd_remote_price)

    for verb, help_text in (("rm", "Remove your price for a model"), ("delete", "Alias for `grid price rm`")):
        prm = price_sub.add_parser(verb, help=help_text)
        prm.add_argument("-m", "--model", required=True, help="Model id whose price to remove.")
        prm.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
        prm.set_defaults(handler=cmd_remote_price)

    show = price_sub.add_parser("show", help="Show the grid's model prices")
    show.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    show.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    show.set_defaults(handler=cmd_remote_price)


def _add_router(sub) -> None:
    """Remote-only auto-routing config for a grid you own (model `auto`, ADR 0013, revised):
    `grid router status|enable|disable [--grid <grid>]`, `grid router models`, `grid router set-advisors
    <provider[:model]> …`, and `grid router remove-advisor <provider[:model]>`. Gated in local mode by
    dispatch (`router` is in `REMOTE_ONLY`).

    An Advisor is picked BY NAME from the platform catalog — there is NO key or URL input anywhere in this
    group. Grid selection is a uniform `--grid` FLAG on every subcommand that acts on a grid (omit for the
    active grid), matching `grid price`. The flag (not a positional `[grid]`) is forced by `set-advisors`,
    whose `nargs="+"` advisor tokens make a leading positional `[grid]` ambiguous — is the first token a
    grid or an advisor?; the other subcommands adopt it too so the whole group reads one way and a
    positional-grid habit can't silently become an advisor token. `models` takes no grid at all (it reads
    the account-level catalog)."""
    router = sub.add_parser("router", help="Configure auto-routing (model `auto`) for a grid you own")
    router_sub = router.add_subparsers(dest="subcommand", required=True)

    # status / enable / disable share the same shape (`--grid` + `--json`); build them in a loop, mirroring
    # `_add_price`'s rm/delete idiom. `--grid` (not a positional) keeps the whole group's selection uniform.
    for name, help_text in (
        ("status", "Show routing state and the advisor chain (no keys)"),
        ("enable", "Enable auto-routing on the grid"),
        ("disable", "Disable auto-routing on the grid"),
    ):
        simple = router_sub.add_parser(name, help=help_text)
        simple.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
        simple.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
        simple.set_defaults(handler=cmd_remote_router)

    # `models` lists the platform advisor catalog — account-level, needs no grid (and no grid running).
    models = router_sub.add_parser(
        "models", help="List the advisor catalog (providers + whitelisted models; no grid needed)")
    models.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    models.set_defaults(handler=cmd_remote_router)

    set_advisors = router_sub.add_parser(
        "set-advisors",
        help=f"Replace the advisor chain (up to {MAX_ADVISORS} `provider[:model]`, order = priority)",
        description=(
            f"Replace the whole advisor chain with 1-{MAX_ADVISORS} `provider[:model]` tokens, in priority "
            "order. A bare `provider` uses the catalog's default model. Advisors are picked by name from the "
            "platform catalog (`grid router models`) — there is no URL or key to supply."
        ),
    )
    set_advisors.add_argument(
        "advisors", nargs="+", metavar="provider[:model]", type=parse_advisor_token, action=AdvisorsAction,
        help=f"1-{MAX_ADVISORS} advisors in priority order, e.g. `openai:gpt-5-mini openai:gpt-4o-mini`.")
    set_advisors.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    set_advisors.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    set_advisors.set_defaults(handler=cmd_remote_router)

    remove_advisor = router_sub.add_parser(
        "remove-advisor",
        help="Remove an advisor by name (exact `provider:model`, or bare `provider` for all its entries)")
    remove_advisor.add_argument(
        "advisor", metavar="provider[:model]", type=parse_advisor_token,
        help="Exact `provider:model` removes one entry; bare `provider` removes all of its entries.")
    remove_advisor.add_argument("--grid", default=None, help="Grid to act on (default: active grid).")
    remove_advisor.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    remove_advisor.set_defaults(handler=cmd_remote_router)


def _add_engine_setup(sub) -> None:
    engine = sub.add_parser("engine", help="Set up built-in engines and list live ones")
    engine_sub = engine.add_subparsers(dest="subcommand", required=True)

    install = engine_sub.add_parser("install", help="Install an engine: llama.cpp (text) or comfyui (media)")
    install.add_argument("name", choices=("llama.cpp", "comfyui"))
    install.add_argument(
        "--from-source",
        action="store_true",
        help=(
            "llama.cpp only: build from source instead of downloading a pinned release "
            "(Metal on macOS, CUDA on Linux NVIDIA)."
        ),
    )
    install.add_argument(
        "--target-sm",
        default=None,
        help="llama.cpp on Linux NVIDIA: override the detected compute capability, e.g. sm_86.",
    )
    install.set_defaults(handler=cmd_engine_install)

    pull = engine_sub.add_parser("pull", help="Download a media model bundle (comfyui)")
    pull.add_argument("bundle", choices=VALID_MEDIA_BUNDLES)
    pull.set_defaults(handler=cmd_engine_pull)

    status = engine_sub.add_parser("status", help="Show the built-in media engine (ComfyUI) status")
    status.add_argument("--port", type=int, default=8188)
    status.set_defaults(handler=cmd_engine_status)

    start = engine_sub.add_parser("start", help="Start the built-in media engine (ComfyUI)")
    start.add_argument("--port", type=int, default=8188)
    start.add_argument(
        "--detach",
        action="store_true",
        help="Return after ComfyUI is ready instead of blocking on its lifetime.",
    )
    start.set_defaults(handler=cmd_engine_start)

    stop = engine_sub.add_parser("stop", help="Stop the built-in media engine (ComfyUI)")
    stop.add_argument("--port", type=int, default=8188)
    stop.set_defaults(handler=cmd_engine_stop)

    # `grid engine ls`/`list`: live engines joined to the grid (mode-aware, like `grid engines`).
    for verb, help_text in (("ls", "List live engines (like `grid engines`)"),
                            ("list", "Alias for `grid engine ls`")):
        engine_list = engine_sub.add_parser(verb, help=help_text)
        engine_list.add_argument("grid", nargs="?", default=None,
                                 help="Grid name or id (ag-…). Omit for the active grid.")
        engine_list.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
        engine_list.set_defaults(handler=cmd_engine_list)

    agent = sub.add_parser("agent", help="Set up the agents that run tools in chat (hermes, codex)")
    agent_sub = agent.add_subparsers(dest="subcommand", required=True)

    agent_install = agent_sub.add_parser("install", help="Install an agent (no Homebrew, no admin rights)")
    agent_install.add_argument("name", choices=("hermes", "codex"))
    agent_install.add_argument(
        "--force",
        action="store_true",
        help="Reinstall (or upgrade) even when the agent is already present.",
    )
    agent_install.set_defaults(handler=cmd_agent_install)

    agent_status = agent_sub.add_parser("status", help="Show whether the agent is installed")
    agent_status.set_defaults(handler=cmd_agent_status)


def _add_media_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--grid", default=None)
    parser.add_argument("--timeout", type=int, default=1800, help="Seconds to wait for the streamed result.")
    parser.add_argument(
        "-o",
        "--output-dir",
        default=None,
        help="Directory for returned media files. Defaults to ~/.grid/outputs.",
    )


def _add_remote_use_flags(parser: argparse.ArgumentParser) -> None:
    """Remote-only request-routing flags shared by chat/image/edit/video (DECISIONS D16). Declared on
    the unified parser; the local handlers reject them (cli/request.py) since the concept is remote-only.
    ``--target-provider`` defaults to ``None`` and ``--allow-self-provider`` to ``False`` so a wrong-mode
    use is detectable."""
    parser.add_argument(
        "--target-provider",
        default=None,
        help="Remote only: pin this request to a specific engine by id.",
    )
    parser.add_argument(
        "--allow-self-provider",
        action="store_true",
        help="Remote only: let your own engine serve this request.",
    )
def _add_launch(sub) -> None:
    launch = sub.add_parser(
        "launch",
        help="Start an app on your grid (claude)",
        # The separator is invisible in a usage line, and the one thing it cannot carry is invisible
        # everywhere — so both are spelled out here, which is the only place a user goes looking.
        epilog=(
            "Anything after `--` is passed to the app unchanged:\n"
            "  grid launch claude -- --continue\n"
            "  grid launch claude -- -p 'summarise this repo'\n"
            "\n"
            "Two words are the exception: `--local` and `--remote` are this CLI's one-shot mode\n"
            "override and are removed from anywhere on the command line, so they cannot be\n"
            "forwarded to the app — a warning says so when one is taken. `-- --local` also\n"
            "switches this command to local mode, where it refuses, so it is reported as a mode\n"
            "error rather than as a missing argument."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Two optional positionals, `target` first: argparse fills them left to right, so
    # `grid launch claude` is target=claude/grid=None and `grid launch claude team` names both.
    # `grid` matches info/models/engines verbatim — a name or id, omitted for the active grid.
    launch.add_argument("target", nargs="?", default=None,
                        help="Launch target. Omit to list what can be launched.")
    launch.add_argument("grid", nargs="?", default=None,
                        help="Grid name or id (ag-…). Omit for the active grid.")
    launch.add_argument(
        "--print-env",
        action="store_true",
        help="Print the environment as shell exports instead of starting the app. "
             "Prints your grid's access token.",
    )
    # `forward` is never parsed from here — `cli.dispatch.split_forwarded` takes everything after the
    # first `--` out of argv before this parser runs (argparse would bind the app's own flags to the
    # two positionals above). The default is what makes `grid launch claude --`, with nothing after
    # it, identical to no `--` at all.
    launch.set_defaults(handler=cmd_launch, forward=())


def _add_train(sub) -> None:
    from .train import (
        cmd_train_autopilot,
        cmd_train_collect,
        cmd_train_convert,
        cmd_train_deploy,
        cmd_train_doctor,
        cmd_train_eval,
        cmd_train_init,
        cmd_train_nightly,
        cmd_train_outcomes,
        cmd_train_packs,
        cmd_train_pull,
        cmd_train_run,
        cmd_train_schedule,
        cmd_train_serve,
        cmd_train_sft,
        cmd_train_ui,
        cmd_train_web,
        cmd_train_where,
    )

    train = sub.add_parser("train", help="RL fine-tuning served by your grid (ADR 0019)")
    train_sub = train.add_subparsers(dest="subcommand", required=True)

    init = train_sub.add_parser("init", help="Write a starter grid-train.toml (or install a pack)")
    init.add_argument("--config", default=None, help="Path to write (default: ./grid-train.toml)")
    init.add_argument("--force", action="store_true", help="Overwrite an existing file.")
    init.add_argument(
        "--pack",
        default=None,
        help="Install a task pack instead (see `grid train packs`), e.g. support-replies.",
    )
    init.add_argument("--dest", default=None, help="Directory for --pack (default: ./<pack>/)")
    init.set_defaults(handler=cmd_train_init)

    packs = train_sub.add_parser("packs", help="List bundled task packs for business data")
    packs.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    packs.set_defaults(handler=cmd_train_packs)

    ui = train_sub.add_parser("ui", help="Local dashboard: runs, reward/eval curves (read-only)")
    ui.add_argument("--port", type=int, default=8321)
    ui.set_defaults(handler=cmd_train_ui)

    web = train_sub.add_parser(
        "web", help="Open the point-and-click interface (for non-engineers)"
    )
    web.add_argument("--port", type=int, default=8322)
    web.add_argument("--host", default="127.0.0.1",
                     help="0.0.0.0 to let colleagues on your network use it — it then prints a "
                          "link with a code in it, and only that link works.")
    web.set_defaults(handler=cmd_train_web)

    serve = train_sub.add_parser(
        "serve", help="Run this Mac as a rollout node (MLX; serves the training contract)"
    )
    serve.add_argument("--model", default="mlx-community/SmolLM2-135M-Instruct")
    serve.add_argument("--adapter-path", default=None, help="LoRA adapter dir (mlx_lm format)")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8080)
    serve.set_defaults(handler=cmd_train_serve)

    convert = train_sub.add_parser(
        "convert-adapter", help="Convert a LoRA adapter between torch/peft and MLX formats"
    )
    convert.add_argument("source", help="Adapter directory to read")
    convert.add_argument("dest", help="Directory to write")
    convert.add_argument("--to", choices=("mlx", "peft"), default=None,
                         help="Target format (default: the one the source is not)")
    convert.set_defaults(handler=cmd_train_convert)

    doctor = train_sub.add_parser(
        "doctor", help="Readiness check: deps, rollout endpoint, data/rewards"
    )
    doctor.add_argument("--config", default=None, help="Run config (default: ./grid-train.toml)")
    doctor.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    doctor.set_defaults(handler=cmd_train_doctor)

    run = train_sub.add_parser("run", help="Run the training climb (GRPO)")
    run.add_argument("--config", default=None, help="Run config (default: ./grid-train.toml)")
    # Same flag the imitation stage has taken all along. Its absence here is what left Apple
    # Silicon able to do stage one and not stage two, with a working MLX GRPO loop in the tree.
    run.add_argument("--backend", choices=("auto", "mlx", "torch"), default="auto",
                     help="auto picks MLX on Apple Silicon, torch elsewhere.")
    run.add_argument("--steps", type=int, default=None, help="Override [trainer].steps.")
    run.set_defaults(handler=cmd_train_run)

    collect = train_sub.add_parser(
        "collect", help="Learn from the work the grid already does (off until you turn it on)"
    )
    collect.add_argument("--on", action="store_true", help="Start keeping served requests.")
    collect.add_argument("--off", action="store_true", help="Stop keeping them.")
    collect.add_argument("--teacher", action="append", default=[],
                         help="A model whose answers count as teaching examples (repeatable).")
    collect.add_argument("--retain-days", type=int, default=0, help="How long to keep them.")
    collect.add_argument("--sample", type=float, default=None,
                         help="Fraction of requests to keep (1.0 = all).")
    collect.add_argument("--no-redact", action="store_true",
                         help="Store text as-is instead of scrubbing emails/phones/cards.")
    collect.add_argument("--prune", action="store_true", help="Delete files past the window now.")
    collect.add_argument("--days", type=int, default=30, help="Window to summarise.")
    collect.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    collect.set_defaults(handler=cmd_train_collect)

    auto = train_sub.add_parser(
        "autopilot", help="Improve a model from captured work, unattended (see `schedule`)"
    )
    auto.add_argument("--config", default=None, help="Run config (default: ./grid-train.toml)")
    auto.add_argument("--days", type=int, default=30, help="How far back to draw examples from.")
    auto.add_argument("--min-examples", type=int, default=120,
                      help="Refuse to train on less than this.")
    auto.add_argument("--stage", choices=("auto", "sft", "rl"), default="auto",
                      help="auto = imitate the corrections (needs no rollout engine).")
    auto.add_argument("--no-deploy", action="store_true",
                      help="Prove it but don't serve it. (It is still loaded under a checking "
                           "name — that is the only way to score it.)")
    auto.add_argument("--ignore-host", action="store_true",
                      help="Run even on battery or while the machine is in use.")
    auto.add_argument("--history", action="store_true", help="Show recent cycles instead.")
    auto.set_defaults(handler=cmd_train_autopilot)

    sft = train_sub.add_parser(
        "sft", help="Stage one: learn from the replies your team already wrote (works on a Mac)"
    )
    sft.add_argument("--config", default=None, help="Run config (default: ./grid-train.toml)")
    sft.add_argument("--backend", choices=("auto", "mlx", "torch"), default="auto",
                     help="auto picks MLX on Apple Silicon, torch elsewhere.")
    sft.add_argument("--iters", type=int, default=None, help="Training iterations (MLX path).")
    sft.add_argument("--run-dir", default=None,
                     help="Where to write adapter/log/run.json (default: a timestamped folder).")
    sft.set_defaults(handler=cmd_train_sft)

    nightly = train_sub.add_parser(
        "nightly", help="One unattended cycle: train, prove it, ship it only if it won"
    )
    nightly.add_argument("--config", default=None, help="Run config (default: ./grid-train.toml)")
    nightly.add_argument("--no-deploy", action="store_true",
                         help="Train and prove it, but don't serve it even on a pass. (It is "
                              "still loaded under a checking name so it can be scored.)")
    nightly.add_argument("--ignore-host", action="store_true",
                         help="Train even on battery or while the machine is in use.")
    nightly.add_argument("--history", action="store_true", help="Show recent nights instead.")
    nightly.set_defaults(handler=cmd_train_nightly)

    pull = train_sub.add_parser(
        "pull", help="Pull examples straight from Zendesk or HubSpot into a local file"
    )
    pull.add_argument("source", choices=("zendesk", "hubspot"))
    pull.add_argument("--out", default=None, help="Where to write (default: <source>-export.jsonl)")
    pull.add_argument("--subdomain", default=None, help="Zendesk: the bit before .zendesk.com")
    pull.add_argument("--email", default=None, help="Zendesk: the account email for the API token")
    pull.add_argument("--max-rows", type=int, default=5000, help="Stop after this many rows.")
    pull.add_argument("--status", default="solved",
                      help="Zendesk: which tickets to take (default solved).")
    pull.set_defaults(handler=cmd_train_pull)

    outcomes = train_sub.add_parser(
        "outcomes", help="Ask the helpdesk what actually happened, and record it as feedback"
    )
    outcomes.add_argument("source", choices=("zendesk",))
    outcomes.add_argument("--subdomain", default=None, help="Zendesk: the bit before .zendesk.com")
    outcomes.add_argument("--email", default=None, help="Zendesk: the account email for the token")
    outcomes.add_argument("--days", type=int, default=7,
                          help="How far back to look for answers to judge.")
    outcomes.add_argument("--dry-run", action="store_true",
                          help="Say what it would record, and record nothing.")
    outcomes.set_defaults(handler=cmd_train_outcomes)

    schedule = train_sub.add_parser(
        "schedule", help="Run the nightly cycle automatically (launchd on macOS, systemd on Linux)"
    )
    schedule.add_argument("action", nargs="?", choices=("status", "on", "off"), default="status",
                          help="status (default), on = install it, off = remove it.")
    schedule.add_argument("--at", default="23:00", help="Time of day to run, HH:MM (default 23:00).")
    schedule.add_argument("--config", default=None, help="Run config (default: ./grid-train.toml)")
    schedule.add_argument("--name", default=None,
                          help="Label, if this machine schedules more than one model.")
    schedule.set_defaults(handler=cmd_train_schedule)

    where = train_sub.add_parser("where", help="Which grids training can use (LAN and hosted)")
    where.set_defaults(handler=cmd_train_where)

    ev = train_sub.add_parser(
        "eval", help="Score a trained model against the one you serve, on held-out work"
    )
    ev.add_argument("--run", required=True, help="Run directory under ~/.grid/artifacts/train/")
    ev.add_argument("--candidate", required=True,
                    help="The name customers use for this model (what the winner will serve as)")
    ev.add_argument("--adapter", default=None,
                    help="The trained adapter to check (default: <run>/adapter). It is loaded "
                         "under a checking name AFTER the incumbent is scored, so the comparison "
                         "is between two different models rather than one model twice.")
    ev.add_argument("--base", default=None, help="Incumbent model name (default: the config's model)")
    ev.add_argument("--config", default=None, help="Run config (default: ./grid-train.toml)")
    ev.set_defaults(handler=cmd_train_eval)

    deploy = train_sub.add_parser("deploy", help="Hot-load a trained adapter onto serving nodes")
    deploy.add_argument(
        "--gate",
        action="store_true",
        help="Refuse to deploy unless it beats the model you already serve on held-out work.",
    )
    deploy.add_argument("--run", default=None, help="Run directory for --gate (default: the adapter's parent)")
    deploy.add_argument("--adapter", required=True, help="Adapter directory (contains adapter_config.json)")
    deploy.add_argument("--node", action="append", help="Serving node /v1 root (repeatable).")
    deploy.add_argument("--name", default=None, help="Adapter name to serve under.")
    deploy.add_argument("--config", default=None, help="Fill nodes/name from a run config.")
    deploy.set_defaults(handler=cmd_train_deploy)


def _add_stt(sub) -> None:
    stt = sub.add_parser("stt", help="Speech-to-text (voice input)")
    stt_sub = stt.add_subparsers(dest="subcommand", required=True)

    transcribe = stt_sub.add_parser("transcribe", help="Transcribe a short audio clip to text")
    transcribe.add_argument("audio", help="Path to the audio file (WAV) to transcribe.")
    transcribe.add_argument(
        "--lang", default="en",
        help="Recognition language hint, 'en' or 'vi' (default: en).",
    )
    transcribe.add_argument("--timeout", type=float, default=30.0)
    transcribe.add_argument("--json", action="store_true", help="Print the full JSON response.")
    transcribe.set_defaults(handler=cmd_stt_transcribe)
