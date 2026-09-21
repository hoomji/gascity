"""Versioned question taxonomy for the Jev request builder.

Question definitions live in JSON so the taxonomy is versioned and inspectable
without touching code. The keys of the question map are identifiers for the
response, NOT model instructions -- instructions are carried explicitly in the
request's ``instructions`` field.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import canonical_hash, canonical_json
from .errors import TaxonomyError

DEFAULT_TAXONOMY_PATH = Path(__file__).parent / "taxonomy" / "jev_taxonomy_v1.json"

CHOICE = "Choice"
NOUL = "Noul"
SUPPORTED_QUESTION_TYPES = (CHOICE, NOUL)


@dataclass(frozen=True)
class Question:
    """One taxonomy question."""

    question_id: str
    type: str
    description: str
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class Taxonomy:
    """A versioned, immutable question set."""

    taxonomy_version: str
    model: str
    endpoint: str
    instructions: str
    questions: tuple[Question, ...]

    def as_request_questions(self) -> dict[str, dict[str, Any]]:
        """Return the question map placed on the wire."""
        result: dict[str, dict[str, Any]] = {}
        for question in self.questions:
            entry: dict[str, Any] = {"type": question.type, "description": question.description}
            if question.type == CHOICE:
                entry["options"] = list(question.options)
            result[question.question_id] = entry
        return result

    def question_hash(self) -> str:
        """Stable hash of the taxonomy identity and question definitions."""
        return canonical_hash(
            {
                "taxonomy_version": self.taxonomy_version,
                "model": self.model,
                "questions": self.as_request_questions(),
            }
        )

    def by_id(self) -> dict[str, Question]:
        return {question.question_id: question for question in self.questions}

    def to_json(self) -> str:
        return canonical_json(
            {
                "taxonomy_version": self.taxonomy_version,
                "model": self.model,
                "endpoint": self.endpoint,
                "instructions": self.instructions,
                "questions": self.as_request_questions(),
            }
        )


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise TaxonomyError(message)


def load_taxonomy(path: str | Path | None = None) -> Taxonomy:
    """Load and validate a taxonomy JSON file."""
    taxonomy_path = Path(path) if path is not None else DEFAULT_TAXONOMY_PATH
    try:
        raw = json.loads(taxonomy_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TaxonomyError(f"cannot read taxonomy {taxonomy_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise TaxonomyError(f"taxonomy {taxonomy_path} is not valid JSON: {exc}") from exc

    _require(isinstance(raw, dict), "taxonomy must be a JSON object")
    taxonomy_version = raw.get("taxonomy_version")
    model = raw.get("model")
    endpoint = raw.get("endpoint")
    instructions = raw.get("instructions")
    _require(isinstance(taxonomy_version, str) and taxonomy_version, "taxonomy_version is required")
    _require(isinstance(model, str) and model, "model is required")
    _require(isinstance(endpoint, str) and endpoint, "endpoint is required")
    _require(isinstance(instructions, str) and instructions, "instructions are required")

    questions: list[Question] = []
    seen: set[str] = set()

    def add_question(entry: Any, expect_type: str | None = None) -> None:
        _require(isinstance(entry, dict), "each question must be an object")
        question_id = entry.get("question_id")
        question_type = entry.get("type")
        description = entry.get("description", "")
        _require(isinstance(question_id, str) and question_id, "question_id is required")
        _require(question_id not in seen, f"duplicate question_id {question_id!r}")
        _require(
            question_type in SUPPORTED_QUESTION_TYPES,
            f"question {question_id!r} has unsupported type {question_type!r}",
        )
        if expect_type is not None:
            _require(
                question_type == expect_type,
                f"question {question_id!r} must be type {expect_type}",
            )
        _require(isinstance(description, str), f"question {question_id!r} description must be a string")
        options: tuple[str, ...] = ()
        if question_type == CHOICE:
            raw_options = entry.get("options")
            _require(
                isinstance(raw_options, list) and raw_options,
                f"Choice question {question_id!r} requires a non-empty options list",
            )
            _require(
                all(isinstance(option, str) and option for option in raw_options),
                f"Choice question {question_id!r} options must be non-empty strings",
            )
            _require(
                len(set(raw_options)) == len(raw_options),
                f"Choice question {question_id!r} options must be unique",
            )
            options = tuple(raw_options)
        else:
            _require(
                "options" not in entry,
                f"Noul question {question_id!r} must not declare options",
            )
        seen.add(question_id)
        questions.append(Question(question_id, question_type, description, options))

    add_question(raw.get("primary_intent"), CHOICE)
    primary_options = questions[0].options
    _require("unknown" in primary_options, "primary_intent must include an 'unknown' option")

    if "scope" in raw:
        add_question(raw.get("scope"), CHOICE)

    overlapping = raw.get("overlapping_labels", [])
    _require(isinstance(overlapping, list), "overlapping_labels must be a list")
    for entry in overlapping:
        add_question(entry, NOUL)

    return Taxonomy(
        taxonomy_version=taxonomy_version,
        model=model,
        endpoint=endpoint,
        instructions=instructions,
        questions=tuple(questions),
    )
