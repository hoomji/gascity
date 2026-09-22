"""M4 checkpointed backfill, debounced collection and the classification queue.

The collector is a separate, bounded process. Agents never wait on it: it reads
explicit transcript roots, imports what changed into the projection, and queues
changed sessions for a later, budgeted classification drain. Nothing here is
consulted by dispatch or routing.

Guarantees:

* **Every scoped source has a status and a reason.** Each discovered file,
  unsupported provider file, unlistable directory and vanished source is kept
  in ``collector_sources`` with one of :data:`SOURCE_STATUSES`. Files no
  adapter recognizes (locks, notes, sidecars) are counted per run by suffix in
  ``ignored_files`` rather than dropped silently.
* **Checkpoints make replay safe.** A source is re-read only when its stat
  changes; imports go through the idempotent store import, so a crash between
  import and checkpoint re-imports as duplicates and counts are preserved.
* **Bounded runs.** Per-run source/byte caps and a projection size cap defer
  work (``deferred`` with a reason) rather than dropping it. At least one
  changed source makes progress per run, so a byte cap cannot wedge the
  backlog; a per-source cap keeps files too large to parse in memory out
  entirely, visible as ``deferred``.
* **Debounce.** A file modified within ``debounce_seconds`` is still being
  written; it is ``debounced`` and picked up on a later run. A file that never
  goes quiet is read anyway once ``max_debounce_seconds`` have passed since
  its last import (or first sighting); the adapters hold back a partial
  trailing line, so a live transcript is collected incrementally.
* **Kill switch.** When the switch file exists (or the environment variable
  :data:`KILL_SWITCH_ENV` is ``1``) collection and draining stop without
  touching the city.
* **The unknown queue.** A session whose classification keeps failing ends in
  ``unknown`` with its last failure, never as a fabricated label. Budget
  exhaustion, an open circuit and credential/model refusals leave items
  ``pending`` and stop the drain instead of burning attempts.

The default classification state is **metadata only** (event kinds, tool names,
command categories, models, duration). No transcript text, titles or command
lines are sent: a text-bearing state is a separate, explicit data-scope
decision.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from .adapters import AdapterContext, AdapterError, load_source_data, validated_records
from .canonical import identity_key, sha256_text
from .commands import categorize_command
from .errors import ContractError, ObservatoryError
from .inventory import SourceRoot, _decide_generation, discover_sources, records_to_jsonl
from .jev import build_request
from .store import ObservatoryStore
from .taxonomy import Taxonomy
from .transport import (
    OUTCOME_CLASSIFIED,
    ClassifyResult,
    TransportConfig,
    TransportError,
    classify,
)

try:  # POSIX advisory locking; the collector degrades to no lock elsewhere.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

COLLECTOR_STATE_VERSION = "1.0"
KILL_SWITCH_ENV = "OBSERVATORY_COLLECTOR_DISABLED"

SOURCE_STATUSES = (
    "imported",
    "unchanged",
    "debounced",
    "deferred",
    "error",
    "unreadable",
    "unsupported",
    "missing",
)
QUEUE_STATUSES = ("pending", "done", "unknown", "superseded")

# Failure classes that say nothing about the subject: the drain stops and the
# item stays pending with no attempt charged. A rejected key (401/403) and a
# throttle that outlasted the transport's retries (429/529) are account or
# provider state, not evidence about the session.
_STOP_FAILURES = frozenset(
    {
        "budget_exhausted",
        "circuit_open",
        "credential_error",
        "model_drift",
        "http_401",
        "http_403",
        "http_429",
        "http_529",
    }
)
# Failed sources are retried only when their stat changes, so a file that
# cannot parse does not take a per-run slot on every pass.
_STAT_SKIP_STATUSES = frozenset({"imported", "unchanged", "error", "unreadable"})

_COLLECTOR_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS collector_sources (
        realpath TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        provider TEXT,
        root TEXT,
        path TEXT NOT NULL,
        status TEXT NOT NULL,
        reason TEXT NOT NULL,
        change TEXT,
        generation INTEGER,
        content_sha256 TEXT,
        logical_size INTEGER,
        raw_size INTEGER,
        mtime REAL,
        records INTEGER,
        partial_trailing_line INTEGER,
        adapter_errors INTEGER,
        first_seen_at REAL NOT NULL,
        last_seen_at REAL NOT NULL,
        last_imported_at REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS collector_runs (
        run_id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at REAL NOT NULL,
        finished_at REAL NOT NULL,
        status TEXT NOT NULL,
        summary_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS collector_queue (
        queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
        city_id TEXT NOT NULL,
        host_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        session_id TEXT NOT NULL,
        snapshot_hash TEXT NOT NULL,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at REAL NOT NULL,
        last_failure_class TEXT,
        last_error TEXT,
        classification_id INTEGER,
        enqueued_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE (city_id, host_id, provider, session_id, snapshot_hash)
    )
    """,
    "CREATE INDEX IF NOT EXISTS collector_queue_status ON collector_queue(status, next_attempt_at)",
)


