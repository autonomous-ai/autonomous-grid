"""Privacy-preserving request lifecycle intelligence for autonomous placement.

Classification is deliberately local, deterministic, bounded, and advisory. Raw request and
response content never enters the allocator's durable state. The resulting workload forecasts are
portfolio-planning evidence; they are not request-routing decisions.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import asdict, dataclass, replace
from typing import Any, Iterable, Mapping

from shared.allocator.demand import DemandTracker
from shared.allocator.models import (
    MAX_COUNTER,
    SCHEMA_VERSION,
    DemandForecast,
    ModelProfile,
    canonical_sha256,
)

GENERAL = "general"
KNOWN_WORKLOADS = frozenset(
    {
        GENERAL,
        "coding",
        "research",
        "marketing",
        "sales",
        "design",
        "image",
        "video",
        "embedding",
    }
)
_MAX_CLASSIFICATION_CHARS = 32_768
_MAX_FEATURE_UNITS = 1_000_000_000
_TOKEN = re.compile(r"[a-z0-9_+#.-]+")
_OUTCOME_HALF_LIFE_SECONDS = 7 * 24 * 60 * 60.0
_OUTCOME_FULL_CONFIDENCE_REQUESTS = 20.0
_QUALITY_FULL_CONFIDENCE_SAMPLES = 8.0
_MAX_EXPLORATION_BONUS = 0.06
_KEYWORDS = {
    "coding": frozenset(
        {
            "api",
            "bug",
            "code",
            "commit",
            "compile",
            "debug",
            "function",
            "git",
            "javascript",
            "python",
            "refactor",
            "repository",
            "sql",
            "test",
            "typescript",
        }
    ),
    "research": frozenset(
        {
            "analyze",
            "citation",
            "compare",
            "evidence",
            "literature",
            "paper",
            "research",
            "source",
            "study",
            "survey",
            "verify",
        }
    ),
    "marketing": frozenset(
        {
            "ad",
            "audience",
            "brand",
            "campaign",
            "content",
            "copy",
            "headline",
            "marketing",
            "positioning",
            "seo",
            "social",
        }
    ),
    "sales": frozenset(
        {
            "account",
            "crm",
            "customer",
            "deal",
            "lead",
            "objection",
            "outreach",
            "pipeline",
            "prospect",
            "sales",
        }
    ),
    "design": frozenset(
        {
            "design",
            "figma",
            "layout",
            "mockup",
            "prototype",
            "style",
            "typography",
            "ui",
            "ux",
            "wireframe",
        }
    ),
}


@dataclass(frozen=True, slots=True)
class RequestFeatures:
    """Bounded facts extracted transiently from one request."""

    endpoint: str
    requested_model: str
    workload: str = GENERAL
    modalities: tuple[str, ...] = ("text",)
    input_units: int = 0
    requested_output_units: int = 0
    is_evaluation: bool = False

    def __post_init__(self) -> None:
        if not self.endpoint or len(self.endpoint) > 256:
            raise ValueError("endpoint is required and must be bounded")
        if len(self.requested_model) > 1_024:
            raise ValueError("requested_model is too long")
        if self.workload not in KNOWN_WORKLOADS:
            raise ValueError(f"unknown workload {self.workload!r}")
        if not 0 <= self.input_units <= _MAX_FEATURE_UNITS:
            raise ValueError("input_units is outside the supported range")
        if not 0 <= self.requested_output_units <= _MAX_FEATURE_UNITS:
            raise ValueError("requested_output_units is outside the supported range")
        modalities = tuple(sorted({str(item) for item in self.modalities if str(item)}))
        if not modalities or any(len(item) > 64 for item in modalities):
            raise ValueError("modalities must contain bounded names")
        object.__setattr__(self, "modalities", modalities)
        if not isinstance(self.is_evaluation, bool):
            raise ValueError("is_evaluation must be a boolean")


@dataclass(frozen=True, slots=True)
class ModelWorkloadOutcome:
    model_id: str
    workload: str
    artifact_sha256: str = ""
    requests: int = 0
    errors: int = 0
    latency_ms: float = 0.0
    output_units: float = 0.0
    quality: float = 0.0
    quality_samples: int = 0
    # Exponentially decayed sample mass is kept separately from lifetime counters.  Lifetime
    # counters are useful diagnostics, but they must not regain decision confidence when one fresh
    # request arrives after a long idle period.
    service_evidence: float = 0.0
    error_evidence: float = 0.0
    quality_evidence: float = 0.0
    service_updated_at: float = 0.0
    quality_updated_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.model_id or self.workload not in KNOWN_WORKLOADS:
            raise ValueError("model_id and known workload are required")
        object.__setattr__(
            self, "artifact_sha256", canonical_sha256(self.artifact_sha256)
        )
        if not 0 <= self.errors <= self.requests <= MAX_COUNTER:
            raise ValueError("outcome counters are invalid")
        if not 0 <= self.quality_samples <= self.requests:
            raise ValueError("quality sample count is invalid")
        for name in (
            "latency_ms",
            "output_units",
            "quality",
            "service_evidence",
            "error_evidence",
            "quality_evidence",
            "service_updated_at",
            "quality_updated_at",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.quality > 1:
            raise ValueError("quality cannot exceed 1")
        if self.service_evidence > MAX_COUNTER:
            raise ValueError("service evidence is outside the supported range")
        if not 0 <= self.error_evidence <= self.service_evidence:
            raise ValueError("error evidence cannot exceed service evidence")
        # Quality has its own timestamp because latency-only traffic must not refresh benchmark
        # evidence. Its stored mass can therefore exceed service mass while being much older.
        if not 0 <= self.quality_evidence <= MAX_COUNTER:
            raise ValueError("quality evidence is outside the supported range")


class WorkloadIntelligence:
    """Learns workload pressure and model outcomes without retaining content."""

    def __init__(
        self,
        *,
        portfolio_min_samples: int = 3,
        portfolio_min_offered_concurrency: float = 1.5,
    ) -> None:
        if (
            isinstance(portfolio_min_samples, bool)
            or not isinstance(portfolio_min_samples, int)
            or portfolio_min_samples < 1
        ):
            raise ValueError("portfolio_min_samples must be positive")
        if (
            isinstance(portfolio_min_offered_concurrency, bool)
            or not math.isfinite(portfolio_min_offered_concurrency)
            or portfolio_min_offered_concurrency <= 0
        ):
            raise ValueError(
                "portfolio_min_offered_concurrency must be finite and positive"
            )
        self.portfolio_min_samples = int(portfolio_min_samples)
        self.portfolio_min_offered_concurrency = float(
            portfolio_min_offered_concurrency
        )
        self.demand = DemandTracker()
        self.unbound_demand = DemandTracker()
        self._outcomes: dict[tuple[str, str, str], ModelWorkloadOutcome] = {}

    @property
    def outcomes(self) -> tuple[ModelWorkloadOutcome, ...]:
        return tuple(self._outcomes[key] for key in sorted(self._outcomes))

    def observe(
        self,
        features: RequestFeatures,
        *,
        served_model: str = "",
        served_artifact_sha256: str = "",
        portfolio_unbound: bool = False,
        service_seconds: float = 0.0,
        latency_ms: float | None = None,
        queue_depth: int = 0,
        error: bool = False,
        output_units: int = 0,
        quality: float | None = None,
        timestamp: float | None = None,
    ) -> None:
        observed_at = time.time() if timestamp is None else float(timestamp)
        measured_latency = (
            float(service_seconds) * 1_000.0
            if latency_ms is None
            else float(latency_ms)
        )
        self.demand.observe(
            features.workload,
            service_seconds=service_seconds,
            latency_ms=measured_latency,
            queue_depth=queue_depth,
            errors=int(error),
            timestamp=observed_at,
        )
        if portfolio_unbound:
            self.unbound_demand.observe(
                features.workload,
                service_seconds=service_seconds,
                latency_ms=measured_latency,
                queue_depth=queue_depth,
                errors=int(error),
                timestamp=observed_at,
            )
        if served_model:
            self._observe_outcome(
                served_model,
                features.workload,
                artifact_sha256=served_artifact_sha256,
                error=error,
                latency_ms=measured_latency,
                output_units=output_units,
                quality=quality,
                timestamp=observed_at,
            )

    def workload_forecasts(
        self, *, now: float | None = None
    ) -> tuple[DemandForecast, ...]:
        timestamp = time.time() if now is None else float(now)
        keys = tuple(sorted((self.demand.to_dict().get("models") or {}).keys()))
        return tuple(self.demand.forecast(key, now=timestamp) for key in keys)

    def portfolio_evidence_ready(
        self,
        sample_count: int,
        offered_concurrency: float,
    ) -> bool:
        """Admit repeated cheap work or one replica-equivalent of device pressure."""

        return bool(
            sample_count >= self.portfolio_min_samples
            or offered_concurrency >= self.portfolio_min_offered_concurrency
        )

    def clear(self) -> None:
        """Clear learned demand and outcomes for a repeatable development simulation."""

        self.demand.clear()
        self.unbound_demand.clear()
        self._outcomes.clear()

    def observe_model_evaluation(
        self,
        model_id: str,
        workload: str,
        *,
        artifact_sha256: str = "",
        quality: float,
        error: bool = False,
        latency_ms: float = 0.0,
        output_units: int = 0,
        timestamp: float | None = None,
    ) -> ModelWorkloadOutcome:
        """Record a bounded canary/evaluation outcome without manufacturing live demand.

        Offline or shadow evaluations answer a different question from production lifecycle
        telemetry: how well did a candidate solve a known task?  Feeding them through ``observe``
        would also increment demand and could cause the benchmark itself to scale the model it is
        judging.  Keep quality evidence separate while reusing the same EWMA outcome record used by
        portfolio selection.
        """

        if not model_id or len(model_id) > 1_024:
            raise ValueError("model_id is required and must be bounded")
        if workload not in KNOWN_WORKLOADS:
            raise ValueError(f"unknown workload {workload!r}")
        observed_at = time.time() if timestamp is None else float(timestamp)
        if not math.isfinite(observed_at) or observed_at < 0:
            raise ValueError("timestamp must be finite and non-negative")
        self._observe_outcome(
            model_id,
            workload,
            artifact_sha256=artifact_sha256,
            error=bool(error),
            latency_ms=float(latency_ms),
            output_units=output_units,
            quality=float(quality),
            timestamp=observed_at,
        )
        return self._outcomes[(model_id, workload, canonical_sha256(artifact_sha256))]

    def portfolio_forecasts(
        self,
        profiles: Iterable[ModelProfile],
        direct: Iterable[DemandForecast],
        *,
        now: float,
        placement_hints: Mapping[str, Mapping[str, Any]] | None = None,
        chosen_models: Mapping[str, str] | None = None,
    ) -> tuple[DemandForecast, ...]:
        """Project unbound workload demand onto a bounded speculative model portfolio.

        Selection occurs once per planning tick over aggregate demand. The projected lineage is
        marked correlation-only so the existing planner may use spare capacity for a canary but
        cannot evict directly demanded work.
        """

        profile_list = tuple(profiles)
        merged = {item.model_id: item for item in direct}
        workload_keys = tuple(
            sorted((self.unbound_demand.to_dict().get("models") or {}).keys())
        )
        for workload in workload_keys:
            forecast = self.unbound_demand.forecast(workload, now=now)
            if not self.portfolio_evidence_ready(
                forecast.sample_count,
                forecast.offered_concurrency,
            ) or forecast.requests_per_minute <= 0:
                continue
            candidates = [
                profile
                for profile in profile_list
                if profile.max_replicas > 0
                and profile.workload_score(workload) > 0
                and _placement_feasible(profile.model_id, placement_hints)
            ]
            if not candidates:
                continue
            forced_model_id = (
                chosen_models.get(workload) if chosen_models is not None else None
            )
            if forced_model_id is not None:
                chosen = next(
                    (
                        profile
                        for profile in candidates
                        if profile.model_id == forced_model_id
                    ),
                    None,
                )
                if chosen is None:
                    continue
            else:
                chosen = max(
                    candidates,
                    key=lambda profile: (
                        self._portfolio_score_with_placement(
                            profile,
                            workload,
                            placement_hints,
                            now=now,
                        ),
                        -profile.maximum_memory_mb,
                        -(profile.load_seconds + profile.warm_seconds),
                        profile.model_id,
                    ),
                )
            projected = replace(
                forecast,
                model_id=chosen.model_id,
                # Workload-level congestion proves that capacity is needed, but it does not prove
                # this counterfactual model is slow or failing. Model-specific pressure begins only
                # after the canary actually serves work; retaining these fields would promote a
                # speculative projection into the planner's direct-evidence urgency tier.
                queue_depth=0,
                p95_latency_ms=0.0,
                error_rate=0.0,
                observed_requests_per_minute=0.0,
                correlated_requests_per_minute=forecast.requests_per_minute,
                correlation_confidence=forecast.confidence,
                correlation_sources=(f"workload:{workload}",),
            )
            merged[chosen.model_id] = _merge_forecasts(
                merged.get(chosen.model_id), projected
            )
        return tuple(merged[key] for key in sorted(merged))

    def projections(
        self,
        profiles: Iterable[ModelProfile],
        *,
        now: float | None = None,
        placement_hints: Mapping[str, Mapping[str, Any]] | None = None,
        chosen_models: Mapping[str, str] | None = None,
    ) -> tuple[dict[str, Any], ...]:
        timestamp = time.time() if now is None else float(now)
        rows: list[dict[str, Any]] = []
        profile_list = tuple(profiles)
        keys = tuple(sorted((self.unbound_demand.to_dict().get("models") or {}).keys()))
        for workload in keys:
            forecast = self.unbound_demand.forecast(workload, now=timestamp)
            configured_candidates = [
                profile
                for profile in profile_list
                if profile.max_replicas > 0 and profile.workload_score(workload) > 0
            ]
            candidates = [
                profile
                for profile in configured_candidates
                if _placement_feasible(profile.model_id, placement_hints)
            ]
            if not candidates:
                rows.append(
                    {
                        "workload": workload,
                        "requests_per_minute": forecast.requests_per_minute,
                        "offered_concurrency": forecast.offered_concurrency,
                        "samples": forecast.sample_count,
                        "chosen_model": "",
                        "reason": (
                            "no fleet-feasible configured model"
                            if configured_candidates and placement_hints is not None
                            else "no compatible configured model"
                        ),
                        "candidates": _candidate_rows(
                            configured_candidates,
                            workload,
                            placement_hints,
                            self,
                            now=timestamp,
                        ),
                    }
                )
                continue
            forced_model_id = (
                chosen_models.get(workload) if chosen_models is not None else None
            )
            if forced_model_id is not None:
                chosen = next(
                    (
                        profile
                        for profile in candidates
                        if profile.model_id == forced_model_id
                    ),
                    None,
                )
                if chosen is None:
                    rows.append(
                        {
                            "workload": workload,
                            "requests_per_minute": forecast.requests_per_minute,
                            "samples": forecast.sample_count,
                            "chosen_model": "",
                            "reason": "joint portfolio selection is no longer fleet-feasible",
                            "candidates": _candidate_rows(
                                configured_candidates,
                                workload,
                                placement_hints,
                                self,
                                now=timestamp,
                            ),
                        }
                    )
                    continue
            else:
                chosen = max(
                    candidates,
                    key=lambda profile: (
                        self._portfolio_score_with_placement(
                            profile,
                            workload,
                            placement_hints,
                            now=timestamp,
                        ),
                        -profile.maximum_memory_mb,
                        profile.model_id,
                    ),
                )
            chosen_evidence = self._outcome_evidence(
                chosen.model_id,
                workload,
                artifact_sha256=chosen.artifact_sha256,
                now=timestamp,
            )
            rows.append(
                {
                    "workload": workload,
                    "requests_per_minute": forecast.requests_per_minute,
                    "offered_concurrency": forecast.offered_concurrency,
                    "samples": forecast.sample_count,
                    "confidence": forecast.confidence,
                    "chosen_model": chosen.model_id,
                    "score": self._portfolio_score_with_placement(
                        chosen,
                        workload,
                        placement_hints,
                        now=timestamp,
                    ),
                    "placement": dict(
                        (placement_hints or {}).get(chosen.model_id) or {}
                    ),
                    "candidates": _candidate_rows(
                        configured_candidates,
                        workload,
                        placement_hints,
                        self,
                        now=timestamp,
                    ),
                    "reason": (
                        (
                            "confidence-aware canary; "
                            f"{chosen_evidence['effective_requests']:.1f} effective samples"
                        )
                        if chosen_evidence["exploration_bonus"] > 0
                        else "evidence-backed portfolio choice"
                    )
                    + (
                        "; joint fleet optimization"
                        if chosen_models is not None
                        else ""
                    ),
                }
            )
        return tuple(rows)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "portfolio_min_samples": self.portfolio_min_samples,
            "portfolio_min_offered_concurrency": (
                self.portfolio_min_offered_concurrency
            ),
            "demand": self.demand.to_dict(),
            "unbound_demand": self.unbound_demand.to_dict(),
            "outcomes": [asdict(item) for item in self.outcomes],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> WorkloadIntelligence:
        if int(value.get("schema_version", 0)) != SCHEMA_VERSION:
            raise ValueError("unsupported workload intelligence schema")
        result = cls(
            portfolio_min_samples=int(value.get("portfolio_min_samples") or 3),
            portfolio_min_offered_concurrency=float(
                value.get("portfolio_min_offered_concurrency", 1.5)
            ),
        )
        if value.get("demand"):
            result.demand = DemandTracker.from_dict(dict(value["demand"]))
        if value.get("unbound_demand"):
            result.unbound_demand = DemandTracker.from_dict(
                dict(value["unbound_demand"])
            )
        for row in value.get("outcomes") or ():
            fields = dict(row)
            # Legacy records shared one timestamp between service and quality. Retain their service
            # evidence, but conservatively age ambiguous quality evidence from the Unix epoch so a
            # restart cannot revive it through a fresh latency-only request.
            legacy_updated_at = fields.pop("updated_at", None)
            if legacy_updated_at is not None:
                fields.setdefault("service_updated_at", legacy_updated_at)
                fields.setdefault("quality_updated_at", 0.0)
                fields["quality"] = 0.0
                fields["quality_samples"] = 0
            # Records written before decayed sufficient statistics used the lifetime counters as
            # their effective evidence.  Seed the new fields from those counters at the record's
            # existing timestamp; ordinary time decay then migrates them conservatively.
            fields.setdefault("service_evidence", float(fields.get("requests") or 0))
            fields.setdefault("error_evidence", float(fields.get("errors") or 0))
            fields.setdefault(
                "quality_evidence", float(fields.get("quality_samples") or 0)
            )
            outcome = ModelWorkloadOutcome(**fields)
            result._outcomes[
                (outcome.model_id, outcome.workload, outcome.artifact_sha256)
            ] = outcome
        return result

    def _observe_outcome(
        self,
        model_id: str,
        workload: str,
        *,
        artifact_sha256: str,
        error: bool,
        latency_ms: float,
        output_units: int,
        quality: float | None,
        timestamp: float,
    ) -> None:
        if quality is not None and (
            not math.isfinite(float(quality)) or not 0 <= quality <= 1
        ):
            raise ValueError("quality must be in [0, 1]")
        if output_units < 0 or output_units > _MAX_FEATURE_UNITS:
            raise ValueError("output_units is outside the supported range")
        artifact = canonical_sha256(artifact_sha256)
        key = (model_id, workload, artifact)
        prior = self._outcomes.get(key)
        requests = min(MAX_COUNTER, (prior.requests if prior else 0) + 1)
        if prior is None:
            service_evidence = 1.0
            error_evidence = float(error)
            service_updated_at = timestamp
            service_alpha = 1.0
        elif timestamp >= prior.service_updated_at:
            service_decay = _evidence_decay(timestamp - prior.service_updated_at)
            prior_service_evidence = prior.service_evidence * service_decay
            service_evidence = min(MAX_COUNTER, prior_service_evidence + 1.0)
            error_evidence = min(
                service_evidence,
                prior.error_evidence * service_decay + float(error),
            )
            service_updated_at = timestamp
            # A first observation after an idle period should dominate decayed history. Under a
            # steady stream retain the responsive EWMA used for live regressions.
            service_alpha = max(0.20, 1.0 / service_evidence)
        else:
            # Completion callbacks may arrive out of order. Add their appropriately aged mass at
            # the current evidence watermark and never move that watermark backwards.
            observation_weight = _evidence_decay(prior.service_updated_at - timestamp)
            service_evidence = min(
                MAX_COUNTER, prior.service_evidence + observation_weight
            )
            error_evidence = min(
                service_evidence,
                prior.error_evidence + float(error) * observation_weight,
            )
            service_updated_at = prior.service_updated_at
            service_alpha = min(0.20, observation_weight / service_evidence)
        quality_samples = min(
            MAX_COUNTER,
            (prior.quality_samples if prior else 0) + int(quality is not None),
        )
        quality_evidence = prior.quality_evidence if prior else 0.0
        quality_updated_at = prior.quality_updated_at if prior else 0.0
        quality_ewma = prior.quality if prior else 0.0
        if quality is not None:
            if not prior or not prior.quality_samples:
                quality_evidence = 1.0
                quality_updated_at = timestamp
                quality_alpha = 1.0
            elif timestamp >= prior.quality_updated_at:
                quality_decay = _evidence_decay(timestamp - prior.quality_updated_at)
                prior_quality_evidence = prior.quality_evidence * quality_decay
                quality_evidence = min(MAX_COUNTER, prior_quality_evidence + 1.0)
                quality_updated_at = timestamp
                quality_alpha = max(0.20, 1.0 / quality_evidence)
            else:
                quality_weight = _evidence_decay(prior.quality_updated_at - timestamp)
                quality_evidence = min(
                    MAX_COUNTER, prior.quality_evidence + quality_weight
                )
                quality_updated_at = prior.quality_updated_at
                quality_alpha = min(0.20, quality_weight / quality_evidence)
            quality_ewma = quality_alpha * float(quality) + (
                1.0 - quality_alpha
            ) * quality_ewma
        self._outcomes[key] = ModelWorkloadOutcome(
            model_id=model_id,
            workload=workload,
            artifact_sha256=artifact,
            requests=requests,
            errors=min(requests, (prior.errors if prior else 0) + int(error)),
            latency_ms=(
                latency_ms
                if prior is None
                else service_alpha * latency_ms
                + (1.0 - service_alpha) * prior.latency_ms
            ),
            output_units=(
                float(output_units)
                if prior is None
                else service_alpha * output_units
                + (1.0 - service_alpha) * prior.output_units
            ),
            quality=quality_ewma,
            quality_samples=quality_samples,
            service_evidence=service_evidence,
            error_evidence=error_evidence,
            quality_evidence=quality_evidence,
            service_updated_at=service_updated_at,
            quality_updated_at=quality_updated_at,
        )

    def _outcome_evidence(
        self,
        model_id: str,
        workload: str,
        *,
        artifact_sha256: str = "",
        now: float,
    ) -> dict[str, float]:
        outcome = self._outcomes.get(
            (model_id, workload, canonical_sha256(artifact_sha256))
        )
        if outcome is None or not outcome.requests:
            return {
                "age_seconds": 0.0,
                "freshness": 0.0,
                "effective_requests": 0.0,
                "effective_errors": 0.0,
                "confidence": 0.0,
                "quality_age_seconds": 0.0,
                "quality_freshness": 0.0,
                "effective_quality_samples": 0.0,
                "quality_confidence": 0.0,
                "exploration_bonus": _MAX_EXPLORATION_BONUS,
            }
        age = max(0.0, now - outcome.service_updated_at)
        freshness = _evidence_decay(age)
        effective_requests = outcome.service_evidence * freshness
        effective_errors = outcome.error_evidence * freshness
        confidence = min(1.0, effective_requests / _OUTCOME_FULL_CONFIDENCE_REQUESTS)
        quality_age = max(0.0, now - outcome.quality_updated_at)
        quality_freshness = (
            _evidence_decay(quality_age)
            if outcome.quality_samples
            else 0.0
        )
        effective_quality_samples = outcome.quality_evidence * quality_freshness
        quality_confidence = min(
            1.0,
            effective_quality_samples / _QUALITY_FULL_CONFIDENCE_SAMPLES,
        )
        return {
            "age_seconds": age,
            "freshness": freshness,
            "effective_requests": effective_requests,
            "effective_errors": effective_errors,
            "confidence": confidence,
            "quality_age_seconds": quality_age,
            "quality_freshness": quality_freshness,
            "effective_quality_samples": effective_quality_samples,
            "quality_confidence": quality_confidence,
            "exploration_bonus": _MAX_EXPLORATION_BONUS * (1.0 - confidence),
        }

    def _portfolio_score(
        self,
        profile: ModelProfile,
        workload: str,
        *,
        now: float,
    ) -> float:
        score = profile.workload_score(workload)
        outcome = self._outcomes.get(
            (profile.model_id, workload, profile.artifact_sha256)
        )
        evidence = self._outcome_evidence(
            profile.model_id,
            workload,
            artifact_sha256=profile.artifact_sha256,
            now=now,
        )
        if outcome and outcome.requests:
            confidence = evidence["confidence"]
            success = (
                evidence["effective_requests"]
                - evidence["effective_errors"]
                + 1.0
            ) / (
                evidence["effective_requests"] + 2.0
            )
            quality = outcome.quality if outcome.quality_samples else 0.5
            score += confidence * 0.15 * (success - 0.5)
            score += evidence["quality_confidence"] * 0.20 * (quality - 0.5)
            if profile.latency_slo_ms > 0 and outcome.latency_ms > 0:
                # Quality remains dominant, but two similarly capable candidates should not tie
                # when one consistently consumes much more of the workload's latency budget.
                latency_ratio = outcome.latency_ms / profile.latency_slo_ms
                score -= confidence * min(0.05, latency_ratio * 0.02)
        # Optimism under uncertainty lets a feasible cold arm earn a bounded canary. It decays to
        # zero after twenty fresh observations and cannot overcome a substantial configured
        # suitability difference.
        score += evidence["exploration_bonus"]
        # Small deterministic pressure toward efficient canaries. Configured suitability remains
        # dominant; this does not turn memory size into a semantic request router.
        score -= min(0.10, math.log2(max(1, profile.maximum_memory_mb)) / 200.0)
        score -= min(0.05, (profile.load_seconds + profile.warm_seconds) / 7_200.0)
        return score

    def _portfolio_score_with_placement(
        self,
        profile: ModelProfile,
        workload: str,
        placement_hints: Mapping[str, Mapping[str, Any]] | None,
        *,
        now: float,
    ) -> float:
        score = self._portfolio_score(profile, workload, now=now)
        if placement_hints is None:
            return score
        hint = placement_hints.get(profile.model_id) or {}
        score -= _placement_transition_penalty(hint)
        if hint.get("feasible_after_preemption") is True and not (
            hint.get("feasible_now") is True or hint.get("feasible") is True
        ):
            # Exploration is canary authority, not eviction authority. A preemption-only arm must
            # earn a material evidence advantage before replacing a currently feasible model.
            score -= 0.08
        return score


def _evidence_decay(age_seconds: float) -> float:
    return math.pow(0.5, max(0.0, age_seconds) / _OUTCOME_HALF_LIFE_SECONDS)


def _placement_feasible(
    model_id: str,
    placement_hints: Mapping[str, Mapping[str, Any]] | None,
) -> bool:
    if placement_hints is None:
        return True
    hint = placement_hints.get(model_id)
    return bool(
        hint
        and (
            hint.get("feasible") is True
            or (
                hint.get("feasible_after_preemption") is True
                and hint.get("portfolio_preemption_safe") is True
            )
        )
    )


def _placement_transition_penalty(hint: Mapping[str, Any]) -> float:
    """Penalize avoidable model churn using the best current startup path.

    The penalty disappears once a model is resident, which creates state-dependent hysteresis:
    a cold challenger must provide a meaningful score improvement, while an incumbent that becomes
    infeasible loses its protection through the ordinary hard placement filter.
    """

    try:
        startup_seconds = float(hint.get("startup_seconds") or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(startup_seconds) or startup_seconds <= 0:
        return 0.0
    return min(0.05, 0.01 + startup_seconds / 1_200.0)
def _candidate_rows(
    profiles: Iterable[ModelProfile],
    workload: str,
    placement_hints: Mapping[str, Mapping[str, Any]] | None,
    intelligence: WorkloadIntelligence,
    *,
    now: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for profile in profiles:
        evidence = intelligence._outcome_evidence(
            profile.model_id,
            workload,
            artifact_sha256=profile.artifact_sha256,
            now=now,
        )
        score = intelligence._portfolio_score_with_placement(
            profile,
            workload,
            placement_hints,
            now=now,
        )
        rows.append(
            {
                "model_id": profile.model_id,
                "score": score,
                # This counterfactual removes only optimism under uncertainty. It lets the joint
                # optimizer distinguish serving an unevaluated workload from deliberately spending
                # one of its bounded exploration slots on a non-incumbent arm.
                "exploitation_score": score - evidence["exploration_bonus"],
                "transition_penalty": _placement_transition_penalty(
                    (placement_hints or {}).get(profile.model_id) or {}
                ),
                "evidence": evidence,
                "feasible": _placement_feasible(profile.model_id, placement_hints),
                "selectable": _placement_feasible(
                    profile.model_id,
                    placement_hints,
                ),
                "placement": dict((placement_hints or {}).get(profile.model_id) or {}),
            }
        )
    return sorted(rows, key=lambda row: (-float(row["score"]), str(row["model_id"])))


def classify_request(endpoint: str, body: Mapping[str, Any]) -> RequestFeatures:
    """Extract a bounded allocator feature vector without retaining request content."""

    endpoint = str(endpoint).strip("/")[:256] or "unknown"
    requested_model = str(body.get("model") or "")[:1_024]
    endpoint_lower = endpoint.lower()
    modalities = {"text"}
    if "image" in endpoint_lower:
        workload = "image"
        modalities.add("image")
    elif "video" in endpoint_lower or "i2v" in endpoint_lower:
        workload = "video"
        modalities.add("video")
    elif "embedding" in endpoint_lower:
        workload = "embedding"
    else:
        text, saw_image = _bounded_request_text(body)
        if saw_image:
            modalities.add("image")
        words = _TOKEN.findall(text.lower())
        word_set = set(words)
        scores = {
            name: len(word_set.intersection(keywords))
            for name, keywords in _KEYWORDS.items()
        }
        workload = (
            max(sorted(scores), key=lambda name: scores[name]) if scores else GENERAL
        )
        if not scores or scores[workload] == 0:
            workload = GENERAL
    text, saw_image = _bounded_request_text(body)
    if saw_image:
        modalities.add("image")
    input_units = min(_MAX_FEATURE_UNITS, max(0, math.ceil(len(text) / 4)))
    requested_output = _bounded_nonnegative_int(
        body.get(
            "max_completion_tokens", body.get("max_tokens", body.get("n_predict", 0))
        )
    )
    return RequestFeatures(
        endpoint=endpoint,
        requested_model=requested_model,
        workload=workload,
        modalities=tuple(modalities),
        input_units=input_units,
        requested_output_units=requested_output,
    )


def _bounded_request_text(value: Any) -> tuple[str, bool]:
    parts: list[str] = []
    remaining = _MAX_CLASSIFICATION_CHARS
    saw_image = False

    def visit(item: Any, *, depth: int = 0) -> None:
        nonlocal remaining, saw_image
        if remaining <= 0 or depth > 8:
            return
        if isinstance(item, str):
            chunk = item[:remaining]
            parts.append(chunk)
            remaining -= len(chunk)
        elif isinstance(item, list):
            for child in item[:256]:
                visit(child, depth=depth + 1)
                if remaining <= 0:
                    break
        elif isinstance(item, Mapping):
            kind = str(item.get("type") or "").lower()
            if "image" in kind or any(
                key in item for key in ("image_url", "input_image")
            ):
                saw_image = True
            for key in (
                "prompt",
                "input",
                "messages",
                "content",
                "text",
                "description",
            ):
                if key in item:
                    visit(item[key], depth=depth + 1)

    visit(value)
    return "\n".join(parts), saw_image


def _bounded_nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return min(_MAX_FEATURE_UNITS, max(0, parsed))


def _merge_forecasts(
    direct: DemandForecast | None,
    projected: DemandForecast,
) -> DemandForecast:
    if direct is None:
        return projected
    direct_count = direct.sample_count
    projected_count = projected.sample_count
    total_count = direct_count + projected_count
    return DemandForecast(
        model_id=direct.model_id,
        requests_per_minute=direct.requests_per_minute + projected.requests_per_minute,
        observed_requests_per_minute=direct.observed_requests_per_minute,
        offered_concurrency=direct.offered_concurrency + projected.offered_concurrency,
        queue_depth=max(direct.queue_depth, projected.queue_depth),
        p95_latency_ms=max(direct.p95_latency_ms, projected.p95_latency_ms),
        error_rate=(
            (direct.error_rate * direct_count + projected.error_rate * projected_count)
            / total_count
            if total_count
            else 0.0
        ),
        trend_per_minute=direct.trend_per_minute + projected.trend_per_minute,
        confidence=max(direct.confidence, projected.confidence),
        correlated_requests_per_minute=(
            direct.correlated_requests_per_minute
            + projected.correlated_requests_per_minute
        ),
        correlation_confidence=max(
            direct.correlation_confidence, projected.correlation_confidence
        ),
        correlation_sources=tuple(
            sorted(set(direct.correlation_sources).union(projected.correlation_sources))
        ),
        sample_count=total_count,
        updated_at=max(direct.updated_at, projected.updated_at),
    )
