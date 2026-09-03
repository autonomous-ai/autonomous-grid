"""Immutable model discovery, fleet-fit analysis, and benchmark proposal state.

The scout deliberately stops short of silently replacing a production model.  Hub metadata is
useful for finding candidates, not evidence of answer quality.  A candidate becomes proposal-ready
only after its repository revision and artifact digest are immutable, its license passes policy,
the current fleet can host it, and a real canary benchmark records fresh evaluation evidence.
"""

from __future__ import annotations

import hashlib
import math
import re
import statistics
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import httpx

from shared import jsonio


SCOUT_SCHEMA_VERSION = 1
DEFAULT_HUB_URL = "https://huggingface.co"
DEFAULT_ALLOWED_LICENSES = (
    "apache-2.0",
    "bsd-2-clause",
    "bsd-3-clause",
    "cc-by-4.0",
    "mit",
)
DEFAULT_TRUSTED_AUTHORS = (
    "deepseek-ai",
    "google",
    "ggml-org",
    "meta-llama",
    "microsoft",
    "mistralai",
    "nvidia",
    "qwen",
)
DEFAULT_QUANTIZATIONS = ("Q4_K_M", "Q5_K_M", "Q4_K_S", "Q5_K_S", "Q6_K")
MAX_DISCOVERY_RESULTS = 100
MAX_REPOSITORIES_INSPECTED = 40
MAX_ARTIFACT_SIZE_MB = 500_000
_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_FULL_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_SPLIT_GGUF = re.compile(r"-\d{5}-of-\d{5}\.gguf$", re.IGNORECASE)
_PARAMETERS = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*[bB](?![A-Za-z])")
_HUB_AUTHOR_CASE = {"qwen": "Qwen", "nvidia": "NVIDIA"}


@dataclass(frozen=True, slots=True)
class ScoutPolicy:
    workloads: tuple[str, ...] = ("coding", "general", "research")
    runtimes: tuple[str, ...] = ("llama.cpp", "vllm")
    trusted_authors: tuple[str, ...] = DEFAULT_TRUSTED_AUTHORS
    allowed_licenses: tuple[str, ...] = DEFAULT_ALLOWED_LICENSES
    quantizations: tuple[str, ...] = DEFAULT_QUANTIZATIONS
    max_results: int = 30
    max_repositories: int = 12
    max_artifact_size_mb: int = 100_000
    min_downloads: int = 0

    def __post_init__(self) -> None:
        for name in ("workloads", "runtimes", "trusted_authors", "allowed_licenses"):
            values = tuple(sorted({str(item).strip().lower() for item in getattr(self, name) if str(item).strip()}))
            if not values:
                raise ValueError(f"{name} must not be empty")
            object.__setattr__(self, name, values)
        quantizations = tuple(dict.fromkeys(str(item).strip().upper() for item in self.quantizations if str(item).strip()))
        if not quantizations:
            raise ValueError("quantizations must not be empty")
        object.__setattr__(self, "quantizations", quantizations)
        if not 1 <= self.max_results <= MAX_DISCOVERY_RESULTS:
            raise ValueError(f"max_results must be in [1, {MAX_DISCOVERY_RESULTS}]")
        if not 1 <= self.max_repositories <= MAX_REPOSITORIES_INSPECTED:
            raise ValueError(f"max_repositories must be in [1, {MAX_REPOSITORIES_INSPECTED}]")
        if not 1 <= self.max_artifact_size_mb <= MAX_ARTIFACT_SIZE_MB:
            raise ValueError(f"max_artifact_size_mb must be in [1, {MAX_ARTIFACT_SIZE_MB}]")
        if self.min_downloads < 0:
            raise ValueError("min_downloads must be non-negative")