def ensure_collector_schema(conn: Any) -> None:
    """Create the collector tables lazily (additive; no projection rebuild)."""

    for statement in _COLLECTOR_SCHEMA:
        conn.execute(statement)


# -- configuration ---------------------------------------------------------


@dataclass(frozen=True)
class CollectorConfig:
    """One collector's scope and bounds."""

    roots: tuple[SourceRoot, ...]
    city_id: str
    host_id: str
    repo: str | None = None
    debounce_seconds: float = 30.0
    max_debounce_seconds: float = 600.0
    max_sources_per_run: int | None = None
    max_bytes_per_run: int | None = None
    max_source_bytes: int | None = None
    max_db_bytes: int | None = None
    kill_switch_path: str | None = None
    spool_dir: str | None = None
    enqueue: bool = True

    def __post_init__(self) -> None:
        if not self.roots:
            raise ObservatoryError("collector requires at least one explicit root")
        if not self.city_id or not self.host_id:
            raise ObservatoryError("collector requires city_id and host_id")
        if self.debounce_seconds < 0:
            raise ObservatoryError("debounce_seconds must be >= 0")
        if self.max_debounce_seconds < self.debounce_seconds:
            raise ObservatoryError("max_debounce_seconds must be >= debounce_seconds")
        for name in ("max_sources_per_run", "max_bytes_per_run", "max_source_bytes", "max_db_bytes"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                raise ObservatoryError(f"{name} must be a positive integer when set")


def default_kill_switch_path(db_path: str) -> str:
    return f"{db_path}.collector-disabled"


def kill_switch_engaged(path: str | None, environ: dict[str, str] | None = None) -> str | None:
    """Return why collection is disabled, or ``None`` when it may run."""

    env = os.environ if environ is None else environ
    if env.get(KILL_SWITCH_ENV, "").strip() in {"1", "true", "yes", "on"}:
        return f"{KILL_SWITCH_ENV} is set"
    if path and os.path.exists(path):
        return f"kill switch file present: {path}"
    return None


def set_kill_switch(path: str, disabled: bool) -> bool:
    """Engage (create) or release (remove) the kill switch file; return new state."""

    if disabled:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(
            f"collector disabled at {_iso(time.time())}\n", encoding="utf-8"
        )
        return True
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    return False


# -- collection ------------------------------------------------------------


@dataclass
class CollectRun:
    """What one collection pass did; safe to print (no transcript content)."""

    status: str
    reason: str | None = None
    started_at: float = 0.0
    finished_at: float = 0.0
    by_status: dict[str, int] = field(default_factory=dict)
    by_change: dict[str, int] = field(default_factory=dict)
    events_inserted: int = 0
    events_duplicate: int = 0
    events_conflicting: int = 0
    bytes_read: int = 0
    sessions_enqueued: int = 0
    queue_superseded: int = 0
    ignored_files: dict[str, int] = field(default_factory=dict)
    run_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "by_status": dict(sorted(self.by_status.items())),
            "by_change": dict(sorted(self.by_change.items())),
            "events_inserted": self.events_inserted,
            "events_duplicate": self.events_duplicate,
            "events_conflicting": self.events_conflicting,
            "bytes_read": self.bytes_read,
            "sessions_enqueued": self.sessions_enqueued,
            "queue_superseded": self.queue_superseded,
            "ignored_files": dict(sorted(self.ignored_files.items())),
            "run_id": self.run_id,
        }


