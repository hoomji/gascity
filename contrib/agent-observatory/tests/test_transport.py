"""Tests for the bounded Jev ``/v1/systemone`` transport.

All HTTP is served by an in-process fake ``urlopen``; nothing here touches the
network. The fake records the request (URL, method, headers, timeout) so the
wire contract, retry policy, budget, and redaction surfaces can be asserted
directly.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import email.utils
import http.client
import io
import json
import logging
import os
import socket
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
from unittest import mock

try:
    from . import support
except ImportError:  # pragma: no cover
    import support

from agent_observatory import cli, load_taxonomy
from agent_observatory.errors import ObservatoryError
from agent_observatory.jev import build_request
from agent_observatory.store import ObservatoryStore
from agent_observatory.transport import (
    DEFAULT_ENDPOINT,
    MAX_TIMEOUT_SECONDS,
    PINNED_MODEL,
    Budget,
    CircuitBreaker,
    CircuitOpenError,
    CredentialError,
    JevTransport,
    ModelDriftError,
    RetryPolicy,
    TransportConfig,
    TransportConfigError,
    classify,
    iter_provenance,
    load_credential,
    mask_secret,
    redact_text,
)

CREDENTIAL = "sk-test-credential-4321"


class FakeResponse:
    """Minimal stand-in for the object returned by ``urlopen``."""

    def __init__(self, status: int = 200, body: object = b"", headers: dict | None = None):
        if isinstance(body, str):
            self._body = body.encode("utf-8")
        elif isinstance(body, bytes):
            self._body = body
        else:
            self._body = json.dumps(body).encode("utf-8")
        self.status = status
        self.headers = headers if headers is not None else {}
        self.closed = False

    def read(self, amt: int | None = None) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status

    def getheader(self, name: str, default: str | None = None):
        return self.headers.get(name, default)

    def close(self) -> None:
        self.closed = True


class FakeHttp:
    """A scripted fake ``urlopen``.

    Each step is a :class:`FakeResponse`, an exception instance to raise, or a
    callable ``(request, timeout) -> response``. With ``repeat_last`` the final
    step is served forever, which is how "always 500" style tests are written.
    """

    def __init__(self, *steps: object, repeat_last: bool = False):
        self.steps = list(steps)
        self.repeat_last = repeat_last
        self.calls: list[dict] = []

    def __call__(self, request, timeout=None):
        self.calls.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "headers": {key.lower(): value for key, value in request.header_items()},
                "timeout": timeout,
                "data": request.data,
            }
        )
        if not self.steps:
            raise AssertionError("unexpected extra HTTP call")
        if len(self.steps) == 1 and self.repeat_last:
            step = self.steps[0]
        else:
            step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            return step(request, timeout)
        return step

    @property
    def call_count(self) -> int:
        return len(self.calls)


class RecordingSleeper:
    def __init__(self):
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _valid_response_json(request) -> str:
    return json.dumps(support.valid_response(request.body))


def _http_error(status: int, body: bytes, headers: dict | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        DEFAULT_ENDPOINT,
        status,
        f"HTTP {status}",
        headers if headers is not None else {},
        io.BytesIO(body),
    )


class TransportTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ObservatoryStore(os.path.join(self.tmp.name, "projection.db"))
        self.addCleanup(self.store.close)
        self.taxonomy = load_taxonomy()
        self.request = build_request(
            {"summary": "fix a bug"}, self.taxonomy, snapshot_hash="a" * 64
        )

    def _classify(self, fake: FakeHttp, **kwargs):
        sleeper = kwargs.pop("sleeper", RecordingSleeper())
        return classify(
            self.store,
            self.request,
            credential=kwargs.pop("credential", CREDENTIAL),
            urlopen=fake,
            sleeper=sleeper,
            random_source=kwargs.pop("random_source", lambda: 0.5),
            **kwargs,
        ), sleeper


class ClassifySuccessTest(TransportTestBase):
    def test_successful_classification_stores_response_usage_and_latency(self):
        fake = FakeHttp(FakeResponse(200, _valid_response_json(self.request)))
        result, sleeper = self._classify(fake)

        self.assertEqual(result.outcome, "classified")
        self.assertEqual(result.deduplicated, False)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.usage, {"input_tokens": 12, "output_tokens": 7})
        self.assertGreaterEqual(result.latency_ms, 0.0)
        self.assertEqual(self.store.classification_count(), 1)
        self.assertEqual(sleeper.delays, [])

        self.assertEqual(fake.calls[0]["url"], DEFAULT_ENDPOINT)
        self.assertEqual(fake.calls[0]["method"], "POST")
        self.assertEqual(fake.calls[0]["timeout"], 30.0)
        self.assertEqual(fake.calls[0]["headers"]["authorization"], f"Bearer {CREDENTIAL}")
        self.assertEqual(fake.calls[0]["headers"]["content-type"], "application/json")
        self.assertEqual(fake.calls[0]["data"], self.request.serialized.encode("utf-8"))

        provenance = iter_provenance(self.store)
        self.assertEqual(len(provenance), 1)
        row = provenance[0]
        self.assertEqual(row["request_hash"], self.request.request_hash)
        self.assertEqual(row["taxonomy_version"], self.request.taxonomy_version)
        self.assertEqual(row["question_hash"], self.request.question_hash)
        self.assertEqual(row["model"], PINNED_MODEL)
        self.assertEqual(row["outcome"], "classified")
        self.assertIsNone(row["failure_class"])
        self.assertIsNotNone(row["latency_ms"])
        self.assertEqual(row["input_tokens"], 12)
        self.assertEqual(row["output_tokens"], 7)
        self.assertIn("input_tokens", row["usage_json"])
        self.assertEqual(row["credential_masked"], mask_secret(CREDENTIAL))

    def test_replayed_response_is_deduplicated_by_the_store(self):
        first, _ = self._classify(FakeHttp(FakeResponse(200, _valid_response_json(self.request))))
        second, _ = self._classify(FakeHttp(FakeResponse(200, _valid_response_json(self.request))))

        self.assertEqual(first.outcome, "classified")
        self.assertFalse(first.deduplicated)
        self.assertTrue(second.deduplicated)
        self.assertEqual(first.classification_id, second.classification_id)
        self.assertEqual(self.store.classification_count(), 1)
        self.assertEqual(len(iter_provenance(self.store)), 2)

    def test_default_timeout_is_thirty_seconds(self):
        self.assertEqual(TransportConfig().timeout_seconds, 30.0)
        fake = FakeHttp(FakeResponse(200, _valid_response_json(self.request)))
        self._classify(fake)
        self.assertEqual(fake.calls[0]["timeout"], 30.0)


class ClassifyValidationFailureTest(TransportTestBase):
    def test_malformed_json_records_pending(self):
        result, _ = self._classify(FakeHttp(FakeResponse(200, "{this is not json")))
        self.assertEqual(result.outcome, "pending")
        self.assertEqual(result.failure_class, "malformed_json")
        self.assertEqual(self.store.classification_count(), 0)
        row = iter_provenance(self.store)[0]
        self.assertEqual(row["outcome"], "pending")
        self.assertEqual(row["request_hash"], self.request.request_hash)
        self.assertEqual(row["model"], PINNED_MODEL)
        self.assertEqual(row["taxonomy_version"], self.request.taxonomy_version)
        self.assertIsNotNone(row["latency_ms"])

    def test_schema_invalid_json_records_pending(self):
        broken = support.valid_response(self.request.body)
        broken["answers"].pop("primary_intent")
        result, _ = self._classify(FakeHttp(FakeResponse(200, json.dumps(broken))))
        self.assertEqual(result.outcome, "pending")
        self.assertEqual(result.failure_class, "schema_invalid")
        self.assertEqual(self.store.classification_count(), 0)

    def test_model_mismatch_in_body_records_pending(self):
        broken = support.valid_response(self.request.body, model="some-other-model")
        result, _ = self._classify(FakeHttp(FakeResponse(200, json.dumps(broken))))
        self.assertEqual(result.outcome, "pending")
        self.assertEqual(result.failure_class, "schema_invalid")

    def test_non_object_json_top_level_records_pending(self):
        result, _ = self._classify(FakeHttp(FakeResponse(200, json.dumps([1, 2, 3]))))
        self.assertEqual(result.outcome, "pending")
        self.assertEqual(result.failure_class, "schema_invalid")


class OperatorActionableStatusTest(TransportTestBase):
    def _config(self, **kwargs) -> TransportConfig:
        return TransportConfig(retry=RetryPolicy(max_attempts=4), **kwargs)

    def test_401_surfaces_once_without_retry(self):
        fake = FakeHttp(_http_error(401, b'{"error":"bad key"}'), repeat_last=True)
        result, sleeper = self._classify(fake, config=self._config())

        self.assertEqual(result.outcome, "pending")
        self.assertEqual(result.failure_class, "http_401")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(fake.call_count, 1)
        self.assertEqual(sleeper.delays, [])

    def test_422_surfaces_once_without_retry(self):
        fake = FakeHttp(FakeResponse(422, {"error": "unprocessable"}), repeat_last=True)
        result, sleeper = self._classify(fake, config=self._config())

        self.assertEqual(result.failure_class, "http_422")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(fake.call_count, 1)
        self.assertEqual(sleeper.delays, [])

    def test_403_surfaces_once_without_retry(self):
        fake = FakeHttp(FakeResponse(403, ""), repeat_last=True)
        result, _ = self._classify(fake, config=self._config())
        self.assertEqual(result.failure_class, "http_403")
        self.assertEqual(fake.call_count, 1)

    def test_400_surfaces_once_without_retry(self):
        fake = FakeHttp(FakeResponse(400, ""), repeat_last=True)
        result, _ = self._classify(fake, config=self._config())
        self.assertEqual(result.failure_class, "http_400")
        self.assertEqual(fake.call_count, 1)


class RetryPolicyTest(TransportTestBase):
    def test_429_with_retry_after_is_honoured_then_succeeds(self):
        fake = FakeHttp(
            FakeResponse(429, "", {"Retry-After": "2"}),
            FakeResponse(200, _valid_response_json(self.request)),
        )
        result, sleeper = self._classify(
            fake, config=TransportConfig(retry=RetryPolicy(max_attempts=3))
        )

        self.assertEqual(result.outcome, "classified")
        self.assertEqual(result.attempts, 2)
        self.assertEqual(fake.call_count, 2)
        self.assertEqual(sleeper.delays, [2.0])

    def test_429_without_retry_after_uses_jittered_exponential_backoff(self):
        fake = FakeHttp(
            FakeResponse(429, ""),
            FakeResponse(200, _valid_response_json(self.request)),
        )
        result, sleeper = self._classify(
            fake,
            config=TransportConfig(
                retry=RetryPolicy(max_attempts=3, base_delay_seconds=0.5, jitter=0.25)
            ),
        )

        self.assertEqual(result.outcome, "classified")
        self.assertEqual(sleeper.delays, [0.5])

    def test_retry_after_is_bounded_by_the_policy(self):
        fake = FakeHttp(
            FakeResponse(429, "", {"Retry-After": "9999"}),
            FakeResponse(200, _valid_response_json(self.request)),
        )
        _, sleeper = self._classify(
            fake,
            config=TransportConfig(
                retry=RetryPolicy(max_attempts=3, max_retry_after_seconds=10.0)
            ),
        )
        self.assertEqual(sleeper.delays, [10.0])

    def test_529_retries_then_succeeds(self):
        fake = FakeHttp(
            FakeResponse(529, ""),
            FakeResponse(200, _valid_response_json(self.request)),
        )
        result, sleeper = self._classify(
            fake, config=TransportConfig(retry=RetryPolicy(max_attempts=3))
        )
        self.assertEqual(result.outcome, "classified")
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(sleeper.delays), 1)

    def test_529_exhausts_retries_and_records_pending(self):
        fake = FakeHttp(FakeResponse(529, ""), repeat_last=True)
        result, sleeper = self._classify(
            fake, config=TransportConfig(retry=RetryPolicy(max_attempts=3))
        )
        self.assertEqual(result.outcome, "pending")
        self.assertEqual(result.failure_class, "http_529")
        self.assertEqual(result.attempts, 3)
        self.assertEqual(fake.call_count, 3)
        self.assertEqual(len(sleeper.delays), 2)

    def test_transient_503_retries(self):
        fake = FakeHttp(
            FakeResponse(503, ""),
            FakeResponse(200, _valid_response_json(self.request)),
        )
        result, _ = self._classify(
            fake, config=TransportConfig(retry=RetryPolicy(max_attempts=3))
        )
        self.assertEqual(result.outcome, "classified")
        self.assertEqual(fake.call_count, 2)

    def test_timeout_retries_and_records_pending(self):
        fake = FakeHttp(TimeoutError("timed out"), repeat_last=True)
        result, sleeper = self._classify(
            fake, config=TransportConfig(retry=RetryPolicy(max_attempts=3))
        )
        self.assertEqual(result.outcome, "pending")
        self.assertEqual(result.failure_class, "timeout")
        self.assertEqual(result.attempts, 3)
        self.assertEqual(fake.call_count, 3)
        self.assertEqual(len(sleeper.delays), 2)
        self.assertEqual(iter_provenance(self.store)[0]["failure_class"], "timeout")

    def test_urlerror_with_timeout_reason_is_a_timeout(self):
        wrapped = urllib.error.URLError(socket.timeout("timed out"))
        fake = FakeHttp(wrapped, repeat_last=True)
        result, _ = self._classify(
            fake, config=TransportConfig(retry=RetryPolicy(max_attempts=2))
        )
        self.assertEqual(result.failure_class, "timeout")
        self.assertEqual(result.attempts, 2)

    def test_connection_error_retries_then_pending(self):
        fake = FakeHttp(urllib.error.URLError("connection refused"), repeat_last=True)
        result, _ = self._classify(
            fake, config=TransportConfig(retry=RetryPolicy(max_attempts=2))
        )
        self.assertEqual(result.failure_class, "connection")
        self.assertEqual(result.attempts, 2)


class BudgetTest(TransportTestBase):
    def test_budget_exhausted_before_send_makes_no_http_call(self):
        fake = FakeHttp(FakeResponse(200, _valid_response_json(self.request)))
        result, _ = self._classify(
            fake, config=TransportConfig(budget=Budget(max_requests=0))
        )
        self.assertEqual(result.outcome, "pending")
        self.assertEqual(result.failure_class, "budget_exhausted")
        self.assertEqual(fake.call_count, 0)

    def test_per_run_request_cap_stops_retries(self):
        fake = FakeHttp(FakeResponse(429, ""), repeat_last=True)
        result, sleeper = self._classify(
            fake,
            config=TransportConfig(
                retry=RetryPolicy(max_attempts=5), budget=Budget(max_requests=2)
            ),
        )
        self.assertEqual(result.failure_class, "budget_exhausted")
        self.assertEqual(fake.call_count, 2)
        self.assertEqual(len(sleeper.delays), 1)

    def test_token_budget_exhausted_after_usage(self):
        budget = Budget(max_tokens=10)
        budget.record_usage({"input_tokens": 12, "output_tokens": 7})
        self.assertFalse(budget.can_attempt_more())
        fake = FakeHttp(FakeResponse(200, _valid_response_json(self.request)))
        result, _ = self._classify(fake, config=TransportConfig(budget=budget))
        self.assertEqual(result.failure_class, "budget_exhausted")
        self.assertEqual(fake.call_count, 0)

    def test_known_prices_compute_cost(self):
        budget = Budget(
            max_tokens=1_000_000,
            max_cost_usd=1.0,
            price_per_million_input_usd=2.0,
            price_per_million_output_usd=4.0,
        )
        budget.record_usage({"input_tokens": 1000, "output_tokens": 500})
        self.assertTrue(budget.cost_known)
        self.assertAlmostEqual(budget.cost_usd, (1000 * 2.0 + 500 * 4.0) / 1_000_000.0)

    def test_dollar_ceiling_with_known_price_can_block(self):
        fake = FakeHttp(FakeResponse(200, _valid_response_json(self.request)))
        budget = Budget(
            max_cost_usd=0.0,
            price_per_million_input_usd=1.0,
            price_per_million_output_usd=1.0,
        )
        result, _ = self._classify(fake, config=TransportConfig(budget=budget))
        self.assertEqual(result.failure_class, "budget_exhausted")
        self.assertEqual(fake.call_count, 0)

    def test_unknown_price_cannot_be_spent_against_dollar_ceiling(self):
        # With an unknown price the accumulated cost is unknown, so it is not
        # charged against the ceiling: a positive ceiling keeps allowing work.
        budget = Budget(max_cost_usd=5.0)
        budget.record_usage({"input_tokens": 1_000_000, "output_tokens": 1_000_000})
        self.assertFalse(budget.cost_known)
        self.assertIsNone(budget.cost_usd)
        self.assertTrue(budget.can_attempt_more())

        fake = FakeHttp(FakeResponse(200, _valid_response_json(self.request)))
        result, _ = self._classify(fake, config=TransportConfig(budget=budget))

        self.assertEqual(result.outcome, "classified")
        self.assertFalse(result.cost_known)
        self.assertIsNone(result.cost_usd)
        self.assertEqual(fake.call_count, 1)
        self.assertEqual(iter_provenance(self.store)[0]["cost_known"], 0)

    def test_zero_dollar_ceiling_blocks_immediately_when_cost_is_known(self):
        fake = FakeHttp(FakeResponse(200, _valid_response_json(self.request)))
        budget = Budget(max_cost_usd=0.0)
        self.assertTrue(budget.cost_known)
        result, _ = self._classify(fake, config=TransportConfig(budget=budget))
        self.assertEqual(result.failure_class, "budget_exhausted")
        self.assertEqual(fake.call_count, 0)


class CircuitBreakerTest(TransportTestBase):
    def test_circuit_opens_after_threshold_and_blocks_next_call(self):
        clock = FakeClock()
        config = TransportConfig(
            retry=RetryPolicy(max_attempts=1),
            circuit_failure_threshold=2,
            circuit_cooldown_seconds=999.0,
        )
        fake = FakeHttp(FakeResponse(500, ""), repeat_last=True)

        first, _ = self._classify(fake, config=config, clock=clock)
        second, _ = self._classify(fake, config=config, clock=clock)
        third, _ = self._classify(fake, config=config, clock=clock)

        self.assertEqual(first.failure_class, "http_500")
        self.assertEqual(second.failure_class, "http_500")
        self.assertEqual(third.failure_class, "circuit_open")
        self.assertEqual(fake.call_count, 2)

    def test_circuit_state_is_persisted_in_the_store(self):
        clock = FakeClock()
        breaker = CircuitBreaker(
            self.store.conn,
            key="default",
            failure_threshold=2,
            cooldown_seconds=999.0,
            clock=clock,
        )
        breaker.record_failure()
        self.assertFalse(breaker.is_open())
        breaker.record_failure()
        self.assertTrue(breaker.is_open())

        reopened = CircuitBreaker(
            self.store.conn,
            key="default",
            failure_threshold=2,
            cooldown_seconds=999.0,
            clock=clock,
        )
        self.assertTrue(reopened.is_open())
        with self.assertRaises(ObservatoryError):
            reopened.check()

    def test_circuit_probe_after_cooldown_resets_on_success(self):
        clock = FakeClock()
        breaker = CircuitBreaker(
            self.store.conn,
            failure_threshold=1,
            cooldown_seconds=10.0,
            clock=clock,
        )
        breaker.record_failure()
        self.assertTrue(breaker.is_open())
        clock.advance(11.0)
        self.assertFalse(breaker.is_open())
        breaker.record_success()
        self.assertEqual(breaker.state(), (0, None, None))

    def test_non_transient_4xx_does_not_count_toward_the_circuit(self):
        config = TransportConfig(
            retry=RetryPolicy(max_attempts=1),
            circuit_failure_threshold=1,
        )
        fake = FakeHttp(FakeResponse(401, ""), repeat_last=True)
        result, _ = self._classify(fake, config=config)
        self.assertEqual(result.failure_class, "http_401")

        breaker = CircuitBreaker(
            self.store.conn, key="default", failure_threshold=1, cooldown_seconds=999.0
        )
        self.assertFalse(breaker.is_open())

    def test_non_transient_probe_outcome_releases_the_reservation(self):
        clock = FakeClock()
        config = TransportConfig(
            retry=RetryPolicy(max_attempts=1),
            circuit_failure_threshold=1,
            circuit_cooldown_seconds=10.0,
        )
        first, _ = self._classify(
            FakeHttp(FakeResponse(500, ""), repeat_last=True), config=config, clock=clock
        )
        self.assertEqual(first.failure_class, "http_500")

        clock.advance(11.0)
        second, _ = self._classify(FakeHttp(FakeResponse(401, "")), config=config, clock=clock)
        self.assertEqual(second.failure_class, "http_401")

        # The 401 neither reset nor advanced the breaker, but it must not leave
        # the half-open probe reserved forever: the next call can probe again.
        third, _ = self._classify(
            FakeHttp(FakeResponse(200, _valid_response_json(self.request))),
            config=config,
            clock=clock,
        )
        self.assertEqual(third.outcome, "classified")


class ModelDriftTest(TransportTestBase):
    def _drift_request(self):
        drift_taxonomy = dataclasses.replace(self.taxonomy, model="jev-9.9.9")
        return build_request(
            {"summary": "fix a bug"}, drift_taxonomy, snapshot_hash="b" * 64
        )

    def test_model_drift_is_refused_before_any_send(self):
        drift = self._drift_request()
        fake = FakeHttp(FakeResponse(200, _valid_response_json(drift)))
        result = classify(
            self.store, drift, credential=CREDENTIAL, urlopen=fake
        )
        self.assertEqual(result.outcome, "pending")
        self.assertEqual(result.failure_class, "model_drift")
        self.assertEqual(fake.call_count, 0)
        self.assertEqual(self.store.classification_count(), 0)

    def test_transport_send_raises_model_drift(self):
        drift = self._drift_request()
        transport = JevTransport(CREDENTIAL)
        with self.assertRaises(ModelDriftError):
            transport.send(drift)

    def test_allow_model_drift_permits_the_send(self):
        drift = self._drift_request()
        fake = FakeHttp(FakeResponse(200, _valid_response_json(drift)))
        result = classify(
            self.store,
            drift,
            credential=CREDENTIAL,
            config=TransportConfig(allow_model_drift=True),
            urlopen=fake,
        )
        self.assertEqual(result.outcome, "classified")
        self.assertEqual(fake.call_count, 1)


class RedactionTest(TransportTestBase):
    def test_mask_secret_keeps_only_the_last_four(self):
        secret = "sk-super-secret-key-9876"
        masked = mask_secret(secret)
        self.assertTrue(masked.endswith(secret[-4:]))
        self.assertNotIn(secret, masked)
        self.assertEqual(mask_secret("abcd"), "****")
        self.assertEqual(mask_secret(""), "<unset>")

    def test_redact_text_replaces_every_occurrence(self):
        secret = "sk-super-secret-key-9876"
        message = f"failed with {secret} and again {secret}"
        cleaned = redact_text(message, secret)
        self.assertNotIn(secret, cleaned)
        self.assertEqual(cleaned.count(mask_secret(secret)), 2)

    def test_credential_never_appears_in_error_or_provenance(self):
        secret = "sk-super-secret-key-9876"
        fake = FakeHttp(urllib.error.URLError(f"tls handshake failed for {secret}"), repeat_last=True)
        result, _ = self._classify(
            fake,
            credential=secret,
            config=TransportConfig(retry=RetryPolicy(max_attempts=1)),
        )

        self.assertEqual(result.outcome, "pending")
        self.assertNotIn(secret, result.error)
        self.assertEqual(result.credential_masked, mask_secret(secret))
        self.assertEqual(result.to_dict()["credential_masked"], mask_secret(secret))

        for row in iter_provenance(self.store):
            for value in row.values():
                self.assertNotIn(secret, str(value))
        self.assertEqual(iter_provenance(self.store)[0]["credential_masked"], mask_secret(secret))

    def test_no_body_logging(self):
        secret = CREDENTIAL
        fake = FakeHttp(FakeResponse(200, _valid_response_json(self.request)))
        records: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = records.append  # type: ignore[method-assign]
        root = logging.getLogger()
        previous_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            result, _ = self._classify(fake, credential=secret)
        finally:
            root.removeHandler(handler)
            root.setLevel(previous_level)

        self.assertEqual(result.outcome, "classified")
        logged = "\n".join(record.getMessage() for record in records)
        self.assertNotIn(secret, logged)
        self.assertNotIn(self.request.serialized, logged)

    def test_response_body_is_not_echoed_in_error_text(self):
        secret = "sk-body-secret-0001"
        body = json.dumps({"error": f"bad request containing {secret}"}).encode("utf-8")
        fake = FakeHttp(FakeResponse(400, body), repeat_last=True)
        result, _ = self._classify(fake, credential=secret)
        self.assertEqual(result.failure_class, "http_400")
        self.assertNotIn(secret, result.error)


class CredentialTest(TransportTestBase):
    def test_load_credential_from_env_variable(self):
        self.assertEqual(load_credential({"JEV_API_KEY": "env-key-12345"}), "env-key-12345")

    def test_env_variable_takes_precedence_over_key_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".key", delete=False) as handle:
            handle.write("file-key-999\n")
            path = handle.name
        self.addCleanup(os.unlink, path)
        credential = load_credential({"JEV_API_KEY": "env-key-1", "JEV_KEY_FILE": path})
        self.assertEqual(credential, "env-key-1")

    def test_load_credential_from_key_file(self):
        path = os.path.join(self.tmp.name, "jev.key")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("file-secret-5678\n")
        self.assertEqual(load_credential({"JEV_KEY_FILE": path}), "file-secret-5678")

    def test_load_credential_from_key_file_with_bearer_prefix(self):
        path = os.path.join(self.tmp.name, "jev.key")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("Bearer file-secret-9999\n")
        self.assertEqual(load_credential({"JEV_KEY_FILE": path}), "file-secret-9999")

    def test_missing_credential_is_recorded_pending(self):
        fake = FakeHttp(FakeResponse(200, _valid_response_json(self.request)))
        with mock.patch.dict(os.environ, {}, clear=True):
            result = classify(self.store, self.request, urlopen=fake, credential=None)
        self.assertEqual(result.outcome, "pending")
        self.assertEqual(result.failure_class, "credential_error")
        self.assertEqual(fake.call_count, 0)

    def test_missing_credential_file_is_an_error(self):
        with self.assertRaises(CredentialError):
            load_credential({"JEV_KEY_FILE": os.path.join(self.tmp.name, "absent.key")})


class ConfigTest(unittest.TestCase):
    def test_defaults_are_pinned(self):
        config = TransportConfig()
        self.assertEqual(config.endpoint, DEFAULT_ENDPOINT)
        self.assertEqual(config.pinned_model, PINNED_MODEL)
        self.assertFalse(config.allow_model_drift)
        self.assertEqual(config.timeout_seconds, 30.0)

    def test_from_mapping_reads_nested_retry_budget_and_circuit(self):
        config = TransportConfig.from_mapping(
            {
                "timeout_seconds": 12.5,
                "retry": {"max_attempts": 7, "base_delay_seconds": 0.1},
                "budget": {"max_requests": 3, "max_tokens": 500},
                "circuit": {"failure_threshold": 2, "cooldown_seconds": 5.0},
            }
        )
        self.assertEqual(config.timeout_seconds, 12.5)
        self.assertEqual(config.retry.max_attempts, 7)
        self.assertEqual(config.retry.base_delay_seconds, 0.1)
        self.assertEqual(config.budget.max_requests, 3)
        self.assertEqual(config.budget.max_tokens, 500)
        self.assertEqual(config.circuit_failure_threshold, 2)
        self.assertEqual(config.circuit_cooldown_seconds, 5.0)

    def test_unknown_config_keys_are_rejected(self):
        with self.assertRaises(TransportConfigError):
            TransportConfig.from_mapping({"budget": {"max_request": 3}})
        with self.assertRaises(TransportConfigError):
            TransportConfig.from_mapping({"not_a_setting": True})

    def test_invalid_values_are_rejected(self):
        with self.assertRaises(TransportConfigError):
            TransportConfig(timeout_seconds=0)
        with self.assertRaises(TransportConfigError):
            TransportConfig(retry=RetryPolicy(max_attempts=0))
        with self.assertRaises(TransportConfigError):
            Budget(max_requests=-1)


class StrictCredentialParserTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "jev.key")

    def _load(self, text, encoding="utf-8"):
        with open(self.path, "w", encoding=encoding) as handle:
            handle.write(text)
        return load_credential({"JEV_KEY_FILE": self.path})

    def _load_error(self, text):
        with self.assertRaises(CredentialError) as caught:
            self._load(text)
        return str(caught.exception)

    def test_recognized_labelled_lines(self):
        for name in (
            "KEY",
            "API_KEY",
            "APIKEY",
            "SECRET",
            "TOKEN",
            "TYPESAFE_API_KEY",
            "JEV_API_KEY",
            "SOME_PROVIDER_KEY",
        ):
            self.assertEqual(self._load(f"{name}=sk-1234567890abcdef\n"), "sk-1234567890abcdef")
            self.assertEqual(self._load(f"{name}: sk-1234567890abcdef\n"), "sk-1234567890abcdef")

    def test_bare_quoted_export_bearer_and_crlf(self):
        self.assertEqual(self._load("sk-1234567890abcdef\n"), "sk-1234567890abcdef")
        self.assertEqual(self._load('API_KEY="sk-1234567890abcdef"\n'), "sk-1234567890abcdef")
        self.assertEqual(self._load("API_KEY=`sk-1234567890abcdef`\n"), "sk-1234567890abcdef")
        self.assertEqual(self._load("export TYPESAFE_API_KEY=sk-1234567890abcdef\n"), "sk-1234567890abcdef")
        self.assertEqual(self._load("Bearer sk-1234567890abcdef\n"), "sk-1234567890abcdef")
        self.assertEqual(self._load("KEY=sk-1234567890abcdef\r\n"), "sk-1234567890abcdef")

    def test_bom_is_stripped_from_a_bare_key_and_a_comment(self):
        bom_path = os.path.join(self.tmp.name, "bom.key")
        with open(bom_path, "wb") as handle:
            handle.write("\ufeffsk-1234567890abcdef\n".encode("utf-8"))
        self.assertEqual(load_credential({"JEV_KEY_FILE": bom_path}), "sk-1234567890abcdef")

        comment_path = os.path.join(self.tmp.name, "bom-comment.key")
        with open(comment_path, "wb") as handle:
            handle.write("\ufeff# key\nKEY=sk-1234567890abcdef\n".encode("utf-8"))
        self.assertEqual(load_credential({"JEV_KEY_FILE": comment_path}), "sk-1234567890abcdef")

    def test_prose_label_and_markdown_are_errors_naming_skipped_lines(self):
        for text in (
            "The key lives in the vault, see below.\n",
            "API key: (see vault)\n",
            "**API key**: `sk-1234567890abcdef`\n",
            "export FOO=bar\n",
        ):
            message = self._load_error(text)
            self.assertIn("line(s): 1", message, text)

    def test_first_valid_line_wins_over_skipped_lines(self):
        text = "The key lives in the vault.\n# note\nJEV_API_KEY=sk-1234567890abcdef\n"
        self.assertEqual(self._load(text), "sk-1234567890abcdef")

    def test_all_comment_file_errors_cleanly(self):
        message = self._load_error("# only a comment\n\n")
        self.assertIn("no credential", message)


class CredentialEnvAliasTest(unittest.TestCase):
    def test_typesafe_key_is_preferred_over_alias_and_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".key", delete=False) as handle:
            handle.write("file-secret-12345678\n")
            path = handle.name
        self.addCleanup(os.unlink, path)
        environ = {
            "TYPESAFE_API_KEY": "typesafe-secret-1",
            "JEV_API_KEY": "jev-secret-1",
            "JEV_KEY_FILE": path,
        }
        self.assertEqual(load_credential(environ), "typesafe-secret-1")

    def test_jev_api_key_is_the_alias(self):
        self.assertEqual(load_credential({"JEV_API_KEY": "jev-secret-1"}), "jev-secret-1")

    def test_missing_credential_message_names_both_env_variables(self):
        with self.assertRaises(CredentialError) as caught:
            load_credential({})
        self.assertIn("TYPESAFE_API_KEY", str(caught.exception))
        self.assertIn("JEV_API_KEY", str(caught.exception))


class CircuitBreakerConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "circuit.db")
        # Create the transport schema once so the threads below only contend on
        # data writes, not DDL.
        with ObservatoryStore(self.db) as store:
            CircuitBreaker(store.conn, key="init", failure_threshold=1)

    def test_concurrent_failures_are_not_lost(self):
        def hammer():
            conn = sqlite3.connect(self.db, isolation_level=None, timeout=30.0)
            try:
                breaker = CircuitBreaker(
                    conn, key="k", failure_threshold=10 ** 9, cooldown_seconds=0.0
                )
                for _ in range(100):
                    breaker.record_failure()
            finally:
                conn.close()

        threads = [threading.Thread(target=hammer) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        with ObservatoryStore(self.db) as store:
            failures, _, _ = CircuitBreaker(
                store.conn, key="k", failure_threshold=10 ** 9
            ).state()
        self.assertEqual(failures, 200)

    def test_concurrent_probe_grants_exactly_one(self):
        clock = FakeClock()
        with ObservatoryStore(self.db) as store:
            CircuitBreaker(
                store.conn, key="k", failure_threshold=1, cooldown_seconds=10.0, clock=clock
            ).record_failure()
        clock.advance(11.0)

        granted: list[int] = []
        denied: list[int] = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def probe():
            conn = sqlite3.connect(self.db, isolation_level=None, timeout=30.0)
            try:
                breaker = CircuitBreaker(
                    conn, key="k", failure_threshold=1, cooldown_seconds=10.0, clock=clock
                )
                barrier.wait()
                try:
                    breaker.check()
                    with lock:
                        granted.append(1)
                except CircuitOpenError:
                    with lock:
                        denied.append(1)
            finally:
                conn.close()

        threads = [threading.Thread(target=probe) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(granted), 1)
        self.assertEqual(len(denied), 7)


class BoundedReadTest(TransportTestBase):
    def test_oversized_error_body_is_bounded_and_not_retried(self):
        fake = FakeHttp(_http_error(500, b"x" * (3 * 1024 * 1024)), repeat_last=True)
        config = TransportConfig(
            max_response_bytes=2 * 1024 * 1024, retry=RetryPolicy(max_attempts=4)
        )
        result, sleeper = self._classify(fake, config=config)
        self.assertEqual(result.outcome, "pending")
        self.assertEqual(result.failure_class, "response_too_large")
        self.assertEqual(fake.call_count, 1)
        self.assertEqual(sleeper.delays, [])

    def test_oversized_2xx_body_is_bounded(self):
        fake = FakeHttp(FakeResponse(200, b"x" * (3 * 1024 * 1024)))
        config = TransportConfig(max_response_bytes=2 * 1024 * 1024)
        result, _ = self._classify(fake, config=config)
        self.assertEqual(result.outcome, "pending")
        self.assertEqual(result.failure_class, "response_too_large")

    def test_http_error_body_is_closed(self):
        error = _http_error(500, b"boom")
        fake = FakeHttp(error, repeat_last=True)
        self._classify(fake, config=TransportConfig(retry=RetryPolicy(max_attempts=1)))
        self.assertTrue(error.fp.closed)


class TransportExceptionTest(TransportTestBase):
    def _assert_connection_failure(self, exc):
        fake = FakeHttp(exc, repeat_last=True)
        result, _ = self._classify(fake, config=TransportConfig(retry=RetryPolicy(max_attempts=2)))
        self.assertEqual(result.outcome, "pending")
        self.assertEqual(result.failure_class, "connection")
        rows = iter_provenance(self.store)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["failure_class"], "connection")

    def test_bad_status_line_is_a_connection_failure(self):
        self._assert_connection_failure(http.client.BadStatusLine("garbage status"))

    def test_incomplete_read_is_a_connection_failure(self):
        self._assert_connection_failure(http.client.IncompleteRead(b"partial"))

    def test_http_exception_does_not_escape_send(self):
        transport = JevTransport(
            CREDENTIAL,
            TransportConfig(retry=RetryPolicy(max_attempts=1)),
            urlopen=FakeHttp(http.client.BadStatusLine("x")),
        )
        result = transport.send(self.request)
        self.assertFalse(result.ok)
        self.assertEqual(result.failure_class, "connection")


class BudgetChargingTest(TransportTestBase):
    def test_float_usage_is_not_charged_and_records_why(self):
        body = support.valid_response(self.request.body)
        body["usage"] = {"input_tokens": 5000.0, "output_tokens": 250.0}
        fake = FakeHttp(FakeResponse(200, json.dumps(body)))
        config = TransportConfig(
            retry=RetryPolicy(max_attempts=1), budget=Budget(max_tokens=10, max_requests=5)
        )
        result, _ = self._classify(fake, config=config)

        self.assertEqual(result.outcome, "pending")
        self.assertEqual(result.failure_class, "schema_invalid")
        self.assertEqual(config.budget.total_tokens, 0)
        self.assertIsNone(result.usage)
        self.assertIn("usage", result.error)
        row = iter_provenance(self.store)[0]
        self.assertIsNone(row["input_tokens"])
        self.assertIsNone(row["usage_json"])

    def test_budget_block_of_a_mid_loop_retry_reports_budget_exhausted(self):
        fake = FakeHttp(TimeoutError("timed out"), repeat_last=True)
        config = TransportConfig(
            retry=RetryPolicy(max_attempts=3), budget=Budget(max_requests=1)
        )
        result, _ = self._classify(fake, config=config)
        self.assertEqual(result.failure_class, "budget_exhausted")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(fake.call_count, 1)


class UsageSurfaceTest(TransportTestBase):
    def test_usage_extra_keys_never_reach_the_result_or_the_row(self):
        body = support.valid_response(self.request.body)
        body["usage"] = {"input_tokens": 12, "output_tokens": 7, "note": CREDENTIAL}
        fake = FakeHttp(FakeResponse(200, json.dumps(body)))
        result, _ = self._classify(fake)

        self.assertEqual(result.outcome, "classified")
        self.assertEqual(result.usage, {"input_tokens": 12, "output_tokens": 7})
        self.assertNotIn(CREDENTIAL, json.dumps(result.to_dict()))
        row = iter_provenance(self.store)[0]
        self.assertNotIn(CREDENTIAL, json.dumps(row))
        self.assertNotIn("note", row["usage_json"])


class BackoffTest(TransportTestBase):
    def test_jitter_cannot_exceed_max_delay(self):
        config = TransportConfig(
            retry=RetryPolicy(
                max_attempts=3, base_delay_seconds=8.0, max_delay_seconds=8.0, jitter=0.25
            )
        )
        transport = JevTransport(CREDENTIAL, config, random_source=lambda: 1.0)
        self.assertLessEqual(transport._compute_delay(1, None), 8.0)

    def test_nan_retry_after_falls_back_to_backoff(self):
        fake = FakeHttp(
            FakeResponse(429, "", {"Retry-After": "nan"}),
            FakeResponse(200, _valid_response_json(self.request)),
        )
        _, sleeper = self._classify(
            fake,
            config=TransportConfig(
                retry=RetryPolicy(max_attempts=3, base_delay_seconds=0.5)
            ),
        )
        self.assertEqual(sleeper.delays, [0.5])

    def test_http_date_retry_after_uses_the_injected_clock(self):
        clock = FakeClock(1000.0)
        when = email.utils.formatdate(1030, usegmt=True)
        fake = FakeHttp(
            FakeResponse(429, "", {"Retry-After": when}),
            FakeResponse(200, _valid_response_json(self.request)),
        )
        _, sleeper = self._classify(
            fake,
            config=TransportConfig(retry=RetryPolicy(max_attempts=3)),
            clock=clock,
        )
        self.assertAlmostEqual(sleeper.delays[0], 30.0, places=2)


class TimeoutBoundTest(unittest.TestCase):
    def test_timeout_above_the_ceiling_is_refused(self):
        with self.assertRaises(TransportConfigError):
            TransportConfig(timeout_seconds=MAX_TIMEOUT_SECONDS + 1)
        self.assertEqual(
            TransportConfig(timeout_seconds=MAX_TIMEOUT_SECONDS).timeout_seconds,
            MAX_TIMEOUT_SECONDS,
        )


class MaskSecretEdgeTest(unittest.TestCase):
    def test_secrets_of_eight_or_fewer_are_fully_masked(self):
        self.assertEqual(mask_secret("abcd"), "****")
        self.assertEqual(mask_secret("12345678"), "********")
        self.assertEqual(mask_secret("123456789"), "*****6789")

    def test_bytes_are_decoded(self):
        expected = mask_secret("sk-super-secret-9876")
        self.assertEqual(mask_secret(b"sk-super-secret-9876"), expected)
        self.assertEqual(mask_secret(bytearray(b"sk-super-secret-9876")), expected)

    def test_other_types_raise_without_leaking_the_value(self):
        with self.assertRaises(TypeError) as caught:
            mask_secret(12345678901234567890)  # type: ignore[arg-type]
        self.assertNotIn("12345678901234567890", str(caught.exception))


class StoreErrorSurfaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state.json")
        with open(self.state, "w", encoding="utf-8") as handle:
            json.dump({"summary": "store error"}, handle)

    def _run_cli(self, args):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = cli.main(args)
        return code, stderr.getvalue()

    def test_unwritable_db_reports_error_without_a_traceback(self):
        code, stderr = self._run_cli(
            [
                "classify",
                "--db",
                "/dev/null",
                "--state",
                self.state,
                "--snapshot-hash",
                "a" * 64,
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("error:", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_sqlite_error_from_use_is_surfaced_with_a_pending_row(self):
        store = ObservatoryStore(os.path.join(self.tmp.name, "projection.db"))
        self.addCleanup(store.close)
        request = build_request(
            {"summary": "store error"}, load_taxonomy(), snapshot_hash="a" * 64
        )
        with mock.patch(
            "agent_observatory.transport.persist_request",
            side_effect=sqlite3.OperationalError("disk I/O error"),
        ):
            with self.assertRaises(ObservatoryError):
                classify(store, request, credential=CREDENTIAL)
        rows = iter_provenance(store)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "pending")
        self.assertEqual(rows[0]["failure_class"], "store_error")


class SnapshotHashCrossCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "projection.db")
        fixture = support.write_jsonl(
            os.path.join(self.tmp.name, "events.jsonl"),
            [support.make_record(event_id="e1")],
        )
        with ObservatoryStore(self.db) as store:
            store.import_jsonl(fixture)

    def _args(self, snapshot_hash):
        return argparse.Namespace(
            db=self.db,
            session="city-a|host-a|codex|session-1",
            subject_kind="session",
            event_id=None,
            snapshot_hash=snapshot_hash,
        )

    def test_explicit_hash_is_cross_checked_against_the_store(self):
        real = cli._resolve_snapshot_hash(self._args(None))
        self.assertEqual(cli._resolve_snapshot_hash(self._args(real)), real)
        with self.assertRaises(ObservatoryError) as caught:
            cli._resolve_snapshot_hash(self._args("b" * 64))
        self.assertIn("does not match", str(caught.exception))

    def test_explicit_hash_without_db_is_used_as_is(self):
        args = self._args("c" * 64)
        args.db = None
        self.assertEqual(cli._resolve_snapshot_hash(args), "c" * 64)


if __name__ == "__main__":
    unittest.main()
