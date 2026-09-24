"""Tests for the M8 controlled, reversible policy canary.

The suite pins the acceptance properties:

* the canary is opt-in and the prior policy is the default; a kill switch
  restores it;
* assignment is seeded, deterministic, logged and balanced within strata;
* live classification requests are bounded by ``--max-requests`` up to the
  owner-approved cap, and units beyond the cap stay on the prior policy;
* the outcome report carries exposure, control/treatment quality and net
  effect, and never claims an improvement the pre-registered design and data do
  not support.
"""

from __future__ import annotations

import json
import os
import unittest

try:
    from . import support
except ImportError:  # pragma: no cover
    import support

from agent_observatory.canary import (
    MAX_ALLOWED_REQUESTS,
    CanaryError,
    assign_arms,
    assignment_digest,
    build_canary_report,
    load_canary_bundle,
    load_registration,
    normalize_canary_bundle,
    normalize_registration,
)
from agent_observatory.policy import load_catalog

PACKAGE_ROOT = support.PACKAGE_ROOT
FIXTURES = os.path.join(PACKAGE_ROOT, "tests", "fixtures", "canary")
POLICY_FIXTURES = os.path.join(PACKAGE_ROOT, "tests", "fixtures", "policy")

CATALOG = load_catalog(os.path.join(POLICY_FIXTURES, "catalog.json"))

AS_OF = "2026-09-24T00:00:00Z"
OBSERVED = "2026-09-23T00:00:00Z"


def registration(**overrides):
    raw = {
        "schema_version": "1.0",
        "registration_id": "canary-1",
        "policy_id": "context-full-for-planning-v1",
        "policy_kind": "context_bundle",
        "treatment_candidate": "context:full",
        "prior_candidate": "context:minimal",
        "catalog_version": "cat-1",
        "seed": "seed-1",
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


def record(episode_id, **overrides):
    raw = {
        "episode_id": episode_id,
        "as_of": AS_OF,
        "observed_at": OBSERVED,
        "intent": "planning_spec",
        "scope": "large",
        "confidence": 0.95,
        "repo": "gascity",
        "current_routing": {"formula": "formula:planning"},
    }
    raw.update(overrides)
    return raw


def unit(unit_id, **overrides):
    raw = {
        "unit_id": unit_id,
        "stratum": "repo=gascity|intent=planning_spec",
        "record": record("e-" + unit_id),
    }
    raw.update(overrides)
    return raw


def bundle(units, **overrides):
    raw = {"schema_version": "1.0", "units": units}
    raw.update(overrides)
    return raw


def outcome(**overrides):
    raw = {"accepted": True, "time_to_accepted_seconds": 100.0, "cost_usd": 1.0, "quality_score": 1.0}
    raw.update(overrides)
    return raw


def with_outcomes(reg, values, *, n=8):
    """Build a bundle whose treatment/control outcomes follow the real assignment.

    *values* is a ``(control_value, treatment_value)`` pair so a test can pin a
    directional effect without knowing which unit the seed assigns where.
    """
    blank = normalize_canary_bundle(bundle([unit(f"u{i:02d}") for i in range(1, n + 1)]))
    arms = {uid: item["arm"] for uid, item in assign_arms(blank.units, reg).items()}
    units = []
    for i in range(1, n + 1):
        uid = f"u{i:02d}"
        value = values[0] if arms[uid] == "control" else values[1]
        units.append(unit(uid, outcome=outcome(time_to_accepted_seconds=value)))
    return normalize_canary_bundle(bundle(units))


class RegistrationValidationTest(unittest.TestCase):
    def test_accepts_defaults(self):
        reg = normalize_registration(registration())
        self.assertEqual(reg.policy_kind, "context_bundle")
        self.assertEqual(reg.max_requests, MAX_ALLOWED_REQUESTS)
        self.assertTrue(reg.registration_hash())

    def test_rejects_unknown_key(self):
        with self.assertRaises(CanaryError):
            normalize_registration(registration(nope=1))

    def test_rejects_bad_schema_version(self):
        with self.assertRaises(CanaryError):
            normalize_registration(registration(schema_version="2.0"))

    def test_rejects_unknown_policy_kind(self):
        with self.assertRaises(CanaryError):
            normalize_registration(registration(policy_kind="spaceship"))

    def test_rejects_treatment_fraction_out_of_range(self):
        with self.assertRaises(CanaryError):
            normalize_registration(registration(treatment_fraction=1.0))
        with self.assertRaises(CanaryError):
            normalize_registration(registration(treatment_fraction=0.0))

    def test_rejects_request_cap_above_owner_ceiling(self):
        with self.assertRaises(CanaryError):
            normalize_registration(registration(max_requests=MAX_ALLOWED_REQUESTS + 1))

    def test_rejects_unknown_outcome(self):
        with self.assertRaises(CanaryError):
            normalize_registration(registration(primary_outcome="vibes"))

    def test_rejects_zero_sample_size(self):
        with self.assertRaises(CanaryError):
            normalize_registration(registration(sample_size=0))

    def test_rejects_naive_created_at(self):
        with self.assertRaises(CanaryError):
            normalize_registration(registration(created_at="2026-09-24T22:00:00"))

    def test_hash_is_stable_and_seed_sensitive(self):
        first = normalize_registration(registration()).registration_hash()
        second = normalize_registration(registration()).registration_hash()
        other = normalize_registration(registration(seed="seed-2")).registration_hash()
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)

    def test_loads_committed_fixture(self):
        reg = load_registration(os.path.join(FIXTURES, "registration.json"))
        self.assertEqual(reg.treatment_candidate, "context:full")
        self.assertIn("disagreement_rows", reg.evidence)