@contextlib.contextmanager
def _collector_lock(db_path: str) -> Iterator[bool]:
    """Hold a non-blocking exclusive lock beside the projection; yield acquired."""

    if db_path == ":memory:" or fcntl is None:
        yield True
        return
    lock_path = f"{db_path}.collector.lock"
    Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def collect_once(
    store: ObservatoryStore,
    config: CollectorConfig,
    *,
    clock: Callable[[], float] = time.time,
    environ: dict[str, str] | None = None,
) -> CollectRun:
    """Run one bounded, checkpointed collection pass over the configured roots."""

    ensure_collector_schema(store.conn)
    started = clock()
    run = CollectRun(status="ok", started_at=started)

    disabled = kill_switch_engaged(config.kill_switch_path, environ)
    if disabled is not None:
        run.status, run.reason = "disabled", disabled
        return _finish_run(store, run, clock)

    with _collector_lock(store.path) as acquired:
        if not acquired:
            run.status, run.reason = "locked", "another collector holds the projection lock"
            return _finish_run(store, run, clock)
        _collect_locked(store, config, run, started)
    return _finish_run(store, run, clock)


def _collect_locked(store: ObservatoryStore, config: CollectorConfig, run: CollectRun, now: float) -> None:
    context = AdapterContext(city_id=config.city_id, host_id=config.host_id, repo=config.repo)
    unreadable: list[dict[str, Any]] = []
    ignored: list[str] = []
    sources, unsupported = discover_sources(config.roots, unreadable=unreadable, ignored=ignored)
    for path in ignored:
        suffix = Path(path).suffix.lower() or "(none)"
        run.ignored_files[suffix] = run.ignored_files.get(suffix, 0) + 1
    previous = _source_rows(store)
    seen: set[str] = set()
    processed = 0
    touched: set[tuple[str, str, str, str]] = set()

    # A stable spool path keeps import provenance and identical-file skips
    # stable across runs; spooled JSONL is deleted right after each import.
    spool = config.spool_dir or (None if store.path == ":memory:" else f"{store.path}.collector-spool")
    spool_ctx = (
        contextlib.nullcontext(spool) if spool else tempfile.TemporaryDirectory(prefix="observatory-spool-")
    )
    with spool_ctx as spool_dir:
        Path(spool_dir).mkdir(parents=True, exist_ok=True)
        for source in sources:
            seen.add(source.realpath)
            prev = previous.get(source.realpath)
            row = _base_row(source.realpath, source.provider, source.root, source.path, prev, now)
            try:
                stat = os.stat(source.realpath)
            except OSError as exc:
                _save_source(store, row, "unreadable", f"cannot stat source: {exc}", run)
                continue

            if _stat_unchanged(prev, stat):
                if prev["status"] in {"error", "unreadable"}:
                    _save_source(store, row, prev["status"], prev["reason"], run)
                else:
                    _save_source(store, row, "unchanged", "size and mtime match the checkpoint", run)
                continue
            # Record the observed stat so lag is measurable while the file waits.
            # A waiting status is never ``unchanged``-eligible, and generation
            # decisions use the last imported content hash, not this stat.
            row.update(raw_size=stat.st_size, mtime=stat.st_mtime)
            quiet_for = now - stat.st_mtime
            waiting_since = (prev.get("last_imported_at") or prev.get("first_seen_at")) if prev else now
            starved = now - waiting_since >= config.max_debounce_seconds
            if quiet_for < config.debounce_seconds and not starved:
                _save_source(
                    store,
                    row,
                    "debounced",
                    f"modified {max(quiet_for, 0.0):.1f}s ago; waits for {config.debounce_seconds:g}s of quiet",
                    run,
                )
                continue
            deferral = _deferral_reason(store, config, processed, run.bytes_read, stat.st_size)
            if deferral is not None:
                _save_source(store, row, "deferred", deferral, run)
                continue

            processed += 1
            run.bytes_read += stat.st_size
            _import_source(store, context, source, prev, row, stat, spool_dir, run, touched, config.enqueue)

        for record in unreadable:
            realpath = str(record.get("realpath") or record.get("path"))
            if realpath in seen:
                continue
            seen.add(realpath)
            row = _base_row(realpath, record.get("provider"), record.get("root"), record["path"], previous.get(realpath), now)
            _save_source(store, row, "unreadable", f"cannot list directory: {record.get('error')}", run)

        for entry in unsupported:
            path = str(entry.get("path") or "")
            realpath = os.path.realpath(path)
            if realpath in seen:
                continue
            seen.add(realpath)
            row = _base_row(realpath, entry.get("provider"), entry.get("root"), path, previous.get(realpath), now)
            _save_source(store, row, "unsupported", str(entry.get("reason")), run)

    scoped_roots = {root.path for root in config.roots}
    for realpath, prev in previous.items():
        if realpath in seen or prev["status"] == "missing" or prev["root"] not in scoped_roots:
            continue
        row = dict(prev)
        row["last_seen_at"] = prev["last_seen_at"]
        _save_source(store, row, "missing", "source no longer present under its root; prior events are kept", run)



