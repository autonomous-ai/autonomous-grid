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
_TENANT_CLASS_PATTERN = re.compile(r"cohort-(?:0[0-9]|1[0-5])\Z")
_ACTIVE_COHORT_SECONDS = 300.0
_OUTCOME_HALF_LIFE_SECONDS = 7 * 24 * 60 * 60.0
_OUTCOME_FULL_CONFIDENCE_REQUESTS = 20.0
_QUALITY_FULL_CONFIDENCE_SAMPLES = 8.0
_MAX_EXPLORATION_BONUS = 0.06
_GRADUATION_COHORTS = 3
_GRADUATION_SAMPLES = 12
_GRADUATION_SAMPLES_PER_COHORT = 4
_GRADUATION_BREACH_RATE = 0.50
_KEYWORDS = {
    "coding": frozenset(
        {
            "api", "bug", "code", "commit", "compile", "debug", "function", "git",
            "javascript", "python", "refactor", "repository", "sql", "test", "typescript",
        }
    ),
    "research": frozenset(
        {
            "analyze", "citation", "compare", "evidence", "literature", "paper", "research",
            "source", "study", "survey", "verify",
        }
    ),
    "marketing": frozenset(
        {
            "ad", "audience", "brand", "campaign", "content", "copy", "headline", "marketing",
            "positioning", "seo", "social",
        }
    ),
    "sales": frozenset(
        {
            "account", "crm", "customer", "deal", "lead", "objection", "outreach", "pipeline",
            "prospect", "sales",
        }
    ),
    "design": frozenset(
        {
            "design", "figma", "layout", "mockup", "prototype", "style", "typography", "ui",
            "ux", "wireframe",
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
    tenant_class: str = "default"
    tenant_attested: bool = False

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
        tenant = str(self.tenant_class or "default")
        if tenant not in {"default", "anonymous", "allocator-evaluation"} and not (
            _TENANT_CLASS_PATTERN.fullmatch(tenant)
        ):
            raise ValueError("tenant_class must be an anonymous bounded cohort")
        object.__setattr__(self, "tenant_class", tenant)
        if not isinstance(self.tenant_attested, bool):
            raise ValueError("tenant_attested must be a boolean")


def anonymous_tenant_cohort(digest: bytes | None) -> str:
    """Reduce an opaque digest to one fixed cohort; never accept or retain a raw identity."""

    if digest is None:
        return "anonymous"
    if not isinstance(digest, bytes) or len(digest) < 2:
        raise ValueError("tenant cohort requires a binary digest")
    return f"cohort-{int.from_bytes(digest[:2], 'big') % 16:02d}"


@dataclass(frozen=True, slots=True)
class ModelWorkloadOutcome:
    model_id: str
    workload: str
    requests: int = 0
    errors: int = 0
    latency_ms: float = 0.0
    output_units: float = 0.0
    quality: float = 0.0
    quality_samples: int = 0
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.model_id or self.workload not in KNOWN_WORKLOADS:
            raise ValueError("model_id and known workload are required")
        if not 0 <= self.errors <= self.requests <= MAX_COUNTER:
            raise ValueError("outcome counters are invalid")
        if not 0 <= self.quality_samples <= self.requests:
            raise ValueError("quality sample count is invalid")
        for name in ("latency_ms", "output_units", "quality", "updated_at"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.quality > 1:
            raise ValueError("quality cannot exceed 1")


class WorkloadIntelligence:
    """Learns workload pressure and model outcomes without retaining content."""

    def __init__(self, *, portfolio_min_samples: int = 3) -> None:
        if portfolio_min_samples < 1:
            raise ValueError("portfolio_min_samples must be positive")
        self.portfolio_min_samples = int(portfolio_min_samples)
        self.demand = DemandTracker()
        self.unbound_demand = DemandTracker()
        self.cohort_demand = DemandTracker(
            window_seconds=_ACTIVE_COHORT_SECONDS,
            max_samples_per_model=64,
        )
        self._outcomes: dict[tuple[str, str], ModelWorkloadOutcome] = {}

    @property
    def outcomes(self) -> tuple[ModelWorkloadOutcome, ...]:
        return tuple(self._outcomes[key] for key in sorted(self._outcomes))

    def observe(
        self,
        features: RequestFeatures,
        *,
        served_model: str = "",
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
            float(service_seconds) * 1_000.0 if latency_ms is None else float(latency_ms)
        )
        self.demand.observe(
            features.workload,
            service_seconds=service_seconds,
            latency_ms=measured_latency,
            queue_depth=queue_depth,
            errors=int(error),
            timestamp=observed_at,
        )
        self.cohort_demand.observe(
            _cohort_key(
                features.workload,
                features.tenant_class,
                attested=features.tenant_attested,
            ),
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
                error=error,
                latency_ms=measured_latency,
                output_units=output_units,
                quality=quality,
                timestamp=observed_at,
            )

    def workload_forecasts(self, *, now: float | None = None) -> tuple[DemandForecast, ...]:
        timestamp = time.time() if now is None else float(now)
        keys = tuple(sorted((self.demand.to_dict().get("models") or {}).keys()))
        return tuple(self.demand.forecast(key, now=timestamp) for key in keys)

    def clear(self) -> None:
        """Clear learned demand and outcomes for a repeatable development simulation."""

        self.demand.clear()
        self.unbound_demand.clear()
        self.cohort_demand.clear()
        self._outcomes.clear()

    def observe_model_evaluation(
        self,
        model_id: str,
        workload: str,
        *,
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
            error=bool(error),
            latency_ms=float(latency_ms),
            output_units=output_units,
            quality=float(quality),
            timestamp=observed_at,
        )
        return self._outcomes[(model_id, workload)]

    def portfolio_forecasts(
        self,
        profiles: Iterable[ModelProfile],
        direct: Iterable[DemandForecast],
        *,
        now: float,
        placement_hints: Mapping[str, Mapping[str, Any]] | None = None,
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
            if forecast.sample_count < self.portfolio_min_samples or forecast.requests_per_minute <= 0:
                continue
            candidates = [
                profile
                for profile in profile_list
                if profile.max_replicas > 0
                and profile.workload_score(workload) > 0
                and _placement_feasible(
                    profile.model_id,
                    placement_hints,
                    allow_preemption=bool(
                        self._cohort_summary(
                            workload,
                            latency_slo_ms=profile.latency_slo_ms,
                            now=now,
                        )["graduated_allocation_pressure"]
                    ),
                )
            ]
            if not candidates:
                continue
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
            cohort_summary = self._cohort_summary(
                workload,
                latency_slo_ms=chosen.latency_slo_ms,
                now=now,
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
                active_cohorts=int(cohort_summary["active_cohorts"]),
                cohort_slo_breach_rate=float(cohort_summary["slo_breach_rate"]),
                cohort_fairness=float(cohort_summary["fairness"]),
                trusted_active_cohorts=int(cohort_summary["trusted_active_cohorts"]),
                trusted_cohort_slo_breach_rate=float(
                    cohort_summary["trusted_slo_breach_rate"]
                ),
                trusted_cohort_graduated=bool(
                    cohort_summary["graduated_allocation_pressure"]
                ),
            )
            merged[chosen.model_id] = _merge_forecasts(merged.get(chosen.model_id), projected)
        return tuple(merged[key] for key in sorted(merged))

    def projections(
        self,
        profiles: Iterable[ModelProfile],
        *,
        now: float | None = None,
        placement_hints: Mapping[str, Mapping[str, Any]] | None = None,
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
                if _placement_feasible(
                    profile.model_id,
                    placement_hints,
                    allow_preemption=bool(
                        self._cohort_summary(
                            workload,
                            latency_slo_ms=profile.latency_slo_ms,
                            now=timestamp,
                        )["graduated_allocation_pressure"]
                    ),
                )
            ]
            if not candidates:
                rows.append(
                    {
                        "workload": workload,
                        "requests_per_minute": forecast.requests_per_minute,
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
            cohort_summary = self._cohort_summary(
                workload,
                latency_slo_ms=chosen.latency_slo_ms,
                now=timestamp,
            )
            chosen_evidence = self._outcome_evidence(
                chosen.model_id,
                workload,
                now=timestamp,
            )
            rows.append(
                {
                    "workload": workload,
                    "requests_per_minute": forecast.requests_per_minute,
                    "samples": forecast.sample_count,
                    "confidence": forecast.confidence,
                    "chosen_model": chosen.model_id,
                    "score": self._portfolio_score_with_placement(
                        chosen,
                        workload,
                        placement_hints,
                        now=timestamp,
                    ),
                    "placement": dict((placement_hints or {}).get(chosen.model_id) or {}),
                    "candidates": _candidate_rows(
                        configured_candidates,
                        workload,
                        placement_hints,
                        self,
                        now=timestamp,
                    ),
                    "cohort_evidence": cohort_summary,
                    "reason": (
                        "trusted service pressure; planner must authorize preemption"
                        if (
                            cohort_summary["graduated_allocation_pressure"]
                            and bool(
                                ((placement_hints or {}).get(chosen.model_id) or {}).get(
                                    "feasible_after_preemption"
                                )
                            )
                        )
                        else (
                            (
                                "confidence-aware canary; "
                                f"{chosen_evidence['effective_requests']:.1f} effective samples"
                            )
                            if chosen_evidence["exploration_bonus"] > 0
                            else "evidence-backed portfolio choice"
                        )
                    ),
                }
            )
        return tuple(rows)

    def cohort_summaries(self, *, now: float | None = None) -> tuple[dict[str, Any], ...]:
        """Return bounded anonymous-cohort health without exposing identities or content."""

        timestamp = time.time() if now is None else float(now)
        workloads = sorted(
            {
                key.split(":", 1)[0]
                for key in (self.cohort_demand.to_dict().get("models") or {})
                if ":" in key
            }
        )
        return tuple(
            self._cohort_summary(workload, latency_slo_ms=0.0, now=timestamp)
            for workload in workloads
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "portfolio_min_samples": self.portfolio_min_samples,
            "demand": self.demand.to_dict(),
            "unbound_demand": self.unbound_demand.to_dict(),
            "cohort_demand": self.cohort_demand.to_dict(),
            "outcomes": [asdict(item) for item in self.outcomes],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> WorkloadIntelligence:
        if int(value.get("schema_version", 0)) != SCHEMA_VERSION:
            raise ValueError("unsupported workload intelligence schema")
        result = cls(portfolio_min_samples=int(value.get("portfolio_min_samples") or 3))
        if value.get("demand"):
            result.demand = DemandTracker.from_dict(dict(value["demand"]))
        if value.get("unbound_demand"):
            result.unbound_demand = DemandTracker.from_dict(dict(value["unbound_demand"]))
        if value.get("cohort_demand"):
            result.cohort_demand = DemandTracker.from_dict(dict(value["cohort_demand"]))
        for row in value.get("outcomes") or ():
            outcome = ModelWorkloadOutcome(**dict(row))
            result._outcomes[(outcome.model_id, outcome.workload)] = outcome
        return result

    def _cohort_summary(
        self,
        workload: str,
        *,
        latency_slo_ms: float,
        now: float,
    ) -> dict[str, Any]:
        prefix = f"{workload}:"
        rows: list[tuple[str, DemandForecast, bool]] = []
        for key in sorted((self.cohort_demand.to_dict().get("models") or {})):
            if not key.startswith(prefix):
                continue
            forecast = self.cohort_demand.forecast(key, now=now)
            age = max(0.0, now - forecast.updated_at) if forecast.updated_at else math.inf
            if (
                forecast.requests_per_minute > 0
                and forecast.sample_count > 0
                and age < _ACTIVE_COHORT_SECONDS
            ):
                rows.append((_cohort_key_name(key), forecast, _cohort_key_attested(key)))
        grouped: dict[str, list[DemandForecast]] = {}
        for cohort, forecast, _ in rows:
            grouped.setdefault(cohort, []).append(forecast)
        attainments: list[float] = []
        breaches = 0
        for cohort_rows in grouped.values():
            cohort_samples = sum(row.sample_count for row in cohort_rows)
            cohort_error_rate = (
                sum(row.error_rate * row.sample_count for row in cohort_rows)
                / cohort_samples
                if cohort_samples
                else 0.0
            )
            cohort_latency = max((row.p95_latency_ms for row in cohort_rows), default=0.0)
            latency_attainment = 1.0
            if latency_slo_ms > 0 and cohort_latency > 0:
                latency_attainment = min(1.0, latency_slo_ms / cohort_latency)
            attainments.append(
                max(0.0, (1.0 - cohort_error_rate) * latency_attainment)
            )
            breaches += int(
                cohort_error_rate >= 0.20
                or (latency_slo_ms > 0 and cohort_latency > latency_slo_ms)
            )
        trusted_attainments: list[float] = []
        trusted_breaches = 0
        trusted_samples = 0
        qualifying_trusted = 0
        for _, row, attested in rows:
            if not attested:
                continue
            latency_attainment = 1.0
            if latency_slo_ms > 0 and row.p95_latency_ms > 0:
                latency_attainment = min(1.0, latency_slo_ms / row.p95_latency_ms)
            attainment = max(0.0, (1.0 - row.error_rate) * latency_attainment)
            trusted_attainments.append(attainment)
            trusted_samples += row.sample_count
            qualifying_trusted += int(
                row.sample_count >= _GRADUATION_SAMPLES_PER_COHORT
            )
            trusted_breaches += int(
                row.error_rate >= 0.20
                or (latency_slo_ms > 0 and row.p95_latency_ms > latency_slo_ms)
            )
        active = len(grouped)
        samples = sum(row.sample_count for _, row, _ in rows)
        breach_rate = breaches / active if active else 0.0
        fairness = _jain(attainments)
        trusted_active = len(trusted_attainments)
        trusted_breach_rate = (
            trusted_breaches / trusted_active if trusted_active else 0.0
        )
        graduated = (
            qualifying_trusted >= _GRADUATION_COHORTS
            and trusted_samples >= _GRADUATION_SAMPLES
            and trusted_breach_rate >= _GRADUATION_BREACH_RATE
        )
        return {
            "workload": workload,
            "active_cohorts": active,
            "samples": samples,
            "slo_ms": float(latency_slo_ms),
            "slo_breach_rate": breach_rate,
            "fairness": fairness,
            "trusted_active_cohorts": trusted_active,
            "trusted_qualifying_cohorts": qualifying_trusted,
            "trusted_samples": trusted_samples,
            "trusted_slo_breach_rate": trusted_breach_rate,
            "trusted_fairness": _jain(trusted_attainments),
            "graduated_allocation_pressure": graduated,
        }

    def _observe_outcome(
        self,
        model_id: str,
        workload: str,
        *,
        error: bool,
        latency_ms: float,
        output_units: int,
        quality: float | None,
        timestamp: float,
    ) -> None:
        if quality is not None and (not math.isfinite(float(quality)) or not 0 <= quality <= 1):
            raise ValueError("quality must be in [0, 1]")
        if output_units < 0 or output_units > _MAX_FEATURE_UNITS:
            raise ValueError("output_units is outside the supported range")
        key = (model_id, workload)
        prior = self._outcomes.get(key)
        requests = min(MAX_COUNTER, (prior.requests if prior else 0) + 1)
        alpha = 1.0 if prior is None else 0.20
        quality_samples = min(
            MAX_COUNTER,
            (prior.quality_samples if prior else 0) + int(quality is not None),
        )
        quality_ewma = prior.quality if prior else 0.0
        if quality is not None:
            quality_ewma = float(quality) if not prior or not prior.quality_samples else (
                alpha * float(quality) + (1.0 - alpha) * prior.quality
            )
        self._outcomes[key] = ModelWorkloadOutcome(
            model_id=model_id,
            workload=workload,
            requests=requests,
            errors=min(requests, (prior.errors if prior else 0) + int(error)),
            latency_ms=(
                latency_ms
                if prior is None
                else alpha * latency_ms + (1.0 - alpha) * prior.latency_ms
            ),
            output_units=(
                float(output_units)
                if prior is None
                else alpha * output_units + (1.0 - alpha) * prior.output_units
            ),
            quality=quality_ewma,
            quality_samples=quality_samples,
            updated_at=timestamp,
        )

    def _outcome_evidence(
        self,
        model_id: str,
        workload: str,
        *,
        now: float,
    ) -> dict[str, float]:
        outcome = self._outcomes.get((model_id, workload))
        if outcome is None or not outcome.requests:
            return {
                "age_seconds": 0.0,
                "freshness": 0.0,
                "effective_requests": 0.0,
                "confidence": 0.0,
                "quality_confidence": 0.0,
                "exploration_bonus": _MAX_EXPLORATION_BONUS,
            }
        age = max(0.0, now - outcome.updated_at)
        freshness = math.pow(0.5, age / _OUTCOME_HALF_LIFE_SECONDS)
        effective_requests = outcome.requests * freshness
        confidence = min(1.0, effective_requests / _OUTCOME_FULL_CONFIDENCE_REQUESTS)
        quality_confidence = min(
            confidence,
            outcome.quality_samples * freshness / _QUALITY_FULL_CONFIDENCE_SAMPLES,
        )
        return {
            "age_seconds": age,
            "freshness": freshness,
            "effective_requests": effective_requests,
            "confidence": confidence,
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
        outcome = self._outcomes.get((profile.model_id, workload))
        evidence = self._outcome_evidence(profile.model_id, workload, now=now)
        if outcome and outcome.requests:
            confidence = evidence["confidence"]
            success = (outcome.requests - outcome.errors + 1.0) / (outcome.requests + 2.0)
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
        try:
            cost_per_hour = float(hint.get("cost_per_hour") or 0.0)
        except (TypeError, ValueError, OverflowError):
            cost_per_hour = 0.0
        if math.isfinite(cost_per_hour) and cost_per_hour > 0:
            # Quality and configured suitability remain dominant. This bounded term distinguishes
            # similarly effective candidates by the cheapest node that can actually host them.
            score -= min(0.10, 0.02 * math.log1p(cost_per_hour))
        if hint.get("feasible_after_preemption") is True and not (
            hint.get("feasible_now") is True or hint.get("feasible") is True
        ):
            # Exploration is canary authority, not eviction authority. A preemption-only arm must
            # earn a material evidence advantage before replacing a currently feasible model.
            score -= 0.08
        return score


def _placement_feasible(
    model_id: str,
    placement_hints: Mapping[str, Mapping[str, Any]] | None,
    *,
    allow_preemption: bool = False,
) -> bool:
    if placement_hints is None:
        return True
    hint = placement_hints.get(model_id)
    return bool(
        hint
        and (
            hint.get("feasible_now") is True
            or hint.get("feasible") is True
            or (allow_preemption and hint.get("feasible_after_preemption") is True)
        )
    )


def _cohort_key(workload: str, tenant_class: str, *, attested: bool) -> str:
    trust = "trusted" if attested else "untrusted"
    return f"{workload}:{trust}:{tenant_class}"


def _cohort_key_attested(key: str) -> bool:
    parts = key.split(":", 2)
    return len(parts) == 3 and parts[1] == "trusted"


def _cohort_key_name(key: str) -> str:
    parts = key.split(":", 2)
    return parts[-1]


def _jain(values: Iterable[float]) -> float:
    items = tuple(max(0.0, float(value)) for value in values)
    if not items:
        return 1.0
    squared_sum = sum(items) ** 2
    sum_squares = sum(value * value for value in items)
    return squared_sum / (len(items) * sum_squares) if sum_squares else 1.0


def _candidate_rows(
    profiles: Iterable[ModelProfile],
    workload: str,
    placement_hints: Mapping[str, Mapping[str, Any]] | None,
    intelligence: WorkloadIntelligence,
    *,
    now: float,
) -> list[dict[str, Any]]:
    rows = [
        {
            "model_id": profile.model_id,
            "score": intelligence._portfolio_score_with_placement(
                profile,
                workload,
                placement_hints,
                now=now,
            ),
            "evidence": intelligence._outcome_evidence(
                profile.model_id,
                workload,
                now=now,
            ),
            "feasible": _placement_feasible(profile.model_id, placement_hints),
            "placement": dict((placement_hints or {}).get(profile.model_id) or {}),
        }
        for profile in profiles
    ]
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
        workload = max(sorted(scores), key=lambda name: scores[name]) if scores else GENERAL
        if not scores or scores[workload] == 0:
            workload = GENERAL
    text, saw_image = _bounded_request_text(body)
    if saw_image:
        modalities.add("image")
    input_units = min(_MAX_FEATURE_UNITS, max(0, math.ceil(len(text) / 4)))
    requested_output = _bounded_nonnegative_int(
        body.get("max_completion_tokens", body.get("max_tokens", body.get("n_predict", 0)))
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
            if "image" in kind or any(key in item for key in ("image_url", "input_image")):
                saw_image = True
            for key in ("prompt", "input", "messages", "content", "text", "description"):
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
            (direct.error_rate * direct_count + projected.error_rate * projected_count) / total_count
            if total_count
            else 0.0
        ),
        trend_per_minute=direct.trend_per_minute + projected.trend_per_minute,
        confidence=max(direct.confidence, projected.confidence),
        correlated_requests_per_minute=(
            direct.correlated_requests_per_minute + projected.correlated_requests_per_minute
        ),
        correlation_confidence=max(
            direct.correlation_confidence, projected.correlation_confidence
        ),
        correlation_sources=tuple(
            sorted(set(direct.correlation_sources).union(projected.correlation_sources))
        ),
        sample_count=total_count,
        updated_at=max(direct.updated_at, projected.updated_at),
        active_cohorts=max(direct.active_cohorts, projected.active_cohorts),
        cohort_slo_breach_rate=max(
            direct.cohort_slo_breach_rate,
            projected.cohort_slo_breach_rate,
        ),
        cohort_fairness=min(direct.cohort_fairness, projected.cohort_fairness),
        trusted_active_cohorts=max(
            direct.trusted_active_cohorts,
            projected.trusted_active_cohorts,
        ),
        trusted_cohort_slo_breach_rate=max(
            direct.trusted_cohort_slo_breach_rate,
            projected.trusted_cohort_slo_breach_rate,
        ),
        trusted_cohort_graduated=(
            direct.trusted_cohort_graduated or projected.trusted_cohort_graduated
        ),
    )
