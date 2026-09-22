"""CLI wiring tests for the ``inventory`` and ``export`` subcommands."""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adapter_support as support  # noqa: E402

from agent_observatory.cli import main  # noqa: E402
from agent_observatory.store import ObservatoryStore  # noqa: E402


class AdapterCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = support.make_temp_dir()
        self.addCleanup(self.tmp.cleanup)

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_inventory_writes_a_manifest(self):
        support.claude_source(self.tmp.name)
        support.codex_source(self.tmp.name)
        manifest_path = os.path.join(self.tmp.name, "manifest.json")
        code, _stdout, stderr = self._run(
            [
                "inventory",
                "--root",
                self.tmp.name,
                "--city",
                "city-a",
                "--host",
                "host-a",
                "--out",
                manifest_path,
            ]
        )
        self.assertEqual(code, 0)
        summary = json.loads(stderr)
        self.assertGreaterEqual(summary["sources"], 2)
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertIn("sources", manifest)
        self.assertEqual(manifest["city_id"], "city-a")

    def test_export_writes_jsonl_and_imports(self):
        out_path = os.path.join(self.tmp.name, "exported.jsonl")
        db_path = os.path.join(self.tmp.name, "projection.db")
        code, _stdout, stderr = self._run(
            [
                "export",
                "--provider",
                "claude",
                "--input",
                support.fixture("claude"),
                "--city",
                "city-a",
                "--host",
                "host-a",
                "--out",
                out_path,
                "--db",
                db_path,
            ]
        )
        self.assertEqual(code, 0)
        summary = json.loads(stderr)
        self.assertGreater(summary["records"], 0)
        self.assertEqual(summary["inserted"], summary["records"])
        with open(out_path, encoding="utf-8") as handle:
            lines = [line for line in handle if line.strip()]
        self.assertEqual(len(lines), summary["records"])
        with ObservatoryStore(db_path) as store:
            self.assertEqual(store.event_count(), summary["records"])
            self.assertEqual(store.session_count(), 1)

    def test_export_to_stdout(self):
        code, stdout, _stderr = self._run(
            [
                "export",
                "--provider",
                "codex",
                "--input",
                support.fixture("codex"),
                "--city",
                "c",
                "--host",
                "h",
            ]
        )
        self.assertEqual(code, 0)
        records = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        self.assertTrue(records)
        self.assertTrue(all(record["provider"] == "codex" for record in records))

    def test_export_without_input_is_a_clean_error(self):
        code, _stdout, stderr = self._run(
            ["export", "--provider", "claude", "--city", "c", "--host", "h"]
        )
        self.assertEqual(code, 1)
        self.assertIn("error:", stderr)

    def test_export_rejects_invalid_generation_with_a_clean_error(self):
        for bad in ("0", "-3", "abc", "1.5"):
            code, _stdout, stderr = self._run(
                [
                    "export",
                    "--provider",
                    "claude",
                    "--input",
                    support.fixture("claude"),
                    "--city",
                    "c",
                    "--host",
                    "h",
                    "--generation",
                    bad,
                ]
            )
            self.assertEqual(code, 1, f"generation={bad!r}")
            self.assertIn("error:", stderr, f"generation={bad!r}")
            self.assertIn("--generation", stderr, f"generation={bad!r}")

    def test_export_rejects_multi_sign_generation_with_a_clean_error(self):
        # F1: ``--5`` and ``+-5`` must not slip through lstrip("+-").isdigit()
        # and raise a bare ValueError. Use the ``=`` form so argparse passes the
        # sign-led value through instead of treating it as an option.
        for bad in ("--5", "+-5", "5+", "++3"):
            code, _stdout, stderr = self._run(
                [
                    "export",
                    "--provider",
                    "claude",
                    "--input",
                    support.fixture("claude"),
                    "--city",
                    "c",
                    "--host",
                    "h",
                    f"--generation={bad}",
                ]
            )
            self.assertEqual(code, 1, f"generation={bad!r}")
            self.assertIn("error:", stderr, f"generation={bad!r}")
            self.assertIn("--generation", stderr, f"generation={bad!r}")

    def test_export_accepts_positive_generation(self):
        code, stdout, _stderr = self._run(
            [
                "export",
                "--provider",
                "claude",
                "--input",
                support.fixture("claude"),
                "--city",
                "c",
                "--host",
                "h",
                "--generation",
                "4",
            ]
        )
        self.assertEqual(code, 0)
        self.assertTrue(stdout.strip())

    def test_inventory_missing_root_is_a_clean_error(self):
        code, _stdout, stderr = self._run(
            ["inventory", "--root", "/definitely/not/here", "--city", "c", "--host", "h"]
        )
        self.assertEqual(code, 1)
        self.assertIn("error:", stderr)


if __name__ == "__main__":
    unittest.main()