def _deferral_reason(
    store: ObservatoryStore,
    config: CollectorConfig,
    processed: int,
    bytes_read: int,
    size: int,
) -> str | None:
    if config.max_db_bytes is not None and store.path != ":memory:":
        with contextlib.suppress(OSError):
            db_size = os.path.getsize(store.path)
            if db_size >= config.max_db_bytes:
                return f"projection size {db_size} bytes reached the storage cap {config.max_db_bytes}"
    # Adapters parse a whole source in memory (peak RSS is several times the
    # file size), so an oversized file waits for a streaming reader instead of
    # riding the first-source exemption below.
    if config.max_source_bytes is not None and size > config.max_source_bytes:
        return f"source is {size} bytes, above the per-source cap {config.max_source_bytes}"
    # The first changed source always proceeds so one large file cannot wedge
    # the backlog behind a byte cap it alone exceeds.
    if processed == 0:
        return None
    if config.max_sources_per_run is not None and processed >= config.max_sources_per_run:
        return f"per-run source cap reached ({processed}/{config.max_sources_per_run})"
    if config.max_bytes_per_run is not None and bytes_read + size > config.max_bytes_per_run:
        return f"per-run byte cap would be exceeded ({bytes_read + size}/{config.max_bytes_per_run})"
    return None


def _import_source(
    store: ObservatoryStore,
    context: AdapterContext,
    source: Any,
    prev: dict[str, Any] | None,
    row: dict[str, Any],
    stat: os.stat_result,
    spool_dir: str,
    run: CollectRun,
    touched: set[tuple[str, str, str, str]],
    enqueue: bool = True,
) -> None:
    try:
        adapter, data, digest = load_source_data(source.path, provider=source.provider)
    except AdapterError as exc:
        _save_source(store, row, "unreadable", str(exc), run)
        return
    except OSError as exc:
        # I/O trouble may clear on its own: drop the stat so the next run retries.
        row.update(mtime=None)
        _save_source(store, row, "unreadable", str(exc), run)
        return

    manifest_prev = None
    if prev and prev.get("content_sha256"):
        manifest_prev = {
            "content_generation": prev["content_sha256"],
            "generation": prev.get("generation"),
            "size_bytes": prev.get("logical_size"),
        }
    generation, change, _supersedes = _decide_generation(manifest_prev, data, digest)
    row.update(
        generation=generation,
        content_sha256=digest,
        logical_size=len(data),
        raw_size=stat.st_size,
        mtime=stat.st_mtime,
        change=change,
    )
    try:
        result = adapter.parse(
            data,
            context=context,
            generation=generation,
            source_path=source.path,
            source_sha256=digest,
        )
        records = validated_records(result.records, source.path, result)
    except AdapterError as exc:
        _save_source(store, row, "error", str(exc), run)
        return

    spool_path = os.path.join(spool_dir, f"{row['source_id']}.jsonl")
    try:
        Path(spool_path).write_text(records_to_jsonl(records), encoding="utf-8")
        imported = store.import_jsonl(spool_path)
    except (ContractError, OSError) as exc:
        # A store-side failure says nothing about the file: always retry it.
        row.update(mtime=None)
        _save_source(store, row, "error", f"import failed: {exc}", run)
        return
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(spool_path)

    run.events_inserted += imported.inserted
    run.events_duplicate += imported.duplicates
    run.events_conflicting += imported.skipped_conflicts
    run.by_change[change] = run.by_change.get(change, 0) + 1
    sessions = {(record["city_id"], record["host_id"], record["provider"], record["session_id"]) for record in records}
    touched.update(sessions)
    # Queue before the "imported" checkpoint: a crash between the two re-imports
    # the source next run (duplicates are ignored and re-enqueueing an unchanged
    # snapshot is a no-op), while the reverse order would lose the sessions.
    if enqueue and sessions:
        now = row["last_seen_at"]
        enqueued, superseded = enqueue_sessions(store, sorted(sessions), clock=lambda: now)
        run.sessions_enqueued += enqueued
        run.queue_superseded += superseded
    row.update(
        records=len(records),
        partial_trailing_line=1 if result.partial_trailing_line else 0,
        adapter_errors=len(result.errors),
        last_imported_at=row["last_seen_at"],
    )
    reason = f"{change} generation {generation}: {imported.inserted} new, {imported.duplicates} duplicate events"
    if imported.skipped_conflicts:
        reason += f", {imported.skipped_conflicts} conflicting identities skipped"
    if result.partial_trailing_line:
        reason += "; partial trailing line held for the next pass"
    _save_source(store, row, "imported", reason, run)


