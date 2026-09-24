"""End-to-end CLI tests for the M8 canary commands (no network)."""

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

from agent_observatory.canary import load_registration

PACKAGE_ROOT = support.PACKAGE_ROOT
FIXTURES = os.path.join(PACKAGE_ROOT, "tests", "fixtures", "canary")
POLICY_FIXTURES = os.path.join(PACKAGE_ROOT, "tests", "fixtures", "policy")


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


class CanaryCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.catalog = os.path.join(POLICY_FIXTURES, "catalog.json")
        self.registration = os.path.join(FIXTURES, "registration.json")
        self.units = os.path.join(FIXTURES, "units.json")
        self.unclassified = os.path.join(FIXTURES, "units-unclassified.json")

    def test_register_writes_artifact(self):
        out = os.path.join(self.tmp.name, "registered.json")
        result = run_cli(["canary-register", "--input", self.registration, "--out", out])
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stderr)
        self.assertTrue(summary["registration_hash"])
        registered = load_registration(out)
        self.assertEqual(registered.policy_id, "context-full-for-planning-v1")

    def test_default_run_is_prior_policy(self):
        out = os.path.join(self.tmp.name, "canary.json")
        result = run_cli(
            ["canary", "--registration", self.registration, "--catalog", self.catalog,
             "--input", self.units, "--out", out]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(out, encoding="utf-8") as handle:
            report = json.load(handle)
        self.assertEqual(report["kind"], "policy_canary")
        self.assertFalse(report["canary"]["executes_changes"])
        self.assertEqual(report["canary"]["mode"], "disabled")
        self.assertFalse(report["canary"]["stop_recommended"])
        self.assertEqual(report["canary"]["exposure"]["assigned_treatment"], 0)
        self.assertEqual(report["canary"]["net_effect"]["improvement_claim"], "none")
        self.assertTrue(
            all(
                row["applied_candidate"] == row["prior_candidate"]
                for row in report["canary"]["assignment_ledger"]
            )
        )

    def test_enabled_run_assigns_treatment(self):
        result = run_cli(
            ["canary", "--registration", self.registration, "--catalog", self.catalog,
             "--input", self.units, "--enable"]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stderr)
        self.assertEqual(summary["mode"], "randomized")
        self.assertGreater(summary["assigned_treatment"], 0)
        self.assertEqual(summary["assigned_control"] + summary["assigned_treatment"], 8)

    def test_kill_switch_restores_prior_policy(self):
        switch = os.path.join(self.tmp.name, "canary-off")
        with open(switch, "w", encoding="utf-8") as handle:
            handle.write("off\n")
        out = os.path.join(self.tmp.name, "rollback.json")
        result = run_cli(
            ["canary", "--registration", self.registration, "--catalog", self.catalog,
             "--input", self.units, "--enable", "--kill-switch", switch, "--out", out]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(out, encoding="utf-8") as handle:
            report = json.load(handle)
        self.assertEqual(report["canary"]["mode"], "rolled_back")
        self.assertTrue(report["canary"]["kill_switch"])
        self.assertEqual(report["canary"]["exposure"]["assigned_treatment"], 0)
        self.assertTrue(
            all(
                not row["applied_treatment"]
                and row["applied_candidate"] == row["prior_candidate"]
                for row in report["canary"]["assignment_ledger"]
            )
        )

    def test_max_requests_caps_live_classification(self):
        out = os.path.join(self.tmp.name, "capped.json")
        result = run_cli(
            ["canary", "--registration", self.registration, "--catalog", self.catalog,
             "--input", self.unclassified, "--enable", "--max-requests", "0", "--out", out]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(out, encoding="utf-8") as handle:
            report = json.load(handle)
        budget = report["canary"]["classification_budget"]
        self.assertEqual(budget["requests_used"], 0)
        self.assertTrue(budget["requests_capped"])
        self.assertEqual(len(budget["deferred_units"]), 4)

    def test_max_requests_above_owner_cap_is_refused(self):
        result = run_cli(
            ["canary", "--registration", self.registration, "--catalog", self.catalog,
             "--input", self.units, "--max-requests", "51"]
        )
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("error:", result.stderr)

    def test_missing_registration_reports_clean_error(self):
        result = run_cli(
            ["canary", "--registration", os.path.join(self.tmp.name, "nope.json"),
             "--catalog", self.catalog, "--input", self.units]
        )
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)
        self.assertTrue(result.stderr.strip().startswith("error:"), result.stderr)

    def test_missing_catalog_reports_clean_error(self):
        result = run_cli(
            ["canary", "--registration", self.registration,
             "--catalog", os.path.join(self.tmp.name, "nope.json"), "--input", self.units]
        )
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("error:", result.stderr)

    def test_invalid_registration_reports_clean_error(self):
        bad = os.path.join(self.tmp.name, "bad.json")
        with open(bad, "w", encoding="utf-8") as handle:
            json.dump({"schema_version": "1.0", "nope": 1}, handle)
        result = run_cli(
            ["canary", "--registration", bad, "--catalog", self.catalog, "--input", self.units]
        )
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("error:", result.stderr)

    def test_run_is_deterministic(self):
        first = run_cli(
            ["canary", "--registration", self.registration, "--catalog", self.catalog,
             "--input", self.units, "--enable"]
        )
        second = run_cli(
            ["canary", "--registration", self.registration, "--catalog", self.catalog,
             "--input", self.units, "--enable"]
        )
        self.assertEqual(json.loads(first.stderr)["report_hash"], json.loads(second.stderr)["report_hash"])


if __name__ == "__main__":
    unittest.main()
