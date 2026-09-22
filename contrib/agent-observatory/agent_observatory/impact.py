"""Accepted-task impact reports (M6).

``measurement.md`` makes the work item / accepted task the primary unit and is
explicit about the failure modes a naive before/after report falls into:

* episodes and sessions are nested observations, not independent samples;
* failed and abandoned runs belong in the success-rate denominator, and their
  attempts belong in the cost of the tasks they were spent on;
* incomplete tasks are right-censored, never dropped and never recorded as zero;
* a missing price or missing usage is unknown, never zero, so cost can be only
  partially measured;
* "no accepted tasks" makes cost-per-accepted undefined, not zero;
* a zero baseline makes a relative change undefined;
* concurrent work must be unioned, not double-counted or summed as wall time;
* classifier overhead is part of the net cost;
* a semantic label or chronology alone cannot establish causality, so the report
  carries an attribution grade and states whether a claim is associational.

This module consumes an explicit, versioned JSON *impact bundle*. It never
crawls, never calls the network and never invents a work item, a baseline, a
price, a matched control or a causal effect. A companion helper
:func:`observed_evidence_from_store` derives the *descriptive* real-data half
(cost/usage/time distributions and classifier overhead) from a read-only
projection, where no acceptance or cohort evidence exists yet.

The report is deterministic: identical bundles produce byte-identical output
(apart from floats, which the CLI rounds) and every report carries a
``report_hash`` over its own content.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .canonical import canonical_hash
from .contract import USAGE_INT_FIELDS, normalize_timestamp
from .errors import ImpactError
from .evaluation import wilson_lower_bound

IMPACT_BUNDLE_VERSION = "1.0"
IMPACT_REPORT_VERSION = "1"
IMPACT_REPORT_KIND = "accepted_task_impact"

OUTCOMES = ("accepted", "rejected", "abandoned", "in_progress", "unknown")
TERMINAL_OUTCOMES = ("rejected", "abandoned")
COHORTS = ("treatment", "control", "unknown")
PHASES = ("active", "queue", "idle", "human_review")
ATTEMPT_KINDS = ("execute", "review", "fix", "retry", "classify", "other")

# Attribution strength, strongest first. The grade says how much causal weight
# the comparison can carry; it is never inferred from the size of the effect.
ATTRIBUTION_GRADES = (
    "controlled",
    "quasi_experimental",
    "matched_observational",
    "descriptive",
    "unmeasurable",
)

# Default pre-treatment matching covariates. Deliberately excludes post-treatment
# variables (final diff size, test outcome, elapsed time) per measurement.md.
DEFAULT_MATCH_ON = (
    "repo",
    "task_class",
    "scope",
    "provider",
    "model",
    "harness",
    "effort",
    "host",
    "workload",
    "cache_state",
    "baseline_complexity",
    "concurrency",
)

_BUNDLE_KEYS = frozenset(
    {"schema_version", "generated_by", "evidence", "match_on", "work_items", "classifier_overhead"}
)
_EVIDENCE_KEYS = frozenset({"randomized", "parallel_pre_trends", "design", "assignment_logged"})
_WORK_ITEM_KEYS = frozenset(
    {
        "work_item_id",
        "repo",
        "task_class",
        "scope",
        "provider",
        "model",
        "harness",
        "effort",
        "host",
        "baseline_complexity",
        "workload",
        "cache_state",
        "concurrency",
        "ready_at",
        "accepted_at",
        "acceptance_kind",
        "outcome",
        "cohort",
        "intervention_id",
        "post_merge_followup_days",
        "attempts",
        "quality",
    }
)
_ATTEMPT_KEYS = frozenset(
    {
        "attempt_id",
        "kind",
        "phase",
        "started_at",
        "ended_at",
        "usage",
        "price",
        "cost_usd",
        "first_pass",
        "review_rounds",
        "tests_executed",
        "tests_skipped",
        "verification_scope",
    }
)
_QUALITY_KEYS = frozenset(
    {
        "first_pass_verified",
        "review_rounds",
        "reopened",
        "reverted",
        "regression",
        "tests_executed",
        "tests_skipped",
        "verification_scope",
    }
)
_OVERHEAD_KEYS = frozenset(
    {
        "work_item_id",
        "requests",
        "input_tokens",
        "output_tokens",
        "latency_ms",
        "retries",
        "cache_hits",
    }
)
_PRICE_KEYS = frozenset({"input_per_million_usd", "output_per_million_usd"})


def _require_mapping(raw: Any, what: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ImpactError(f"{what} must be a JSON object")
    return raw


def _reject_unknown_keys(raw: Mapping[str, Any], allowed: frozenset[str], what: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ImpactError(f"{what} has unknown key(s): {', '.join(unknown)}")


def _require_str(raw: Mapping[str, Any], field: str, what: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value:
        raise ImpactError(f"{what} field {field!r} must be a non-empty string")
    return value


def _optional_str(raw: Mapping[str, Any], field: str, what: str) -> str | None:
    value = raw.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ImpactError(f"{what} field {field!r} must be a string or null")
    return value or None


def _optional_timestamp(raw: Mapping[str, Any], field: str, what: str) -> str | None:
    value = raw.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ImpactError(f"{what} field {field!r} must be a timestamp string or null")
    try:
        return normalize_timestamp(value)
    except Exception as exc:  # ContractError carries the reason
        raise ImpactError(f"{what} field {field!r} is not a valid timestamp: {exc}") from exc


def _finite_number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ImpactError(f"{what} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ImpactError(f"{what} must be finite")
    return number


def _optional_number(raw: Mapping[str, Any], field: str, what: str) -> float | None:
    value = raw.get(field)
    if value is None:
        return None
    return _finite_number(value, f"{what} field {field!r}")


def _optional_int(raw: Mapping[str, Any], field: str, what: str) -> int | None:
    value = raw.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ImpactError(f"{what} field {field!r} must be an integer or null")
    return value


def _optional_nonneg_int(raw: Mapping[str, Any], field: str, what: str) -> int | None:
    value = _optional_int(raw, field, what)
    if value is not None and value < 0:
        raise ImpactError(f"{what} field {field!r} must be nonnegative")
    return value


def _optional_bool(raw: Mapping[str, Any], field: str, what: str) -> bool | None:
    value = raw.get(field)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ImpactError(f"{what} field {field!r} must be a boolean or null")
    return value


def _optional_usage(raw: Mapping[str, Any], field: str, what: str) -> dict[str, float | None] | None:
    value = raw.get(field)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ImpactError(f"{what} field {field!r} must be an object or null")
    unknown = sorted(set(value) - set(USAGE_INT_FIELDS))
    if unknown:
        raise ImpactError(f"{what} field {field!r} has unknown token key(s): {', '.join(unknown)}")
    normalized: dict[str, float | None] = {}
    for key in USAGE_INT_FIELDS:
        item = value.get(key)
        if item is None:
            normalized[key] = None
        elif isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ImpactError(f"{what} field {field!r}.{key} must be a number or null")
        elif not math.isfinite(float(item)) or float(item) < 0:
            raise ImpactError(f"{what} field {field!r}.{key} must be finite and nonnegative")
        else:
            normalized[key] = float(item)
    return normalized


def _optional_price(raw: Mapping[str, Any], field: str, what: str) -> dict[str, float] | None:
    value = raw.get(field)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ImpactError(f"{what} field {field!r} must be an object or null")
    unknown = sorted(set(value) - _PRICE_KEYS)
    if unknown:
        raise ImpactError(f"{what} field {field!r} has unknown price key(s): {', '.join(unknown)}")
    normalized: dict[str, float] = {}
    for key in sorted(_PRICE_KEYS):
        item = value.get(key)
        if item is None:
            continue
        number = _finite_number(item, f"{what} field {field!r}.{key}")
        if number < 0:
            raise ImpactError(f"{what} field {field!r}.{key} must be nonnegative")
        normalized[key] = number
    if not normalized:
        raise ImpactError(f"{what} field {field!r} must carry at least one price")
    return normalized


# -- data model -------------------------------------------------------------


@dataclass(frozen=True)
class Attempt:
    """One observed attempt attributable to a work item."""

    attempt_id: str
    kind: str = "execute"
    phase: str = "active"
    started_at: str | None = None
    ended_at: str | None = None
    usage: Mapping[str, float | None] | None = None
    price: Mapping[str, float] | None = None
    cost_usd: float | None = None
    first_pass: bool | None = None
    review_rounds: int | None = None
    tests_executed: int | None = None
    tests_skipped: int | None = None
    verification_scope: str | None = None

    def resolved_cost_usd(self) -> float | None:
        """Return the attempt cost, or ``None`` when it is not measurable.

        An explicit ``cost_usd`` wins. Otherwise input/output tokens and both
        prices are required: a missing price or a missing usage count stays
        unknown, never zero. Cache tokens are reported separately and are not
        priced here, so no hidden cache cost is invented.
        """
        if self.cost_usd is not None:
            return float(self.cost_usd)
        if self.usage is None or self.price is None:
            return None
        input_tokens = self.usage.get("input_tokens")
        output_tokens = self.usage.get("output_tokens")
        price_in = self.price.get("input_per_million_usd")
        price_out = self.price.get("output_per_million_usd")
        if None in (input_tokens, output_tokens, price_in, price_out):
            return None
        return (float(input_tokens) * float(price_in) + float(output_tokens) * float(price_out)) / 1_000_000.0


@dataclass(frozen=True)
class WorkItem:
    """One eligible assigned task and the attempts spent on it."""

    work_item_id: str
    outcome: str
    cohort: str = "unknown"
    repo: str | None = None
    task_class: str | None = None
    scope: str | None = None
    provider: str | None = None
    model: str | None = None
    harness: str | None = None
    effort: str | None = None
    host: str | None = None
    baseline_complexity: str | None = None
    workload: str | None = None
    cache_state: str | None = None
    concurrency: str | None = None
    ready_at: str | None = None
    accepted_at: str | None = None
    acceptance_kind: str | None = None
    intervention_id: str | None = None
    post_merge_followup_days: int | None = None
    attempts: tuple[Attempt, ...] = ()
    quality: Mapping[str, Any] | None = None

    @property
    def accepted(self) -> bool:
        return self.outcome == "accepted"

    @property
    def censored(self) -> bool:
        """Still open: not accepted and not terminally rejected/abandoned."""
        return not self.accepted and self.outcome not in TERMINAL_OUTCOMES

    def covariate(self, field: str) -> str:
        value = getattr(self, field, None)
        return str(value) if value is not None and value != "" else "unknown"


@dataclass(frozen=True)
class ClassifierOverhead:
    """Measured classifier spend, attributed to a work item when possible."""

    work_item_id: str | None = None
    requests: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float | None = None
    retries: int = 0
    cache_hits: int = 0


@dataclass(frozen=True)
class ImpactDataset:
    """Validated impact input plus the evidence level of its assignment."""

    work_items: tuple[WorkItem, ...]
    classifier_overhead: tuple[ClassifierOverhead, ...]
    evidence: Mapping[str, Any]
    match_on: tuple[str, ...]
    bundle_schema_version: str | None = None
    generated_by: str | None = None

    def dataset_hash(self) -> str:
        return canonical_hash(
            {
                "schema_version": self.bundle_schema_version,
                "evidence": dict(self.evidence),
                "match_on": list(self.match_on),
                "work_items": [_work_item_content(item) for item in self.work_items],
                "classifier_overhead": [_overhead_content(item) for item in self.classifier_overhead],
            }
        )


def _work_item_content(item: WorkItem) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "work_item_id": item.work_item_id,
        "outcome": item.outcome,
        "cohort": item.cohort,
        "attempts": [
            {
                "attempt_id": attempt.attempt_id,
                "kind": attempt.kind,
                "phase": attempt.phase,
                "started_at": attempt.started_at,
                "ended_at": attempt.ended_at,
                "usage": dict(attempt.usage) if attempt.usage is not None else None,
                "price": dict(attempt.price) if attempt.price is not None else None,
                "cost_usd": attempt.cost_usd,
            }
            for attempt in item.attempts
        ],
    }
    for field in DEFAULT_MATCH_ON:
        payload[field] = getattr(item, field, None)
    for field in (
        "ready_at",
        "accepted_at",
        "acceptance_kind",
        "intervention_id",
        "post_merge_followup_days",
        "quality",
    ):
        payload[field] = getattr(item, field, None)
    return payload


def _overhead_content(item: ClassifierOverhead) -> dict[str, Any]:
    return {
        "work_item_id": item.work_item_id,
        "requests": item.requests,
        "input_tokens": item.input_tokens,
        "output_tokens": item.output_tokens,
        "latency_ms": item.latency_ms,
        "retries": item.retries,
        "cache_hits": item.cache_hits,
    }


# -- bundle loading ---------------------------------------------------------


def _normalize_attempt(raw: Any, what: str) -> Attempt:
    attempt = _require_mapping(raw, what)
    _reject_unknown_keys(attempt, _ATTEMPT_KEYS, what)
    kind = _optional_str(attempt, "kind", what) or "execute"
    if kind not in ATTEMPT_KINDS:
        raise ImpactError(f"{what} kind {kind!r} is not one of {', '.join(ATTEMPT_KINDS)}")
    phase = _optional_str(attempt, "phase", what) or "active"
    if phase not in PHASES:
        raise ImpactError(f"{what} phase {phase!r} is not one of {', '.join(PHASES)}")
    return Attempt(
        attempt_id=_require_str(attempt, "attempt_id", what),
        kind=kind,
        phase=phase,
        started_at=_optional_timestamp(attempt, "started_at", what),
        ended_at=_optional_timestamp(attempt, "ended_at", what),
        usage=_optional_usage(attempt, "usage", what),
        price=_optional_price(attempt, "price", what),
        cost_usd=_optional_number(attempt, "cost_usd", what),
        first_pass=_optional_bool(attempt, "first_pass", what),
        review_rounds=_optional_nonneg_int(attempt, "review_rounds", what),
        tests_executed=_optional_nonneg_int(attempt, "tests_executed", what),
        tests_skipped=_optional_nonneg_int(attempt, "tests_skipped", what),
        verification_scope=_optional_str(attempt, "verification_scope", what),
    )


def _normalize_quality(raw: Any, what: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    quality = _require_mapping(raw, what)
    _reject_unknown_keys(quality, _QUALITY_KEYS, what)
    normalized: dict[str, Any] = {}
    for field in ("first_pass_verified", "reopened", "reverted", "regression"):
        normalized[field] = _optional_bool(quality, field, what)
    for field in ("review_rounds", "tests_executed", "tests_skipped"):
        normalized[field] = _optional_nonneg_int(quality, field, what)
    normalized["verification_scope"] = _optional_str(quality, "verification_scope", what)
    return normalized


def _normalize_work_item(raw: Any, seen_ids: set[str]) -> WorkItem:
    item = _require_mapping(raw, "work item")
    _reject_unknown_keys(item, _WORK_ITEM_KEYS, "work item")
    work_item_id = _require_str(item, "work_item_id", "work item")
    if work_item_id in seen_ids:
        raise ImpactError(f"bundle contains two work items with the same id {work_item_id!r}")
    seen_ids.add(work_item_id)

    outcome = _require_str(item, "outcome", "work item")
    if outcome not in OUTCOMES:
        raise ImpactError(f"work item outcome {outcome!r} is not one of {', '.join(OUTCOMES)}")
    cohort = _optional_str(item, "cohort", "work item") or "unknown"
    if cohort not in COHORTS:
        raise ImpactError(f"work item cohort {cohort!r} is not one of {', '.join(COHORTS)}")

    raw_attempts = item.get("attempts", [])
    if not isinstance(raw_attempts, list):
        raise ImpactError("work item field 'attempts' must be a list")
    attempts = [_normalize_attempt(entry, f"work item {work_item_id} attempt") for entry in raw_attempts]

    return WorkItem(
        work_item_id=work_item_id,
        outcome=outcome,
        cohort=cohort,
        repo=_optional_str(item, "repo", "work item"),
        task_class=_optional_str(item, "task_class", "work item"),
        scope=_optional_str(item, "scope", "work item"),
        provider=_optional_str(item, "provider", "work item"),
        model=_optional_str(item, "model", "work item"),
        harness=_optional_str(item, "harness", "work item"),
        effort=_optional_str(item, "effort", "work item"),
        host=_optional_str(item, "host", "work item"),
        baseline_complexity=_optional_str(item, "baseline_complexity", "work item"),
        workload=_optional_str(item, "workload", "work item"),
        cache_state=_optional_str(item, "cache_state", "work item"),
        concurrency=_optional_str(item, "concurrency", "work item"),
        ready_at=_optional_timestamp(item, "ready_at", "work item"),
        accepted_at=_optional_timestamp(item, "accepted_at", "work item"),
        acceptance_kind=_optional_str(item, "acceptance_kind", "work item"),
        intervention_id=_optional_str(item, "intervention_id", "work item"),
        post_merge_followup_days=_optional_nonneg_int(item, "post_merge_followup_days", "work item"),
        attempts=tuple(attempts),
        quality=_normalize_quality(item.get("quality"), f"work item {work_item_id} quality"),
    )


def _normalize_overhead(raw: Any) -> ClassifierOverhead:
    entry = _require_mapping(raw, "classifier overhead")
    _reject_unknown_keys(entry, _OVERHEAD_KEYS, "classifier overhead")
    return ClassifierOverhead(
        work_item_id=_optional_str(entry, "work_item_id", "classifier overhead"),
        requests=_optional_nonneg_int(entry, "requests", "classifier overhead") or 0,
        input_tokens=_optional_nonneg_int(entry, "input_tokens", "classifier overhead"),
        output_tokens=_optional_nonneg_int(entry, "output_tokens", "classifier overhead"),
        latency_ms=_optional_number(entry, "latency_ms", "classifier overhead"),
        retries=_optional_nonneg_int(entry, "retries", "classifier overhead") or 0,
        cache_hits=_optional_nonneg_int(entry, "cache_hits", "classifier overhead") or 0,
    )


def normalize_impact_bundle(raw: Any) -> ImpactDataset:
    """Validate an impact bundle and return a typed :class:`ImpactDataset`."""
    raw = _require_mapping(raw, "impact bundle")
    _reject_unknown_keys(raw, _BUNDLE_KEYS, "impact bundle")
    schema_version = raw.get("schema_version")
    if schema_version != IMPACT_BUNDLE_VERSION:
        raise ImpactError(
            f"impact bundle schema_version {schema_version!r} is not supported; "
            f"expected {IMPACT_BUNDLE_VERSION!r}"
        )

    raw_evidence = raw.get("evidence")
    if raw_evidence is None:
        evidence: dict[str, Any] = {}
    else:
        evidence = _require_mapping(raw_evidence, "bundle evidence")
        _reject_unknown_keys(evidence, _EVIDENCE_KEYS, "bundle evidence")

    match_on_raw = raw.get("match_on")
    if match_on_raw is None:
        match_on = DEFAULT_MATCH_ON
    else:
        if not isinstance(match_on_raw, list) or not all(
            isinstance(field, str) and field in DEFAULT_MATCH_ON for field in match_on_raw
        ):
            raise ImpactError(
                "bundle match_on must be a list of allowed covariates: "
                + ", ".join(DEFAULT_MATCH_ON)
            )
        match_on = tuple(match_on_raw)

    raw_items = raw.get("work_items", [])
    if not isinstance(raw_items, list):
        raise ImpactError("bundle field 'work_items' must be a list")
    seen_ids: set[str] = set()
    work_items = [_normalize_work_item(entry, seen_ids) for entry in raw_items]

    raw_overhead = raw.get("classifier_overhead", [])
    if not isinstance(raw_overhead, list):
        raise ImpactError("bundle field 'classifier_overhead' must be a list")
    overhead = [_normalize_overhead(entry) for entry in raw_overhead]

    return ImpactDataset(
        work_items=tuple(work_items),
        classifier_overhead=tuple(overhead),
        evidence=evidence,
        match_on=match_on,
        bundle_schema_version=schema_version,
        generated_by=raw.get("generated_by"),
    )


def load_impact_bundle(path: str | Path) -> ImpactDataset:
    """Load and validate an impact bundle JSON document."""
    bundle_path = Path(path)
    try:
        text = bundle_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ImpactError(f"cannot read impact bundle {bundle_path}: {exc}") from exc
    try:
        raw = json.loads(text, parse_constant=_reject_json_constant)
    except ValueError as exc:
        raise ImpactError(f"impact bundle {bundle_path} is not valid JSON: {exc}") from exc
    return normalize_impact_bundle(raw)


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant {value!r} is not allowed")


# -- config -----------------------------------------------------------------


@dataclass(frozen=True)
class ImpactConfig:
    """Matching, uncertainty and censoring parameters for one impact run."""

    primary_outcome: str = "time_to_accepted_seconds"
    min_stratum_size: int = 1
    imbalance_threshold: float = 0.25
    min_overlap: float = 0.5
    bootstrap_resamples: int = 2000
    confidence: float = 0.95
    followup_window_days: int = 7

    def validated(self) -> "ImpactConfig":
        if self.primary_outcome not in _OUTCOME_EXTRACTORS:
            raise ImpactError(f"unknown primary outcome {self.primary_outcome!r}")
        if self.min_stratum_size < 1:
            raise ImpactError("min_stratum_size must be >= 1")
        if not (0.0 <= self.imbalance_threshold <= 1.0):
            raise ImpactError("imbalance_threshold must be in [0,1]")
        if not (0.0 <= self.min_overlap <= 1.0):
            raise ImpactError("min_overlap must be in [0,1]")
        if self.bootstrap_resamples < 1:
            raise ImpactError("bootstrap_resamples must be >= 1")
        if not (0.0 < self.confidence < 1.0):
            raise ImpactError("confidence must be strictly between 0 and 1")
        if self.followup_window_days < 0:
            raise ImpactError("followup_window_days must be nonnegative")
        return self


# -- statistics primitives --------------------------------------------------


def _parse_timestamp(value: str) -> float:
    """Return a timestamp as POSIX seconds (canonical UTC microseconds input)."""
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _percentile(sorted_values: Sequence[float], quantile: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = quantile * (len(sorted_values) - 1)
    low = int(math.floor(position))
    high = int(math.ceil(position))
    if low == high:
        return float(sorted_values[low])
    weight = position - low
    return float(sorted_values[low] * (1.0 - weight) + sorted_values[high] * weight)


def _numeric_stats(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "mean": None, "median": None, "p90": None, "p95": None, "min": None, "max": None}
    ordered = sorted(float(value) for value in values)
    return {
        "n": len(ordered),
        "mean": sum(ordered) / len(ordered),
        "median": _percentile(ordered, 0.5),
        "p90": _percentile(ordered, 0.9),
        "p95": _percentile(ordered, 0.95),
        "min": ordered[0],
        "max": ordered[-1],
    }


def _rate_stats(values: Sequence[bool]) -> dict[str, Any]:
    total = len(values)
    successes = sum(1 for value in values if value)
    return {
        "n": total,
        "successes": successes,
        "rate": (successes / total) if total else None,
        "wilson_low": wilson_lower_bound(successes, total) if total else None,
        "wilson_high": _wilson_upper_bound(successes, total) if total else None,
    }


def _wilson_upper_bound(successes: int, total: int, *, z: float = 1.96) -> float | None:
    if total <= 0:
        return None
    phat = successes / total
    denominator = 1 + z * z / total
    centre = phat + z * z / (2 * total)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * total)) / total)
    return min(1.0, (centre + margin) / denominator)


def _distribution(values: Sequence[str]) -> dict[str, float]:
    if not values:
        return {}
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    total = len(values)
    return {key: count / total for key, count in sorted(counts.items())}


def _total_variation(left: Sequence[str], right: Sequence[str]) -> float:
    """Total-variation distance between two categorical distributions in [0,1]."""
    left_dist = _distribution(left)
    right_dist = _distribution(right)
    keys = set(left_dist) | set(right_dist)
    return 0.5 * sum(abs(left_dist.get(key, 0.0) - right_dist.get(key, 0.0)) for key in keys)


def _union_seconds(intervals: Sequence[tuple[float, float]]) -> float:
    ordered = sorted((start, end) for start, end in intervals if end > start)
    if not ordered:
        return 0.0
    total = 0.0
    current_start, current_end = ordered[0]
    for start, end in ordered[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            total += current_end - current_start
            current_start, current_end = start, end
    return total + (current_end - current_start)


def _sum_seconds(intervals: Sequence[tuple[float, float]]) -> float:
    return sum(end - start for start, end in intervals if end > start)


# -- per-work-item derivations ---------------------------------------------


def _item_intervals(item: WorkItem, phase: str) -> tuple[list[tuple[float, float]], int]:
    intervals: list[tuple[float, float]] = []
    missing = 0
    for attempt in item.attempts:
        if attempt.phase != phase:
            continue
        if attempt.started_at is None or attempt.ended_at is None:
            missing += 1
            continue
        start = _parse_timestamp(attempt.started_at)
        end = _parse_timestamp(attempt.ended_at)
        if end < start:
            raise ImpactError(
                f"work item {item.work_item_id!r} attempt {attempt.attempt_id!r} ends before it starts"
            )
        intervals.append((start, end))
    return intervals, missing


def _item_cost_usd(item: WorkItem) -> tuple[float | None, int]:
    """Return ``(cost, unmeasured_attempts)`` for one work item.

    A work item with no attempts has unknown cost, not zero. One unmeasured
    attempt makes the whole item's cost unknown; the measured part is never
    silently presented as the total.
    """
    if not item.attempts:
        return None, 0
    total = 0.0
    unmeasured = 0
    for attempt in item.attempts:
        cost = attempt.resolved_cost_usd()
        if cost is None:
            unmeasured += 1
        else:
            total += cost
    if unmeasured:
        return None, unmeasured
    return total, 0


def _time_to_accept_seconds(item: WorkItem) -> float | None:
    if not item.accepted or item.ready_at is None or item.accepted_at is None:
        return None
    return _parse_timestamp(item.accepted_at) - _parse_timestamp(item.ready_at)


def _attempt_count(item: WorkItem) -> float:
    return float(len(item.attempts))


def _review_rounds(item: WorkItem) -> float | None:
    quality = item.quality or {}
    if quality.get("review_rounds") is not None:
        return float(quality["review_rounds"])
    rounds = [attempt.review_rounds for attempt in item.attempts if attempt.review_rounds is not None]
    if not rounds:
        return None
    return float(sum(rounds))


def _first_pass_verified(item: WorkItem) -> bool | None:
    quality = item.quality or {}
    if quality.get("first_pass_verified") is not None:
        return bool(quality["first_pass_verified"])
    attempts = [attempt.first_pass for attempt in item.attempts if attempt.first_pass is not None]
    if not attempts:
        return None
    return all(attempts)


def _rework(item: WorkItem) -> bool | None:
    quality = item.quality or {}
    flags = [quality.get("reopened"), quality.get("reverted"), quality.get("regression")]
    known = [bool(flag) for flag in flags if flag is not None]
    if known:
        return any(known)
    return None


_OUTCOME_EXTRACTORS: dict[str, Any] = {
    "time_to_accepted_seconds": _time_to_accept_seconds,
    "cost_usd": lambda item: _item_cost_usd(item)[0],
    "attempt_count": _attempt_count,
    "review_rounds": _review_rounds,
}


def _outcome_value(item: WorkItem, outcome: str) -> float | None:
    return _OUTCOME_EXTRACTORS[outcome](item)


def _match_key(item: WorkItem, match_on: Sequence[str]) -> tuple[str, ...]:
    return tuple(item.covariate(field) for field in match_on)


# -- cohort comparison ------------------------------------------------------


def _arm_values(
    items: Sequence[WorkItem], outcome: str
) -> tuple[dict[str, list[float]], dict[str, int]]:
    values: dict[str, list[float]] = {"treatment": [], "control": []}
    missing: dict[str, int] = {"treatment": 0, "control": 0}
    for item in items:
        if item.cohort not in ("treatment", "control"):
            continue
        value = _outcome_value(item, outcome)
        if value is None:
            missing[item.cohort] += 1
        else:
            values[item.cohort].append(float(value))
    return values, missing


def _difference_ci_bootstrap(
    strata: Sequence[dict[str, Any]],
    *,
    resamples: int,
    confidence: float,
    seed_material: str,
) -> dict[str, Any] | None:
    """Deterministic stratified bootstrap CI for the matched mean difference."""
    usable = [
        stratum
        for stratum in strata
        if stratum["treatment_values"] and stratum["control_values"]
    ]
    if not usable:
        return None
    rng = random.Random(int(canonical_hash(seed_material)[:16], 16))
    alpha = (1.0 - confidence) / 2.0
    differences: list[float] = []
    for _ in range(resamples):
        treatment_sum = 0.0
        control_sum = 0.0
        treatment_n = 0
        control_n = 0
        for stratum in usable:
            treatment_values = stratum["treatment_values"]
            control_values = stratum["control_values"]
            for _ in range(len(treatment_values)):
                treatment_sum += treatment_values[rng.randrange(len(treatment_values))]
                treatment_n += 1
            for _ in range(len(control_values)):
                control_sum += control_values[rng.randrange(len(control_values))]
                control_n += 1
        differences.append(treatment_sum / treatment_n - control_sum / control_n)
    ordered = sorted(differences)
    return {
        "ci_low": _percentile(ordered, alpha),
        "ci_high": _percentile(ordered, 1.0 - alpha),
        "bootstrap_samples": resamples,
    }


def _match_outcome(
    items: Sequence[WorkItem],
    outcome: str,
    match_on: Sequence[str],
    config: ImpactConfig,
) -> dict[str, Any]:
    keyed: dict[tuple[str, ...], dict[str, list[WorkItem]]] = {}
    for item in items:
        if item.cohort not in ("treatment", "control"):
            continue
        keyed.setdefault(_match_key(item, match_on), {"treatment": [], "control": []})[item.cohort].append(item)

    strata: list[dict[str, Any]] = []
    matched_treatment: list[WorkItem] = []
    matched_control: list[WorkItem] = []
    for key in sorted(keyed):
        arms = keyed[key]
        if len(arms["treatment"]) < config.min_stratum_size or len(arms["control"]) < config.min_stratum_size:
            continue
        treatment_values = [
            value for value in (_outcome_value(item, outcome) for item in arms["treatment"]) if value is not None
        ]
        control_values = [
            value for value in (_outcome_value(item, outcome) for item in arms["control"]) if value is not None
        ]
        matched_treatment.extend(arms["treatment"])
        matched_control.extend(arms["control"])
        strata.append(
            {
                "key": list(key),
                "n_treatment": len(arms["treatment"]),
                "n_control": len(arms["control"]),
                "treatment_values": [float(value) for value in treatment_values],
                "control_values": [float(value) for value in control_values],
                "mean_treatment": (sum(treatment_values) / len(treatment_values)) if treatment_values else None,
                "mean_control": (sum(control_values) / len(control_values)) if control_values else None,
            }
        )

    treatment_all = [
        value
        for value in (_outcome_value(item, outcome) for item in items if item.cohort == "treatment")
        if value is not None
    ]
    control_all = [
        value
        for value in (_outcome_value(item, outcome) for item in items if item.cohort == "control")
        if value is not None
    ]
    naive = None
    if treatment_all and control_all:
        naive = sum(treatment_all) / len(treatment_all) - sum(control_all) / len(control_all)

    matched = None
    matched_treatment_values = [
        value for value in (_outcome_value(item, outcome) for item in matched_treatment) if value is not None
    ]
    matched_control_values = [
        value for value in (_outcome_value(item, outcome) for item in matched_control) if value is not None
    ]
    if matched_treatment_values and matched_control_values:
        difference = sum(matched_treatment_values) / len(matched_treatment_values) - sum(
            matched_control_values
        ) / len(matched_control_values)
        matched = {
            "difference": difference,
            "n_treatment": len(matched_treatment_values),
            "n_control": len(matched_control_values),
            "strata": len(strata),
        }
        ci = _difference_ci_bootstrap(
            strata,
            resamples=config.bootstrap_resamples,
            confidence=config.confidence,
            seed_material=canonical_hash(["matched-ci", outcome, [list(stratum["key"]) for stratum in strata]]),
        )
        if ci is not None:
            matched.update(ci)

    def _relative(control_mean: float | None, difference: float | None) -> dict[str, Any]:
        if difference is None or control_mean is None:
            return {"value": None, "reason": "no_comparison"}
        if control_mean == 0:
            return {"value": None, "reason": "zero_baseline"}
        return {"value": difference / control_mean, "reason": None}

    control_all_mean = (sum(control_all) / len(control_all)) if control_all else None
    matched_control_mean = (
        sum(matched_control_values) / len(matched_control_values) if matched_control_values else None
    )
    missing = {"treatment": 0, "control": 0}
    for item in items:
        if item.cohort in missing and _outcome_value(item, outcome) is None:
            missing[item.cohort] += 1
    return {
        "treatment": _numeric_stats(treatment_all),
        "control": _numeric_stats(control_all),
        "missing": missing,
        "naive_difference": naive,
        "naive_relative_change": _relative(control_all_mean, naive),
        "matched": matched,
        "matched_relative_change": _relative(
            matched_control_mean, matched["difference"] if matched else None
        ),
        "strata": [
            {
                "key": stratum["key"],
                "n_treatment": stratum["n_treatment"],
                "n_control": stratum["n_control"],
                "mean_treatment": stratum["mean_treatment"],
                "mean_control": stratum["mean_control"],
            }
            for stratum in strata
        ],
        "matched_items": len(matched_treatment) + len(matched_control),
        "excluded_treatment": sum(1 for item in items if item.cohort == "treatment") - len(matched_treatment),
        "excluded_control": sum(1 for item in items if item.cohort == "control") - len(matched_control),
    }


def _boolean_outcome(item: WorkItem, outcome: str) -> bool | None:
    if outcome == "first_pass_verified":
        return _first_pass_verified(item)
    if outcome == "rework":
        return _rework(item)
    raise ImpactError(f"unknown boolean outcome {outcome!r}")


def _rate_outcome(
    items: Sequence[WorkItem],
    outcome: str,
    match_on: Sequence[str],
    config: ImpactConfig,
) -> dict[str, Any]:
    """Boolean outcome (first-pass verification, rework) as an arm rate + CI.

    Rates are also computed on the same matched cohort as the numeric outcomes
    so a rate is never taken from a different, easier subset.
    """
    values: dict[str, list[bool]] = {"treatment": [], "control": []}
    missing = {"treatment": 0, "control": 0}
    for item in items:
        if item.cohort not in ("treatment", "control"):
            continue
        value = _boolean_outcome(item, outcome)
        if value is None:
            missing[item.cohort] += 1
        else:
            values[item.cohort].append(bool(value))

    keyed: dict[tuple[str, ...], dict[str, list[bool]]] = {}
    for item in items:
        if item.cohort not in ("treatment", "control"):
            continue
        value = _boolean_outcome(item, outcome)
        if value is None:
            continue
        keyed.setdefault(_match_key(item, match_on), {"treatment": [], "control": []})[item.cohort].append(bool(value))
    matched_treatment: list[bool] = []
    matched_control: list[bool] = []
    for key in sorted(keyed):
        arms = keyed[key]
        if len(arms["treatment"]) < config.min_stratum_size or len(arms["control"]) < config.min_stratum_size:
            continue
        matched_treatment.extend(arms["treatment"])
        matched_control.extend(arms["control"])

    treatment = _rate_stats(values["treatment"])
    control = _rate_stats(values["control"])
    difference = None
    if treatment["rate"] is not None and control["rate"] is not None:
        difference = treatment["rate"] - control["rate"]
    matched = None
    matched_treatment_stats = _rate_stats(matched_treatment)
    matched_control_stats = _rate_stats(matched_control)
    if matched_treatment_stats["rate"] is not None and matched_control_stats["rate"] is not None:
        matched = {
            "treatment": matched_treatment_stats,
            "control": matched_control_stats,
            "difference": matched_treatment_stats["rate"] - matched_control_stats["rate"],
            "n_treatment": len(matched_treatment),
            "n_control": len(matched_control),
        }
    return {
        "treatment": treatment,
        "control": control,
        "missing": missing,
        "naive_difference": difference,
        "matched": matched,
    }


def _imbalance_report(
    items: Sequence[WorkItem],
    match_on: Sequence[str],
    extra_covariates: Sequence[str],
    matched_items: Sequence[WorkItem],
    config: ImpactConfig,
) -> dict[str, Any]:
    treatment = [item for item in items if item.cohort == "treatment"]
    control = [item for item in items if item.cohort == "control"]
    matched_treatment = [item for item in matched_items if item.cohort == "treatment"]
    matched_control = [item for item in matched_items if item.cohort == "control"]
    covariates: dict[str, Any] = {}
    flagged: list[str] = []
    for field in match_on:
        full = _total_variation(
            [item.covariate(field) for item in treatment],
            [item.covariate(field) for item in control],
        )
        matched = _total_variation(
            [item.covariate(field) for item in matched_treatment],
            [item.covariate(field) for item in matched_control],
        )
        entry = {"full_tvd": full, "matched_tvd": matched}
        covariates[field] = entry
        if full > config.imbalance_threshold:
            flagged.append(field)
    extra: dict[str, Any] = {}
    for field in extra_covariates:
        if field in match_on:
            continue
        full = _total_variation(
            [item.covariate(field) for item in treatment],
            [item.covariate(field) for item in control],
        )
        matched = _total_variation(
            [item.covariate(field) for item in matched_treatment],
            [item.covariate(field) for item in matched_control],
        )
        extra[field] = {"full_tvd": full, "matched_tvd": matched}
        if full > config.imbalance_threshold:
            flagged.append(field)
    return {
        "match_on": list(match_on),
        "covariates": covariates,
        "extra_covariates": extra,
        "imbalance_threshold": config.imbalance_threshold,
        "imbalanced_covariates": sorted(set(flagged)),
    }


# -- rollups ----------------------------------------------------------------


def _accepted_task_rollup(items: Sequence[WorkItem]) -> dict[str, Any]:
    eligible = len(items)
    accepted = [item for item in items if item.accepted]
    rejected = [item for item in items if item.outcome == "rejected"]
    abandoned = [item for item in items if item.outcome == "abandoned"]
    censored = [item for item in items if item.censored]

    measured_cost = 0.0
    measured_cost_items = 0
    unmeasured_cost_items = 0
    for item in items:
        cost, unmeasured = _item_cost_usd(item)
        if cost is None:
            unmeasured_cost_items += 1
        else:
            measured_cost += cost
            measured_cost_items += 1

    attempt_total = sum(len(item.attempts) for item in items)
    per_phase_totals: dict[str, float] = {}
    parallel_overlap = 0.0
    global_active_intervals: list[tuple[float, float]] = []
    for phase in PHASES:
        phase_sum = 0.0
        for item in items:
            intervals, _missing = _item_intervals(item, phase)
            phase_sum += _union_seconds(intervals)
            if phase == "active":
                global_active_intervals.extend(intervals)
        per_phase_totals[phase] = phase_sum
    global_active_union = _union_seconds(global_active_intervals)
    parallel_overlap = per_phase_totals["active"] - global_active_union

    cost_per_accepted = None
    cost_per_accepted_reason = None
    if not accepted:
        cost_per_accepted_reason = "no_accepted_tasks"
    elif unmeasured_cost_items:
        cost_per_accepted_reason = "incomplete_price_or_usage_coverage"
    else:
        cost_per_accepted = measured_cost / len(accepted)

    success_rate = (len(accepted) / eligible) if eligible else None
    attempts_per_accepted = (attempt_total / len(accepted)) if accepted else None

    return {
        "eligible": eligible,
        "accepted": len(accepted),
        "rejected": len(rejected),
        "abandoned": len(abandoned),
        "censored": len(censored),
        "still_open": len(censored),
        "success_rate": success_rate,
        "zero_accepted_tasks": not accepted,
        "attempts": {
            "total": attempt_total,
            "mean_per_eligible": (attempt_total / eligible) if eligible else None,
            "per_accepted": attempts_per_accepted,
        },
        "cost": {
            "measured_attempt_cost_usd": measured_cost,
            "measured_cost_items": measured_cost_items,
            "unmeasured_cost_items": unmeasured_cost_items,
            "price_coverage": (measured_cost_items / eligible) if eligible else None,
            "cost_per_accepted_usd": cost_per_accepted,
            "cost_per_accepted_reason": cost_per_accepted_reason,
        },
        "execution_time_seconds": {
            "active_union": per_phase_totals["active"],
            "queue": per_phase_totals["queue"],
            "idle": per_phase_totals["idle"],
            "human_review": per_phase_totals["human_review"],
            "global_active_union": global_active_union,
            "parallel_overlap": parallel_overlap,
            "note": (
                "Per-item active time is the union of that item's observed active intervals; "
                "the global union is wall time across items and the overlap is parallel work "
                "counted once, never summed as wall time."
            ),
        },
    }


def _classifier_overhead_rollup(overhead: Sequence[ClassifierOverhead]) -> dict[str, Any]:
    requests = sum(item.requests for item in overhead)
    retries = sum(item.retries for item in overhead)
    cache_hits = sum(item.cache_hits for item in overhead)
    input_tokens = [item.input_tokens for item in overhead if item.input_tokens is not None]
    output_tokens = [item.output_tokens for item in overhead if item.output_tokens is not None]
    latencies = [item.latency_ms for item in overhead if item.latency_ms is not None]
    return {
        "entries": len(overhead),
        "requests": requests,
        "retries": retries,
        "retry_rate": (retries / requests) if requests else None,
        "cache_hits": cache_hits,
        "cache_hit_rate": (cache_hits / requests) if requests else None,
        "input_tokens": sum(input_tokens) if input_tokens else None,
        "output_tokens": sum(output_tokens) if output_tokens else None,
        "latency_ms": _numeric_stats([float(value) for value in latencies]),
        "unattributed_entries": sum(1 for item in overhead if item.work_item_id is None),
    }


# -- top-level report -------------------------------------------------------


def _attribution(
    dataset: ImpactDataset,
    items: Sequence[WorkItem],
    matched: Mapping[str, Any] | None,
    imbalance: Mapping[str, Any],
    config: ImpactConfig,
    primary: Mapping[str, Any],
) -> dict[str, Any]:
    evidence = dataset.evidence
    reasons: list[str] = []
    treatment_n = sum(1 for item in items if item.cohort == "treatment")
    control_n = sum(1 for item in items if item.cohort == "control")
    matched_n = int(matched["matched_items"]) if matched else 0
    eligible = len(items)
    overlap_ratio = (matched_n / eligible) if eligible else 0.0

    if not items:
        grade = "unmeasurable"
        reasons.append("no_work_items")
    elif treatment_n == 0 or control_n == 0:
        grade = "descriptive"
        reasons.append("no_comparison_arm")
    else:
        randomized = bool(evidence.get("randomized") or evidence.get("assignment_logged"))
        pre_trends = bool(evidence.get("parallel_pre_trends"))
        if matched_n == 0:
            grade = "descriptive"
            reasons.append("no_matched_strata")
        elif randomized:
            grade = "controlled"
        elif pre_trends:
            grade = "quasi_experimental"
        elif overlap_ratio >= config.min_overlap:
            grade = "matched_observational"
        else:
            grade = "descriptive"
            reasons.append("limited_overlap")

    imbalanced = list(imbalance.get("imbalanced_covariates") or [])
    confounded = False
    if imbalanced:
        reasons.append("covariate_imbalance")
        # A residual imbalance is a known confound; never let it carry the
        # strongest grade merely because the effect looks large.
        if grade in ("controlled", "quasi_experimental", "matched_observational"):
            grade = "descriptive"
            confounded = True
        else:
            confounded = True

    causal_claim = {
        "controlled": "causal_supported_by_randomized_assignment",
        "quasi_experimental": "limited_causal_with_pretrends",
        "matched_observational": "associational_matched",
        "descriptive": "associational",
        "unmeasurable": "none",
    }[grade]

    effect = primary.get("matched") if matched else None
    ci_low = effect.get("ci_low") if effect else None
    ci_high = effect.get("ci_high") if effect else None
    direction_lower_better = _OUTCOME_DIRECTIONS.get(config.primary_outcome, "lower_is_better") == "lower_is_better"

    if len(items) == 0:
        conclusion = "no_work_items"
    elif sum(1 for item in items if item.accepted) == 0:
        conclusion = "no_accepted_tasks"
    elif ci_low is None or ci_high is None:
        conclusion = "insufficient_matched_evidence"
    elif direction_lower_better and ci_high < 0:
        conclusion = "observed_improvement"
    elif direction_lower_better and ci_low > 0:
        conclusion = "observed_regression"
    elif not direction_lower_better and ci_low > 0:
        conclusion = "observed_improvement"
    elif not direction_lower_better and ci_high < 0:
        conclusion = "observed_regression"
    else:
        conclusion = "no_observed_effect"

    return {
        "grade": grade,
        "causal_claim": causal_claim,
        "confounded": confounded,
        "conclusion": conclusion,
        "reasons": sorted(set(reasons)),
        "overlap_ratio": overlap_ratio,
        "matched_items": matched_n,
        "imbalanced_covariates": imbalanced,
        "note": (
            "Semantic labels and chronology alone are not causal evidence. The grade reflects "
            "the strength of the assignment, never the size of the effect."
        ),
    }


_OUTCOME_DIRECTIONS = {
    "time_to_accepted_seconds": "lower_is_better",
    "cost_usd": "lower_is_better",
    "attempt_count": "lower_is_better",
    "review_rounds": "lower_is_better",
    "first_pass_verified": "higher_is_better",
    "rework": "lower_is_better",
}


def build_impact_report(
    dataset: ImpactDataset,
    config: ImpactConfig | None = None,
    *,
    observed_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the deterministic accepted-task impact report."""
    rules = (config or ImpactConfig()).validated()
    items = list(dataset.work_items)

    match_on = dataset.match_on
    # Match on the first numeric primary outcome to anchor the matched set, then
    # use the same matched items for every outcome so a lucky outcome cannot pick
    # a different cohort.
    anchor = _match_outcome(items, rules.primary_outcome, match_on, rules)
    matched_keys = {
        tuple(stratum["key"])
        for stratum in anchor["strata"]
        if stratum["n_treatment"] and stratum["n_control"]
    }
    matched_items = [
        item
        for item in items
        if item.cohort in ("treatment", "control") and _match_key(item, match_on) in matched_keys
    ]

    outcomes: dict[str, Any] = {}
    for outcome in _OUTCOME_EXTRACTORS:
        outcomes[outcome] = _match_outcome(items, outcome, match_on, rules)
    for outcome in ("first_pass_verified", "rework"):
        outcomes[outcome] = _rate_outcome(items, outcome, match_on, rules)

    imbalance = _imbalance_report(
        items,
        match_on,
        extra_covariates=("task_class", "scope", "baseline_complexity", "cache_state"),
        matched_items=matched_items,
        config=rules,
    )

    accepted_tasks = _accepted_task_rollup(items)
    overhead = _classifier_overhead_rollup(dataset.classifier_overhead)
    attribution = _attribution(dataset, items, anchor, imbalance, rules, anchor)

    treatment_items = [item for item in items if item.cohort == "treatment"]
    control_items = [item for item in items if item.cohort == "control"]
    missingness = {
        "cohort_unknown_work_items": sum(1 for item in items if item.cohort == "unknown"),
        "cohort_treatment": len(treatment_items),
        "cohort_control": len(control_items),
        "accepted_missing_acceptance_time": sum(
            1 for item in items if item.accepted and item.accepted_at is None
        ),
        "accepted_missing_ready_time": sum(1 for item in items if item.accepted and item.ready_at is None),
        "cost_unmeasured_items": accepted_tasks["cost"]["unmeasured_cost_items"],
        "time_unmeasured_items": sum(
            1 for item in items if _time_to_accept_seconds(item) is None and item.accepted
        ),
        "quality_unmeasured_accepted": sum(
            1
            for item in items
            if item.accepted and _first_pass_verified(item) is None and _review_rounds(item) is None
        ),
        "open_attempts_without_end": sum(
            1 for item in items for attempt in item.attempts if attempt.started_at and attempt.ended_at is None
        ),
        "classifier_overhead_unattributed": overhead["unattributed_entries"],
        "note": "Missing usage/price/acceptance/cohort evidence is unknown, never zero.",
    }

    content: dict[str, Any] = {
        "report_version": IMPACT_REPORT_VERSION,
        "kind": IMPACT_REPORT_KIND,
        "generated_by": dataset.generated_by,
        "provenance": {
            "bundle_schema_version": dataset.bundle_schema_version,
            "evidence": dict(dataset.evidence),
            "work_items": len(items),
            "match_on": list(match_on),
            "dataset_hash": dataset.dataset_hash(),
        },
        "config": {
            "primary_outcome": rules.primary_outcome,
            "min_stratum_size": rules.min_stratum_size,
            "imbalance_threshold": rules.imbalance_threshold,
            "min_overlap": rules.min_overlap,
            "bootstrap_resamples": rules.bootstrap_resamples,
            "confidence": rules.confidence,
            "followup_window_days": rules.followup_window_days,
        },
        "accepted_tasks": accepted_tasks,
        "outcomes": outcomes,
        "imbalance": imbalance,
        "attribution": attribution,
        "classifier_overhead": overhead,
        "missingness": missingness,
        "notes": [
            "Work item / accepted task is the primary unit; sessions and episodes are nested observations.",
            "Failed and abandoned runs stay in the success-rate denominator and their attempts stay in cost.",
            "Incomplete tasks are right-censored, never dropped and never recorded as zero.",
            "Missing price or usage is unknown, never zero; a zero-token event with a known price is a measured zero.",
            "No accepted tasks makes cost per accepted task undefined, not zero.",
            "A zero baseline makes a relative change undefined.",
            "Semantic labels and chronology alone are not causal evidence.",
        ],
    }
    if observed_evidence is not None:
        content["observed_evidence"] = dict(observed_evidence)
    content["report_hash"] = canonical_hash(content)
    return content