def _stat_unchanged(prev: dict[str, Any] | None, stat: os.stat_result) -> bool:
    return (
        prev is not None
        and prev.get("status") in _STAT_SKIP_STATUSES
        and prev.get("raw_size") == stat.st_size
        and prev.get("mtime") == stat.st_mtime
    )


def _base_row(
    realpath: str,
    provider: str | None,
    root: str | None,
    path: str,
    prev: dict[str, Any] | None,
    now: float,
) -> dict[str, Any]:
    row = dict(prev) if prev else {
        "realpath": realpath,
        "source_id": sha256_text(realpath)[:32],
        "first_seen_at": now,
        "change": None,
        "generation": None,
        "content_sha256": None,
        "logical_size": None,
        "raw_size": None,
        "mtime": None,
        "records": None,
        "partial_trailing_line": None,
        "adapter_errors": None,
        "last_imported_at": None,
    }
    row.update(provider=provider, root=root, path=path, last_seen_at=now)
    return row


_SOURCE_COLUMNS = (
    "realpath",
    "source_id",
    "provider",
    "root",
    "path",
    "status",
    "reason",
    "change",
    "generation",
    "content_sha256",
    "logical_size",
    "raw_size",
    "mtime",
    "records",
    "partial_trailing_line",
    "adapter_errors",
    "first_seen_at",
    "last_seen_at",
    "last_imported_at",
)


def _save_source(store: ObservatoryStore, row: dict[str, Any], status: str, reason: str, run: CollectRun) -> None:
    row["status"] = status
    row["reason"] = reason
    placeholders = ", ".join("?" for _ in _SOURCE_COLUMNS)
    store.conn.execute(
        f"INSERT OR REPLACE INTO collector_sources({', '.join(_SOURCE_COLUMNS)}) VALUES ({placeholders})",
        tuple(row.get(column) for column in _SOURCE_COLUMNS),
    )
    run.by_status[status] = run.by_status.get(status, 0) + 1


def _source_rows(store: ObservatoryStore) -> dict[str, dict[str, Any]]:
    rows = store.conn.execute(f"SELECT {', '.join(_SOURCE_COLUMNS)} FROM collector_sources").fetchall()
    return {row["realpath"]: dict(row) for row in rows}


def _finish_run(store: ObservatoryStore, run: CollectRun, clock: Callable[[], float]) -> CollectRun:
    run.finished_at = clock()
    summary = run.to_dict()
    summary.pop("run_id", None)
    cursor = store.conn.execute(
        "INSERT INTO collector_runs(started_at, finished_at, status, summary_json) VALUES (?, ?, ?, ?)",
        (run.started_at, run.finished_at, run.status, json.dumps(summary, sort_keys=True)),
    )
    run.run_id = int(cursor.lastrowid)
    return run


