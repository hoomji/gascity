"""Tests for the compressed dsh session adapter."""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adapter_support as support  # noqa: E402

import agent_observatory.adapters.dsh as dsh_module  # noqa: E402
from agent_observatory.adapters import AdapterError, adapter_for_path, read_source  # noqa: E402
from agent_observatory.adapters.dsh import DshAdapter  # noqa: E402
from agent_observatory.canonical import sha256_bytes  # noqa: E402
from agent_observatory.store import ObservatoryStore  # noqa: E402


class DshAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = support.make_temp_dir()
        self.addCleanup(self.tmp.cleanup)
        self.source = support.fixture("dsh")
        with open(self.source, "rb") as handle:
            self.data = handle.read()

    def _parse(self, source_path=None):
        adapter = DshAdapter()
        return adapter.parse(
            self.data,
            context=support.CONTEXT,
            generation=1,
            source_path=source_path or self.source,
            source_sha256=sha256_bytes(self.data),
        )

    def _parse_bytes(self, lines, name="session.v3.jsonl"):
        data = ("\n".join(json.dumps(line) for line in lines) + "\n").encode("utf-8")
        source_path = os.path.join(self.tmp.name, ".dsh", "sessions", "--tmp--", name)
        return DshAdapter().parse(
            data,
            context=support.CONTEXT,
            generation=1,
            source_path=source_path,
            source_sha256=sha256_bytes(data),
        )

    def test_epoch_second_timestamp_is_not_misread_as_1970(self):
        result = self._parse_bytes(
            [
                {
                    "type": "user/message",
                    "seq": 1,
                    "time": 1790000000,  # epoch seconds, not milliseconds
                    "data": {"content": [{"type": "text", "text": "hello"}]},
                }
            ]
        )
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0]["timestamp"][:4], "2026", result.records[0]["timestamp"])

    def test_float_usage_values_are_kept(self):
        result = self._parse_bytes(
            [
                {
                    "type": "assistant/message",
                    "seq": 7,
                    "time": 1789000001000.0,
                    "data": {
                        "message": {
                            "role": "assistant",
                            "id": "m-float",
                            "content": [{"type": "text", "text": "fractional tokens"}],
                        },
                        "usage": {"inputTokens": 100.5, "outputTokens": 2.25},
                    },
                }
            ]
        )
        record = next(record for record in result.records if record.get("usage"))
        self.assertEqual(record["usage"]["input_tokens"], 100.5)
        self.assertEqual(record["usage"]["output_tokens"], 2.25)

    def test_usage_without_an_emitted_record_is_flagged(self):
        result = self._parse_bytes(
            [
                {
                    "type": "assistant/message",
                    # No seq => no assistant_message anchor for a content-less record.
                    "time": 1789000001000,
                    "data": {
                        "message": {
                            "role": "assistant",
                            "id": "m-reasoning",
                            "content": [{"type": "reasoning", "text": "private"}],
                        },
                        "usage": {"inputTokens": 5, "outputTokens": 1},
                    },
                }
            ]
        )
        self.assertEqual(result.skipped.get("usage_dropped"), 1)
        self.assertFalse(any(record.get("usage") for record in result.records))

    def test_unpaired_tool_result_is_flagged(self):
        result = self._parse_bytes(
            [
                {
                    "type": "tool/result",
                    "seq": 9,
                    "time": 1789000003001,
                    "data": {
                        "message": {
                            "source": {"kind": "tool", "callId": "call-orphan"},
                            "content": [
                                {
                                    "type": "tool-result",
                                    "toolCallId": "call-orphan",
                                    "content": [{"type": "text", "text": "orphan output"}],
                                }
                            ],
                        }
                    },
                }
            ]
        )
        self.assertEqual(result.skipped.get("tool_result_unpaired"), 1)
        record = next(record for record in result.records if record["kind"] == "tool_result")
        self.assertIsNone(record["duration_ms"])

    def test_paired_tool_result_is_not_flagged(self):
        result = self._parse()
        self.assertIsNone(result.skipped.get("tool_result_unpaired"))

    def test_detect_requires_content_signature_for_uncompressed(self):
        adapter = DshAdapter()
        session_dir = os.path.join(self.tmp.name, ".dsh", "sessions", "--tmp--", "session-x")
        os.makedirs(session_dir, exist_ok=True)
        transcript = os.path.join(session_dir, "session.v3.jsonl")
        with open(transcript, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "session", "version": 3, "id": "s"}) + "\n")
        self.assertTrue(adapter.detect(transcript))

        notes = os.path.join(session_dir, "notes.jsonl")
        with open(notes, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"note": "not a dsh transcript"}) + "\n")
        self.assertFalse(adapter.detect(notes))

    def test_detect_rejects_non_jsonl_under_dsh_layout(self):
        path = os.path.join(self.tmp.name, ".dsh", "sessions", "--tmp--", "README.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("just notes")
        self.assertFalse(DshAdapter().detect(path))

    def test_uncompressed_session_round_trips_through_read_source(self):
        session_dir = os.path.join(self.tmp.name, ".dsh", "sessions", "--tmp--", "session-u")
        os.makedirs(session_dir, exist_ok=True)
        path = os.path.join(session_dir, "session.v3.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "session", "version": 3, "id": "session-dsh-u"}) + "\n")
            handle.write(
                json.dumps(
                    {
                        "type": "assistant/message",
                        "seq": 1,
                        "time": 1790000000,  # epoch seconds
                        "data": {
                            "message": {
                                "role": "assistant",
                                "id": "m-u",
                                "content": [{"type": "text", "text": "uncompressed"}],
                            },
                            "usage": {"inputTokens": 100.5},
                        },
                    }
                )
                + "\n"
            )
        # Provider is auto-detected from the content signature, then read as-is.
        result = read_source(path, context=support.CONTEXT)
        self.assertEqual(result.session_id, "session-dsh-u")
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0]["timestamp"][:4], "2026")
        self.assertEqual(result.records[0]["usage"]["input_tokens"], 100.5)

    def test_detects_dsh_layout(self):
        adapter = adapter_for_path(
            "/tmp/.dsh/sessions/--tmp--/session-x/session.v3.jsonl.zstd"
        )
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.provider, "dsh")

    def test_session_model_and_usage(self):
        result = self._parse()
        self.assertEqual(result.session_id, "session-dsh-1")
        coverage = result.coverage()
        self.assertEqual(coverage["usage_events"], 3)
        messages = [record for record in result.records if record["kind"] == "message"]
        first = next(record for record in messages if record.get("usage"))
        self.assertEqual(first["model"], "test-store/test-model")
        self.assertEqual(first["usage"]["input_tokens"], 100)
        self.assertEqual(first["usage"]["cache_read_tokens"], 40)

    def test_tool_call_and_result_pair_with_duration(self):
        result = self._parse()
        call = next(record for record in result.records if record["kind"] == "tool_call")
        self.assertEqual(call["tool_call_id"], "call_dsh_1")
        self.assertEqual(call["tool_name"], "bash")
        self.assertEqual(call["command"], "gc hook --claim")
        result_event = next(record for record in result.records if record["kind"] == "tool_result")
        self.assertEqual(result_event["tool_call_id"], "call_dsh_1")
        self.assertEqual(result_event["duration_ms"], 2000)

    def test_reasoning_and_assistant_tool_call_copy_are_skipped(self):
        result = self._parse()
        joined = " ".join(record.get("text") or "" for record in result.records)
        self.assertNotIn("private reasoning", joined)
        self.assertNotIn("only reasoning here", joined)
        self.assertGreaterEqual(result.skipped.get("reasoning", 0), 2)
        self.assertGreaterEqual(result.skipped.get("assistant_tool_call_copy", 0), 1)

    def test_secrets_are_redacted(self):
        result = self._parse()
        joined = " ".join(record.get("text") or "" for record in result.records)
        self.assertNotIn("supersecrettokenvalue", joined)
        self.assertIn("[REDACTED]", joined)

    def test_titles_preserve_revisions(self):
        result = self._parse()
        self.assertEqual([revision.title for revision in result.title_revisions], ["First dsh title", "Second dsh title"])
        self.assertTrue(all(revision.observed_timestamp for revision in result.title_revisions))

    @unittest.skipUnless(support.zstd_available(), "zstd binary is not available")
    def test_compressed_source_round_trips(self):
        compressed = support.dsh_source(self.tmp.name)
        result = read_source(compressed, provider="dsh", context=support.CONTEXT)
        plaintext = self._parse()
        self.assertEqual(len(result.records), len(plaintext.records))
        self.assertEqual(result.session_id, "session-dsh-1")
        self.assertEqual(result.source_sha256, sha256_bytes(self.data))

    @unittest.skipUnless(support.zstd_available(), "zstd binary is not available")
    def test_compressed_replay_is_idempotent(self):
        compressed = support.dsh_source(self.tmp.name)
        result = read_source(compressed, provider="dsh", context=support.CONTEXT)
        db = os.path.join(self.tmp.name, "proj.db")
        first = support.import_records(db, result.records, self.tmp.name, name="first.jsonl")
        self.assertEqual(first.inserted, len(result.records))
        second = support.import_records(db, result.records, self.tmp.name, name="second.jsonl")
        self.assertEqual(second.inserted, 0)
        with ObservatoryStore(db) as store:
            usage_rows = store.conn.execute("SELECT COUNT(*) FROM event_usage").fetchone()[0]
            self.assertEqual(usage_rows, 3)

    def test_missing_zstd_support_is_a_clear_error(self):
        with mock.patch.object(dsh_module.shutil, "which", return_value=None):
            with mock.patch.dict(sys.modules, {"zstandard": None}):
                with self.assertRaises(AdapterError) as caught:
                    DshAdapter().decompress(b"whatever", "x.zstd")
        self.assertIn("zstd", str(caught.exception))

    @unittest.skipUnless(support.zstd_available(), "zstd binary is not available")
    def test_corrupt_compressed_source_is_a_clear_error(self):
        path = os.path.join(self.tmp.name, "session.v3.jsonl.zstd")
        with open(path, "wb") as handle:
            handle.write(b"this is not a zstd stream")
        with self.assertRaises(AdapterError):
            read_source(path, provider="dsh", context=support.CONTEXT)


if __name__ == "__main__":
    unittest.main()
