"""Tests for versioned gold annotations and their separation from predictions."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest

try:
    from . import support
except ImportError:  # pragma: no cover
    import support

from agent_observatory.annotations import load_gold_set, save_gold_annotations
from agent_observatory.errors import AnnotationError
from agent_observatory.store import ObservatoryStore
from agent_observatory.taxonomy import load_taxonomy

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "gold")
GOLD_PATH = os.path.join(FIXTURES, "gold_episodes_v1.json")
V2_PATH = os.path.join(support.PACKAGE_ROOT, "agent_observatory", "taxonomy", "jev_taxonomy_v2.json")


class GoldAnnotationTest(unittest.TestCase):
    def setUp(self):
        self.taxonomy = load_taxonomy(V2_PATH)
        self.gold_set = load_gold_set(GOLD_PATH, self.taxonomy)

    def test_fixture_loads_with_facets_and_flags(self):
        self.assertEqual(self.gold_set.gold_set_version, "pinned-mechanics-1")
        self.assertEqual(self.gold_set.taxonomy_version, "2.0.0")
        self.assertEqual(len(self.gold_set.episodes), 14)
        flags = {flag for episode in self.gold_set.episodes for flag in episode.flags}
        self.assertEqual(flags, {"injected", "uncertain", "rare", "contested"})

    def test_gold_set_hash_is_stable_and_content_addressed(self):
        again = load_gold_set(GOLD_PATH, self.taxonomy)
        self.assertEqual(self.gold_set.gold_set_hash(), again.gold_set_hash())
        self.assertEqual(len(self.gold_set.gold_set_hash()), 64)

    def test_annotation_hash_changes_with_labels(self):
        original = self.gold_set.episodes[0]
        self.assertNotEqual(original.annotation_hash(), self.gold_set.episodes[1].annotation_hash())

    def test_annotations_persist_separately_and_are_append_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            with ObservatoryStore(os.path.join(tmp, "p.db")) as store:
                self.assertEqual(save_gold_annotations(store, self.gold_set), 14)
                self.assertEqual(store.gold_annotation_count(), 14)
                # Identical replay dedupes.
                self.assertEqual(save_gold_annotations(store, self.gold_set), 0)
                self.assertEqual(store.gold_annotation_count(), 14)
                rows = list(store.iter_gold_annotations())
                self.assertEqual(rows[0]["labels"]["primary_intent"], ["bugfix"])
                # No predictions were written by an annotation save.
                self.assertEqual(store.classification_count(), 0)

    def test_correction_is_a_new_row_not_an_overwrite(self):
        import dataclasses

        corrected_episode = dataclasses.replace(
            self.gold_set.episodes[0], labels={"primary_intent": ("implementation",)}
        )
        corrected = dataclasses.replace(self.gold_set, episodes=(corrected_episode,))
        with tempfile.TemporaryDirectory() as tmp:
            with ObservatoryStore(os.path.join(tmp, "p.db")) as store:
                save_gold_annotations(store, self.gold_set)
                save_gold_annotations(store, corrected)
                self.assertEqual(store.gold_annotation_count(), 15)

    def test_second_annotator_is_a_new_row(self):
        import dataclasses

        original_annotator = self.gold_set.episodes[0].annotator
        bob = dataclasses.replace(
            self.gold_set,
            episodes=tuple(
                dataclasses.replace(episode, annotator="bob")
                for episode in self.gold_set.episodes
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            with ObservatoryStore(os.path.join(tmp, "p.db")) as store:
                self.assertEqual(save_gold_annotations(store, self.gold_set), 14)
                # Identical labels, different annotator: a genuinely new row.
                self.assertEqual(save_gold_annotations(store, bob), 14)
                self.assertEqual(store.gold_annotation_count(), 28)
                annotators = {row["annotator"] for row in store.iter_gold_annotations()}
                self.assertEqual(annotators, {original_annotator, "bob"})

    def test_metadata_correction_is_a_new_row(self):
        import dataclasses

        first = self.gold_set.episodes[0]
        corrected_episode = dataclasses.replace(
            first, metadata={**dict(first.metadata), "bead_kind": "feature"}
        )
        corrected = dataclasses.replace(self.gold_set, episodes=(corrected_episode,))
        with tempfile.TemporaryDirectory() as tmp:
            with ObservatoryStore(os.path.join(tmp, "p.db")) as store:
                save_gold_annotations(store, self.gold_set)
                # Identical labels, corrected metadata: still a new row.
                self.assertEqual(save_gold_annotations(store, corrected), 1)
                self.assertEqual(store.gold_annotation_count(), 15)
                rows = list(store.iter_gold_annotations())
                self.assertEqual(rows[-1]["metadata"]["bead_kind"], "feature")

    def _load_mutated(self, mutation):
        with open(GOLD_PATH, encoding="utf-8") as handle:
            document = copy.deepcopy(json.load(handle))
        mutation(document)
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "gold.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        return path

    def test_unknown_label_is_rejected(self):
        path = self._load_mutated(
            lambda doc: doc["episodes"][0]["labels"].__setitem__("primary_intent", ["not_a_label"])
        )
        with self.assertRaises(AnnotationError):
            load_gold_set(path, self.taxonomy)

    def test_single_valued_facet_rejects_multiple_labels(self):
        path = self._load_mutated(
            lambda doc: doc["episodes"][0]["labels"].__setitem__(
                "primary_intent", ["bugfix", "implementation"]
            )
        )
        with self.assertRaises(AnnotationError):
            load_gold_set(path, self.taxonomy)

    def test_unknown_flag_is_rejected(self):
        path = self._load_mutated(lambda doc: doc["episodes"][0]["flags"].append("wishful"))
        with self.assertRaises(AnnotationError):
            load_gold_set(path, self.taxonomy)

    def test_duplicate_episode_ids_are_rejected(self):
        path = self._load_mutated(
            lambda doc: doc["episodes"][1].__setitem__("episode_id", doc["episodes"][0]["episode_id"])
        )
        with self.assertRaises(AnnotationError):
            load_gold_set(path, self.taxonomy)

    def test_taxonomy_version_mismatch_is_rejected(self):
        path = self._load_mutated(lambda doc: doc.__setitem__("taxonomy_version", "1.1.0"))
        with self.assertRaises(AnnotationError):
            load_gold_set(path, self.taxonomy)

    def test_naive_timestamp_is_rejected(self):
        path = self._load_mutated(
            lambda doc: doc["episodes"][0].__setitem__("observed_at", "2026-09-01T00:00:00")
        )
        with self.assertRaises(Exception):
            load_gold_set(path, self.taxonomy)


class AnnotateCliTest(unittest.TestCase):
    def test_cli_validates_and_stores_gold_set(self):
        from agent_observatory.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "p.db")
            status = main(
                [
                    "annotate",
                    "--db",
                    db,
                    "--gold",
                    GOLD_PATH,
                    "--taxonomy",
                    V2_PATH,
                ]
            )
            self.assertEqual(status, 0)
            with ObservatoryStore(db) as store:
                self.assertEqual(store.gold_annotation_count(), 14)
                self.assertEqual(store.classification_count(), 0)


if __name__ == "__main__":
    unittest.main()
