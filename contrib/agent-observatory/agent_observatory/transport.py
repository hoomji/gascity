"""Bounded live-classification transport for the Jev ``/v1/systemone`` API.

This is the transport phase of the Jev integration: it takes a request built by
the existing :mod:`agent_observatory.jev` builder and sends it with a bounded,
auditable policy instead of an unbounded ``urlopen`` call.

Safety and accounting properties implemented here:

* **Model pin.** The request model must be ``jev-1.13.0``; any other model is
  refused before anything is sent unless model drift is explicitly allowed.
* **Runtime-only credential.** The bearer token is read from
  ``TYPESAFE_API_KEY`` (preferred) or its alias ``JEV_API_KEY``, or from a file
  named by ``JEV_KEY_FILE``, at call time. It is never accepted as an argv
  value and is redacted to its last four characters on every state, log, and
  error surface. Key-file parsing is strict: only a recognized ``NAME=value``
  line, a ``Bearer <token>`` line, or a single bare token is accepted, so prose,
  labels, and note references can never be sent as a bearer token.
* **Bounded retries with jitter.** ``429``/``529``/transient ``5xx`` (and
  timeout/connection failures) are retried with jittered exponential backoff,
  honouring a bounded ``Retry-After``. Operator-actionable statuses
  (``400``/``401``/``403``/``422``) surface once and are never retried. The
  delay is capped *after* jitter, and an unparseable ``Retry-After`` falls back
  to backoff.
* **Budgets.** A per-run request cap plus token and dollar ceilings. A dollar
  ceiling is only enforceable when both prices are known; unknown prices stay
  unknown and cannot be charged against the ceiling. Usage is validated before
  it is charged, and only the validated integer token fields are ever exposed.
* **Circuit breaker.** After N consecutive failures the circuit opens and its
  state is persisted in the SQLite projection, so the refusal survives process
  restarts. The read-modify-write is transactional (``BEGIN IMMEDIATE``) and a
  single half-open probe is reserved atomically.
* **Validation.** A successful HTTP response is validated by the existing
  response validator and stored as an immutable classification; any failure
  records an ``unknown``/``pending`` provenance row instead of raising past the
  CLI. Response bodies are read in bounded chunks on both the 2xx and error
  paths.
* **No body logging.** Request and response bodies are never logged.

Transport-owned tables are created lazily through the existing projection
connection (``transport_circuit_state`` and ``transport_provenance``); the core
store schema is untouched.
"""

from __future__ import annotations

import http.client
import math
import os
import random
import re
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .canonical import canonical_json
from .errors import ObservatoryError, ResponseError
from .jev import (
    JevRequest,
    _validate_usage,
    import_response,
    parse_json_document,
    persist_request,
)
from .store import ObservatoryStore

DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
PINNED_MODEL = "jev-1.13.0"

# Upper bound on the HTTP timeout. A larger value is an unbounded hang in
# practice, so the configuration refuses it loudly instead of accepting it.
MAX_TIMEOUT_SECONDS = 300.0

# Statuses that are worth another attempt. Everything else (notably 400, 401,
# 403, and 422) is operator-actionable and surfaces after a single attempt.
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504, 529})

OUTCOME_CLASSIFIED = "classified"
OUTCOME_PENDING = "pending"

# Concurrency 1 is a transport contract, not just a convention: classification
# runs are serialized process-wide so a caller cannot fan out live requests.
_CLASSIFY_LOCK = threading.Lock()

# Failure classes that represent a transient dependency problem. These are the
# only outcomes that advance the circuit breaker; configuration refusals and
# budget/circuit short-circuits do not.
_CIRCUIT_COUNTED_FAILURES = frozenset(
    {
        "timeout",
        "connection",
        "http_408",
        "http_425",
        "http_429",
        "http_500",
        "http_502",
        "http_503",
        "http_504",
        "http_529",
        "malformed_json",
        "response_too_large",
        "invalid_status",
    }
)


class TransportError(ObservatoryError):
    """Base class for transport-layer failures."""


class TransportConfigError(TransportError):
    """The transport configuration or credential is invalid."""


class CredentialError(TransportConfigError):
    """No runtime credential could be loaded."""


class ModelDriftError(TransportConfigError):
    """The request model is not the pinned model and drift was not allowed."""


class BudgetExceededError(TransportError):
    """A per-run request/token/dollar budget ceiling is already exhausted."""


class CircuitOpenError(TransportError):
    """The persisted circuit breaker is open and refused the request."""


class TransportTimeoutError(TransportError):
    """The HTTP call exceeded its timeout."""


class TransportConnectionError(TransportError):
    """The HTTP call failed before a status was available."""


# ---------------------------------------------------------------------------
# Secret handling
# ---------------------------------------------------------------------------


def mask_secret(secret: str | bytes | None) -> str:
    """Return a display form that keeps only the last four secret characters.

    A secret of eight characters or fewer is fully masked: revealing a short
    secret's "last four" would reveal most or all of it. ``bytes`` is accepted
    and decoded so an embedding caller cannot crash the redaction surface; any
    other type raises a ``TypeError`` that never includes the value itself.
    """
    if secret is None:
        return "<unset>"
    if isinstance(secret, (bytes, bytearray)):
        secret = bytes(secret).decode("utf-8", "replace")
    elif not isinstance(secret, str):
        raise TypeError(
            f"mask_secret expects str or bytes, got {type(secret).__name__}"
        )
    if not secret:
        return "<unset>"
    if len(secret) <= 8:
        return "*" * len(secret)
    return "*" * (len(secret) - 4) + secret[-4:]


def redact_text(text: Any, *secrets: str | None) -> str:
    """Replace every occurrence of *secrets* in *text* with its masked form."""
    rendered = str(text)
    for secret in secrets:
        if secret:
            rendered = rendered.replace(secret, mask_secret(secret))
    return rendered


# A credential value must be a single opaque token. Real API keys are long; the
# lower bound is what keeps prose, note references, and markdown labels from
# being returned as a bearer token.
_CREDENTIAL_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.\-]{16,}$")

# Recognized key-file names. Anything whose normalized name ends in ``KEY`` is
# also accepted (``SOME_PROVIDER_KEY``, ``JEV_KEY``, ...).
_CREDENTIAL_NAMES = frozenset(
    {"KEY", "APIKEY", "API_KEY", "SECRET", "TOKEN", "TYPESAFE_API_KEY", "JEV_API_KEY"}
)


