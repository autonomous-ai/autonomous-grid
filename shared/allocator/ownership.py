"""Per-model allocator ownership audit for mixed provider unions."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping


@dataclass(frozen=True, slots=True)
class OwnershipAudit:
    rows: tuple[dict[str, Any], ...]
    summary: dict[str, int]
    requirements: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return all(bool(item["passed"]) for item in self.requirements)

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, **asdict(self)}


def audit_ownership(
    allocator_status: Mapping[str, Any],
    *,
    require_managed: Iterable[str] = (),
    forbid_external: Iterable[str] = (),
) -> OwnershipAudit:
    """Flatten allocator status without allowing host aggregation to hide model ownership."""

    desired = {
        (str(item.get("node_id") or ""), str(item.get("model_id") or ""))
        for item in ((allocator_status.get("plan") or {}).get("assignments") or ())
        if isinstance(item, Mapping)
    }
    profiles = {
        str(item.get("model_id") or "")
        for item in allocator_status.get("models") or ()
        if isinstance(item, Mapping)
    }
    rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    by_model: dict[str, Counter[str]] = {}
    warnings: list[str] = []
    for node in allocator_status.get("nodes") or ():
        if not isinstance(node, Mapping):
            continue
        node_id = str(node.get("node_id") or "")
        host_manual = bool(node.get("manually_managed", False))
        for residency in node.get("residencies") or ():
            if not isinstance(residency, Mapping):
                continue
            model_id = str(residency.get("model_id") or "")
            if not node_id or not model_id:
                continue
            managed = bool(residency.get("managed", not host_manual))
            owner = "allocator" if managed else "external"
            state = str(residency.get("state") or "unknown")
            row = {
                "model_id": model_id,
                "node_id": node_id,
                "runtime": str(residency.get("runtime") or "unknown"),
                "state": state,
                "owner": owner,
                "desired": (node_id, model_id) in desired,
                "profiled": model_id in profiles,
                "artifact_sha256": str(residency.get("artifact_sha256") or ""),
            }
            rows.append(row)
            counts[f"{owner}_{state}"] += 1
            model_counts = by_model.setdefault(model_id, Counter())
            model_counts[f"{owner}_{state}"] += 1
            if owner == "external" and state == "ready" and not row["profiled"]:
                warnings.append(
                    f"{model_id}@{node_id} is routable external inventory with no allocator profile"
                )

    for model_id, model_counts in sorted(by_model.items()):
        if model_counts["allocator_ready"] and model_counts["external_ready"]:
            warnings.append(
                f"{model_id} has both allocator-owned and external ready routes; verify cutover intent"
            )

    requirements: list[dict[str, Any]] = []
    for model_id in sorted({str(item) for item in require_managed if str(item)}):
        model_counts = by_model.get(model_id, Counter())
        managed = model_counts["allocator_ready"]
        external = model_counts["external_ready"]
        requirements.append(
            {
                "kind": "require-managed",
                "model_id": model_id,
                "passed": managed > 0 and external == 0,
                "managed_ready_replicas": managed,
                "external_ready_replicas": external,
                "reason": (
                    "only allocator-owned ready routes are visible"
                    if managed > 0 and external == 0
                    else "requires at least one allocator-owned ready route and zero external ready routes"
                ),
            }
        )
    for model_id in sorted({str(item) for item in forbid_external if str(item)}):
        model_counts = by_model.get(model_id, Counter())
        external = model_counts["external_ready"]
        requirements.append(
            {
                "kind": "forbid-external",
                "model_id": model_id,
                "passed": external == 0,
                "managed_ready_replicas": model_counts["allocator_ready"],
                "external_ready_replicas": external,
                "reason": (
                    "no external ready routes are visible"
                    if external == 0
                    else "external ready routes must be drained before cutover"
                ),
            }
        )

    return OwnershipAudit(
        rows=tuple(sorted(rows, key=lambda item: (item["model_id"], item["node_id"], item["owner"]))),
        summary=dict(sorted(counts.items())),
        requirements=tuple(requirements),
        warnings=tuple(sorted(set(warnings))),
    )