# -- real projection evidence ----------------------------------------------


def _table_exists(conn: Any, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def file_sha256(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Stream a file's sha256 so a large projection copy can be pinned cheaply."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def observed_evidence_from_store(
    store: Any,
    *,
    source_label: str = "projection",
    source_hash: str | None = None,
    max_duration_samples: int = 200_000,
) -> dict[str, Any]:
    """Derive descriptive real-data evidence from a read-only projection.

    The projection currently carries no acceptance, work-link or cohort evidence,
    so this half cannot compute an accepted-task effect. It reports exactly what
    is measurable -- cost/usage tokens, observed duration and classifier overhead
    -- with explicit missingness, so the gap is visible rather than filled in.
    """
    conn = store.conn
    totals = {"sessions": int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])}
    totals["events"] = int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
    time_range = conn.execute(
        "SELECT MIN(timestamp) AS first_timestamp, MAX(timestamp) AS last_timestamp FROM events"
    ).fetchone()

    by_provider: list[dict[str, Any]] = []
    for row in conn.execute(
        """
        SELECT e.provider AS provider,
               COUNT(DISTINCT e.session_id) AS sessions,
               COUNT(*) AS events,
               SUM(CASE WHEN u.input_tokens IS NOT NULL THEN u.input_tokens ELSE 0 END) AS input_tokens,
               SUM(CASE WHEN u.output_tokens IS NOT NULL THEN u.output_tokens ELSE 0 END) AS output_tokens,
               SUM(CASE WHEN u.total_tokens IS NOT NULL THEN u.total_tokens ELSE 0 END) AS total_tokens,
               SUM(CASE WHEN u.input_tokens IS NULL THEN 1 ELSE 0 END) AS usage_rows_missing,
               SUM(CASE WHEN u.input_tokens = 0 THEN 1 ELSE 0 END) AS usage_rows_zero
        FROM events e LEFT JOIN event_usage u
          ON e.city_id = u.city_id AND e.host_id = u.host_id AND e.provider = u.provider
         AND e.session_id = u.session_id AND e.event_id = u.event_id
        GROUP BY e.provider ORDER BY e.provider
        """
    ):
        by_provider.append(dict(zip(row.keys(), tuple(row))))

    by_model: list[dict[str, Any]] = []
    for row in conn.execute(
        """
        SELECT e.model AS model,
               COUNT(*) AS events,
               SUM(CASE WHEN u.input_tokens IS NOT NULL THEN u.input_tokens ELSE 0 END) AS input_tokens,
               SUM(CASE WHEN u.output_tokens IS NOT NULL THEN u.output_tokens ELSE 0 END) AS output_tokens,
               SUM(CASE WHEN u.total_tokens IS NOT NULL THEN u.total_tokens ELSE 0 END) AS total_tokens
        FROM events e LEFT JOIN event_usage u
          ON e.city_id = u.city_id AND e.host_id = u.host_id AND e.provider = u.provider
         AND e.session_id = u.session_id AND e.event_id = u.event_id
        WHERE e.model IS NOT NULL
        GROUP BY e.model ORDER BY e.model
        """
    ):
        by_model.append(dict(zip(row.keys(), tuple(row))))

    duration_rows = conn.execute(
        "SELECT duration_ms FROM events WHERE duration_ms IS NOT NULL LIMIT ?",
        (max_duration_samples,),
    ).fetchall()
    durations = _numeric_stats([float(row[0]) for row in duration_rows])

    usage_coverage = conn.execute(
        """
        SELECT COUNT(*) AS usage_rows,
               SUM(CASE WHEN input_tokens IS NULL THEN 1 ELSE 0 END) AS input_missing,
               SUM(CASE WHEN input_tokens = 0 THEN 1 ELSE 0 END) AS input_zero
        FROM event_usage
        """
    ).fetchone()

    overhead_rows = (
        conn.execute(
            """
            SELECT outcome, COUNT(*) AS requests, SUM(attempts) AS attempts,
                   SUM(CASE WHEN input_tokens IS NOT NULL THEN input_tokens ELSE 0 END) AS input_tokens,
                   SUM(CASE WHEN output_tokens IS NOT NULL THEN output_tokens ELSE 0 END) AS output_tokens,
                   SUM(CASE WHEN cost_known = 1 AND cost_usd IS NOT NULL THEN cost_usd ELSE 0 END) AS known_cost_usd,
                   SUM(CASE WHEN cost_known = 1 THEN 1 ELSE 0 END) AS cost_known_rows,
                   SUM(CASE WHEN cost_known = 0 THEN 1 ELSE 0 END) AS cost_unknown_rows
            FROM transport_provenance GROUP BY outcome ORDER BY outcome
            """
        ).fetchall()
        if _table_exists(conn, "transport_provenance")
        else []
    )
    latencies = [
        float(row[0])
        for row in (
            conn.execute(
                "SELECT latency_ms FROM transport_provenance WHERE latency_ms IS NOT NULL"
            ).fetchall()
            if _table_exists(conn, "transport_provenance")
            else []
        )
    ]
    queue_counts = (
        {
            row["status"]: row["n"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM collector_queue GROUP BY status ORDER BY status"
            ).fetchall()
        }
        if _table_exists(conn, "collector_queue")
        else {}
    )

    classifier_overhead = {
        "by_outcome": [dict(zip(row.keys(), tuple(row))) for row in overhead_rows],
        "latency_ms": _numeric_stats(latencies),
        "total_requests": sum(int(row["requests"]) for row in overhead_rows),
        "total_attempts": sum(int(row["attempts"] or 0) for row in overhead_rows),
        "known_cost_usd": sum(float(row["known_cost_usd"] or 0.0) for row in overhead_rows),
        "cost_known_rows": sum(int(row["cost_known_rows"] or 0) for row in overhead_rows),
        "cost_unknown_rows": sum(int(row["cost_unknown_rows"] or 0) for row in overhead_rows),
    }

    return {
        "source": source_label,
        "source_hash": source_hash,
        "totals": {
            "sessions": int(totals["sessions"]),
            "events": int(totals["events"]),
            "first_timestamp": time_range["first_timestamp"],
            "last_timestamp": time_range["last_timestamp"],
        },
        "by_provider": by_provider,
        "by_model": by_model,
        "observed_duration_ms": durations,
        "usage_coverage": {
            "usage_rows": int(usage_coverage["usage_rows"] or 0),
            "input_missing": int(usage_coverage["input_missing"] or 0),
            "input_zero": int(usage_coverage["input_zero"] or 0),
            "note": "Missing is distinct from zero: an absent counter is unknown, an explicit 0 is measured.",
        },
        "classifier_overhead": classifier_overhead,
        "collector_queue": queue_counts,
        "acceptance_evidence": {
            "work_items": 0,
            "accepted": 0,
            "note": (
                "The projection carries no acceptance, work-link or cohort evidence, so no "
                "accepted-task effect can be computed from it. This is missingness, not zero."
            ),
        },
    }


__all__ = [
    "ATTEMPT_KINDS",
    "ATTRIBUTION_GRADES",
    "COHORTS",
    "DEFAULT_MATCH_ON",
    "IMPACT_BUNDLE_VERSION",
    "IMPACT_REPORT_KIND",
    "IMPACT_REPORT_VERSION",
    "OUTCOMES",
    "PHASES",
    "Attempt",
    "ClassifierOverhead",
    "ImpactConfig",
    "ImpactDataset",
    "WorkItem",
    "build_impact_report",
    "file_sha256",
    "load_impact_bundle",
    "normalize_impact_bundle",
    "observed_evidence_from_store",
]
