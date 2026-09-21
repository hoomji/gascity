"""End-to-end CLI tests using synthetic fixtures and a temporary SQLite file."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

try:
    from . import support
except ImportError:  # pragma: no cover
    import support

PACKAGE_ROOT = support.PACKAGE_ROOT


def run_cli(args, cwd=PACKAGE_ROOT):
    env = dict(os.environ)
    env["PYTHONPATH"] = PACKAGE_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "agent_observatory", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "projection.db")
        self.fixture = support.write_jsonl(
            os.path.join(self.tmp.name, "synthetic.jsonl"),
            [
                support.make_record(
                    event_id="e1", timestamp="2026-09-21T10:00:00Z",
                    kind="tool_call", command="go test ./...", tool_call_id="tc1",
                ),
                support.make_record(
                    event_id="e2", timestamp="2026-09-21T10:00:01Z",
                    kind="tool_result", tool_call_id="tc1", exit_code=0,
                ),
                support.make_record(
                    event_id="e3", timestamp="2026-09-21T10:00:02Z",
                    kind="tool_call", command="rg needle", tool_name="rg",
                ),
            ],
        )

    def test_import_report_and_replay_preserves_counts(self):
        first = run_cli(["import-jsonl", "--db", self.db, self.fixture])
        self.assertEqual(first.returncode, 0, first.stderr)
        first_summary = json.loads(first.stdout)["imports"][0]
        self.assertEqual(first_summary["inserted"], 3)

        replay = run_cli(["import-jsonl", "--db", self.db, self.fixture])
        self.assertEqual(replay.returncode, 0, replay.stderr)
        replay_summary = json.loads(replay.stdout)["imports"][0]
        self.assertTrue(replay_summary["skipped_identical_file"])
        self.assertEqual(replay_summary["events"], 3)

        report = run_cli(["report", "--db", self.db])
        self.assertEqual(report.returncode, 0, report.stderr)
        payload = json.loads(report.stdout)
        self.assertEqual(payload["coverage"]["events"], 3)
        self.assertEqual(payload["coverage"]["sessions"], 1)
        self.assertEqual(payload["test"]["invocations"], 1)
        self.assertEqual(payload["test"]["results"], {"passed": 1, "failed": 0, "unknown": 0})

    def test_build_request_and_import_response_round_trip(self):
        self.assertEqual(run_cli(["import-jsonl", "--db", self.db, self.fixture]).returncode, 0)
        state_path = os.path.join(self.tmp.name, "state.json")
        with open(state_path, "w", encoding="utf-8") as handle:
            json.dump({"summary": "ran the test suite"}, handle)

        built = run_cli(
            [
                "build-request",
                "--db", self.db,
                "--state", state_path,
                "--subject-kind", "session",
                "--session", "city-a|host-a|codex|session-1",
            ]
        )
        self.assertEqual(built.returncode, 0, built.stderr)
        request_body = json.loads(built.stdout)
        self.assertEqual(request_body["model"], "jev-1.13.0")
        summary = json.loads(built.stderr.strip().splitlines()[-1])
        request_hash = summary["request_hash"]

        response = support.valid_response(request_body)
        response_path = os.path.join(self.tmp.name, "response.json")
        with open(response_path, "w", encoding="utf-8") as handle:
            json.dump(response, handle)

        imported = run_cli(
            ["import-response", "--db", self.db, "--response", response_path, "--request-hash", request_hash]
        )
        self.assertEqual(imported.returncode, 0, imported.stderr)
        imported_payload = json.loads(imported.stdout)
        self.assertFalse(imported_payload["deduplicated"])

        replayed = run_cli(
            ["import-response", "--db", self.db, "--response", response_path, "--request-hash", request_hash]
        )
        self.assertEqual(replayed.returncode, 0, replayed.stderr)
        self.assertTrue(json.loads(replayed.stdout)["deduplicated"])

    def test_malformed_file_fails_with_line_context(self):
        bad = os.path.join(self.tmp.name, "bad.jsonl")
        with open(bad, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(support.make_record(event_id="e1")) + "\n")
            handle.write("{not json}\n")
        result = run_cli(["import-jsonl", "--db", self.db, bad])
        self.assertEqual(result.returncode, 1)
        self.assertIn("bad.jsonl:2", result.stderr)


if __name__ == "__main__":
    unittest.main()
