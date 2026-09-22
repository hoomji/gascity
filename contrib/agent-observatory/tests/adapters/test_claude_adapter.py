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

    def _write_records(self, name, records):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        return path

    def test_numeric_timestamp_is_accepted_not_dropped(self):
        path = self._write_records(
            "numeric-ts.jsonl",
            [
                {
                    "type": "user",
                    "uuid": "u-numeric",
                    "sessionId": "sess-numeric",
                    "timestamp": 1789000000000,  # epoch milliseconds
                    "message": {"role": "user", "content": [{"type": "text", "text": "numeric"}]},
                }
            ],
        )
        result = self._read(path)
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0]["timestamp"][:4], "2026", result.records[0]["timestamp"])

    def test_digit_string_timestamp_is_treated_as_epoch_not_dropped(self):
        # F7: a bare all-digit string is an epoch, not an ISO timestamp.
        path = self._write_records(
            "digit-ts.jsonl",
            [
                {
                    "type": "user",
                    "uuid": "u-digit",
                    "sessionId": "sess-digit",
                    "timestamp": "1758198061",  # epoch seconds as a string
                    "message": {"role": "user", "content": [{"type": "text", "text": "digit"}]},
                }
            ],
        )
        result = self._read(path)
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0]["timestamp"][:4], "2025", result.records[0]["timestamp"])

    def test_float_usage_values_are_kept(self):
        path = self._write_records(
            "float-usage.jsonl",
            [
                {
                    "type": "assistant",
                    "uuid": "u-float",
                    "sessionId": "sess-float",
                    "timestamp": "2026-09-21T10:00:01.000Z",
                    "message": {
                        "id": "msg-float",
                        "role": "assistant",
                        "model": "claude-test-1",
                        "content": [{"type": "text", "text": "fractional"}],
                        "usage": {"input_tokens": 10.5, "output_tokens": 2.25},
                    },
                }
            ],
        )
        result = self._read(path)
        record = next(record for record in result.records if record.get("usage"))
        self.assertEqual(record["usage"]["input_tokens"], 10.5)
        self.assertEqual(record["usage"]["output_tokens"], 2.25)
        self.assertEqual(record["usage"]["total_tokens"], 12.75)

    def test_usage_without_a_record_is_flagged(self):
        path = self._write_records(
            "dropped-usage.jsonl",
            [
                {
                    "type": "assistant",
                    "sessionId": "sess-drop",
                    "timestamp": "2026-09-21T10:00:01.000Z",
                    "message": {
                        "id": "msg-drop",
                        "role": "assistant",
                        "content": [{"type": "thinking", "thinking": "private"}],
                        "usage": {"input_tokens": 5, "output_tokens": 1},
                    },
                }
            ],
        )
        result = self._read(path)
        self.assertEqual(result.skipped.get("usage_dropped"), 1)
        self.assertFalse(any(record.get("usage") for record in result.records))

    def test_repeated_usage_is_explained(self):
        path = self._write_records(
            "duplicate-usage.jsonl",
            [
                {
                    "type": "assistant",
                    "uuid": "u-dup-1",
                    "sessionId": "sess-dup",
                    "timestamp": "2026-09-21T10:00:01.000Z",
                    "message": {
                        "id": "msg-dup",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "one"}],
                        "usage": {"input_tokens": 3, "output_tokens": 1},
                    },
                },
                {
                    "type": "assistant",
                    "uuid": "u-dup-2",
                    "sessionId": "sess-dup",
                    "timestamp": "2026-09-21T10:00:02.000Z",
                    "message": {
                        "id": "msg-dup",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "two"}],
                        "usage": {"input_tokens": 3, "output_tokens": 1},
                    },
                },
            ],
        )
        result = self._read(path)
        self.assertEqual(result.skipped.get("usage_duplicate"), 1)
        self.assertEqual(len([r for r in result.records if r.get("usage")]), 1)

    def test_resumed_sessions_are_attributed_per_record(self):
        path = os.path.join(self.tmp.name, "resumed.jsonl")
        records = [
            {
                "type": "user",
                "uuid": "u-old",
                "sessionId": "sess-old",
                "timestamp": "2026-09-21T10:00:00.000Z",
                "message": {"role": "user", "content": [{"type": "text", "text": "before resume"}]},
            },
            {
                "type": "user",
                "uuid": "u-new",
                "sessionId": "sess-new",
                "session_id": "sess-old",
                "timestamp": "2026-09-21T10:00:01.000Z",
                "message": {"role": "user", "content": [{"type": "text", "text": "after resume"}]},
            },
        ]
        with open(path, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

        result = self._read(path)
        self.assertEqual(result.session_id, "sess-old")
        self.assertIsNone(result.parent_session_id)
        first, second = result.records
        self.assertEqual(first["session_id"], "sess-old")
        self.assertIsNone(first["parent_session_id"])
        self.assertEqual(second["session_id"], "sess-new")
        self.assertEqual(second["parent_session_id"], "sess-old")
        self.assertNotEqual(second["session_id"], second["parent_session_id"])

    def test_invalid_record_is_skipped_not_fatal(self):
        path = os.path.join(self.tmp.name, "naive.jsonl")
        records = [
            {
                "type": "user",
                "uuid": "u-good",
                "sessionId": "naive-sess",
                "timestamp": "2026-09-21T10:00:00.000Z",
                "message": {"role": "user", "content": [{"type": "text", "text": "good"}]},
            },
            {
                "type": "user",
                "uuid": "u-naive",
                "sessionId": "naive-sess",
                "timestamp": "2026-09-22 02:00:00",
                "message": {"role": "user", "content": [{"type": "text", "text": "bad"}]},
            },
        ]
        with open(path, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

        result = self._read(path)
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0]["text"], "good")
        self.assertEqual(result.skipped.get("invalid_record"), 1)

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
