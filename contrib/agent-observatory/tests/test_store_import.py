"""Tests for the schema-versioned, transactional SQLite projection."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest

try:
    from . import support
except ImportError:  # pragma: no cover
    import support

from agent_observatory.errors import ContractError, ImportConflictError, SchemaVersionError
from agent_observatory.store import ObservatoryStore


class StoreImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "projection.db")

    def _write(self, name, records):
        return support.write_jsonl(os.path.join(self.tmp.name, name), records)

    def test_repeated_file_is_idempotent_and_does_not_duplicate_events(self):
        path = self._write("a.jsonl", [support.make_record(event_id="e1"), support.make_record(event_id="e2")])
        with ObservatoryStore(self.db_path) as store:
            first = store.import_jsonl(path)
            self.assertEqual(first.inserted, 2)
            second = store.import_jsonl(path)
            self.assertTrue(second.skipped_identical_file)
            self.assertEqual(store.event_count(), 2)

    def test_appended_imports_add_new_events_without_duplicating_old(self):
        path = self._write("a.jsonl", [support.make_record(event_id="e1")])
        with ObservatoryStore(self.db_path) as store:
            store.import_jsonl(path)
            support.write_jsonl(path, [support.make_record(event_id="e1"), support.make_record(event_id="e2")])
            result = store.import_jsonl(path)
            self.assertEqual(result.inserted, 1)
            self.assertEqual(result.duplicates, 1)
            self.assertEqual(store.event_count(), 2)

    def test_same_events_from_a_different_path_are_deduplicated(self):
        first = self._write("a.jsonl", [support.make_record(event_id="e1")])
        second = self._write("b.jsonl", [support.make_record(event_id="e1")])
        with ObservatoryStore(self.db_path) as store:
            store.import_jsonl(first)
            result = store.import_jsonl(second)
            self.assertEqual(result.inserted, 0)
            self.assertEqual(result.duplicates, 1)
            self.assertEqual(store.event_count(), 1)

    def test_same_session_different_provider_and_host_are_distinct_events(self):
        records = [
            support.make_record(event_id="e1", provider="codex", host_id="h1"),
            support.make_record(event_id="e1", provider="claude", host_id="h1"),
            support.make_record(event_id="e1", provider="codex", host_id="h2"),
        ]
        path = self._write("providers.jsonl", records)
        with ObservatoryStore(self.db_path) as store:
            store.import_jsonl(path)
            self.assertEqual(store.event_count(), 3)

    def test_conflicting_event_identity_aborts_the_whole_file(self):
        path = self._write(
            "conflict.jsonl",
            [
                support.make_record(event_id="e1", command="ls"),
                support.make_record(event_id="e2", command="pwd"),
                support.make_record(event_id="e1", command="rm -rf /"),  # conflict
            ],
        )
        with ObservatoryStore(self.db_path) as store:
            with self.assertRaises(ImportConflictError):
                store.import_jsonl(path)
            self.assertEqual(store.event_count(), 0)

    def test_conflict_across_files_preserves_earlier_commit_and_rolls_back_file(self):
        first = self._write("first.jsonl", [support.make_record(event_id="e1", command="ls")])
        second = self._write(
            "second.jsonl",
            [
                support.make_record(event_id="e9", command="pwd"),
                support.make_record(event_id="e1", command="rm -rf /"),
            ],
        )
        with ObservatoryStore(self.db_path) as store:
            store.import_jsonl(first)
            with self.assertRaises(ImportConflictError):
                store.import_jsonl(second)
            self.assertEqual(store.event_count(), 1)
            self.assertIsNone(store.get_event(("city-a", "host-a", "codex", "session-1", "e9")))

    def test_truncated_input_reports_line_and_commits_nothing(self):
        path = os.path.join(self.tmp.name, "truncated.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(support.make_record(event_id="e1")) + "\n")
            handle.write('{"schema_version": "1.0", "city_id": "city-a"')
        with ObservatoryStore(self.db_path) as store:
            with self.assertRaises(ContractError) as caught:
                store.import_jsonl(path)
            self.assertIn(":2", str(caught.exception))
            self.assertEqual(store.event_count(), 0)

    def test_provenance_source_file_hash_and_line_are_preserved(self):
        path = self._write("prov.jsonl", [support.make_record(event_id="e1"), support.make_record(event_id="e2")])
        with ObservatoryStore(self.db_path) as store:
            store.import_jsonl(path)
            event = store.get_event(("city-a", "host-a", "codex", "session-1", "e2"))
            self.assertEqual(event["source_path"], path)
            self.assertEqual(event["source_line"], 2)
            self.assertEqual(len(event["source_sha256"]), 64)

    def test_unknown_optional_data_stays_null(self):
        path = self._write("nulls.jsonl", [support.make_record(event_id="e1")])
        with ObservatoryStore(self.db_path) as store:
            store.import_jsonl(path)
            event = store.get_event(("city-a", "host-a", "codex", "session-1", "e1"))
            for column in ("title", "text", "tool_name", "exit_code", "duration_ms", "model"):
                self.assertIsNone(event[column], column)

    def test_usage_missing_versus_zero_is_distinguishable(self):
        records = [
            support.make_record(event_id="e1"),
            support.make_record(event_id="e2", usage={"input_tokens": 0}),
        ]
        path = self._write("usage.jsonl", records)
        with ObservatoryStore(self.db_path) as store:
            store.import_jsonl(path)
            self.assertIsNone(store.get_usage(("city-a", "host-a", "codex", "session-1", "e1")))
            usage = store.get_usage(("city-a", "host-a", "codex", "session-1", "e2"))
            self.assertEqual(usage["input_tokens"], 0)
            self.assertIsNone(usage["output_tokens"])

    def test_schema_version_is_recorded_and_reopen_succeeds(self):
        with ObservatoryStore(self.db_path) as store:
            self.assertEqual(store.schema_version(), ObservatoryStore.SCHEMA_VERSION)
        with ObservatoryStore(self.db_path) as reopened:
            self.assertEqual(reopened.schema_version(), ObservatoryStore.SCHEMA_VERSION)

    def test_events_are_ordered_chronologically_across_offsets_and_fractions(self):
        path = self._write(
            "time.jsonl",
            [
                # Lexically this ".5Z" string sorts before the bare "Z" string,
                # but it is later in time.
                support.make_record(event_id="e1", timestamp="2026-09-21T10:00:00.5Z"),
                support.make_record(event_id="e2", timestamp="2026-09-21T10:00:00Z"),
                # Lexically this "09:" string sorts first, but -05:00 makes it
                # 14:00 UTC, the latest of the three.
                support.make_record(event_id="e3", timestamp="2026-09-21T09:00:00-05:00"),
            ],
        )
        with ObservatoryStore(self.db_path) as store:
            store.import_jsonl(path)
            ordered = store.session_events(("city-a", "host-a", "codex", "session-1"))
            self.assertEqual([event["event_id"] for event in ordered], ["e2", "e1", "e3"])
            self.assertEqual([event["event_id"] for event in store.iter_events()], ["e2", "e1", "e3"])

    def test_raw_timestamp_provenance_is_stored(self):
        path = self._write(
            "raw.jsonl",
            [support.make_record(event_id="e1", timestamp="2026-09-21T12:00:00.5+02:00")],
        )
        with ObservatoryStore(self.db_path) as store:
            store.import_jsonl(path)
            event = store.get_event(("city-a", "host-a", "codex", "session-1", "e1"))
            self.assertEqual(event["observed_timestamp"], "2026-09-21T12:00:00.5+02:00")
            self.assertEqual(event["timestamp"], "2026-09-21T10:00:00.500000Z")

    def test_unknown_future_schema_version_is_rejected(self):
        with ObservatoryStore(self.db_path):
            pass
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA user_version = 999")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(SchemaVersionError):
            ObservatoryStore(self.db_path)


if __name__ == "__main__":
    unittest.main()