def collect_watch(
    store: ObservatoryStore,
    config: CollectorConfig,
    *,
    interval_seconds: float,
    iterations: int | None = None,
    clock: Callable[[], float] = time.time,
    sleeper: Callable[[float], Any] = time.sleep,
    environ: dict[str, str] | None = None,
    on_run: Callable[[CollectRun], Any] | None = None,
) -> list[CollectRun]:
    """Repeat :func:`collect_once` until the kill switch engages or *iterations* runs.

    The kill switch is checked before every pass, so engaging it stops the loop
    at the next interval without interrupting an import mid-transaction.
    """

    if interval_seconds <= 0:
        raise ObservatoryError("watch interval must be > 0 seconds")
    runs: list[CollectRun] = []
    while iterations is None or len(runs) < iterations:
        run = collect_once(store, config, clock=clock, environ=environ)
        runs.append(run)
        if on_run is not None:
            on_run(run)
        if run.status == "disabled":
            break
        if iterations is not None and len(runs) >= iterations:
            break
        sleeper(interval_seconds)
    return runs


# -- classification queue -------------------------------------------------


def enqueue_sessions(
    store: ObservatoryStore,
    session_keys: Iterable[Sequence[str]],
    *,
    clock: Callable[[], float] = time.time,
) -> tuple[int, int]:
    """Queue each session's current snapshot; supersede its older pending items.

    Returns ``(enqueued, superseded)``. Re-enqueueing an unchanged snapshot is a
    no-op, so replay never duplicates queue work.
    """

    ensure_collector_schema(store.conn)
    now = clock()
    enqueued = superseded = 0
    for key in session_keys:
        city_id, host_id, provider, session_id = key
        try:
            snapshot = store.session_snapshot(key)
        except ContractError:
            continue
        cursor = store.conn.execute(
            "INSERT OR IGNORE INTO collector_queue(city_id, host_id, provider, session_id, snapshot_hash, "
            "status, attempts, next_attempt_at, enqueued_at, updated_at) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)",
            (city_id, host_id, provider, session_id, snapshot, now, now, now),
        )
        enqueued += cursor.rowcount
        cursor = store.conn.execute(
            "UPDATE collector_queue SET status = 'superseded', updated_at = ? "
            "WHERE city_id = ? AND host_id = ? AND provider = ? AND session_id = ? "
            "AND snapshot_hash != ? AND status = 'pending'",
            (now, city_id, host_id, provider, session_id, snapshot),
        )
        superseded += cursor.rowcount
    return enqueued, superseded


def metadata_state(store: ObservatoryStore, session_key: Sequence[str]) -> dict[str, Any]:
    """Return a transcript-free classification state for one session.

    Only counts and identifiers already present as structured metadata are
    included. Titles, text and command lines are deliberately excluded.
    """

    events = store.session_events(session_key)
    kinds: dict[str, int] = {}
    tools: dict[str, int] = {}
    categories: dict[str, int] = {}
    models: set[str] = set()
    for event in events:
        kind = event.get("kind") or "unknown"
        kinds[kind] = kinds.get(kind, 0) + 1
        if kind in {"tool_call", "command"}:
            name = event.get("tool_name") or "unknown"
            tools[name] = tools.get(name, 0) + 1
            for category in categorize_command(event.get("command")):
                categories[category] = categories.get(category, 0) + 1
        if event.get("model"):
            models.add(event["model"])
    timestamps = sorted(event["timestamp"] for event in events if event.get("timestamp"))
    duration = None
    if len(timestamps) >= 2:
        duration = round((_parse_iso(timestamps[-1]) - _parse_iso(timestamps[0])), 3)
    top_tools = dict(sorted(tools.items(), key=lambda item: (-item[1], item[0]))[:20])
    return {
        "state_kind": "session_metadata",
        "state_version": COLLECTOR_STATE_VERSION,
        "provider": session_key[2],
        "events": len(events),
        "event_kinds": dict(sorted(kinds.items())),
        "tool_invocations": top_tools,
        "command_categories": dict(sorted(categories.items())),
        "models": sorted(models),
        "duration_seconds": duration,
        "has_bead": any(event.get("bead_id") for event in events),
        "has_formula": any(event.get("formula_id") for event in events),
        "has_parent_session": any(event.get("parent_session_id") for event in events),
    }


@dataclass
class DrainResult:
    """Outcome of one queue drain; safe to print."""

    status: str
    reason: str | None = None
    attempted: int = 0
    classified: int = 0
    retry_scheduled: int = 0
    unknown: int = 0
    superseded: int = 0
    budget: dict[str, Any] | None = None
    items: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "attempted": self.attempted,
            "classified": self.classified,
            "retry_scheduled": self.retry_scheduled,
            "unknown": self.unknown,
            "superseded": self.superseded,
            "budget": self.budget,
            "items": self.items,
        }


