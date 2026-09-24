"""End-to-end CLI tests for the M7 ``shadow`` command (no network)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

try:
    from . import support
except ImportError:  # pragma: no cover
    import support

from agent_observatory.store import ObservatoryStore

PACKAGE_ROOT = support.PACKAGE_ROOT
FIXTURES = os.path.join(PACKAGE_ROOT, "tests", "fixtures", "policy")


def run_cli(args, cwd=PACKAGE_ROOT):
    env = dict(os.environ)
    env["PYTHONPATH"] = PACKAGE_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "agent_observatory", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


class ShadowCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "projection.db")
        self.catalog = os.path.join(FIXTURES, "catalog.json")
        self.bundle = os.path.join(FIXTURES, "shadow_bundle.json")

    def test_shadow_writes_report_and_persists(self):
        out = os.path.join(self.tmp.name, "shadow.json")
        result = run_cli(
            ["shadow", "--catalog", self.catalog, "--input", self.bundle, "--db", self.db, "--out", out]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(out, encoding="utf-8") as handle:
            report = json.load(handle)
        self.assertEqual(report["kind"], "shadow_policy_recommendations")
        self.assertFalse(report["shadow"]["executes_changes"])
        summary = json.loads(result.stderr)
        self.assertEqual(summary["stored"], len(report["recommendations"]))
        with ObservatoryStore(self.db) as store:
            self.assertEqual(store.recommendation_count(), len(report["recommendations"]))

    def test_shadow_is_idempotent(self):
        first = run_cli(["shadow", "--catalog", self.catalog, "--input", self.bundle, "--db", self.db])
        self.assertEqual(first.returncode, 0, first.stderr)
        second = run_cli(["shadow", "--catalog", self.catalog, "--input", self.bundle, "--db", self.db])
        self.assertEqual(second.returncode, 0, second.stderr)
        summary = json.loads(second.stderr)
        self.assertEqual(summary["stored"], 0)
        self.assertEqual(summary["deduplicated"], summary["recommendations"])

    def test_shadow_without_db_does_not_persist(self):
        result = run_cli(["shadow", "--catalog", self.catalog, "--input", self.bundle])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(os.path.exists(self.db))
        summary = json.loads(result.stderr)
        self.assertIsNone(summary["stored"])

    def test_shadow_reports_disagreements(self):
        result = run_cli(["shadow", "--catalog", self.catalog, "--input", self.bundle])
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stderr)
        self.assertGreaterEqual(summary["disagreements"], 1)
        self.assertFalse(summary["leak_free"])

    def test_missing_catalog_reports_clean_error(self):
        result = run_cli(
            ["shadow", "--catalog", os.path.join(self.tmp.name, "nope.json"), "--input", self.bundle]
        )
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)
        self.assertTrue(result.stderr.strip().startswith("error:"), result.stderr)

    def test_missing_bundle_reports_clean_error(self):
        result = run_cli(
            ["shadow", "--catalog", self.catalog, "--input", os.path.join(self.tmp.name, "nope.json")]
        )
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)
        self.assertTrue(result.stderr.strip().startswith("error:"), result.stderr)

    def test_invalid_catalog_reports_clean_error(self):
        bad = os.path.join(self.tmp.name, "bad-catalog.json")
        payload = {
            "schema_version": "1.0",
            "catalog_version": "x",
            "candidates": [{"candidate_id": "a", "kind": "spaceship"}],
        }
        with open(bad, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        result = run_cli(["shadow", "--catalog", bad, "--input", self.bundle])
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("error:", result.stderr)


if __name__ == "__main__":
    unittest.main()
