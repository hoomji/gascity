"""Collapse re-scoring: mapping, confusion, kappa, references, Jev scoring."""

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

from agent_observatory.collapse import (
    DEFAULT_MAJORITY,
    PRIMARY_INTENT_COLLAPSE_V1,
    AgreementReference,
    JudgeVote,
    LabelCollapse,
    build_agreement_references,
    build_collapsed_judge_prompt,
    confusion_matrix,
    disagreement_groups,
    disagreement_pairs,
    evaluate_references,
    fleiss_kappa,
    identity_collapse,
    kappa_summary,
    label_rows,
    load_judge_checkpoint,
    pairwise_cohen_kappa,
    pairwise_confusion_matrices,
    rescore_collapsed,
    resolve_collapse,
    votes_from_checkpoint,
    votes_from_report,
    write_collapse_report,
)
from agent_observatory.errors import SilverError

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures", "collapse")
CHECKPOINT = os.path.join(FIXTURES, "recorded_judge_checkpoint.json")
REPORT = os.path.join(FIXTURES, "recorded_judge_report.json")
JEV = os.path.join(FIXTURES, "jev_predictions.json")
PACKAGE_ROOT = support.PACKAGE_ROOT

JUDGES = ("glm-5p3-flash", "deepseek-v4-flash", "gpt6-luna", "gemini-3p8-flash")


def _fixture_votes():
    checkpoint = load_judge_checkpoint(CHECKPOINT)
    return votes_from_checkpoint(checkpoint)


def _fixture_jev():
    with open(JEV, encoding="utf-8") as handle:
        document = json.load(handle)
    return {prediction["episode_id"]: prediction["primary_intent"] for prediction in document["predictions"]}


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


class LabelCollapseTests(unittest.TestCase):
    def test_v1_maps_every_v2_label_and_groups_the_boundary(self):
        spec = PRIMARY_INTENT_COLLAPSE_V1
        self.assertEqual(len(spec.source_labels()), 9)
        self.assertEqual(spec.apply("dependency_worktree_agent_ops"), "agent_ops_review")
        self.assertEqual(spec.apply("pr_review"), "agent_ops_review")
        self.assertEqual(spec.apply("adversarial_review"), "agent_ops_review")
        self.assertEqual(spec.apply("bugfix"), "bugfix")
        self.assertEqual(spec.groups()["agent_ops_review"], (
            "adversarial_review",
            "dependency_worktree_agent_ops",
            "pr_review",
        ))
        self.assertEqual(set(spec.collapsed_labels()), set(spec.definitions))
        self.assertEqual(spec.validate(), spec)

    def test_validate_requires_mapping_and_definitions(self):
        with self.assertRaises(SilverError):
            LabelCollapse("empty", "1", {}).validate()
        missing_definition = LabelCollapse(
            "no-defs", "1", {"bugfix": "bugfix", "unknown": "unknown"}
        )
        with self.assertRaises(SilverError):
            missing_definition.validate()
        # The identity control opts out of definitions and is otherwise valid.
        identity = identity_collapse(["bugfix", "unknown"])
        self.assertEqual(identity.validate(require_definitions=False).apply("bugfix"), "bugfix")

    def test_validate_rejects_required_labels_it_cannot_map(self):
        spec = LabelCollapse(
            "partial",
            "1",
            {"bugfix": "bugfix", "unknown": "unknown"},
            definitions={"bugfix": "b", "unknown": "u"},
        )
        spec.validate()
        with self.assertRaises(SilverError):
            spec.validate(required_labels=["bugfix", "implementation"])

    def test_apply_rejects_an_unmapped_label(self):
        with self.assertRaises(SilverError):
            PRIMARY_INTENT_COLLAPSE_V1.apply("nonsense")

    def test_resolve_collapse_rejects_unknown_id(self):
        self.assertIs(resolve_collapse("primary_intent_collapse_v1"), PRIMARY_INTENT_COLLAPSE_V1)
        with self.assertRaises(SilverError):
            resolve_collapse("does-not-exist")

    def test_abstain_label_must_survive_the_collapse(self):
        with self.assertRaises(SilverError):
            LabelCollapse(
                "drops-unknown",
                "1",
                {"bugfix": "bugfix"},
                definitions={"bugfix": "b"},
            ).validate(require_definitions=False)