class BundleValidationTest(unittest.TestCase):
    def test_rejects_duplicate_unit_ids(self):
        with self.assertRaises(CanaryError):
            normalize_canary_bundle(bundle([unit("u1"), unit("u1")]))

    def test_rejects_unknown_unit_key(self):
        with self.assertRaises(CanaryError):
            normalize_canary_bundle(bundle([dict(unit("u1"), nope=1)]))

    def test_rejects_unknown_outcome_key(self):
        with self.assertRaises(CanaryError):
            normalize_canary_bundle(bundle([unit("u1", outcome=dict(outcome(), nope=1))]))

    def test_rejects_negative_outcome(self):
        with self.assertRaises(CanaryError):
            normalize_canary_bundle(bundle([unit("u1", outcome=outcome(cost_usd=-1.0))]))

    def test_rejects_bad_accepted_type(self):
        with self.assertRaises(CanaryError):
            normalize_canary_bundle(bundle([unit("u1", outcome=outcome(accepted="yes"))]))

    def test_dataset_hash_is_stable(self):
        first = normalize_canary_bundle(bundle([unit("u1")])).dataset_hash()
        second = normalize_canary_bundle(bundle([unit("u1")])).dataset_hash()
        self.assertEqual(first, second)

    def test_loads_committed_bundle(self):
        loaded = load_canary_bundle(os.path.join(FIXTURES, "units.json"))
        self.assertEqual(len(loaded.units), 8)


class AssignmentTest(unittest.TestCase):
    def setUp(self):
        self.reg = normalize_registration(registration())
        self.units = normalize_canary_bundle(bundle([unit(f"u{i}") for i in range(8)])).units

    def test_assignment_is_deterministic(self):
        first = assign_arms(self.units, self.reg)
        second = assign_arms(self.units, self.reg)
        self.assertEqual(first, second)

    def test_different_seed_changes_assignment(self):
        other = normalize_registration(registration(seed="seed-2"))
        self.assertNotEqual(assign_arms(self.units, self.reg), assign_arms(self.units, other))

    def test_assignment_is_balanced(self):
        assignments = assign_arms(self.units, self.reg)
        arms = [item["arm"] for item in assignments.values()]
        self.assertEqual(arms.count("control"), 4)
        self.assertEqual(arms.count("treatment"), 4)

    def test_assignment_logs_seed_and_digest(self):
        assignments = assign_arms(self.units, self.reg)
        digest = assignment_digest(self.reg.seed, "repo=gascity|intent=planning_spec", "u0")
        self.assertEqual(assignments["u0"]["digest"], digest)

    def test_single_unit_stratum_is_control(self):
        solo = normalize_canary_bundle(bundle([unit("only", stratum="solo")]))
        assignments = assign_arms(solo.units, self.reg)
        self.assertEqual(assignments["only"]["arm"], "control")

    def test_two_unit_stratum_keeps_both_arms(self):
        pair = normalize_canary_bundle(bundle([unit("a", stratum="pair"), unit("b", stratum="pair")]))
        assignments = assign_arms(pair.units, self.reg)
        self.assertEqual({item["arm"] for item in assignments.values()}, {"control", "treatment"})


