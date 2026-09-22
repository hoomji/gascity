"""Tests for the normalized record import contract."""

from __future__ import annotations

import unittest

try:
    from . import support
except ImportError:  # pragma: no cover - depends on discovery invocation
    import support

from agent_observatory.canonical import event_snapshot_hash
from agent_observatory.contract import payload_hash, record_identity, validate_record
from agent_observatory.errors import ContractError


class ContractTest(unittest.TestCase):
    def test_accepts_valid_record_and_fills_missing_optional_fields_with_none(self):
        record = validate_record(support.make_record())
        self.assertEqual(record["title"], None)
        self.assertEqual(record["text"], None)
        self.assertEqual(record["usage"], None)
        self.assertEqual(record["exit_code"], None)

    def test_required_field_missing_is_rejected_with_line_context(self):
        raw = support.make_record()
        del raw["event_id"]
        with self.assertRaises(ContractError) as caught:
            validate_record(raw, "events.jsonl", 7)
        self.assertIn("events.jsonl:7", str(caught.exception))
        self.assertIn("event_id", str(caught.exception))

    def test_wrong_type_is_rejected(self):
        raw = support.make_record(duration_ms="12")
        with self.assertRaises(ContractError):
            validate_record(raw, "events.jsonl", 3)

    def test_bool_is_not_accepted_as_integer(self):
        raw = support.make_record(exit_code=True)
        with self.assertRaises(ContractError):
            validate_record(raw, "events.jsonl", 1)

    def test_unknown_top_level_keys_are_dropped_so_they_stay_null(self):
        raw = support.make_record(secret_token="oops", raw_reasoning="hidden")
        record = validate_record(raw)
        self.assertNotIn("secret_token", record)
        self.assertNotIn("raw_reasoning", record)

    def test_unsupported_schema_version_is_rejected(self):
        raw = support.make_record(schema_version="999")
        with self.assertRaises(ContractError):
            validate_record(raw)

    def test_non_iso_timestamp_is_rejected(self):
        raw = support.make_record(timestamp="yesterday")
        with self.assertRaises(ContractError):
            validate_record(raw)

    def test_timezone_naive_timestamp_is_rejected(self):
        raw = support.make_record(timestamp="2026-09-21T10:00:00")
        with self.assertRaises(ContractError) as caught:
            validate_record(raw)
        self.assertIn("timezone", str(caught.exception).lower())

    def test_timestamp_is_normalized_to_utc_microseconds(self):
        record = validate_record(support.make_record(timestamp="2026-09-21T12:00:00.5+02:00"))
        self.assertEqual(record["timestamp"], "2026-09-21T10:00:00.500000Z")

    def test_observed_timestamp_provenance_is_preserved(self):
        original = "2026-09-21T12:00:00.5+02:00"
        record = validate_record(support.make_record(timestamp=original))
        self.assertEqual(record["observed_timestamp"], original)
        self.assertNotEqual(record["timestamp"], original)

    def test_offset_equivalent_timestamps_hash_identically(self):
        first = validate_record(support.make_record(timestamp="2026-09-21T12:00:00+02:00"))
        second = validate_record(support.make_record(timestamp="2026-09-21T10:00:00Z"))
        self.assertEqual(first["timestamp"], second["timestamp"])
        self.assertEqual(payload_hash(first), payload_hash(second))

    def test_negative_duration_is_rejected(self):
        raw = support.make_record(duration_ms=-1)
        with self.assertRaises(ContractError):
            validate_record(raw)

    def test_usage_missing_versus_zero_is_preserved(self):
        missing = validate_record(support.make_record())
        self.assertIsNone(missing["usage"])

        zeroed = validate_record(support.make_record(usage={"input_tokens": 0}))
        self.assertEqual(zeroed["usage"]["input_tokens"], 0)
        self.assertIsNone(zeroed["usage"]["output_tokens"])

    def test_negative_usage_is_rejected(self):
        raw = support.make_record(usage={"input_tokens": -1})
        with self.assertRaises(ContractError):
            validate_record(raw)

    def test_float_usage_values_are_kept(self):
        raw = support.make_record(usage={"input_tokens": 10.5, "total_tokens": 10.5})
        normalized = validate_record(raw)
        self.assertEqual(normalized["usage"]["input_tokens"], 10.5)
        self.assertEqual(normalized["usage"]["total_tokens"], 10.5)

    def test_negative_float_usage_is_rejected(self):
        raw = support.make_record(usage={"input_tokens": -0.5})
        with self.assertRaises(ContractError):
            validate_record(raw)

    def test_boolean_usage_is_rejected(self):
        raw = support.make_record(usage={"input_tokens": True})
        with self.assertRaises(ContractError):
            validate_record(raw)

    def test_identity_ignores_title_and_text(self):
        first = validate_record(support.make_record(title="alpha", text="one"))
        second = validate_record(support.make_record(title="beta", text="two"))
        self.assertEqual(record_identity(first), record_identity(second))
        self.assertNotEqual(payload_hash(first), payload_hash(second))

    def test_payload_hash_is_stable_for_identical_evidence(self):
        first = validate_record(support.make_record(command="ls", exit_code=0))
        second = validate_record(support.make_record(command="ls", exit_code=0))
        self.assertEqual(payload_hash(first), payload_hash(second))

    def test_payload_hash_excludes_identity_fields(self):
        base = validate_record(support.make_record(event_id="e1"))
        other_identity = validate_record(
            support.make_record(
                city_id="city-b",
                host_id="host-b",
                provider="claude",
                session_id="session-2",
                event_id="e9",
            )
        )
        # Identity is matched separately; it is not part of the content payload.
        self.assertEqual(payload_hash(base), payload_hash(other_identity))
        # Non-identity content still changes the payload hash.
        changed = validate_record(support.make_record(event_id="e1", command="ls"))
        self.assertNotEqual(payload_hash(base), payload_hash(changed))

    def test_event_snapshot_hash_still_covers_identity(self):
        first = validate_record(support.make_record(event_id="e1"))
        second = validate_record(support.make_record(event_id="e2"))
        self.assertEqual(payload_hash(first), payload_hash(second))
        self.assertNotEqual(
            event_snapshot_hash(record_identity(first), payload_hash(first)),
            event_snapshot_hash(record_identity(second), payload_hash(second)),
        )


if __name__ == "__main__":
    unittest.main()
