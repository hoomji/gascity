"""End-to-end CLI tests for the M5 registry commands (no network)."""

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

PACKAGE_ROOT = support.PACKAGE_ROOT


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


MERGE = "M" * 40
BASE = "B" * 40


class ChangesCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "projection.db")
        self.bundle = os.path.join(self.tmp.name, "changes.json")
        with open(self.bundle, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "schema_version": "1.0",
                    "changes": [
                        {
                            "repo": "gascity",
                            "kind": "pr",
                            "pr": 6,
                            "title": "perf: cache the build",
                            "merge_sha": MERGE,
                            "merged_at": "2026-09-21T10:00:00Z",
                            "changed_paths": ["Makefile"],
                        }
                    ],
                    "commit_graph": {"commits": [{"sha": MERGE, "parents": [BASE]}, {"sha": BASE, "parents": []}]},
                },
                handle,
            )

    def _import_events(self, db, commit_sha):
        fixture = support.write_jsonl(
            os.path.join(self.tmp.name, f"events-{commit_sha[:4]}.jsonl"),
            [
                support.make_record(
                    event_id="e1",
                    repo="gascity",
                    commit_sha=commit_sha,
                    timestamp="2026-09-22T00:00:00Z",
                )
            ],
        )
        result = run_cli(["import-jsonl", "--db", db, fixture])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_changes_sync_is_idempotent(self):
        first = run_cli(["changes-sync", "--db", self.db, "--input", self.bundle])
        self.assertEqual(first.returncode, 0, first.stderr)
        payload = json.loads(first.stdout)
        self.assertEqual(payload["changes_inserted"], 1)
        self.assertEqual(payload["activations_inserted"], 1)

        second = run_cli(["changes-sync", "--db", self.db, "--input", self.bundle])
        self.assertEqual(second.returncode, 0, second.stderr)
        payload = json.loads(second.stdout)
        self.assertEqual(payload["changes_inserted"], 0)
        self.assertEqual(payload["changes_deduplicated"], 1)

    def test_exposure_reports_observed_merge_as_exposed(self):
        self._import_events(self.db, MERGE)
        self.assertEqual(run_cli(["changes-sync", "--db", self.db, "--input", self.bundle]).returncode, 0)
        result = run_cli(["exposure", "--db", self.db])
        self.assertEqual(result.returncode, 0, result.stderr)
        ledger = json.loads(result.stdout)
        self.assertEqual(ledger["screening"]["screened"], 1)
        self.assertEqual(ledger["screening"]["optimization"], 1)
        self.assertEqual(ledger["exposure_totals"]["exposed"], 1)
        entry = ledger["changes"][0]
        self.assertEqual(entry["exposure"]["status"], "exposed")

    def test_pre_merge_worktree_session_is_not_exposed(self):
        self._import_events(self.db, BASE)
        self.assertEqual(run_cli(["changes-sync", "--db", self.db, "--input", self.bundle]).returncode, 0)
        result = run_cli(["exposure", "--db", self.db])
        self.assertEqual(result.returncode, 0, result.stderr)
        ledger = json.loads(result.stdout)
        self.assertEqual(ledger["exposure_totals"]["exposed"], 0)
        self.assertEqual(ledger["exposure_totals"]["unexposed"], 1)

    def test_non_optimization_pr_stays_in_denominator(self):
        bundle = os.path.join(self.tmp.name, "mixed.json")
        with open(bundle, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "schema_version": "1.0",
                    "changes": [
                        {
                            "repo": "gascity",
                            "kind": "pr",
                            "pr": 1,
                            "title": "docs",
                            "labels": ["documentation"],
                            "changed_paths": ["README.md"],
                        },
                        {"repo": "gascity", "kind": "pr", "pr": 2, "changed_paths": ["internal/x.go"]},
                        {
                            "repo": "gascity",
                            "kind": "pr",
                            "pr": 3,
                            "merge_sha": MERGE,
                            "merged_at": "2026-09-21T10:00:00Z",
                            "title": "perf: cache",
                            "changed_paths": ["Makefile"],
                        },
                    ],
                    "commit_graph": {"commits": [{"sha": MERGE, "parents": []}]},
                },
                handle,
            )
        self.assertEqual(run_cli(["changes-sync", "--db", self.db, "--input", bundle]).returncode, 0)
        result = run_cli(["exposure", "--db", self.db])
        self.assertEqual(result.returncode, 0, result.stderr)
        ledger = json.loads(result.stdout)
        self.assertEqual(ledger["screening"]["screened"], 3)
        self.assertEqual(ledger["screening"]["non_optimization"], 1)
        self.assertEqual(ledger["screening"]["unknown"], 1)
        self.assertEqual(ledger["denominator"]["screened_total"], 3)
        self.assertEqual(ledger["missingness"]["baseline_unknown"], 3)

    def test_missing_bundle_reports_clean_error(self):
        missing = os.path.join(self.tmp.name, "nope.json")
        result = run_cli(["changes-sync", "--db", self.db, "--input", missing])
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)
        self.assertTrue(result.stderr.strip().startswith("error:"), result.stderr)


if __name__ == "__main__":
    unittest.main()