class VoteLoadingTests(unittest.TestCase):
    def test_checkpoint_and_report_yield_the_same_votes(self):
        checkpoint_votes, checkpoint_judges = _fixture_votes()
        with open(REPORT, encoding="utf-8") as handle:
            report_votes, report_judges = votes_from_report(json.load(handle))
        by_id = {vote.episode_id: dict(vote.labels) for vote in report_votes}
        self.assertEqual(set(checkpoint_judges), set(report_judges))
        for vote in checkpoint_votes:
            self.assertEqual(dict(vote.labels), by_id[vote.episode_id])
        # gemini is absent from e12 in both sources, and must be a None vote.
        e12 = {vote.episode_id: vote for vote in checkpoint_votes}["e12"]
        self.assertIsNone(e12.label("gemini-3p8-flash"))
        self.assertEqual(e12.label("glm-5p3-flash"), "bugfix")

    def test_checkpoint_rejects_malformed_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            with self.assertRaises(SilverError):
                load_judge_checkpoint(path)
            path2 = os.path.join(tmp, "bad-answer.json")
            with open(path2, "w", encoding="utf-8") as handle:
                json.dump({"glm": {"e1": "not json at all"}}, handle)
            with self.assertRaises(SilverError):
                votes_from_checkpoint(load_judge_checkpoint(path2))

    def test_report_requires_judges_and_episode_ids(self):
        with self.assertRaises(SilverError):
            votes_from_report({"episodes": []})
        with self.assertRaises(SilverError):
            votes_from_report({"judges": [{"judge_id": "j"}], "episodes": [{"adjudication": "x"}]})


class ConfusionAndDisagreementTests(unittest.TestCase):
    def test_confusion_matrix_counts_and_agreement(self):
        votes = (
            JudgeVote("e1", {"a": "bugfix", "b": "bugfix"}),
            JudgeVote("e2", {"a": "bugfix", "b": "implementation"}),
            JudgeVote("e3", {"a": "unknown", "b": "bugfix"}),
        )
        matrix = confusion_matrix(votes, ["a", "b"], "a", "b")
        self.assertEqual(matrix["counts"]["bugfix"]["bugfix"], 1)
        self.assertEqual(matrix["counts"]["bugfix"]["implementation"], 1)
        self.assertEqual(matrix["counts"]["unknown"]["bugfix"], 1)
        self.assertEqual(matrix["overlap"], 3)
        self.assertEqual(matrix["agreements"], 1)
        self.assertAlmostEqual(matrix["agreement_rate"], 1 / 3, places=9)
        # Treating unknown as an abstention drops e3 from the overlap.
        abstain = confusion_matrix(votes, ["a", "b"], "a", "b", drop_abstain=True)
        self.assertEqual(abstain["overlap"], 2)
        self.assertEqual(abstain["agreements"], 1)
        self.assertEqual(abstain["labels"], ["bugfix", "implementation"])

    def test_confusion_matrix_rejects_bad_judges(self):
        votes = (JudgeVote("e1", {"a": "bugfix"}),)
        with self.assertRaises(SilverError):
            confusion_matrix(votes, ["a"], "a", "b")
        with self.assertRaises(SilverError):
            confusion_matrix(votes, ["a"], "a", "a")

    def test_pairwise_matrices_cover_every_pair(self):
        votes, judges = _fixture_votes()
        matrices = pairwise_confusion_matrices(votes, judges)
        self.assertEqual(len(matrices), 6)  # 4 choose 2

    def test_disagreement_groups_rank_the_ops_unknown_boundary(self):
        votes, judges = _fixture_votes()
        groups = disagreement_groups(votes, judges)
        self.assertEqual(groups[0]["labels"], ["dependency_worktree_agent_ops", "unknown"])
        self.assertEqual(groups[0]["count"], 2)
        self.assertEqual(groups[0]["episode_ids"], ["e05", "e06"])
        # With unknown treated as abstain, e05/e06 stop being disagreements.
        abstain_groups = disagreement_groups(votes, judges, drop_abstain=True)
        self.assertNotIn(
            ["dependency_worktree_agent_ops", "unknown"],
            [entry["labels"] for entry in abstain_groups],
        )

    def test_disagreement_pairs_count_comparisons_and_episodes(self):
        votes, judges = _fixture_votes()
        pairs = disagreement_pairs(votes, judges)
        self.assertEqual(pairs[0]["labels"], ["dependency_worktree_agent_ops", "unknown"])
        self.assertEqual(pairs[0]["episodes"], 2)
        self.assertEqual(pairs[0]["comparisons"], 7)
        top_abstain = disagreement_pairs(votes, judges, drop_abstain=True)[0]
        self.assertEqual(top_abstain["episodes"], 1)


