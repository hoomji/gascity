"""Tests for the M6 accepted-task impact reports.

The fixtures prove the three required replay semantics: a known effect, no
effect, and a confounded comparison. The unit cases pin the accounting edges
that a plausible-looking report gets wrong -- censoring, zero accepted tasks,
zero baselines, missing price/usage vs measured zero, concurrent work, and the
attribution grade.
"""

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

from agent_observatory.errors import ImpactError
from agent_observatory.impact import (
    DEFAULT_MATCH_ON,
    ImpactConfig,
    build_impact_report,
    file_sha256,
    load_impact_bundle,
    normalize_impact_bundle,
    observed_evidence_from_store,
)
from agent_observatory.store import ObservatoryStore

PACKAGE_ROOT = support.PACKAGE_ROOT
FIXTURES = os.path.join(PACKAGE_ROOT, "tests", "fixtures", "impact")


def run_cli(args):
    env = dict(os.environ)
    env["PYTHONPATH"] = PACKAGE_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "agent_observatory", *args],
        cwd=PACKAGE_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


BASE = "2026-09-10T00:00:00Z"


def ts(seconds: int) -> str:
    minutes, remainder = divmod(int(seconds), 60)
    return f"2026-09-10T00:{minutes:02d}:{remainder:02d}Z"


def make_item(
    work_item_id,
    *,
    outcome="accepted",
    cohort="treatment",
    time_s=60,
    cost_usd=1.0,
    attempts=None,
    quality=None,
    **overrides,
):
    item = {
        "work_item_id": work_item_id,
        "outcome": outcome,
        "cohort": cohort,
        "repo": "gas",
        "task_class": "bugfix",
        "scope": "small",
        "provider": "dsh",
        "model": "deepseek-flash",
        "harness": "fleet",
        "effort": "high",
        "host": "ryzen",
        "baseline_complexity": "low",
        "workload": "unit",
        "cache_state": "warm",
        "concurrency": "1",
        "ready_at": BASE,
        "accepted_at": ts(time_s) if outcome == "accepted" else None,
        "acceptance_kind": "merge" if outcome == "accepted" else None,
        "attempts": attempts
        if attempts is not None
        else [
            {
                "attempt_id": f"a-{work_item_id}",
                "kind": "execute",
                "phase": "active",
                "started_at": BASE,
                "ended_at": ts(time_s),
                "cost_usd": cost_usd,
            }
        ],
    }
    if quality is not None:
        item["quality"] = quality
    item.update(overrides)
    return item


def report_for(items, config=None, **bundle_kwargs):
    bundle = {"schema_version": "1.0", "work_items": items, **bundle_kwargs}
    return build_impact_report(normalize_impact_bundle(bundle), config or ImpactConfig())


class BundleValidationTest(unittest.TestCase):
    def test_rejects_unknown_key(self):
        with self.assertRaises(ImpactError):
            normalize_impact_bundle({"schema_version": "1.0", "work_items": [], "nope": 1})

    def test_rejects_bad_schema_version(self):
        with self.assertRaises(ImpactError):
            normalize_impact_bundle({"schema_version": "2.0", "work_items": []})

    def test_rejects_duplicate_work_item_id(self):
        with self.assertRaises(ImpactError):
            normalize_impact_bundle(
                {"schema_version": "1.0", "work_items": [make_item("w1"), make_item("w1")]}
            )

    def test_rejects_unknown_outcome(self):
        with self.assertRaises(ImpactError):
            normalize_impact_bundle(
                {"schema_version": "1.0", "work_items": [make_item("w1", outcome="finished")]}
            )

    def test_rejects_unknown_cohort(self):
        with self.assertRaises(ImpactError):
            normalize_impact_bundle(
                {"schema_version": "1.0", "work_items": [make_item("w1", cohort="treated")]}
            )

    def test_rejects_bad_phase(self):
        item = make_item("w1", attempts=[{"attempt_id": "a", "phase": "sleeping"}])
        with self.assertRaises(ImpactError):
            normalize_impact_bundle({"schema_version": "1.0", "work_items": [item]})

    def test_rejects_unknown_price_key(self):
        item = make_item(
            "w1", attempts=[{"attempt_id": "a", "price": {"input_per_million_usd": 1.0, "bogus": 2.0}}]
        )
        with self.assertRaises(ImpactError):
            normalize_impact_bundle({"schema_version": "1.0", "work_items": [item]})

    def test_rejects_unknown_usage_key(self):
        item = make_item("w1", attempts=[{"attempt_id": "a", "usage": {"reasoning_tokens": 5}}])
        with self.assertRaises(ImpactError):
            normalize_impact_bundle({"schema_version": "1.0", "work_items": [item]})

    def test_match_on_must_be_a_supported_subset(self):
        with self.assertRaises(ImpactError):
            normalize_impact_bundle(
                {"schema_version": "1.0", "match_on": ["repo", "final_diff_size"], "work_items": []}
            )
        dataset = normalize_impact_bundle(
            {"schema_version": "1.0", "match_on": ["repo", "scope"], "work_items": []}
        )
        self.assertEqual(dataset.match_on, ("repo", "scope"))


