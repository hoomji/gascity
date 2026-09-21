"""Tests for the deterministic report."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

try:
    from . import support
except ImportError:  # pragma: no cover
    import support

from agent_observatory.commands import CATEGORIES
from agent_observatory.report import build_report
from agent_observatory.store import ObservatoryStore


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "projection.db")

    def _store_with(self, records):
        path = support.write_jsonl(os.path.join(self.tmp.name, "events.jsonl"), records)
        store = ObservatoryStore(self.db_path)
        self.addCleanup(store.close)
        store.import_jsonl(path)
        return store

    def test_coverage_counts_sessions_events_providers_and_missing_fields(self):
        store = self._store_with(
            [
                support.make_record(event_id="e1"),
                support.make_record(event_id="e2", provider="claude", title="second", usage={"input_tokens": 3}),
            ]
        )
        report = build_report(store)
        # sessions are keyed on city/host/provider/session, so a different
        # provider is a different observed session even with the same session id.
        self.assertEqual(report["coverage"]["sessions"], 2)
        self.assertEqual(report["coverage"]["events"], 2)
        self.assertEqual(report["coverage"]["by_provider"], {"codex": 1, "claude": 1})
        self.assertEqual(report["coverage"]["missing_fields"]["title"], 1)
        self.assertEqual(report["coverage"]["missing_fields"]["usage"], 1)

    def test_tool_counts(self):
        store = self._store_with(
            [
                support.make_record(event_id="e1", tool_name="rg"),
                support.make_record(event_id="e2", tool_name="rg"),
                support.make_record(event_id="e3", tool_name="go"),
            ]
        )
        report = build_report(store)
        self.assertEqual(report["tool_counts"], {"go": 1, "rg": 2})

    def test_command_categories_and_unknown_fallback(self):
        store = self._store_with(
            [
                support.make_record(event_id="e1", command="go test ./..."),
                support.make_record(event_id="e2", command="frobnicate --now"),
            ]
        )
        report = build_report(store)
        self.assertEqual(report["command_categories"]["test"], 1)
        self.assertEqual(report["command_categories"]["unknown"], 1)
        self.assertEqual(set(report["command_categories"]), set(CATEGORIES))

    def test_observed_outcomes_distinguish_success_failure_and_unknown(self):
        store = self._store_with(
            [
                support.make_record(event_id="e1", command="ls", exit_code=0),
                support.make_record(event_id="e2", command="ls", exit_code=2),
                support.make_record(event_id="e3", command="ls"),
            ]
        )
        report = build_report(store)
        totals = report["observed_outcomes"]["totals"]
        self.assertEqual(totals, {"success": 1, "failure": 1, "unknown": 1})

    def test_test_invocation_and_result_are_reported_separately(self):
        store = self._store_with(
            [
                support.make_record(
                    event_id="e1", kind="tool_call", command="pytest -q", tool_call_id="tc1"
                ),
                support.make_record(
                    event_id="e2", kind="tool_result", tool_call_id="tc1", exit_code=1
                ),
            ]
        )
        report = build_report(store)
        self.assertEqual(report["test"]["invocations"], 1)
        self.assertEqual(report["test"]["results"], {"passed": 0, "failed": 1, "unknown": 0})

    def test_missing_exit_code_is_unknown_not_success(self):
        store = self._store_with(
            [support.make_record(event_id="e1", kind="tool_call", command="go test ./...", tool_call_id="tc9")]
        )
        report = build_report(store)
        self.assertEqual(report["test"]["invocations"], 1)
        self.assertEqual(report["test"]["results"], {"passed": 0, "failed": 0, "unknown": 1})

    def test_duplicate_result_events_are_not_counted_twice(self):
        store = self._store_with(
            [
                support.make_record(
                    event_id="e1", kind="tool_call", command="go test ./...", tool_call_id="tc1"
                ),
                support.make_record(event_id="e2", kind="tool_result", tool_call_id="tc1", exit_code=1),
                support.make_record(event_id="e3", kind="tool_result", tool_call_id="tc1", exit_code=1),
            ]
        )
        report = build_report(store)
        self.assertEqual(report["test"]["invocations"], 1)
        self.assertEqual(report["test"]["results"]["failed"], 1)
        self.assertEqual(report["observed_outcomes"]["totals"]["failure"], 1)
        # Result events must not be counted as command invocations either.
        self.assertEqual(report["command_categories"]["test"], 1)

    def test_same_session_different_provider_and_host_have_distinct_step_sequences(self):
        store = self._store_with(
            [
                support.make_record(event_id="e1", provider="codex", host_id="h1"),
                support.make_record(event_id="e1", provider="claude", host_id="h1"),
            ]
        )
        report = build_report(store)
        self.assertIn("city-a|h1|codex|session-1", report["session_steps"])
        self.assertIn("city-a|h1|claude|session-1", report["session_steps"])

    def test_step_sequences_and_transitions_are_recorded(self):
        store = self._store_with(
            [
                support.make_record(event_id="e1", timestamp="2026-09-21T10:00:00Z", kind="tool_call"),
                support.make_record(event_id="e2", timestamp="2026-09-21T10:00:01Z", kind="tool_result"),
            ]
        )
        report = build_report(store)
        steps = report["session_steps"]["city-a|host-a|codex|session-1"]
        self.assertEqual(steps["sequence"], ["tool_call", "tool_result"])
        self.assertEqual(steps["transitions"], {"tool_call->tool_result": 1})

    def test_report_is_byte_identical_across_runs(self):
        store = self._store_with(
            [
                support.make_record(event_id="e1", command="go test ./...", exit_code=0),
                support.make_record(event_id="e2", command="rg needle", tool_name="rg"),
            ]
        )
        first = json.dumps(build_report(store), sort_keys=True)
        second = json.dumps(build_report(store), sort_keys=True)
        self.assertEqual(first, second)

    def test_report_does_not_claim_effectiveness_causality_or_dollars(self):
        store = self._store_with([support.make_record(event_id="e1", command="go test ./...", exit_code=0)])
        report = build_report(store)

        def keys(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    yield key
                    yield from keys(value)
            elif isinstance(node, list):
                for item in node:
                    yield from keys(item)

        forbidden_keys = {"effectiveness", "causality", "dollars", "cost_usd", "savings"}
        self.assertFalse(forbidden_keys & set(keys(report)))
        self.assertTrue(report["notes"])


if __name__ == "__main__":
    unittest.main()
