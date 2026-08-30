"""Bounded demand telemetry and conservative short-horizon forecasting.

The allocator does not need a speculative forecasting service to be useful.  A bucketed EWMA gives
stable replica targets, a small trend term warms capacity before a ramp reaches the queue, and the
planner's hysteresis handles uncertainty.  Raw prompts and responses never enter this store.
"""

from __future__ import annotations

import math
import statistics
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from typing import Any, Mapping

from shared.allocator.models import SCHEMA_VERSION, DemandForecast

_LATENCY_HISTOGRAM_BINS = 160
_LATENCY_BUCKETS_PER_OCTAVE = 8
_SEQUENTIAL_MIN_TRANSITIONS = 5


@dataclass(frozen=True, slots=True)
class DemandSample:
    timestamp: float
    requests: int = 1
    service_seconds: float = 0.0
    latency_ms: float = 0.0
    latency_histogram: tuple[tuple[int, int], ...] = ()
    queue_depth: int = 0
    errors: int = 0

    def __post_init__(self) -> None:
        for name in ("timestamp", "service_seconds", "latency_ms"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.requests < 0 or self.queue_depth < 0 or self.errors < 0:
            raise ValueError("requests, queue_depth, and errors must be non-negative")
        if self.errors > self.requests:
            raise ValueError("errors cannot exceed requests")
        histogram: dict[int, int] = {}
        for item in self.latency_histogram:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise ValueError(
                    "latency histogram entries must be (bucket, count) pairs"
                )
            bucket, count = item
            if (
                isinstance(bucket, bool)
                or not isinstance(bucket, int)
                or not 0 <= bucket < _LATENCY_HISTOGRAM_BINS
                or bucket in histogram
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count <= 0
            ):
                raise ValueError("latency histogram contains an invalid bucket")
            histogram[bucket] = count
        if sum(histogram.values()) > self.requests:
            raise ValueError(
                "latency histogram cannot contain more observations than requests"
            )
        object.__setattr__(
            self,
            "latency_histogram",
            tuple(sorted(histogram.items())),
        )


class DemandTracker:
    """A bounded, serializable demand history keyed by model ID.

    Samples may arrive out of order (stream completion after a later short request) and a wall clock
    may move backwards. Histories stay sorted and pruning uses the greatest timestamp observed for
    that model, so a clock correction cannot resurrect its expired demand or let another model's
    skewed timestamp discard its newest bucket.
    """

    def __init__(
        self,
        *,
        window_seconds: float = 3_600.0,
        bucket_seconds: float = 60.0,
        max_samples_per_model: int = 4_096,
        ewma_alpha: float = 0.35,
        confidence_samples: int = 20,
        max_future_skew_seconds: float = 300.0,
        correlation_min_buckets: int = 3,
        correlation_threshold: float = 0.70,
        correlation_max_growth: float = 2.0,
        correlation_max_sources: int = 32,
    ) -> None:
        try:
            window_seconds = float(window_seconds)
            bucket_seconds = float(bucket_seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                "window_seconds and bucket_seconds must be finite and positive"
            ) from exc
        if (
            not math.isfinite(window_seconds)
            or not math.isfinite(bucket_seconds)
            or window_seconds <= 0
            or bucket_seconds <= 0
        ):
            raise ValueError(
                "window_seconds and bucket_seconds must be finite and positive"
            )
        if (
            isinstance(max_samples_per_model, bool)
            or not isinstance(max_samples_per_model, int)
            or max_samples_per_model < 1
            or isinstance(confidence_samples, bool)
            or not isinstance(confidence_samples, int)
            or confidence_samples < 1
        ):
            raise ValueError("sample limits must be positive integers")
        if not 0 < ewma_alpha <= 1:
            raise ValueError("ewma_alpha must be in (0, 1]")
        if not math.isfinite(max_future_skew_seconds) or max_future_skew_seconds < 0:
            raise ValueError("max_future_skew_seconds must be finite and non-negative")
        if (
            isinstance(correlation_min_buckets, bool)
            or not isinstance(correlation_min_buckets, int)
            or correlation_min_buckets < 1
        ):
            raise ValueError("correlation_min_buckets must be positive")
        if (
            not math.isfinite(correlation_threshold)
            or not 0 < correlation_threshold <= 1
        ):
            raise ValueError("correlation_threshold must be in (0, 1]")
        if not math.isfinite(correlation_max_growth) or correlation_max_growth < 1:
            raise ValueError("correlation_max_growth must be finite and at least 1")
        if (
            isinstance(correlation_max_sources, bool)
            or not isinstance(correlation_max_sources, int)
            or correlation_max_sources < 1
        ):
            raise ValueError("correlation_max_sources must be positive")
        self.window_seconds = float(window_seconds)
        self.bucket_seconds = float(bucket_seconds)
        self.max_samples_per_model = int(max_samples_per_model)
        self.ewma_alpha = float(ewma_alpha)
        self.confidence_samples = int(confidence_samples)
        self.max_future_skew_seconds = float(max_future_skew_seconds)
        self.correlation_min_buckets = int(correlation_min_buckets)
        self.correlation_threshold = float(correlation_threshold)
        self.correlation_max_growth = float(correlation_max_growth)
        self.correlation_max_sources = int(correlation_max_sources)
        self._samples: dict[str, list[DemandSample]] = {}
        # Clock rollback protection is model-local. A single global watermark lets one skewed
        # request timestamp prune every other model's healthy history and then reject their fresh
        # post-rollback samples until wall time catches up.
        self._high_watermarks: dict[str, float] = {}

    def observe(
        self,
        model_id: str,
        *,
        requests: int = 1,
        service_seconds: float = 0.0,
        latency_ms: float | None = None,
        latency_histogram: (
            tuple[tuple[int, int], ...] | list[list[int]] | None
        ) = None,
        queue_depth: int = 0,
        errors: int = 0,
        timestamp: float | None = None,
    ) -> None:
        if not model_id:
            raise ValueError("model_id is required")
        measured_latency = (
            service_seconds * 1_000.0 if latency_ms is None else latency_ms
        )
        sample = DemandSample(
            timestamp=time.time() if timestamp is None else float(timestamp),
            requests=requests,
            service_seconds=service_seconds,
            latency_ms=measured_latency,
            latency_histogram=(
                _latency_histogram(float(measured_latency), requests)
                if latency_histogram is None
                else tuple(latency_histogram)
            ),
            queue_depth=queue_depth,
            errors=errors,
        )
        self._repair_future_skew(model_id, clock_time=sample.timestamp)
        history = self._samples.setdefault(model_id, [])
        bucket = int(sample.timestamp // self.bucket_seconds)
        existing_index = next(
            (
                index
                for index, item in enumerate(history)
                if int(item.timestamp // self.bucket_seconds) == bucket
            ),
            None,
        )
        if existing_index is None:
            history.append(sample)
            history.sort(key=lambda item: item.timestamp)
        else:
            prior = history[existing_index]
            requests = prior.requests + sample.requests
            weighted_service = (
                prior.service_seconds * prior.requests
                + sample.service_seconds * sample.requests
            )
            history[existing_index] = DemandSample(
                timestamp=max(prior.timestamp, sample.timestamp),
                requests=requests,
                service_seconds=(weighted_service / requests if requests else 0.0),
                # Retain the worst latency in a compact bucket. It is conservative under pressure
                # and remains useful to legacy readers. The histogram below retains request-level
                # tail mass instead of turning one outlier into the latency of an entire minute.
                latency_ms=max(prior.latency_ms, sample.latency_ms),
                latency_histogram=_merge_latency_histograms(
                    prior.latency_histogram,
                    sample.latency_histogram,
                ),
                queue_depth=max(prior.queue_depth, sample.queue_depth),
                errors=prior.errors + sample.errors,
            )
            history.sort(key=lambda item: item.timestamp)
        reference = max(self._high_watermarks.get(model_id, 0.0), sample.timestamp)
        self._high_watermarks[model_id] = reference
        self._prune_model(model_id, reference=reference)

    def forecast(
        self,
        model_id: str,
        *,
        now: float | None = None,
    ) -> DemandForecast:
        if not model_id:
            raise ValueError("model_id is required")
        requested_now = time.time() if now is None else float(now)
        if not math.isfinite(requested_now) or requested_now < 0:
            raise ValueError("now must be finite and non-negative")
        self._repair_future_skew(model_id, clock_time=requested_now)
        reference = max(requested_now, self._high_watermarks.get(model_id, 0.0))
        self._prune_model(model_id, reference=reference)
        samples = self._samples.get(model_id, [])
        if not samples:
            # ``updated_at`` is evidence of an observed request, not the time somebody asked for a
            # forecast. Keeping it at zero prevents a never-used or fully expired series from
            # masquerading as recent demand and pinning a zero-minimum model forever.
            return DemandForecast(model_id=model_id)

        buckets = self._buckets(samples, reference)
        rates = [bucket["requests"] * 60.0 / self.bucket_seconds for bucket in buckets]
        ewma = rates[0]
        for rate in rates[1:]:
            ewma = self.ewma_alpha * rate + (1.0 - self.ewma_alpha) * ewma

        recent_width = min(3, max(1, len(rates) // 2))
        recent = statistics.fmean(rates[-recent_width:])
        prior_slice = rates[-2 * recent_width : -recent_width]
        prior = statistics.fmean(prior_slice) if prior_slice else recent
        trend = (recent - prior) / max(recent_width * self.bucket_seconds / 60.0, 1.0)
        # Warm for a rising workload, but do not immediately scale down on a negative trend; the
        # planner owns scale-down hysteresis.
        predicted_rate = max(0.0, ewma + max(0.0, trend) * self.bucket_seconds / 60.0)

        request_count = sum(item.requests for item in samples)
        service_weight = sum(item.service_seconds * item.requests for item in samples)
        average_service = service_weight / request_count if request_count else 0.0
        queue_depth = max((item.queue_depth for item in samples[-5:]), default=0)
        offered_concurrency = predicted_rate / 60.0 * average_service + queue_depth
        latency_histogram = _merge_latency_histograms(
            *(item.latency_histogram for item in samples)
        )
        errors = sum(item.errors for item in samples)
        coverage = min(
            1.0,
            max(self.bucket_seconds, samples[-1].timestamp - samples[0].timestamp)
            / min(self.window_seconds, 10 * self.bucket_seconds),
        )
        confidence = min(1.0, request_count / self.confidence_samples) * coverage
        return DemandForecast(
            model_id=model_id,
            requests_per_minute=predicted_rate,
            observed_requests_per_minute=predicted_rate,
            offered_concurrency=max(0.0, offered_concurrency),
            queue_depth=queue_depth,
            p95_latency_ms=_histogram_percentile(latency_histogram, 0.95),
            error_rate=(errors / request_count if request_count else 0.0),
            trend_per_minute=trend,
            confidence=confidence,
            sample_count=request_count,
            updated_at=max(item.timestamp for item in samples),
        )

    def forecasts(
        self,
        model_ids: list[str] | tuple[str, ...] | None = None,
        *,
        now: float | None = None,
        sequential_only: bool = False,
        sequence_edges: Mapping[tuple[str, str], float] | None = None,
    ) -> tuple[DemandForecast, ...]:
        if not isinstance(sequential_only, bool):
            raise ValueError("sequential_only must be a boolean")
        validated_edges: dict[tuple[str, str], float] | None = None
        if sequence_edges is not None:
            if not sequential_only:
                raise ValueError("sequence_edges require sequential_only=True")
            validated_edges = {}
            for edge, confidence in sequence_edges.items():
                if (
                    not isinstance(edge, tuple)
                    or len(edge) != 2
                    or not all(isinstance(item, str) and item for item in edge)
                    or edge[0] == edge[1]
                ):
                    raise ValueError("sequence_edges contain an invalid workload pair")
                try:
                    confidence = float(confidence)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError("sequence edge confidence must be in (0, 1]") from exc
                if not math.isfinite(confidence) or not 0 < confidence <= 1:
                    raise ValueError("sequence edge confidence must be in (0, 1]")
                validated_edges[edge] = confidence
        requested_now = time.time() if now is None else float(now)
        if not math.isfinite(requested_now) or requested_now < 0:
            raise ValueError("now must be finite and non-negative")
        ids = sorted(self._samples if model_ids is None else set(model_ids))
        base = tuple(self.forecast(model_id, now=requested_now) for model_id in ids)
        return self._apply_correlated_demand(
            base,
            now=requested_now,
            sequential_only=sequential_only,
            sequence_edges=validated_edges,
        )

    def clear(self, model_id: str | None = None) -> None:
        if model_id is None:
            self._samples.clear()
            self._high_watermarks.clear()
        else:
            self._samples.pop(model_id, None)
            self._high_watermarks.pop(model_id, None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "config": {
                "window_seconds": self.window_seconds,
                "bucket_seconds": self.bucket_seconds,
                "max_samples_per_model": self.max_samples_per_model,
                "ewma_alpha": self.ewma_alpha,
                "confidence_samples": self.confidence_samples,
                "max_future_skew_seconds": self.max_future_skew_seconds,
                "correlation_min_buckets": self.correlation_min_buckets,
                "correlation_threshold": self.correlation_threshold,
                "correlation_max_growth": self.correlation_max_growth,
                "correlation_max_sources": self.correlation_max_sources,
            },
            # Retain the scalar summary for older readers while new readers use the model-local
            # map. It is diagnostic only and never drives pruning in this version.
            "high_watermark": max(self._high_watermarks.values(), default=0.0),
            "model_high_watermarks": dict(sorted(self._high_watermarks.items())),
            "models": {
                model_id: [asdict(sample) for sample in samples]
                for model_id, samples in sorted(self._samples.items())
            },
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DemandTracker:
        if int(value.get("schema_version", 0)) != SCHEMA_VERSION:
            raise ValueError("unsupported allocator demand schema")
        tracker = cls(**dict(value.get("config") or {}))
        for model_id, rows in (value.get("models") or {}).items():
            for row in rows or ():
                tracker.observe(str(model_id), **row)
        supplied_watermarks = value.get("model_high_watermarks")
        if supplied_watermarks is not None:
            if not isinstance(supplied_watermarks, dict):
                raise ValueError("allocator demand model watermarks must be an object")
            for model_id, raw_watermark in supplied_watermarks.items():
                key = str(model_id)
                try:
                    supplied_watermark = float(raw_watermark)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError(
                        "allocator demand model watermark is invalid"
                    ) from exc
                if (
                    not key
                    or not math.isfinite(supplied_watermark)
                    or supplied_watermark < 0
                ):
                    raise ValueError("allocator demand model watermark is invalid")
                tracker._high_watermarks[key] = max(
                    tracker._high_watermarks.get(key, 0.0),
                    supplied_watermark,
                )
        else:
            # Legacy state has only one cross-model scalar. Applying it to every model would
            # recreate the clock-poisoning bug on restart, so the per-model watermarks already
            # derived from each model's serialized samples are the safe backward-compatible value.
            supplied_watermark = float(value.get("high_watermark") or 0.0)
            if not math.isfinite(supplied_watermark) or supplied_watermark < 0:
                raise ValueError("allocator demand high watermark is invalid")
        return tracker

    def _prune_model(self, model_id: str, *, reference: float) -> None:
        history = self._samples.get(model_id)
        if not history:
            return
        cutoff = reference - self.window_seconds
        first = 0
        while first < len(history) and history[first].timestamp < cutoff:
            first += 1
        if first:
            del history[:first]
        # Histories contain bucket aggregates rather than individual requests. A burst of 100,000
        # requests in one minute therefore remains one bounded row with its full request/service
        # mass instead of being truncated to ``max_samples_per_model``.
        if len(history) > self.max_samples_per_model:
            del history[: -self.max_samples_per_model]
        if not history:
            self._samples.pop(model_id, None)

    def _repair_future_skew(self, model_id: str, *, clock_time: float) -> None:
        """Rebase one model after an implausible forward-jump-then-rollback.

        A high watermark prevents a modest rollback from resurrecting already-expired buckets. It
        cannot remain unbounded, though: one bad RTC/NTP sample must not pin a model or discard its
        real traffic for hours or years. Once the gap exceeds the smaller of the configured skew
        tolerance and the complete history window, future-dated rows are discarded and the fence
        resumes from the corrected clock. Other models are untouched.
        """

        watermark = self._high_watermarks.get(model_id, 0.0)
        tolerance = min(self.window_seconds, self.max_future_skew_seconds)
        if watermark <= clock_time + tolerance:
            return
        maximum_valid_time = clock_time + tolerance
        history = self._samples.get(model_id, [])
        valid = [sample for sample in history if sample.timestamp <= maximum_valid_time]
        if valid:
            self._samples[model_id] = valid
        else:
            self._samples.pop(model_id, None)
        self._high_watermarks[model_id] = max(
            clock_time,
            max((sample.timestamp for sample in valid), default=0.0),
        )

    def _buckets(
        self, samples: list[DemandSample], reference: float
    ) -> list[dict[str, float]]:
        grouped: dict[int, dict[str, float]] = defaultdict(
            lambda: {"requests": 0.0, "service": 0.0, "errors": 0.0}
        )
        for sample in samples:
            index = int(sample.timestamp // self.bucket_seconds)
            grouped[index]["requests"] += sample.requests
            grouped[index]["service"] += sample.service_seconds * sample.requests
            grouped[index]["errors"] += sample.errors
        start = min(grouped)
        end = max(start, int(reference // self.bucket_seconds))
        max_buckets = max(1, math.ceil(self.window_seconds / self.bucket_seconds))
        start = max(start, end - max_buckets + 1)
        return [grouped[index] for index in range(start, end + 1)]

    def _apply_correlated_demand(
        self,
        forecasts: tuple[DemandForecast, ...],
        *,
        now: float,
        sequential_only: bool = False,
        sequence_edges: Mapping[tuple[str, str], float] | None = None,
    ) -> tuple[DemandForecast, ...]:
        """Lift quiet peers of a mature coactive group or directional model sequence.

        Coactivity uses symmetric cosine similarity over non-empty time buckets. Sequential evidence
        uses the conditional frequency of a target in the bucket after a source, considering only
        completed target buckets. Boosts use only the unmodified forecasts passed into this method,
        so inferred demand cannot cascade through a graph. Multiple sources take a maximum rather
        than summing into fleet-wide amplification.
        """

        if len(forecasts) < 2:
            return forecasts
        forecast_by_id = {item.model_id: item for item in forecasts}
        rates_by_model = {
            model_id: self._bucket_rates(self._samples.get(model_id, ()))
            for model_id in forecast_by_id
        }
        recent_sources = tuple(
            sorted(
                (
                    item
                    for item in forecasts
                    if item.requests_per_minute > 0
                    and item.updated_at
                    and -self.max_future_skew_seconds
                    <= now - item.updated_at
                    <= self.bucket_seconds
                ),
                key=lambda item: (
                    -item.requests_per_minute,
                    -item.confidence,
                    item.model_id,
                ),
            )[: self.correlation_max_sources]
        )
        if not recent_sources:
            return forecasts
        current_bucket = int(now // self.bucket_seconds)

        augmented: list[DemandForecast] = []
        for target in forecasts:
            target_rates = rates_by_model[target.model_id]
            if len(target_rates) < self.correlation_min_buckets:
                augmented.append(target)
                continue
            best_rate = target.requests_per_minute
            best_confidence = 0.0
            sources: list[str] = []
            target_buckets = set(target_rates)
            target_peak = max(target_rates.values(), default=0.0)
            for source in recent_sources:
                if source.model_id == target.model_id or source.confidence <= 0:
                    continue
                admitted_sequence_confidence = (
                    sequence_edges.get((source.model_id, target.model_id))
                    if sequence_edges is not None
                    else None
                )
                if sequential_only and sequence_edges is not None and admitted_sequence_confidence is None:
                    continue
                source_rates = rates_by_model[source.model_id]
                source_buckets = set(source_rates)
                evidence: list[tuple[float, list[float]]] = []
                if not sequential_only:
                    coactive = target_buckets.intersection(source_buckets)
                    if len(coactive) >= self.correlation_min_buckets:
                        association = len(coactive) / math.sqrt(
                            len(target_buckets) * len(source_buckets)
                        )
                        if association >= self.correlation_threshold:
                            evidence.append(
                                (
                                    association,
                                    [
                                        target_rates[index] / source_rates[index]
                                        for index in coactive
                                        if source_rates[index] > 0
                                    ],
                                )
                            )

                # A source in the current bucket predicts a target in the next bucket using only
                # older, fully observable transitions. The current and immediately prior source
                # buckets are not counted as failures while their target buckets are incomplete.
                # A transition begins only when the target is absent from the source bucket;
                # otherwise sustained coactivity would manufacture a directional workflow edge.
                completed_sources = [
                    index
                    for index in source_buckets
                    if index + 1 < current_bucket and index not in target_buckets
                ]
                transitioned = [
                    index for index in completed_sources if index + 1 in target_buckets
                ]
                # Directional edges are searched across every workload pair, so a short busy
                # trace will inevitably contain a few accidental 3/3 transitions. Require five
                # completed successes for sequence-only portfolio anticipation, then shrink the
                # observed conditional rate toward an uninformative prior. This still learns a
                # repeated user workflow quickly while a handful of coincidences or one-off phase
                # changes cannot move models during scarce capacity or an outage.
                minimum_transitions = max(
                    self.correlation_min_buckets,
                    (
                        _SEQUENTIAL_MIN_TRANSITIONS
                        if sequential_only and sequence_edges is None
                        else 0
                    ),
                )
                if len(transitioned) >= minimum_transitions:
                    transition_confidence = (
                        admitted_sequence_confidence
                        if admitted_sequence_confidence is not None
                        else (
                            (len(transitioned) + 1) / (len(completed_sources) + 2)
                            if sequential_only
                            else len(transitioned) / len(completed_sources)
                        )
                    )
                    if transition_confidence >= self.correlation_threshold:
                        evidence.append(
                            (
                                transition_confidence,
                                [
                                    target_rates[index + 1] / source_rates[index]
                                    for index in transitioned
                                    if source_rates[index] > 0
                                ],
                            )
                        )

                source_best_rate = target.requests_per_minute
                source_best_confidence = 0.0
                for association, ratios in evidence:
                    if not ratios:
                        continue
                    learned_ratio = min(10.0, max(0.1, statistics.median(ratios)))
                    confidence = association * source.confidence
                    candidate_rate = (
                        source.requests_per_minute * learned_ratio * confidence
                    )
                    candidate_rate = min(
                        candidate_rate,
                        target_peak * self.correlation_max_growth,
                    )
                    if candidate_rate > source_best_rate:
                        source_best_rate = candidate_rate
                        source_best_confidence = confidence
                if source_best_rate <= target.requests_per_minute:
                    continue
                sources.append(source.model_id)
                if source_best_rate > best_rate:
                    best_rate = source_best_rate
                    best_confidence = source_best_confidence
            if best_rate <= target.requests_per_minute:
                augmented.append(target)
                continue

            target_samples = self._samples.get(target.model_id, ())
            request_count = sum(item.requests for item in target_samples)
            service_mass = sum(
                item.service_seconds * item.requests for item in target_samples
            )
            average_service = service_mass / request_count if request_count else 0.0
            if not average_service and target.requests_per_minute > 0:
                average_service = (
                    target.offered_concurrency * 60.0 / target.requests_per_minute
                )
            correlated_concurrency = best_rate / 60.0 * average_service
            source_updated_at = max(
                forecast_by_id[source_id].updated_at for source_id in sources
            )
            target_is_recent = bool(
                target.updated_at
                and -self.max_future_skew_seconds
                <= now - target.updated_at
                < self.bucket_seconds
            )
            augmented.append(
                replace(
                    target,
                    requests_per_minute=best_rate,
                    offered_concurrency=max(
                        target.offered_concurrency,
                        correlated_concurrency,
                    ),
                    # Refresh only the inferred rate. Old target-local queue/SLO failures must not
                    # become current merely because a correlated peer received a new request.
                    queue_depth=target.queue_depth if target_is_recent else 0,
                    p95_latency_ms=target.p95_latency_ms if target_is_recent else 0.0,
                    error_rate=target.error_rate if target_is_recent else 0.0,
                    # This lift is already a forecast from another model. Do not extrapolate the
                    # target's older independent slope across its load time a second time.
                    trend_per_minute=0.0,
                    confidence=max(target.confidence, best_confidence),
                    correlated_requests_per_minute=best_rate,
                    correlation_confidence=best_confidence,
                    correlation_sources=tuple(sources),
                    updated_at=max(target.updated_at, source_updated_at),
                )
            )
        return tuple(augmented)

    def _bucket_rates(
        self,
        samples: list[DemandSample] | tuple[DemandSample, ...],
    ) -> dict[int, float]:
        rates: dict[int, float] = defaultdict(float)
        for sample in samples:
            rates[int(sample.timestamp // self.bucket_seconds)] += (
                sample.requests * 60.0 / self.bucket_seconds
            )
        return {index: rate for index, rate in rates.items() if rate > 0}


def _latency_histogram(
    latency_ms: float,
    requests: int,
) -> tuple[tuple[int, int], ...]:
    if latency_ms <= 0 or requests <= 0:
        return ()
    index = min(
        _LATENCY_HISTOGRAM_BINS - 1,
        max(
            0,
            math.ceil(math.log2(max(1.0, latency_ms)) * _LATENCY_BUCKETS_PER_OCTAVE),
        ),
    )
    return ((index, requests),)


def _merge_latency_histograms(
    *histograms: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, int], ...]:
    merged: dict[int, int] = defaultdict(int)
    for histogram in histograms:
        for index, count in histogram:
            merged[index] += count
    return tuple(sorted(merged.items()))


def _histogram_percentile(
    histogram: tuple[tuple[int, int], ...],
    quantile: float,
) -> float:
    total = sum(count for _, count in histogram)
    if total <= 0:
        return 0.0
    target = max(1, math.ceil(quantile * total))
    cumulative = 0
    for index, count in histogram:
        cumulative += count
        if cumulative >= target:
            if index == 0:
                return 1.0
            return float(2 ** ((index - 0.5) / _LATENCY_BUCKETS_PER_OCTAVE))
    return float(2 ** ((_LATENCY_HISTOGRAM_BINS - 1) / _LATENCY_BUCKETS_PER_OCTAVE))
