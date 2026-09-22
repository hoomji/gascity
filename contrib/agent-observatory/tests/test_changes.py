"""Tests for the M5 optimization change registry and screen."""

from __future__ import annotations

import unittest

try:
    from . import support  # noqa: F401  (path bootstrap)
except ImportError:  # pragma: no cover
    import support  # noqa: F401

from agent_observatory.changes import (
    BUNDLE_SCHEMA_VERSION,
    OPTIMIZATION_CATEGORIES,
    build_change_bundle,
    change_identity,
    classify_categories,
    normalize_activation,
    normalize_change,
    normalize_change_bundle,
)
from agent_observatory.errors import RegistryConflictError, RegistryError


class ChangeIdentityTest(unittest.TestCase):
    def test_pr_identity_is_repo_and_number_only(self):
        first = change_identity(repo="gascity", kind="pr", pr=6, source_ref=None)
        second = change_identity(repo="gascity", kind="pr", pr=6)
        other = change_identity(repo="gascity", kind="pr", pr=7)
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertNotEqual(first, change_identity(repo="gateway-llm", kind="pr", pr=6))

    def test_non_pr_identity_requires_artifact_digest(self):
        with self.assertRaises(RegistryError):
            change_identity(repo="gascity", kind="model")
        self.assertNotEqual(
            change_identity(repo="gascity", kind="model", artifact_digest="a"),
            change_identity(repo="gascity", kind="model", artifact_digest="b"),
        )

    def test_pr_requires_number_or_source_ref(self):
        with self.assertRaises(RegistryError):
            change_identity(repo="gascity", kind="pr")


class ChangeNormalizationTest(unittest.TestCase):
    def test_unknown_change_keys_are_rejected(self):
        with self.assertRaises(RegistryError) as caught:
            normalize_change({"repo": "gascity", "kind": "pr", "pr": 1, "surprise": True})
        self.assertIn("surprise", str(caught.exception))

    def test_non_pr_carries_no_pr_number_and_needs_digest(self):
        with self.assertRaises(RegistryError):
            normalize_change({"repo": "gascity", "kind": "model", "pr": 3, "artifact_digest": "d"})
        with self.assertRaises(RegistryError):
            normalize_change({"repo": "gascity", "kind": "model"})

    def test_pr_carries_no_artifact_digest(self):
        with self.assertRaises(RegistryError):
            normalize_change({"repo": "gascity", "kind": "pr", "pr": 1, "artifact_digest": "d"})

    def test_invalid_kind_rejected(self):
        with self.assertRaises(RegistryError):
            normalize_change({"repo": "gascity", "kind": "magic"})

    def test_bad_classification_value_rejected(self):
        with self.assertRaises(RegistryError):
            normalize_change(
                {"repo": "gascity", "kind": "pr", "pr": 1, "classification": "great"}
            )

    def test_invalid_timestamp_rejected(self):
        with self.assertRaises(RegistryError):
            normalize_change(
                {"repo": "gascity", "kind": "pr", "pr": 1, "merged_at": "2026-09-21 10:00"}
            )

    def test_change_hash_tracks_content_but_id_does_not(self):
        first = normalize_change({"repo": "gascity", "kind": "pr", "pr": 1, "title": "a"})
        second = normalize_change({"repo": "gascity", "kind": "pr", "pr": 1, "title": "b"})
        self.assertEqual(first["change_id"], second["change_id"])
        self.assertNotEqual(first["change_hash"], second["change_hash"])


class ScreeningTest(unittest.TestCase):
    def test_lockfile_path_alone_screens_as_package_resolution(self):
        categories, evidence = classify_categories(changed_paths=["go.sum"])
        self.assertIn("package_resolution", categories)
        self.assertTrue(any("package_resolution" in item for item in evidence))

    def test_test_files_need_keyword_evidence(self):
        without = classify_categories(
            changed_paths=["internal/foo/bar_test.go"], title="add more assertions"
        )[0]
        self.assertNotIn("test_selection", without)
        with_keyword = classify_categories(
            changed_paths=["internal/foo/bar_test.go"],
            title="reduce test runtime by sharding",
        )[0]
        self.assertIn("test_selection", with_keyword)

    def test_ci_workflow_needs_keyword_evidence(self):
        without = classify_categories(
            changed_paths=[".github/workflows/ci.yml"], title="add a job"
        )[0]
        self.assertNotIn("ci_runner_cache", without)
        with_keyword = classify_categories(
            changed_paths=[".github/workflows/ci.yml"],
            title="cache dependencies to cut ci time",
        )[0]
        self.assertIn("ci_runner_cache", with_keyword)

    def test_explicit_category_hint_is_accepted_and_validated(self):
        categories, evidence = classify_categories(
            changed_paths=["internal/x.go"], category_hint=["ci_runner_cache"]
        )
        self.assertEqual(categories, ["ci_runner_cache"])
        self.assertIn("ci_runner_cache:hint", evidence)
        with self.assertRaises(RegistryError):
            classify_categories(changed_paths=["x"], category_hint=["not_a_category"])

    def test_non_optimization_label_is_positive_evidence(self):
        change = normalize_change(
            {
                "repo": "gascity",
                "kind": "pr",
                "pr": 2,
                "title": "rewrite the docs",
                "labels": ["documentation"],
                "changed_paths": ["README.md"],
            }
        )
        self.assertEqual(change["classification"], "non_optimization")

    def test_uncategorized_change_stays_unknown(self):
        change = normalize_change(
            {"repo": "gascity", "kind": "pr", "pr": 3, "changed_paths": ["internal/x.go"]}
        )
        self.assertEqual(change["classification"], "unknown")
        self.assertEqual(change["categories"], [])

    def test_non_pr_kinds_get_default_intervention_categories(self):
        model = normalize_change({"repo": "gateway-llm", "kind": "model", "artifact_digest": "m1"})
        self.assertEqual(model["classification"], "optimization")
        self.assertIn("agent_runtime", model["categories"])
        toolchain = normalize_change({"repo": "gascity", "kind": "toolchain", "artifact_digest": "t1"})
        self.assertIn("dependency_footprint", toolchain["categories"])
        host = normalize_change({"repo": "gascity", "kind": "host", "artifact_digest": "h1"})
        self.assertIn("host_io_network", host["categories"])

    def test_explicit_classification_overrides_kind_default(self):
        change = normalize_change(
            {
                "repo": "gateway-llm",
                "kind": "config",
                "artifact_digest": "c1",
                "classification": "non_optimization",
            }
        )
        self.assertEqual(change["classification"], "non_optimization")

    def test_category_set_matches_plan_taxonomy(self):
        self.assertIn("agent_runtime", OPTIMIZATION_CATEGORIES)
        self.assertIn("dependency_footprint", OPTIMIZATION_CATEGORIES)
        self.assertEqual(len(set(OPTIMIZATION_CATEGORIES)), len(OPTIMIZATION_CATEGORIES))