def drain_queue(
    store: ObservatoryStore,
    taxonomy: Taxonomy,
    *,
    transport_config: TransportConfig,
    max_items: int,
    max_attempts: int = 3,
    retry_backoff_seconds: float = 300.0,
    kill_switch_path: str | None = None,
    state_builder: Callable[[ObservatoryStore, Sequence[str]], dict[str, Any]] = metadata_state,
    classify_fn: Callable[..., ClassifyResult] = classify,
    clock: Callable[[], float] = time.time,
    environ: dict[str, str] | None = None,
) -> DrainResult:
    """Classify up to *max_items* due pending sessions within the transport budget."""

    if max_items < 0 or max_attempts < 1 or retry_backoff_seconds < 0:
        raise ObservatoryError("max_items >= 0, max_attempts >= 1 and retry_backoff_seconds >= 0 are required")
    ensure_collector_schema(store.conn)
    budget = transport_config.budget
    disabled = kill_switch_engaged(kill_switch_path, environ)
    if disabled is not None:
        return DrainResult(status="disabled", reason=disabled, budget=budget.to_state())

    now = clock()
    rows = store.conn.execute(
        "SELECT * FROM collector_queue WHERE status = 'pending' AND next_attempt_at <= ? "
        "ORDER BY enqueued_at, queue_id LIMIT ?",
        (now, max_items),
    ).fetchall()
    result = DrainResult(status="ok")
    for row in rows:
        key = (row["city_id"], row["host_id"], row["provider"], row["session_id"])
        try:
            current = store.session_snapshot(key)
        except ContractError:
            current = None
        if current != row["snapshot_hash"]:
            _update_queue(store, row["queue_id"], clock(), status="superseded")
            result.superseded += 1
            continue
        if not budget.can_attempt_more():
            result.status, result.reason = "stopped", budget.exhaustion_reason()
            break

        request = build_request(state_builder(store, key), taxonomy, snapshot_hash=current, subject_kind="session")
        item = {"session": identity_key(key), "snapshot_hash": current}
        result.attempted += 1
        try:
            outcome = classify_fn(store, request, config=transport_config)
        except TransportError as exc:
            _update_queue(store, row["queue_id"], clock(), last_failure_class="transport_error", last_error=str(exc))
            item.update(outcome="pending", failure_class="transport_error")
            result.items.append(item)
            result.status, result.reason = "stopped", f"transport error: {exc}"
            break

        item.update(outcome=outcome.outcome, failure_class=outcome.failure_class)
        result.items.append(item)
        if outcome.outcome == OUTCOME_CLASSIFIED:
            _update_queue(
                store,
                row["queue_id"],
                clock(),
                status="done",
                attempts=row["attempts"] + 1,
                classification_id=outcome.classification_id,
                last_failure_class=None,
                last_error=None,
            )
            result.classified += 1
            continue
        if outcome.failure_class in _STOP_FAILURES:
            _update_queue(
                store, row["queue_id"], clock(), last_failure_class=outcome.failure_class, last_error=outcome.error
            )
            result.status = "stopped"
            result.reason = f"{outcome.failure_class}: {outcome.error or 'no detail'}"
            break
        attempts = row["attempts"] + 1
        if attempts >= max_attempts:
            _update_queue(
                store,
                row["queue_id"],
                clock(),
                status="unknown",
                attempts=attempts,
                last_failure_class=outcome.failure_class,
                last_error=outcome.error,
            )
            result.unknown += 1
        else:
            _update_queue(
                store,
                row["queue_id"],
                clock(),
                attempts=attempts,
                next_attempt_at=clock() + retry_backoff_seconds * (2 ** (attempts - 1)),
                last_failure_class=outcome.failure_class,
                last_error=outcome.error,
            )
            result.retry_scheduled += 1
    result.budget = budget.to_state()
    return result


def _update_queue(store: ObservatoryStore, queue_id: int, now: float, **fields: Any) -> None:
    fields["updated_at"] = now
    assignments = ", ".join(f"{name} = ?" for name in fields)
    store.conn.execute(
        f"UPDATE collector_queue SET {assignments} WHERE queue_id = ?",
        (*fields.values(), queue_id),
    )


