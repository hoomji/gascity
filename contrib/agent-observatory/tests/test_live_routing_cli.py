"""End-to-end CLI tests for the M8b advisory live-routing command (no network)."""

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

from agent_observatory.canary import normalize_registration

PACKAGE_ROOT = support.PACKAGE_ROOT
POLICY_FIXTURES = os.path.join(PACKAGE_ROOT, "tests", "fixtures", "policy")
CATALOG = os.path.join(POLICY_FIXTURES, "catalog.json")

RECORDED_AT = "2026-09-26T12:00:00Z"


def run_cli(args, cwd=PACKAGE_ROOT, stdin=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = PACKAGE_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "agent_observatory", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        input=stdin,
    )


def registration(**overrides):
    raw = {
        "schema_version": "1.0",
        "registration_id": "canary-live-cli",
        "policy_id": "context-full-for-planning-v1",
        "policy_kind": "context_bundle",
        "treatment_candidate": "context:full",
        "prior_candidate": "context:minimal",
        "catalog_version": "2026-09-24",
        "seed": "seed-live-cli",
        "sample_size": 4,
        "treatment_fraction": 0.5,
        "max_requests": 50,
        "created_at": "2026-09-24T22:00:00Z",
        "primary_outcome": "time_to_accepted_seconds",
        "guardrails": {
            "min_control_n": 1,
            "min_treatment_n": 1,
            "max_quality_regression": 0.5,
            "max_cost_regression_usd": 1.0,
            "min_acceptance_rate": 0.0,
            "alpha": 0.05,
        },
    }
    raw.update(overrides)
    return raw


class LiveRoutingCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registration_path = os.path.join(self.tmp.name, "registration.json")
        self.ledger = os.path.join(self.tmp.name, "ledger.jsonl")
        self.raw_registration = registration()
        with open(self.registration_path, "w", encoding="utf-8") as handle:
            json.dump(self.raw_registration, handle)
        self.registration_hash = normalize_registration(self.raw_registration).registration_hash()

    def base_args(self):
        return [
            "canary-live-route",
            "--registration",
            self.registration_path,
            "--catalog",
            CATALOG,
            "--ledger",
            self.ledger,
            "--expected-registration-hash",
            self.registration_hash,
        ]

    def dispatch(self, **overrides):
        raw = {
            "dispatch_id": "dispatch-1",
            "bead_id": "gl-abc123",
            "actual_route": "agent:planner",
            "recorded_at": RECORDED_AT,
            "repo": "gascity",
        }
        raw.update(overrides)
        return raw

    def test_records_advisory_without_changing_route(self):
        request = os.path.join(self.tmp.name, "request.json")
        with open(request, "w", encoding="utf-8") as handle:
            json.dump(self.dispatch(), handle)
        result = run_cli([*self.base_args(), "--request", request])
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["kind"], "live_routing_batch")
        self.assertFalse(payload["executes_changes"])
        self.assertEqual(len(payload["records"]), 1)
        record = payload["records"][0]
        self.assertEqual(record["applied_route"], "agent:planner")
        self.assertEqual(record["actual_route"], "agent:planner")
        self.assertFalse(record["route_changed"])
        self.assertTrue(record["unknown_as_abstain"])

    def test_reads_request_from_stdin(self):
        result = run_cli(self.base_args(), stdin=json.dumps(self.dispatch()))
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["records"][0]["bead_id"], "gl-abc123")

    def test_mismatched_hash_is_refused(self):
        bad = [*self.base_args()]
        bad[bad.index("--expected-registration-hash") + 1] = "0" * 64
        request = os.path.join(self.tmp.name, "request.json")
        with open(request, "w", encoding="utf-8") as handle:
            json.dump(self.dispatch(), handle)
        result = run_cli([*bad, "--request", request])
        self.assertEqual(result.returncode, 1)
        self.assertIn("error:", result.stderr)
        self.assertIn("hash mismatch", result.stderr)
        self.assertFalse(os.path.exists(self.ledger))

    def test_sidecar_hash_is_accepted(self):
        with open(self.registration_path + ".hash", "w", encoding="utf-8") as handle:
            handle.write(self.registration_hash + "\n")
        args = [a for a in self.base_args() if a not in ("--expected-registration-hash", self.registration_hash)]
        result = run_cli(args, stdin=json.dumps(self.dispatch()))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_classification_map_yields_a_suggestion(self):
        classifications = os.path.join(self.tmp.name, "classifications.json")
        with open(classifications, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "dispatch-1": {
                        "episode_id": "dispatch-1",
                        "as_of": RECORDED_AT,
                        "observed_at": RECORDED_AT,
                        "intent": "planning_spec",
                        "scope": "large",
                        "confidence": 0.95,
                        "repo": "gascity",
                    }
                },
                handle,
            )
        result = run_cli(
            [*self.base_args(), "--classification", classifications],
            stdin=json.dumps(self.dispatch()),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)["records"][0]
        self.assertEqual(record["primary_intent"], "planning_spec")
        self.assertEqual(record["suggested_route"], "context:full")
        self.assertEqual(record["applied_route"], "agent:planner")

    def test_ledger_budget_is_re_derived_across_processes(self):
        # A previous process already spent the whole registered budget.
        with open(self.ledger, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "kind": "live_routing_request",
                        "version": "1",
                        "dispatch_id": "earlier",
                        "attempt": 1,
                        "requests": 50,
                    }
                )
                + "\n"
            )
        result = run_cli(self.base_args(), stdin=json.dumps(self.dispatch()))
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["requests_spent"], 50)
        self.assertEqual(payload["requests_remaining"], 0)

    def test_batch_request_keeps_one_row_per_dispatch(self):
        batch = os.path.join(self.tmp.name, "batch.json")
        with open(batch, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "batch_id": "batch-1",
                    "dispatches": [
                        self.dispatch(dispatch_id="d1", bead_id="gl-1"),
                        self.dispatch(dispatch_id="d2", bead_id="gl-2"),
                    ],
                },
                handle,
            )
        result = run_cli([*self.base_args(), "--request", batch])
        self.assertEqual(result.returncode, 0, result.stderr)
        records = json.loads(result.stdout)["records"]
        self.assertEqual([record["dispatch_id"] for record in records], ["d1", "d2"])
        self.assertTrue(all(record["applied_route"] == "agent:planner" for record in records))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
