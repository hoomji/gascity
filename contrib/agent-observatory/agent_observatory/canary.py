"""Controlled, reversible optimization canary (M8).

``requirements.md`` R4 asks that every enabled policy version carry exposure
logs, budget limits, stop conditions and a reversible rollback. M7
(:mod:`agent_observatory.policy`) produces *shadow* recommendations over a
catalog of currently configured candidates; M8 is the bounded canary that
turns one of those disagreements into a **pre-registered**, **opt-in**,
seeded randomized assignment and measures its net effect.

This module is deliberately conservative:

* **Opt-in, prior policy is the default.** A canary only applies a treatment
  when it is explicitly enabled (``enabled=True``) *and* the kill switch is not
  engaged. With either condition the prior policy is returned unchanged for
  every unit, so the default behavior of any caller is the status quo.
* **Pre-registered.** The policy, seed, sample size, guardrails and request cap
  live in a ``CanaryRegistration`` artifact that must be written before a run;
  :func:`build_canary_report` refuses to treat a missing or malformed
  registration as evidence.
* **Seeded and balanced.** Assignment is a deterministic hash of
  ``(seed, stratum, unit_id)``, permuted within each stratum so control and
  treatment are balanced without importing a random-number generator. The seed
  and every per-unit digest are logged.
* **Bounded spend.** Live classification is capped by ``--max-requests`` with an
  owner-approved hard ceiling of :data:`MAX_ALLOWED_REQUESTS` (50). Units beyond
  the cap stay on the prior policy.
* **Reversible.** Engaging the kill switch restores the prior policy for every
  unit on the next run without changing the recorded (planned) assignment.
* **No unsupported claim.** An improvement claim is only emitted when the
  arm sample sizes reach the pre-registered minimums, every guardrail passes,
  and the uncertainty interval excludes zero in the beneficial direction. Any
  missing outcome, shortfall or failed guardrail yields ``improvement_claim:
  none``.

The report is advisory evidence: the module writes no routing, dispatch or
configuration and ``canary.executes_changes`` is always ``false``. A live
routing change is a separate, owner-authorized step.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .canonical import canonical_hash, canonical_json, sha256_text
from .contract import normalize_timestamp
from .errors import CanaryError
from .policy import (
    CANDIDATE_KINDS,
    DEFAULT_CONFIDENCE_THRESHOLD,
    ClassificationRecord,
    PolicyCatalog,
    PolicyConfig,
    normalize_recommendation_bundle,
    recommendations_for_record,
)

CANARY_REGISTRATION_SCHEMA_VERSION = "1.0"
CANARY_BUNDLE_SCHEMA_VERSION = "1.0"
CANARY_REPORT_VERSION = "1"
CANARY_REPORT_KIND = "policy_canary"

#: Owner-approved hard ceiling on live Jev classification requests per run.
MAX_ALLOWED_REQUESTS = 50
DEFAULT_TREATMENT_FRACTION = 0.5
VALID_ARMS = ("control", "treatment")
KNOWN_OUTCOMES = (
    "time_to_accepted_seconds",
    "cost_usd",
    "acceptance_rate",
    "quality_score",
)
_OUTCOME_DIRECTION = {
    "time_to_accepted_seconds": "lower",
    "cost_usd": "lower",
    "acceptance_rate": "higher",
    "quality_score": "higher",
}
_OUTCOME_LABELS = {
    "time_to_accepted_seconds": "time to accepted delivery (seconds)",
    "cost_usd": "cost per unit (USD)",
    "acceptance_rate": "acceptance rate",
    "quality_score": "quality score",
}

_REGISTRATION_KEYS = frozenset(
    {
        "schema_version",
        "registration_id",
        "policy_id",
        "policy_kind",
        "treatment_candidate",
        "prior_candidate",
        "catalog_version",
        "seed",
        "sample_size",
        "treatment_fraction",
        "max_requests",
        "created_at",
        "primary_outcome",
        "confidence_threshold",
        "guardrails",
        "evidence",
        "notes",
    }
)
_GUARDRAIL_KEYS = frozenset(
    {
        "min_control_n",
        "min_treatment_n",
        "max_quality_regression",
        "max_cost_regression_usd",
        "min_acceptance_rate",
        "alpha",
    }
)
_UNIT_KEYS = frozenset(
    {"unit_id", "record", "stratum", "prior_candidate", "requires_classification", "outcome"}
)
_BUNDLE_KEYS = frozenset({"schema_version", "generated_by", "units"})
_OUTCOME_KEYS = frozenset(
    {"accepted", "time_to_accepted_seconds", "cost_usd", "quality_score", "missing"}
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CanaryError(message)


def _require_mapping(raw: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise CanaryError(f"{what} must be a JSON object, got {type(raw).__name__}")
    return raw


def _reject_unknown_keys(raw: Mapping[str, Any], allowed: frozenset[str], what: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise CanaryError(f"{what} has unknown key(s): {', '.join(unknown)}")


def _require_str(raw: Mapping[str, Any], key: str, what: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise CanaryError(f"{what}.{key} must be a non-empty string")
    return value


def _optional_str(raw: Mapping[str, Any], key: str, what: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CanaryError(f"{what}.{key} must be a string or null")
    return value


def _require_int(raw: Mapping[str, Any], key: str, what: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise CanaryError(f"{what}.{key} must be an integer")
    return value


def _finite_number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CanaryError(f"{what} must be a number")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise CanaryError(f"{what} must be finite")
    return number


def _optional_number(raw: Mapping[str, Any], key: str, what: str) -> float | None:
    value = raw.get(key)
    if value is None:
        return None
    return _finite_number(value, f"{what}.{key}")


def _unit_interval(raw: Mapping[str, Any], key: str, what: str) -> float:
    value = _finite_number(raw.get(key), f"{what}.{key}")
    if not (0.0 <= value <= 1.0):
        raise CanaryError(f"{what}.{key} must be in [0,1]")
    return value


def _string_tuple(raw: Mapping[str, Any], key: str, what: str) -> tuple[str, ...]:
    value = raw.get(key, [])
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise CanaryError(f"{what}.{key} must be a list of strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise CanaryError(f"{what}.{key} must contain only strings")
        out.append(item)
    return tuple(out)


# -- artifacts ---------------------------------------------------------------


@dataclass(frozen=True)
class CanaryGuardrails:
    """Pre-registered non-inferiority and minimum-sample guardrails."""

    min_control_n: int = 5
    min_treatment_n: int = 5
    max_quality_regression: float = 0.0
    max_cost_regression_usd: float = 0.0
    min_acceptance_rate: float = 0.0
    alpha: float = 0.05

    def content(self) -> dict[str, Any]:
        return {
            "min_control_n": self.min_control_n,
            "min_treatment_n": self.min_treatment_n,
            "max_quality_regression": self.max_quality_regression,
            "max_cost_regression_usd": self.max_cost_regression_usd,
            "min_acceptance_rate": self.min_acceptance_rate,
            "alpha": self.alpha,
        }


@dataclass(frozen=True)
class CanaryRegistration:
    """The pre-registered canary design written before any run.

    ``evidence`` is free-form but is intended to carry the M7 shadow rows that
    justify the selected policy (the disagreement rows), so the choice remains
    reviewable after the run.
    """

    registration_id: str
    policy_id: str
    policy_kind: str
    treatment_candidate: str
    prior_candidate: str
    catalog_version: str
    seed: str
    sample_size: int
    created_at: str
    treatment_fraction: float = DEFAULT_TREATMENT_FRACTION
    max_requests: int = MAX_ALLOWED_REQUESTS
    primary_outcome: str = "time_to_accepted_seconds"
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    guardrails: CanaryGuardrails = field(default_factory=CanaryGuardrails)
    evidence: Mapping[str, Any] | None = None
    notes: tuple[str, ...] = ()
    schema_version: str = CANARY_REGISTRATION_SCHEMA_VERSION

    def content(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "registration_id": self.registration_id,
            "policy_id": self.policy_id,
            "policy_kind": self.policy_kind,
            "treatment_candidate": self.treatment_candidate,
            "prior_candidate": self.prior_candidate,
            "catalog_version": self.catalog_version,
            "seed": self.seed,
            "sample_size": self.sample_size,
            "treatment_fraction": self.treatment_fraction,
            "max_requests": self.max_requests,
            "created_at": self.created_at,
            "primary_outcome": self.primary_outcome,
            "confidence_threshold": self.confidence_threshold,
            "guardrails": self.guardrails.content(),
            "notes": list(self.notes),
        }
        if self.evidence is not None:
            payload["evidence"] = dict(self.evidence)
        return payload

    def registration_hash(self) -> str:
        return canonical_hash(self.content())


@dataclass(frozen=True)
class ObservedOutcome:
    """One unit's observed result, if any.

    ``missing`` distinguishes "not yet measured / not linked" from a measured
    zero. An absent outcome is unknown, never an implicit failure or success.
    """

    accepted: bool | None = None
    time_to_accepted_seconds: float | None = None
    cost_usd: float | None = None
    quality_score: float | None = None
    missing: bool = False

    def content(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "time_to_accepted_seconds": self.time_to_accepted_seconds,
            "cost_usd": self.cost_usd,
            "quality_score": self.quality_score,
            "missing": self.missing,
        }

    def observed(self) -> bool:
        if self.missing:
            return False
        return any(
            value is not None
            for value in (
                self.accepted,
                self.time_to_accepted_seconds,
                self.cost_usd,
                self.quality_score,
            )
        )


@dataclass(frozen=True)
class CanaryUnit:
    """One eligible-or-ineligible unit considered for canary assignment."""

    unit_id: str
    record: ClassificationRecord
    stratum: str = ""
    prior_candidate: str | None = None
    requires_classification: bool = False
    outcome: ObservedOutcome | None = None

    def content(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "unit_id": self.unit_id,
            "record": self.record.content(),
            "stratum": self.stratum,
            "prior_candidate": self.prior_candidate,
            "requires_classification": self.requires_classification,
        }
        if self.outcome is not None:
            payload["outcome"] = self.outcome.content()
        return payload


@dataclass(frozen=True)
class CanaryBundle:
    """A set of canary units plus their observed outcomes, if any."""

    units: tuple[CanaryUnit, ...]
    schema_version: str = CANARY_BUNDLE_SCHEMA_VERSION
    generated_by: str | None = None

    def dataset_hash(self) -> str:
        return canonical_hash(
            {
                "schema_version": self.schema_version,
                "generated_by": self.generated_by,
                "units": [unit.content() for unit in self.units],
            }
        )


# -- normalization -----------------------------------------------------------


def normalize_registration(raw: Any) -> CanaryRegistration:
    """Validate a registration JSON object and return the frozen artifact."""
    document = _require_mapping(raw, "registration")
    _reject_unknown_keys(document, _REGISTRATION_KEYS, "registration")

    schema_version = document.get("schema_version", CANARY_REGISTRATION_SCHEMA_VERSION)
    if schema_version != CANARY_REGISTRATION_SCHEMA_VERSION:
        raise CanaryError(
            f"registration.schema_version must be {CANARY_REGISTRATION_SCHEMA_VERSION!r}"
        )

    policy_kind = _require_str(document, "policy_kind", "registration")
    if policy_kind not in CANDIDATE_KINDS:
        raise CanaryError(
            f"registration.policy_kind {policy_kind!r} is not one of {', '.join(CANDIDATE_KINDS)}"
        )

    treatment_fraction = _unit_interval(document, "treatment_fraction", "registration")
    _require(0.0 < treatment_fraction < 1.0, "registration.treatment_fraction must be in (0,1)")

    sample_size = _require_int(document, "sample_size", "registration")
    _require(sample_size >= 1, "registration.sample_size must be at least 1")

    max_requests = _require_int(document, "max_requests", "registration")
    _require(max_requests >= 0, "registration.max_requests must be nonnegative")
    _require(
        max_requests <= MAX_ALLOWED_REQUESTS,
        f"registration.max_requests {max_requests} exceeds the owner-approved cap "
        f"{MAX_ALLOWED_REQUESTS}",
    )

    primary_outcome = _require_str(document, "primary_outcome", "registration")
    if primary_outcome not in KNOWN_OUTCOMES:
        raise CanaryError(
            f"registration.primary_outcome {primary_outcome!r} is not one of "
            f"{', '.join(KNOWN_OUTCOMES)}"
        )

    created_at = _require_str(document, "created_at", "registration")
    try:
        created_at = normalize_timestamp(created_at)
    except Exception as exc:  # ContractError carries the location prefix
        raise CanaryError(f"registration.created_at is not ISO-8601: {exc}") from exc

    guardrail_doc = document.get("guardrails", {})
    guardrail_map = _require_mapping(guardrail_doc, "registration.guardrails")
    _reject_unknown_keys(guardrail_map, _GUARDRAIL_KEYS, "registration.guardrails")
    max_quality_regression = (
        _optional_number(guardrail_map, "max_quality_regression", "registration.guardrails")
        if "max_quality_regression" in guardrail_map
        else 0.0
    )
    max_cost_regression_usd = (
        _optional_number(guardrail_map, "max_cost_regression_usd", "registration.guardrails")
        if "max_cost_regression_usd" in guardrail_map
        else 0.0
    )
    if max_quality_regression is None:
        raise CanaryError("registration.guardrails.max_quality_regression must not be null")
    if max_cost_regression_usd is None:
        raise CanaryError("registration.guardrails.max_cost_regression_usd must not be null")
    guardrails = CanaryGuardrails(
        min_control_n=_require_int(guardrail_map, "min_control_n", "registration.guardrails")
        if "min_control_n" in guardrail_map
        else 5,
        min_treatment_n=_require_int(guardrail_map, "min_treatment_n", "registration.guardrails")
        if "min_treatment_n" in guardrail_map
        else 5,
        max_quality_regression=max_quality_regression,
        max_cost_regression_usd=max_cost_regression_usd,
        min_acceptance_rate=_unit_interval(
            guardrail_map, "min_acceptance_rate", "registration.guardrails"
        )
        if "min_acceptance_rate" in guardrail_map
        else 0.0,
        alpha=_unit_interval(guardrail_map, "alpha", "registration.guardrails")
        if "alpha" in guardrail_map
        else 0.05,
    )
    _require(guardrails.min_control_n >= 1, "registration.guardrails.min_control_n must be >= 1")
    _require(
        guardrails.min_treatment_n >= 1, "registration.guardrails.min_treatment_n must be >= 1"
    )
    _require(
        0.0 < guardrails.alpha < 1.0, "registration.guardrails.alpha must be in (0,1)"
    )

    confidence_threshold = _unit_interval(
        document, "confidence_threshold", "registration"
    ) if "confidence_threshold" in document else DEFAULT_CONFIDENCE_THRESHOLD

    evidence = document.get("evidence")
    if evidence is not None:
        evidence = dict(_require_mapping(evidence, "registration.evidence"))

    return CanaryRegistration(
        registration_id=_require_str(document, "registration_id", "registration"),
        policy_id=_require_str(document, "policy_id", "registration"),
        policy_kind=policy_kind,
        treatment_candidate=_require_str(document, "treatment_candidate", "registration"),
        prior_candidate=_require_str(document, "prior_candidate", "registration"),
        catalog_version=_require_str(document, "catalog_version", "registration"),
        seed=_require_str(document, "seed", "registration"),
        sample_size=sample_size,
        created_at=created_at,
        treatment_fraction=treatment_fraction,
        max_requests=max_requests,
        primary_outcome=primary_outcome,
        confidence_threshold=confidence_threshold,
        guardrails=guardrails,
        evidence=evidence,
        notes=_string_tuple(document, "notes", "registration"),
    )


def _read_json_object(path: str | Path, what: str) -> Any:
    raw = Path(path).read_text(encoding="utf-8")
    try:
        value = json.loads(raw, parse_constant=_reject_json_constant)
    except ValueError as exc:
        raise CanaryError(f"{what} {path} is not valid JSON: {exc}") from exc
    return _require_mapping(value, what)


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant {value!r} is not allowed")


def load_registration(path: str | Path) -> CanaryRegistration:
    """Load and validate a pre-registered canary artifact."""
    return normalize_registration(_read_json_object(path, "canary registration"))


def _normalize_record(raw: Any, unit_id: str) -> ClassificationRecord:
    try:
        bundle = normalize_recommendation_bundle({"schema_version": "1.0", "episodes": [raw]})
    except Exception as exc:
        raise CanaryError(f"canary unit {unit_id!r} record is invalid: {exc}") from exc
    return bundle.records[0]


def _normalize_outcome(raw: Any, unit_id: str) -> ObservedOutcome | None:
    if raw is None:
        return None
    document = _require_mapping(raw, f"canary unit {unit_id!r} outcome")
    _reject_unknown_keys(document, _OUTCOME_KEYS, f"canary unit {unit_id!r} outcome")
    accepted = document.get("accepted")
    if accepted is not None and not isinstance(accepted, bool):
        raise CanaryError(f"canary unit {unit_id!r} outcome.accepted must be a boolean or null")
    missing = document.get("missing", False)
    if not isinstance(missing, bool):
        raise CanaryError(f"canary unit {unit_id!r} outcome.missing must be a boolean")
    time = _optional_number(document, "time_to_accepted_seconds", f"canary unit {unit_id!r} outcome")
    cost = _optional_number(document, "cost_usd", f"canary unit {unit_id!r} outcome")
    quality = _optional_number(document, "quality_score", f"canary unit {unit_id!r} outcome")
    for label, value in (("time_to_accepted_seconds", time), ("cost_usd", cost)):
        if value is not None and value < 0:
            raise CanaryError(f"canary unit {unit_id!r} outcome.{label} must be nonnegative")
    return ObservedOutcome(
        accepted=accepted,
        time_to_accepted_seconds=time,
        cost_usd=cost,
        quality_score=quality,
        missing=missing,
    )


def normalize_canary_bundle(raw: Any) -> CanaryBundle:
    """Validate a canary bundle and return the frozen artifact."""
    document = _require_mapping(raw, "canary bundle")
    _reject_unknown_keys(document, _BUNDLE_KEYS, "canary bundle")
    schema_version = document.get("schema_version", CANARY_BUNDLE_SCHEMA_VERSION)
    if schema_version != CANARY_BUNDLE_SCHEMA_VERSION:
        raise CanaryError(f"canary bundle.schema_version must be {CANARY_BUNDLE_SCHEMA_VERSION!r}")
    units_raw = document.get("units")
    if not isinstance(units_raw, Sequence) or isinstance(units_raw, (str, bytes)):
        raise CanaryError("canary bundle.units must be a list")

    units: list[CanaryUnit] = []
    seen: set[str] = set()
    for index, raw_unit in enumerate(units_raw):
        where = f"canary bundle.units[{index}]"
        unit = _require_mapping(raw_unit, where)
        _reject_unknown_keys(unit, _UNIT_KEYS, where)
        unit_id = _require_str(unit, "unit_id", where)
        if unit_id in seen:
            raise CanaryError(f"canary bundle has duplicate unit_id {unit_id!r}")
        seen.add(unit_id)
        requires_classification = unit.get("requires_classification", False)
        if not isinstance(requires_classification, bool):
            raise CanaryError(f"{where}.requires_classification must be a boolean")
        units.append(
            CanaryUnit(
                unit_id=unit_id,
                record=_normalize_record(unit.get("record"), unit_id),
                stratum=_optional_str(unit, "stratum", where) or "",
                prior_candidate=_optional_str(unit, "prior_candidate", where),
                requires_classification=requires_classification,
                outcome=_normalize_outcome(unit.get("outcome"), unit_id),
            )
        )

    generated_by = document.get("generated_by")
    if generated_by is not None and not isinstance(generated_by, str):
        raise CanaryError("canary bundle.generated_by must be a string or null")
    return CanaryBundle(units=tuple(units), generated_by=generated_by)


def load_canary_bundle(path: str | Path) -> CanaryBundle:
    """Load and validate a canary assignment bundle."""
    return normalize_canary_bundle(_read_json_object(path, "canary bundle"))


# -- assignment --------------------------------------------------------------


def assignment_digest(seed: str, stratum: str, unit_id: str) -> str:
    """Return the deterministic per-unit assignment digest.

    The digest is a sha256 over the seed, stratum and unit id. It is logged for
    every unit so an assignment can be re-derived and audited without re-running
    the experiment, and it never depends on dict or file iteration order.
    """
    return sha256_text(canonical_json(["canary-assignment", seed, stratum, unit_id]))


def assign_arms(
    units: Sequence[CanaryUnit],
    registration: CanaryRegistration,
) -> dict[str, dict[str, Any]]:
    """Assign each unit to control/treatment, balanced within each stratum.

    Units are permuted by :func:`assignment_digest` inside each stratum and the
    first ``round(n * treatment_fraction)`` become treatment. Every stratum with
    at least two units keeps at least one unit per arm, so a run cannot spend
    its whole sample on one arm. One-unit strata fall to control.
    """
    groups: dict[str, list[CanaryUnit]] = {}
    for unit in units:
        groups.setdefault(unit.stratum, []).append(unit)

    assignments: dict[str, dict[str, Any]] = {}
    for stratum, group in sorted(groups.items()):
        ordered = sorted(
            group,
            key=lambda unit: (assignment_digest(registration.seed, stratum, unit.unit_id), unit.unit_id),
        )
        size = len(ordered)
        treatment_n = int(round(size * registration.treatment_fraction))
        if size >= 2:
            treatment_n = max(1, min(size - 1, treatment_n))
        else:
            treatment_n = 0
        for index, unit in enumerate(ordered):
            arm = "treatment" if index < treatment_n else "control"
            assignments[unit.unit_id] = {
                "arm": arm,
                "stratum": stratum,
                "digest": assignment_digest(registration.seed, stratum, unit.unit_id),
                "position": index,
            }
    return assignments


# -- report ------------------------------------------------------------------


def _recommendation_for_kind(
    record: ClassificationRecord,
    catalog: PolicyCatalog,
    kind: str,
    config: PolicyConfig,
) -> Mapping[str, Any]:
    for item in recommendations_for_record(record, catalog, config):
        if item["kind"] == kind:
            return item
    raise CanaryError(f"policy recommender returned no {kind!r} recommendation")  # pragma: no cover


def _resolve_request_cap(
    registration: CanaryRegistration, max_requests: int | None
) -> tuple[int, bool]:
    if max_requests is None:
        return registration.max_requests, False
    if not isinstance(max_requests, int) or isinstance(max_requests, bool):
        raise CanaryError("max_requests must be an integer")
    if max_requests < 0:
        raise CanaryError("max_requests must be nonnegative")
    if max_requests > MAX_ALLOWED_REQUESTS:
        raise CanaryError(
            f"max_requests {max_requests} exceeds the owner-approved hard cap "
            f"{MAX_ALLOWED_REQUESTS}"
        )
    effective = min(max_requests, registration.max_requests)
    return effective, effective != max_requests


def _prior_candidate(unit: CanaryUnit, recommendation: Mapping[str, Any], registration: CanaryRegistration) -> str | None:
    return (
        unit.prior_candidate
        or recommendation.get("fallback_candidate")
        or recommendation.get("current_candidate")
        or registration.prior_candidate
    )


def _mean_stats(values: Sequence[float | None]) -> dict[str, Any]:
    present = [float(value) for value in values if value is not None]
    if not present:
        return {"n": 0, "mean": None, "median": None, "stdev": None}
    stdev = statistics.stdev(present) if len(present) > 1 else None
    return {
        "n": len(present),
        "mean": statistics.fmean(present),
        "median": statistics.median(present),
        "stdev": stdev,
    }


def _arm_metrics(units: Sequence[CanaryUnit]) -> dict[str, Any]:
    def value(unit: CanaryUnit, attribute: str) -> float | None:
        outcome = unit.outcome
        if outcome is None or not outcome.observed():
            return None
        return getattr(outcome, attribute)

    accepted_flags = [
        bool(unit.outcome.accepted)
        for unit in units
        if unit.outcome is not None and unit.outcome.observed() and unit.outcome.accepted is not None
    ]
    observed_n = sum(1 for unit in units if unit.outcome is not None and unit.outcome.observed())
    metrics = {
        "n_assigned": len(units),
        "n_observed": observed_n,
        "n_missing_outcome": sum(
            1 for unit in units if unit.outcome is None or not unit.outcome.observed()
        ),
        "accepted_n": sum(1 for flag in accepted_flags if flag),
        "acceptance_rate": (
            sum(1 for flag in accepted_flags if flag) / len(accepted_flags)
            if accepted_flags
            else None
        ),
        "acceptance_n": len(accepted_flags),
        "time_to_accepted_seconds": _mean_stats(
            [value(unit, "time_to_accepted_seconds") for unit in units]
        ),
        "cost_usd": _mean_stats([value(unit, "cost_usd") for unit in units]),
        "quality_score": _mean_stats([value(unit, "quality_score") for unit in units]),
    }
    return metrics


def _point_estimate(metrics: Mapping[str, Any], outcome: str) -> float | None:
    if outcome == "acceptance_rate":
        return metrics["acceptance_rate"]
    return metrics[outcome]["mean"]


def _standard_error(metrics: Mapping[str, Any], outcome: str) -> float | None:
    if outcome == "acceptance_rate":
        rate = metrics["acceptance_rate"]
        n = metrics["acceptance_n"]
        if rate is None or n <= 0:
            return None
        return (rate * (1.0 - rate) / n) ** 0.5
    block = metrics[outcome]
    if block["mean"] is None or block["stdev"] is None or block["n"] < 2:
        return None
    return block["stdev"] / (block["n"] ** 0.5)


def _net_effect(
    control: Mapping[str, Any],
    treatment: Mapping[str, Any],
    registration: CanaryRegistration,
    *,
    active: bool,
) -> dict[str, Any]:
    outcome = registration.primary_outcome
    z = statistics.NormalDist().inv_cdf(1.0 - registration.guardrails.alpha / 2.0)
    control_value = _point_estimate(control, outcome)
    treatment_value = _point_estimate(treatment, outcome)
    effect: dict[str, Any] = {
        "primary_outcome": outcome,
        "label": _OUTCOME_LABELS[outcome],
        "direction": _OUTCOME_DIRECTION[outcome],
        "control_value": control_value,
        "treatment_value": treatment_value,
        "absolute_difference": None,
        "relative_difference": None,
        "ci_low": None,
        "ci_high": None,
        "confidence_level": 1.0 - registration.guardrails.alpha,
        "conclusion": "not_run" if not active else "unmeasured",
        "improvement_claim": "none",
    }
    if control_value is None or treatment_value is None:
        return effect

    difference = treatment_value - control_value
    effect["absolute_difference"] = difference
    if control_value != 0.0:
        effect["relative_difference"] = difference / control_value

    control_se = _standard_error(control, outcome)
    treatment_se = _standard_error(treatment, outcome)
    if control_se is not None and treatment_se is not None:
        combined = (control_se**2 + treatment_se**2) ** 0.5
        effect["ci_low"] = difference - z * combined
        effect["ci_high"] = difference + z * combined

    if not active:
        effect["conclusion"] = "not_run"
        return effect
    if effect["ci_low"] is None or effect["ci_high"] is None:
        effect["conclusion"] = "unmeasured"
        return effect

    nontrivial = not (effect["ci_low"] <= 0.0 <= effect["ci_high"])
    if not nontrivial:
        effect["conclusion"] = "no_observed_effect"
        return effect
    beneficial = difference < 0.0 if _OUTCOME_DIRECTION[outcome] == "lower" else difference > 0.0
    effect["conclusion"] = "observed_improvement" if beneficial else "observed_regression"
    return effect


def _guardrail_results(
    registration: CanaryRegistration,
    control: Mapping[str, Any],
    treatment: Mapping[str, Any],
) -> dict[str, Any]:
    guardrails = registration.guardrails
    control_quality = control["quality_score"]["mean"]
    treatment_quality = treatment["quality_score"]["mean"]
    control_cost = control["cost_usd"]["mean"]
    treatment_cost = treatment["cost_usd"]["mean"]

    detail: dict[str, Any] = {}

    def check(name: str, passed: bool | None, **values: Any) -> None:
        entry = {"pass": passed}
        entry.update(values)
        detail[name] = entry

    check(
        "min_control_n",
        control["n_assigned"] >= guardrails.min_control_n,
        value=control["n_assigned"],
        required=guardrails.min_control_n,
    )
    check(
        "min_treatment_n",
        treatment["n_assigned"] >= guardrails.min_treatment_n,
        value=treatment["n_assigned"],
        required=guardrails.min_treatment_n,
    )
    check(
        "quality_non_inferior",
        None
        if control_quality is None or treatment_quality is None
        else treatment_quality >= control_quality - guardrails.max_quality_regression,
        control=control_quality,
        treatment=treatment_quality,
        max_regression=guardrails.max_quality_regression,
    )
    check(
        "cost_non_inferior",
        None
        if control_cost is None or treatment_cost is None
        else treatment_cost <= control_cost + guardrails.max_cost_regression_usd,
        control=control_cost,
        treatment=treatment_cost,
        max_increase=guardrails.max_cost_regression_usd,
    )
    check(
        "acceptance_floor",
        None
        if treatment["acceptance_rate"] is None
        else treatment["acceptance_rate"] >= guardrails.min_acceptance_rate,
        value=treatment["acceptance_rate"],
        required=guardrails.min_acceptance_rate,
    )

    evaluated = all(entry["pass"] is not None for entry in detail.values())
    results = dict(detail)
    results["stop_recommended"] = {
        "value": any(entry["pass"] is False for entry in detail.values()),
    }
    results["evaluated"] = {"value": evaluated}
    return results


def build_canary_report(
    bundle: CanaryBundle,
    catalog: PolicyCatalog,
    registration: CanaryRegistration,
    *,
    enabled: bool = False,
    kill_switch: bool = False,
    max_requests: int | None = None,
    generated_by: str | None = None,
) -> dict[str, Any]:
    """Build the deterministic canary assignment and outcome report.

    ``enabled`` is the opt-in switch (default ``False``) and ``kill_switch``
    rolls a previously enabled canary back. Either way the prior policy is
    returned for every unit; the planned assignment stays visible for audit.
    """
    request_cap, cap_clamped = _resolve_request_cap(registration, max_requests)
    config = PolicyConfig(confidence_threshold=registration.confidence_threshold)

    needs_classification = sorted(
        (unit for unit in bundle.units if unit.requires_classification),
        key=lambda unit: unit.unit_id,
    )
    funded = {unit.unit_id for unit in needs_classification[:request_cap]}
    deferred = sorted(unit.unit_id for unit in needs_classification[request_cap:])
    requests_used = min(len(needs_classification), request_cap)
    requests_capped = len(needs_classification) > request_cap

    details: dict[str, dict[str, Any]] = {}
    eligible_units: list[CanaryUnit] = []
    for unit in bundle.units:
        recommendation = _recommendation_for_kind(unit.record, catalog, registration.policy_kind, config)
        prior = _prior_candidate(unit, recommendation, registration)
        eligible = (
            recommendation.get("decision") == "recommended"
            and recommendation.get("recommended_candidate") == registration.treatment_candidate
            and (not unit.requires_classification or unit.unit_id in funded)
        )
        details[unit.unit_id] = {
            "recommendation": recommendation,
            "prior_candidate": prior,
            "eligible": eligible,
        }
        if eligible:
            eligible_units.append(unit)

    planned = assign_arms(eligible_units, registration)
    active = bool(enabled) and not bool(kill_switch)
    mode = "disabled" if not enabled else ("rolled_back" if kill_switch else "randomized")

    ledger: list[dict[str, Any]] = []
    for unit in sorted(bundle.units, key=lambda item: item.unit_id):
        detail = details[unit.unit_id]
        assignment = planned.get(unit.unit_id)
        if detail["eligible"] and assignment is not None:
            planned_arm = assignment["arm"]
            digest = assignment["digest"]
        else:
            planned_arm = "none"
            digest = assignment_digest(registration.seed, unit.stratum, unit.unit_id)
        if not detail["eligible"]:
            if unit.unit_id in deferred:
                reason = "classification_budget_deferred"
            else:
                reason = "ineligible:" + str(
                    detail["recommendation"].get("eligibility_reason") or "recommendation_mismatch"
                )
        elif mode == "disabled":
            reason = "canary_disabled"
        elif mode == "rolled_back":
            reason = "kill_switch"
        elif planned_arm == "treatment":
            reason = "treatment"
        else:
            reason = "control"

        applied_treatment = active and detail["eligible"] and planned_arm == "treatment"
        applied_candidate = (
            registration.treatment_candidate if applied_treatment else detail["prior_candidate"]
        )
        ledger.append(
            {
                "unit_id": unit.unit_id,
                "stratum": unit.stratum,
                "eligible": detail["eligible"],
                "planned_arm": planned_arm,
                "reason": reason,
                "prior_candidate": detail["prior_candidate"],
                "treatment_candidate": registration.treatment_candidate,
                "applied_candidate": applied_candidate,
                "applied_treatment": applied_treatment,
                "assignment_hash": digest,
            }
        )

    control_units = [
        unit
        for unit in eligible_units
        if planned.get(unit.unit_id, {}).get("arm") == "control"
    ]
    treatment_units = [
        unit
        for unit in eligible_units
        if planned.get(unit.unit_id, {}).get("arm") == "treatment"
    ]
    control_metrics = _arm_metrics(control_units if active else [])
    treatment_metrics = _arm_metrics(treatment_units if active else [])
    net_effect = _net_effect(control_metrics, treatment_metrics, registration, active=active)
    guardrails = _guardrail_results(registration, control_metrics, treatment_metrics)

    sample_size_met = len(eligible_units) >= registration.sample_size
    guardrails_pass = all(
        entry["pass"] is not False
        for name, entry in guardrails.items()
        if isinstance(entry, Mapping) and name not in ("stop_recommended", "evaluated")
    )
    if active and not sample_size_met:
        net_effect["conclusion"] = "insufficient_sample"
    elif active and not guardrails_pass:
        net_effect["conclusion"] = "guardrail_failed"
    if (
        active
        and net_effect["conclusion"] == "observed_improvement"
        and sample_size_met
        and guardrails_pass
    ):
        net_effect["improvement_claim"] = "supported_by_randomized_assignment"

    exposure = {
        "units_total": len(bundle.units),
        "eligible_units": len(eligible_units),
        "planned_control": len(control_units),
        "planned_treatment": len(treatment_units),
        "assigned_control": len(control_units) if active else 0,
        "assigned_treatment": len(treatment_units) if active else 0,
        "ineligible_units": len(bundle.units) - len(eligible_units),
        "deferred_unclassified": len(deferred),
        "analyzed_control": control_metrics["n_observed"] if active else 0,
        "analyzed_treatment": treatment_metrics["n_observed"] if active else 0,
        "missing_outcome_control": control_metrics["n_missing_outcome"] if active else 0,
        "missing_outcome_treatment": treatment_metrics["n_missing_outcome"] if active else 0,
        "sample_size_target": registration.sample_size,
        "sample_size_met": sample_size_met,
    }

    assignment_balance = {
        "treatment_fraction_requested": registration.treatment_fraction,
        "treatment_fraction_observed": (
            len(treatment_units) / len(eligible_units) if eligible_units else None
        ),
        "strata": {},
    }
    strata: dict[str, dict[str, int]] = {}
    for unit in eligible_units:
        entry = strata.setdefault(unit.stratum, {"control": 0, "treatment": 0})
        entry[planned[unit.unit_id]["arm"]] += 1
    assignment_balance["strata"] = {key: dict(sorted(value.items())) for key, value in sorted(strata.items())}

    classification_budget = {
        "max_requests_requested": registration.max_requests if max_requests is None else max_requests,
        "max_requests_effective": request_cap,
        "max_requests_hard_ceiling": MAX_ALLOWED_REQUESTS,
        "clamped_to_registration": cap_clamped,
        "requests_required": len(needs_classification),
        "requests_used": requests_used,
        "requests_capped": requests_capped,
        "deferred_units": deferred,
    }

    analyzed_units = exposure["analyzed_control"] + exposure["analyzed_treatment"]
    if not active or analyzed_units == 0:
        attribution = "unmeasurable"
    elif sample_size_met and guardrails_pass:
        attribution = "randomized"
    else:
        attribution = "insufficient"

    content: dict[str, Any] = {
        "report_version": CANARY_REPORT_VERSION,
        "kind": CANARY_REPORT_KIND,
        "generated_by": generated_by or bundle.generated_by,
        "registration": {
            **registration.content(),
            "registration_hash": registration.registration_hash(),
        },
        "provenance": {
            "registration_schema_version": registration.schema_version,
            "bundle_schema_version": bundle.schema_version,
            "catalog_version": registration.catalog_version,
            "dataset_hash": bundle.dataset_hash(),
        },
        "canary": {
            "mode": mode,
            "enabled": bool(enabled),
            "kill_switch": bool(kill_switch),
            "executes_changes": False,
            "advisory_only": True,
            "policy_id": registration.policy_id,
            "policy_kind": registration.policy_kind,
            "treatment_candidate": registration.treatment_candidate,
            "prior_candidate": registration.prior_candidate,
            "seed": registration.seed,
            "primary_outcome": registration.primary_outcome,
            "classification_budget": classification_budget,
            "exposure": exposure,
            "assignment_balance": assignment_balance,
            "assignment_ledger": ledger,
            "control": control_metrics,
            "treatment": treatment_metrics,
            "net_effect": net_effect,
            "guardrails": guardrails,
            "stop_recommended": bool(guardrails["stop_recommended"]["value"]),
            "attribution": attribution,
        },
        "missingness": {
            "units_without_outcome": sum(
                1 for unit in bundle.units if unit.outcome is None or not unit.outcome.observed()
            ),
            "units_requiring_classification": len(needs_classification),
            "note": "A missing outcome is unknown, never zero and never an implicit failure.",
        },
        "notes": [
            "The canary is opt-in and defaults to the prior policy; disabled or "
            "rolled-back runs apply no treatment.",
            "Assignments are randomized from a logged seed and balanced within strata; "
            "the seed and every assignment digest are recorded.",
            "Live classification requests are bounded by --max-requests up to the "
            f"owner-approved cap of {MAX_ALLOWED_REQUESTS}.",
            "The canary is advisory: it writes no routing, dispatch or configuration.",
            "No improvement is claimed unless the pre-registered sample size and "
            "guardrails pass and the uncertainty interval excludes zero.",
        ],
    }
    content["report_hash"] = canonical_hash(content)
    return content
