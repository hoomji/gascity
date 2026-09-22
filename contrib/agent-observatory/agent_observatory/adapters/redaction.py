"""Credential redaction and bounded tool-output handling.

Native adapters read untrusted provider transcripts. Two rules are enforced
here before any text reaches the normalized contract:

* anything that looks like a credential (bearer tokens, ``key=value`` secrets,
  well-known provider token prefixes) is replaced with ``[REDACTED]``;
* raw tool output larger than :data:`MAX_TOOL_OUTPUT_BYTES` is never exported.
  The digest and byte length are recorded instead so the evidence stays
  verifiable without shipping the payload.

Redaction is best-effort pattern matching over text, not a guarantee that no
secret can ever survive an unusual encoding. It is a defense-in-depth layer on
top of the rule that raw transcripts are never uploaded wholesale.
"""

from __future__ import annotations

import re

from ..canonical import sha256_bytes, sha256_text

# Raw tool output is capped at 2 KB per event. Larger payloads become a digest
# marker so the text column can never carry an unbounded transcript blob.
MAX_TOOL_OUTPUT_BYTES = 2048

REDACTED = "[REDACTED]"

# ``key=value`` / ``key: value`` assignments for secret-looking keys. Values may
# be quoted; only the value is replaced. A JSON string is commonly observed with
# its quotes escaped (``\"password\": \"hunter2\"``), so an optional backslash is
# accepted before every quote; the value pattern stops at the next (escaped)
# quote or separator so the secret is dropped without eating the delimiters.
_QUOTE = r"""(?:\\?["'])"""
_VALUE = r"""(?!Bearer\b)[^\\\s"',;]+"""
_SECRET_KEY = (
    r"(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|refresh[_-]?token|"
    r"secret|client[_-]?secret|password|passwd|private[_-]?key|authorization|token)"
)
_ASSIGNMENT_RE = re.compile(
    rf"(?i)({_QUOTE}?\b{_SECRET_KEY}\b{_QUOTE}?\s*[:=]\s*)({_QUOTE}?)({_VALUE})({_QUOTE}?)",
)
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/=-]{8,})")
# Well-known token shapes that are secret regardless of surrounding key names.
_TOKEN_SHAPE_RE = re.compile(
    r"\b(?:"
    r"sk-[A-Za-z0-9_-]{12,}"
    r"|gh[pousr]_[A-Za-z0-9]{16,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_-]{20,}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r")\b"
)


def redact_text(value: str) -> str:
    """Return *value* with credential-looking substrings replaced.

    The function is intentionally conservative about what it redacts: only
    values attached to a secret-looking key, explicit bearer tokens, and the
    well-known token prefixes above are touched, so ordinary prose and hashes
    survive unchanged.
    """

    if not value:
        return value

    def _assignment(match: re.Match[str]) -> str:
        prefix, quote, _secret, closing = match.group(1), match.group(2), match.group(3), match.group(4)
        return f"{prefix}{quote}{REDACTED}{closing}"

    # Bearer/token shapes run first so an ``Authorization: Bearer <secret>``
    # header cannot leave the secret behind when the assignment rule consumes
    # only the ``Bearer`` word.
    redacted = _BEARER_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", value)
    redacted = _TOKEN_SHAPE_RE.sub(REDACTED, redacted)
    redacted = _ASSIGNMENT_RE.sub(_assignment, redacted)
    return redacted


def elide_large_text(value: str, *, limit: int = MAX_TOOL_OUTPUT_BYTES) -> str:
    """Redact *value* and elide it when its UTF-8 form exceeds *limit* bytes.

    An oversized payload is replaced by a marker carrying its sha256 digest and
    byte length, so callers can still prove what was observed without exporting
    the raw bytes.
    """

    redacted = redact_text(value)
    data = redacted.encode("utf-8")
    if len(data) <= limit:
        return redacted
    return f"[elided: {len(data)} bytes sha256={sha256_bytes(data)}]"


def redact_and_bound(value: str, *, limit: int = MAX_TOOL_OUTPUT_BYTES) -> str:
    """Return *value* redacted, and bounded to *limit* UTF-8 bytes.

    Unlike :func:`elide_large_text` this truncates on a character boundary and
    appends a digest of the dropped tail, which is useful for messages where a
    readable prefix matters. Tool output uses :func:`elide_large_text`, which is
    all-or-nothing.
    """

    redacted = redact_text(value)
    data = redacted.encode("utf-8")
    if len(data) <= limit:
        return redacted
    head = data[:limit].decode("utf-8", errors="ignore")
    return f"{head}\n[truncated: {len(data)} bytes sha256={sha256_text(redacted)}]"