class KappaTests(unittest.TestCase):
    def test_fleiss_perfect_agreement_and_no_complete_rows(self):
        rows = [["a", "a", "a"], ["b", "b", "b"], ["a", "a", "a"]]
        self.assertEqual(fleiss_kappa(rows), 1.0)
        self.assertIsNone(fleiss_kappa([["a", None], [None, "b"]]))
        with self.assertRaises(SilverError):
            fleiss_kappa([["a"], ["b"]])

    def test_fleiss_matches_an_independent_hand_computation(self):
        rows = [["a", "a", "b"], ["a", "b", "b"], ["a", "a", "b"], ["b", "b", "b"]]
        # Po per subject: 1/3, 1/3, 1/3, 1 -> Pbar 0.5.
        # Marginals: a 5/12, b 7/12 -> Pe = 37/72. kappa = -1/35.
        self.assertAlmostEqual(fleiss_kappa(rows), -1 / 35, places=9)

    def test_pairwise_kappa_keeps_judge_ids_and_overlap(self):
        rows = [["a", "a", "b"], ["a", "a", "a"], [None, "b", "b"]]
        pairs = pairwise_cohen_kappa(rows, ["x", "y", "z"])
        self.assertEqual([(entry["left"], entry["right"]) for entry in pairs], [
            ("x", "y"), ("x", "z"), ("y", "z")
        ])
        self.assertEqual(pairs[0]["overlap"], 2)
        self.assertEqual(pairs[1]["overlap"], 2)
        self.assertEqual(pairs[2]["overlap"], 3)

    def test_kappa_summary_uses_the_minimum_pair(self):
        rows = [
            ["a", "a", "a"],
            ["a", "a", "a"],
            ["b", "b", "b"],
            ["b", "b", "b"],
        ]
        summary = kappa_summary(rows, ["x", "y", "z"])
        self.assertEqual(summary["min_pairwise_cohen_kappa"], 1.0)
        self.assertEqual(summary["fleiss_kappa"], 1.0)
        self.assertEqual(len(summary["pairwise_cohen_kappa"]), 3)


class ReferenceTests(unittest.TestCase):
    def test_references_separate_unanimous_majority_and_none(self):
        votes, judges = _fixture_votes()
        label_refs = build_agreement_references(votes, judges, collapse=PRIMARY_INTENT_COLLAPSE_V1)
        unanimous = [ref.episode_id for ref in label_refs if ref.kind == "unanimous"]
        majority = [ref.episode_id for ref in label_refs if ref.kind == "majority"]
        none = [ref.episode_id for ref in label_refs if ref.kind == "none"]
        self.assertEqual(unanimous, ["e01", "e02", "e03", "e07", "e09"])
        self.assertEqual(majority, ["e04", "e06", "e11", "e12"])
        self.assertEqual(sorted(unanimous + majority + none), sorted(vote.episode_id for vote in votes))

    def test_unknown_as_abstain_removes_unknown_only_references(self):
        votes, judges = _fixture_votes()
        refs = build_agreement_references(
            votes, judges, collapse=PRIMARY_INTENT_COLLAPSE_V1, unknown_as_abstain=True
        )
        unanimous = [ref.episode_id for ref in refs if ref.kind == "unanimous"]
        # e07 is all-unknown and e06's lone unknown is dropped; e03 becomes unanimous.
        self.assertEqual(unanimous, ["e01", "e02", "e03", "e09"])

    def test_majority_reference_needs_a_unique_label(self):
        votes = (
            JudgeVote("tie", {"a": "bugfix", "b": "implementation", "c": "bugfix", "d": "implementation"}),
            JudgeVote("three", {"a": "bugfix", "b": "bugfix", "c": "bugfix", "d": "unknown"}),
        )
        refs = {ref.episode_id: ref for ref in build_agreement_references(votes, ["a", "b", "c", "d"])}
        self.assertEqual(refs["tie"].kind, "none")
        self.assertEqual(refs["three"].kind, "majority")
        self.assertEqual(refs["three"].label, "bugfix")
        self.assertEqual(refs["three"].agreeing, 3)

    def test_evaluate_references_scores_only_selected_kinds(self):
        refs = (
            AgreementReference("u", "bugfix", "unanimous", 4, 4, {}),
            AgreementReference("m", "implementation", "majority", 3, 4, {}),
            AgreementReference("n", None, "none", 0, 4, {}),
        )
        result = evaluate_references(refs, {"u": "bugfix", "m": "implementation", "n": "bugfix"})
        self.assertEqual(result["reference_kinds"], ["majority", "unanimous"])
        self.assertEqual(result["reference_episodes"], 2)
        self.assertEqual(result["accuracy"], 1.0)
        # Missing Jev labels are reported, not silently dropped from the count.
        missing = evaluate_references(refs, {"u": "bugfix"}, kinds=("unanimous", "majority"))
        self.assertEqual(missing["missing"], ["m"])


