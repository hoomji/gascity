"""SQLite-backed analytical projection.

The database is a *derived* artifact: beads and events remain the authoritative
record. Everything here is rebuildable from source JSONL, so the schema is
versioned and imports are transactional and idempotent. A partial or malformed
file never commits; a conflicting reuse of an event identity is rejected rather
than silently overwritten.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .canonical import (
    canonical_hash,
    canonical_json,
    event_snapshot_hash,
    identity_key,
    sha256_bytes,
    session_snapshot_hash,
)
from .contract import (
    USAGE_INT_FIELDS,
    payload_hash,
    record_identity,
    validate_record,
)
from .errors import (
    ContractError,
    ImportConflictError,
    LabelConflictError,
    SchemaVersionError,
)

# Bump when the projection schema changes. The database is derived and
# rebuildable, so an older schema is rejected rather than migrated in place.
DB_SCHEMA_VERSION = 2

# Normalized record fields, in table order. ``observed_timestamp`` is not here:
# it is derived provenance (the raw input string), not part of the payload hash.
_DATA_COLUMNS = (
    "city_id",
    "host_id",
    "provider",
    "session_id",
    "event_id",
    "timestamp",
    "kind",
    "title",
    "text",
    "tool_name",
    "tool_call_id",
    "command",
    "exit_code",
    "duration_ms",
    "model",
    "repo",
    "commit_sha",
    "parent_session_id",
    "bead_id",
    "formula_id",
)

# Derived provenance: payload hash plus the exact source of the observation.
_PROVENANCE_COLUMNS = (
    "payload_hash",
    "source_path",
    "source_sha256",
    "source_line",
    "observed_timestamp",
)

_EVENT_COLUMNS = _DATA_COLUMNS + _PROVENANCE_COLUMNS

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS imported_files (
        source_path TEXT NOT NULL,
        source_sha256 TEXT NOT NULL,
        line_count INTEGER NOT NULL,
        imported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        PRIMARY KEY (source_path, source_sha256)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        city_id TEXT NOT NULL,
        host_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        session_id TEXT NOT NULL,
        parent_session_id TEXT,
        first_timestamp TEXT,
        PRIMARY KEY (city_id, host_id, provider, session_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        city_id TEXT NOT NULL,
        host_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        session_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        kind TEXT NOT NULL,
        title TEXT,
        text TEXT,
        tool_name TEXT,
        tool_call_id TEXT,
        command TEXT,
        exit_code INTEGER,
        duration_ms INTEGER,
        model TEXT,
        repo TEXT,
        commit_sha TEXT,
        parent_session_id TEXT,
        bead_id TEXT,
        formula_id TEXT,
        payload_hash TEXT NOT NULL,
        source_path TEXT NOT NULL,
        source_sha256 TEXT NOT NULL,
        source_line INTEGER NOT NULL,
        observed_timestamp TEXT,
        PRIMARY KEY (city_id, host_id, provider, session_id, event_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS event_usage (
        city_id TEXT NOT NULL,
        host_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        session_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        input_tokens INTEGER,
        output_tokens INTEGER,
        cache_read_tokens INTEGER,
        cache_write_tokens INTEGER,
        total_tokens INTEGER,
        PRIMARY KEY (city_id, host_id, provider, session_id, event_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS jev_requests (
        request_hash TEXT PRIMARY KEY,
        snapshot_hash TEXT NOT NULL,
        subject_kind TEXT NOT NULL,
        taxonomy_version TEXT NOT NULL,
        question_hash TEXT NOT NULL,
        model TEXT NOT NULL,
        request_json TEXT NOT NULL,
        request_bytes INTEGER NOT NULL,
        source_path TEXT,
        source_sha256 TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS classifications (
        classification_id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_kind TEXT NOT NULL,
        snapshot_hash TEXT NOT NULL,
        taxonomy_version TEXT NOT NULL,
        question_hash TEXT NOT NULL,
        model_version TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        response_hash TEXT NOT NULL,
        source_path TEXT,
        source_sha256 TEXT,
        UNIQUE (subject_kind, snapshot_hash, taxonomy_version, question_hash, model_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS classification_answers (
        classification_id INTEGER NOT NULL,
        question_id TEXT NOT NULL,
        question_type TEXT NOT NULL,
        value_json TEXT NOT NULL,
        PRIMARY KEY (classification_id, question_id),
        FOREIGN KEY (classification_id) REFERENCES classifications(classification_id) ON DELETE CASCADE
    )
    """,
)

