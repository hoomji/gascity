"""Shared test helpers.

Importing this module also makes the package importable when tests are run with
the documented command::

    python3 -m unittest discover -s contrib/agent-observatory/tests -v

That command puts the tests directory itself on ``sys.path`` (not the package
root), so this module inserts the package root before any test imports
``agent_observatory``.
"""

from __future__ import annotations

import json
import os
import sys

PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PACKAGE_ROOT not in sys.path:
    sys.path.insert(0, PACKAGE_ROOT)

from agent_observatory import load_taxonomy  # noqa: E402  (path bootstrap must run first)

REQUIRED = {
    "schema_version": "1.0",
    "city_id": "city-a",
    "host_id": "host-a",
    "provider": "codex",
    "session_id": "session-1",
    "event_id": "event-1",
    "timestamp": "2026-09-21T10:00:00Z",
    "kind": "command",
}


def make_record(**overrides):
    """Return a valid normalized record with *overrides* applied."""
    record = dict(REQUIRED)
    record.update(overrides)
    return record


def write_jsonl(path, records):
    """Write records as JSONL (including a trailing newline)."""
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return str(path)


def questions_from(request_body):
    return request_body["questions"]


def valid_response(request_body, *, model=None, usage=None, choice_value=None, noul_value=0.25):
    """Build a fully valid saved ``/v1/systemone`` response for *request_body*.

    The wire shape is the real one: lowercase ``type`` values and
    ``choice``/``noul`` answer fields (never ``value``), with the Choice option
    set taken from the question's ``criteria`` keys.
    """
    answers = {}
    for question_id, question in request_body["questions"].items():
        if question["type"] == "choice":
            options = list(question["criteria"])
            chosen = choice_value if choice_value in options else options[0]
            probabilities = {option: 0.0 for option in options}
            probabilities[chosen] = 1.0
            answers[question_id] = {
                "type": "choice",
                "choice": chosen,
                "confidence": 0.9,
                "probabilities": probabilities,
            }
        else:
            answers[question_id] = {"type": "noul", "noul": noul_value}
    return {
        "model": model if model is not None else request_body["model"],
        "usage": usage if usage is not None else {"input_tokens": 12, "output_tokens": 7},
        "answers": answers,
    }


__all__ = ["make_record", "write_jsonl", "valid_response", "questions_from", "load_taxonomy", "PACKAGE_ROOT"]
