"""Advisory live-routing recorder (M8b).

M7 (:mod:`agent_observatory.policy`) shadow-recommends a route for a validated
classification and M8 (:mod:`agent_observatory.canary`) runs a bounded,
pre-registered experiment over recorded units. M8b is the **live** layer: as a
dispatch is routed, record next to the route that was actually taken

* Jev's ``primary_intent`` for the dispatch, and
* the route the M7 shadow policy **would** suggest next.

It is deliberately powerless:

* **Never changes the route.** Every record carries ``applied_route`` equal to
  the caller's ``actual_route`` and ``route_changed=false``. The module writes
  no routing, dispatch or configuration; the live Go hook that calls it is
  documented as advisory-only.
* **Unknown is abstain.** A dispatch without a usable classification gets
  ``primary_intent=unknown`` and ``suggested_route=null``. It is never guessed,
  and an abstaining M7 recommendation never yields a suggestion.
* **Bound to canary-register output.** The registration is loaded through
  :func:`agent_observatory.canary.verify_registration_output`, so an artifact
  that was edited after registration (or supplied without its register-time
  hash) is refused before a single record is written (requirement (c)).
* **Budget re-derived at the call site.** The per-run request cap is not copied
  from the M8 report's estimate. Every Jev attempt is appended to a durable
  ledger and the remaining budget is re-read from that ledger immediately
  before each attempt, so retries and batch items both consume it (requirement
  (a)). A process-per-dispatch caller (the Go hook) therefore cannot overspend
  across invocations.
* **Kill switch re-polled.** The switch file is read inside every retry and
  batch iteration rather than once per run (requirement (b)).

A *classifier* is injected: any callable ``(dispatch) -> ClassificationOutcome``
performs exactly one Jev request. :class:`SingleAttemptTransportClassifier`
adapts the bounded transport for callers that have a built request, so the live
accounting loop owns the retries and charges each one.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from .canary import MAX_ALLOWED_REQUESTS, CanaryRegistration, verify_registration_output
from .canonical import canonical_hash
from .contract import normalize_timestamp
from .errors import LiveRoutingError
from .policy import (
    ClassificationRecord,
    PolicyCatalog,
    PolicyConfig,
    load_catalog,
    recommendations_for_record,
)

LIVE_ROUTING_RECORD_VERSION = "1"
LIVE_ROUTING_RECORD_KIND = "live_routing_advisory"
LIVE_ROUTING_BATCH_KIND = "live_routing_batch"
LEDGER_REQUEST_KIND = "live_routing_request"
LEDGER_RECORD_KIND = "live_routing_advisory"
LIVE_ROUTING_SCHEMA_VERSION = "1.0"

DEFAULT_MAX_ATTEMPTS = 3

try:  # POSIX advisory locking; degrade to no lock elsewhere.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_str(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LiveRoutingError(f"{what} must be a non-empty string")
    return value.strip()


def _optional_str(value: Any, what: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LiveRoutingError(f"{what} must be a string or null")
    value = value.strip()
    return value or None


# -- typed model -------------------------------------------------------------


@dataclass(frozen=True)
class ClassificationOutcome:
    """One Jev classification result for a dispatched unit.

    ``attempts`` is the number of transport attempts the classifier made for
    this result. The live loop charges that many requests, so an adapter that
    retries internally is still accounted for. A classifier supplied to
    :class:`LiveRoutingAdvisor` normally makes exactly one attempt and raises on
    failure; the loop then retries and charges each attempt itself.
    """

    intent: str | None
    confidence: float | None = None
    flags: tuple[str, ...] = ()
    attempts: int = 1
    observed: bool = True


@dataclass(frozen=True)
class LiveDispatch:
    """One routed dispatch the advisory layer considers."""

    dispatch_id: str
    bead_id: str
    actual_route: str
    recorded_at: str
    target: str = ""
    stratum: str = ""
    repo: str | None = None
    host: str | None = None
    provider: str | None = None
    classification: ClassificationRecord | None = None
    batch_id: str | None = None
    batch_index: int | None = None

    @classmethod
    def from_mapping(cls, raw: Any) -> "LiveDispatch":
        if not isinstance(raw, Mapping):
            raise LiveRoutingError("dispatch must be a JSON object")
        recorded_at = raw.get("recorded_at")
        if recorded_at is None:
            recorded_at = _utc_now()
        try:
            recorded_at = normalize_timestamp(str(recorded_at))
        except Exception as exc:
            raise LiveRoutingError(f"dispatch.recorded_at is not ISO-8601: {exc}") from exc
        classification = raw.get("classification")
        record: ClassificationRecord | None = None
        if classification is not None:
            record = _classification_record(classification)
        batch_index = raw.get("batch_index")
        if batch_index is not None and (
            isinstance(batch_index, bool) or not isinstance(batch_index, int) or batch_index < 0
        ):
            raise LiveRoutingError("dispatch.batch_index must be a non-negative integer")
        return cls(
            dispatch_id=_require_str(raw.get("dispatch_id"), "dispatch.dispatch_id"),
            bead_id=_require_str(raw.get("bead_id"), "dispatch.bead_id"),
            actual_route=_require_str(raw.get("actual_route"), "dispatch.actual_route"),
            recorded_at=recorded_at,
            target=_optional_str(raw.get("target"), "dispatch.target") or "",
            stratum=_optional_str(raw.get("stratum"), "dispatch.stratum") or "",
            repo=_optional_str(raw.get("repo"), "dispatch.repo"),
            host=_optional_str(raw.get("host"), "dispatch.host"),
            provider=_optional_str(raw.get("provider"), "dispatch.provider"),
            classification=record,
            batch_id=_optional_str(raw.get("batch_id"), "dispatch.batch_id"),
            batch_index=batch_index,
        )

    def content(self) -> dict[str, Any]:
        return {
            "dispatch_id": self.dispatch_id,
            "bead_id": self.bead_id,
            "actual_route": self.actual_route,
            "recorded_at": self.recorded_at,
            "target": self.target,
            "stratum": self.stratum,
            "repo": self.repo,
            "host": self.host,
            "provider": self.provider,
            "batch_id": self.batch_id,
            "batch_index": self.batch_index,
        }


def _classification_record(raw: Any) -> ClassificationRecord:
    """Validate a dispatch's embedded classification with the M7 contract."""
    try:
        from .policy import normalize_recommendation_bundle

        bundle = normalize_recommendation_bundle({"schema_version": "1.0", "episodes": [raw]})
    except Exception as exc:
        raise LiveRoutingError(f"dispatch.classification is invalid: {exc}") from exc
    return bundle.records[0]


