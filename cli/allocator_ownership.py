"""Operator-facing allocator ownership audit."""

from __future__ import annotations

import argparse
import json

from local import config
from shared.allocator.ownership import audit_ownership

from .allocator import _request


def cmd_allocator_audit(args: argparse.Namespace) -> int:
    cfg = config.select_grid(getattr(args, "grid", None))
    status = _request(cfg, "GET", "/allocator/status")
    audit = audit_ownership(
        status,
        require_managed=args.require_managed,
        forbid_external=args.forbid_external,
    )
    if args.json:
        print(json.dumps(audit.to_dict(), indent=2))
    else:
        print(
            f"Allocator ownership audit · {len(audit.rows)} residencies · "
            f"{'PASS' if audit.passed else 'ACTION REQUIRED'}"
        )
        for row in audit.rows:
            marker = "managed" if row["owner"] == "allocator" else "EXTERNAL"
            desired = " · desired" if row["desired"] else ""
            profiled = "" if row["profiled"] else " · no profile"
            print(
                f"  {row['model_id']:<34} {row['node_id']:<24} "
                f"{row['runtime']:<10} {row['state']:<9} {marker}{desired}{profiled}"
            )
        if audit.warnings:
            print("\nWarnings")
            for warning in audit.warnings:
                print(f"  - {warning}")
        if audit.requirements:
            print("\nCutover gates")
            for item in audit.requirements:
                requirement = (
                    "managed route"
                    if item["kind"] == "require-managed"
                    else "external route absent"
                )
                print(
                    f"  {'PASS' if item['passed'] else 'FAIL'} {item['model_id']} "
                    f"({requirement}): "
                    f"{item['managed_ready_replicas']} managed ready, "
                    f"{item['external_ready_replicas']} external ready"
                )
    return 0 if audit.passed else 1
