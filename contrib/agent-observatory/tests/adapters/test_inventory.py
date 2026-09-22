"""Tests for source discovery, the coverage manifest, and generation tracking."""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adapter_support as support  # noqa: E402

from agent_observatory.errors import ObservatoryError  # noqa: E402
from agent_observatory.inventory import (  # noqa: E402
    SourceRoot,
    build_manifest,
    discover_sources,
    manifest_json,
)


class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = support.make_temp_dir()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.claude_path = support.claude_source(self.root)
        self.codex_path = support.codex_source(self.root)

    def _manifest(self, **kwargs):
        return build_manifest(
            [SourceRoot(self.root)],
            city_id="city-a",
            host_id="host-a",
            **kwargs,
        )

    def _entry_for(self, manifest, provider):
        return next(entry for entry in manifest["sources"] if entry["provider"] == provider)

    def test_manifest_covers_discovered_sources(self):
        manifest = self._manifest()
        providers = {entry["provider"] for entry in manifest["sources"]}
        self.assertIn("claude", providers)
        self.assertIn("codex", providers)
        self.assertEqual(manifest["city_id"], "city-a")
        self.assertEqual(manifest["host_id"], "host-a")
        self.assertEqual(manifest["totals"]["sources"], len(manifest["sources"]))
        self.assertGreater(manifest["totals"]["events"], 0)

        entry = self._entry_for(manifest, "claude")
        for key in (
            "source_id",
            "provider",
            "root",
            "realpath",
            "size_bytes",
            "mtime",
            "content_generation",
            "generation",
            "discovery_status",
            "adapter_version",
            "checkpoint",
            "coverage",
            "supersedes",
        ):
            self.assertIn(key, entry)
        self.assertEqual(entry["generation"], 1)
        self.assertEqual(entry["discovery_status"], "new")
        self.assertEqual(entry["checkpoint"]["records"], entry["coverage"]["records_emitted"])
        self.assertEqual(entry["adapter_version"], "claude/1.0.0")

    def test_unsupported_providers_are_reported_with_reasons(self):
        manifest = self._manifest()
        unsupported = {entry["provider"]: entry for entry in manifest["unsupported"]}
        self.assertIn("opencode", unsupported)
        self.assertIn("pi", unsupported)
        self.assertIn("remote", unsupported)
        for entry in unsupported.values():
            self.assertTrue(entry["reason"])

    def test_unchanged_source_keeps_generation(self):
        first = self._manifest()
        second = self._manifest(previous=first)
        entry = self._entry_for(second, "claude")
        self.assertEqual(entry["generation"], 1)
        self.assertEqual(entry["discovery_status"], "unchanged")
        self.assertIsNone(entry["supersedes"])

    def test_appended_source_keeps_generation(self):
        first = self._manifest(previous=None)
        with open(self.claude_path, "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "u-append",
                        "sessionId": "claude-sess-1",
                        "session_id": "claude-parent-0",
                        "timestamp": "2026-09-21T10:00:09.000Z",
                        "message": {
                            "id": "msg-append",
                            "role": "assistant",
                            "model": "claude-test-1",
                            "content": [{"type": "text", "text": "appended"}],
                            "usage": {"input_tokens": 1, "output_tokens": 1},
                        },
                    }
                )
                + "\n"
            )
        second = self._manifest(previous=first)
        entry = self._entry_for(second, "claude")
        self.assertEqual(entry["generation"], 1)
        self.assertEqual(entry["discovery_status"], "appended")
        self.assertIsNone(entry["supersedes"])
        first_entry = self._entry_for(first, "claude")
        self.assertEqual(entry["coverage"]["records_emitted"], first_entry["coverage"]["records_emitted"] + 1)

    def test_rewritten_source_increments_generation_and_supersedes(self):
        first = self._manifest()
        original_generation = self._entry_for(first, "claude")["content_generation"]
        # Replace the whole file with different (non-prefix) content.
        with open(self.claude_path, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "u-rewrite",
                        "sessionId": "claude-sess-1",
                        "timestamp": "2026-09-21T10:01:00.000Z",
                        "message": {"role": "user", "content": [{"type": "text", "text": "rewritten"}]},
                    }
                )
                + "\n"
            )
        second = self._manifest(previous=first)
        entry = self._entry_for(second, "claude")
        self.assertEqual(entry["generation"], 2)
        self.assertEqual(entry["discovery_status"], "rewritten")
        self.assertEqual(entry["supersedes"], original_generation)

    def test_partial_trailing_line_is_visible_in_the_manifest(self):
        with open(self.claude_path, "a", encoding="utf-8") as handle:
            handle.write('{"type":"assistant","uuid":"u-cut","message":')
        manifest = self._manifest()
        entry = self._entry_for(manifest, "claude")
        self.assertTrue(entry["partial_trailing_line"])
        self.assertTrue(entry["coverage"]["errors"])
        self.assertGreaterEqual(manifest["totals"]["partial_trailing_lines"], 1)

    def test_unreadable_source_is_reported_not_silently_dropped(self):
        bad = os.path.join(self.root, ".claude", "projects", "-tmp", "bad.jsonl")
        os.makedirs(os.path.dirname(bad), exist_ok=True)
        with open(bad, "wb") as handle:
            handle.write(b"\xff\xfe not utf-8")
        manifest = self._manifest()
        entry = next(item for item in manifest["sources"] if item["path"].endswith("bad.jsonl"))
        self.assertEqual(entry["discovery_status"], "error")
        self.assertTrue(entry["error_reason"])
        self.assertGreaterEqual(manifest["totals"]["unreadable_sources"], 1)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "chmod 000 is not enforced for root")
    def test_unreadable_subdirectory_is_reported_with_an_error_reason(self):
        locked = os.path.join(self.root, "locked-tree")
        support.claude_source(locked)
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o700)

        manifest = self._manifest()
        entry = next(item for item in manifest["sources"] if item["realpath"] == os.path.realpath(locked))
        self.assertEqual(entry["discovery_status"], "unreadable")
        self.assertTrue(entry["error_reason"])
        self.assertIsNone(entry["coverage"])
        self.assertGreaterEqual(manifest["totals"]["unreadable_sources"], 1)
        # The readable transcript outside the locked tree is still covered.
        self.assertGreaterEqual(manifest["totals"]["events"], 1)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "chmod 000 is not enforced for root")
    def test_unreadable_root_is_reported_with_an_error_reason(self):
        locked_root = os.path.join(self.tmp.name, "locked-root")
        support.claude_source(locked_root)
        os.chmod(locked_root, 0)
        self.addCleanup(os.chmod, locked_root, 0o700)

        manifest = build_manifest([SourceRoot(locked_root)], city_id="city-a", host_id="host-a")
        self.assertEqual(len(manifest["sources"]), 1)
        entry = manifest["sources"][0]
        self.assertEqual(entry["realpath"], os.path.realpath(locked_root))
        self.assertEqual(entry["discovery_status"], "unreadable")
        self.assertTrue(entry["error_reason"])
        self.assertEqual(manifest["totals"]["unreadable_sources"], 1)

    def test_invalid_record_is_isolated_not_fatal(self):
        naive = os.path.join(self.root, ".claude", "projects", "-tmp-naive", "naive.jsonl")
        os.makedirs(os.path.dirname(naive), exist_ok=True)
        good = {
            "type": "user",
            "uuid": "u-good",
            "sessionId": "naive-sess",
            "timestamp": "2026-09-21T10:00:00.000Z",
            "message": {"role": "user", "content": [{"type": "text", "text": "good"}]},
        }
        naive_timestamp = {
            "type": "user",
            "uuid": "u-naive",
            "sessionId": "naive-sess",
            "timestamp": "2026-09-22 02:00:00",
            "message": {"role": "user", "content": [{"type": "text", "text": "bad"}]},
        }
        with open(naive, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(good) + "\n")
            handle.write(json.dumps(naive_timestamp) + "\n")

        manifest = self._manifest()
        entry = next(item for item in manifest["sources"] if item["path"].endswith("naive.jsonl"))
        self.assertEqual(entry["discovery_status"], "new")
        self.assertIsNone(entry["error_reason"])
        self.assertEqual(entry["checkpoint"]["records"], 1)
        self.assertEqual(entry["coverage"]["records_emitted"], 1)
        self.assertEqual(entry["coverage"]["skipped"].get("invalid_record"), 1)

    def test_explicit_unsupported_provider_root_is_manifested(self):
        manifest = build_manifest([SourceRoot(self.root, provider="opencode")], city_id="c", host_id="h")
        self.assertEqual(manifest["sources"], [])
        self.assertTrue(any(entry["provider"] == "opencode" and entry.get("root") for entry in manifest["unsupported"]))

    def test_unsupported_artifacts_are_detected_under_a_root(self):
        artifact_root = os.path.join(self.tmp.name, "opencode-store")
        os.makedirs(artifact_root, exist_ok=True)
        with open(os.path.join(artifact_root, "opencode.db"), "wb") as handle:
            handle.write(b"sqlite")
        sources, unsupported = discover_sources([SourceRoot(artifact_root)])
        self.assertEqual(sources, [])
        self.assertTrue(any(entry["provider"] == "opencode" for entry in unsupported))

    def test_missing_root_is_an_error(self):
        with self.assertRaises(ObservatoryError):
            build_manifest([SourceRoot("/definitely/not/here")], city_id="c", host_id="h")

    def test_no_roots_discovers_nothing(self):
        sources, unsupported = discover_sources([])
        self.assertEqual(sources, [])
        # The three static unsupported providers remain listed in the manifest.
        manifest = build_manifest([], city_id="c", host_id="h")
        self.assertEqual(manifest["sources"], [])
        self.assertEqual(len(manifest["unsupported"]), 3)

    def test_manifest_serialization_is_deterministic(self):
        first = manifest_json(self._manifest())
        second = manifest_json(self._manifest())
        self.assertEqual(first, second)

    @unittest.skipUnless(support.zstd_available(), "zstd binary is not available")
    def test_manifest_includes_compressed_dsh_sources(self):
        support.dsh_source(self.root)
        manifest = self._manifest()
        entry = self._entry_for(manifest, "dsh")
        self.assertEqual(entry["provider"], "dsh")
        self.assertEqual(entry["generation"], 1)
        self.assertGreater(entry["coverage"]["usage_events"], 0)


if __name__ == "__main__":
    unittest.main()
