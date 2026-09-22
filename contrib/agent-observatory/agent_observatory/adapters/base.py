"""Shared types and helpers for read-only native transcript adapters.

An adapter turns one provider transcript into records that satisfy the
normalized JSONL contract in :mod:`agent_observatory.contract`. Adapters are
read-only: they never mutate a source and never execute transcript content.

Event identity follows the plan's rule: prefer a provider-native stable id;
otherwise fall back to source generation + record position + canonical digest.
Title text and other prose are never part of identity.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..canonical import canonical_json, sha256_text
from ..errors import ObservatoryError

# Bump when the adapter output shape changes in a way callers must notice.
ADAPTER_CONTRACT_VERSION = "1.0"


class AdapterError(ObservatoryError):
    """A provider transcript could not be adapted.

    Carries the source path and, where known, the 1-based line number so the
    failure points at the exact record instead of an opaque stack trace.
    """

    def __init__(self, message: str, source_path: str | None = None, line_number: int | None = None):
        self.source_path = source_path
        self.line_number = line_number
        location = ""
        if source_path is not None and line_number is not None:
            location = f"{source_path}:{line_number}: "
        elif source_path is not None:
            location = f"{source_path}: "
        super().__init__(location + message)


@dataclass(frozen=True)
class AdapterContext:
    """City/host scope applied to every record an adapter emits."""

    city_id: str
    host_id: str
    repo: str | None = None


@dataclass(frozen=True)
class TitleRevision:
    """One observed title value for a session, with its source position."""

    title: str
    position: int
    observed_timestamp: str | None = None
    source: str | None = None


@dataclass
class AdapterResult:
    """Everything one adapter learned from one source file."""

    provider: str
    adapter_version: str
    source_path: str
    source_sha256: str
    source_size_bytes: int
    session_id: str
    parent_session_id: str | None
    records: list[dict[str, Any]] = field(default_factory=list)
    title_revisions: list[TitleRevision] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    partial_trailing_line: bool = False
    line_count: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def note_skip(self, reason: str, detail: str | None = None) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1
        if detail:
            self.errors.append(detail)

    def coverage(self) -> dict[str, Any]:
        """Return deterministic, transcript-free coverage for this source."""

        by_kind: dict[str, int] = {}
        usage_events = 0
        tool_calls = 0
        tool_results = 0
        for record in self.records:
            kind = record.get("kind") or ""
            by_kind[kind] = by_kind.get(kind, 0) + 1
            if record.get("usage") is not None:
                usage_events += 1
            if kind == "tool_call":
                tool_calls += 1
            elif kind == "tool_result":
                tool_results += 1
        return {
            "records_emitted": len(self.records),
            "sessions": 1 if self.records or self.session_id else 0,
            "by_kind": dict(sorted(by_kind.items())),
            "usage_events": usage_events,
            "tool_calls": tool_calls,
            "tool_results": tool_results,
            "title_revisions": len(self.title_revisions),
            "skipped": dict(sorted(self.skipped.items())),
            "partial_trailing_line": self.partial_trailing_line,
            "errors": list(self.errors),
        }


class SourceAdapter:
    """Base class for the provider-native readers."""

    provider = ""
    adapter_version = "0.0.0"

    def decompress(self, raw: bytes, source_path: str) -> bytes:
        """Return the logical content of *raw* (identity for uncompressed files)."""

        return raw

    def detect(self, source_path: str) -> bool:
        """Return whether *source_path* looks like this adapter's format."""

        raise NotImplementedError

    def parse(
        self,
        data: bytes,
        *,
        context: AdapterContext,
        generation: int,
        source_path: str,
        source_sha256: str,
    ) -> AdapterResult:
        """Parse decompressed *data* into an :class:`AdapterResult`."""

        raise NotImplementedError


def split_jsonl(data: bytes, source_path: str) -> tuple[list[tuple[int, Any]], bool, list[str]]:
    """Split JSONL on LF only and decode each non-blank line.

    Returns ``(records, partial_trailing_line, errors)`` where *records* are
    ``(line_number, decoded)`` pairs. A malformed interior line raises
    :class:`AdapterError`; a malformed final line with no trailing newline is a
    *partial trailing line* -- it is reported in *errors* and skipped rather
    than silently dropped or allowed to abort the whole file.

    A missing final newline is **not** itself partial: an unterminated final
    line that parses cleanly is a complete record, so ``partial_trailing_line``
    is only set when that final line actually fails to parse. A UTF-8 BOM at
    the start of the file is tolerated (stripped) so it cannot abort the whole
    source or masquerade as a partial line.
    """

    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AdapterError(f"source is not valid UTF-8: {exc}", source_path) from exc

    raw_lines = text.split("\n")
    if raw_lines and raw_lines[-1] == "":
        # A fully terminated file ends with an empty element that we drop.
        raw_lines = raw_lines[:-1]
        last_index = -1
    else:
        # The final element is an unterminated line; keep it so a parse failure
        # can be reported as partial instead of aborting the whole file.
        last_index = len(raw_lines) - 1

    partial = False
    records: list[tuple[int, Any]] = []
    errors: list[str] = []
    for index, line in enumerate(raw_lines):
        if line.endswith("\r"):
            line = line[:-1]
        line_number = index + 1
        if not line.strip():
            continue
        try:
            decoded = json.loads(line, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError) as exc:
            if index == last_index:
                errors.append(f"{source_path}:{line_number}: partial trailing line not parsed: {exc}")
                partial = True
                continue
            raise AdapterError(f"invalid JSON: {exc}", source_path, line_number) from exc
        records.append((line_number, decoded))
    return records, partial, errors


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant {value!r} is not allowed")


def fallback_event_id(
    provider: str,
    generation: int,
    position: int,
    kind: str,
    raw: Any,
) -> str:
    """Return a stable id for a record with no provider-native identifier.

    Identity is generation + record position + canonical digest, never text or
    title alone. The same source bytes in the same generation always produce the
    same id, so replay is idempotent; a rewritten generation produces new ids.
    """

    digest = sha256_text(canonical_json(raw))[:16]
    return f"{provider}-g{generation}-p{position}-{kind}-{digest}"


def iso_from_epoch_millis(value: Any) -> str | None:
    """Convert epoch milliseconds to a UTC ISO-8601 string, or ``None``.

    Returns ``None`` for missing/non-numeric input rather than inventing a time.
    """

    from datetime import datetime, timezone

    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def extract_command(tool_name: str | None, arguments: Any) -> str | None:
    """Return a shell command from decoded tool arguments, if present.

    Only explicit command-like keys are used (``command``/``cmd``); the function
    never executes or parses the value beyond JSON decoding.
    """

    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (json.JSONDecodeError, ValueError):
            return None
    if not isinstance(arguments, dict):
        return None
    for key in ("command", "cmd", "script"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None