def _is_credential_name(name: str) -> bool:
    normalized = name.strip().upper()
    return normalized in _CREDENTIAL_NAMES or normalized.endswith("KEY")


def _strip_credential_wrapping(value: str) -> str:
    return value.strip().strip("'\"`").strip()


def _parse_key_file(text: str) -> tuple[str, list[int]]:
    """Return ``(credential, skipped_line_numbers)`` from a key-file body.

    Strict contract; the first accepted line wins:

    * ``[export ]NAME=value`` or ``NAME: value`` where ``NAME`` is a recognized
      credential name and ``value`` is a single ``[A-Za-z0-9_.-]{16,}`` token
      after stripping surrounding quotes/backticks;
    * ``Bearer <token>``;
    * a single bare token line.

    Blank lines and ``#`` comments are skipped silently. Every other non-blank
    line is recorded in *skipped_line_numbers* and is never returned as a token.
    """
    skipped: list[int] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.replace("\ufeff", "").strip()
        if not line or line.startswith("#"):
            continue
        working = line
        if working[:7].lower() == "export ":
            working = working[7:].lstrip()
        cut = min(
            (index for index in (working.find("="), working.find(":")) if index != -1),
            default=-1,
        )
        if cut != -1:
            name, value = working[:cut], working[cut + 1 :]
            if _is_credential_name(name):
                token = _strip_credential_wrapping(value)
                if _CREDENTIAL_TOKEN_RE.match(token):
                    return token, skipped
        if line[:7].lower() == "bearer ":
            token = _strip_credential_wrapping(line[7:])
            if _CREDENTIAL_TOKEN_RE.match(token):
                return token, skipped
        bare = _strip_credential_wrapping(line)
        if _CREDENTIAL_TOKEN_RE.match(bare):
            return bare, skipped
        skipped.append(line_number)
    return "", skipped


def _first_key_like_line(text: str) -> str:
    """Extract one strict credential from a key file.

    Returns the credential, or raises :class:`CredentialError` naming every
    non-blank, non-comment line number that was skipped. Prose and labels are
    never returned.
    """
    token, skipped = _parse_key_file(text)
    if token:
        return token
    if skipped:
        raise CredentialError(
            "no credential line found; skipped line(s): "
            + ", ".join(str(number) for number in skipped)
        )
    raise CredentialError("no credential line found (file has no non-comment lines)")


def load_credential(environ: Mapping[str, str] | None = None) -> str:
    """Load the Jev credential from the runtime environment only.

    Precedence is ``TYPESAFE_API_KEY`` (preferred) then its alias
    ``JEV_API_KEY`` then ``JEV_KEY_FILE``. The value is never read from argv,
    the repository, or a config file. A key file is decoded as ``utf-8-sig``
    (so a UTF-8 BOM cannot poison the token or turn a comment into one) and is
    parsed under the strict contract in :func:`_parse_key_file`.
    """
    env = environ if environ is not None else os.environ
    for name in ("TYPESAFE_API_KEY", "JEV_API_KEY"):
        api_key = (env.get(name) or "").strip()
        if api_key:
            return api_key
    key_file = (env.get("JEV_KEY_FILE") or "").strip()
    if not key_file:
        raise CredentialError(
            "no Jev credential: set TYPESAFE_API_KEY (preferred) or "
            "JEV_API_KEY, or JEV_KEY_FILE"
        )
    try:
        raw = Path(key_file).read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise CredentialError(f"cannot read JEV_KEY_FILE {key_file!r}: {exc}") from exc
    try:
        secret = _first_key_like_line(raw)
    except CredentialError as exc:
        raise CredentialError(f"JEV_KEY_FILE {key_file!r}: {exc}") from exc
    return secret


# ---------------------------------------------------------------------------
# Configuration value objects
# ---------------------------------------------------------------------------