class RescoreTests(unittest.TestCase):
    def test_rescore_reports_both_unknown_policies_and_a_baseline(self):
        votes, judges = _fixture_votes()
        report = rescore_collapsed(votes, judges, _fixture_jev())
        self.assertEqual(report["kind"], "silver_collapse_rescore")
        self.assertFalse(report["clears_floor"])
        self.assertIsNotNone(report["baseline"])
        self.assertEqual(report["baseline"]["collapse"]["collapse_id"], "identity")
        self.assertEqual(report["baseline"]["modes"]["label"]["trust"]["min_pairwise_cohen_kappa"], 0.25961538461538464)
        modes = report["modes"]
        self.assertAlmostEqual(modes["label"]["trust"]["min_pairwise_cohen_kappa"], 0.26666666666666666, places=9)
        self.assertAlmostEqual(modes["abstain"]["trust"]["min_pairwise_cohen_kappa"], 0.34375, places=9)
        self.assertEqual(modes["label"]["references"]["unanimous"], 5)
        self.assertEqual(modes["label"]["references"]["majority_or_better"], 9)
        self.assertEqual(modes["abstain"]["references"]["unanimous"], 4)
        self.assertEqual(modes["abstain"]["references"]["majority_or_better"], 8)
        self.assertAlmostEqual(modes["label"]["jev"]["unanimous"]["accuracy"], 0.6, places=9)
        self.assertAlmostEqual(modes["label"]["jev"]["majority"]["accuracy"], 0.625, places=9)
        self.assertAlmostEqual(modes["abstain"]["jev"]["unanimous"]["accuracy"], 0.75, places=9)
        self.assertAlmostEqual(modes["abstain"]["jev"]["majority"]["accuracy"], 5 / 7, places=9)
        self.assertEqual(modes["label"]["jev"]["majority"]["missing"], ["e12"])
        self.assertEqual(report["diagnostics"]["disagreement_pairs"][0]["labels"], [
            "dependency_worktree_agent_ops", "unknown"
        ])
        self.assertEqual(len(report["diagnostics"]["pairwise_confusion_matrices"]), 6)

    def test_rescore_can_omit_the_baseline(self):
        votes, judges = _fixture_votes()
        report = rescore_collapsed(votes, judges, {}, include_baseline=False)
        self.assertIsNone(report["baseline"])

    def test_rescore_flags_a_clearing_design(self):
        agreeing = tuple(
            JudgeVote(f"e{index}", {judge: "bugfix" for judge in JUDGES}) for index in range(6)
        )
        report = rescore_collapsed(agreeing, JUDGES, {vote.episode_id: "bugfix" for vote in agreeing})
        self.assertTrue(report["clears_floor"])
        self.assertTrue(report["modes"]["label"]["trust"]["clears_floor"])
        self.assertEqual(report["modes"]["label"]["trust"]["min_pairwise_cohen_kappa"], 1.0)

    def test_collapse_overlap_floor_blocks_a_thin_abstain_overlap(self):
        # Under unknown-as-abstain the six unknown votes from judge b are dropped,
        # leaving a perfect but thin four-episode overlap. kappa is 1.0 and the
        # sample is ten, so only the shared overlap floor can refuse the gate.
        votes = tuple(
            JudgeVote(
                f"e{index}",
                {"a": "bugfix", "b": "unknown" if index <= 6 else "bugfix"},
            )
            for index in range(1, 11)
        )
        report = rescore_collapsed(votes, ("a", "b"), {}, include_baseline=False)
        abstain = report["modes"]["abstain"]["trust"]
        self.assertEqual(abstain["min_pairwise_cohen_kappa"], 1.0)
        self.assertEqual(abstain["sample_size"], 10)
        self.assertFalse(abstain["coverage_ok"])
        self.assertEqual(abstain["trust_reason"], "missing_labels_over_floor")
        self.assertFalse(abstain["clears_floor"])
        self.assertFalse(report["clears_floor"])

    def test_rescore_rejects_a_collapse_that_cannot_map_the_votes(self):
        votes = (
            JudgeVote("e1", {"glm-5p3-flash": "bugfix", "deepseek-v4-flash": "implementation",
                             "gpt6-luna": "bugfix", "gemini-3p8-flash": "bugfix"}),
        )
        partial = LabelCollapse(
            "partial", "1", {"bugfix": "bugfix", "unknown": "unknown"},
            definitions={"bugfix": "b", "unknown": "u"},
        )
        with self.assertRaises(SilverError):
            rescore_collapsed(votes, JUDGES, {}, collapse=partial)

    def test_rescore_rejects_empty_inputs(self):
        with self.assertRaises(SilverError):
            rescore_collapsed([], JUDGES, {})
        with self.assertRaises(SilverError):
            rescore_collapsed([JudgeVote("e1", {})], [], {})

    def test_label_rows_project_and_abstain(self):
        votes = (JudgeVote("e1", {"a": "pr_review", "b": "unknown"}),)
        rows = label_rows(votes, ["a", "b"], collapse=PRIMARY_INTENT_COLLAPSE_V1)
        self.assertEqual(rows, [["agent_ops_review", "unknown"]])
        abstained = label_rows(
            votes, ["a", "b"], collapse=PRIMARY_INTENT_COLLAPSE_V1, unknown_as_abstain=True
        )
        self.assertEqual(abstained, [["agent_ops_review", None]])


