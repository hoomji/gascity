"""Tests for the M8b advisory live-routing layer.

Pins the four properties the brief calls out:

* the registration is bound to ``canary-register`` output (hash checked before
  start; refuse on mismatch);
* request accounting is re-derived at the live call site and counts retries and
  batch items;
* the kill switch is re-polled inside every long-running loop;
* the layer never changes a route: ``applied_route`` is always the actual
  route, and a missing classification abstains instead of guessing.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

try:
    from . import support
except ImportError:  # pragma: no cover
    import support

from agent_observatory.canary import CanaryError, normalize_registration, verify_registration_output
from agent_observatory.errors import LiveRoutingError
from agent_observatory.live_routing import (
    ClassificationOutcome,
    LiveDispatch,
    LiveRoutingAdvisor,
    SingleAttemptTransportClassifier,
    load_batch_request,
)
from agent_observatory.policy import load_catalog

PACKAGE_ROOT = support.PACKAGE_ROOT
POLICY_FIXTURES = os.path.join(PACKAGE_ROOT, "tests", "fixtures", "policy")
CATALOG = os.path.join(POLICY_FIXTURES, "catalog.json")

RECORDED_AT = "2026-09-26T12:00:00Z"


def registration(**overrides):
    raw = {
        "schema_version": "1.0",
        "registration_id": "canary-live-1",
        "policy_id": "context-full-for-planning-v1",
        "policy_kind": "context_bundle",
        "treatment_candidate": "context:full",
        "prior_candidate": "context:minimal",
        "catalog_version": "2026-09-24",
        "seed": "seed-live-1",
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


def classification(**overrides):
    raw = {
        "episode_id": "dispatch-1",
        "as_of": RECORDED_AT,
        "observed_at": RECORDED_AT,
        "intent": "planning_spec",
        "scope": "large",
        "confidence": 0.95,
        "repo": "gascity",
    }
    raw.update(overrides)
    return raw


def dispatch(**overrides):
    raw = {
        "dispatch_id": "dispatch-1",
        "bead_id": "gl-abc123",
        "actual_route": "context:minimal",
        "recorded_at": RECORDED_AT,
        "repo": "gascity",
    }
    raw.update(overrides)
    return LiveDispatch.from_mapping(raw)


class RegistrationBindingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write(self, raw, name="registration.json"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(raw, handle)
        return path, normalize_registration(raw).registration_hash()

    def test_accepts_register_time_hash(self):
        path, digest = self._write(registration())
        loaded, actual = verify_registration_output(path, digest)
        self.assertEqual(actual, digest)
        self.assertEqual(loaded.registration_id, "canary-live-1")

    def test_accepts_sidecar_hash(self):
        path, digest = self._write(registration())
        with open(path + ".hash", "w", encoding="utf-8") as handle:
            handle.write(digest + "\n")
        _, actual = verify_registration_output(path, None)
        self.assertEqual(actual, digest)

    def test_refuses_mismatched_hash(self):
        path, _ = self._write(registration())
        with self.assertRaises(CanaryError):
            verify_registration_output(path, "0" * 64)

    def test_refuses_unbound_registration(self):
        path, _ = self._write(registration())
        with self.assertRaises(CanaryError):
            verify_registration_output(path, None)

    def test_refuses_catalog_version_not_registered(self):
        path, digest = self._write(registration(catalog_version="some-other-catalog"))
        with self.assertRaises(LiveRoutingError):
            LiveRoutingAdvisor.open(
                registration_path=path,
                catalog_path=CATALOG,
                ledger_path=os.path.join(self.tmp.name, "ledger.jsonl"),
                expected_registration_hash=digest,
            )

    def test_advisor_refuses_hash_mismatch_before_writing(self):
        path, _ = self._write(registration())
        ledger = os.path.join(self.tmp.name, "ledger.jsonl")
        with self.assertRaises(CanaryError):
            LiveRoutingAdvisor.open(
                registration_path=path,
                catalog_path=CATALOG,
                ledger_path=ledger,
                expected_registration_hash="f" * 64,
            )
        self.assertFalse(os.path.exists(ledger))

    def test_max_requests_respects_owner_ceiling(self):
        path, digest = self._write(registration())
        with self.assertRaises(LiveRoutingError):
            LiveRoutingAdvisor.open(
                registration_path=path,
                catalog_path=CATALOG,
                ledger_path=os.path.join(self.tmp.name, "ledger.jsonl"),
                expected_registration_hash=digest,
                max_requests=51,
            )


class LiveRoutingAdvisorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "registration.json")
        self.ledger = os.path.join(self.tmp.name, "ledger.jsonl")
        self.kill_switch = os.path.join(self.tmp.name, "kill-switch")

    def advisor(self, *, expected_hash=None, max_requests=None, max_attempts=3, **overrides):
        raw = registration(**overrides)
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(raw, handle)
        digest = normalize_registration(raw).registration_hash()
        return LiveRoutingAdvisor.open(
            registration_path=self.path,
            catalog_path=CATALOG,
            ledger_path=self.ledger,
            expected_registration_hash=digest if expected_hash is None else expected_hash,
            kill_switch_path=self.kill_switch,
            max_requests=max_requests,
            max_attempts=max_attempts,
        )

    def test_missing_classification_is_unknown_and_abstains(self):
        advisor = self.advisor()
        record = advisor.advise(dispatch())
        self.assertIsNone(record["primary_intent"])
        self.assertIsNone(record["suggested_route"])
        self.assertTrue(record["unknown_as_abstain"])
        self.assertEqual(record["applied_route"], record["actual_route"])
        self.assertFalse(record["route_changed"])
        self.assertFalse(record["executes_changes"])
        self.assertEqual(record["requests_charged"], 0)
        self.assertEqual(advisor.ledger.requests_spent(), 0)

    def test_supplied_classification_recommends_without_a_request(self):
        advisor = self.advisor()
        record = advisor.advise(dispatch(classification=classification()))
        self.assertEqual(record["primary_intent"], "planning_spec")
        self.assertEqual(record["suggested_route"], "context:full")
        self.assertEqual(record["classification_source"], "supplied")
        self.assertEqual(record["applied_route"], "context:minimal")
        self.assertEqual(record["requests_charged"], 0)
        self.assertEqual(advisor.ledger.requests_spent(), 0)

    def test_retries_are_charged_at_the_live_call_site(self):
        advisor = self.advisor(max_attempts=5)
        attempts = {"n": 0}

        def flaky(_dispatch):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise LiveRoutingError("transient")
            return ClassificationOutcome(intent="planning_spec", confidence=0.9)

        record = advisor.advise(dispatch(), flaky)
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(advisor.ledger.requests_spent(), 3)
        self.assertEqual(record["classification_source"], "jev")
        self.assertEqual(record["suggested_route"], "context:full")
        request_attempts = [
            entry["attempt"]
            for entry in advisor.ledger.entries()
            if entry.get("kind") == "live_routing_request"
        ]
        self.assertEqual(request_attempts, [1, 2, 3])

    def test_budget_is_re_derived_and_never_overspent(self):
        advisor = self.advisor(max_requests=1, max_attempts=3)

        def always_fail(_dispatch):
            raise LiveRoutingError("no")

        record = advisor.advise(dispatch(), always_fail)
        self.assertEqual(advisor.ledger.requests_spent(), 1)
        self.assertIsNone(record["primary_intent"])
        self.assertEqual(record["requests_charged"], 1)

    def test_kill_switch_repolled_inside_retry_loop(self):
        advisor = self.advisor(max_attempts=5)

        def engages_the_switch(_dispatch):
            with open(self.kill_switch, "w", encoding="utf-8") as handle:
                handle.write("stop\n")
            raise LiveRoutingError("stop")

        record = advisor.advise(dispatch(), engages_the_switch)
        self.assertEqual(advisor.ledger.requests_spent(), 1)
        self.assertTrue(record["kill_switch"])
        self.assertTrue(record["unknown_as_abstain"])

    def test_kill_switch_engaged_before_call_spends_nothing(self):
        with open(self.kill_switch, "w", encoding="utf-8") as handle:
            handle.write("stop\n")
        advisor = self.advisor()

        def should_not_run(_dispatch):  # pragma: no cover - asserted by call count
            raise AssertionError("classifier must not run while the kill switch is engaged")

        record = advisor.advise(dispatch(), should_not_run)
        self.assertEqual(advisor.ledger.requests_spent(), 0)
        self.assertTrue(record["kill_switch"])
        self.assertEqual(record["applied_route"], record["actual_route"])

    def test_batch_shares_one_budget_and_records_batch_position(self):
        advisor = self.advisor(max_requests=2, max_attempts=1)
        calls = {"n": 0}

        def succeed(_dispatch):
            calls["n"] += 1
            return ClassificationOutcome(intent="planning_spec", confidence=0.9)

        first = dispatch(dispatch_id="d1", bead_id="gl-1")
        second = dispatch(dispatch_id="d2", bead_id="gl-2")
        records = advisor.advise_batch([first, second], succeed)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(advisor.ledger.requests_spent(), 2)
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertEqual(record["applied_route"], "context:minimal")
            self.assertFalse(record["route_changed"])

    def test_parse_batch_request_assigns_positions(self):
        dispatches = load_batch_request(
            {
                "batch_id": "batch-1",
                "dispatches": [
                    {"dispatch_id": "d1", "bead_id": "b1", "actual_route": "r1", "recorded_at": RECORDED_AT},
                    {"dispatch_id": "d2", "bead_id": "b2", "actual_route": "r2", "recorded_at": RECORDED_AT},
                ],
            }
        )
        self.assertEqual([d.batch_index for d in dispatches], [0, 1])
        self.assertTrue(all(d.batch_id == "batch-1" for d in dispatches))

    def test_advise_is_deterministic_for_identical_inputs(self):
        advisor = self.advisor()
        first = advisor.advise(dispatch(classification=classification()))
        second = advisor.advise(dispatch(classification=classification()))
        self.assertEqual(first["advisory_hash"], second["advisory_hash"])


class CatalogVersionTest(unittest.TestCase):
    def test_catalog_fixture_version_matches_registration_helper(self):
        self.assertEqual(load_catalog(CATALOG).catalog_version, registration()["catalog_version"])


class _FakeRequest:
    def __init__(self, body):
        self.body = body


class _FakeTransport:
    def __init__(self, response, attempts=1):
        self._response = response
        self._attempts = attempts
        self.requests = []

    def send(self, request):
        from agent_observatory.transport import SendResult

        self.requests.append(request)
        return SendResult(ok=True, response=self._response, attempts=self._attempts)


class SingleAttemptTransportClassifierTest(unittest.TestCase):
    def test_reads_primary_intent_from_the_validated_answer(self):
        body = {
            "model": "jev-model",
            "questions": {
                "primary_intent": {
                    "type": "choice",
                    "criteria": {"planning_spec": "planning", "unknown": "unknown"},
                }
            },
        }
        response = {
            "model": "jev-model",
            "usage": {"input_tokens": 3, "output_tokens": 2},
            "answers": {
                "primary_intent": {
                    "type": "choice",
                    "choice": "planning_spec",
                    "confidence": 0.9,
                    "probabilities": {"planning_spec": 1.0, "unknown": 0.0},
                }
            },
        }
        transport = _FakeTransport(response, attempts=1)
        classifier = SingleAttemptTransportClassifier(transport, lambda _dispatch: _FakeRequest(body))
        outcome = classifier(dispatch())
        self.assertEqual(outcome.intent, "planning_spec")
        self.assertAlmostEqual(outcome.confidence, 0.9)
        self.assertEqual(outcome.attempts, 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
