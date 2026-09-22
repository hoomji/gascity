"""Tests for the M5 actual-exposure join and optimization ledger."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

try:
    from . import support
except ImportError:  # pragma: no cover
    import support

from agent_observatory.changes import build_change_bundle, normalize_change, normalize_change_bundle
from agent_observatory.errors import RegistryConflictError
from agent_observatory.exposure import (
    CommitGraph,
    attach_session_fingerprints,
    baseline_status,
    build_ledger,
    evaluate_change,
    price_status,
    session_evidence_from_store,
)
from agent_observatory.store import ObservatoryStore

SHARED = "M" * 40


def _bundle(changes, activations=(), graph=None, fingerprints=()):
    return build_change_bundle(
        changes=changes,
        activations=activations,
        commit_graph=graph or {},
        session_fingerprints=fingerprints,
    )


def _session(
    session_id="session-1",
    *,
    repo="gascity",
    host="host-a",
    provider="codex",
    commits=(),
    models=(),
    fingerprints=(),
    first="2026-09-22T00:00:00Z",
    last="2026-09-22T01:00:00Z",
):
    return {
        "session": ["city-a", host, provider, session_id],
        "repo": repo,
        "host_id": host,
        "provider": provider,
        "commit_shas": list(commits),
        "models": list(models),
        "fingerprints": list(fingerprints),
        "first_timestamp": first,
        "last_timestamp": last,
        "event_count": 1,
    }


class CommitGraphTest(unittest.TestCase):
    def test_contains_is_three_valued(self):
        graph = CommitGraph({"M": ["B"], "B": []})
        self.assertTrue(graph.contains("M", "M"))
        self.assertTrue(graph.contains("B", "M"))
        self.assertFalse(graph.contains("M", "B"))
        self.assertIsNone(graph.contains("M", "unknown"))
        self.assertIsNone(graph.contains("ghost", "M"))

    def test_equal_commits_outside_graph_are_unknown(self):
        graph = CommitGraph({})
        self.assertIsNone(graph.contains("X", "X"))


class ExposureJoinTest(unittest.TestCase):
    def _merged_pr(self, pr=6, **overrides):
        raw = {
            "repo": "gascity",
            "kind": "pr",
            "pr": pr,
            "title": "perf: cache the build",
            "merge_sha": SHARED,
            "merged_at": "2026-09-21T10:00:00Z",
            "changed_paths": ["Makefile"],
        }
        raw.update(overrides)
        return _bundle([raw])

    def test_exact_merge_commit_is_exposed(self):
        bundle = self._merged_pr()
        rollup = evaluate_change(
            bundle["changes"][0],
            bundle["activations"],
            graph=CommitGraph({"M" * 40: []}),
            sessions=[_session(commits=[SHARED])],
        )
        self.assertEqual(rollup["status"], "exposed")
        self.assertEqual(rollup["counts"]["exposed"], 1)

    def test_descendant_commit_is_exposed_by_ancestry(self):
        bundle = self._merged_pr()
        graph = {"child": [SHARED], SHARED: []}
        rollup = evaluate_change(
            bundle["changes"][0],
            bundle["activations"],
            graph=CommitGraph(graph),
            sessions=[_session(commits=["child"])],
        )
        self.assertEqual(rollup["status"], "exposed")
        self.assertEqual(rollup["rows"][0]["evidence"][0]["verdict"], "commit_ancestry")

    def test_pre_merge_worktree_is_not_exposed(self):
        # The worktree was cut at B, which is an ancestor of the merge commit:
        # running after the merge does not put the change in the worktree.
        bundle = self._merged_pr()
        graph = {SHARED: ["B"], "B": []}
        rollup = evaluate_change(
            bundle["changes"][0],
            bundle["activations"],
            graph=CommitGraph(graph),
            sessions=[_session(commits=["B"])],
        )
        self.assertEqual(rollup["status"], "unexposed")
        self.assertEqual(rollup["counts"]["exposed"], 0)

    def test_merged_but_unobserved_commit_is_unknown_not_exposed(self):
        bundle = self._merged_pr()
        rollup = evaluate_change(
            bundle["changes"][0],
            bundle["activations"],
            graph=CommitGraph({SHARED: []}),
            sessions=[_session(commits=["X" * 40])],
        )
        self.assertEqual(rollup["status"], "unknown")
        self.assertEqual(rollup["counts"]["exposed"], 0)

    def test_session_without_commit_evidence_is_unknown(self):
        bundle = self._merged_pr()
        rollup = evaluate_change(
            bundle["changes"][0],
            bundle["activations"],
            graph=CommitGraph({SHARED: []}),
            sessions=[_session(commits=[])],
        )
        self.assertEqual(rollup["status"], "unknown")

    def test_session_before_merge_is_unexposed(self):
        bundle = self._merged_pr()
        rollup = evaluate_change(
            bundle["changes"][0],
            bundle["activations"],
            graph=CommitGraph({SHARED: []}),
            sessions=[
                _session(
                    commits=[SHARED],
                    first="2026-09-20T00:00:00Z",
                    last="2026-09-20T01:00:00Z",
                )
            ],
        )
        self.assertEqual(rollup["status"], "unexposed")

    def test_offset_session_timestamps_are_compared_chronologically(self):
        # Raw session bounds must be normalized before the activation window
        # check: 09:30-04:00 is 13:30Z, so this session is *inside* the window
        # even though the raw string sorts before the Z-suffixed activation.
        bundle = self._merged_pr()
        rollup = evaluate_change(
            bundle["changes"][0],
            bundle["activations"],
            graph=CommitGraph({SHARED: []}),
            sessions=[
                _session(
                    commits=[SHARED],
                    first="2026-09-21T09:30:00-04:00",
                    last="2026-09-21T10:00:00-04:00",
                )
            ],
        )
        self.assertEqual(rollup["status"], "exposed")
        self.assertEqual(rollup["rows"][0]["reason"], "exact_commit")

    def test_session_after_deactivation_is_unexposed(self):
        # ``after_deactivation`` is a window verdict and needs its own coverage.
        bundle = _bundle(
            [{"repo": "gateway-llm", "kind": "config", "artifact_digest": "cfg-2"}],
            activations=[
                {
                    "change_id": normalize_change(
                        {"repo": "gateway-llm", "kind": "config", "artifact_digest": "cfg-2"}
                    )["change_id"],
                    "mechanism": "config_toggle",
                    "target": "gateway-llm",
                    "activated_at": "2026-09-22T00:00:00Z",
                    "deactivated_at": "2026-09-22T01:00:00Z",
                    "fingerprint": {"type": "config_digest", "value": "cfg-2"},
                }
            ],
        )
        rollup = evaluate_change(
            bundle["changes"][0],
            bundle["activations"],
            graph=CommitGraph(),
            sessions=[
                _session(
                    repo="gateway-llm",
                    first="2026-09-22T02:00:00Z",
                    last="2026-09-22T03:00:00Z",
                    fingerprints=[
                        {"type": "config_digest", "value": "cfg-2", "observed_at": "2026-09-22T02:30:00Z"}
                    ],
                )
            ],
        )
        self.assertEqual(rollup["status"], "unexposed")
        self.assertEqual(rollup["rows"][0]["reason"], "after_deactivation")

    def test_other_repo_session_is_not_a_candidate(self):
        bundle = self._merged_pr()
        rollup = evaluate_change(
            bundle["changes"][0],
            bundle["activations"],
            graph=CommitGraph({SHARED: []}),
            sessions=[_session(repo="gateway-llm", commits=[SHARED])],
        )
        self.assertTrue(rollup["no_candidates"])
        self.assertEqual(rollup["status"], "unknown")

    def test_repo_less_session_is_not_a_candidate_for_repo_bound_change(self):
        # A session whose events carried no repo must not be a candidate for
        # every repo-bound change, which would let a shared model fingerprint
        # create cross-repo exposure rows.
        bundle = self._merged_pr()
        rollup = evaluate_change(
            bundle["changes"][0],
            bundle["activations"],
            graph=CommitGraph({SHARED: []}),
            sessions=[_session(repo=None, models=["glm-4.6"])],
        )
        self.assertTrue(rollup["no_candidates"])
        self.assertEqual(rollup["candidate_sessions"], 0)
        self.assertEqual(rollup["status"], "unknown")

    def test_overlapping_changes_both_exposed(self):
        bundle = _bundle(
            [
                {
                    "repo": "gascity",
                    "kind": "pr",
                    "pr": 1,
                    "merge_sha": "A" * 40,
                    "merged_at": "2026-09-21T10:00:00Z",
                },
                {
                    "repo": "gascity",
                    "kind": "pr",
                    "pr": 2,
                    "merge_sha": "B" * 40,
                    "merged_at": "2026-09-21T11:00:00Z",
                },
            ]
        )
        graph = CommitGraph({"S": ["A" * 40, "B" * 40], "A" * 40: [], "B" * 40: []})
        by_change = {}
        for change in bundle["changes"]:
            by_change[change["pr"]] = evaluate_change(
                change, bundle["activations"], graph=graph, sessions=[_session(commits=["S"])]
            )
        self.assertEqual(by_change[1]["status"], "exposed")
        self.assertEqual(by_change[2]["status"], "exposed")

    def test_delayed_deployment_is_not_exposed(self):
        bundle = _bundle(
            [
                {
                    "repo": "gateway-llm",
                    "kind": "config",
                    "artifact_digest": "cfg-2",
                    "changed_paths": ["city.toml"],
                }
            ],
            activations=[
                {
                    "change_id": normalize_change(
                        {
                            "repo": "gateway-llm",
                            "kind": "config",
                            "artifact_digest": "cfg-2",
                            "changed_paths": ["city.toml"],
                        }
                    )["change_id"],
                    "mechanism": "deploy",
                    "target": "gateway-llm",
                    "pending": True,
                    "fingerprint": {"type": "config_digest", "value": "cfg-2"},
                }
            ],
        )
        rollup = evaluate_change(
            bundle["changes"][0],
            bundle["activations"],
            graph=CommitGraph(),
            sessions=[_session(repo="gateway-llm")],
        )
        self.assertEqual(rollup["status"], "unexposed")
        self.assertEqual(rollup["rows"][0]["reason"], "pending_activation")

    def test_matching_fingerprint_conflicts_with_pending_activation(self):
        # F4: a pending activation whose fingerprint the session actually
        # observed is contradictory direct evidence. The evidence cannot decide,
        # so the verdict is unknown, not a silent unexposed.
        bundle = _bundle(
            [{"repo": "gateway-llm", "kind": "config", "artifact_digest": "cfg-2"}],
            activations=[
                {
                    "change_id": normalize_change(
                        {"repo": "gateway-llm", "kind": "config", "artifact_digest": "cfg-2"}
                    )["change_id"],
                    "mechanism": "deploy",
                    "target": "gateway-llm",
                    "pending": True,
                    "fingerprint": {"type": "config_digest", "value": "cfg-2"},
                }
            ],
        )
        rollup = evaluate_change(
            bundle["changes"][0],
            bundle["activations"],
            graph=CommitGraph(),
            sessions=[
                _session(
                    repo="gateway-llm",
                    fingerprints=[
                        {"type": "config_digest", "value": "cfg-2", "observed_at": "2026-09-22T00:30:00Z"}
                    ],
                )
            ],
        )
        self.assertEqual(rollup["status"], "unknown")
        self.assertEqual(rollup["rows"][0]["reason"], "evidence_conflicts_window")
        self.assertEqual(rollup["counts"]["unexposed"], 0)

    def test_matching_fingerprint_conflicts_with_before_window(self):
        # The same precedence applies when the session predates activated_at:
        # the change was not yet activated, yet the matching fingerprint was
        # observed, so the two pieces of evidence contradict each other.
        bundle = _bundle(
            [{"repo": "gateway-llm", "kind": "config", "artifact_digest": "cfg-2"}],
            activations=[
                {
                    "change_id": normalize_change(
                        {"repo": "gateway-llm", "kind": "config", "artifact_digest": "cfg-2"}
                    )["change_id"],
                    "mechanism": "config_toggle",
                    "target": "gateway-llm",
                    "activated_at": "2026-09-23T00:00:00Z",
                    "fingerprint": {"type": "config_digest", "value": "cfg-2"},
                }
            ],
        )
        rollup = evaluate_change(
            bundle["changes"][0],
            bundle["activations"],
            graph=CommitGraph(),
            sessions=[
                _session(
                    repo="gateway-llm",
                    first="2026-09-22T00:00:00Z",
                    last="2026-09-22T01:00:00Z",
                    fingerprints=[
                        {"type": "config_digest", "value": "cfg-2", "observed_at": "2026-09-22T00:30:00Z"}
                    ],
                )
            ],
        )
        self.assertEqual(rollup["status"], "unknown")
        self.assertEqual(rollup["rows"][0]["reason"], "evidence_conflicts_window")

    def test_mismatched_fingerprint_before_activation_stays_unexposed(self):
        # A conflicting fingerprint value agrees with the before window, so the
        # window verdict still decides: unexposed, never unknown.
        bundle = _bundle(
            [{"repo": "gateway-llm", "kind": "config", "artifact_digest": "cfg-2"}],
            activations=[
                {
                    "change_id": normalize_change(
                        {"repo": "gateway-llm", "kind": "config", "artifact_digest": "cfg-2"}
                    )["change_id"],
                    "mechanism": "config_toggle",
                    "target": "gateway-llm",
                    "activated_at": "2026-09-23T00:00:00Z",
                    "fingerprint": {"type": "config_digest", "value": "cfg-2"},
                }
            ],
        )
        rollup = evaluate_change(
            bundle["changes"][0],
            bundle["activations"],
            graph=CommitGraph(),
            sessions=[
                _session(
                    repo="gateway-llm",
                    first="2026-09-22T00:00:00Z",
                    last="2026-09-22T01:00:00Z",
                    fingerprints=[
                        {"type": "config_digest", "value": "cfg-1", "observed_at": "2026-09-22T00:30:00Z"}
                    ],
                )
            ],
        )
        self.assertEqual(rollup["status"], "unexposed")
        self.assertEqual(rollup["rows"][0]["reason"], "before_activation")

    def test_config_fingerprint_inside_validity_is_exposed(self):
        bundle = _bundle(
            [{"repo": "gateway-llm", "kind": "config", "artifact_digest": "cfg-2"}],
            activations=[
                {
                    "change_id": normalize_change(
                        {"repo": "gateway-llm", "kind": "config", "artifact_digest": "cfg-2"}
                    )["change_id"],
                    "mechanism": "config_toggle",
                    "target": "gateway-llm",
                    "activated_at": "2026-09-22T00:00:00Z",
                    "fingerprint": {"type": "config_digest", "value": "cfg-2"},
                }
            ],
        )
        exposed = evaluate_change(
            bundle["changes"][0],
            bundle["activations"],
            graph=CommitGraph(),
            sessions=[
                _session(
                    repo="gateway-llm",
                    fingerprints=[
                        {"type": "config_digest", "value": "cfg-2", "observed_at": "2026-09-22T00:30:00Z"}
                    ],
                )
            ],
        )
        self.assertEqual(exposed["status"], "exposed")
        mismatch = evaluate_change(
            bundle["changes"][0],
            bundle["activations"],
            graph=CommitGraph(),
            sessions=[
                _session(
                    repo="gateway-llm",
                    fingerprints=[
                        {"type": "config_digest", "value": "cfg-1", "observed_at": "2026-09-22T00:30:00Z"}
                    ],
                )
            ],
        )
        self.assertEqual(mismatch["status"], "unexposed")
        unobserved = evaluate_change(
            bundle["changes"][0],
            bundle["activations"],
            graph=CommitGraph(),
            sessions=[_session(repo="gateway-llm")],
        )
        self.assertEqual(unobserved["status"], "unknown")

    def test_model_switch_uses_observed_model(self):
        bundle = _bundle(
            [{"repo": "gateway-llm", "kind": "model", "artifact_digest": "glm-4.6"}],
            activations=[
                {
                    "change_id": normalize_change(
                        {"repo": "gateway-llm", "kind": "model", "artifact_digest": "glm-4.6"}
                    )["change_id"],
                    "mechanism": "model_switch",
                    "target": "gateway-llm",
                    "activated_at": "2026-09-22T00:00:00Z",
                    "fingerprint": {"type": "model", "value": "glm-4.6"},
                }
            ],
        )
        rollup = evaluate_change(
            bundle["changes"][0],
            bundle["activations"],
            graph=CommitGraph(),
            sessions=[_session(repo="gateway-llm", models=["glm-4.6"])],
        )
        self.assertEqual(rollup["status"], "exposed")


class SessionEvidenceTest(unittest.TestCase):
    def test_attach_fingerprints_adds_sessions_without_events(self):
        fingerprints = [
            {
                "session": ["city-a", "host-a", "codex", "explicit"],
                "type": "model",
                "value": "glm-4.6",
                "observed_at": "2026-09-22T00:00:00Z",
            }
        ]
        sessions = attach_session_fingerprints([], fingerprints)
        self.assertEqual(len(sessions), 1)
        self.assertIn("glm-4.6", sessions[0]["models"])
        self.assertFalse(sessions[0]["derived"])

    def test_store_events_become_commit_and_model_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "projection.db")
            path = support.write_jsonl(
                os.path.join(tmp, "events.jsonl"),
                [
                    support.make_record(
                        event_id="e1",
                        repo="gascity",
                        commit_sha=SHARED,
                        model="glm-4.6",
                        timestamp="2026-09-22T00:00:00Z",
                    ),
                    support.make_record(
                        event_id="e2",
                        repo="gascity",
                        commit_sha="N" * 40,
                        timestamp="2026-09-22T00:01:00Z",
                    ),
                ],
            )
            with ObservatoryStore(db) as store:
                store.import_jsonl(path)
                sessions = session_evidence_from_store(store)
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["commit_shas"], sorted(["M" * 40, "N" * 40]))
            self.assertEqual(sessions[0]["models"], ["glm-4.6"])
            self.assertEqual(sessions[0]["repo"], "gascity")


class BaselinePriceTest(unittest.TestCase):
    def test_missing_baseline_and_price_stay_unknown(self):
        change = normalize_change({"repo": "gascity", "kind": "pr", "pr": 1})
        self.assertEqual(baseline_status(change), "unknown")
        self.assertEqual(price_status(change), "unknown")

    def test_explicit_baseline_and_price_are_present(self):
        change = normalize_change(
            {
                "repo": "gascity",
                "kind": "pr",
                "pr": 1,
                "baseline": {"metric": "test_seconds", "value": 12.5, "unit": "s", "source": "ci"},
                "prices": {"input_per_million_usd": 1.0},
            }
        )
        self.assertEqual(baseline_status(change), "present")
        self.assertEqual(price_status(change), "present")


class LedgerTest(unittest.TestCase):
    def _changes(self):
        return [
            {
                "repo": "gascity",
                "kind": "pr",
                "pr": 1,
                "title": "perf: cache the build",
                "merge_sha": SHARED,
                "merged_at": "2026-09-21T10:00:00Z",
                "changed_paths": ["Makefile"],
            },
            {
                "repo": "gascity",
                "kind": "pr",
                "pr": 2,
                "title": "rewrite the docs",
                "labels": ["documentation"],
                "changed_paths": ["README.md"],
            },
            {"repo": "gascity", "kind": "pr", "pr": 3, "changed_paths": ["internal/x.go"]},
        ]

    def test_denominator_retains_non_optimization_and_unknown(self):
        bundle = _bundle(self._changes(), graph={SHARED: []})
        ledger = build_ledger(
            bundle["changes"],
            bundle["activations"],
            [_session(commits=[SHARED])],
            graph=CommitGraph({SHARED: []}),
        )
        screening = ledger["screening"]
        self.assertEqual(screening["screened"], 3)
        self.assertEqual(screening["optimization"], 1)
        self.assertEqual(screening["non_optimization"], 1)
        self.assertEqual(screening["unknown"], 1)
        self.assertEqual(ledger["denominator"]["screened_total"], 3)
        self.assertEqual(ledger["denominator"]["registered_interventions"], 1)
        self.assertEqual(ledger["denominator"]["non_optimization_retained"], 1)

    def test_missingness_counts_unknown_baselines_and_prices(self):
        bundle = _bundle(self._changes(), graph={SHARED: []})
        ledger = build_ledger(
            bundle["changes"],
            bundle["activations"],
            [_session(commits=[SHARED])],
            graph=CommitGraph({SHARED: []}),
        )
        self.assertEqual(ledger["missingness"]["baseline_unknown"], 3)
        self.assertEqual(ledger["missingness"]["price_unknown"], 3)
        self.assertEqual(ledger["exposure_totals"]["exposed"], 1)

    def test_non_optimization_entry_has_no_exposure_rollup(self):
        bundle = _bundle(self._changes(), graph={SHARED: []})
        ledger = build_ledger(
            bundle["changes"], bundle["activations"], [], graph=CommitGraph({SHARED: []})
        )
        docs = [entry for entry in ledger["changes"] if entry["pr"] == 2][0]
        self.assertNotIn("exposure", docs)
        self.assertFalse(docs["registered_intervention"])


class StoreRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "projection.db")

    def _bundle(self, title="perf: cache the build", graph=None):
        return _bundle(
            [
                {
                    "repo": "gascity",
                    "kind": "pr",
                    "pr": 6,
                    "title": title,
                    "merge_sha": SHARED,
                    "merged_at": "2026-09-21T10:00:00Z",
                    "changed_paths": ["Makefile"],
                }
            ],
            graph={SHARED: []} if graph is None else graph,
        )

    def test_import_is_idempotent_and_conflicts_are_refused(self):
        with ObservatoryStore(self.db) as store:
            first = store.import_registry(self._bundle())
            self.assertEqual(first.changes_inserted, 1)
            self.assertEqual(first.activations_inserted, 1)
            second = store.import_registry(self._bundle())
            self.assertEqual(second.changes_inserted, 0)
            self.assertEqual(second.changes_deduplicated, 1)
            self.assertEqual(second.activations_deduplicated, 1)
            self.assertEqual(store.change_count(), 1)
            with self.assertRaises(RegistryConflictError):
                store.import_registry(self._bundle(title="perf: a different description"))
            self.assertEqual(store.change_count(), 1)

    def test_conflicting_commit_parents_are_refused(self):
        # ``commit_parents`` is immutable evidence: the same sha with different
        # parents must be a conflict, not a silent INSERT OR IGNORE no-op.
        with ObservatoryStore(self.db) as store:
            first = store.import_registry(self._bundle())
            self.assertEqual(first.commit_parents_inserted, 1)
            second = store.import_registry(self._bundle())
            self.assertEqual(second.commit_parents_inserted, 0)
            with self.assertRaises(RegistryConflictError):
                store.import_registry(self._bundle(graph={SHARED: ["P" * 40]}))
            self.assertEqual(store.load_commit_graph(), {SHARED: []})

    def test_exposures_are_replaced_not_appended(self):
        with ObservatoryStore(self.db) as store:
            store.import_registry(self._bundle())
            changes = list(store.iter_changes())
            activations = list(store.iter_activations())
            sessions = [_session(commits=[SHARED])]
            ledger = build_ledger(
                changes, activations, sessions, graph=CommitGraph(store.load_commit_graph())
            )
            rows = [
                {
                    "change_id": entry["change_id"],
                    "session": row["session"],
                    "status": row["status"],
                    "evidence": row["evidence"],
                }
                for entry in ledger["changes"]
                if entry.get("exposure")
                for row in entry["exposure"]["rows"]
            ]
            self.assertEqual(store.replace_exposures(rows), 1)
            self.assertEqual(store.exposure_count(), 1)
            store.replace_exposures(rows)
            self.assertEqual(store.exposure_count(), 1)
            stored = list(store.iter_exposures())
            self.assertEqual(stored[0]["status"], "exposed")

    def test_merge_alone_is_not_exposure(self):
        with ObservatoryStore(self.db) as store:
            store.import_registry(self._bundle())
            changes = list(store.iter_changes())
            activations = list(store.iter_activations())
            # A session with no commit evidence is a candidate but stays unknown.
            sessions = [_session(commits=[])]
            ledger = build_ledger(
                changes, activations, sessions, graph=CommitGraph(store.load_commit_graph())
            )
            entry = ledger["changes"][0]
            self.assertEqual(entry["exposure"]["status"], "unknown")
            self.assertEqual(ledger["exposure_totals"]["exposed"], 0)


class FixtureScenarioTest(unittest.TestCase):
    """Load the synthetic verification fixture covering the M5 gate scenarios."""

    def _bundle(self):
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "fixtures",
            "exposure",
            "scenarios.json",
        )
        with open(path, encoding="utf-8") as handle:
            return normalize_change_bundle(json.load(handle))

    def test_before_after_premerge_delayed_and_overlapping(self):
        bundle = self._bundle()
        graph = CommitGraph(bundle["commit_graph"])
        sessions = attach_session_fingerprints(
            [
                _session("after", repo="gascity", commits=["SESSIONCOMMIT000000000000000000000000000"]),
                _session("before", repo="gascity", commits=["BASECOMMIT0000000000000000000000000000000"]),
                _session("config-session", repo="gateway-llm"),
                _session("model-session", repo="gateway-llm"),
            ],
            bundle["session_fingerprints"],
        )
        ledger = build_ledger(bundle["changes"], bundle["activations"], sessions, graph=graph)
        by_pr = {entry["pr"]: entry for entry in ledger["changes"] if entry.get("pr")}

        # Overlapping merges are both exposed through the descendant session; the
        # pre-merge worktree session in the same repo is unexposed, not exposed.
        self.assertEqual(by_pr[101]["exposure"]["status"], "exposed")
        self.assertEqual(by_pr[102]["exposure"]["status"], "exposed")
        self.assertEqual(by_pr[101]["exposure"]["counts"]["unexposed"], 1)

        # A documentation PR stays in the denominator without becoming an intervention.
        self.assertEqual(by_pr[103]["classification"], "non_optimization")
        self.assertNotIn("exposure", by_pr[103])

        # Delayed deployment with direct fingerprint evidence: the pending
        # activation says the config is not live, but the session observed the
        # matching cfg-2 fingerprint. The window and the evidence disagree, so
        # the evidence cannot decide and exposure is unknown -- never a silent
        # unexposed that would under-count real use (F4).
        config = [entry for entry in ledger["changes"] if entry["kind"] == "config"][0]
        self.assertEqual(config["exposure"]["status"], "unknown")
        config_rows = {row["session"][-1]: row for row in config["exposure"]["rows"]}
        self.assertEqual(config_rows["config-session"]["reason"], "evidence_conflicts_window")
        # A session with no config fingerprint at all offers no positive
        # evidence, so the pending window still stands as unexposed.
        self.assertEqual(config_rows["model-session"]["status"], "unexposed")
        self.assertEqual(config_rows["model-session"]["reason"], "pending_activation")

        # Model switch exposed by the observed model, with a second session unknown.
        model = [entry for entry in ledger["changes"] if entry["kind"] == "model"][0]
        self.assertEqual(model["exposure"]["status"], "exposed")
        self.assertEqual(model["exposure"]["counts"]["unknown"], 1)

        self.assertEqual(ledger["screening"]["screened"], 5)
        self.assertEqual(ledger["denominator"]["registered_interventions"], 4)
        self.assertEqual(ledger["denominator"]["non_optimization_retained"], 1)
        self.assertEqual(ledger["missingness"]["baseline_unknown"], 5)
        self.assertEqual(ledger["missingness"]["price_unknown"], 5)


if __name__ == "__main__":
    unittest.main()