class PromptTests(unittest.TestCase):
    def test_prompt_defines_every_collapsed_label_once(self):
        prompt = build_collapsed_judge_prompt("Please fix the flaky test")
        for label, definition in PRIMARY_INTENT_COLLAPSE_V1.definitions.items():
            self.assertIn(f"- {label}: {definition}", prompt)
        self.assertIn(PRIMARY_INTENT_COLLAPSE_V1.collapse_id, prompt)
        self.assertIn("Collapsed judge prompt version", prompt)
        self.assertIn("Please fix the flaky test", prompt)
        self.assertIn("ONE JSON object", prompt)

    def test_prompt_bounds_text(self):
        prompt = build_collapsed_judge_prompt("x" * 5000, max_bytes=64)
        self.assertIn("[truncated:", prompt)


class ReportWriterTests(unittest.TestCase):
    def test_write_collapse_report_is_stable_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "report.json")
            write_collapse_report({"b": 1, "a": {"z": 2}}, path)
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        self.assertTrue(text.endswith("\n"))
        self.assertLess(text.index('"a"'), text.index('"b"'))

    def test_write_collapse_report_rejects_non_finite_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "report.json")
            with self.assertRaises(ValueError):
                write_collapse_report({"value": float("nan")}, path)


class CollapseCliTests(unittest.TestCase):
    def test_cli_writes_the_rescore_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "collapse.json")
            result = run_cli([
                "silver-collapse",
                "--checkpoint", CHECKPOINT,
                "--jev-predictions", JEV,
                "--out", out,
            ])
            self.assertEqual(result.returncode, 0, result.stderr)
            with open(out, encoding="utf-8") as handle:
                report = json.load(handle)
        self.assertEqual(report["kind"], "silver_collapse_rescore")
        self.assertFalse(report["clears_floor"])
        self.assertEqual(report["collapse"]["collapse_id"], PRIMARY_INTENT_COLLAPSE_V1.collapse_id)

    def test_cli_require_clear_fails_but_still_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "collapse.json")
            result = run_cli([
                "silver-collapse",
                "--report", REPORT,
                "--jev-predictions", JEV,
                "--out", out,
                "--require-clear",
            ])
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()
