"""Versioned question taxonomy for the Jev request builder.

Question definitions live in JSON so the taxonomy is versioned and inspectable
without touching code. The wire shape follows the TypeSafe ``/v1/systemone``
contract:

* there is **no top-level instructions field**;
* every question has a lowercase ``type`` (``choice`` or ``noul``), an explicit
  ``instructions`` value, and a ``criteria`` map;
* a ``choice`` criteria map is the option-to-rubric mapping (its keys are the
  allowed answers); a ``noul`` criteria map names what yes and no mean;
* there are no per-question ``description`` or ``options`` fields.

The keys of the question map are identifiers for the response, NOT model
instructions. Instructions are carried in each question's ``instructions`` value.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import canonical_hash, canonical_json
from .errors import TaxonomyError

DEFAULT_TAXONOMY_PATH = Path(__file__).parent / "taxonomy" / "jev_taxonomy_v1.json"

CHOICE = "choice"
NOUL = "noul"
SUPPORTED_QUESTION_TYPES = (CHOICE, NOUL)

# Criteria values may be a plain string or the structured object/array the API
# accepts; ``None`` is allowed by the API when an option needs no extra detail.
def _is_criterion_value(value: Any) -> bool:
    return value is None or isinstance(value, (str, dict, list))


@dataclass(frozen=True)
class Question:
    """One taxonomy question in its on-the-wire form."""

    question_id: str
    type: str
    instructions: Any
    criteria: tuple[tuple[str, Any], ...] = ()

    @property
    def criteria_map(self) -> dict[str, Any]:
        return dict(self.criteria)

    @property
    def option_keys(self) -> tuple[str, ...]:
        """Choice option names (criteria keys) in declaration order."""
        return tuple(key for key, _ in self.criteria)


@dataclass(frozen=True)
class Taxonomy:
    """A versioned, immutable question set."""

    taxonomy_version: str
    model: str
    endpoint: str
    questions: tuple[Question, ...]

    def as_request_questions(self) -> dict[str, dict[str, Any]]:
        """Return the question map placed on the wire."""
        result: dict[str, dict[str, Any]] = {}
        for question in self.questions:
            entry: dict[str, Any] = {
                "type": question.type,
                "instructions": question.instructions,
            }
            if question.criteria:
                entry["criteria"] = question.criteria_map
            result[question.question_id] = entry
        return result

    def question_hash(self) -> str:
        """Stable hash over every effective instruction and criterion.

        The hash covers the taxonomy version, model, and the full wire question
        definitions (types, instructions, and criteria), so a change to any
        instruction or rubric changes the classification key.
        """
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
                "questions": self.as_request_questions(),
            }
        )


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise TaxonomyError(message)


def _validate_instructions(value: Any, question_id: str) -> None:
    if isinstance(value, str):
        _require(bool(value.strip()), f"question {question_id!r} instructions must not be blank")
        return
    _require(
        isinstance(value, (dict, list)) and bool(value),
        f"question {question_id!r} instructions must be a non-empty string, object, or array",
    )


def _validate_criteria(raw: Any, question_id: str, *, required: bool) -> tuple[tuple[str, Any], ...]:
    if raw is None:
        _require(not required, f"Choice question {question_id!r} requires a criteria map")
        return ()
    _require(isinstance(raw, dict) and bool(raw), f"question {question_id!r} criteria must be a non-empty object")
    items: list[tuple[str, Any]] = []
    for key, value in raw.items():
        _require(isinstance(key, str) and key, f"question {question_id!r} criteria keys must be non-empty strings")
        _require(
            _is_criterion_value(value),
            f"question {question_id!r} criterion {key!r} must be a string, object, array, or null",
        )
        items.append((key, value))
    _require(
        len({key for key, _ in items}) == len(items),
        f"question {question_id!r} criteria keys must be unique",
    )
    return tuple(items)


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
    _require(isinstance(taxonomy_version, str) and taxonomy_version, "taxonomy_version is required")
    _require(isinstance(model, str) and model, "model is required")
    _require(isinstance(endpoint, str) and endpoint, "endpoint is required")
    _require(
        "instructions" not in raw,
        "taxonomy must not define top-level instructions; put instructions on each question",
    )

    questions: list[Question] = []
    seen: set[str] = set()

    def add_question(entry: Any, expect_type: str | None = None) -> None:
        _require(isinstance(entry, dict), "each question must be an object")
        question_id = entry.get("question_id")
        question_type = entry.get("type")
        _require(isinstance(question_id, str) and question_id, "question_id is required")
        _require(question_id not in seen, f"duplicate question_id {question_id!r}")
        _require(
            question_type in SUPPORTED_QUESTION_TYPES,
            f"question {question_id!r} has unsupported type {question_type!r}; "
            f"supported: {', '.join(SUPPORTED_QUESTION_TYPES)}",
        )
        if expect_type is not None:
            _require(
                question_type == expect_type,
                f"question {question_id!r} must be type {expect_type!r}",
            )
        _require(
            "description" not in entry,
            f"question {question_id!r} must not use a description field; use instructions",
        )
        _require("instructions" in entry, f"question {question_id!r} requires instructions")
        _validate_instructions(entry.get("instructions"), question_id)

        if question_type == CHOICE:
            _require(
                "options" not in entry,
                f"choice question {question_id!r} must express options as criteria keys, not an options list",
            )
            criteria = _validate_criteria(entry.get("criteria"), question_id, required=True)
        else:
            criteria = _validate_criteria(entry.get("criteria"), question_id, required=False)
            for key, _ in criteria:
                _require(
                    key in {"true", "false"},
                    f"Noul question {question_id!r} criteria keys must be 'true' and/or 'false'",
                )
        seen.add(question_id)
        questions.append(Question(question_id, question_type, entry.get("instructions"), criteria))

    add_question(raw.get("primary_intent"), CHOICE)
    primary = questions[0]
    _require(
        "unknown" in primary.option_keys,
        "primary_intent must include an 'unknown' criterion",
    )

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
        questions=tuple(questions),
    )
