"""Framework-payload stripping for transcript text (task provenance filter)."""

from __future__ import annotations

import unittest

try:
    from . import support  # noqa: F401  (puts the package root on sys.path)
except ImportError:  # pragma: no cover
    import support  # noqa: F401

from agent_observatory.framework import (
    FRAMEWORK_FILTER_VERSION,
    is_framework_text,
    strip_framework_text,
)


class FrameworkFilterTests(unittest.TestCase):
    def test_version_is_recorded(self):
        self.assertRegex(FRAMEWORK_FILTER_VERSION, r"^\d+\.\d+\.\d+$")

    def test_city_role_prompt_is_dropped(self):
        prompt = (
            "[city] gateway-llm/gc.implementation-worker-1 • 2026-09-17T06:35:03\n\n"
            "# GC Role Worker\n\nYou are `gateway-llm/...`\n\n## Startup Claim Protocol\n"
            "run gc hook --claim\n"
        )
        self.assertTrue(is_framework_text(prompt))
        self.assertEqual(strip_framework_text(prompt), "")

    def test_runtime_context_is_dropped(self):
        prompt = (
            "Current runtime context. This snapshot supersedes earlier runtime-context snapshots.\n\n"
            "Current DSH file policy: workspace-write."
        )
        self.assertTrue(is_framework_text(prompt))
        self.assertEqual(strip_framework_text(prompt), "")

    def test_skill_directory_is_dropped(self):
        prompt = "Base directory for this skill: /home/x/skills/bmad-review\n\n# Review"
        self.assertTrue(is_framework_text(prompt))
        self.assertEqual(strip_framework_text(prompt), "")

    def test_embedded_reminder_is_removed_and_real_text_kept(self):
        message = (
            "Please fix the flaky scheduler test.\n"
            "<system-reminder>\nYou have a deferred reminder.\n</system-reminder>\n"
            "The failure is in TestScheduler."
        )
        stripped = strip_framework_text(message)
        self.assertIn("Please fix the flaky scheduler test.", stripped)
        self.assertIn("The failure is in TestScheduler.", stripped)
        self.assertNotIn("system-reminder", stripped)
        self.assertNotIn("deferred reminder", stripped)
        self.assertFalse(is_framework_text(message))

    def test_available_skills_block_is_removed(self):
        message = (
            "Add the export button.\n"
            "<available_skills>\n- `analogy`: forced analogy\n</available_skills>\n"
        )
        stripped = strip_framework_text(message)
        self.assertIn("Add the export button.", stripped)
        self.assertNotIn("available_skills", stripped)
        self.assertNotIn("forced analogy", stripped)

    def test_reminder_only_message_is_empty(self):
        message = (
            "<system-reminder>\nYou have a deferred reminder that was queued until a safe boundary:\n"
            "- [session] check for assigned work\n</system-reminder>"
        )
        self.assertTrue(is_framework_text(message))
        self.assertEqual(strip_framework_text(message), "")

    def test_real_prompt_is_kept(self):
        self.assertEqual(strip_framework_text("Please fix the bug"), "Please fix the bug")
        self.assertFalse(is_framework_text("Please fix the bug"))

    # F1: a leading ``[city]`` line or a bare harness phrase is not proof that the
    # message is a harness payload. Real tasks must survive.
    def test_city_header_before_a_real_task_keeps_the_task(self):
        text = (
            "[city] gateway-llm/dsh-glm-flash • 2026-09-22T16:45:29\n\n"
            "Why did my workflow stall this morning? Please investigate the router config."
        )
        self.assertFalse(is_framework_text(text))
        stripped = strip_framework_text(text)
        self.assertIn("Why did my workflow stall this morning?", stripped)
        self.assertIn("investigate the router config", stripped)
        self.assertNotIn("[city]", stripped)

    def test_command_task_mentioning_the_claim_protocol_is_kept(self):
        text = "run gc hook --claim and report what it returns"
        self.assertFalse(is_framework_text(text))
        self.assertEqual(strip_framework_text(text), text)

    def test_task_mentioning_the_protocol_name_is_kept(self):
        text = "Explain the Startup Claim Protocol to a new contributor"
        self.assertFalse(is_framework_text(text))
        self.assertEqual(strip_framework_text(text), text)

    # F2: the injected payload is also quoted/indented (``> [city] ...``); the
    # body markers still make it a payload and it must be dropped whole.
    def test_quoted_city_role_payload_is_dropped(self):
        text = (
            "> [city] gateway-llm/gc.worker-1 • 2026-09-22T16:45:29\n\n"
            "# GC Role Worker\n\nYou are a role worker. run gc hook --claim"
        )
        self.assertTrue(is_framework_text(text))
        self.assertEqual(strip_framework_text(text), "")

    # F6: ``is_framework_text`` and ``strip_framework_text`` must reach the same
    # verdict regardless of the message size.
    def test_filter_is_consistent_above_the_size_cap(self):
        text = (
            "# GC Role Worker\n<system-reminder>"
            + ("x" * (33 * 1024))
            + "</system-reminder>"
        )
        self.assertTrue(is_framework_text(text))
        self.assertEqual(strip_framework_text(text), "")

    def test_filter_agrees_on_a_large_real_task(self):
        text = (
            "[city] gateway-llm/dsh-glm-flash • 2026-09-22T16:45:29\n\n"
            "Please investigate the router config.\n"
            + ("real task detail " * 3000)
        )
        self.assertFalse(is_framework_text(text))
        stripped = strip_framework_text(text)
        self.assertIn("Please investigate the router config.", stripped)
        self.assertIn("real task detail", stripped)


if __name__ == "__main__":
    unittest.main()
