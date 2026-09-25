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
FRAMEWORK_FILTER_VERSION = "1.1.0"

# A leading ``[city] <identity> • <ISO timestamp>`` line is a Gas City header.
# ``•`` is U+2022. The header alone does NOT make the message framework: a real
# task can be prefixed with it, so it is removed while stripping and the body
# decides. A blockquote marker (``> [city] ...``) is handled too because
# transcripts sometimes quote the injected prompt.
_CITY_ROLE_PROMPT = re.compile(r"^\s*(?:>\s*)?\[city\]\s+\S+\s+•[^\n]*\n?", re.UNICODE)
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

# Markers of the harness body itself. These appear in an injected role prompt or
# skill payload; a genuine task that merely quotes one is kept because it has
# independent prose left after the marker is removed (see ``_is_harness_payload``).
_STRONG_FRAMEWORK_MARKERS = (
    "# GC Role Worker",
    "# Dolt Dog Context",
    "A skill is a reusable set of task-specific instructions",
    "The following skills are available in this session",
)
# Phrases that occur in the harness but can also occur inside a real task
# ("run gc hook --claim and report what it returns"), so they never mark a
# message as framework on their own. They are only subtracted when computing the
# residual of a message that already carries a strong marker.
_WEAK_FRAMEWORK_PHRASES = (
    "GC Role Worker",
    "Startup Claim Protocol",
    "gc hook --claim",
)
# Longest/most specific markers first so removing one does not strand a prefix.
_FRAMEWORK_PHRASES = _STRONG_FRAMEWORK_MARKERS + _WEAK_FRAMEWORK_PHRASES
# A framework prompt is normally a whole message; its marker sits in the head.
_FRAMEWORK_HEAD_BYTES = 2048
# Below this many remaining characters there is no independent task left.
_FRAMEWORK_RESIDUAL_BYTES = 64


def _remove_embedded_blocks(text: str) -> str:
    for pattern in (_REMINDER_BLOCK, _AVAILABLE_SKILLS, _COMMAND_BLOCK):
        text = pattern.sub(" ", text)
    return text


def _strip_framework_markers(text: str) -> str:
    residual = text
    for phrase in _FRAMEWORK_PHRASES:
        if phrase in residual:
            residual = residual.replace(phrase, " ")
    return residual.strip()


def _is_harness_payload(cleaned: str) -> bool:
    """True only when *cleaned* is the harness body itself, not real prose.

    *cleaned* has already had embedded blocks removed. A leading ``[city]``
    header is removed because it can prefix a genuine task; the decision is made
    on what follows it. A message is the harness payload when nothing task-like
    survives, or when it carries a strong harness marker and no independent prose
    remains once the markers are removed. This is the single decision shared by
    :func:`is_framework_text` and :func:`strip_framework_text`, so the two can
    never disagree (including for messages above any size threshold).
    """

    body = _CITY_ROLE_PROMPT.sub("", cleaned).strip()
    if not body:
        return True
    head = body[: _FRAMEWORK_HEAD_BYTES]
    if not any(marker in head for marker in _STRONG_FRAMEWORK_MARKERS):
        return False
    return len(_strip_framework_markers(body)) < _FRAMEWORK_RESIDUAL_BYTES


def is_framework_text(text: str) -> bool:
    """Return True when *text* is an injected framework payload, not task text."""

    if not isinstance(text, str) or not text.strip():
        return True
    if _RUNTIME_CONTEXT.match(text) or _SKILL_DIRECTORY.match(text):
        return True
    return _is_harness_payload(_remove_embedded_blocks(text))


def strip_framework_text(text: str) -> str:
    """Return *text* with injected framework blocks removed.

    A whole framework message returns ``""``. A real message that merely carries
    an embedded reminder keeps its real prose. A real task prefixed with a
    ``[city]`` header keeps its prose with the header line removed.
    """

    if not isinstance(text, str) or not text.strip():
        return ""
    if _RUNTIME_CONTEXT.match(text) or _SKILL_DIRECTORY.match(text):
        return ""
    cleaned = _remove_embedded_blocks(text)
    if _is_harness_payload(cleaned):
        return ""
    cleaned = _CITY_ROLE_PROMPT.sub("", cleaned)
    return re.sub(r"[ \t]+\n", "\n", cleaned).strip()
