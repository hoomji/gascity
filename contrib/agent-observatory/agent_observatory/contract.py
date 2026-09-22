"""Normalized JSONL import contract.

The observatory stores *observed evidence*. A tool request record does not prove
the tool executed, and does not prove success. Import only accepts explicitly
supplied, already-normalized records: there is no crawling of home directories
or provider transcript formats, and no field for raw reasoning or secrets.

Strictness rules:

* required string fields must be present and non-empty strings;
* optional fields, when present and non-null, must have the declared type;
* ``bool`` is never accepted where an integer is expected;
* unknown keys are dropped -- they stay NULL in the projection rather than being
  silently promoted to a known column;
* timestamps must be timezone-aware ISO-8601, normalized to UTC microseconds
  for ordering and hashing, with the raw input retained as provenance;
* ``duration_ms`` is rejected when negative.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .canonical import IDENTITY_FIELDS, canonical_hash, event_identity
from .errors import ContractError

# Bump when the normalized record shape changes in a non-backwards-compatible way.
SCHEMA_VERSION = "1.0"
SUPPORTED_SCHEMA_VERSIONS = ("1.0",)

REQUIRED_STRING_FIELDS = (
    "schema_version",
    "city_id",
    "host_id",
    "provider",
    "session_id",
    "event_id",
    "timestamp",
    "kind",
)

OPTIONAL_STRING_FIELDS = (
    "title",
    "text",
    "tool_name",
    "tool_call_id",
    "command",
    "model",
    "repo",
    "commit_sha",
    "parent_session_id",
    "bead_id",
    "formula_id",
)

OPTIONAL_INT_FIELDS = (
    "exit_code",
    "duration_ms",
)

USAGE_INT_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "total_tokens",
)

KNOWN_FIELDS = frozenset(REQUIRED_STRING_FIELDS + OPTIONAL_STRING_FIELDS + OPTIONAL_INT_FIELDS + ("usage",))

# The evidence payload is everything except the canonical identity fields. The
# identity is matched separately (SQL PRIMARY KEY / WHERE clauses), and
# ``event_snapshot_hash`` covers identity plus this payload hash.
PAYLOAD_FIELDS = tuple(sorted(field for field in KNOWN_FIELDS if field not in IDENTITY_FIELDS))

# Fields whose NULL counts are surfaced in report coverage. Timestamp/kind are
# required, so their presence is guaranteed by the contract.
COVERAGE_FIELDS = OPTIONAL_STRING_FIELDS + OPTIONAL_INT_FIELDS + ("usage",)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def normalize_timestamp(
    value: str,
    source_path: str | None = None,
    line_number: int | None = None,
) -> str:
    """Validate an ISO-8601 timestamp and return a canonical UTC string.

    Timestamps **must** carry an explicit UTC offset (or ``Z``). A naive
    timestamp is rejected because ``fromisoformat`` would otherwise accept it,
    leaving its timezone ambiguous. The returned form is UTC at microsecond
    precision so plain string ordering equals chronological ordering even for
    mixed offsets and mixed fractional widths.
    """
    candidate = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ContractError(f"timestamp is not ISO-8601: {value!r}", source_path, line_number) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(
            f"timestamp must include a UTC offset or Z (timezone-naive is ambiguous): {value!r}",
            source_path,
            line_number,
        )
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def validate_record(
    raw: Any,
    source_path: str | None = None,
    line_number: int | None = None,
) -> dict[str, Any]:
    """Validate one decoded JSON record and return a normalized copy.

    Raises :class:`ContractError` with source/line context on any violation.
    Unknown keys are dropped; missing optional fields become ``None``.
    """
    if not isinstance(raw, dict):
        raise ContractError("record must be a JSON object", source_path, line_number)

    normalized: dict[str, Any] = {}

    for field in REQUIRED_STRING_FIELDS:
        if field not in raw or raw[field] is None:
            raise ContractError(f"missing required field {field!r}", source_path, line_number)
        value = raw[field]
        if not isinstance(value, str):
            raise ContractError(
                f"field {field!r} must be a string, got {type(value).__name__}",
                source_path,
                line_number,
            )
        if not value:
            raise ContractError(f"field {field!r} must not be empty", source_path, line_number)
        normalized[field] = value

    if normalized["schema_version"] not in SUPPORTED_SCHEMA_VERSIONS:
        raise ContractError(
            f"unsupported schema_version {normalized['schema_version']!r}; "
            f"supported: {', '.join(SUPPORTED_SCHEMA_VERSIONS)}",
            source_path,
            line_number,
        )

    # ``timestamp`` becomes the canonical UTC form used for ordering and hashing;
    # ``observed_timestamp`` preserves the exact input string as provenance.
    raw_timestamp = normalized["timestamp"]
    normalized["observed_timestamp"] = raw_timestamp
    normalized["timestamp"] = normalize_timestamp(raw_timestamp, source_path, line_number)

    for field in OPTIONAL_STRING_FIELDS:
        value = raw.get(field)
        if value is None:
            normalized[field] = None
            continue
        if not isinstance(value, str):
            raise ContractError(
                f"field {field!r} must be a string or null, got {type(value).__name__}",
                source_path,
                line_number,
            )
        normalized[field] = value

    for field in OPTIONAL_INT_FIELDS:
        value = raw.get(field)
        if value is None:
            normalized[field] = None
            continue
        if not _is_int(value):
            raise ContractError(
                f"field {field!r} must be an integer or null, got {type(value).__name__}",
                source_path,
                line_number,
            )
        if field == "duration_ms" and value < 0:
            raise ContractError(
                f"field {field!r} must be nonnegative, got {value!r}",
                source_path,
                line_number,
            )
        normalized[field] = value

    usage = raw.get("usage")
    if usage is None:
        normalized["usage"] = None
    else:
        if not isinstance(usage, dict):
            raise ContractError(
                f"field 'usage' must be an object or null, got {type(usage).__name__}",
                source_path,
                line_number,
            )
        normalized_usage: dict[str, int | None] = {}
        for field in USAGE_INT_FIELDS:
            value = usage.get(field)
            if value is None:
                normalized_usage[field] = None
                continue
            if not _is_int(value) or value < 0:
                raise ContractError(
                    f"field 'usage.{field}' must be a nonnegative integer or null, "
                    f"got {value!r}",
                    source_path,
                    line_number,
                )
            normalized_usage[field] = value
        normalized["usage"] = normalized_usage

    return normalized


def payload_hash(record: dict[str, Any]) -> str:
    """Hash the evidence payload that defines an event's content.

    Canonical identity fields (``city_id``/``host_id``/``provider``/``session_id``/
    ``event_id``) and provenance are excluded by construction; identity is
    covered separately by ``canonical.event_snapshot_hash``. The caller passes an
    already-normalized record, and unknown keys are already dropped.
    """
    payload = {field: record.get(field) for field in PAYLOAD_FIELDS}
    return canonical_hash(payload)


def record_identity(record: dict[str, Any]) -> tuple[str, str, str, str, str]:
    """Return the canonical identity of a normalized record."""
    return event_identity(record)
