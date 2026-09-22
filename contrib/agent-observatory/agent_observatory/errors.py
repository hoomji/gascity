"""Error types for the agent observatory.

Every error carries enough context to point at the exact input that failed
(source path and, where relevant, 1-based line number) so callers never have to
guess which record was bad.
"""

from __future__ import annotations


class ObservatoryError(Exception):
    """Base class for all observatory errors."""


class ContractError(ObservatoryError):
    """A normalized record violates the import contract."""

    def __init__(self, message: str, source_path: str | None = None, line_number: int | None = None):
        self.source_path = source_path
        self.line_number = line_number
        location = ""
        if source_path is not None and line_number is not None:
            location = f"{source_path}:{line_number}: "
        elif source_path is not None:
            location = f"{source_path}: "
        super().__init__(location + message)


class ImportConflictError(ObservatoryError):
    """An existing event id would be silently overwritten with different content."""


class SchemaVersionError(ObservatoryError):
    """The SQLite schema version is missing, unknown, or incompatible."""


class TaxonomyError(ObservatoryError):
    """The question taxonomy is missing or malformed."""


class RequestError(ObservatoryError):
    """A Jev request could not be built."""


class RequestByteCapExceeded(RequestError):
    """The serialized request exceeds the conservative UTF-8 byte cap."""

    def __init__(self, actual_bytes: int, cap_bytes: int):
        self.actual_bytes = actual_bytes
        self.cap_bytes = cap_bytes
        super().__init__(
            f"serialized request is {actual_bytes} UTF-8 bytes, exceeding the "
            f"{cap_bytes}-byte safety cap (byte cap is not tokenizer proof)"
        )


class ResponseError(ObservatoryError):
    """A saved Jev response is invalid or does not match its request."""


class LabelConflictError(ObservatoryError):
    """A classification already exists for this subject and would be overwritten."""
