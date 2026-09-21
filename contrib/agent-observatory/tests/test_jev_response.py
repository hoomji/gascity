"""Tests for saved Jev response validation and immutable classification."""

from __future__ import annotations

import copy
import os
import tempfile
import unittest

try:
    from . import support
except ImportError:  # pragma: no cover
    import support

from agent_observatory import load_taxonomy
from agent_observatory.errors import LabelConflictError, ResponseError
from agent_observatory.jev import build_request, import_response, parse_json_document, persist_request
from agent_observatory.store import ObservatoryStore


class JevResponseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ObservatoryStore(os.path.join(self.tmp.name, "p.db"))
        self.addCleanup(self.store.close)
        self.taxonomy = load_taxonomy()
        self.request = build_request(
            {"summary": "fix a bug"}, self.taxonomy, snapshot_hash="c" * 64
        )
        persist_request(self.store, self.request)
        self.valid = support.valid_response(self.request.body)

    def _primary_options(self):
        return list(self.request.body["questions"]["primary_intent"]["criteria"])

    def _noul_id(self):
        return next(
            qid for qid, q in self.request.body["questions"].items() if q["type"] == "noul"
        )

    def test_valid_response_is_stored_with_all_probabilities(self):
        result = import_response(self.store, self.valid, request_hash=self.request.request_hash)
        self.assertFalse(result.deduplicated)
        self.assertEqual(self.store.classification_count(), 1)
        stored = self.store.get_classification(
            subject_kind=self.request.subject_kind,
            snapshot_hash=self.request.snapshot_hash,
            taxonomy_version=self.request.taxonomy_version,
            question_hash=self.request.question_hash,
            model_version=self.request.model,
        )
        self.assertEqual(stored["response_hash"], result.response_hash)
        primary = next(a for a in stored["answers"] if a["question_id"] == "primary_intent")
        self.assertEqual(set(primary["answer"]["probabilities"]), set(self._primary_options()))
        self.assertEqual(primary["answer"]["choice"], self._primary_options()[0])

    def test_wire_response_uses_choice_and_noul_fields_not_value(self):
        answers = self.valid["answers"]
        self.assertEqual(answers["primary_intent"]["type"], "choice")
        self.assertIn("choice", answers["primary_intent"])
        self.assertNotIn("value", answers["primary_intent"])
        noul = answers[self._noul_id()]
        self.assertEqual(noul["type"], "noul")
        self.assertIn("noul", noul)
        self.assertNotIn("value", noul)

    def test_replayed_response_deduplicates(self):
        first = import_response(self.store, self.valid, request_hash=self.request.request_hash)
        second = import_response(self.store, self.valid, request_hash=self.request.request_hash)
        self.assertEqual(first.classification_id, second.classification_id)
        self.assertTrue(second.deduplicated)
        self.assertEqual(self.store.classification_count(), 1)

    def test_different_response_for_same_subject_is_rejected_not_overwritten(self):
        import_response(self.store, self.valid, request_hash=self.request.request_hash)
        options = self._primary_options()
        conflicting = support.valid_response(self.request.body, choice_value=options[1])
        with self.assertRaises(LabelConflictError):
            import_response(self.store, conflicting, request_hash=self.request.request_hash)
        self.assertEqual(self.store.classification_count(), 1)

    def test_missing_answer_is_rejected(self):
        broken = copy.deepcopy(self.valid)
        broken["answers"].pop("primary_intent")
        with self.assertRaises(ResponseError):
            import_response(self.store, broken, request_hash=self.request.request_hash)

    def test_unknown_answer_is_rejected(self):
        broken = copy.deepcopy(self.valid)
        broken["answers"]["not_a_question"] = {"type": "noul", "noul": 0.5}
        with self.assertRaises(ResponseError):
            import_response(self.store, broken, request_hash=self.request.request_hash)

    def test_answer_type_must_match_question_type(self):
        broken = copy.deepcopy(self.valid)
        broken["answers"]["primary_intent"]["type"] = "noul"
        with self.assertRaises(ResponseError):
            import_response(self.store, broken, request_hash=self.request.request_hash)

    def test_invented_value_field_is_not_accepted_as_a_choice(self):
        broken = copy.deepcopy(self.valid)
        primary = broken["answers"]["primary_intent"]
        primary["value"] = primary.pop("choice")
        with self.assertRaises(ResponseError):
            import_response(self.store, broken, request_hash=self.request.request_hash)

    def test_probabilities_must_sum_to_one(self):
        broken = copy.deepcopy(self.valid)
        probabilities = broken["answers"]["primary_intent"]["probabilities"]
        for option in probabilities:
            probabilities[option] = 0.1
        with self.assertRaises(ResponseError):
            import_response(self.store, broken, request_hash=self.request.request_hash)

    def test_probabilities_must_cover_every_option(self):
        broken = copy.deepcopy(self.valid)
        probabilities = broken["answers"]["primary_intent"]["probabilities"]
        probabilities.pop(self._primary_options()[0])
        with self.assertRaises(ResponseError):
            import_response(self.store, broken, request_hash=self.request.request_hash)

    def test_confidence_must_be_in_unit_interval(self):
        broken = copy.deepcopy(self.valid)
        broken["answers"]["primary_intent"]["confidence"] = 1.5
        with self.assertRaises(ResponseError):
            import_response(self.store, broken, request_hash=self.request.request_hash)

    def test_nan_probability_is_rejected(self):
        broken = copy.deepcopy(self.valid)
        probabilities = broken["answers"]["primary_intent"]["probabilities"]
        probabilities[self._primary_options()[0]] = float("nan")
        with self.assertRaises(ResponseError):
            import_response(self.store, broken, request_hash=self.request.request_hash)

    def test_nan_in_json_text_is_rejected(self):
        with self.assertRaises(ResponseError):
            parse_json_document('{"value": NaN}', what="response")

    def test_noul_must_not_carry_confidence(self):
        broken = copy.deepcopy(self.valid)
        broken["answers"][self._noul_id()]["confidence"] = 0.5
        with self.assertRaises(ResponseError):
            import_response(self.store, broken, request_hash=self.request.request_hash)

    def test_noul_must_be_in_unit_interval(self):
        broken = copy.deepcopy(self.valid)
        broken["answers"][self._noul_id()]["noul"] = 2.0
        with self.assertRaises(ResponseError):
            import_response(self.store, broken, request_hash=self.request.request_hash)

    def test_model_mismatch_is_rejected(self):
        broken = copy.deepcopy(self.valid)
        broken["model"] = "some-other-model"
        with self.assertRaises(ResponseError):
            import_response(self.store, broken, request_hash=self.request.request_hash)

    def test_usage_must_be_nonnegative_integers(self):
        for bad_usage in ({"input_tokens": -1, "output_tokens": 2}, {"input_tokens": 1}, {"input_tokens": 1.5, "output_tokens": 2}):
            broken = copy.deepcopy(self.valid)
            broken["usage"] = bad_usage
            with self.assertRaises(ResponseError):
                import_response(self.store, broken, request_hash=self.request.request_hash)

    def test_stored_noul_answer_has_only_noul(self):
        import_response(self.store, self.valid, request_hash=self.request.request_hash)
        stored = self.store.get_classification(
            subject_kind=self.request.subject_kind,
            snapshot_hash=self.request.snapshot_hash,
            taxonomy_version=self.request.taxonomy_version,
            question_hash=self.request.question_hash,
            model_version=self.request.model,
        )
        noul = next(a for a in stored["answers"] if a["question_type"] == "noul")
        self.assertEqual(set(noul["answer"]), {"noul"})

    def test_unknown_request_hash_is_rejected(self):
        with self.assertRaises(ResponseError):
            import_response(self.store, self.valid, request_hash="0" * 64)


if __name__ == "__main__":
    unittest.main()