def _require_number(value: Any, name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TransportConfigError(f"{name} must be a number, got {value!r}")
    number = float(value)
    if minimum is not None and number < minimum:
        raise TransportConfigError(f"{name} must be >= {minimum}, got {value!r}")
    return number


def _require_optional_int(value: Any, name: str, *, minimum: int | None = None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TransportConfigError(f"{name} must be an integer or null, got {value!r}")
    if minimum is not None and value < minimum:
        raise TransportConfigError(f"{name} must be >= {minimum}, got {value!r}")
    return value


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retry policy for transient transport failures."""

    max_attempts: int = 4
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0
    jitter: float = 0.25
    max_retry_after_seconds: float = 60.0

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int) or self.max_attempts < 1:
            raise TransportConfigError(f"retry.max_attempts must be >= 1, got {self.max_attempts!r}")
        _require_number(self.base_delay_seconds, "retry.base_delay_seconds", minimum=0.0)
        _require_number(self.max_delay_seconds, "retry.max_delay_seconds", minimum=0.0)
        jitter = _require_number(self.jitter, "retry.jitter", minimum=0.0)
        if jitter > 1.0:
            raise TransportConfigError("retry.jitter must be <= 1.0")
        _require_number(self.max_retry_after_seconds, "retry.max_retry_after_seconds", minimum=0.0)


@dataclass
class Budget:
    """Per-run request/token/dollar ceilings and their running counters.

    ``max_cost_usd`` is only enforceable when both per-million-token prices are
    known. If a price is unknown the accumulated cost becomes ``None`` (unknown)
    and unknown cost is deliberately *not* charged against the ceiling.

    The token and dollar ceilings are checked before each request, but usage is
    only known after a response arrives. The cap therefore cannot predict the
    next response's usage and a run can overshoot ``max_tokens`` /
    ``max_cost_usd`` by at most one request's usage before the following
    ``check_before_request`` refuses. This one-request overshoot is inherent to
    a post-hoc accounting transport and is not a bug.
    """

    max_requests: int | None = None
    max_tokens: int | None = None
    max_cost_usd: float | None = None
    price_per_million_input_usd: float | None = None
    price_per_million_output_usd: float | None = None
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = 0.0

    def __post_init__(self) -> None:
        _require_optional_int(self.max_requests, "budget.max_requests", minimum=0)
        _require_optional_int(self.max_tokens, "budget.max_tokens", minimum=0)
        for name in (
            "max_cost_usd",
            "price_per_million_input_usd",
            "price_per_million_output_usd",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_number(value, f"budget.{name}", minimum=0.0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def price_known(self) -> bool:
        return (
            self.price_per_million_input_usd is not None
            and self.price_per_million_output_usd is not None
        )

    @property
    def cost_known(self) -> bool:
        return self.cost_usd is not None

    def exhaustion_reason(self) -> str | None:
        """Return why the next request is not allowed, or ``None``."""
        if self.max_requests is not None and self.requests >= self.max_requests:
            return f"per-run request cap reached ({self.requests}/{self.max_requests})"
        if self.max_tokens is not None and self.total_tokens >= self.max_tokens:
            return f"token budget exhausted ({self.total_tokens}/{self.max_tokens})"
        if (
            self.max_cost_usd is not None
            and self.cost_usd is not None
            and self.cost_usd >= self.max_cost_usd
        ):
            return f"dollar budget exhausted (${self.cost_usd:.6f}/${self.max_cost_usd:.6f})"
        return None

    def check_before_request(self) -> None:
        reason = self.exhaustion_reason()
        if reason is not None:
            raise BudgetExceededError(reason)

    def can_attempt_more(self) -> bool:
        return self.exhaustion_reason() is None

    def record_request(self) -> None:
        self.requests += 1

    def record_usage(self, usage: Any) -> None:
        """Accumulate token usage and, when prices are known, dollar cost."""
        if not isinstance(usage, Mapping):
            return
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        added_input = (
            input_tokens
            if isinstance(input_tokens, int) and not isinstance(input_tokens, bool) and input_tokens > 0
            else 0
        )
        added_output = (
            output_tokens
            if isinstance(output_tokens, int) and not isinstance(output_tokens, bool) and output_tokens > 0
            else 0
        )
        self.input_tokens += added_input
        self.output_tokens += added_output
        if added_input + added_output <= 0:
            return
        if self.price_known:
            self.cost_usd = (self.cost_usd or 0.0) + (
                added_input * float(self.price_per_million_input_usd)
                + added_output * float(self.price_per_million_output_usd)
            ) / 1_000_000.0
        else:
            self.cost_usd = None

    def to_state(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "max_requests": self.max_requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "max_tokens": self.max_tokens,
            "cost_usd": self.cost_usd,
            "max_cost_usd": self.max_cost_usd,
            "cost_known": self.cost_known,
            "price_known": self.price_known,
        }


@dataclass
class TransportConfig:
    """Everything the transport needs besides a built request and a credential."""

    endpoint: str = DEFAULT_ENDPOINT
    pinned_model: str = PINNED_MODEL
    allow_model_drift: bool = False
    timeout_seconds: float = 30.0
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    budget: Budget = field(default_factory=Budget)
    circuit_failure_threshold: int = 5
    circuit_cooldown_seconds: float = 60.0
    circuit_key: str = "default"
    max_response_bytes: int = 2 * 1024 * 1024
    user_agent: str = "agent-observatory-transport/0.1"

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, str) or not self.endpoint:
            raise TransportConfigError("endpoint must be a non-empty string")
        if not isinstance(self.pinned_model, str) or not self.pinned_model:
            raise TransportConfigError("pinned_model must be a non-empty string")
        _require_number(self.timeout_seconds, "timeout_seconds", minimum=0.0)
        if self.timeout_seconds <= 0:
            raise TransportConfigError("timeout_seconds must be > 0")
        if self.timeout_seconds > MAX_TIMEOUT_SECONDS:
            raise TransportConfigError(
                f"timeout_seconds must be <= {MAX_TIMEOUT_SECONDS:g} "
                f"(got {self.timeout_seconds!r}); a larger timeout is an "
                "unbounded hang"
            )
        if isinstance(self.circuit_failure_threshold, bool) or not isinstance(self.circuit_failure_threshold, int):
            raise TransportConfigError("circuit_failure_threshold must be an integer")
        if self.circuit_failure_threshold < 1:
            raise TransportConfigError("circuit_failure_threshold must be >= 1")
        _require_number(self.circuit_cooldown_seconds, "circuit_cooldown_seconds", minimum=0.0)
        if not isinstance(self.circuit_key, str) or not self.circuit_key:
            raise TransportConfigError("circuit_key must be a non-empty string")
        if (
            isinstance(self.max_response_bytes, bool)
            or not isinstance(self.max_response_bytes, int)
            or self.max_response_bytes < 1
        ):
            raise TransportConfigError("max_response_bytes must be a positive integer")
        if not isinstance(self.retry, RetryPolicy):
            raise TransportConfigError("retry must be a RetryPolicy")
        if not isinstance(self.budget, Budget):
            raise TransportConfigError("budget must be a Budget")

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "TransportConfig":
        """Build a config from a JSON-compatible mapping.

        Unknown keys are rejected so a typo in a budget or retry field can never
        be silently ignored.
        """
        if not isinstance(data, Mapping):
            raise TransportConfigError("transport config must be a JSON object")
        data = dict(data)
        allowed = {
            "endpoint",
            "pinned_model",
            "allow_model_drift",
            "timeout_seconds",
            "retry",
            "budget",
            "circuit",
        }
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise TransportConfigError(f"unknown transport config key(s): {', '.join(unknown)}")

        retry_data = data.get("retry") or {}
        budget_data = data.get("budget") or {}
        circuit_data = data.get("circuit") or {}
        if not isinstance(retry_data, Mapping):
            raise TransportConfigError("transport config 'retry' must be an object")
        if not isinstance(budget_data, Mapping):
            raise TransportConfigError("transport config 'budget' must be an object")
        if not isinstance(circuit_data, Mapping):
            raise TransportConfigError("transport config 'circuit' must be an object")

        try:
            retry = RetryPolicy(**dict(retry_data))
        except TypeError as exc:
            raise TransportConfigError(f"invalid retry config: {exc}") from exc
        try:
            budget = Budget(**dict(budget_data))
        except TypeError as exc:
            raise TransportConfigError(f"invalid budget config: {exc}") from exc

        circuit_allowed = {"failure_threshold", "cooldown_seconds", "key"}
        unknown_circuit = sorted(set(circuit_data) - circuit_allowed)
        if unknown_circuit:
            raise TransportConfigError(
                f"unknown circuit config key(s): {', '.join(unknown_circuit)}"
            )

        kwargs: dict[str, Any] = {
            "endpoint": data.get("endpoint", DEFAULT_ENDPOINT),
            "pinned_model": data.get("pinned_model", PINNED_MODEL),
            "allow_model_drift": _require_bool(
                data.get("allow_model_drift", False), "allow_model_drift"
            ),
            "timeout_seconds": data.get("timeout_seconds", 30.0),
            "retry": retry,
            "budget": budget,
            "circuit_failure_threshold": circuit_data.get("failure_threshold", 5),
            "circuit_cooldown_seconds": circuit_data.get("cooldown_seconds", 60.0),
            "circuit_key": circuit_data.get("key", "default"),
        }
        return cls(**kwargs)

    @classmethod
    def from_file(cls, path: str | Path) -> "TransportConfig":
        import json

        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise TransportConfigError(f"cannot read transport config {path!r}: {exc}") from exc
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise TransportConfigError(f"transport config {path!r} is not valid JSON: {exc}") from exc
        return cls.from_mapping(data)


def _require_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise TransportConfigError(f"{name} must be a boolean, got {value!r}")
    return value


# ---------------------------------------------------------------------------
# Persistence: circuit state and provenance
# ---------------------------------------------------------------------------

_TRANSPORT_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS transport_circuit_state (
        circuit_key TEXT PRIMARY KEY,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        opened_at REAL,
        open_until REAL,
        probe_token TEXT,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS transport_provenance (
        provenance_id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_hash TEXT,
        snapshot_hash TEXT,
        subject_kind TEXT,
        taxonomy_version TEXT,
        question_hash TEXT,
        model TEXT NOT NULL,
        endpoint TEXT NOT NULL,
        outcome TEXT NOT NULL,
        failure_class TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        http_status INTEGER,
        latency_ms REAL,
        input_tokens INTEGER,
        output_tokens INTEGER,
        usage_json TEXT,
        cost_usd REAL,
        cost_known INTEGER NOT NULL DEFAULT 0,
        credential_masked TEXT,
        created_at TEXT NOT NULL
    )
    """,
)


def _ensure_transport_schema(conn: Any) -> None:
    for statement in _TRANSPORT_SCHEMA:
        conn.execute(statement)
    # Older databases created before half-open probe reservation lack the
    # column; add it lazily rather than requiring a migration step.
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(transport_circuit_state)").fetchall()
    }
    if "probe_token" not in columns:
        conn.execute(
            "ALTER TABLE transport_circuit_state ADD COLUMN probe_token TEXT"
        )


