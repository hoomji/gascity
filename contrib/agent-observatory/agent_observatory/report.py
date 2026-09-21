"""Deterministic report JSON over the analytical projection.

Reports are observational. Counts describe what the imported evidence contains;
they do not establish effectiveness, causality, or dollar cost. The output has no
timestamps and uses sorted keys so two runs over the same projection are
byte-identical.
"""

from __future__ import annotations

from collections import Counter, OrderedDict
from typing import Any, Iterable

from .commands import CATEGORIES, categorize_command
from .contract import COVERAGE_FIELDS
from .store import ObservatoryStore

REPORT_VERSION = "1"

RESULT_KINDS = frozenset({"result", "tool_result", "command_result", "test_result", "test_call_result"})
INVOCATION_KINDS = frozenset({"tool_call", "tool_request", "command", "test_invocation"})

_OUTCOME_SUCCESS = "success"
_OUTCOME_FAILURE = "failure"
_OUTCOME_UNKNOWN = "unknown"


def event_role(kind: str) -> str:
    """Classify an event as an invocation, a result, or other.

    A request is not proof of execution: ``tool_call``/``command`` events are
    invocations, while ``*_result`` kinds carry an observed outcome.
    """
    if kind in RESULT_KINDS:
        return "result"
    if kind in INVOCATION_KINDS:
        return "invocation"
    return "other"


def exit_outcome(exit_code: int | None) -> str:
    """Map an exit code to success/failure/unknown.

    A missing exit code means unknown -- it is never treated as success.
    """
    if exit_code is None:
        return _OUTCOME_UNKNOWN
    if exit_code == 0:
        return _OUTCOME_SUCCESS
    return _OUTCOME_FAILURE


def _session_label(event: dict[str, Any]) -> str:
    return "|".join(
        [event["city_id"], event["host_id"], event["provider"], event["session_id"]]
    )


def _usage_is_present(usage_row: dict[str, Any] | None) -> bool:
    if usage_row is None:
        return False
    return any(
        usage_row.get(field) is not None
        for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens")
    )