# -- status ----------------------------------------------------------------


def collector_status(
    store: ObservatoryStore,
    *,
    now: float | None = None,
    kill_switch_path: str | None = None,
    unknown_limit: int = 50,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return coverage, lag and queue health; no transcript content."""

    ensure_collector_schema(store.conn)
    now = time.time() if now is None else now
    sources = [dict(row) for row in store.conn.execute("SELECT * FROM collector_sources ORDER BY realpath")]
    by_status: dict[str, int] = {}
    by_provider: dict[str, dict[str, int]] = {}
    lagging: list[dict[str, Any]] = []
    for source in sources:
        status = source["status"]
        by_status[status] = by_status.get(status, 0) + 1
        provider = source["provider"] or "unknown"
        by_provider.setdefault(provider, {})
        by_provider[provider][status] = by_provider[provider].get(status, 0) + 1
        if status in {"debounced", "deferred", "error", "unreadable"} and source.get("mtime") is not None:
            lagging.append(
                {
                    "path": source["path"],
                    "status": status,
                    "reason": source["reason"],
                    "lag_seconds": round(max(now - source["mtime"], 0.0), 3),
                }
            )
    lagging.sort(key=lambda item: (-item["lag_seconds"], item["path"]))
    scoped = sum(count for status, count in by_status.items() if status not in {"unsupported", "missing"})
    current = by_status.get("imported", 0) + by_status.get("unchanged", 0)

    queue_counts = {status: 0 for status in QUEUE_STATUSES}
    for row in store.conn.execute("SELECT status, COUNT(*) AS n FROM collector_queue GROUP BY status"):
        queue_counts[row["status"]] = row["n"]
    oldest = store.conn.execute(
        "SELECT MIN(enqueued_at) FROM collector_queue WHERE status = 'pending'"
    ).fetchone()[0]
    unknown_items = [
        {
            "session": identity_key((row["city_id"], row["host_id"], row["provider"], row["session_id"])),
            "snapshot_hash": row["snapshot_hash"],
            "attempts": row["attempts"],
            "last_failure_class": row["last_failure_class"],
            "last_error": row["last_error"],
        }
        for row in store.conn.execute(
            "SELECT * FROM collector_queue WHERE status = 'unknown' ORDER BY updated_at, queue_id LIMIT ?",
            (unknown_limit,),
        )
    ]
    last_run = store.conn.execute(
        "SELECT run_id, started_at, finished_at, status, summary_json FROM collector_runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    return {
        "collector_state_version": COLLECTOR_STATE_VERSION,
        "disabled": kill_switch_engaged(kill_switch_path, environ),
        "sources": {
            "total": len(sources),
            "by_status": dict(sorted(by_status.items())),
            "by_provider": {name: dict(sorted(counts.items())) for name, counts in sorted(by_provider.items())},
            "scoped": scoped,
            "current": current,
            "coverage": round(current / scoped, 6) if scoped else None,
            "lagging": lagging,
            "max_lag_seconds": lagging[0]["lag_seconds"] if lagging else 0.0,
        },
        "queue": {
            "by_status": queue_counts,
            "oldest_pending_age_seconds": round(now - oldest, 3) if oldest is not None else None,
            "unknown": unknown_items,
        },
        "last_run": None
        if last_run is None
        else {
            "run_id": last_run["run_id"],
            "status": last_run["status"],
            "finished_at": _iso(last_run["finished_at"]),
            "age_seconds": round(now - last_run["finished_at"], 3),
            "summary": json.loads(last_run["summary_json"]),
        },
    }


# -- helpers ---------------------------------------------------------------


def _iso(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_iso(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


__all__ = [
    "COLLECTOR_STATE_VERSION",
    "KILL_SWITCH_ENV",
    "QUEUE_STATUSES",
    "SOURCE_STATUSES",
    "CollectRun",
    "CollectorConfig",
    "DrainResult",
    "collect_once",
    "collect_watch",
    "collector_status",
    "default_kill_switch_path",
    "drain_queue",
    "enqueue_sessions",
    "ensure_collector_schema",
    "kill_switch_engaged",
    "metadata_state",
    "set_kill_switch",
]