class CircuitBreaker:
    """A consecutive-failure circuit breaker persisted in the projection.

    The breaker opens once ``failure_threshold`` consecutive transport failures
    have been recorded. While open it refuses requests; after
    ``cooldown_seconds`` it allows a single probe, which either resets the
    breaker (success) or re-opens it (failure). The reservation of that single
    probe is itself transactional, so concurrent callers cannot all probe at
    once. Read-modify-write paths use ``BEGIN IMMEDIATE`` (as the store's own
    writers do) so concurrent failures are never lost.
    """

    # How long a granted half-open probe holds its reservation when
    # ``cooldown_seconds`` is shorter. If the prober crashes without recording
    # an outcome, the reservation expires after this window and a later caller
    # may probe again.
    _MIN_PROBE_RESERVATION_SECONDS = 300.0

    def __init__(
        self,
        conn: Any,
        *,
        key: str = "default",
        failure_threshold: int = 5,
        cooldown_seconds: float = 60.0,
        clock: Callable[[], float] = time.time,
    ):
        _ensure_transport_schema(conn)
        if isinstance(failure_threshold, bool) or not isinstance(failure_threshold, int) or failure_threshold < 1:
            raise TransportConfigError("circuit failure_threshold must be >= 1")
        if cooldown_seconds < 0:
            raise TransportConfigError("circuit cooldown_seconds must be >= 0")
        self._conn = conn
        self.key = key
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = float(cooldown_seconds)
        self._clock = clock
        # Unique to this breaker instance: identifies which caller holds a
        # half-open probe reservation.
        self._probe_token = uuid.uuid4().hex

    def _row(self) -> Any:
        return self._conn.execute(
            "SELECT consecutive_failures, opened_at, open_until, probe_token FROM "
            "transport_circuit_state WHERE circuit_key = ?",
            (self.key,),
        ).fetchone()

    def _corrupt(self, column: str, value: Any) -> TransportError:
        """Return the error for a stored circuit value that will not coerce.

        SQLite's INTEGER/REAL affinity still stores non-numeric TEXT, so a row
        written by an older binary or a manual repair can hold junk in these
        columns. The breaker must refuse such a projection instead of leaking a
        raw ``ValueError`` past the CLI's no-traceback boundary.
        """
        return TransportError(
            f"circuit {self.key!r} has a corrupted {column} value {value!r}; "
            "cannot read the persisted circuit state"
        )

    def _coerce_failures(self, value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise self._corrupt("consecutive_failures", value) from exc

    def _coerce_timestamp(self, value: Any, column: str) -> float:
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise self._corrupt(column, value) from exc

    def state(self) -> tuple[int, float | None, float | None]:
        row = self._conn.execute(
            "SELECT consecutive_failures, opened_at, open_until FROM "
            "transport_circuit_state WHERE circuit_key = ?",
            (self.key,),
        ).fetchone()
        if row is None:
            return 0, None, None
        failures = self._coerce_failures(row[0])
        opened_at = (
            self._coerce_timestamp(row[1], "opened_at") if row[1] is not None else None
        )
        open_until = (
            self._coerce_timestamp(row[2], "open_until") if row[2] is not None else None
        )
        return failures, opened_at, open_until

    def is_open(self) -> bool:
        _, _, open_until = self.state()
        return open_until is not None and self._clock() < float(open_until)

    def check(self) -> None:
        """Allow the request, or refuse and raise :class:`CircuitOpenError`.

        On a half-open breaker this atomically reserves the single probe for
        this instance; another caller (process or thread) is refused until the
        probe records an outcome or its reservation window expires.
        """
        now = self._clock()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._row()
            if row is None:
                self._conn.execute("COMMIT")
                return
            open_until = row[2]
            probe_token = row[3]
            if open_until is None:
                self._conn.execute("COMMIT")
                return
            if probe_token is not None and probe_token == self._probe_token:
                # This caller already holds the half-open probe; allow its
                # retries and record path to proceed.
                self._conn.execute("COMMIT")
                return
            open_until_value = self._coerce_timestamp(open_until, "open_until")
            if now < open_until_value:
                opened_at = (
                    self._coerce_timestamp(row[1], "opened_at")
                    if row[1] is not None
                    else now
                )
                self._conn.execute("COMMIT")
                raise CircuitOpenError(
                    f"circuit {self.key!r} is open until {open_until_value:.3f} "
                    f"(opened at {opened_at:.3f}); refusing to send"
                )
            window = max(self.cooldown_seconds, self._MIN_PROBE_RESERVATION_SECONDS)
            self._conn.execute(
                "UPDATE transport_circuit_state SET probe_token = ?, opened_at = ?, "
                "open_until = ?, updated_at = ? WHERE circuit_key = ?",
                (self._probe_token, now, now + window, now, self.key),
            )
            self._conn.execute("COMMIT")
        except CircuitOpenError:
            raise
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def record_success(self) -> None:
        self._write_locked(0, None, None, None)

    def release_probe(self) -> None:
        """Release a half-open probe reservation held by this instance.

        Used when a probe completes with an outcome that neither resets nor
        advances the breaker (for example an operator-actionable 4xx), so the
        circuit returns to the probe-able state instead of staying reserved for
        the full reservation window. A no-op when this instance holds no
        reservation.
        """
        now = self._clock()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "UPDATE transport_circuit_state SET probe_token = NULL, "
                "open_until = MIN(COALESCE(open_until, ?), ?), updated_at = ? "
                "WHERE circuit_key = ? AND probe_token = ?",
                (now, now, now, self.key, self._probe_token),
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def record_failure(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._row()
            failures = (
                self._coerce_failures(row[0]) if row is not None else 0
            ) + 1
            now = self._clock()
            if failures >= self.failure_threshold:
                self._write(failures, now, now + self.cooldown_seconds, None)
            else:
                self._write(failures, None, None, None)
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def _write_locked(
        self,
        failures: int,
        opened_at: float | None,
        open_until: float | None,
        probe_token: str | None,
    ) -> None:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._write(failures, opened_at, open_until, probe_token)
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def _write(
        self,
        failures: int,
        opened_at: float | None,
        open_until: float | None,
        probe_token: str | None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO transport_circuit_state(circuit_key, consecutive_failures, opened_at, "
            "open_until, probe_token, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(circuit_key) DO UPDATE SET "
            "consecutive_failures = excluded.consecutive_failures, "
            "opened_at = excluded.opened_at, open_until = excluded.open_until, "
            "probe_token = excluded.probe_token, "
            "updated_at = excluded.updated_at",
            (self.key, failures, opened_at, open_until, probe_token, self._clock()),
        )


def iter_provenance(store: ObservatoryStore) -> list[dict[str, Any]]:
    """Return every stored transport provenance row, oldest first."""
    _ensure_transport_schema(store.conn)
    rows = store.conn.execute(
        "SELECT * FROM transport_provenance ORDER BY provenance_id"
    ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


@dataclass
class HttpResponse:
    """A minimal, framework-free HTTP response."""

    status: int | None
    headers: Any
    body: bytes


@dataclass
class SendResult:
    """Outcome of one bounded send (including retries)."""

    ok: bool
    response: Any = None
    http_status: int | None = None
    attempts: int = 0
    latency_ms: float = 0.0
    usage: dict[str, Any] | None = None
    failure_class: str | None = None
    error: str | None = None
    retry_after: float | None = None


@dataclass
class ClassifyResult:
    """Outcome of one end-to-end classify call, safe to print or store."""

    outcome: str
    request_hash: str
    snapshot_hash: str
    subject_kind: str
    taxonomy_version: str
    question_hash: str
    model: str
    endpoint: str
    attempts: int = 0
    http_status: int | None = None
    latency_ms: float = 0.0
    usage: dict[str, Any] | None = None
    cost_usd: float | None = None
    cost_known: bool = False
    classification_id: int | None = None
    deduplicated: bool = False
    answers: list[dict[str, Any]] | None = None
    failure_class: str | None = None
    error: str | None = None
    credential_masked: str = "<unset>"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe, secret-free result mapping."""
        return {
            "outcome": self.outcome,
            "request_hash": self.request_hash,
            "snapshot_hash": self.snapshot_hash,
            "subject_kind": self.subject_kind,
            "taxonomy_version": self.taxonomy_version,
            "question_hash": self.question_hash,
            "model": self.model,
            "endpoint": self.endpoint,
            "attempts": self.attempts,
            "http_status": self.http_status,
            "latency_ms": round(self.latency_ms, 3),
            "usage": self.usage,
            "cost_usd": self.cost_usd,
            "cost_known": self.cost_known,
            "classification_id": self.classification_id,
            "deduplicated": self.deduplicated,
            "answers": self.answers,
            "failure_class": self.failure_class,
            "error": self.error,
            "credential_masked": self.credential_masked,
        }


def _header(headers: Any, name: str) -> str | None:
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(name)
        if value is None:
            value = getter(name.lower())
        if value is not None:
            return str(value)
    return None


def _parse_retry_after(value: str | None, *, now: Callable[[], float] = time.time) -> float | None:
    """Parse a ``Retry-After`` header (delay-seconds or HTTP-date).

    A non-finite numeric value such as ``"nan"`` is treated as unparseable so
    the caller falls back to backoff instead of retrying immediately; HTTP-date
    values are evaluated against the injected clock.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        try:
            target = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        seconds = target.timestamp() - now()
    if math.isnan(seconds):
        return None
    return max(0.0, seconds)


def _status_of(response: Any) -> int | None:
    status = getattr(response, "status", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return status
    getcode = getattr(response, "getcode", None)
    if callable(getcode):
        code = getcode()
        if isinstance(code, int) and not isinstance(code, bool):
            return code
    return None


def _read_body(response: Any, max_bytes: int) -> bytes:
    """Read at most ``max_bytes + 1`` bytes from *response*.

    Reading one byte past the cap lets the caller distinguish an over-cap body
    from an exactly-at-cap one without buffering the whole (possibly unbounded)
    response in memory. A short read means the reader is exhausted (``urlopen``
    reads up to the requested amount and returns short only at EOF); a reader
    that ignores the requested size is still truncated at the cap boundary.
    """
    reader = getattr(response, "read", None)
    if not callable(reader):
        return b""
    limit = max_bytes + 1
    chunks: list[bytes] = []
    total = 0
    while total < limit:
        requested = min(65536, limit - total)
        chunk = reader(requested)
        if not chunk:
            break
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        chunk = bytes(chunk)
        chunks.append(chunk)
        total += len(chunk)
        if len(chunk) < requested:
            # Short read: the reader has no more data (single-shot readers land
            # here immediately instead of being polled forever).
            break
    return b"".join(chunks)[:limit]


def _close(response: Any) -> None:
    closer = getattr(response, "close", None)
    if callable(closer):
        try:
            closer()
        except OSError:
            pass


class JevTransport:
    """HTTP transport with retries, budgets, and a persisted circuit breaker.

    HTTP is injected through *urlopen* so tests can substitute a fake without
    touching the network; the default is :func:`urllib.request.urlopen`.
    """

    def __init__(
        self,
        credential: str,
        config: TransportConfig | None = None,
        *,
        urlopen: Callable[..., Any] | None = None,
        sleeper: Callable[[float], Any] | None = None,
        clock: Callable[[], float] | None = None,
        random_source: Callable[[], float] | None = None,
        circuit: CircuitBreaker | None = None,
    ):
        self.credential = credential
        self.config = config if config is not None else TransportConfig()
        self._urlopen = urlopen if urlopen is not None else urllib.request.urlopen
        self._sleep = sleeper if sleeper is not None else time.sleep
        self._clock = clock if clock is not None else time.time
        self._random = random_source if random_source is not None else random.random
        self._circuit = circuit
        # Concurrency 1: one in-flight classification per transport instance.
        self._lock = threading.Lock()

    # -- helpers -----------------------------------------------------------

    def _redact(self, text: Any) -> str:
        return redact_text(text, self.credential)

    def _compute_delay(self, attempt: int, retry_after: float | None) -> float:
        policy = self.config.retry
        if retry_after is not None:
            return max(0.0, min(float(retry_after), policy.max_retry_after_seconds))
        base = min(policy.base_delay_seconds * (2 ** (attempt - 1)), policy.max_delay_seconds)
        factor = 1.0 + policy.jitter * (2.0 * float(self._random()) - 1.0)
        # The jitter is applied before the cap so a positive jitter cannot push
        # the delay above the configured ceiling.
        return max(0.0, min(base * factor, policy.max_delay_seconds))

    def _http_post(self, body: bytes, max_bytes: int) -> HttpResponse:
        headers = {
            "Authorization": f"Bearer {self.credential}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": self.config.user_agent,
        }
        request = urllib.request.Request(
            self.config.endpoint, data=body, headers=headers, method="POST"
        )
        try:
            raw = self._urlopen(request, timeout=self.config.timeout_seconds)
        except urllib.error.HTTPError as exc:
            try:
                response_body = _read_body(exc, max_bytes)
            finally:
                _close(exc)
            return HttpResponse(
                status=exc.code,
                headers=getattr(exc, "headers", None),
                body=response_body,
            )
        except (TimeoutError, socket.timeout) as exc:
            raise TransportTimeoutError(
                f"request to {self.config.endpoint} timed out after "
                f"{self.config.timeout_seconds}s"
            ) from exc
        except http.client.HTTPException as exc:
            # HTTPException (BadStatusLine, IncompleteRead, InvalidURL, ...) is
            # not an OSError, so without this arm it would escape past the CLI as
            # a raw traceback with no provenance row.
            raise TransportConnectionError(
                self._redact(f"connection to {self.config.endpoint} failed: {exc}")
            ) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise TransportTimeoutError(
                    f"request to {self.config.endpoint} timed out after "
                    f"{self.config.timeout_seconds}s"
                ) from exc
            raise TransportConnectionError(
                self._redact(f"connection to {self.config.endpoint} failed: {exc}")
            ) from exc
        except OSError as exc:
            raise TransportConnectionError(
                self._redact(f"connection to {self.config.endpoint} failed: {exc}")
            ) from exc
        try:
            return HttpResponse(
                status=_status_of(raw),
                headers=getattr(raw, "headers", None),
                body=_read_body(raw, max_bytes),
            )
        finally:
            _close(raw)

    # -- send --------------------------------------------------------------

    def send(self, request: JevRequest) -> SendResult:
        """Send *request* with the configured bounded policy.

        Configuration refusals (model drift) raise; transient or remote failures
        return a failed :class:`SendResult`.
        """
        if request.model != self.config.pinned_model and not self.config.allow_model_drift:
            raise ModelDriftError(
                f"refusing to send model {request.model!r}; pinned model is "
                f"{self.config.pinned_model!r} (allow model drift to override)"
            )
        with self._lock:
            return self._send_locked(request)

    def _send_locked(self, request: JevRequest) -> SendResult:
        config = self.config
        budget = config.budget
        body = request.serialized.encode("utf-8")
        attempts = 0
        last_status: int | None = None
        last_failure: str | None = None
        last_error: str | None = None
        last_retry_after: float | None = None
        last_latency_ms: float = 0.0

        for attempt in range(1, config.retry.max_attempts + 1):
            attempts = attempt
            if self._circuit is not None:
                try:
                    self._circuit.check()
                except CircuitOpenError as exc:
                    return SendResult(
                        ok=False,
                        attempts=attempt - 1,
                        failure_class="circuit_open",
                        error=self._redact(exc),
                    )
            try:
                budget.check_before_request()
            except BudgetExceededError as exc:
                if self._circuit is not None:
                    self._circuit.release_probe()
                return SendResult(
                    ok=False,
                    attempts=attempt - 1,
                    failure_class="budget_exhausted",
                    error=self._redact(exc),
                )
            budget.record_request()
            started = self._clock()
            try:
                response = self._http_post(body, config.max_response_bytes)
            except TransportTimeoutError as exc:
                last_failure = "timeout"
                last_error = self._redact(exc)
                last_latency_ms = max(0.0, (self._clock() - started) * 1000.0)
                if attempt < config.retry.max_attempts:
                    if not budget.can_attempt_more():
                        last_failure = "budget_exhausted"
                        last_error = self._redact("budget exhausted before the next retry")
                        break
                    self._sleep(self._compute_delay(attempt, None))
                    continue
                break
            except TransportConnectionError as exc:
                last_failure = "connection"
                last_error = self._redact(exc)
                last_latency_ms = max(0.0, (self._clock() - started) * 1000.0)
                if attempt < config.retry.max_attempts:
                    if not budget.can_attempt_more():
                        last_failure = "budget_exhausted"
                        last_error = self._redact("budget exhausted before the next retry")
                        break
                    self._sleep(self._compute_delay(attempt, None))
                    continue
                break
            latency_ms = max(0.0, (self._clock() - started) * 1000.0)
            last_latency_ms = latency_ms
            status = response.status

            if status is None:
                last_failure = "invalid_status"
                last_error = self._redact("response carried no HTTP status")
                if attempt < config.retry.max_attempts:
                    if not budget.can_attempt_more():
                        last_failure = "budget_exhausted"
                        last_error = self._redact("budget exhausted before the next retry")
                        break
                    self._sleep(self._compute_delay(attempt, None))
                    continue
                break

            # The body is already truncated to cap+1 bytes by ``_read_body``, so
            # this bound holds for both the 2xx and the error paths and never
            # buffers an unbounded error body.
            if len(response.body) > config.max_response_bytes:
                last_status = status
                last_failure = "response_too_large"
                last_error = self._redact(
                    f"response body is {len(response.body)} bytes, exceeding the "
                    f"{config.max_response_bytes}-byte cap"
                )
                break

            if 200 <= status < 300:
                try:
                    text = response.body.decode("utf-8")
                except UnicodeDecodeError as exc:
                    last_status = status
                    last_failure = "malformed_json"
                    last_error = self._redact(f"response body is not valid UTF-8: {exc}")
                    break
                try:
                    parsed = parse_json_document(text, what="response")
                except ResponseError as exc:
                    last_status = status
                    last_failure = "malformed_json"
                    last_error = self._redact(exc)
                    break
                usage = parsed.get("usage") if isinstance(parsed, dict) else None
                # Validate usage before charging, and expose only the validated
                # integer token fields. Server-controlled usage is never passed
                # through verbatim, so an extra key can never leak a credential.
                validated_usage: dict[str, int] | None = None
                if isinstance(usage, Mapping):
                    try:
                        normalized = _validate_usage(usage)
                    except ResponseError:
                        normalized = None
                    if normalized is not None:
                        validated_usage = {
                            "input_tokens": normalized["input_tokens"],
                            "output_tokens": normalized["output_tokens"],
                        }
                        budget.record_usage(validated_usage)
                if self._circuit is not None:
                    self._circuit.record_success()
                return SendResult(
                    ok=True,
                    response=parsed,
                    http_status=status,
                    attempts=attempt,
                    latency_ms=latency_ms,
                    usage=validated_usage,
                )

            last_status = status
            retry_after = _parse_retry_after(
                _header(response.headers, "Retry-After"), now=self._clock
            )
            last_retry_after = retry_after
            last_failure = f"http_{status}"
            last_error = self._redact(f"HTTP {status} from {config.endpoint}")
            retryable = status in RETRYABLE_STATUSES
            if retryable and attempt < config.retry.max_attempts:
                if not budget.can_attempt_more():
                    last_failure = "budget_exhausted"
                    last_error = self._redact("budget exhausted before the next retry")
                    break
                self._sleep(self._compute_delay(attempt, retry_after))
                continue
            break

        if self._circuit is not None:
            if last_failure in _CIRCUIT_COUNTED_FAILURES:
                self._circuit.record_failure()
            else:
                # A reachable-but-not-transient outcome (e.g. an operator-
                # actionable 4xx) must not leave the half-open probe reserved.
                self._circuit.release_probe()
        return SendResult(
            ok=False,
            http_status=last_status,
            attempts=attempts,
            latency_ms=last_latency_ms,
            failure_class=last_failure or "unknown_failure",
            error=last_error,
            retry_after=last_retry_after,
        )


# ---------------------------------------------------------------------------
# End-to-end classify
# ---------------------------------------------------------------------------


def _pending_result(
    request: JevRequest,
    config: TransportConfig,
    *,
    failure_class: str,
    error: str,
    http_status: int | None = None,
    attempts: int = 0,
    latency_ms: float = 0.0,
    usage: dict[str, Any] | None = None,
    credential_masked: str = "<unset>",
) -> ClassifyResult:
    return ClassifyResult(
        outcome=OUTCOME_PENDING,
        request_hash=request.request_hash,
        snapshot_hash=request.snapshot_hash,
        subject_kind=request.subject_kind,
        taxonomy_version=request.taxonomy_version,
        question_hash=request.question_hash,
        model=request.model,
        endpoint=config.endpoint,
        attempts=attempts,
        http_status=http_status,
        latency_ms=latency_ms,
        usage=usage,
        cost_usd=config.budget.cost_usd,
        cost_known=config.budget.cost_known,
        failure_class=failure_class,
        error=error,
        credential_masked=credential_masked,
    )


def _record_provenance(
    store: ObservatoryStore,
    request: JevRequest,
    config: TransportConfig,
    result: ClassifyResult,
    credential_masked: str,
) -> None:
    _ensure_transport_schema(store.conn)
    usage = result.usage if isinstance(result.usage, Mapping) else {}
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    created_at = (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    store.conn.execute(
        "INSERT INTO transport_provenance(request_hash, snapshot_hash, subject_kind, "
        "taxonomy_version, question_hash, model, endpoint, outcome, failure_class, "
        "attempts, http_status, latency_ms, input_tokens, output_tokens, usage_json, "
        "cost_usd, cost_known, credential_masked, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            request.request_hash,
            request.snapshot_hash,
            request.subject_kind,
            request.taxonomy_version,
            request.question_hash,
            request.model,
            config.endpoint,
            result.outcome,
            result.failure_class,
            result.attempts,
            result.http_status,
            result.latency_ms,
            input_tokens if isinstance(input_tokens, int) else None,
            output_tokens if isinstance(output_tokens, int) else None,
            canonical_json(dict(usage)) if usage else None,
            result.cost_usd,
            1 if result.cost_known else 0,
            credential_masked,
            created_at,
        ),
    )


def _record_store_error(
    store: ObservatoryStore,
    request: JevRequest,
    config: TransportConfig,
    exc: BaseException,
    credential: str | None = None,
) -> None:
    """Best-effort pending provenance row for a local store failure.

    A broken store may not be writable at all (``--db /dev/null``); the row is
    therefore attempted, never required.
    """
    result = _pending_result(
        request,
        config,
        failure_class="store_error",
        error=redact_text(f"store error: {exc}", credential),
    )
    try:
        _record_provenance(store, request, config, result, "<unset>")
    except sqlite3.Error:
        pass


def _classify_impl(
    store: ObservatoryStore,
    request: JevRequest,
    *,
    credential: str | None,
    config: TransportConfig,
    urlopen: Callable[..., Any] | None = None,
    sleeper: Callable[[float], Any] | None = None,
    clock: Callable[[], float] | None = None,
    random_source: Callable[[], float] | None = None,
) -> ClassifyResult:
    # A request is persisted before the network call so a failed send still has
    # replayable provenance for a later saved response.
    persist_request(store, request)

    try:
        secret = credential if credential is not None else load_credential()
    except CredentialError as exc:
        result = _pending_result(
            request,
            config,
            failure_class="credential_error",
            error=redact_text(exc, credential),
        )
        _record_provenance(store, request, config, result, "<unset>")
        return result

    masked = mask_secret(secret)
    circuit = CircuitBreaker(
        store.conn,
        key=config.circuit_key,
        failure_threshold=config.circuit_failure_threshold,
        cooldown_seconds=config.circuit_cooldown_seconds,
        clock=clock if clock is not None else time.time,
    )
    transport = JevTransport(
        secret,
        config,
        urlopen=urlopen,
        sleeper=sleeper,
        clock=clock,
        random_source=random_source,
        circuit=circuit,
    )

    try:
        with _CLASSIFY_LOCK:
            send = transport.send(request)
    except ModelDriftError as exc:
        result = _pending_result(
            request,
            config,
            failure_class="model_drift",
            error=redact_text(exc, secret),
            credential_masked=masked,
        )
        _record_provenance(store, request, config, result, masked)
        return result
    except TransportError as exc:
        # Best-effort pending provenance first (so a corrupted projection still
        # leaves a replayable row), then surface the failure to the CLI. The
        # CLI renders ``error: ...`` and exits 1 without a traceback; swallowing
        # it here would hide an operator-actionable data-integrity fault.
        result = _pending_result(
            request,
            config,
            failure_class="transport_error",
            error=redact_text(exc, secret),
            credential_masked=masked,
        )
        _record_provenance(store, request, config, result, masked)
        raise

    if send.ok:
        try:
            imported = import_response(store, send.response, request_hash=request.request_hash)
        except ObservatoryError as exc:
            result = _pending_result(
                request,
                config,
                failure_class="schema_invalid",
                error=redact_text(exc, secret),
                http_status=send.http_status,
                attempts=send.attempts,
                latency_ms=send.latency_ms,
                usage=send.usage,
                credential_masked=masked,
            )
            _record_provenance(store, request, config, result, masked)
            return result
        result = ClassifyResult(
            outcome=OUTCOME_CLASSIFIED,
            request_hash=request.request_hash,
            snapshot_hash=request.snapshot_hash,
            subject_kind=request.subject_kind,
            taxonomy_version=request.taxonomy_version,
            question_hash=request.question_hash,
            model=request.model,
            endpoint=config.endpoint,
            attempts=send.attempts,
            http_status=send.http_status,
            latency_ms=send.latency_ms,
            usage=send.usage,
            cost_usd=config.budget.cost_usd,
            cost_known=config.budget.cost_known,
            classification_id=imported.classification_id,
            deduplicated=imported.deduplicated,
            answers=imported.answers,
            credential_masked=masked,
        )
        _record_provenance(store, request, config, result, masked)
        return result

    result = _pending_result(
        request,
        config,
        failure_class=send.failure_class or "unknown_failure",
        error=send.error or "",
        http_status=send.http_status,
        attempts=send.attempts,
        latency_ms=send.latency_ms,
        usage=send.usage,
        credential_masked=masked,
    )
    _record_provenance(store, request, config, result, masked)
    return result


def classify(
    store: ObservatoryStore,
    request: JevRequest,
    *,
    credential: str | None = None,
    config: TransportConfig | None = None,
    allow_model_drift: bool | None = None,
    urlopen: Callable[..., Any] | None = None,
    sleeper: Callable[[float], Any] | None = None,
    clock: Callable[[], float] | None = None,
    random_source: Callable[[], float] | None = None,
) -> ClassifyResult:
    """Send one built request and record its outcome.

    The credential is loaded from the runtime environment unless one is passed
    explicitly (for embedding). Every outcome -- success, invalid response,
    operator-actionable status, exhausted budget, open circuit, or missing
    credential -- is recorded as provenance, and a failed *send* is stored as
    ``pending``/unknown rather than raised. A transport-layer exception (for
    example a corrupted persisted circuit row) is still recorded as a
    ``transport_error`` pending row, then re-raised so the CLI renders
    ``error: ...`` and exits 1. Configuration refusals and transport errors never
    escape as a traceback past the CLI.

    A local ``sqlite3`` failure from using the projection is surfaced as an
    :class:`ObservatoryError` (so the CLI prints ``error: ...`` and exits 1)
    after a best-effort ``store_error`` pending row when the store is usable.
    """
    if config is None:
        config = TransportConfig()
    if allow_model_drift is not None:
        config = replace(config, allow_model_drift=bool(allow_model_drift))
    try:
        return _classify_impl(
            store,
            request,
            credential=credential,
            config=config,
            urlopen=urlopen,
            sleeper=sleeper,
            clock=clock,
            random_source=random_source,
        )
    except sqlite3.Error as exc:
        _record_store_error(store, request, config, exc, credential)
        raise ObservatoryError(f"sqlite error: {exc}") from exc


__all__ = [
    "DEFAULT_ENDPOINT",
    "PINNED_MODEL",
    "MAX_TIMEOUT_SECONDS",
    "RETRYABLE_STATUSES",
    "OUTCOME_CLASSIFIED",
    "OUTCOME_PENDING",
    "Budget",
    "CircuitBreaker",
    "ClassifyResult",
    "CredentialError",
    "HttpResponse",
    "JevTransport",
    "ModelDriftError",
    "BudgetExceededError",
    "CircuitOpenError",
    "RetryPolicy",
    "SendResult",
    "TransportConfig",
    "TransportConfigError",
    "TransportConnectionError",
    "TransportError",
    "TransportTimeoutError",
    "classify",
    "iter_provenance",
    "load_credential",
    "mask_secret",
    "redact_text",
]
