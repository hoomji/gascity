"""M4 collector: checkpoints, replay, rotation, debounce, budgets, kill switch, queue."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest

import support  # noqa: F401  (puts the package root on sys.path)

from agent_observatory.cli import main
from agent_observatory.collector import (
    KILL_SWITCH_ENV,
    CollectorConfig,
    _collector_lock,
    collect_once,
    collect_watch,
    collector_status,
    drain_queue,
    enqueue_sessions,
    metadata_state,
    set_kill_switch,
)
from agent_observatory.errors import ObservatoryError
from agent_observatory.inventory import SourceRoot
from agent_observatory.store import ObservatoryStore
from agent_observatory.taxonomy import load_taxonomy
from agent_observatory.transport import Budget, ClassifyResult, TransportConfig, TransportError

HERE = os.path.dirname(os.path.abspath(__file__))
CLAUDE_FIXTURE = os.path.join(HERE, "fixtures", "adapters", "claude", "sample.jsonl")
CODEX_FIXTURE = os.path.join(HERE, "fixtures", "adapters", "codex", "sample.jsonl")

EXTRA_LINE = (
    '{"type":"assistant","uuid":"u-a4","sessionId":"claude-sess-1","session_id":"claude-parent-0",'
    '"timestamp":"2026-09-21T10:00:05.000Z","message":{"id":"msg-3","role":"assistant",'
    '"model":"claude-test-1","content":[{"type":"text","text":"done"}],'
    '"usage":{"input_tokens":1,"output_tokens":1}}}\n'
)
NO_ENV: dict[str, str] = {}


def _later(seconds: float = 3600.0):
    return lambda: time.time() + seconds


class CollectorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="collector-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "home")
        self.claude_dir = os.path.join(self.root, ".claude", "projects", "proj")
        os.makedirs(self.claude_dir)
        self.db = os.path.join(self.tmp, "obs.db")
        self.kill = os.path.join(self.tmp, "disabled")

    def claude_source(self, name: str = "sess.jsonl") -> str:
        path = os.path.join(self.claude_dir, name)
        shutil.copyfile(CLAUDE_FIXTURE, path)
        return path

    def config(self, **overrides) -> CollectorConfig:
        values = dict(
            roots=(SourceRoot(path=self.root),),
            city_id="city-t",
            host_id="host-t",
            debounce_seconds=30.0,
            kill_switch_path=self.kill,
        )
        values.update(overrides)
        return CollectorConfig(**values)

    def collect(self, store, clock=None, **overrides):
        return collect_once(store, self.config(**overrides), clock=clock or _later(), environ=NO_ENV)

    def statuses(self, store) -> dict[str, tuple[str, str]]:
        rows = store.conn.execute("SELECT path, status, reason FROM collector_sources").fetchall()
        return {os.path.basename(row["path"]): (row["status"], row["reason"]) for row in rows}


class CollectTests(CollectorTestCase):
    def test_every_scoped_source_has_status_and_reason(self):
        self.claude_source()
        opencode = os.path.join(self.root, ".opencode")
        os.makedirs(opencode)
        with open(os.path.join(opencode, "opencode.db"), "wb") as handle:
            handle.write(b"x")
        with open(os.path.join(self.claude_dir, "sess.lock"), "w") as handle:
            handle.write("")
        locked = os.path.join(self.root, "locked")
        os.makedirs(locked)
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o755)
        with ObservatoryStore(self.db) as store:
            run = self.collect(store)
            rows = store.conn.execute("SELECT status, reason FROM collector_sources").fetchall()
        self.assertEqual(run.status, "ok")
        self.assertEqual(run.ignored_files, {".lock": 1})
        statuses = sorted(row["status"] for row in rows)
        self.assertIn("imported", statuses)
        self.assertIn("unsupported", statuses)
        if os.geteuid() != 0:
            self.assertIn("unreadable", statuses)
        for row in rows:
            self.assertTrue(row["reason"])

    def test_replay_is_idempotent_and_unchanged(self):
        self.claude_source()
        with ObservatoryStore(self.db) as store:
            first = self.collect(store)
            events = store.event_count()
            second = self.collect(store)
            self.assertEqual(store.event_count(), events)
        self.assertGreater(first.events_inserted, 0)
        self.assertEqual(second.by_status, {"unchanged": 1})
        self.assertEqual(second.events_inserted, 0)

    def test_append_keeps_generation_and_adds_only_new_events(self):
        path = self.claude_source()
        with ObservatoryStore(self.db) as store:
            self.collect(store)
            before = store.event_count()
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(EXTRA_LINE)
            os.utime(path, (time.time() + 5, time.time() + 5))
            run = self.collect(store)
            row = store.conn.execute("SELECT generation, change FROM collector_sources").fetchone()
            self.assertEqual(store.event_count(), before + 1)
        self.assertEqual(run.by_change, {"appended": 1})
        self.assertEqual(run.events_inserted, 1)
        self.assertEqual((row["generation"], row["change"]), (1, "appended"))

    def test_rewrite_increments_generation(self):
        path = self.claude_source()
        with ObservatoryStore(self.db) as store:
            self.collect(store)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(EXTRA_LINE)
            os.utime(path, (time.time() + 5, time.time() + 5))
            run = self.collect(store)
            row = store.conn.execute("SELECT generation, change FROM collector_sources").fetchone()
        self.assertEqual(run.by_change, {"rewritten": 1})
        self.assertEqual((row["generation"], row["change"]), (2, "rewritten"))

    def test_lost_checkpoint_replays_as_duplicates(self):
        """A crash after import but before the checkpoint write loses nothing."""
        self.claude_source()
        with ObservatoryStore(self.db) as store:
            self.collect(store)
            events = store.event_count()
            store.conn.execute("DELETE FROM collector_sources")
            store.conn.execute("DELETE FROM imported_files")
            run = self.collect(store)
            self.assertEqual(store.event_count(), events)
        self.assertEqual(run.events_inserted, 0)
        self.assertEqual(run.events_duplicate, events)

    def test_failed_import_leaves_no_checkpoint_and_retries(self):
        self.claude_source()
        with ObservatoryStore(self.db) as store:
            original = store.import_jsonl

            def crash(path):
                raise OSError("disk full")

            store.import_jsonl = crash
            run = self.collect(store)
            self.assertEqual(run.by_status, {"error": 1})
            self.assertEqual(store.event_count(), 0)
            store.import_jsonl = original
            run = self.collect(store)
        self.assertEqual(run.by_status, {"imported": 1})
        self.assertGreater(run.events_inserted, 0)

    def test_rotation_keeps_counts_and_marks_old_path_missing(self):
        path = self.claude_source()
        with ObservatoryStore(self.db) as store:
            self.collect(store)
            events = store.event_count()
            os.rename(path, os.path.join(self.claude_dir, "sess-rotated.jsonl"))
            run = self.collect(store)
            statuses = self.statuses(store)
            self.assertEqual(store.event_count(), events)
        self.assertEqual(statuses["sess.jsonl"][0], "missing")
        self.assertEqual(statuses["sess-rotated.jsonl"][0], "imported")
        self.assertEqual(run.events_inserted, 0)

    def test_debounce_defers_recently_modified_files(self):
        self.claude_source()
        with ObservatoryStore(self.db) as store:
            run = self.collect(store, clock=time.time)
            self.assertEqual(run.by_status, {"debounced": 1})
            self.assertEqual(store.event_count(), 0)
            run = self.collect(store)
        self.assertEqual(run.by_status, {"imported": 1})

    def test_never_quiet_file_is_read_after_max_debounce(self):
        path = self.claude_source()
        start = time.time()
        with ObservatoryStore(self.db) as store:
            os.utime(path, (start, start))
            run = self.collect(store, clock=lambda: start + 1, max_debounce_seconds=60)
            self.assertEqual(run.by_status, {"debounced": 1})
            os.utime(path, (start + 100, start + 100))
            run = self.collect(store, clock=lambda: start + 101, max_debounce_seconds=60)
        self.assertEqual(run.by_status, {"imported": 1})

    def test_source_cap_defers_but_first_source_always_progresses(self):
        self.claude_source("a.jsonl")
        self.claude_source("b.jsonl")
        with ObservatoryStore(self.db) as store:
            run = self.collect(store, max_sources_per_run=1)
            self.assertEqual(run.by_status, {"imported": 1, "deferred": 1})
            run = self.collect(store, max_sources_per_run=1)
            self.assertEqual(run.by_status, {"imported": 1, "unchanged": 1})
            run = self.collect(store, max_bytes_per_run=1)
        self.assertEqual(run.by_status, {"unchanged": 2})

    def test_byte_cap_and_storage_cap(self):
        self.claude_source("a.jsonl")
        self.claude_source("b.jsonl")
        with ObservatoryStore(self.db) as store:
            run = self.collect(store, max_bytes_per_run=10)
            self.assertEqual(run.by_status, {"imported": 1, "deferred": 1})
            run = self.collect(store, max_db_bytes=1)
            reasons = [reason for status, reason in self.statuses(store).values() if status == "deferred"]
        self.assertEqual(run.by_status, {"unchanged": 1, "deferred": 1})
        self.assertIn("storage cap", reasons[0])

    def test_per_source_cap_defers_even_the_first_source(self):
        self.claude_source()
        with ObservatoryStore(self.db) as store:
            run = self.collect(store, max_source_bytes=100)
            reason = self.statuses(store)["sess.jsonl"][1]
            self.assertEqual(store.event_count(), 0)
        self.assertEqual(run.by_status, {"deferred": 1})
        self.assertIn("per-source cap", reason)

    def test_kill_switch_file_and_env_stop_collection(self):
        self.claude_source()
        with ObservatoryStore(self.db) as store:
            set_kill_switch(self.kill, True)
            run = self.collect(store)
            self.assertEqual(run.status, "disabled")
            self.assertEqual(store.event_count(), 0)
            set_kill_switch(self.kill, False)
            run = collect_once(store, self.config(), clock=_later(), environ={KILL_SWITCH_ENV: "1"})
            self.assertEqual(run.status, "disabled")
            run = self.collect(store)
        self.assertEqual(run.status, "ok")

    def test_watch_stops_when_switch_engages(self):
        self.claude_source()
        sleeps = []
        with ObservatoryStore(self.db) as store:
            runs = collect_watch(
                store,
                self.config(),
                interval_seconds=1,
                clock=_later(),
                sleeper=lambda seconds: (sleeps.append(seconds), set_kill_switch(self.kill, True)),
                environ=NO_ENV,
            )
        self.assertEqual([run.status for run in runs], ["ok", "disabled"])
        self.assertEqual(sleeps, [1])

    def test_concurrent_collector_is_locked_out(self):
        self.claude_source()
        with ObservatoryStore(self.db) as store:
            with _collector_lock(self.db) as acquired:
                self.assertTrue(acquired)
                run = self.collect(store)
        self.assertEqual(run.status, "locked")

    def test_config_validation(self):
        with self.assertRaises(ObservatoryError):
            self.config(roots=())
        with self.assertRaises(ObservatoryError):
            self.config(max_sources_per_run=0)
        with self.assertRaises(ObservatoryError):
            self.config(debounce_seconds=-1)
        with self.assertRaises(ObservatoryError):
            self.config(debounce_seconds=60, max_debounce_seconds=10)


def _fake_classify(outcomes):
    calls = []

    def fake(store, request, *, config):
        outcome, failure = outcomes[min(len(calls), len(outcomes) - 1)]
        calls.append(request)
        if outcome == "raise":
            raise TransportError("corrupt circuit row")
        config.budget.record_request()
        return ClassifyResult(
            outcome=outcome,
            request_hash=request.request_hash,
            snapshot_hash=request.snapshot_hash,
            subject_kind=request.subject_kind,
            taxonomy_version=request.taxonomy_version,
            question_hash=request.question_hash,
            model=request.model,
            endpoint="test",
            classification_id=7 if outcome == "classified" else None,
            failure_class=failure,
            error=None if failure is None else f"{failure} detail",
        )

    return fake, calls


class QueueTests(CollectorTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.claude_source()
        self.store = ObservatoryStore(self.db)
        self.addCleanup(self.store.close)
        self.run = self.collect(self.store)
        self.taxonomy = load_taxonomy(None)

    def drain(self, outcomes, now=None, **kwargs):
        fake, calls = _fake_classify(outcomes)
        config = kwargs.pop("transport_config", TransportConfig(budget=Budget(max_requests=10)))
        result = drain_queue(
            self.store,
            self.taxonomy,
            transport_config=config,
            max_items=kwargs.pop("max_items", 10),
            classify_fn=fake,
            clock=now or _later(),
            environ=NO_ENV,
            **kwargs,
        )
        return result, calls

    def queue(self):
        return [dict(row) for row in self.store.conn.execute("SELECT * FROM collector_queue ORDER BY queue_id")]

    def test_collect_enqueues_changed_sessions_once(self):
        self.assertEqual(self.run.sessions_enqueued, 1)
        self.assertEqual(self.collect(self.store).sessions_enqueued, 0)
        self.assertEqual(len(self.queue()), 1)

    def test_classified_item_is_done(self):
        result, calls = self.drain([("classified", None)])
        self.assertEqual((result.classified, len(calls)), (1, 1))
        self.assertEqual(self.queue()[0]["status"], "done")
        self.assertEqual(self.queue()[0]["classification_id"], 7)

    def test_repeated_failures_end_in_unknown_queue(self):
        result, _ = self.drain([("pending", "http_500")], max_attempts=2, retry_backoff_seconds=10)
        self.assertEqual(result.retry_scheduled, 1)
        item = self.queue()[0]
        self.assertEqual((item["status"], item["attempts"]), ("pending", 1))
        result, calls = self.drain([("pending", "http_500")], max_attempts=2)
        self.assertEqual(calls, [])  # not due yet
        result, _ = self.drain([("pending", "http_500")], now=_later(7200), max_attempts=2)
        self.assertEqual(result.unknown, 1)
        item = self.queue()[0]
        self.assertEqual((item["status"], item["last_failure_class"]), ("unknown", "http_500"))
        status = collector_status(self.store, now=time.time(), environ=NO_ENV)
        self.assertEqual(status["queue"]["by_status"]["unknown"], 1)
        self.assertEqual(status["queue"]["unknown"][0]["last_failure_class"], "http_500")

    def test_budget_and_circuit_stop_without_charging_attempts(self):
        for failure in ("budget_exhausted", "circuit_open", "credential_error"):
            result, _ = self.drain([("pending", failure)])
            self.assertEqual(result.status, "stopped")
            item = self.queue()[0]
            self.assertEqual((item["status"], item["attempts"]), ("pending", 0))

    def test_budget_ceiling_checked_before_sending(self):
        config = TransportConfig(budget=Budget(max_requests=0))
        result, calls = self.drain([("classified", None)], transport_config=config)
        self.assertEqual((result.status, calls), ("stopped", []))
        self.assertIn("request cap", result.reason)

    def test_transport_error_stops_drain(self):
        result, _ = self.drain([("raise", None)])
        self.assertEqual(result.status, "stopped")
        self.assertEqual(self.queue()[0]["last_failure_class"], "transport_error")

    def test_changed_session_supersedes_stale_item(self):
        path = os.path.join(self.claude_dir, "sess.jsonl")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(EXTRA_LINE)
        os.utime(path, (time.time() + 5, time.time() + 5))
        run = self.collect(self.store)
        self.assertEqual((run.sessions_enqueued, run.queue_superseded), (1, 1))
        result, calls = self.drain([("classified", None)])
        self.assertEqual((result.classified, len(calls)), (1, 1))
        self.assertEqual([item["status"] for item in self.queue()], ["superseded", "done"])

    def test_drain_respects_kill_switch(self):
        set_kill_switch(self.kill, True)
        result, calls = self.drain([("classified", None)], kill_switch_path=self.kill)
        self.assertEqual((result.status, calls), ("disabled", []))

    def test_metadata_state_carries_no_transcript_text(self):
        key = self.store.session_keys()[0]
        state = metadata_state(self.store, key)
        encoded = json.dumps(state)
        for secret in ("Please fix the bug", "supersecretvalue", "echo hi", "First title", "Second title"):
            self.assertNotIn(secret, encoded)
        self.assertEqual(state["state_kind"], "session_metadata")
        self.assertGreater(state["events"], 0)
        _, calls = self.drain([("classified", None)])
        self.assertNotIn("Please fix the bug", json.dumps(calls[0].body))

    def test_enqueue_ignores_unknown_session(self):
        self.assertEqual(enqueue_sessions(self.store, [("c", "h", "p", "nope")]), (0, 0))


class StatusAndCliTests(CollectorTestCase):
    def test_status_reports_coverage_and_lag(self):
        self.claude_source("a.jsonl")
        self.claude_source("b.jsonl")
        with ObservatoryStore(self.db) as store:
            self.collect(store, max_sources_per_run=1)
            status = collector_status(store, now=time.time() + 100, kill_switch_path=self.kill, environ=NO_ENV)
        self.assertEqual(status["sources"]["scoped"], 2)
        self.assertEqual(status["sources"]["current"], 1)
        self.assertEqual(status["sources"]["coverage"], 0.5)
        self.assertEqual(len(status["sources"]["lagging"]), 1)
        self.assertGreater(status["sources"]["max_lag_seconds"], 0)
        self.assertIsNone(status["disabled"])
        self.assertEqual(status["last_run"]["status"], "ok")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_cli_collect_status_and_switch(self):
        self.claude_source()
        common = ["--db", self.db, "--kill-switch", self.kill]
        base = ["collect", *common, "--root", self.root, "--city", "c", "--host", "h", "--debounce-seconds", "0"]
        code, out, _ = self.run_cli(*base)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["by_status"], {"imported": 1})
        code, out, _ = self.run_cli("collector-switch", *common, "off")
        self.assertEqual(json.loads(out)["collector"], "disabled")
        code, out, _ = self.run_cli(*base)
        self.assertEqual(json.loads(out)["status"], "disabled")
        code, out, _ = self.run_cli("collect-status", *common)
        self.assertEqual(code, 0)
        status = json.loads(out)
        self.assertIn("kill switch", status["disabled"])
        self.assertEqual(status["queue"]["by_status"]["pending"], 1)
        self.run_cli("collector-switch", *common, "on")
        code, out, _ = self.run_cli(*base, "--watch", "--interval", "0.01", "--iterations", "2")
        self.assertEqual(code, 0)
        self.assertEqual(out.count('"status": "ok"'), 2)

    def test_cli_queue_drain_requires_request_ceiling(self):
        code, _, err = self.run_cli("queue-drain", "--db", self.db)
        self.assertEqual(code, 1)
        self.assertIn("--max-requests", err)

    def test_cli_queue_drain_without_credential_stays_pending(self):
        self.claude_source()
        common = ["--db", self.db, "--kill-switch", self.kill]
        self.run_cli("collect", *common, "--root", self.root, "--city", "c", "--host", "h", "--debounce-seconds", "0")
        saved = {name: os.environ.pop(name) for name in ("TYPESAFE_API_KEY", "JEV_API_KEY", "JEV_KEY_FILE") if name in os.environ}
        self.addCleanup(os.environ.update, saved)
        code, out, _ = self.run_cli("queue-drain", *common, "--max-requests", "1")
        self.assertEqual(code, 1)
        result = json.loads(out)
        self.assertEqual(result["items"][0]["failure_class"], "credential_error")
        with ObservatoryStore(self.db) as store:
            row = store.conn.execute("SELECT status, attempts FROM collector_queue").fetchone()
        self.assertEqual((row["status"], row["attempts"]), ("pending", 0))


if __name__ == "__main__":
    unittest.main()
