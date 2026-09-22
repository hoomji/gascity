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

    def test_email_addresses_are_redacted(self):
        for address in ("alice@example.com", "bob.smith@corp.co.uk", "a+tag@sub.domain.io"):
            redacted = redact_text(f"contact {address} now")
            self.assertNotIn(address, redacted)
            self.assertIn("[REDACTED]", redacted)

    def test_email_hostname_may_contain_underscore(self):
        # F5: ``_`` is legal in a hostname, so this is still a personal address.
        redacted = redact_text("contact alice@host_name.com now")
        self.assertNotIn("alice@host_name.com", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_url_path_segments_are_not_home_directories(self):
        # F4: a doc URL path is not a user home directory.
        for value in (
            "https://docs.example.com/Users/guide",
            "https://docs.example.com/home/guide",
            "https://docs.example.com:8443/root/guide",
        ):
            self.assertEqual(redact_text(value), value)
            self.assertNotIn("[REDACTED]", redact_text(value))

    def test_home_paths_at_value_starts_are_still_redacted(self):
        for value in (
            "/home/alice/secret",
            "see /home/alice/secret now",
            "path=/home/alice/secret",
            "path:/home/alice/secret",
            '"/home/alice/secret"',
        ):
            self.assertIn("[REDACTED]", redact_text(value), value)
            self.assertNotIn("alice", redact_text(value), value)

    def test_home_directory_paths_are_redacted(self):
        for path in ("/home/alice/secret", "/Users/alice/proj", r"C:\Users\alice\secret"):
            redacted = redact_text(f"see {path} now")
            self.assertNotIn("alice", redacted)
            self.assertIn("[REDACTED]", redacted)
        self.assertNotIn("/root", redact_text("see /root/.ssh/id_rsa now"))
        # A path that merely starts with the same letters is not a home dir.
        self.assertEqual(redact_text("/rooted/not-home"), "/rooted/not-home")

    def test_redaction_is_idempotent(self):
        samples = (
            "Authorization: Bearer sk-ABCDEFGHIJKLMNOP",
            "Authorization: Bearer abcdefgh12345",
            "api_key=supersecretvalue",
            'refresh_token="abcdef123456"',
            r'{\"password\": \"hunter2\"}',
            "contact alice@example.com now",
            "see /home/alice/secret and /root/.ssh/id_rsa",
            r"C:\Users\alice\secret",
            "plain ordinary text with total_tokens=42",
        )
        for value in samples:
            once = redact_text(value)
            self.assertEqual(redact_text(once), once, value)

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
