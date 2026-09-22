"""Tests for grouped temporal splits, metrics, calibration and the routing gate."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

try:
    from . import support
except ImportError:  # pragma: no cover
    import support

from agent_observatory.annotations import GoldEpisode, load_gold_set
from agent_observatory.evaluation import (
    EvaluationConfig,
    HoldoutSplit,
    Prediction,
    audit_split,
    automation_eligibility,
    brier_score,
    evaluate_gold_set,
    grouped_temporal_split,
    load_predictions,
    multi_label_metrics,
    reliability_bins,
    report_json,
    single_label_metrics,
    wilson_lower_bound,
)
from agent_observatory.taxonomy import load_taxonomy

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "gold")
GOLD_PATH = os.path.join(FIXTURES, "gold_episodes_v1.json")
PRED_PATH = os.path.join(FIXTURES, "predictions_jev_v1.json")
V2_PATH = os.path.join(support.PACKAGE_ROOT, "agent_observatory", "taxonomy", "jev_taxonomy_v2.json")


def make_episode(episode_id, group_key, observed_at, primary="bugfix", flags=()):
    return GoldEpisode(
        episode_id=episode_id,
        group_key=group_key,
        observed_at=observed_at,
        provider="codex",
        labels={"primary_intent": (primary,)},
        annotator="test",
        flags=frozenset(flags),
    )


class MetricPrimitiveTest(unittest.TestCase):
    def test_single_label_metrics_exact(self):
        pairs = [
            ("bugfix", "bugfix"),
            ("bugfix", "implementation"),
            ("implementation", "implementation"),
            ("implementation", None),
        ]
        metrics = single_label_metrics(pairs)
        self.assertAlmostEqual(metrics["accuracy"], 0.5)
        self.assertAlmostEqual(metrics["coverage"], 0.75)
        self.assertEqual(metrics["abstentions"], 1)
        bugfix = metrics["per_class"]["bugfix"]
        self.assertAlmostEqual(bugfix["precision"], 1.0)
        self.assertAlmostEqual(bugfix["recall"], 0.5)
        self.assertAlmostEqual(bugfix["f1"], 2 / 3)
        implementation = metrics["per_class"]["implementation"]
        self.assertAlmostEqual(implementation["precision"], 0.5)
        self.assertAlmostEqual(implementation["recall"], 0.5)
        self.assertAlmostEqual(metrics["macro_f1"], (2 / 3 + 0.5) / 2)

    def test_single_label_metrics_never_inflate_precision_with_abstention(self):
        metrics = single_label_metrics([("bugfix", None), ("bugfix", None)])
        self.assertAlmostEqual(metrics["coverage"], 0.0)
        self.assertAlmostEqual(metrics["per_class"]["bugfix"]["precision"], 0.0)
        self.assertAlmostEqual(metrics["per_class"]["bugfix"]["recall"], 0.0)

    def test_multi_label_metrics_exact(self):
        metrics = multi_label_metrics([(["a", "b"], ["a"]), (["a"], ["a", "b"])])
        self.assertAlmostEqual(metrics["per_label"]["a"]["f1"], 1.0)
        self.assertAlmostEqual(metrics["per_label"]["b"]["f1"], 0.0)
        self.assertAlmostEqual(metrics["macro_f1"], 0.5)
        self.assertAlmostEqual(metrics["micro_f1"], 2 / 3)
        self.assertAlmostEqual(metrics["exact_match"], 0.0)

    def test_brier_score(self):
        self.assertAlmostEqual(brier_score([{"a": 1.0, "b": 0.0}], ["a"], ["a", "b"]), 0.0)
        self.assertAlmostEqual(
            brier_score([{"a": 0.5, "b": 0.5}], ["a"], ["a", "b"]), 0.5
        )
        self.assertIsNone(brier_score([], [], ["a"]))

    def test_reliability_error(self):
        result = reliability_bins([0.9, 0.9, 0.1, 0.1], [True, True, False, False], bins=2)
        self.assertAlmostEqual(result["expected_calibration_error"], 0.1)
        self.assertEqual(result["samples"], 4)
        self.assertEqual(result["bins"][1]["count"], 2)

    def test_wilson_lower_bound(self):
        bound = wilson_lower_bound(9, 10)
        self.assertLess(bound, 0.9)
        self.assertGreater(bound, 0.5)
        self.assertIsNone(wilson_lower_bound(0, 0))


class SplitTest(unittest.TestCase):
    def _episodes(self):
        return [
            make_episode("e1", "g1", "2026-09-01T00:00:00Z"),
            make_episode("e2", "g1", "2026-09-02T00:00:00Z"),
            make_episode("e3", "g2", "2026-09-03T00:00:00Z"),
            make_episode("e4", "g2", "2026-09-04T00:00:00Z"),
            make_episode("e5", "g3", "2026-09-05T00:00:00Z"),
            make_episode("e6", "g3", "2026-09-06T00:00:00Z"),
        ]

    def test_grouped_temporal_split_is_deterministic_and_leak_free(self):
        first = grouped_temporal_split(self._episodes(), holdout_fraction=0.5)
        second = grouped_temporal_split(self._episodes(), holdout_fraction=0.5)
        self.assertEqual(first.tuning_groups, second.tuning_groups)
        self.assertEqual(first.holdout_groups, second.holdout_groups)
        audit = audit_split(first)
        self.assertTrue(audit["leak_free"])
        self.assertTrue(audit["temporal_order_ok"])
        self.assertEqual(audit["violations"], [])
        self.assertEqual(set(first.tuning_groups) & set(first.holdout_groups), set())
        # Whole groups stay together.
        self.assertEqual({ep.group_key for ep in first.tuning}, set(first.tuning_groups))

    def test_audit_flags_group_overlap(self):
        episode = make_episode("e1", "g1", "2026-09-01T00:00:00Z")
        split = HoldoutSplit(
            tuning=(episode,), holdout=(episode,), tuning_groups=("g1",), holdout_groups=("g1",),
            boundary_time="2026-09-01T00:00:00Z",
        )
        audit = audit_split(split)
        self.assertFalse(audit["leak_free"])
        self.assertTrue(any("group appears in both" in violation for violation in audit["violations"]))

    def test_single_group_lands_in_one_partition(self):
        split = grouped_temporal_split([make_episode("e1", "g1", "2026-09-01T00:00:00Z")])
        counts = len(split.tuning) + len(split.holdout)
        self.assertEqual(counts, 1)


class AutomationGateTest(unittest.TestCase):
    def _episode(self, flags=(), primary="bugfix"):
        return make_episode("e1", "g1", "2026-09-01T00:00:00Z", primary=primary, flags=flags)

    def _prediction(self, label="bugfix", confidence=0.95):
        return Prediction(
            episode_id="e1", predictor="jev", labels={"primary_intent": (label,)}, confidence=confidence
        )

    def _eligible(self, episode, prediction, rare=frozenset()):
        return automation_eligibility(
            episode,
            prediction,
            primary_facet="primary_intent",
            rare_labels=frozenset(rare),
            min_class_support=2,
            confidence_threshold=0.9,
        )

    def test_injected_state_never_routes(self):
        self.assertEqual(self._eligible(self._episode(("injected",)), self._prediction()), (False, "injected_state"))

    def test_uncertain_and_contested_never_route(self):
        self.assertEqual(self._eligible(self._episode(("uncertain",)), self._prediction()), (False, "uncertain"))
        self.assertEqual(self._eligible(self._episode(("contested",)), self._prediction()), (False, "uncertain"))

    def test_rare_case_never_routes(self):
        self.assertEqual(self._eligible(self._episode(("rare",)), self._prediction()), (False, "rare_class"))
        self.assertEqual(
            self._eligible(self._episode(), self._prediction(), rare={"bugfix"}), (False, "rare_class")
        )

    def test_low_confidence_and_unknown_abstain(self):
        self.assertEqual(self._eligible(self._episode(), self._prediction(confidence=0.5)), (False, "low_confidence"))
        self.assertEqual(self._eligible(self._episode(), self._prediction(label="unknown")), (False, "unknown_label"))
        self.assertEqual(self._eligible(self._episode(), None), (False, "no_prediction"))

    def test_common_confident_case_is_eligible(self):
        self.assertEqual(self._eligible(self._episode(), self._prediction()), (True, "eligible"))


class EndToEndFixtureTest(unittest.TestCase):
    def setUp(self):
        self.taxonomy = load_taxonomy(V2_PATH)
        self.gold_set = load_gold_set(GOLD_PATH, self.taxonomy)
        self.predictions = load_predictions(PRED_PATH, self.taxonomy)
        self.config = EvaluationConfig(confidence_threshold=0.9, min_class_support=2)

    def test_loads_predictions_with_facets(self):
        self.assertEqual(len(self.predictions), 14)
        self.assertEqual(self.predictions[0].predictor, "jev")
        self.assertEqual(self.predictions[0].model, "jev-1.13.0")

    def test_report_is_leak_free_and_scores_model_and_baselines(self):
        report = evaluate_gold_set(
            self.gold_set, {"jev": self.predictions}, self.taxonomy, self.config
        )
        self.assertTrue(report["split"]["leak_free"])
        self.assertTrue(report["split"]["temporal_order_ok"])
        self.assertEqual(report["split"]["violations"], [])
        self.assertEqual(set(report["evaluations"]), {"jev", "title_only", "metadata_only"})
        for name in ("jev", "title_only", "metadata_only"):
            per_class = report["evaluations"][name]["holdout"]["primary"]["per_class"]
            self.assertIn("bugfix", per_class)
        # Jev is correct on this pinned mechanics holdout.
        self.assertAlmostEqual(report["evaluations"]["jev"]["holdout"]["primary"]["accuracy"], 1.0)
        self.assertAlmostEqual(report["evaluations"]["jev"]["holdout"]["primary"]["coverage"], 1.0)

    def test_flagged_cases_cannot_auto_route(self):
        report = evaluate_gold_set(
            self.gold_set, {"jev": self.predictions}, self.taxonomy, self.config
        )
        gate = report["evaluations"]["jev"]["holdout"]["automation_gate"]
        self.assertEqual(gate["violations"], [])
        self.assertEqual(gate["eligible_count"], 2)
        reasons = {item["episode_id"]: item["reason"] for item in gate["abstained"]}
        self.assertEqual(reasons["ep-research-injected-9"], "injected_state")
        self.assertEqual(reasons["ep-unknown-uncertain-10"], "uncertain")
        self.assertEqual(reasons["ep-rare-flake-11"], "rare_class")
        self.assertEqual(reasons["ep-contested-refactor-12"], "uncertain")

    def test_calibration_is_recorded(self):
        report = evaluate_gold_set(
            self.gold_set, {"jev": self.predictions}, self.taxonomy, self.config
        )
        calibration = report["evaluations"]["jev"]["calibration"]
        self.assertIsNotNone(calibration["brier_score"])
        self.assertIsNotNone(calibration["reliability"])
        self.assertGreater(calibration["confidence_samples"], 0)

    def test_report_is_reproducible(self):
        first = evaluate_gold_set(self.gold_set, {"jev": self.predictions}, self.taxonomy, self.config)
        second = evaluate_gold_set(self.gold_set, {"jev": self.predictions}, self.taxonomy, self.config)
        self.assertEqual(first["evaluation_hash"], second["evaluation_hash"])
        self.assertEqual(report_json(first), report_json(second))
        self.assertIsInstance(json.loads(report_json(first)), dict)

    def test_precision_lower_bounds_are_reported(self):
        report = evaluate_gold_set(
            self.gold_set, {"jev": self.predictions}, self.taxonomy, self.config
        )
        bounds = report["evaluations"]["jev"]["holdout"]["primary_precision_lower_bound"]
        self.assertIn("bugfix", bounds)
        self.assertIsNotNone(bounds["bugfix"])

    def test_evaluation_hash_tracks_predictions(self):
        import dataclasses

        changed = dataclasses.replace(self.predictions[0], confidence=0.5)
        baseline = evaluate_gold_set(self.gold_set, {"jev": self.predictions}, self.taxonomy, self.config)
        altered = evaluate_gold_set(
            self.gold_set, {"jev": (changed,) + self.predictions[1:]}, self.taxonomy, self.config
        )
        self.assertNotEqual(baseline["evaluation_hash"], altered["evaluation_hash"])


class EvaluateCliTest(unittest.TestCase):
    def test_cli_writes_report(self):
        from agent_observatory.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.json")
            status = main(
                [
                    "evaluate",
                    "--gold",
                    GOLD_PATH,
                    "--predictions",
                    PRED_PATH,
                    "--taxonomy",
                    V2_PATH,
                    "--out",
                    out,
                ]
            )
            self.assertEqual(status, 0)
            with open(out, encoding="utf-8") as handle:
                report = json.load(handle)
            self.assertTrue(report["split"]["leak_free"])
            self.assertIn("jev", report["evaluations"])


if __name__ == "__main__":
    unittest.main()