class ReplayFixtureTest(unittest.TestCase):
    def load(self, name):
        return load_impact_bundle(os.path.join(FIXTURES, f"{name}.json"))

    def test_known_effect_is_a_matched_improvement(self):
        report = build_impact_report(self.load("known_effect"))
        attribution = report["attribution"]
        time_outcome = report["outcomes"]["time_to_accepted_seconds"]
        self.assertEqual(attribution["grade"], "matched_observational")
        self.assertEqual(attribution["conclusion"], "observed_improvement")
        self.assertFalse(attribution["confounded"])
        self.assertEqual(attribution["causal_claim"], "associational_matched")
        self.assertAlmostEqual(time_outcome["matched"]["difference"], -30.0, places=6)
        self.assertLess(time_outcome["matched"]["ci_high"], 0.0)
        self.assertEqual(report["accepted_tasks"]["success_rate"], 1.0)

    def test_no_effect_does_not_claim_an_effect(self):
        report = build_impact_report(self.load("no_effect"))
        attribution = report["attribution"]
        time_outcome = report["outcomes"]["time_to_accepted_seconds"]
        self.assertEqual(attribution["conclusion"], "no_observed_effect")
        self.assertFalse(attribution["confounded"])
        self.assertAlmostEqual(time_outcome["matched"]["difference"], 0.0, places=6)
        self.assertLess(time_outcome["matched"]["ci_low"], 0.0)
        self.assertGreater(time_outcome["matched"]["ci_high"], 0.0)

    def test_confounded_comparison_is_not_matched_or_causal(self):
        report = build_impact_report(self.load("confounded"))
        attribution = report["attribution"]
        time_outcome = report["outcomes"]["time_to_accepted_seconds"]
        self.assertEqual(attribution["grade"], "descriptive")
        self.assertTrue(attribution["confounded"])
        self.assertIn("no_matched_strata", attribution["reasons"])
        self.assertIn("covariate_imbalance", attribution["reasons"])
        self.assertIn("task_class", attribution["imbalanced_covariates"])
        self.assertNotEqual(attribution["conclusion"], "observed_improvement")
        self.assertIsNone(time_outcome["matched"])
        self.assertLess(time_outcome["naive_difference"], -30.0)

    def test_fixture_reports_are_deterministic(self):
        for name in ("known_effect", "no_effect", "confounded"):
            first = build_impact_report(self.load(name))
            second = build_impact_report(self.load(name))
            self.assertEqual(first["report_hash"], second["report_hash"], name)
            self.assertEqual(first["provenance"]["dataset_hash"], second["provenance"]["dataset_hash"], name)

    def test_fixtures_carry_no_causal_claim_from_labels(self):
        for name in ("known_effect", "no_effect", "confounded"):
            report = build_impact_report(self.load(name))
            self.assertIn("not causal evidence", report["notes"][-1])
            self.assertNotIn(report["attribution"]["causal_claim"], ("causal", "proven"))


