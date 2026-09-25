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


if __name__ == "__main__":
    unittest.main()
