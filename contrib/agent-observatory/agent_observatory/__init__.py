"""Offline agent observatory: normalized evidence import, classification, reporting.

This is a bounded foundation slice. It provides:

* a versioned normalized JSONL import contract (:mod:`contract`);
* a rebuildable, schema-versioned SQLite projection (:mod:`store`);
* conservative, non-executing command categorization (:mod:`commands`);
* a deterministic report (:mod:`report`);
* a Jev ``/v1/systemone`` request builder and saved-response validator
  (:mod:`jev`).

There is no network transport, no live collector, and no home-directory crawling.
Beads and events remain the authoritative record; the SQLite file is only a
derived analytical projection.
"""

__version__ = "0.1.0"

from .contract import SCHEMA_VERSION, validate_record
from .errors import (
    ContractError,
    ImportConflictError,
    LabelConflictError,
    ObservatoryError,
    RequestByteCapExceeded,
    RequestError,
    ResponseError,
    SchemaVersionError,
    TaxonomyError,
)
from .jev import (
    REQUEST_BYTE_CAP,
    JevRequest,
    ResponseImport,
    build_request,
    import_response,
    validate_response,
)
from .report import build_report
from .store import ObservatoryStore
from .taxonomy import load_taxonomy

__all__ = [
    "__version__",
    "SCHEMA_VERSION",
    "REQUEST_BYTE_CAP",
    "ObservatoryStore",
    "JevRequest",
    "ResponseImport",
    "build_request",
    "import_response",
    "validate_response",
    "build_report",
    "load_taxonomy",
    "validate_record",
    "ObservatoryError",
    "ContractError",
    "ImportConflictError",
    "LabelConflictError",
    "SchemaVersionError",
    "TaxonomyError",
    "RequestError",
    "RequestByteCapExceeded",
    "ResponseError",
]