class AcceptedTaskSemanticsTest(unittest.TestCase):
    def test_zero_accepted_tasks_make_cost_per_accepted_undefined(self):
        item = make_item("w1", outcome="rejected", accepted_at=None, cost_usd=3.0)
        report = report_for([item])
        cost = report["accepted_tasks"]["cost"]
        self.assertEqual(report["accepted_tasks"]["accepted"], 0)
        self.assertTrue(report["accepted_tasks"]["zero_accepted_tasks"])
        self.assertEqual(report["accepted_tasks"]["success_rate"], 0.0)
        self.assertIsNone(cost["cost_per_accepted_usd"])
        self.assertEqual(cost["cost_per_accepted_reason"], "no_accepted_tasks")
        # The failed attempt's measured cost is retained, not discarded.
        self.assertEqual(cost["measured_attempt_cost_usd"], 3.0)

    def test_empty_bundle_has_undefined_success_rate(self):
        report = report_for([])
        self.assertIsNone(report["accepted_tasks"]["success_rate"])
        self.assertIsNone(report["accepted_tasks"]["cost"]["price_coverage"])
        self.assertEqual(report["attribution"]["grade"], "unmeasurable")

    def test_censored_task_is_retained_not_recorded_as_zero(self):
        item = make_item(
            "w1",
            outcome="in_progress",
            accepted_at=None,
            attempts=[
                {
                    "attempt_id": "a1",
                    "kind": "execute",
                    "phase": "active",
                    "started_at": BASE,
                    "ended_at": None,
                    "cost_usd": 2.0,
                }
            ],
        )
        report = report_for([item])
        tasks = report["accepted_tasks"]
        self.assertEqual(tasks["eligible"], 1)
        self.assertEqual(tasks["censored"], 1)
        self.assertEqual(tasks["still_open"], 1)
        self.assertEqual(tasks["accepted"], 0)
        self.assertIsNone(report["outcomes"]["time_to_accepted_seconds"]["naive_difference"])
        # An open attempt has an unknown interval; it is reported, not zeroed.
        self.assertEqual(report["missingness"]["open_attempts_without_end"], 1)

    def test_terminal_rejected_run_stays_in_the_denominator(self):
        report = report_for([make_item("w1", outcome="rejected", accepted_at=None)])
        self.assertEqual(report["accepted_tasks"]["eligible"], 1)
        self.assertEqual(report["accepted_tasks"]["rejected"], 1)
        self.assertEqual(report["accepted_tasks"]["censored"], 0)
        self.assertEqual(report["accepted_tasks"]["success_rate"], 0.0)

    def test_failed_attempt_cost_counts_toward_per_accepted_cost(self):
        failed = make_item("w1", outcome="abandoned", accepted_at=None, cost_usd=5.0)
        accepted = make_item("w2", cost_usd=1.0)
        report = report_for([failed, accepted])
        cost = report["accepted_tasks"]["cost"]
        self.assertEqual(report["accepted_tasks"]["accepted"], 1)
        self.assertEqual(cost["measured_attempt_cost_usd"], 6.0)
        self.assertEqual(cost["cost_per_accepted_usd"], 6.0)


