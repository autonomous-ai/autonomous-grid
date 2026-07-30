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
from dataclasses import asdict, dataclass
from typing import Any

from shared.allocator.models import SCHEMA_VERSION, DemandForecast


@dataclass(frozen=True, slots=True)
class DemandSample:
    timestamp: float
    requests: int = 1
    service_seconds: float = 0.0
    latency_ms: float = 0.0
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
    ) -> None:
        if window_seconds <= 0 or bucket_seconds <= 0:
            raise ValueError("window_seconds and bucket_seconds must be positive")
        if max_samples_per_model < 1 or confidence_samples < 1:
            raise ValueError("sample limits must be positive")
        if not 0 < ewma_alpha <= 1:
            raise ValueError("ewma_alpha must be in (0, 1]")
        if not math.isfinite(max_future_skew_seconds) or max_future_skew_seconds < 0:
            raise ValueError("max_future_skew_seconds must be finite and non-negative")
        self.window_seconds = float(window_seconds)
        self.bucket_seconds = float(bucket_seconds)
        self.max_samples_per_model = int(max_samples_per_model)
        self.ewma_alpha = float(ewma_alpha)
        self.confidence_samples = int(confidence_samples)
        self.max_future_skew_seconds = float(max_future_skew_seconds)
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
        queue_depth: int = 0,
        errors: int = 0,
        timestamp: float | None = None,
    ) -> None:
        if not model_id:
            raise ValueError("model_id is required")
        sample = DemandSample(
            timestamp=time.time() if timestamp is None else float(timestamp),
            requests=requests,
            service_seconds=service_seconds,
            latency_ms=(service_seconds * 1_000.0 if latency_ms is None else latency_ms),
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
                # and avoids silently dropping a tail event when traffic exceeds the raw-sample cap.
                latency_ms=max(prior.latency_ms, sample.latency_ms),
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
        prior_slice = rates[-2 * recent_width:-recent_width]
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
        latencies = [item.latency_ms for item in samples if item.requests and item.latency_ms > 0]
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
            offered_concurrency=max(0.0, offered_concurrency),
            queue_depth=queue_depth,
            p95_latency_ms=_percentile(latencies, 0.95),
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
    ) -> tuple[DemandForecast, ...]:
        ids = sorted(self._samples if model_ids is None else set(model_ids))
        return tuple(self.forecast(model_id, now=now) for model_id in ids)

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
                if not key or not math.isfinite(supplied_watermark) or supplied_watermark < 0:
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
            del history[:-self.max_samples_per_model]
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

    def _buckets(self, samples: list[DemandSample], reference: float) -> list[dict[str, float]]:
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


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(0, math.ceil(quantile * len(ordered)) - 1)
    return float(ordered[rank])
