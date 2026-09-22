"""Tests for the Codex rollout transcript adapter."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adapter_support as support  # noqa: E402

from agent_observatory.adapters import adapter_for_path, read_source  # noqa: E402
from agent_observatory.store import ObservatoryStore  # noqa: E402


class CodexAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = support.make_temp_dir()
        self.addCleanup(self.tmp.cleanup)
        self.source = support.fixture("codex")

    def _read(self, path=None):
        return read_source(path or self.source, provider="codex", context=support.CONTEXT)

    def test_detects_codex_layout(self):
        path = support.codex_source(self.tmp.name)
        adapter = adapter_for_path(path)
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.provider, "codex")

    def test_session_metadata_and_counts(self):
        result = self._read()
        self.assertEqual(result.session_id, "codex-sess-1")
        self.assertEqual(result.parent_session_id, "codex-parent-0")
        coverage = result.coverage()
        self.assertEqual(coverage["by_kind"]["message"], 2)
        self.assertEqual(coverage["by_kind"]["tool_call"], 2)
        self.assertEqual(coverage["by_kind"]["tool_result"], 2)
        self.assertEqual(coverage["by_kind"]["usage"], 1)

    def test_function_call_pairing_exit_code_and_duration(self):
        records = self._read().records
        call = next(record for record in records if record["kind"] == "tool_call" and record["tool_call_id"] == "call_1")
        self.assertEqual(call["tool_name"], "exec_command")
        self.assertEqual(call["command"], "echo hi && pytest")
        result = next(record for record in records if record["kind"] == "tool_result" and record["tool_call_id"] == "call_1")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["duration_ms"], 1500)
        self.assertIn("2 passed", result["text"])

    def test_custom_tool_call_pairing(self):
        records = self._read().records
        call = next(record for record in records if record["kind"] == "tool_call" and record["tool_call_id"] == "call_2")
        self.assertEqual(call["tool_name"], "apply_patch")
        self.assertIn("Begin Patch", call["text"])
        result = next(record for record in records if record["kind"] == "tool_result" and record["tool_call_id"] == "call_2")
        self.assertIn("Success", result["text"])

    def test_usage_is_per_turn_and_mapped(self):
        records = self._read().records
        usage_record = next(record for record in records if record["kind"] == "usage")
        self.assertEqual(
            usage_record["usage"],
            {
                "input_tokens": 100,
                "output_tokens": 7,
                "cache_read_tokens": 20,
                "cache_write_tokens": 5,
                "total_tokens": 112,
            },
        )
        self.assertEqual(usage_record["model"], "gpt-test-1")

    def test_encrypted_reasoning_is_never_exported(self):
        result = self._read()
        blob = " ".join(record.get("text") or "" for record in result.records)
        self.assertNotIn("encrypted-reasoning", blob)
        self.assertGreaterEqual(result.skipped.get("payload:reasoning", 0), 1)
        # item_completed duplicates a response_item and is skipped.
        self.assertGreaterEqual(result.skipped.get("event_msg:item_completed", 0), 1)

    def test_replay_is_idempotent(self):
        result = self._read()
        db = os.path.join(self.tmp.name, "proj.db")
        first = support.import_records(db, result.records, self.tmp.name, name="first.jsonl")
        self.assertEqual(first.inserted, len(result.records))
        second = support.import_records(db, result.records, self.tmp.name, name="second.jsonl")
        self.assertEqual(second.inserted, 0)
        with ObservatoryStore(db) as store:
            self.assertEqual(store.event_count(), len(result.records))
            usage_rows = store.conn.execute("SELECT COUNT(*) FROM event_usage").fetchone()[0]
            self.assertEqual(usage_rows, 1)


if __name__ == "__main__":
    unittest.main()