class CostMeasurementTest(unittest.TestCase):
    def test_missing_price_leaves_cost_unknown(self):
        attempt = {
            "attempt_id": "a1",
            "usage": {"input_tokens": 1000, "output_tokens": 100},
        }
        report = report_for([make_item("w1", attempts=[attempt])])
        cost = report["accepted_tasks"]["cost"]
        self.assertEqual(cost["measured_attempt_cost_usd"], 0.0)
        self.assertEqual(cost["unmeasured_cost_items"], 1)
        self.assertIsNone(cost["cost_per_accepted_usd"])
        self.assertEqual(cost["cost_per_accepted_reason"], "incomplete_price_or_usage_coverage")

    def test_missing_usage_leaves_cost_unknown(self):
        attempt = {"attempt_id": "a1", "price": {"input_per_million_usd": 1.0, "output_per_million_usd": 1.0}}
        report = report_for([make_item("w1", attempts=[attempt])])
        self.assertEqual(report["accepted_tasks"]["cost"]["unmeasured_cost_items"], 1)

    def test_explicit_zero_cost_is_a_measured_zero(self):
        report = report_for([make_item("w1", attempts=[{"attempt_id": "a1", "cost_usd": 0.0}])])
        cost = report["accepted_tasks"]["cost"]
        self.assertEqual(cost["measured_attempt_cost_usd"], 0.0)
        self.assertEqual(cost["unmeasured_cost_items"], 0)
        self.assertEqual(cost["cost_per_accepted_usd"], 0.0)

    def test_zero_tokens_with_price_is_a_measured_zero(self):
        attempt = {
            "attempt_id": "a1",
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "price": {"input_per_million_usd": 10.0, "output_per_million_usd": 10.0},
        }
        report = report_for([make_item("w1", attempts=[attempt])])
        self.assertEqual(report["accepted_tasks"]["cost"]["cost_per_accepted_usd"], 0.0)
        self.assertEqual(report["accepted_tasks"]["cost"]["unmeasured_cost_items"], 0)

    def test_usage_and_price_compute_a_cost(self):
        attempt = {
            "attempt_id": "a1",
            "usage": {"input_tokens": 1_000_000, "output_tokens": 500_000},
            "price": {"input_per_million_usd": 2.0, "output_per_million_usd": 4.0},
        }
        report = report_for([make_item("w1", attempts=[attempt])])
        self.assertEqual(report["accepted_tasks"]["cost"]["cost_per_accepted_usd"], 4.0)

    def test_numeric_outcome_reports_per_arm_distribution(self):
        items = [
            make_item("t1", time_s=10),
            make_item("t2", time_s=20),
            make_item("c1", cohort="control", time_s=40),
            make_item("c2", cohort="control", time_s=60),
        ]
        report = report_for(items)
        time_outcome = report["outcomes"]["time_to_accepted_seconds"]
        self.assertEqual(time_outcome["treatment"]["n"], 2)
        self.assertEqual(time_outcome["treatment"]["median"], 15.0)
        self.assertEqual(time_outcome["control"]["median"], 50.0)
        self.assertEqual(time_outcome["missing"], {"treatment": 0, "control": 0})

    def test_zero_baseline_relative_change_is_undefined(self):
        items = [
            make_item("t1", time_s=0, cost_usd=1.0),
            make_item("t2", time_s=0, cost_usd=1.0),
            make_item("c1", cohort="control", time_s=0, cost_usd=1.0),
            make_item("c2", cohort="control", time_s=0, cost_usd=1.0),
        ]
        report = report_for(items)
        relative = report["outcomes"]["time_to_accepted_seconds"]["matched_relative_change"]
        self.assertIsNone(relative["value"])
        self.assertEqual(relative["reason"], "zero_baseline")


class ConcurrencyTest(unittest.TestCase):
    def test_overlapping_attempts_are_unioned_not_summed(self):
        attempts = [
            {"attempt_id": "a1", "phase": "active", "started_at": BASE, "ended_at": ts(10), "cost_usd": 1.0},
            {"attempt_id": "a2", "phase": "active", "started_at": ts(5), "ended_at": ts(15), "cost_usd": 1.0},
        ]
        report = report_for([make_item("w1", attempts=attempts)])
        execution = report["accepted_tasks"]["execution_time_seconds"]
        self.assertEqual(execution["active_union"], 15.0)
        self.assertEqual(execution["parallel_overlap"], 0.0)

    def test_parallel_work_reports_overlap_and_never_sums_wall_time(self):
        items = [
            make_item(
                "w1",
                attempts=[
                    {"attempt_id": "a1", "phase": "active", "started_at": BASE, "ended_at": ts(10), "cost_usd": 1.0}
                ],
            ),
            make_item(
                "w2",
                attempts=[
                    {"attempt_id": "a2", "phase": "active", "started_at": BASE, "ended_at": ts(10), "cost_usd": 1.0}
                ],
            ),
        ]
        report = report_for(items)
        execution = report["accepted_tasks"]["execution_time_seconds"]
        self.assertEqual(execution["active_union"], 20.0)
        self.assertEqual(execution["global_active_union"], 10.0)
        self.assertEqual(execution["parallel_overlap"], 10.0)

    def test_phases_are_separated(self):
        attempts = [
            {"attempt_id": "a1", "phase": "queue", "started_at": BASE, "ended_at": ts(20), "cost_usd": 0.0},
            {"attempt_id": "a2", "phase": "human_review", "started_at": ts(20), "ended_at": ts(50), "cost_usd": 0.0},
        ]
        report = report_for([make_item("w1", attempts=attempts)])
        execution = report["accepted_tasks"]["execution_time_seconds"]
        self.assertEqual(execution["queue"], 20.0)
        self.assertEqual(execution["human_review"], 30.0)
        self.assertEqual(execution["active_union"], 0.0)


