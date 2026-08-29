"""Dynamic model placement and host-protection primitives for Grid.

The package is deliberately split between deterministic, side-effect-free planning code and the
small integration layers that collect telemetry or execute a plan.  Nothing in the hot request path
needs an optimizer or an LLM.
"""

from shared.allocator.models import (
    ActionKind,
    AllocatorMode,
    DemandForecast,
    ModelPerformance,
    ModelProfile,
    ModelResidency,
    MutationAction,
    NodeSnapshot,
    NodeState,
    PlacementAssignment,
    PlacementPlan,
    PlacementPreemption,
    ResidencyState,
    UnsatisfiedConstraint,
)
from shared.allocator.intelligence import (
    KNOWN_WORKLOADS,
    ModelWorkloadOutcome,
    RequestFeatures,
    WorkloadIntelligence,
    classify_request,
)

__all__ = [
    "ActionKind",
    "AllocatorMode",
    "DemandForecast",
    "ModelPerformance",
    "ModelProfile",
    "ModelResidency",
    "MutationAction",
    "NodeSnapshot",
    "NodeState",
    "PlacementAssignment",
    "PlacementPlan",
    "PlacementPreemption",
    "ResidencyState",
    "UnsatisfiedConstraint",
    "KNOWN_WORKLOADS",
    "ModelWorkloadOutcome",
    "RequestFeatures",
    "WorkloadIntelligence",
    "classify_request",
]