# -- ledger ------------------------------------------------------------------


class LiveRoutingLedger:
    """Append-only JSONL ledger of live requests and advisory records.

    Budget is always re-derived by reading the file; nothing is cached in
    memory between calls, because the Go hook invokes a fresh process for every
    routed dispatch.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def entries(self) -> list[dict[str, Any]]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        entries: list[dict[str, Any]] = []
        for number, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except ValueError as exc:
                raise LiveRoutingError(
                    f"ledger {self.path} line {number} is not valid JSON: {exc}"
                ) from exc
            if not isinstance(value, dict):
                raise LiveRoutingError(f"ledger {self.path} line {number} is not a JSON object")
            entries.append(value)
        return entries

    def requests_spent(self) -> int:
        spent = 0
        for entry in self.entries():
            if entry.get("kind") != LEDGER_REQUEST_KIND:
                continue
            requests = entry.get("requests", 1)
            if isinstance(requests, bool) or not isinstance(requests, int) or requests < 0:
                raise LiveRoutingError(
                    f"ledger {self.path} has a non-integer request count {requests!r}"
                )
            spent += requests
        return spent

    def append(self, entry: Mapping[str, Any]) -> None:
        payload = json.dumps(dict(entry), sort_keys=True, ensure_ascii=False, allow_nan=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(payload + "\n")

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Hold an exclusive cross-process lock for a read-then-append cycle."""
        if fcntl is None:  # pragma: no cover - non-POSIX
            yield
            return
        lock_path = Path(f"{self.path}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def reserve(self, entry: Mapping[str, Any], *, cap: int) -> bool:
        """Atomically charge one request if the budget has room.

        The spent count is re-read under the lock immediately before the append,
        so concurrent processes cannot both consume the last slot.
        """
        with self.lock():
            if self.requests_spent() >= cap:
                return False
            self.append({**entry, "kind": LEDGER_REQUEST_KIND, "requests": 1})
            return True


# -- classifier adapter ------------------------------------------------------


class SingleAttemptTransportClassifier:
    """Adapt a bounded transport send to a one-attempt classifier callback.

    The transport is configured with ``retry.max_attempts == 1`` by the caller;
    the live loop owns retries so that every HTTP request is charged to the
    ledger. The returned intent is read from the validated Jev answer list for
    the ``primary_intent`` question.
    """

    def __init__(self, transport: Any, request_builder: Callable[[LiveDispatch], Any]):
        self._transport = transport
        self._request_builder = request_builder

    def __call__(self, dispatch: LiveDispatch) -> ClassificationOutcome:
        from .jev import validate_response

        request = self._request_builder(dispatch)
        result = self._transport.send(request)
        if not result.ok:
            raise LiveRoutingError(
                f"live Jev request failed for {dispatch.dispatch_id}: "
                f"{result.failure_class or 'unknown_failure'}"
            )
        answers = validate_response(result.response, request.body)
        intent: str | None = None
        confidence: float | None = None
        for answer in answers:
            if answer.get("question_id") != "primary_intent":
                continue
            body = answer.get("answer")
            if isinstance(body, Mapping):
                intent = body.get("choice")
                confidence = body.get("confidence")
            break
        return ClassificationOutcome(
            intent=intent,
            confidence=confidence,
            attempts=result.attempts or 1,
        )


# -- advisor -----------------------------------------------------------------


@dataclass
class LiveRoutingAdvisor:
    """Records advisory next-route evidence without ever changing a route."""

    registration: CanaryRegistration
    registration_hash: str
    catalog: PolicyCatalog
    ledger: LiveRoutingLedger
    max_requests: int
    kill_switch_path: str | None = None
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    clock: Callable[[], str] = _utc_now
    _resolved: dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    @classmethod
    def open(
        cls,
        *,
        registration_path: str | Path,
        catalog_path: str | Path,
        ledger_path: str | Path,
        expected_registration_hash: str | None = None,
        kill_switch_path: str | None = None,
        max_requests: int | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        clock: Callable[[], str] = _utc_now,
    ) -> "LiveRoutingAdvisor":
        registration, registration_hash = verify_registration_output(
            registration_path, expected_registration_hash
        )
        catalog = load_catalog(catalog_path)
        if catalog.catalog_version != registration.catalog_version:
            raise LiveRoutingError(
                f"catalog version {catalog.catalog_version!r} does not match the "
                f"registered catalog_version {registration.catalog_version!r}"
            )
        cap = registration.max_requests
        if max_requests is not None:
            if isinstance(max_requests, bool) or not isinstance(max_requests, int):
                raise LiveRoutingError("max_requests must be an integer")
            if max_requests < 0:
                raise LiveRoutingError("max_requests must be nonnegative")
            if max_requests > MAX_ALLOWED_REQUESTS:
                raise LiveRoutingError(
                    f"max_requests {max_requests} exceeds the owner-approved hard cap "
                    f"{MAX_ALLOWED_REQUESTS}"
                )
            cap = min(cap, max_requests)
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise LiveRoutingError("max_attempts must be an integer >= 1")
        return cls(
            registration=registration,
            registration_hash=registration_hash,
            catalog=catalog,
            ledger=LiveRoutingLedger(ledger_path),
            max_requests=cap,
            kill_switch_path=kill_switch_path,
            max_attempts=max_attempts,
            clock=clock,
        )

    # -- switches and budget (re-derived every call) -------------------------

    def poll_kill_switch(self) -> bool:
        """Read the kill switch fresh; never cache it across loop iterations."""
        if not self.kill_switch_path:
            return False
        return Path(self.kill_switch_path).exists()

    def request_budget(self) -> dict[str, int]:
        """Re-derive spent/remaining from the ledger at the moment of the call."""
        spent = self.ledger.requests_spent()
        return {
            "max_requests": self.max_requests,
            "requests_spent": spent,
            "requests_remaining": max(0, self.max_requests - spent),
        }

    # -- classification ------------------------------------------------------

    def _classify_bounded(
        self,
        dispatch: LiveDispatch,
        classify: Callable[[LiveDispatch], ClassificationOutcome],
    ) -> ClassificationOutcome | None:
        """Run a classifier within the ledger budget, counting every attempt.

        The kill switch and the ledger budget are re-polled inside the loop, so
        a switch engaged mid-retry stops immediately and each retry consumes a
        request slot. Returns ``None`` when the budget is exhausted, the switch
        is engaged, or every attempt failed.
        """
        last_error: str | None = None
        for attempt in range(1, self.max_attempts + 1):
            if self.poll_kill_switch():
                return None
            entry = {
                "version": LIVE_ROUTING_RECORD_VERSION,
                "dispatch_id": dispatch.dispatch_id,
                "bead_id": dispatch.bead_id,
                "batch_id": dispatch.batch_id,
                "batch_index": dispatch.batch_index,
                "attempt": attempt,
                "recorded_at": self.clock(),
            }
            if not self.ledger.reserve(entry, cap=self.max_requests):
                return None
            try:
                outcome = classify(dispatch)
            except Exception as exc:  # fail-open: a failed attempt is recorded, not raised
                last_error = str(exc)
                self._resolved["last_classify_error"] = last_error
                continue
            # A classifier may report extra internal attempts (result.attempts);
            # charge the remainder so retries inside an adapter are not free.
            extra = max(0, int(getattr(outcome, "attempts", 1)) - 1)
            for _ in range(extra):
                if not self.ledger.reserve({**entry, "internal_retry": True}, cap=self.max_requests):
                    break
            return outcome
        return None

    # -- advice --------------------------------------------------------------

    def advise(
        self,
        dispatch: LiveDispatch,
        classify: Callable[[LiveDispatch], ClassificationOutcome] | None = None,
    ) -> dict[str, Any]:
        """Record one advisory row for *dispatch* and return it.

        ``classify`` is only consulted when the dispatch does not already carry
        a classification and the kill switch is clear. With no classifier (or a
        failed one) the intent is ``unknown`` and the suggestion abstains.
        """
        killed = self.poll_kill_switch()
        budget_before = self.request_budget()
        outcome: ClassificationOutcome | None = None
        classification_source = "none"
        if dispatch.classification is not None:
            record = dispatch.classification
            classification_source = "supplied"
        else:
            if classify is not None and not killed:
                outcome = self._classify_bounded(dispatch, classify)
            if outcome is not None:
                classification_source = "jev"
                record = self._record_for(dispatch, outcome)
            else:
                record = self._record_for(dispatch, None)

        # Re-poll once more so a switch engaged by a classifier mid-loop is
        # reflected in the recorded row (and in the batch summary) rather than
        # only stopping the next call.
        killed = self.poll_kill_switch() or killed
        recommendation = self._recommendation_for(record)
        suggested = (
            recommendation.get("recommended_candidate")
            if recommendation.get("decision") == "recommended"
            else None
        )
        budget_after = self.request_budget()
        record_payload: dict[str, Any] = {
            "kind": LIVE_ROUTING_RECORD_KIND,
            "version": LIVE_ROUTING_RECORD_VERSION,
            "dispatch_id": dispatch.dispatch_id,
            "bead_id": dispatch.bead_id,
            "recorded_at": dispatch.recorded_at,
            "registration_id": self.registration.registration_id,
            "registration_hash": self.registration_hash,
            "policy_id": self.registration.policy_id,
            "policy_kind": self.registration.policy_kind,
            "catalog_version": self.catalog.catalog_version,
            "primary_intent": record.intent,
            "confidence": record.confidence,
            "classification_source": classification_source,
            "actual_route": dispatch.actual_route,
            "suggested_route": suggested,
            "recommendation": dict(recommendation),
            "kill_switch": killed,
            "request_budget": budget_after,
            "requests_charged": budget_after["requests_spent"] - budget_before["requests_spent"],
            "unknown_as_abstain": record.intent is None,
            "route_changed": False,
            "applied_route": dispatch.actual_route,
            "executes_changes": False,
            "advisory_only": True,
        }
        record_payload["advisory_hash"] = canonical_hash(record_payload)
        self.ledger.append(record_payload)
        return record_payload

    def advise_batch(
        self,
        dispatches: Sequence[LiveDispatch],
        classify: Callable[[LiveDispatch], ClassificationOutcome] | None = None,
    ) -> list[dict[str, Any]]:
        """Advise each dispatch in order, re-polling the switch per iteration."""
        return [self.advise(dispatch, classify) for dispatch in dispatches]

    # -- helpers -------------------------------------------------------------

    def _record_for(
        self,
        dispatch: LiveDispatch,
        outcome: ClassificationOutcome | None,
    ) -> ClassificationRecord:
        intent = outcome.intent if outcome is not None else None
        confidence = outcome.confidence if outcome is not None else None
        flags = outcome.flags if outcome is not None else ()
        return ClassificationRecord(
            episode_id=dispatch.dispatch_id,
            as_of=dispatch.recorded_at,
            observed_at=dispatch.recorded_at,
            intent=intent,
            confidence=confidence,
            flags=frozenset(flags),
            provider=dispatch.provider,
            repo=dispatch.repo,
            host=dispatch.host,
            current_routing={self.registration.policy_kind: dispatch.actual_route},
        )

    def _recommendation_for(self, record: ClassificationRecord) -> Mapping[str, Any]:
        config = PolicyConfig(confidence_threshold=self.registration.confidence_threshold)
        for item in recommendations_for_record(record, self.catalog, config):
            if item.get("kind") == self.registration.policy_kind:
                return item
        raise LiveRoutingError(
            f"M7 recommender returned no {self.registration.policy_kind!r} recommendation"
        )


def load_batch_request(raw: Any) -> list[LiveDispatch]:
    """Parse a live-routing batch request.

    Accepts either a single dispatch object or a ``{"dispatches": [...]}``
    document. A shared ``batch_id`` may be supplied at the top level or inferred
    from the first dispatch.
    """
    if isinstance(raw, Mapping) and "dispatches" in raw:
        raw_dispatches = raw.get("dispatches")
        if not isinstance(raw_dispatches, list) or not raw_dispatches:
            raise LiveRoutingError("live routing request.dispatches must be a non-empty list")
        batch_id = _optional_str(raw.get("batch_id"), "request.batch_id")
        dispatches = []
        for index, item in enumerate(raw_dispatches):
            if not isinstance(item, Mapping):
                raise LiveRoutingError("each dispatch must be a JSON object")
            merged = dict(item)
            if batch_id is not None and merged.get("batch_id") is None:
                merged["batch_id"] = batch_id
            if merged.get("batch_index") is None:
                merged["batch_index"] = index
            dispatches.append(LiveDispatch.from_mapping(merged))
        return dispatches
    if isinstance(raw, Mapping):
        return [LiveDispatch.from_mapping(raw)]
    raise LiveRoutingError("live routing request must be a JSON object")


def batch_result(records: Sequence[Mapping[str, Any]], advisor: LiveRoutingAdvisor) -> dict[str, Any]:
    """Wrap advisory rows in the deterministic batch envelope the CLI prints."""
    budget = advisor.request_budget()
    payload: dict[str, Any] = {
        "schema_version": LIVE_ROUTING_SCHEMA_VERSION,
        "kind": LIVE_ROUTING_BATCH_KIND,
        "registration_id": advisor.registration.registration_id,
        "registration_hash": advisor.registration_hash,
        "policy_id": advisor.registration.policy_id,
        "policy_kind": advisor.registration.policy_kind,
        "catalog_version": advisor.catalog.catalog_version,
        "max_requests": budget["max_requests"],
        "requests_spent": budget["requests_spent"],
        "requests_remaining": budget["requests_remaining"],
        "executes_changes": False,
        "advisory_only": True,
        "records": [dict(record) for record in records],
    }
    payload["result_hash"] = canonical_hash(payload)
    return payload


def apply_classifications(
    dispatches: Sequence[LiveDispatch],
    classifications: Mapping[str, Any],
) -> list[LiveDispatch]:
    """Attach precomputed Jev classifications (by dispatch_id or bead_id)."""
    resolved: list[LiveDispatch] = []
    for dispatch in dispatches:
        if dispatch.classification is not None:
            resolved.append(dispatch)
            continue
        raw = classifications.get(dispatch.dispatch_id)
        if raw is None:
            raw = classifications.get(dispatch.bead_id)
        if raw is None:
            resolved.append(dispatch)
            continue
        from dataclasses import replace

        resolved.append(replace(dispatch, classification=_classification_record(raw)))
    return resolved


def write_batch_result(payload: Mapping[str, Any], out_path: str | None) -> None:
    text = json.dumps(dict(payload), indent=2, sort_keys=True, ensure_ascii=False)
    if out_path:
        Path(out_path).write_text(text + "\n", encoding="utf-8")
    else:
        os.write(1, (text + "\n").encode("utf-8"))
