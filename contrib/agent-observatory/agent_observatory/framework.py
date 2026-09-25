"""Strip Gas City framework payloads out of transcript text.

The adapters store a provider transcript faithfully. In a Gas City session the
first ``role=user`` record is often **not** a task at all: it is the injected
role prompt (``[city] <agent> • <timestamp>``), a runtime-context snapshot, a
``<system-reminder>`` wake/deferred-reminder, or a skill payload. Those records
are user-role messages but they carry no task provenance, so sending them to a
classifier makes ``primary_intent`` look like the *harness* rather than the work.

This module removes that harness text before a transcript state is built. It is
deliberately conservative and content-based (the projection has no ``role``
column, and Claude/Codex/DSH all flatten injected and human text onto the same
``message`` kind):

* a whole message that *starts* as a framework payload is dropped;
* embedded ``<system-reminder>`` / ``<available_skills>`` / ``<command-*>``
  blocks inside an otherwise real message are excised, keeping the real text;
* an empty result means "nothing task-relevant here" and the excerpt is dropped.

The filter is versioned because changing it changes the classification subject.
"""

from __future__ import annotations

import re

# Bump when the markers or the extraction change: the version is recorded on
# every text-mode classification state so a stored label can be traced back to
# the filter that produced its subject.
FRAMEWORK_FILTER_VERSION = "1.0.0"

# Whole-message framework payloads. ``•`` is U+2022, the separator Gas City uses
# in ``[city] <identity> • <ISO timestamp>``.
_CITY_ROLE_PROMPT = re.compile(r"^\s*\[city\]\s+\S+\s+•", re.UNICODE)
_RUNTIME_CONTEXT = re.compile(
    r"^\s*Current runtime context\.\s*This snapshot supersedes", re.IGNORECASE
)
_SKILL_DIRECTORY = re.compile(r"^\s*Base directory for this skill:", re.IGNORECASE)

# Embedded blocks removed while keeping the surrounding message.
_REMINDER_BLOCK = re.compile(r"<system-reminder\b.*?</system-reminder>", re.DOTALL | re.IGNORECASE)
_AVAILABLE_SKILLS = re.compile(r"<available_skills\b.*?</available_skills>", re.DOTALL | re.IGNORECASE)
_COMMAND_BLOCK = re.compile(
    r"<(command-name|command-message|command-args)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE
)

# Phrases that only appear inside an injected role prompt or skill payload. A
# message whose *prefix* is one of these and whose remaining body is dominated
# by the marker is framework, not task text.
_FRAMEWORK_PHRASES = (
    "# GC Role Worker",
    "GC Role Worker",
    "Startup Claim Protocol",
    "gc hook --claim",
    "# Dolt Dog Context",
    "A skill is a reusable set of task-specific instructions",
    "The following skills are available in this session",
)
# A framework prompt is normally a whole message; if a real prompt quotes one of
# these phrases we only drop it when the marker is in the head and the message is
# short enough that there is no independent task left.
_FRAMEWORK_HEAD_BYTES = 2048
_FRAMEWORK_MAX_BYTES = 32 * 1024


def _remove_embedded_blocks(text: str) -> str:
    for pattern in (_REMINDER_BLOCK, _AVAILABLE_SKILLS, _COMMAND_BLOCK):
        text = pattern.sub(" ", text)
    return text


def is_framework_text(text: str) -> bool:
    """Return True when *text* is an injected framework payload, not task text."""

    if not isinstance(text, str) or not text.strip():
        return True
    head = text[: _FRAMEWORK_HEAD_BYTES]
    if _CITY_ROLE_PROMPT.match(text) or _RUNTIME_CONTEXT.match(text) or _SKILL_DIRECTORY.match(text):
        return True
    remainder = _remove_embedded_blocks(text).strip()
    if not remainder:
        # A record whose whole body was ``<system-reminder>``/skill blocks.
        return True
    if any(phrase in head for phrase in _FRAMEWORK_PHRASES):
        # ``[city]`` role prompts always start the marker; a bare phrase is
        # framework only when there is no substantial non-framework remainder.
        residual = remainder
        for phrase in _FRAMEWORK_PHRASES:
            if phrase in residual:
                residual = residual.replace(phrase, "").strip()
        return len(residual) < 64
    return False


def strip_framework_text(text: str) -> str:
    """Return *text* with injected framework blocks removed.

    A whole framework message returns ``""``. A real message that merely carries
    an embedded reminder keeps its real prose.
    """

    if not isinstance(text, str) or not text.strip():
        return ""
    if _CITY_ROLE_PROMPT.match(text) or _RUNTIME_CONTEXT.match(text) or _SKILL_DIRECTORY.match(text):
        return ""
    cleaned = _remove_embedded_blocks(text)
    if len(text) <= _FRAMEWORK_MAX_BYTES and any(
        phrase in text[: _FRAMEWORK_HEAD_BYTES] for phrase in _FRAMEWORK_PHRASES
    ):
        # A role/skill payload with only whitespace outside the marker is not a
        # task. Comparing the cleaned body to the marker itself keeps a genuine
        # task that happens to mention the harness (for example a review of the
        # role prompt) because that body is far longer than the marker.
        residual = cleaned.strip()
        for phrase in _FRAMEWORK_PHRASES:
            if phrase in residual:
                residual = residual.replace(phrase, "").strip()
        if len(residual) < 64:
            return ""
    return re.sub(r"[ \t]+\n", "\n", cleaned).strip()
