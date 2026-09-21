"""Regression tests pinned to a verified real /v1/systemone smoke exchange.

The fixture under ``tests/fixtures/`` contains only the synthetic case data from
a live smoke test (no credential, no real transcript). It exists so the real wire
schema cannot silently regress back to the previously invented one.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from agent_observatory.jev import validate_response

FIXTURE = Path(__file__).parent / "fixtures" / "jev_smoke_contract.json"


class JevContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.request = cls.fixture["request"]
        cls.response = cls.fixture["response"]

    def test_request_has_no_top_level_instructions(self):
        self.assertNotIn("instructions", self.request)
        self.assertIn("state", self.request)
        self.assertIn("model", self.request)
        self.assertIn("questions", self.request)

    def test_questions_use_lowercase_types_instructions_and_criteria(self):
        for question_id, question in self.request["questions"].items():
            self.assertIn(question["type"], {"choice", "noul"}, question_id)
            self.assertIsInstance(question["instructions"], (str, dict, list))
            self.assertIsInstance(question["criteria"], dict)
            self.assertNotIn("description", question)
            self.assertNotIn("options", question)
        self.assertEqual(self.request["questions"]["primary_intent"]["type"], "choice")
        self.assertEqual(self.request["questions"]["requests_test"]["type"], "noul")

    def test_choice_answer_uses_choice_and_probabilities(self):
        answer = self.response["answers"]["primary_intent"]
        self.assertEqual(answer["type"], "choice")
        self.assertEqual(answer["choice"], "bugfix")
        self.assertNotIn("value", answer)
        self.assertEqual(
            set(answer["probabilities"]),
            set(self.request["questions"]["primary_intent"]["criteria"]),
        )

    def test_noul_answer_uses_noul_without_confidence(self):
        answer = self.response["answers"]["requests_test"]
        self.assertEqual(answer["type"], "noul")
        self.assertIn("noul", answer)
        self.assertNotIn("value", answer)
        self.assertNotIn("confidence", answer)

    def test_validator_accepts_the_real_response_against_the_real_request(self):
        answers = validate_response(self.response, self.request)
        by_id = {answer["question_id"]: answer for answer in answers}
        self.assertEqual(by_id["primary_intent"]["answer"]["choice"], "bugfix")
        self.assertEqual(by_id["requests_test"]["answer"]["noul"], 0.98)
        self.assertEqual(set(by_id["primary_intent"]["answer"]["probabilities"]), {"bugfix", "implementation", "review", "planning", "unknown"})


if __name__ == "__main__":
    unittest.main()