class ClassifierOverheadTest(unittest.TestCase):
    def test_overhead_rates_and_attribution(self):
        bundle = {
            "schema_version": "1.0",
            "work_items": [make_item("w1")],
            "classifier_overhead": [
                {
                    "work_item_id": "w1",
                    "requests": 10,
                    "input_tokens": 1000,
                    "output_tokens": 200,
                    "latency_ms": 100.0,
                    "retries": 2,
                    "cache_hits": 3,
                },
                {"requests": 5, "input_tokens": 50, "output_tokens": 10, "latency_ms": 300.0},
            ],
        }
        report = build_impact_report(normalize_impact_bundle(bundle))
        overhead = report["classifier_overhead"]
        self.assertEqual(overhead["requests"], 15)
        self.assertEqual(overhead["retries"], 2)
        self.assertAlmostEqual(overhead["retry_rate"], 2 / 15)
        self.assertAlmostEqual(overhead["cache_hit_rate"], 3 / 15)
        self.assertEqual(overhead["input_tokens"], 1050)
        self.assertEqual(overhead["unattributed_entries"], 1)
        self.assertEqual(overhead["latency_ms"]["n"], 2)

    def test_overhead_is_not_folded_into_task_cost(self):
        bundle = {
            "schema_version": "1.0",
            "work_items": [make_item("w1", cost_usd=1.0)],
            "classifier_overhead": [{"work_item_id": "w1", "requests": 1, "input_tokens": 10, "output_tokens": 5}],
        }
        report = build_impact_report(normalize_impact_bundle(bundle))
        self.assertEqual(report["accepted_tasks"]["cost"]["cost_per_accepted_usd"], 1.0)
        self.assertEqual(report["classifier_overhead"]["requests"], 1)