@dataclass
class ImportResult:
    """Outcome of one JSONL import."""

    source_path: str
    source_sha256: str
    lines_read: int = 0
    inserted: int = 0
    duplicates: int = 0
    skipped_identical_file: bool = False


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant {value!r} is not allowed")


def _split_jsonl_lines(text: str) -> list[str]:
    """Split JSONL text on ``"\\n"`` only, stripping a trailing ``"\\r"``.

    ``str.splitlines()`` also breaks on U+2028, U+2029 and U+0085, which are
    legal unescaped inside JSON strings (RFC 8259). Splitting on those would
    tear one valid JSON object into several undecodable fragments, so JSONL
    framing must follow the physical newline only. A CRLF file still yields
    clean lines because the trailing carriage return is removed.
    """
    return [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]


class ObservatoryStore:
    """A rebuildable analytical projection over normalized observatory JSONL."""

    SCHEMA_VERSION = DB_SCHEMA_VERSION

    def __init__(self, path: str | os.PathLike[str]):
        self.path = str(path)
        if self.path != ":memory:":
            parent = os.path.dirname(os.path.abspath(self.path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        try:
            self._ensure_schema()
        except BaseException:
            self._conn.close()
            raise

    # -- lifecycle ---------------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ObservatoryStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _ensure_schema(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, self.SCHEMA_VERSION):
            raise SchemaVersionError(
                f"database {self.path!r} has schema version {version}; "
                f"this build supports only {self.SCHEMA_VERSION}"
            )
        for statement in _SCHEMA_STATEMENTS:
            self._conn.execute(statement)
        if version == 0:
            self._conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
        row = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                (str(self.SCHEMA_VERSION),),
            )
        elif row[0] != str(self.SCHEMA_VERSION):
            raise SchemaVersionError(
                f"database {self.path!r} metadata schema version is {row[0]!r}; "
                f"this build supports only {self.SCHEMA_VERSION}"
            )

    # -- import ------------------------------------------------------------

    def import_jsonl(self, source: str | os.PathLike[str]) -> ImportResult:
        """Import one JSONL file atomically.

        The whole file is parsed and type-checked before anything is written, and
        the writes happen in a single transaction. A malformed line aborts the
        file with its 1-based line number and commits nothing.
        """
        source_path = str(source)
        try:
            data = Path(source_path).read_bytes()
        except OSError as exc:
            raise ContractError(f"cannot read import file: {exc}", source_path) from exc
        digest = sha256_bytes(data)

        already = self._conn.execute(
            "SELECT 1 FROM imported_files WHERE source_path = ? AND source_sha256 = ?",
            (source_path, digest),
        ).fetchone()
        if already is not None:
            result = ImportResult(source_path, digest, skipped_identical_file=True)
            result.lines_read = self._count_lines(data)
            return result

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContractError(f"import file is not valid UTF-8: {exc}", source_path) from exc

        parsed: list[tuple[int, dict[str, Any]]] = []
        for line_number, line in enumerate(_split_jsonl_lines(text), start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line, parse_constant=_reject_json_constant)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ContractError(f"invalid JSON: {exc}", source_path, line_number) from exc
            parsed.append((line_number, validate_record(raw, source_path, line_number)))

        result = ImportResult(source_path, digest, lines_read=len(parsed))
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for line_number, record in parsed:
                duplicate = self._insert_record(record, source_path, digest, line_number)
                if duplicate:
                    result.duplicates += 1
                else:
                    result.inserted += 1
            self._conn.execute(
                "INSERT INTO imported_files(source_path, source_sha256, line_count) "
                "VALUES (?, ?, ?)",
                (source_path, digest, len(parsed)),
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return result

    @staticmethod
    def _count_lines(data: bytes) -> int:
        return sum(1 for line in _split_jsonl_lines(data.decode("utf-8", errors="replace")) if line.strip())

    def _insert_record(
        self,
        record: dict[str, Any],
        source_path: str,
        source_sha256: str,
        source_line: int,
    ) -> bool:
        identity = record_identity(record)
        digest = payload_hash(record)
        existing = self._conn.execute(
            "SELECT payload_hash FROM events WHERE city_id = ? AND host_id = ? AND "
            "provider = ? AND session_id = ? AND event_id = ?",
            identity,
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] != digest:
                raise ImportConflictError(
                    "refusing to overwrite event "
                    f"{identity_key(identity)} (existing payload differs); "
                    f"source {source_path}:{source_line}"
                )
            return True

        self._conn.execute(
            "INSERT OR IGNORE INTO sessions(city_id, host_id, provider, session_id, "
            "parent_session_id, first_timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (
                identity[0],
                identity[1],
                identity[2],
                identity[3],
                record.get("parent_session_id"),
                record.get("timestamp"),
            ),
        )
        if record.get("parent_session_id"):
            self._conn.execute(
                "UPDATE sessions SET parent_session_id = ? WHERE city_id = ? AND host_id = ? "
                "AND provider = ? AND session_id = ? AND parent_session_id IS NULL",
                (
                    record["parent_session_id"],
                    identity[0],
                    identity[1],
                    identity[2],
                    identity[3],
                ),
            )
        self._conn.execute(
            f"INSERT INTO events({', '.join(_EVENT_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in _EVENT_COLUMNS)})",
            [
                *(record.get(column) for column in _DATA_COLUMNS),
                digest,
                source_path,
                source_sha256,
                source_line,
                record.get("observed_timestamp"),
            ],
        )
        usage = record.get("usage")
        if usage is not None:
            self._conn.execute(
                "INSERT INTO event_usage(city_id, host_id, provider, session_id, event_id, "
                "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, total_tokens) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                identity + tuple(usage.get(field) for field in USAGE_INT_FIELDS),
            )
        return False

    # -- read helpers ------------------------------------------------------

    def schema_version(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def event_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def session_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])

    def iter_events(self) -> Iterator[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM events ORDER BY timestamp, city_id, host_id, provider, session_id, event_id"
        ).fetchall()
        for row in rows:
            yield dict(row)

    def get_event(self, identity: Sequence[str]) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM events WHERE city_id = ? AND host_id = ? AND provider = ? "
            "AND session_id = ? AND event_id = ?",
            tuple(identity),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_usage(self, identity: Sequence[str]) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM event_usage WHERE city_id = ? AND host_id = ? AND provider = ? "
            "AND session_id = ? AND event_id = ?",
            tuple(identity),
        ).fetchone()
        return dict(row) if row is not None else None

    def session_keys(self) -> list[tuple[str, str, str, str]]:
        rows = self._conn.execute(
            "SELECT city_id, host_id, provider, session_id FROM sessions "
            "ORDER BY city_id, host_id, provider, session_id"
        ).fetchall()
        return [tuple(row) for row in rows]

    def session_events(self, session_key: Sequence[str]) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE city_id = ? AND host_id = ? AND provider = ? "
            "AND session_id = ? ORDER BY timestamp, event_id",
            tuple(session_key),
        ).fetchall()
        return [dict(row) for row in rows]

    def event_snapshot(self, session_key: Sequence[str], event_id: str) -> str:
        row = self.get_event(tuple(session_key) + (event_id,))
        if row is None:
            raise ContractError(
                f"event {identity_key(tuple(session_key) + (event_id,))} not found in projection"
            )
        return event_snapshot_hash(tuple(session_key) + (event_id,), row["payload_hash"])

    def session_snapshot(self, session_key: Sequence[str]) -> str:
        events = self.session_events(session_key)
        if not events:
            raise ContractError(
                f"session {identity_key(session_key)} has no events in projection"
            )
        return session_snapshot_hash(
            session_key, [(event["event_id"], event["payload_hash"]) for event in events]
        )

    # -- Jev request provenance -------------------------------------------

    def save_request(
        self,
        *,
        request_hash: str,
        snapshot_hash: str,
        subject_kind: str,
        taxonomy_version: str,
        question_hash: str,
        model: str,
        request_json: str,
        request_bytes: int,
        source_path: str | None = None,
        source_sha256: str | None = None,
    ) -> bool:
        """Persist a generated request. Returns True when newly stored."""
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO jev_requests(request_hash, snapshot_hash, subject_kind, "
            "taxonomy_version, question_hash, model, request_json, request_bytes, source_path, "
            "source_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request_hash,
                snapshot_hash,
                subject_kind,
                taxonomy_version,
                question_hash,
                model,
                request_json,
                request_bytes,
                source_path,
                source_sha256,
            ),
        )
        return cursor.rowcount == 1

    def get_request(self, request_hash: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM jev_requests WHERE request_hash = ?", (request_hash,)
        ).fetchone()
        return dict(row) if row is not None else None

    # -- classifications ---------------------------------------------------

    def save_classification(
        self,
        *,
        subject_kind: str,
        snapshot_hash: str,
        taxonomy_version: str,
        question_hash: str,
        model_version: str,
        request_hash: str,
        response_hash: str,
        answers: Iterable[dict[str, Any]],
        source_path: str | None = None,
        source_sha256: str | None = None,
    ) -> tuple[int, bool]:
        """Store an immutable classification.

        Returns ``(classification_id, deduplicated)``. Replaying the identical
        response deduplicates; a different response for the same subject and
        taxonomy is rejected rather than overwriting the stored label.
        """
        existing = self._conn.execute(
            "SELECT classification_id, response_hash FROM classifications WHERE "
            "subject_kind = ? AND snapshot_hash = ? AND taxonomy_version = ? AND "
            "question_hash = ? AND model_version = ?",
            (subject_kind, snapshot_hash, taxonomy_version, question_hash, model_version),
        ).fetchone()
        if existing is not None:
            if existing["response_hash"] == response_hash:
                return int(existing["classification_id"]), True
            raise LabelConflictError(
                "refusing to overwrite classification for subject "
                f"{subject_kind}:{snapshot_hash} taxonomy {taxonomy_version} "
                f"model {model_version}"
            )

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._conn.execute(
                "INSERT INTO classifications(subject_kind, snapshot_hash, taxonomy_version, "
                "question_hash, model_version, request_hash, response_hash, source_path, "
                "source_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    subject_kind,
                    snapshot_hash,
                    taxonomy_version,
                    question_hash,
                    model_version,
                    request_hash,
                    response_hash,
                    source_path,
                    source_sha256,
                ),
            )
            classification_id = int(cursor.lastrowid)
            for answer in answers:
                self._conn.execute(
                    "INSERT INTO classification_answers(classification_id, question_id, "
                    "question_type, value_json) VALUES (?, ?, ?, ?)",
                    (
                        classification_id,
                        answer["question_id"],
                        answer["question_type"],
                        canonical_json(answer["answer"]),
                    ),
                )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return classification_id, False

    def get_classification(
        self, *, subject_kind: str, snapshot_hash: str, taxonomy_version: str,
        question_hash: str, model_version: str,
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM classifications WHERE subject_kind = ? AND snapshot_hash = ? "
            "AND taxonomy_version = ? AND question_hash = ? AND model_version = ?",
            (subject_kind, snapshot_hash, taxonomy_version, question_hash, model_version),
        ).fetchone()
        if row is None:
            return None
        answers = self._conn.execute(
            "SELECT question_id, question_type, value_json FROM classification_answers "
            "WHERE classification_id = ? ORDER BY question_id",
            (row["classification_id"],),
        ).fetchall()
        record = dict(row)
        record["answers"] = [
            {
                "question_id": answer["question_id"],
                "question_type": answer["question_type"],
                "answer": json.loads(answer["value_json"]),
            }
            for answer in answers
        ]
        return record

    def classification_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM classifications").fetchone()[0])


def content_hash(value: Any) -> str:
    """Hash arbitrary JSON content canonically (used for responses)."""
    return canonical_hash(value)