class OptInAndKillSwitchTest(unittest.TestCase):
    def setUp(self):
        self.reg = normalize_registration(registration())
        self.bundle = normalize_canary_bundle(bundle([unit(f"u{i}") for i in range(6)]))

    def test_disabled_defaults_to_prior_policy(self):
        report = build_canary_report(self.bundle, CATALOG, self.reg, enabled=False)
        canary = report["canary"]
        self.assertEqual(canary["mode"], "disabled")
        self.assertTrue(all(not row["applied_treatment"] for row in canary["assignment_ledger"]))
        self.assertTrue(
            all(row["applied_candidate"] == row["prior_candidate"] for row in canary["assignment_ledger"])
        )
        self.assertEqual(canary["exposure"]["assigned_treatment"], 0)
        self.assertFalse(canary["executes_changes"])

    def test_enabled_applies_treatment_to_planned_arm(self):
        report = build_canary_report(self.bundle, CATALOG, self.reg, enabled=True)
        canary = report["canary"]
        self.assertEqual(canary["mode"], "randomized")
        treated = [row for row in canary["assignment_ledger"] if row["planned_arm"] == "treatment"]
        self.assertTrue(treated)
        self.assertTrue(all(row["applied_candidate"] == "context:full" for row in treated))

    def test_kill_switch_restores_prior_policy(self):
        enabled = build_canary_report(self.bundle, CATALOG, self.reg, enabled=True)
        rolled_back = build_canary_report(self.bundle, CATALOG, self.reg, enabled=True, kill_switch=True)
        # The planned assignment is retained for audit ...
        self.assertEqual(
            [row["planned_arm"] for row in enabled["canary"]["assignment_ledger"]],
            [row["planned_arm"] for row in rolled_back["canary"]["assignment_ledger"]],
        )
        # ... but no treatment is applied once the switch is engaged.
        self.assertEqual(rolled_back["canary"]["mode"], "rolled_back")
        self.assertTrue(
            all(
                not row["applied_treatment"]
                and row["applied_candidate"] == row["prior_candidate"]
                for row in rolled_back["canary"]["assignment_ledger"]
            )
        )

    def test_kill_switch_without_enable_stays_disabled(self):
        report = build_canary_report(self.bundle, CATALOG, self.reg, enabled=False, kill_switch=True)
        self.assertEqual(report["canary"]["mode"], "disabled")

    def test_disabled_run_does_not_recommend_stop(self):
        # A disabled canary assigns no unit, so the sample-size guardrails were
        # never evaluated and there is no live run to stop (F1).
        canary = build_canary_report(self.bundle, CATALOG, self.reg, enabled=False)["canary"]
        self.assertEqual(canary["mode"], "disabled")
        self.assertFalse(canary["stop_recommended"])
        self.assertFalse(canary["guardrails"]["evaluated"]["value"])
        self.assertFalse(canary["guardrails"]["stop_recommended"]["value"])
        self.assertIsNone(canary["guardrails"]["min_control_n"]["pass"])
        self.assertIsNone(canary["guardrails"]["min_treatment_n"]["pass"])

    def test_rolled_back_run_does_not_recommend_stop(self):
        canary = build_canary_report(
            self.bundle, CATALOG, self.reg, enabled=True, kill_switch=True
        )["canary"]
        self.assertEqual(canary["mode"], "rolled_back")
        self.assertFalse(canary["stop_recommended"])
        self.assertFalse(canary["guardrails"]["evaluated"]["value"])


