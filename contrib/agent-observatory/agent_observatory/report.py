"""Deterministic report JSON over the analytical projection.

Reports are observational. Counts describe what the imported evidence contains;
they do not establish effectiveness, causality, or dollar cost. The output has no
timestamps and uses sorted keys so two runs over the same projection are
byte-identical.

Evidence accounting rules:

* a tool request is not proof of execution, so only *invocation* events add to
  tool counts and command categories;
* an invocation's outcome comes from a result event linked by
  ``(session, tool_call_id)``; a request-only event stays ``unknown`` even if it
  carries a claimed exit code;
* one final outcome is counted per invocation (and per category), so duplicate
  result events never inflate outcomes;
* a result with no matching invocation is still observed evidence and is counted
  once by its own call identity, but its command never becomes an invocation.
"""

from __future__ import annotations

from collections import Counter, OrderedDict
from typing import Any, Iterable

from .canonical import identity_key
from .commands import CATEGORIES, categorize_command
from .contract import COVERAGE_FIELDS
from .store import ObservatoryStore

REPORT_VERSION = "1"

RESULT_KINDS = frozenset({"result", "tool_result", "command_result", "test_result", "test_call_result"})
INVOCATION_KINDS = frozenset({"tool_call", "tool_request", "command", "test_invocation"})

# Request-only invocation kinds: their own exit_code is a claim about a request,
# not an observed execution result, so it never yields success/failure without a
# linked result event.
REQUEST_ONLY_KINDS = frozenset({"tool_call", "tool_request", "test_invocation"})

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


def _usage_is_present(usage_row: dict[str, Any] | None) -> bool:
    if usage_row is None:
        return False
    return any(
        usage_row.get(field) is not None
        for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens")
    )


def _outcome_from_exit_codes(exit_codes: set[int]) -> str:
    """Collapse one unit's observed exit codes into a single outcome.

    Zero codes means no usable result (``unknown``); exactly one distinct code is
    usable; conflicting codes cannot be collapsed, so they stay ``unknown``.
    """
    if len(exit_codes) == 1:
        return exit_outcome(next(iter(exit_codes)))
    return _OUTCOME_UNKNOWN


def _invocation_outcome(invocation: dict[str, Any], linked_results: list[dict[str, Any]]) -> str:
    """Return the single observed outcome for one invocation."""
    exit_codes = {
        result["event"]["exit_code"]
        for result in linked_results
        if result["event"].get("exit_code") is not None
    }
    if exit_codes:
        return _outcome_from_exit_codes(exit_codes)
    event = invocation["event"]
    if event["kind"] not in REQUEST_ONLY_KINDS and event.get("exit_code") is not None:
        # A directly observed command carries its own result. A request does not.
        return exit_outcome(event["exit_code"])
    return _OUTCOME_UNKNOWN


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

    # Per-event projections and role classification.
    projections: list[dict[str, Any]] = []
    for event in events:
        provider_counts[event["provider"]] += 1
        kind_counts[event["kind"]] += 1

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
                "session": identity_key(
                    (event["city_id"], event["host_id"], event["provider"], event["session_id"])
                ),
                "call_id": event.get("tool_call_id") or "",
                "categories": categories,
                "role": event_role(event["kind"]),
            }
        )

    def unit_key(projection: dict[str, Any]) -> tuple[str, str]:
        return (projection["session"], projection["call_id"] or projection["event"]["event_id"])

    # One entry per distinct invocation; repeated invocation records with the same
    # call identity cannot inflate counts.
    invocations: list[dict[str, Any]] = []
    seen_invocation_keys: set[tuple[str, str]] = set()
    for projection in projections:
        if projection["role"] != "invocation":
            continue
        key = unit_key(projection)
        if key in seen_invocation_keys:
            continue
        seen_invocation_keys.add(key)
        invocations.append(projection)

    # Results indexed by call identity so outcomes can be paired with invocations.
    results_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for projection in projections:
        if projection["role"] != "result":
            continue
        results_by_key.setdefault(unit_key(projection), []).append(projection)

    # Invocation-only tool counts: a result event never adds a tool count.
    for invocation in invocations:
        tool_name = invocation["event"].get("tool_name")
        if tool_name:
            tool_counts[tool_name] += 1

    # Command categories describe invocations. A result or arbitrary prose
    # record that merely contains a command is never promoted to an invocation.
    for invocation in invocations:
        for category in invocation["categories"]:
            category_counts[category] += 1

    # One final outcome per invocation, applied to each of its categories.
    test_invocations = 0
    test_results: Counter[str] = Counter()
    matched_result_keys: set[tuple[str, str]] = set()
    for invocation in invocations:
        key = unit_key(invocation)
        linked = results_by_key.get(key, [])
        if linked:
            matched_result_keys.add(key)
        outcome = _invocation_outcome(invocation, linked)
        totals[outcome] += 1
        for category in invocation["categories"]:
            outcomes_by_category[category][outcome] += 1
        if "test" in invocation["categories"]:
            test_invocations += 1
            test_results[outcome] += 1

    # Orphan results (no matching invocation) are still observed evidence. Count
    # one outcome per call identity; their command never becomes an invocation.
    orphan_results: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for projection in projections:
        if projection["role"] != "result":
            continue
        key = unit_key(projection)
        if key in matched_result_keys:
            continue
        orphan_results.setdefault(key, []).append(projection)

    for group in orphan_results.values():
        exit_codes = {
            result["event"]["exit_code"]
            for result in group
            if result["event"].get("exit_code") is not None
        }
        outcome = _outcome_from_exit_codes(exit_codes)
        totals[outcome] += 1
        orphan_categories: set[str] = set()
        for result in group:
            orphan_categories.update(result["categories"])
        for category in orphan_categories:
            outcomes_by_category[category][outcome] += 1

    # Sessions and step sequences. Labels use a collision-free identity encoding
    # because identity components are unrestricted strings.
    session_steps: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    for session_key in store.session_keys():
        session_events = store.session_events(session_key)
        if not session_events:
            continue
        label = identity_key(session_key)
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
            "A tool request event does not prove execution or success; only a linked or directly observed result carries an outcome.",
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