class BundleTest(unittest.TestCase):
    def test_wrong_schema_version_rejected(self):
        with self.assertRaises(RegistryError):
            normalize_change_bundle({"schema_version": "2.0", "changes": []})
        self.assertEqual(BUNDLE_SCHEMA_VERSION, "1.0")

    def test_unknown_top_level_key_rejected(self):
        with self.assertRaises(RegistryError):
            normalize_change_bundle({"schema_version": "1.0", "mystery": 1})

    def test_duplicate_change_identity_rejected(self):
        with self.assertRaises(RegistryError):
            build_change_bundle(
                changes=[
                    {"repo": "gascity", "kind": "pr", "pr": 1},
                    {"repo": "gascity", "kind": "pr", "pr": 1, "title": "different"},
                ]
            )

    def test_activation_references_known_change(self):
        change = normalize_change({"repo": "gascity", "kind": "pr", "pr": 1})
        with self.assertRaises(RegistryError):
            normalize_activation(
                {"change_id": "0" * 64, "mechanism": "deploy"},
                changes_by_id={change["change_id"]: change},
            )

    def test_activation_bad_fingerprint_type_rejected(self):
        change = normalize_change({"repo": "gascity", "kind": "pr", "pr": 1})
        with self.assertRaises(RegistryError):
            normalize_activation(
                {
                    "change_id": change["change_id"],
                    "mechanism": "deploy",
                    "fingerprint": {"type": "magic", "value": "x"},
                },
                changes_by_id={change["change_id"]: change},
            )

    def test_duplicate_activation_rejected(self):
        raw_change = {"repo": "gascity", "kind": "config", "artifact_digest": "d"}
        change_id = normalize_change(raw_change)["change_id"]
        activation = {
            "change_id": change_id,
            "mechanism": "config_toggle",
            "fingerprint": {"type": "config_digest", "value": "d"},
        }
        with self.assertRaises(RegistryError):
            build_change_bundle(changes=[raw_change], activations=[activation, dict(activation)])

    def test_merged_pr_gets_implicit_merge_activation(self):
        bundle = build_change_bundle(
            changes=[
                {
                    "repo": "gascity",
                    "kind": "pr",
                    "pr": 6,
                    "merge_sha": "M" * 40,
                    "merged_at": "2026-09-21T10:00:00Z",
                }
            ]
        )
        self.assertEqual(len(bundle["activations"]), 1)
        activation = bundle["activations"][0]
        self.assertEqual(activation["mechanism"], "merge")
        self.assertEqual(activation["fingerprint"], {"type": "commit_sha", "value": "M" * 40})

    def test_explicit_merge_activation_suppresses_implicit_one(self):
        raw_change = {"repo": "gascity", "kind": "pr", "pr": 6, "merge_sha": "M" * 40}
        change_id = normalize_change(raw_change)["change_id"]
        bundle = build_change_bundle(
            changes=[raw_change],
            activations=[
                {
                    "change_id": change_id,
                    "mechanism": "merge",
                    "fingerprint": {"type": "commit_sha", "value": "M" * 40},
                }
            ],
        )
        self.assertEqual(len(bundle["activations"]), 1)

    def test_conflicting_duplicate_activation_content_rejected(self):
        raw_change = {"repo": "gascity", "kind": "config", "artifact_digest": "d"}
        change_id = normalize_change(raw_change)["change_id"]
        base = {
            "change_id": change_id,
            "mechanism": "config_toggle",
            "activated_at": "2026-09-21T10:00:00Z",
            "evidence": "fleet receipt a",
        }
        with self.assertRaises(RegistryConflictError):
            build_change_bundle(
                changes=[raw_change],
                activations=[base, dict(base, evidence="fleet receipt b")],
            )


if __name__ == "__main__":
    unittest.main()