class CostCapTest(unittest.TestCase):
    def setUp(self):
        self.reg = normalize_registration(registration(max_requests=2))
        self.bundle = normalize_canary_bundle(bundle([unit(f"q{i}") for i in range(4)]))

    def _classify_requires(self):
        raw = []
        for i in range(4):
            raw.append(unit(f"q{i}", requires_classification=True))
        return normalize_canary_bundle(bundle(raw))

    def test_cap_defers_units_beyond_budget(self):
        report = build_canary_report(self._classify_requires(), CATALOG, self.reg, enabled=True)
        budget = report["canary"]["classification_budget"]
        self.assertEqual(budget["requests_used"], 2)
        self.assertTrue(budget["requests_capped"])
        self.assertEqual(budget["deferred_units"], ["q2", "q3"])
        deferred = [
            row for row in report["canary"]["assignment_ledger"] if row["unit_id"] in ("q2", "q3")
        ]
        self.assertTrue(all(row["reason"] == "classification_budget_deferred" for row in deferred))
        self.assertTrue(all(row["applied_candidate"] == row["prior_candidate"] for row in deferred))

    def test_zero_cap_defers_everything(self):
        report = build_canary_report(
            self._classify_requires(), CATALOG, self.reg, enabled=True, max_requests=0
        )
        budget = report["canary"]["classification_budget"]
        self.assertEqual(budget["requests_used"], 0)
        self.assertEqual(budget["deferred_units"], ["q0", "q1", "q2", "q3"])

    def test_cli_flag_is_clamped_to_registration_cap(self):
        report = build_canary_report(
            self._classify_requires(), CATALOG, self.reg, enabled=True, max_requests=50
        )
        budget = report["canary"]["classification_budget"]
        self.assertEqual(budget["max_requests_effective"], 2)
        self.assertTrue(budget["clamped_to_registration"])

    def test_above_owner_ceiling_is_refused(self):
        with self.assertRaises(CanaryError):
            build_canary_report(
                self.bundle, CATALOG, self.reg, enabled=True, max_requests=MAX_ALLOWED_REQUESTS + 1
            )

    def test_preclassified_units_cost_zero_requests(self):
        report = build_canary_report(self.bundle, CATALOG, self.reg, enabled=True)
        self.assertEqual(report["canary"]["classification_budget"]["requests_used"], 0)


class EligibilityTest(unittest.TestCase):
    def setUp(self):
        self.reg = normalize_registration(registration())

    def _report_for(self, raw_unit):
        loaded = normalize_canary_bundle(bundle([raw_unit]))
        return build_canary_report(loaded, CATALOG, self.reg, enabled=True)["canary"]["assignment_ledger"][0]

    def test_unknown_intent_is_ineligible(self):
        row = self._report_for(unit("u1", record=record("e1", intent="unknown")))
        self.assertFalse(row["eligible"])
        self.assertEqual(row["applied_candidate"], row["prior_candidate"])

    def test_low_confidence_is_ineligible(self):
        row = self._report_for(unit("u1", record=record("e1", confidence=0.2)))
        self.assertFalse(row["eligible"])

    def test_injected_flag_is_ineligible(self):
        row = self._report_for(unit("u1", record=record("e1", flags=["injected"])))
        self.assertFalse(row["eligible"])

    def test_wrong_treatment_candidate_is_ineligible(self):
        # context:minimal serves implementation/bugfix, not planning_spec, so a
        # canary registered on context:minimal cannot treat these units.
        reg = normalize_registration(registration(treatment_candidate="context:minimal"))
        loaded = normalize_canary_bundle(bundle([unit("u1")]))
        row = build_canary_report(loaded, CATALOG, reg, enabled=True)["canary"]["assignment_ledger"][0]
        self.assertFalse(row["eligible"])

    def test_prior_candidate_from_current_routing_is_honored(self):
        raw = unit("u1")
        raw["prior_candidate"] = "context:legacy"
        row = self._report_for(raw)
        self.assertEqual(row["prior_candidate"], "context:legacy")
        self.assertEqual(row["applied_candidate"], "context:legacy")