def build_report(store: ObservatoryStore) -> dict[str, Any]:
    """Build a deterministic report dictionary from *store*."""
    events = list(store.iter_events())

    provider_counts: Counter[str] = Counter()
    kind_counts: Counter[str] = Counter()
    tool_counts: Counter[str] = Counter()
    missing_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    outcomes_by_category: dict[str, Counter[str]] = {
        category: Counter() for category in CATEGORIES
    }
    totals: Counter[str] = Counter()

    usage_cache: dict[tuple[str, str, str, str, str], bool] = {}

    # First pass: per-event projections and role classification.
    projections: list[dict[str, Any]] = []
    for event in events:
        provider_counts[event["provider"]] += 1
        kind_counts[event["kind"]] += 1

        if event["tool_name"]:
            tool_counts[event["tool_name"]] += 1

        identity = (
            event["city_id"],
            event["host_id"],
            event["provider"],
            event["session_id"],
            event["event_id"],
        )
        if identity not in usage_cache:
            usage_cache[identity] = _usage_is_present(store.get_usage(identity))
        for field in COVERAGE_FIELDS:
            if field == "usage":
                if not usage_cache[identity]:
                    missing_counts[field] += 1
            elif event.get(field) is None:
                missing_counts[field] += 1

        categories = categorize_command(event.get("command")) if event.get("command") else frozenset()

        projections.append(
            {
                "event": event,
                "session": _session_label(event),
                "call_id": event.get("tool_call_id") or "",
                "categories": categories,
                "role": event_role(event["kind"]),
                "outcome": exit_outcome(event.get("exit_code")),
            }
        )

    # Command categories describe invocations, not results: a result event that
    # repeats its invocation's command must not inflate invocation counts.
    seen_category_keys: set[tuple[str, str]] = set()
    for projection in projections:
        if projection["role"] == "result" or not projection["categories"]:
            continue
        key = (projection["session"], projection["call_id"] or projection["event"]["event_id"])
        if key in seen_category_keys:
            continue
        seen_category_keys.add(key)
        for category in projection["categories"]:
            category_counts[category] += 1

    # Observed outcomes: a result event is counted once per (session, call id)
    # so a repeated result cannot inflate counts.
    seen_result_keys: set[tuple[str, str]] = set()
    for projection in projections:
        event = projection["event"]
        role = projection["role"]
        if not event.get("command") and role != "result":
            continue
        if role == "result":
            key = (projection["session"], projection["call_id"] or event["event_id"])
            if key in seen_result_keys:
                continue
            seen_result_keys.add(key)
        for category in projection["categories"]:
            outcomes_by_category[category][projection["outcome"]] += 1
        totals[projection["outcome"]] += 1

    # Test invocations and their observed results are reported separately. A
    # result event is linked to its invocation by (session, tool_call_id); if no
    # result event exists, the invocation's own exit_code (or unknown) is used.
    result_index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for projection in projections:
        if projection["role"] != "result":
            continue
        key = (projection["session"], projection["call_id"] or projection["event"]["event_id"])
        result_index.setdefault(key, []).append(projection)

    test_invocations = 0
    test_results: Counter[str] = Counter()
    seen_invocation_keys: set[tuple[str, str]] = set()
    for projection in projections:
        if projection["role"] != "invocation" or "test" not in projection["categories"]:
            continue
        event = projection["event"]
        key = (projection["session"], projection["call_id"] or event["event_id"])
        if key in seen_invocation_keys:
            continue
        seen_invocation_keys.add(key)
        test_invocations += 1
        linked = result_index.get(key)
        if linked:
            outcome = linked[0]["outcome"]
        else:
            outcome = projection["outcome"]
        test_results[outcome] += 1

    # Sessions and step sequences.
    session_steps: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    for session_key in store.session_keys():
        session_events = store.session_events(session_key)
        if not session_events:
            continue
        label = "|".join(session_key)
        sequence = [event["kind"] for event in session_events]
        transitions: Counter[str] = Counter()
        for previous, following in zip(sequence, sequence[1:]):
            transitions[f"{previous}->{following}"] += 1
        session_steps[label] = {
            "event_count": len(session_events),
            "sequence": sequence,
            "transitions": dict(transitions),
        }

    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "coverage": {
            "sessions": store.session_count(),
            "events": store.event_count(),
            "by_provider": dict(provider_counts),
            "by_kind": dict(kind_counts),
            "missing_fields": {field: missing_counts[field] for field in COVERAGE_FIELDS},
        },
        "tool_counts": dict(tool_counts),
        "command_categories": {category: category_counts[category] for category in CATEGORIES},
        "observed_outcomes": {
            "by_category": {
                category: {
                    _OUTCOME_SUCCESS: outcomes_by_category[category][_OUTCOME_SUCCESS],
                    _OUTCOME_FAILURE: outcomes_by_category[category][_OUTCOME_FAILURE],
                    _OUTCOME_UNKNOWN: outcomes_by_category[category][_OUTCOME_UNKNOWN],
                }
                for category in CATEGORIES
            },
            "totals": {
                _OUTCOME_SUCCESS: totals[_OUTCOME_SUCCESS],
                _OUTCOME_FAILURE: totals[_OUTCOME_FAILURE],
                _OUTCOME_UNKNOWN: totals[_OUTCOME_UNKNOWN],
            },
        },
        "test": {
            "invocations": test_invocations,
            "results": {
                "passed": test_results[_OUTCOME_SUCCESS],
                "failed": test_results[_OUTCOME_FAILURE],
                "unknown": test_results[_OUTCOME_UNKNOWN],
            },
        },
        "session_steps": session_steps,
        "notes": [
            "Counts are observational and do not establish effectiveness, causality, or cost.",
            "A tool request event does not prove execution or success; only result events carry an observed outcome.",
            "A missing exit_code is reported as unknown, never as success.",
        ],
    }
    return report


def category_summary(commands: Iterable[str | None]) -> dict[str, int]:
    """Count categories for a standalone iterable of commands."""
    counts = {category: 0 for category in CATEGORIES}
    for command in commands:
        for category in categorize_command(command):
            counts[category] += 1
    return counts
