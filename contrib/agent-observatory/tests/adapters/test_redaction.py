"""Tests for credential redaction and bounded tool output."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adapter_support  # noqa: E402,F401  (bootstrap)

from agent_observatory.adapters.redaction import (  # noqa: E402
    MAX_TOOL_OUTPUT_BYTES,
    elide_large_text,
    redact_and_bound,
    redact_text,
)


class RedactionTest(unittest.TestCase):
    def test_secret_assignments_are_redacted(self):
        for value in (
            "api_key=supersecretvalue",
            "API-KEY: anothersecret",
            "password=hunter2",
            "client_secret: shhhh",
            "token=abcdef123456",
            'refresh_token="abcdef123456"',
        ):
            self.assertIn("[REDACTED]", redact_text(value))
            self.assertNotIn("supersecretvalue", redact_text(value))
            self.assertNotIn("anothersecret", redact_text(value))
            self.assertNotIn("hunter2", redact_text(value))
            self.assertNotIn("shhhh", redact_text(value))

    def test_bearer_header_does_not_leak_the_token(self):
        redacted = redact_text("Authorization: Bearer abcdefgh12345")
        self.assertNotIn("abcdefgh12345", redacted)

    def test_escaped_json_assignment_is_redacted(self):
        escaped = r'{\"password\": \"hunter2\"}'
        redacted = redact_text(escaped)
        self.assertNotIn("hunter2", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_plain_assignment_stays_redacted(self):
        redacted = redact_text("password=hunter2")
        self.assertNotIn("hunter2", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_token_shaped_assignment_value_redacts_once(self):
        redacted = redact_text("api_key=sk-ABCDEFGHIJKLMNOP")
        self.assertNotIn("sk-ABCDEFGHIJKLMNOP", redacted)
        self.assertEqual(redacted.count("[REDACTED]"), 1)

    def test_bearer_header_redacts_once(self):
        redacted = redact_text("Authorization: Bearer sk-ABCDEFGHIJKLMNOP")
        self.assertNotIn("sk-ABCDEFGHIJKLMNOP", redacted)
        self.assertEqual(redacted.count("[REDACTED]"), 1)

    def test_known_token_shapes_are_redacted(self):
        for token in ("sk-ABCDEFGHIJKLMNOP", "ghp_ABCDEFGHIJKLMNOPQRST", "AKIAIOSFODNN7EXAMPLE"):
            self.assertIn("[REDACTED]", redact_text(f"here is {token} done"))

    def test_ordinary_text_is_untouched(self):
        value = "total_tokens=42 and the api_key_hash is fine"
        self.assertEqual(redact_text(value), value)

    def test_small_tool_output_is_redacted_but_kept(self):
        value = "api_key=supersecretvalue " + "x" * 100
        result = elide_large_text(value)
        self.assertNotIn("supersecretvalue", result)
        self.assertIn("x", result)

    def test_large_tool_output_is_replaced_by_digest_and_length(self):
        value = "A" * (MAX_TOOL_OUTPUT_BYTES + 500)
        result = elide_large_text(value)
        self.assertTrue(result.startswith("[elided:"))
        self.assertIn(f"{len(value)} bytes", result)
        self.assertIn("sha256=", result)
        self.assertNotIn(value, result)

    def test_redact_and_bound_marks_truncation(self):
        value = "B" * (MAX_TOOL_OUTPUT_BYTES + 10)
        result = redact_and_bound(value)
        self.assertIn("[truncated:", result)
        self.assertIn("sha256=", result)


if __name__ == "__main__":
    unittest.main()