class OutcomeReportTest(unittest.TestCase):
    def setUp(self):
        self.reg = normalize_registration(registration(sample_size=4))

    def test_report_is_deterministic(self):
        bundle = with_outcomes(self.reg, (1000.0, 100.0))
        first = build_canary_report(bundle, CATALOG, self.reg, enabled=True)
        second = build_canary_report(bundle, CATALOG, self.reg, enabled=True)
        self.assertEqual(first["report_hash"], second["report_hash"])
        self.assertEqual(first, second)

    def test_exposure_counts(self):
        bundle = with_outcomes(self.reg, (1000.0, 100.0))
        canary = build_canary_report(bundle, CATALOG, self.reg, enabled=True)["canary"]
        exposure = canary["exposure"]
        self.assertEqual(exposure["units_total"], 8)
        self.assertEqual(exposure["eligible_units"], 8)
        self.assertEqual(exposure["assigned_control"] + exposure["assigned_treatment"], 8)
        self.assertEqual(exposure["analyzed_control"], exposure["assigned_control"])
        self.assertTrue(exposure["sample_size_met"])

    def test_beneficial_effect_is_measured_but_only_claimed_when_supported(self):
        bundle = with_outcomes(self.reg, (1000.0, 100.0))
        net = build_canary_report(bundle, CATALOG, self.reg, enabled=True)["canary"]["net_effect"]
        self.assertEqual(net["conclusion"], "observed_improvement")
        self.assertLess(net["absolute_difference"], 0.0)
        self.assertEqual(net["improvement_claim"], "supported_by_randomized_assignment")

    def test_no_effect_makes_no_claim(self):
        bundle = with_outcomes(self.reg, (100.0, 100.0))
        net = build_canary_report(bundle, CATALOG, self.reg, enabled=True)["canary"]["net_effect"]
        self.assertEqual(net["conclusion"], "no_observed_effect")
        self.assertEqual(net["improvement_claim"], "none")

    def test_disabled_run_makes_no_claim(self):
        bundle = with_outcomes(self.reg, (1000.0, 100.0))
        net = build_canary_report(bundle, CATALOG, self.reg, enabled=False)["canary"]["net_effect"]
        self.assertEqual(net["conclusion"], "not_run")
        self.assertEqual(net["improvement_claim"], "none")

    def test_insufficient_sample_makes_no_claim(self):
        reg = normalize_registration(registration(sample_size=100))
        bundle = with_outcomes(reg, (1000.0, 100.0))
        canary = build_canary_report(bundle, CATALOG, reg, enabled=True)["canary"]
        self.assertEqual(canary["net_effect"]["conclusion"], "insufficient_sample")
        self.assertEqual(canary["net_effect"]["improvement_claim"], "none")
        self.assertEqual(canary["attribution"], "insufficient")

    def test_missing_outcomes_are_unknown_not_zero(self):
        units = [unit(f"u{i}", outcome=outcome(missing=True)) for i in range(6)]
        loaded = normalize_canary_bundle(bundle(units))
        canary = build_canary_report(loaded, CATALOG, self.reg, enabled=True)["canary"]
        self.assertEqual(canary["exposure"]["analyzed_control"], 0)
        self.assertEqual(canary["exposure"]["analyzed_treatment"], 0)
        self.assertEqual(canary["net_effect"]["conclusion"], "unmeasured")
        self.assertEqual(canary["net_effect"]["improvement_claim"], "none")
        self.assertEqual(canary["attribution"], "unmeasurable")

    def test_quality_regression_fails_guardrail(self):
        reg = normalize_registration(
            registration(
                sample_size=4,
                primary_outcome="quality_score",
                guardrails={"min_control_n": 1, "min_treatment_n": 1, "max_quality_regression": 0.0},
            )
        )
        units = []
        blank = normalize_canary_bundle(bundle([unit(f"u{i:02d}") for i in range(1, 9)]))
        arms = {uid: item["arm"] for uid, item in assign_arms(blank.units, reg).items()}
        for i in range(1, 9):
            uid = f"u{i:02d}"
            quality = 1.0 if arms[uid] == "control" else 0.5
            units.append(unit(uid, outcome=outcome(quality_score=quality)))
        loaded = normalize_canary_bundle(bundle(units))
        canary = build_canary_report(loaded, CATALOG, reg, enabled=True)["canary"]
        self.assertFalse(canary["guardrails"]["quality_non_inferior"]["pass"])
        self.assertTrue(canary["stop_recommended"])
        self.assertEqual(canary["net_effect"]["conclusion"], "guardrail_failed")
        self.assertEqual(canary["net_effect"]["improvement_claim"], "none")


if __name__ == "__main__":
    unittest.main()
