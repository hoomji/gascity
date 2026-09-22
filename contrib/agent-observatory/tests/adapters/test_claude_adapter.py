"""Tests for the Claude Code transcript adapter."""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adapter_support as support  # noqa: E402

from agent_observatory.adapters import AdapterError, adapter_for_path, read_source  # noqa: E402
from agent_observatory.store import ObservatoryStore  # noqa: E402


class ClaudeAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = support.make_temp_dir()
        self.addCleanup(self.tmp.cleanup)
        self.source = support.fixture("claude")

    def _read(self, path=None):
        return read_source(path or self.source, provider="claude", context=support.CONTEXT)

    def test_detects_claude_layout(self):
        path = support.claude_source(self.tmp.name)
        adapter = adapter_for_path(path)
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.provider, "claude")

    def test_identity_parent_and_usage_deduplication(self):
        result = self._read()
        self.assertEqual(result.session_id, "claude-sess-1")
        self.assertEqual(result.parent_session_id, "claude-parent-0")
        coverage = result.coverage()
        self.assertEqual(coverage["usage_events"], 2)
        self.assertEqual(coverage["tool_calls"], 1)
        self.assertEqual(coverage["tool_results"], 1)
        # One assistant message is split across two records that repeat usage.
        self.assertEqual(coverage["by_kind"]["assistant_message"], 1)

    def test_reasoning_is_never_exported(self):
        result = self._read()
        for record in result.records:
            self.assertNotIn("private chain of thought", record.get("text") or "")
            self.assertIsNone(record.get("title"))
        self.assertGreaterEqual(result.skipped.get("reasoning", 0), 1)

    def test_tool_call_and_result_pair_by_tool_use_id(self):
        records = self._read().records
        calls = [record for record in records if record["kind"] == "tool_call"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["tool_name"], "Bash")
        self.assertEqual(calls[0]["tool_call_id"], "toolu_1")
        self.assertEqual(calls[0]["command"], "echo hi")
        results = [record for record in records if record["kind"] == "tool_result"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["tool_call_id"], "toolu_1")
        self.assertEqual(results[0]["exit_code"], 0)

    def test_secrets_are_redacted(self):
        records = self._read().records
        texts = " ".join(record.get("text") or "" for record in records)
        self.assertNotIn("supersecretvalue", texts)
        self.assertIn("[REDACTED]", texts)

    def test_title_revisions_preserve_order(self):
        result = self._read()
        self.assertEqual([revision.title for revision in result.title_revisions], ["First title", "Second title"])
        self.assertEqual([revision.position for revision in result.title_revisions], [6, 8])

    def test_partial_trailing_line_is_reported_not_dropped(self):
        path = os.path.join(self.tmp.name, "partial.jsonl")
        with open(self.source, "r", encoding="utf-8") as handle:
            content = handle.read()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.write('{"type":"assistant","uuid":"u-cut","message":')  # unterminated
        result = self._read(path)
        self.assertTrue(result.partial_trailing_line)
        self.assertTrue(result.errors)
        self.assertIn("partial trailing line", result.errors[0])
        # The valid prefix is still adapted.
        self.assertEqual(result.session_id, "claude-sess-1")

    def test_malformed_interior_line_is_a_hard_error(self):
        path = os.path.join(self.tmp.name, "broken.jsonl")
        with open(self.source, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
        lines.insert(2, "{not json}\n")
        with open(path, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
        with self.assertRaises(AdapterError):
            self._read(path)

    def test_unsupported_provider_is_rejected(self):
        with self.assertRaises(AdapterError):
            read_source(self.source, provider="opencode", context=support.CONTEXT)

    def test_replay_is_idempotent(self):
        result = self._read()
        db = os.path.join(self.tmp.name, "proj.db")
        first = support.import_records(db, result.records, self.tmp.name, name="first.jsonl")
        self.assertEqual(first.inserted, len(result.records))
        # Same canonical events from a different file path must not duplicate.
        second = support.import_records(db, result.records, self.tmp.name, name="second.jsonl")
        self.assertEqual(second.inserted, 0)
        self.assertEqual(second.duplicates, len(result.records))
        with ObservatoryStore(db) as store:
            self.assertEqual(store.event_count(), len(result.records))
            usage_rows = store.conn.execute("SELECT COUNT(*) FROM event_usage").fetchone()[0]
            self.assertEqual(usage_rows, 2)

    def test_appended_source_adds_only_new_events(self):
        result = self._read()
        db = os.path.join(self.tmp.name, "append.db")
        with ObservatoryStore(db) as store:
            store.import_jsonl(support.write_temp_jsonl(self.tmp.name, result.records, name="base.jsonl"))
        with open(self.source, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
        extra = {
            "type": "assistant",
            "uuid": "u-extra",
            "sessionId": "claude-sess-1",
            "session_id": "claude-parent-0",
            "timestamp": "2026-09-21T10:00:06.000Z",
            "message": {
                "id": "msg-3",
                "role": "assistant",
                "model": "claude-test-1",
                "content": [{"type": "text", "text": "appended"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        }
        extended = os.path.join(self.tmp.name, "extended.jsonl")
        with open(extended, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
            handle.write(json.dumps(extra) + "\n")
        result2 = self._read(extended)
        self.assertEqual(len(result2.records), len(result.records) + 1)
        with ObservatoryStore(db) as store:
            imported = store.import_jsonl(
                support.write_temp_jsonl(self.tmp.name, result2.records, name="extended-export.jsonl")
            )
            self.assertEqual(imported.duplicates, len(result.records))
            self.assertEqual(imported.inserted, 1)


if __name__ == "__main__":
    unittest.main()