@dataclass(frozen=True, slots=True)
class ModelCandidate:
    candidate_id: str
    repo_id: str
    revision: str
    runtime: str
    model_id: str
    artifact_path: str
    artifact_source: str
    artifact_sha256: str
    artifact_size_mb: int
    estimated_memory_mb: int
    quantization: str = ""
    parameter_billions: float = 0.0
    license: str = ""
    downloads: int = 0
    likes: int = 0
    last_modified: str = ""
    pipeline_tag: str = ""
    workload_scores: tuple[tuple[str, float], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ModelCandidate":
        fields = dict(value)
        fields["workload_scores"] = tuple(tuple(item) for item in fields.get("workload_scores") or ())
        return cls(**fields)


@dataclass(frozen=True, slots=True)
class FleetFit:
    node_id: str
    fits: bool
    runtime: str
    headroom_mb: int
    disk_headroom_mb: int | None
    score: float
    reason: str


@dataclass(frozen=True, slots=True)
class ScoutProposal:
    proposal_id: str
    candidate: ModelCandidate
    state: str
    score: float
    fits: tuple[FleetFit, ...]
    compare_models: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    benchmark_quality: float | None = None
    benchmark_latency_ms: float | None = None
    benchmark_samples: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "candidate": self.candidate.to_dict(),
            "fits": [asdict(item) for item in self.fits],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ScoutProposal":
        fields = dict(value)
        fields["candidate"] = ModelCandidate.from_dict(fields["candidate"])
        fields["fits"] = tuple(FleetFit(**item) for item in fields.get("fits") or ())
        fields["compare_models"] = tuple(fields.get("compare_models") or ())
        fields["reasons"] = tuple(fields.get("reasons") or ())
        return cls(**fields)


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    workload: str
    prompt: str
    required_fragments: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BenchmarkSample:
    workload: str
    quality: float
    latency_ms: float
    output_units: int
    error: bool


BENCHMARK_CASES: tuple[BenchmarkCase, ...] = (
    BenchmarkCase(
        "coding",
        "Write a Python function named clamp(value, low, high). Return only the function.",
        ("def clamp", "return"),
    ),
    BenchmarkCase(
        "research",
        "State the difference between correlation and causation in two concise sentences.",
        ("correlation", "caus"),
    ),
    BenchmarkCase(
        "general",
        "Reply with exactly the four-character string GRID.",
        ("grid",),
    ),
)


class HuggingFaceDiscovery:
    """Read public Hub metadata through its documented API; no artifact bytes are downloaded."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_HUB_URL,
        client: httpx.Client | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=True, trust_env=True)
        self.issues: list[str] = []

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def discover(self, policy: ScoutPolicy, *, search: str = "") -> tuple[ModelCandidate, ...]:
        self.issues = []
        queries: list[dict[str, str]] = []
        if "llama.cpp" in policy.runtimes:
            queries.append({"filter": "gguf"})
        if "vllm" in policy.runtimes:
            queries.append({"apps": "vllm"})
        rows_by_id: dict[str, Mapping[str, Any]] = {}
        # Query publishers independently. A single global "newest GGUF" page is dominated by
        # community conversions and could starve every trusted publisher out of a bounded scan.
        for query in queries:
            for author in policy.trusted_authors:
                try:
                    response = self.client.get(
                        f"{self.base_url}/api/models",
                        params={
                            **query,
                            # Hub owner matching is case-sensitive for some organizations even though
                            # policy comparison is intentionally canonical and case-insensitive.
                            "author": _HUB_AUTHOR_CASE.get(author, author),
                            "sort": "lastModified",
                            "direction": "-1",
                            "limit": str(min(policy.max_results, policy.max_repositories)),
                            **({"search": search} if search else {}),
                        },
                    )
                    response.raise_for_status()
                    rows = response.json()
                except (httpx.HTTPError, ValueError) as exc:
                    self.issues.append(_discovery_issue("listing", author, exc))
                    continue
                if not isinstance(rows, list):
                    self.issues.append(f"listing {author}: non-list response")
                    continue
                for row in rows:
                    if isinstance(row, Mapping):
                        repo_id = str(row.get("id") or row.get("modelId") or "")
                        if repo_id:
                            rows_by_id.setdefault(repo_id, row)
        candidates: list[ModelCandidate] = []
        inspected = 0
        for summary in rows_by_id.values():
            if inspected >= policy.max_repositories:
                break
            if not isinstance(summary, Mapping):
                continue
            repo_id = str(summary.get("id") or summary.get("modelId") or "")
            author = repo_id.partition("/")[0].lower()
            if not repo_id or author not in policy.trusted_authors:
                continue
            if bool(summary.get("gated")) or int(summary.get("downloads") or 0) < policy.min_downloads:
                continue
            inspected += 1
            try:
                detail_response = self.client.get(
                    f"{self.base_url}/api/models/{repo_id}", params={"blobs": "true"}
                )
                detail_response.raise_for_status()
                detail = detail_response.json()
            except (httpx.HTTPError, ValueError) as exc:
                self.issues.append(_discovery_issue("detail", repo_id, exc))
                continue
            if not isinstance(detail, Mapping):
                self.issues.append(f"detail {repo_id}: non-object response")
                continue
            candidates.extend(_candidates_from_hub_detail(detail, policy))
        return tuple(sorted(candidates, key=_candidate_discovery_sort_key))


def _discovery_issue(kind: str, source: str, exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{kind} {source}: HTTP {exc.response.status_code}"
    return f"{kind} {source}: {type(exc).__name__}"


def _candidates_from_hub_detail(
    detail: Mapping[str, Any], policy: ScoutPolicy
) -> tuple[ModelCandidate, ...]:
    repo_id = str(detail.get("id") or detail.get("modelId") or "")
    revision = str(detail.get("sha") or "").lower()
    if not repo_id or not _FULL_SHA.fullmatch(revision) or bool(detail.get("gated")):
        return ()
    license_name = _license_of(detail)
    if license_name.lower() not in policy.allowed_licenses:
        return ()
    siblings = detail.get("siblings")
    if not isinstance(siblings, list):
        return ()
    out: list[ModelCandidate] = []
    if "llama.cpp" in policy.runtimes:
        for item in siblings:
            candidate = _gguf_candidate(detail, item, policy, repo_id, revision, license_name)
            if candidate is not None:
                out.append(candidate)
    if "vllm" in policy.runtimes:
        candidate = _vllm_candidate(detail, siblings, policy, repo_id, revision, license_name)
        if candidate is not None:
            out.append(candidate)
    return tuple(out)


def _gguf_candidate(
    detail: Mapping[str, Any],
    item: object,
    policy: ScoutPolicy,
    repo_id: str,
    revision: str,
    license_name: str,
) -> ModelCandidate | None:
    if not isinstance(item, Mapping):
        return None
    path = str(item.get("rfilename") or item.get("path") or "")
    if not path.lower().endswith(".gguf") or _SPLIT_GGUF.search(path):
        return None
    quant = next((value for value in policy.quantizations if value in path.upper()), "")
    if not quant:
        return None
    size_bytes, digest = _artifact_identity(item)
    size_mb = math.ceil(size_bytes / (1024 * 1024)) if size_bytes > 0 else 0
    if not size_mb or size_mb > policy.max_artifact_size_mb or not _FULL_SHA256.fullmatch(digest):
        return None
    model_id = Path(path).name
    scores = _workload_scores(detail, repo_id, policy.workloads)
    identity = _candidate_id(repo_id, revision, path, digest)
    return ModelCandidate(
        candidate_id=identity,
        repo_id=repo_id,
        revision=revision,
        runtime="llama.cpp",
        model_id=model_id,
        artifact_path=path,
        artifact_source=f"hf://{repo_id}@{revision}/{path}",
        artifact_sha256=digest.lower(),
        artifact_size_mb=size_mb,
        estimated_memory_mb=min(MAX_ARTIFACT_SIZE_MB, math.ceil(size_mb * 1.20) + 768),
        quantization=quant,
        parameter_billions=_parameter_billions(detail, repo_id),
        license=license_name,
        downloads=int(detail.get("downloads") or 0),
        likes=int(detail.get("likes") or 0),
        last_modified=str(detail.get("lastModified") or detail.get("last_modified") or ""),
        pipeline_tag=str(detail.get("pipeline_tag") or ""),
        workload_scores=scores,
    )


def _vllm_candidate(
    detail: Mapping[str, Any],
    siblings: list[Any],
    policy: ScoutPolicy,
    repo_id: str,
    revision: str,
    license_name: str,
) -> ModelCandidate | None:
    tags = {str(item).lower() for item in detail.get("tags") or ()}
    has_config = any(
        isinstance(item, Mapping)
        and str(item.get("rfilename") or item.get("path") or "") == "config.json"
        for item in siblings
    )
    safetensors = [
        item
        for item in siblings
        if isinstance(item, Mapping)
        and str(item.get("rfilename") or item.get("path") or "").endswith(".safetensors")
    ]
    if not has_config or not safetensors or not ({"transformers", "vllm"} & tags):
        return None
    total_bytes = sum(_artifact_identity(item)[0] for item in safetensors)
    size_mb = math.ceil(total_bytes / (1024 * 1024)) if total_bytes > 0 else 0
    if not size_mb or size_mb > policy.max_artifact_size_mb:
        return None
    digest = hashlib.sha256(f"hf://{repo_id}@{revision}".encode()).hexdigest()
    return ModelCandidate(
        candidate_id=_candidate_id(repo_id, revision, "snapshot", digest),
        repo_id=repo_id,
        revision=revision,
        runtime="vllm",
        model_id=repo_id,
        artifact_path="",
        artifact_source=f"hf://{repo_id}@{revision}",
        artifact_sha256=digest,
        artifact_size_mb=size_mb,
        estimated_memory_mb=min(MAX_ARTIFACT_SIZE_MB, math.ceil(size_mb * 1.12) + 2048),
        parameter_billions=_parameter_billions(detail, repo_id),
        license=license_name,
        downloads=int(detail.get("downloads") or 0),
        likes=int(detail.get("likes") or 0),
        last_modified=str(detail.get("lastModified") or detail.get("last_modified") or ""),
        pipeline_tag=str(detail.get("pipeline_tag") or ""),
        workload_scores=_workload_scores(detail, repo_id, policy.workloads),
    )


def analyze_fleet_fit(
    candidate: ModelCandidate, nodes: Iterable[Mapping[str, Any]]
) -> tuple[FleetFit, ...]:
    fits: list[FleetFit] = []
    for node in nodes:
        node_id = str(node.get("node_id") or "")
        runtimes = {str(item).lower() for item in node.get("runtimes") or ()}
        capacity = _safe_nonnegative_int(node.get("capacity_mb"))
        reserved = _safe_nonnegative_int(node.get("reserved_mb"))
        headroom = max(0, capacity - reserved)
        raw_disk = node.get("disk_available_mb")
        disk = _safe_nonnegative_int(raw_disk) if raw_disk is not None else None
        reason = ""
        if str(node.get("state") or "accepting") != "accepting":
            reason = "node is not accepting placements"
        elif candidate.runtime.lower() not in runtimes:
            reason = f"runtime {candidate.runtime} is unavailable"
        elif headroom < candidate.estimated_memory_mb:
            reason = f"needs {candidate.estimated_memory_mb} MB memory; {headroom} MB available"
        elif disk is None:
            reason = "disk availability is unknown"
        elif disk < candidate.artifact_size_mb:
            reason = f"needs {candidate.artifact_size_mb} MB disk; {disk} MB available"
        score = 0.0
        if not reason:
            memory_margin = min(1.0, (headroom - candidate.estimated_memory_mb) / max(1, candidate.estimated_memory_mb))
            disk_margin = min(1.0, (disk - candidate.artifact_size_mb) / max(1, candidate.artifact_size_mb))
            bandwidth = max(0.0, float(node.get("memory_bandwidth_gbps") or 0.0))
            score = round(0.50 + 0.20 * memory_margin + 0.10 * disk_margin + 0.20 * min(1.0, bandwidth / 2_000.0), 6)
        fits.append(FleetFit(node_id, not reason, candidate.runtime, headroom, disk, score, reason))
    return tuple(sorted(fits, key=lambda item: (-int(item.fits), -item.score, item.node_id)))


def build_proposals(
    candidates: Iterable[ModelCandidate], status: Mapping[str, Any]
) -> tuple[ScoutProposal, ...]:
    nodes = [item for item in status.get("nodes") or () if isinstance(item, Mapping)]
    profiles = [item for item in status.get("models") or () if isinstance(item, Mapping)]
    proposals: list[ScoutProposal] = []
    for candidate in candidates:
        fits = analyze_fleet_fit(candidate, nodes)
        compatible = tuple(item for item in fits if item.fits)
        compare = _comparison_models(candidate, profiles)
        reasons: list[str] = []
        if not compatible:
            reasons.append("no current allocator host can safely fit the immutable artifact")
        if not candidate.workload_scores:
            reasons.append("no configured workload suitability could be inferred")
        state = "benchmark-ready" if not reasons else "blocked"
        score = _proposal_score(candidate, compatible)
        proposals.append(
            ScoutProposal(
                proposal_id=f"proposal-{candidate.candidate_id}",
                candidate=candidate,
                state=state,
                score=score,
                fits=fits,
                compare_models=compare,
                reasons=tuple(reasons),
            )
        )
    return tuple(sorted(proposals, key=lambda item: (-int(item.state == "benchmark-ready"), -item.score, item.proposal_id)))


def benchmark_candidate(
    proposal: ScoutProposal,
    runner: Callable[[str, str], tuple[str, float]],
    *,
    workloads: Iterable[str] = (),
) -> tuple[ScoutProposal, tuple[BenchmarkSample, ...]]:
    selected = {str(item).lower() for item in workloads if str(item)}
    cases = [case for case in BENCHMARK_CASES if not selected or case.workload in selected]
    if not cases:
        raise ValueError("no benchmark cases match the requested workloads")
    samples: list[BenchmarkSample] = []
    for case in cases:
        started = time.monotonic()
        try:
            text, reported_latency_ms = runner(proposal.candidate.model_id, case.prompt)
            elapsed_ms = max(0.0, (time.monotonic() - started) * 1_000.0)
            latency_ms = max(elapsed_ms, float(reported_latency_ms or 0.0))
            lowered = str(text).lower()
            hits = sum(fragment.lower() in lowered for fragment in case.required_fragments)
            quality = hits / max(1, len(case.required_fragments))
            samples.append(BenchmarkSample(case.workload, quality, latency_ms, len(str(text)), False))
        except Exception:
            samples.append(BenchmarkSample(case.workload, 0.0, 0.0, 0, True))
    quality = statistics.fmean(item.quality for item in samples)
    successful_latency = [item.latency_ms for item in samples if not item.error]
    latency = statistics.median(successful_latency) if successful_latency else None
    qualified = quality >= 0.80 and not any(item.error for item in samples)
    updated = ScoutProposal(
        proposal_id=proposal.proposal_id,
        candidate=proposal.candidate,
        state="qualified" if qualified else "benchmark-failed",
        score=proposal.score,
        fits=proposal.fits,
        compare_models=proposal.compare_models,
        reasons=proposal.reasons + (() if qualified else ("real canary benchmark did not meet the quality floor",)),
        benchmark_quality=round(quality, 6),
        benchmark_latency_ms=round(latency, 3) if latency is not None else None,
        benchmark_samples=len(samples),
    )
    return updated, tuple(samples)


def load_scout_state(path: Path) -> dict[str, Any]:
    state = jsonio.load_json(path)
    if not state:
        return {"schema_version": SCOUT_SCHEMA_VERSION, "updated_at": 0.0, "proposals": []}
    if int(state.get("schema_version", 0)) != SCOUT_SCHEMA_VERSION:
        raise ValueError("unsupported allocator scout state schema")
    proposals = state.get("proposals")
    if not isinstance(proposals, list):
        raise ValueError("allocator scout proposals must be a list")
    return state


def save_scout_state(
    path: Path,
    proposals: Iterable[ScoutProposal],
    *,
    policy: ScoutPolicy | None = None,
    benchmark_samples: Mapping[str, Iterable[BenchmarkSample]] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCOUT_SCHEMA_VERSION,
        "updated_at": time.time(),
        "policy": asdict(policy) if policy is not None else {},
        "proposals": [item.to_dict() for item in proposals],
        "benchmark_samples": {
            key: [
                dict(sample) if isinstance(sample, Mapping) else asdict(sample)
                for sample in values
            ]
            for key, values in (benchmark_samples or {}).items()
        },
    }
    jsonio.atomic_write_json(path, payload, mode=0o600)


def proposals_from_state(state: Mapping[str, Any]) -> tuple[ScoutProposal, ...]:
    return tuple(ScoutProposal.from_dict(item) for item in state.get("proposals") or ())


def _artifact_identity(item: Mapping[str, Any]) -> tuple[int, str]:
    lfs = item.get("lfs")
    if isinstance(lfs, Mapping):
        size = _safe_nonnegative_int(lfs.get("size") or item.get("size"))
        digest = str(lfs.get("sha256") or "")
    else:
        size = _safe_nonnegative_int(item.get("size"))
        digest = str(item.get("blobId") or item.get("blob_id") or "")
    return size, digest


def _license_of(detail: Mapping[str, Any]) -> str:
    card = detail.get("cardData") or detail.get("card_data")
    if isinstance(card, Mapping):
        value = card.get("license")
        if isinstance(value, list):
            return str(value[0] if value else "")
        if value:
            return str(value)
    for tag in detail.get("tags") or ():
        if str(tag).startswith("license:"):
            return str(tag).partition(":")[2]
    return ""


def _parameter_billions(detail: Mapping[str, Any], repo_id: str) -> float:
    safetensors = detail.get("safetensors")
    if isinstance(safetensors, Mapping):
        total = safetensors.get("total")
        try:
            if total is not None:
                return round(float(total) / 1_000_000_000.0, 3)
        except (TypeError, ValueError, OverflowError):
            pass
    matches = _PARAMETERS.findall(repo_id.replace("-", " "))
    return float(matches[-1]) if matches else 0.0


def _workload_scores(
    detail: Mapping[str, Any], repo_id: str, workloads: Iterable[str]
) -> tuple[tuple[str, float], ...]:
    corpus = " ".join(
        [repo_id, str(detail.get("pipeline_tag") or ""), *(str(item) for item in detail.get("tags") or ())]
    ).lower()
    scores: dict[str, float] = {}
    for workload in workloads:
        if workload == "coding" and any(word in corpus for word in ("code", "coder", "program")):
            scores[workload] = 1.0
        elif workload == "research" and any(word in corpus for word in ("reason", "research", "math", "science")):
            scores[workload] = 0.9
        elif workload in ("general", "research", "coding") and any(
            word in corpus for word in ("text-generation", "causal-lm", "instruct", "chat")
        ):
            scores[workload] = 0.7 if workload != "general" else 0.9
    return tuple(sorted(scores.items()))


def _comparison_models(
    candidate: ModelCandidate, profiles: Iterable[Mapping[str, Any]]
) -> tuple[str, ...]:
    candidate_workloads = {name for name, score in candidate.workload_scores if score > 0}
    matches: list[tuple[float, str]] = []
    for profile in profiles:
        model_id = str(profile.get("model_id") or "")
        if not model_id or model_id == candidate.model_id:
            continue
        scores = {
            str(item[0]): float(item[1])
            for item in profile.get("workload_scores") or ()
            if isinstance(item, (list, tuple)) and len(item) == 2
        }
        overlap = sum(scores.get(workload, 0.0) for workload in candidate_workloads)
        if overlap:
            matches.append((overlap, model_id))
    return tuple(model_id for _score, model_id in sorted(matches, key=lambda item: (-item[0], item[1]))[:3])


def _proposal_score(candidate: ModelCandidate, fits: tuple[FleetFit, ...]) -> float:
    popularity = min(1.0, math.log10(candidate.downloads + 1) / 7.0)
    approval = min(1.0, math.log10(candidate.likes + 1) / 4.0)
    recency = _recency_score(candidate.last_modified)
    suitability = max((score for _name, score in candidate.workload_scores), default=0.0)
    fleet = max((item.score for item in fits), default=0.0)
    # This is only a discovery priority. Quality remains absent until benchmark_candidate records it.
    return round(0.15 * popularity + 0.10 * approval + 0.20 * recency + 0.25 * suitability + 0.30 * fleet, 6)


def _recency_score(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds() / 86_400.0)
        return math.exp(-age_days / 90.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _candidate_discovery_sort_key(candidate: ModelCandidate) -> tuple[Any, ...]:
    quant_rank = (
        DEFAULT_QUANTIZATIONS.index(candidate.quantization)
        if candidate.quantization in DEFAULT_QUANTIZATIONS
        else len(DEFAULT_QUANTIZATIONS)
    )
    return (-_recency_score(candidate.last_modified), quant_rank, -candidate.downloads, candidate.candidate_id)


def _candidate_id(repo_id: str, revision: str, path: str, digest: str) -> str:
    value = f"{repo_id}\0{revision}\0{path}\0{digest}".encode()
    return hashlib.sha256(value).hexdigest()[:20]


def _safe_nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0