class AttributionTest(unittest.TestCase):
    def balanced_items(self):
        items = []
        for index in range(1, 6):
            items.append(make_item(f"t{index}", time_s=50 + index, cost_usd=1.0))
            items.append(make_item(f"c{index}", cohort="control", time_s=80 + index, cost_usd=2.0))
        return items

    def test_randomized_assignment_earns_controlled_grade(self):
        report = report_for(self.balanced_items(), evidence={"randomized": True, "design": "controlled"})
        self.assertEqual(report["attribution"]["grade"], "controlled")
        self.assertEqual(report["attribution"]["causal_claim"], "causal_supported_by_randomized_assignment")

    def test_pretrends_earn_quasi_experimental_grade(self):
        report = report_for(self.balanced_items(), evidence={"parallel_pre_trends": True})
        self.assertEqual(report["attribution"]["grade"], "quasi_experimental")

    def test_observational_balanced_comparison_is_matched_observational(self):
        report = report_for(self.balanced_items(), config=ImpactConfig(min_overlap=0.0))
        self.assertEqual(report["attribution"]["grade"], "matched_observational")

    def test_residual_imbalance_downgrades_a_strong_grade(self):
        # Assignment is randomized, but one arm is only present in one stratum, so
        # the covariate is confounded and the strong grade cannot stand.
        items = [
            make_item("t1", baseline_complexity="low", time_s=50),
            make_item("t2", baseline_complexity="low", time_s=50),
            make_item("c1", cohort="control", baseline_complexity="high", time_s=90),
            make_item("c2", cohort="control", baseline_complexity="high", time_s=90),
        ]
        report = report_for(items, evidence={"randomized": True})
        self.assertEqual(report["attribution"]["grade"], "descriptive")
        self.assertTrue(report["attribution"]["confounded"])
        self.assertIn("covariate_imbalance", report["attribution"]["reasons"])

    def test_rate_outcome_uses_the_same_matched_cohort(self):
        passed = {
            "first_pass_verified": True,
            "review_rounds": 0,
            "reopened": False,
            "reverted": False,
            "regression": False,
        }
        reworked = {
            "first_pass_verified": False,
            "review_rounds": 1,
            "reopened": True,
            "reverted": False,
            "regression": True,
        }
        items = []
        for index in range(1, 5):
            items.append(make_item(f"t{index}", time_s=50, quality=passed))
            items.append(make_item(f"c{index}", cohort="control", time_s=80, quality=reworked))
        report = report_for(items)
        rate = report["outcomes"]["first_pass_verified"]
    def test_b1_matched_point_estimate_restricted_to_usable_strata(self):
        items = [
            make_item(f"t1_{i}", time_s=10, cost_usd=1.0, task_class="bugfix")
            for i in range(4)
        ] + [
            make_item(f"c1_{i}", cohort="control", time_s=20, cost_usd=2.0, task_class="bugfix")
            for i in range(4)
        ] + [
            make_item(f"t2_{i}", time_s=500, cost_usd=50.0, task_class="feature")
            for i in range(4)
        ] + [
            make_item(f"c2_{i}", cohort="control", outcome="in_progress", time_s=500, cost_usd=50.0, task_class="feature")
            for i in range(4)
        ]
        report = report_for(items, evidence={"design": "observational"})
        matched = report["outcomes"]["time_to_accepted_seconds"]["matched"]
        self.assertIsNotNone(matched)
        self.assertAlmostEqual(matched["difference"], -10.0)
        self.assertLessEqual(matched["ci_low"], matched["difference"])
        self.assertGreaterEqual(matched["ci_high"], matched["difference"])
        self.assertEqual(matched["strata"], 1)

    def test_b2_assignment_logged_alone_does_not_earn_controlled_grade(self):
        report = report_for(self.balanced_items(), evidence={"assignment_logged": True})
        self.assertEqual(report["attribution"]["grade"], "matched_observational")
        self.assertEqual(report["attribution"]["causal_claim"], "associational_matched")


class ObservedEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "projection.db")

    def test_fresh_projection_has_no_optional_tables_and_does_not_crash(self):
        with ObservatoryStore(self.db) as store:
            evidence = observed_evidence_from_store(store, source_label="fresh.db")
        self.assertEqual(evidence["totals"]["events"], 0)
        self.assertEqual(evidence["classifier_overhead"]["total_requests"], 0)
        self.assertEqual(evidence["collector_queue"], {})
        self.assertEqual(evidence["acceptance_evidence"]["accepted"], 0)

    def test_usage_missing_vs_zero_and_duration_stats(self):
        fixture = support.write_jsonl(
            os.path.join(self.tmp.name, "events.jsonl"),
            [
                support.make_record(
                    event_id="e1",
                    kind="usage",
                    usage={"input_tokens": 0, "output_tokens": 0},
                    duration_ms=100,
                ),
                support.make_record(event_id="e2", kind="message", timestamp="2026-09-21T10:00:01Z"),
                support.make_record(
                    event_id="e3",
                    kind="usage",
                    timestamp="2026-09-21T10:00:02Z",
                    usage={"input_tokens": 5, "output_tokens": 7},
                    duration_ms=300,
                ),
            ],
        )
        with ObservatoryStore(self.db) as store:
            store.import_jsonl(fixture)
            store.conn.execute(
                "CREATE TABLE transport_provenance ("
                "provenance_id INTEGER PRIMARY KEY, model TEXT NOT NULL, outcome TEXT NOT NULL, "
                "attempts INTEGER NOT NULL DEFAULT 0, latency_ms REAL, input_tokens INTEGER, "
                "output_tokens INTEGER, cost_usd REAL, cost_known INTEGER NOT NULL DEFAULT 0)"
            )
            store.conn.execute(
                "INSERT INTO transport_provenance(model, outcome, attempts, latency_ms, input_tokens, "
                "output_tokens, cost_usd, cost_known) VALUES ('jev-1.13.0', 'classified', 1, 250.0, 10, 20, NULL, 0)"
            )
            store.conn.execute(
                "INSERT INTO transport_provenance(model, outcome, attempts, latency_ms, input_tokens, "
                "output_tokens, cost_usd, cost_known) VALUES ('jev-1.13.0', 'pending', 0, 0.0, NULL, NULL, 0.0, 1)"
            )
            evidence = observed_evidence_from_store(store, source_label="events.db", source_hash="deadbeef")

        provider = evidence["by_provider"][0]
        self.assertEqual(provider["usage_rows_zero"], 1)
        self.assertEqual(provider["usage_rows_missing"], 1)
        self.assertEqual(evidence["usage_coverage"]["usage_rows"], 2)
        self.assertEqual(evidence["usage_coverage"]["input_zero"], 1)
        self.assertEqual(evidence["observed_duration_ms"]["n"], 2)
        self.assertEqual(evidence["observed_duration_ms"]["median"], 200.0)
        self.assertEqual(evidence["classifier_overhead"]["total_requests"], 2)
        self.assertEqual(evidence["classifier_overhead"]["cost_unknown_rows"], 1)
        self.assertEqual(evidence["classifier_overhead"]["cost_known_rows"], 1)
        self.assertEqual(evidence["source_hash"], "deadbeef")

    def test_file_sha256_matches_known_digest(self):
        path = os.path.join(self.tmp.name, "blob.bin")
        with open(path, "wb") as handle:
            handle.write(b"impact")
        self.assertEqual(
            file_sha256(path),
            "6f61f46c23a66607af759dcd348ac132846385d8f39534e69b6cd934bdabad8b",
        )


class ImpactCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_cli_bundle_report(self):
        out = os.path.join(self.tmp.name, "report.json")
        result = run_cli(["impact", "--input", os.path.join(FIXTURES, "known_effect.json"), "--out", out])
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stderr.strip().splitlines()[-1])
        self.assertEqual(summary["attribution_grade"], "matched_observational")
        with open(out, encoding="utf-8") as handle:
            report = json.load(handle)
        self.assertEqual(report["kind"], "accepted_task_impact")
        self.assertEqual(report["attribution"]["conclusion"], "observed_improvement")

    def test_cli_requires_input_or_db(self):
        result = run_cli(["impact"])
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)
        self.assertTrue(result.stderr.strip().startswith("error:"), result.stderr)

    def test_cli_db_only_report_is_honest_about_missing_acceptance(self):
        db = os.path.join(self.tmp.name, "projection.db")
        fixture = support.write_jsonl(
            os.path.join(self.tmp.name, "events.jsonl"),
            [support.make_record(event_id="e1", kind="usage", usage={"input_tokens": 3, "output_tokens": 4})],
        )
        self.assertEqual(run_cli(["import-jsonl", "--db", db, fixture]).returncode, 0)
        out = os.path.join(self.tmp.name, "real.json")
        result = run_cli(["impact", "--db", db, "--out", out])
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(out, encoding="utf-8") as handle:
            report = json.load(handle)
        self.assertEqual(report["attribution"]["grade"], "unmeasurable")
        self.assertEqual(report["observed_evidence"]["totals"]["events"], 1)
        self.assertEqual(report["observed_evidence"]["acceptance_evidence"]["accepted"], 0)
        self.assertIsNotNone(report["observed_evidence"]["source_hash"])


if __name__ == "__main__":
    unittest.main()

