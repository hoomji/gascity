"""Tests for the compressed dsh session adapter."""

from __future__ import annotations

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
