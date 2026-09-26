"""Tests for the read-only Dolt bead-store adapter.

No live Dolt server is touched: every test drives the adapter either through a
synthetic CSV fixture or an injected chunk runner.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adapter_support as support  # noqa: E402

from agent_observatory.adapters import (  # noqa: E402
    ADAPTERS,
    SUPPORTED_PROVIDERS,
    BeadsAdapter,
    BeadsError,
    SourceSizeExceeded,
    diff_bead_snapshots,
    fetch_bead_fields,
    parse_bead_fields,
    read_beads,
)
from agent_observatory.adapters.beads import (  # noqa: E402
    DEFAULT_DATABASE,
    DoltCommand,
    _default_sql_runner,
    _events_sql,
    _fields_sql,
    assert_read_only,
    normalize_ref,
)
from agent_observatory.canonical import sha256_bytes  # noqa: E402
from agent_observatory.episodes import segment_store  # noqa: E402
from agent_observatory.store import ObservatoryStore  # noqa: E402


def _fixture_bytes(name: str) -> bytes:
    with open(support.fixture("beads", name), "rb") as handle:
        return handle.read()


def _runner_from(data: bytes, calls: list | None = None):
    def runner(sql: str, command):
        if calls is not None:
            calls.append((sql, command))
        yield data

    return runner


def _parse(data: bytes, ref: str = "main"):
    return BeadsAdapter().parse(
        data,
        context=support.CONTEXT,
        generation=1,
        source_path=f"dolt://{DEFAULT_DATABASE}@{ref}",
        source_sha256=sha256_bytes(data),
    )


class BeadsRegistrationTest(unittest.TestCase):
    def test_provider_is_registered(self):
        self.assertIn("beads", SUPPORTED_PROVIDERS)
        self.assertIsInstance(ADAPTERS["beads"], BeadsAdapter)

    def test_detect_matches_only_synthetic_dolt_ids(self):
        adapter = BeadsAdapter()
        self.assertTrue(adapter.detect("dolt://gl@main"))
        self.assertTrue(adapter.detect("beads://gl@remotes/origin/main"))
        self.assertFalse(adapter.detect("/home/me/.claude/projects/x/session.jsonl"))

    def test_file_discovery_is_unchanged(self):
        # The non-file provider must not participate in path detection.
        from agent_observatory.adapters import adapter_for_path

        self.assertIsNone(adapter_for_path("/tmp/not-a-transcript.jsonl"))


class BeadsSqlSafetyTest(unittest.TestCase):
    def test_ref_validation(self):
        self.assertEqual(normalize_ref("main"), "main")
        self.assertEqual(normalize_ref("remotes/origin/main"), "remotes/origin/main")
        self.assertEqual(normalize_ref("ammnfp9q2uv95qf6f9c38h1rgd483et8"), "ammnfp9q2uv95qf6f9c38h1rgd483et8")
        for bad in ("", " main", "main; DROP TABLE issues", "main'", "main\nSELECT 1", "tabs\there"):
            with self.subTest(ref=bad):
                with self.assertRaises(BeadsError):
                    normalize_ref(bad)

    def test_read_only_guard_accepts_use_and_select(self):
        sql = "USE gl; SELECT 1 FROM issues;"
        self.assertEqual(assert_read_only(sql), sql)

    def test_read_only_guard_rejects_writes(self):
        for bad in (
            "USE gl; DROP TABLE issues;",
            "UPDATE issues SET title = 'x';",
            "SELECT 1; DELETE FROM comments;",
            "INSERT INTO labels VALUES ('a','b');",
        ):
            with self.subTest(sql=bad):
                with self.assertRaises(BeadsError):
                    assert_read_only(bad)

    def test_read_only_guard_rejects_select_write_escapes(self):
        # A SELECT head is not enough: file writes, variable binding and
        # locking reads all hide inside an otherwise read-only statement.
        for bad in (
            "SELECT 1 INTO OUTFILE '/tmp/leak';",
            "SELECT 1 INTO DUMPFILE '/tmp/leak';",
            "SELECT 1 INTO @leak;",
            "SELECT * FROM issues FOR UPDATE;",
            "USE gl; select 1 into outfile '/tmp/leak';",
            "SELECT * FROM issues for update;",
        ):
            with self.subTest(sql=bad):
                with self.assertRaises(BeadsError):
                    assert_read_only(bad)

    def test_read_only_guard_rejects_comment_split_write_escapes(self):
        # A comment acts as whitespace, so it can satisfy the ``\s`` in the
        # escape patterns without the escape being written as two tokens.
        for bad in (
            "SELECT 1 INTO/**/ OUTFILE '/tmp/x';",
            "SELECT 1 INTO/*c*/@v;",
            "SELECT 1 FOR/*c*/UPDATE;",
            "SELECT 1 INTO /* c */ OUTFILE 'x';",
            "SELECT 1 INTO-- x\n OUTFILE '/tmp/x';",
            "SELECT 1 INTO # x\n OUTFILE '/tmp/x';",
        ):
            with self.subTest(sql=bad):
                with self.assertRaises(BeadsError):
                    assert_read_only(bad)

    def test_read_only_guard_rejects_executable_comments(self):
        # ``/*! ... */`` is executable MySQL, so it must not be stripped and
        # scanned as a comment.
        for bad in (
            "SELECT /*!32302 INTO OUTFILE '/tmp/x' */ 1;",
            "SELECT /*! INTO OUTFILE '/tmp/x' */ 1;",
        ):
            with self.subTest(sql=bad):
                with self.assertRaises(BeadsError):
                    assert_read_only(bad)

    def test_read_only_guard_rejects_variable_binding_without_whitespace(self):
        # MySQL accepts ``INTO@var``: the keyword and the symbol need no space.
        with self.assertRaises(BeadsError):
            assert_read_only("SELECT 1 INTO@leak;")

    def test_read_only_guard_allows_comment_markers_inside_string_literals(self):
        # A marker inside a quoted literal is data, not a comment: a ref such as
        # ``a--b`` must survive the guard.
        for sql in (
            "SELECT '--not a comment', '/* not either */', '#' FROM issues;",
            "SELECT \"--still data\", `--backtick` FROM issues;",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(assert_read_only(sql), sql)

    def test_events_sql_pins_ref_on_every_base_table(self):
        sql = _events_sql("remotes/origin/main", "gl")
        self.assertEqual(sql.count("AS OF 'remotes/origin/main'"), 6)
        self.assertIn("comments", sql)
        self.assertIn("GROUP_CONCAT", sql)
        # Ref validation happens before interpolation.
        with self.assertRaises(BeadsError):
            _events_sql("main'; DROP TABLE issues; --", "gl")

    def test_fields_sql_is_read_only(self):
        sql = _fields_sql("main", "gl")
        assert_read_only(sql)
        self.assertIn("AS OF 'main'", sql)


class BeadsParseTest(unittest.TestCase):
    def setUp(self):
        self.data = _fixture_bytes("events.csv")
        self.result = _parse(self.data)
        self.records = self.result.records

    def test_one_session_per_bead(self):
        sessions = sorted({record["session_id"] for record in self.records})
        self.assertEqual(sessions, ["gl-bead-a@main", "gl-bead-b@main"])

    def test_bead_body_is_in_time_order(self):
        bead_a = [record for record in self.records if record["bead_id"] == "gl-bead-a"]
        self.assertEqual([record["kind"] for record in bead_a], ["note"] * 5)
        self.assertIn("Fix the router stall", bead_a[0]["text"])
        self.assertIn("Why did the router stall?", bead_a[1]["text"])
        self.assertIn("Owner asked for a Friday fix.", bead_a[2]["text"])
        self.assertIn("First comment", bead_a[3]["text"])
        self.assertIn("Second comment", bead_a[4]["text"])
        self.assertEqual([record["timestamp"] for record in bead_a[0:3]], [bead_a[0]["timestamp"]] * 3)
        self.assertLess(bead_a[0]["timestamp"], bead_a[4]["timestamp"])

    def test_event_ids_are_unique_within_a_bead(self):
        for bead in ("gl-bead-a", "gl-bead-b"):
            ids = [record["event_id"] for record in self.records if record["bead_id"] == bead]
            self.assertEqual(len(ids), len(set(ids)))

    def test_event_id_keeps_conforming_source_id_and_event_kind(self):
        data = (
            b"bead_id,event_kind,ordinal,source_id,occurred_at,text,status,assignee,priority,issue_type,labels\n"
            b"gl-x,comment,3,11111111-1111-1111-1111-111111111111,2026-09-20 10:00:00,hello,open,,2,task,\n"
        )
        result = _parse(data)
        self.assertEqual(
            [record["event_id"] for record in result.records],
            ["00000-comment-11111111-1111-1111-1111-111111111111"],
        )

    def test_event_id_does_not_expose_unredacted_source_id(self):
        # N2: source_id is unconstrained bead-store text and event_id is an
        # identity field the body redactor never touches, so a non-conforming
        # id must not ride out verbatim. The synthetic token is built at runtime
        # so no full credential-shaped literal appears in the source.
        secret = "ghp_" + "A" * 36
        data = (
            b"bead_id,event_kind,ordinal,source_id,occurred_at,text,status,assignee,priority,issue_type,labels\n"
            + f"gl-x,comment,3,{secret},2026-09-20 10:00:00,hello,open,,2,task,\n".encode()
        )
        result = _parse(data)
        self.assertEqual(len(result.records), 1)
        event_id = result.records[0]["event_id"]
        self.assertNotIn(secret, event_id)
        self.assertNotIn("ghp_", event_id)
        self.assertRegex(event_id, r"^00000-comment-[A-Za-z0-9][A-Za-z0-9._-]*$")

    def test_event_id_does_not_expose_unredacted_event_kind(self):
        data = (
            b"bead_id,event_kind,ordinal,source_id,occurred_at,text,status,assignee,priority,issue_type,labels\n"
            b"gl-x,password=supersecretvalue,3,3,2026-09-20 10:00:00,hello,open,,2,task,\n"
        )
        result = _parse(data)
        self.assertEqual(len(result.records), 1)
        event_id = result.records[0]["event_id"]
        self.assertNotIn("supersecretvalue", event_id)
        self.assertRegex(event_id, r"^00000-redacted-[0-9a-f]{12}-[A-Za-z0-9][A-Za-z0-9._-]*$")

    def test_nonconforming_source_ids_do_not_collide(self):
        # Distinct malformed ids must still be distinct events so neither is
        # silently deduplicated onto the other.
        data = (
            b"bead_id,event_kind,ordinal,source_id,occurred_at,text,status,assignee,priority,issue_type,labels\n"
            b"gl-x,title,0,title,2026-09-20 10:00:00,Header,open,,2,task,\n"
            b"gl-x,comment,3,id with spaces,2026-09-20 11:00:00,First,open,,2,task,\n"
            b"gl-x,comment,3,id\twith\ttabs,2026-09-20 12:00:00,Second,open,,2,task,\n"
        )
        result = _parse(data)
        ids = [record["event_id"] for record in result.records]
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(set(ids)), 3)

    def test_labels_status_assignee_are_metadata_on_first_event(self):
        bead_a = next(record for record in self.records if record["bead_id"] == "gl-bead-a")
        text = bead_a["text"] or ""
        self.assertIn("[beads]", text)
        self.assertIn("status=open", text)
        self.assertIn("assignee=alice", text)
        self.assertIn("priority=2", text)
        self.assertIn("type=task", text)
        self.assertIn("labels=bug,infra", text)

    def test_secrets_are_redacted(self):
        joined = " ".join(record.get("text") or "" for record in self.records)
        self.assertNotIn("supersecretvalue", joined)
        self.assertNotIn("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", joined)
        self.assertIn("[REDACTED]", joined)

    def test_title_revision_is_recorded(self):
        titles = [revision.title for revision in self.result.title_revisions]
        self.assertIn("Fix the router stall", titles)

    def test_title_revision_is_bounded(self):
        # The title revision must use the bounding redactor, not the unbounded
        # one, so an enormous title cannot ride out in title_revisions.
        long_title = "T" * 5000
        data = (
            b"bead_id,event_kind,ordinal,source_id,occurred_at,text,status,assignee,priority,issue_type,labels\n"
            + f"gl-long,title,0,title,2026-09-20 10:00:00,{long_title},open,,2,task,\n".encode()
        )
        result = _parse(data)
        self.assertEqual(len(result.title_revisions), 1)
        title = result.title_revisions[0].title
        self.assertIn("[truncated:", title)
        self.assertLess(len(title), len(long_title))

    def test_missing_timestamp_is_flagged_not_defaulted(self):
        data = (
            b"bead_id,event_kind,ordinal,source_id,occurred_at,text,status,assignee,priority,issue_type,labels\n"
            b"gl-x,title,0,title,not-a-date,hello,open,,2,task,\n"
        )
        result = _parse(data)
        self.assertEqual(result.records, [])
        self.assertEqual(result.skipped.get("no_timestamp"), 1)

    def test_empty_source_is_flagged(self):
        result = _parse(b"")
        self.assertEqual(result.records, [])
        self.assertEqual(result.skipped.get("empty_source"), 1)

    def test_empty_non_title_fields_are_skipped(self):
        data = (
            b"bead_id,event_kind,ordinal,source_id,occurred_at,text,status,assignee,priority,issue_type,labels\n"
            b"gl-e,title,0,title,2026-09-20 10:00:00,Titled,open,,2,task,\n"
            b"gl-e,description,1,description,2026-09-20 10:00:00,,open,,2,task,\n"
            b"gl-e,notes,2,notes,2026-09-20 10:00:00,Real note,open,,2,task,\n"
        )
        result = _parse(data)
        self.assertEqual([record["event_id"] for record in result.records], [
            "00000-title-title",
            "00001-notes-notes",
        ])
        self.assertEqual(result.skipped.get("empty_field"), 1)


class BeadsReadTest(unittest.TestCase):
    def test_read_beads_uses_injected_runner(self):
        data = _fixture_bytes("events.csv")
        calls: list = []
        result = read_beads(
            "origin/main",
            context=support.CONTEXT,
            runner=_runner_from(data, calls),
        )
        self.assertEqual(len(calls), 1)
        sql, command = calls[0]
        self.assertIn("AS OF 'origin/main'", sql)
        self.assertEqual(command.database, DEFAULT_DATABASE)
        self.assertEqual({record["session_id"] for record in result.records}, {
            "gl-bead-a@origin/main",
            "gl-bead-b@origin/main",
        })
        # Records still satisfy the import contract.
        self.assertTrue(all(record["schema_version"] == "1.0" for record in result.records))

    def test_read_beads_bounds_source_bytes(self):
        data = _fixture_bytes("events.csv")
        with self.assertRaises(SourceSizeExceeded):
            read_beads("main", context=support.CONTEXT, max_bytes=32, runner=_runner_from(data))

    def test_read_beads_rejects_bad_ref_before_running(self):
        called = []

        def runner(sql, command):  # pragma: no cover - must not run
            called.append(sql)
            yield b""

        with self.assertRaises(BeadsError):
            read_beads("main'; DROP TABLE issues; --", context=support.CONTEXT, runner=runner)
        self.assertEqual(called, [])


class BeadsDefaultRunnerTest(unittest.TestCase):
    def test_default_runner_enforces_timeout_and_kills_the_child(self):
        tmp = support.make_temp_dir()
        self.addCleanup(tmp.cleanup)
        pidfile = os.path.join(tmp.name, "child.pid")
        script = os.path.join(tmp.name, "slow-dolt")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n")
            handle.write(f"echo $$ > {shlex.quote(pidfile)}\n")
            handle.write("exec sleep 30\n")
        os.chmod(script, 0o755)
        command = DoltCommand(city=tmp.name, executable=script, timeout_seconds=0.5)

        started = time.monotonic()
        with self.assertRaises(BeadsError):
            list(_default_sql_runner("SELECT 1", command))
        elapsed = time.monotonic() - started
        # A 30 s child must be killed at the deadline, not waited on.
        self.assertLess(elapsed, 10.0)
        with open(pidfile, "r", encoding="utf-8") as handle:
            pid = int(handle.read().strip())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


class BeadsDiffTest(unittest.TestCase):
    def setUp(self):
        self.local = parse_bead_fields(_fixture_bytes("fields-local.csv"))
        self.remote = parse_bead_fields(_fixture_bytes("fields-remote.csv"))
        self.diff = diff_bead_snapshots(self.local, self.remote)

    def test_diff_reports_changed_fields(self):
        entry = next(item for item in self.diff if item["bead_id"] == "gl-bead-a")
        self.assertEqual(entry["change"], "fields")
        self.assertEqual(
            entry["changed_fields"],
            ["assignee", "description", "notes", "priority", "status", "title"],
        )
        self.assertEqual(entry["local"]["status"], "open")
        self.assertEqual(entry["remote"]["status"], "closed")
        self.assertEqual(entry["local"]["assignee"], "alice")
        self.assertEqual(entry["remote"]["assignee"], "carol")

    def test_diff_reports_one_sided_beads(self):
        by_id = {item["bead_id"]: item for item in self.diff}
        self.assertEqual(by_id["gl-bead-c"]["change"], "only_local")
        self.assertIsNone(by_id["gl-bead-c"]["remote"])
        self.assertEqual(by_id["gl-bead-d"]["change"], "only_remote")
        self.assertIsNone(by_id["gl-bead-d"]["local"])

    def test_no_diff_for_identical_snapshots(self):
        self.assertEqual(diff_bead_snapshots(self.local, self.local), [])

    def test_fetch_bead_fields_uses_injected_runner(self):
        data = _fixture_bytes("fields-local.csv")
        fields = fetch_bead_fields("main", runner=_runner_from(data))
        self.assertEqual(fields["gl-bead-a"]["status"], "open")

    def test_secret_bearing_field_never_reaches_the_diff(self):
        # The field snapshot is built by parse_bead_fields, whose values must be
        # redacted before they enter the map (HIGH finding).
        data = (
            b"bead_id,title,description,notes,status,assignee,priority,issue_type\n"
            b"gl-secret,Title,password=supersecretvalue,Note,open,alice,2,task\n"
        )
        local = parse_bead_fields(data)
        remote = parse_bead_fields(
            b"bead_id,title,description,notes,status,assignee,priority,issue_type\n"
            b"gl-secret,Title,password=othersecretvalue,Note,open,alice,2,task\n"
        )
        self.assertNotIn("supersecretvalue", json.dumps(local))
        self.assertIn("[REDACTED]", local["gl-secret"]["description"])
        for snapshot in (
            diff_bead_snapshots(local, {}),
            diff_bead_snapshots({}, local),
            diff_bead_snapshots(local, remote),
        ):
            rendered = json.dumps(snapshot)
            self.assertNotIn("supersecretvalue", rendered)
            self.assertNotIn("othersecretvalue", rendered)


class BeadsPipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = support.make_temp_dir()
        self.addCleanup(self.tmp.cleanup)

    def test_import_and_segment_yields_one_episode_per_bead(self):
        result = read_beads("main", context=support.CONTEXT, runner=_runner_from(_fixture_bytes("events.csv")))
        import_result = support.import_records(
            os.path.join(self.tmp.name, "proj.db"), result.records, self.tmp.name
        )
        self.assertEqual(import_result.inserted, len(result.records))
        with ObservatoryStore(os.path.join(self.tmp.name, "proj.db")) as store:
            episodes = segment_store(store)
        self.assertEqual(len(episodes), 2)
        self.assertEqual({episode.provider for episode in episodes}, {"beads"})

    def test_refs_do_not_collide_in_one_projection(self):
        db = os.path.join(self.tmp.name, "proj.db")
        main = read_beads("main", context=support.CONTEXT, runner=_runner_from(_fixture_bytes("events.csv")))
        remote = read_beads(
            "remotes/origin/main",
            context=support.CONTEXT,
            runner=_runner_from(_fixture_bytes("events.csv")),
        )
        first = support.import_records(db, main.records, self.tmp.name, name="main.jsonl")
        second = support.import_records(db, remote.records, self.tmp.name, name="remote.jsonl")
        self.assertEqual(first.inserted, len(main.records))
        self.assertEqual(second.inserted, len(remote.records))
        with ObservatoryStore(db) as store:
            sessions = {key[3] for key in store.session_keys()}
        self.assertIn("gl-bead-a@main", sessions)
        self.assertIn("gl-bead-a@remotes/origin/main", sessions)


if __name__ == "__main__":
    unittest.main()
