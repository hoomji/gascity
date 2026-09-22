"""Tests for the shared JSONL splitting helpers in the adapter base module."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adapter_support  # noqa: E402,F401  (bootstrap)

from agent_observatory.adapters.base import AdapterError, iso_from_epoch, split_jsonl  # noqa: E402

BOM = b"\xef\xbb\xbf"


class EpochTimestampTest(unittest.TestCase):
    def test_seconds_and_milliseconds_both_land_in_2026(self):
        # 1_790_000_000 is epoch *seconds*; 1_789_000_000_000 is epoch *ms*.
        # Dividing the seconds value by 1000 again would land in 1970.
        from_seconds = iso_from_epoch(1790000000)
        from_millis = iso_from_epoch(1789000000000)
        self.assertIsNotNone(from_seconds)
        self.assertIsNotNone(from_millis)
        self.assertEqual(from_seconds[:4], "2026", from_seconds)
        self.assertEqual(from_millis[:4], "2026", from_millis)
        self.assertTrue(from_seconds.endswith("Z"), from_seconds)

    def test_non_numeric_and_bool_are_rejected(self):
        for value in (None, "1790000000", True, False, {}, [], object()):
            self.assertIsNone(iso_from_epoch(value), repr(value))


class SplitJsonlTest(unittest.TestCase):
    def test_bom_prefixed_two_valid_lines_parse_without_error(self):
        records, partial, errors = split_jsonl(BOM + b'{"a":1}\n{"b":2}\n', "bom.jsonl")
        self.assertEqual([line for line, _ in records], [1, 2])
        self.assertEqual([obj for _, obj in records], [{"a": 1}, {"b": 2}])
        self.assertFalse(partial)
        self.assertEqual(errors, [])

    def test_bom_prefixed_unterminated_valid_line_is_a_complete_record(self):
        records, partial, errors = split_jsonl(BOM + b'{"a":1}', "bom.jsonl")
        self.assertEqual(records, [(1, {"a": 1})])
        self.assertFalse(partial)
        self.assertEqual(errors, [])

    def test_missing_final_newline_on_parsable_line_is_not_partial(self):
        records, partial, errors = split_jsonl(b'{"a":1}\n{"b":2}', "plain.jsonl")
        self.assertEqual([line for line, _ in records], [1, 2])
        self.assertFalse(partial)
        self.assertEqual(errors, [])

    def test_malformed_final_line_is_still_partial(self):
        records, partial, errors = split_jsonl(b'{"a":1}\n{"b":', "cut.jsonl")
        self.assertEqual(records, [(1, {"a": 1})])
        self.assertTrue(partial)
        self.assertEqual(len(errors), 1)
        self.assertIn("partial trailing line", errors[0])

    def test_malformed_interior_line_is_a_hard_error(self):
        with self.assertRaises(AdapterError):
            split_jsonl(b'{"a":1}\nnot json\n{"b":2}\n', "interior.jsonl")


if __name__ == "__main__":
    unittest.main()
