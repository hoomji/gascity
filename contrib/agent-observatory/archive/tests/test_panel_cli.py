"""End-to-end CLI tests using synthetic fixtures and a temporary SQLite file."""

from __future__ import annotations

import json
import os
import shutil
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
    env["PYTHONPATH"] = os.path.join(PACKAGE_ROOT, "archive") + os.pathsep + PACKAGE_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "panel", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


SILVER_CANDIDATES = os.path.join(PACKAGE_ROOT, "archive", "tests", "fixtures", "silver", "candidates.csv")
SILVER_ANSWERS = os.path.join(PACKAGE_ROOT, "archive", "tests", "fixtures", "silver", "recorded_judge_answers.json")

class SilverCliTrustTests(unittest.TestCase):
    """silver-build/evaluate fail closed on a below-floor judge kappa."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "silver.db")
        records = []
        for index, (session_id, provider) in enumerate(
            [
                ("s-1", "codex"),
                ("s-2", "codex"),
                ("s-3", "codex"),
                ("s-4", "claude"),
                ("s-5", "claude"),
                ("s-6", "codex"),
            ],
            start=1,
        ):
            records.append(
                support.make_record(
                    event_id=f"e-{index}",
                    session_id=session_id,
                    provider=provider,
                    kind="message",
                    text=f"Please implement change {session_id}",
                )
            )
        fixture = support.write_jsonl(os.path.join(self.tmp.name, "silver.jsonl"), records)
        imported = run_cli(["import-jsonl", "--db", self.db, fixture])
        self.assertEqual(imported.returncode, 0, imported.stderr)
        # Replay recorded judge answers so the build never touches the network;
        # copy the fixture because the checkpoint client can rewrite its file.
        self.checkpoint = os.path.join(self.tmp.name, "checkpoint.json")
        shutil.copyfile(SILVER_ANSWERS, self.checkpoint)

    def _build(self, *extra):
        gold = os.path.join(self.tmp.name, "gold.json")
        report = os.path.join(self.tmp.name, "report.json")
        result = run_cli(
            [
                "silver-build",
                "--candidates", SILVER_CANDIDATES,
                "--db", self.db,
                "--sample-size", "2",
                "--seed", "silver-v1",
                "--checkpoint", self.checkpoint,
                "--out-gold", gold,
                "--out-report", report,
                *extra,
            ]
        )
        return result, gold, report

    def test_untrusted_build_refuses_out_gold_and_exits_nonzero(self):
        result, gold, report = self._build()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("not trustworthy", result.stderr)
        self.assertIn("allow-untrusted", result.stderr)
        self.assertFalse(os.path.exists(gold))
        # The report is still written so the refusal is durable evidence.
        self.assertTrue(os.path.exists(report))
        with open(report, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertFalse(payload["agreement"]["trustworthy"])
        self.assertLess(payload["agreement"]["cohen_kappa"], payload["agreement"]["kappa_trust_floor"])

    def test_allow_untrusted_writes_the_marked_gold_set(self):
        result, gold, _ = self._build("--allow-untrusted")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.exists(gold))
        with open(gold, encoding="utf-8") as handle:
            document = json.load(handle)
        self.assertTrue(document["silver"])
        self.assertFalse(document["silver_trustworthy"])

    def test_evaluate_refuses_an_untrusted_gold_set_unless_opted_in(self):
        built, gold, _ = self._build("--allow-untrusted")
        self.assertEqual(built.returncode, 0, built.stderr)
        predictions = os.path.join(self.tmp.name, "predictions.json")
        with open(predictions, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "predictor": "jev",
                    "model": "jev-1.13.0",
                    "predictions": [
                        {"episode_id": "ep-plan-4", "primary_intent": "planning_spec"}
                    ],
                },
                handle,
            )
        refused = run_cli(
            [
                "silver-evaluate",
                "--gold", gold,
                "--predictions", predictions,
                "--out", os.path.join(self.tmp.name, "evaluation.json"),
            ]
        )
        self.assertEqual(refused.returncode, 1, refused.stderr)
        self.assertIn("allow-untrusted", refused.stderr)
        allowed = run_cli(
            [
                "silver-evaluate",
                "--gold", gold,
                "--predictions", predictions,
                "--allow-untrusted",
                "--out", os.path.join(self.tmp.name, "evaluation-allowed.json"),
            ]
        )
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        with open(os.path.join(self.tmp.name, "evaluation-allowed.json"), encoding="utf-8") as handle:
            evaluation = json.load(handle)
        self.assertFalse(evaluation["agreement"]["trustworthy"])
