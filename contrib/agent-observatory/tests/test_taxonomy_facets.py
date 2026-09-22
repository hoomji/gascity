"""Tests for the versioned evaluation-facet taxonomy."""

from __future__ import annotations

import os
import unittest

try:
    from . import support
except ImportError:  # pragma: no cover
    import support

from agent_observatory.errors import TaxonomyError
from agent_observatory.taxonomy import load_taxonomy

V2_PATH = os.path.join(support.PACKAGE_ROOT, "agent_observatory", "taxonomy", "jev_taxonomy_v2.json")


class TaxonomyFacetTest(unittest.TestCase):
    def setUp(self):
        self.taxonomy = load_taxonomy(V2_PATH)

    def test_v2_declares_every_taxonomy_facet(self):
        expected = {
            "primary_intent",
            "secondary_activity",
            "work_unit",
            "scope",
            "workflow",
            "phase",
            "target",
            "disposition",
            "bottleneck_hypothesis",
        }
        self.assertEqual({facet.facet_id for facet in self.taxonomy.facets}, expected)

    def test_cardinalities_are_declared(self):
        facets = self.taxonomy.facet_by_id()
        self.assertEqual(facets["primary_intent"].cardinality, "one")
        self.assertEqual(facets["scope"].cardinality, "one")
        self.assertEqual(facets["phase"].cardinality, "one")
        self.assertEqual(facets["secondary_activity"].cardinality, "many")
        self.assertEqual(facets["target"].cardinality, "many")
        self.assertEqual(facets["bottleneck_hypothesis"].cardinality, "many")

    def test_primary_intent_matches_wire_criteria(self):
        facet = self.taxonomy.facet_by_id()["primary_intent"]
        wire = self.taxonomy.by_id()["primary_intent"]
        self.assertEqual(set(facet.value_keys()), set(wire.option_keys))
        self.assertIn("unknown", facet.value_keys())

    def test_facet_hash_is_deterministic_and_versioned(self):
        self.assertEqual(self.taxonomy.facet_hash(), load_taxonomy(V2_PATH).facet_hash())
        self.assertNotEqual(self.taxonomy.facet_hash(), "")
        # v1 has no facets and no facet hash; adding facets changes the hash.
        self.assertEqual(load_taxonomy().facet_hash(), "")

    def test_wire_questions_still_build_a_valid_request(self):
        from agent_observatory.jev import build_request

        request = build_request({}, self.taxonomy, snapshot_hash="a" * 64)
        self.assertEqual(request.body["questions"]["primary_intent"]["type"], "choice")
        self.assertIn("unknown", request.body["questions"]["primary_intent"]["criteria"])

    def test_malformed_facets_are_rejected(self):
        import json
        import tempfile

        cases = {
            "bad cardinality": {"facet_id": "f", "cardinality": "lots", "values": ["a"]},
            "empty values": {"facet_id": "f", "cardinality": "one", "values": []},
            "duplicate value": {"facet_id": "f", "cardinality": "one", "values": ["a", "a"]},
            "unknown definition": {
                "facet_id": "f",
                "cardinality": "one",
                "values": ["a"],
                "definitions": {"b": "x"},
            },
        }
        with open(V2_PATH, encoding="utf-8") as handle:
            base = json.load(handle)
        for name, facet in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                document = dict(base)
                document["facets"] = [facet]
                path = os.path.join(tmp, "t.json")
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(document, handle)
                with self.assertRaises(TaxonomyError):
                    load_taxonomy(path)


if __name__ == "__main__":
    unittest.main()
