"""Offline agent observatory: normalized evidence import, classification, reporting.

This is a bounded foundation slice. It provides:

* a versioned normalized JSONL import contract (:mod:`contract`);
* a rebuildable, schema-versioned SQLite projection (:mod:`store`);
* conservative, non-executing command categorization (:mod:`commands`);
* a deterministic report (:mod:`report`);
* a Jev ``/v1/systemone`` request builder and saved-response validator
  (:mod:`jev`);
* shadow orchestration recommendations over a current configured catalog
  (:mod:`policy`), which are advisory and never modify routing.

Live transport and collection are explicit and bounded: nothing crawls a home
directory implicitly, and the collector (:mod:`collector`) reads only the roots it
is given and stops on its kill switch.
Beads and events remain the authoritative record; the SQLite file is only a
derived analytical projection.
"""

__version__ = "0.6.0"

from .annotations import GoldEpisode, GoldSet, load_gold_set
from .changes import (
    BUNDLE_SCHEMA_VERSION,
    OPTIMIZATION_CATEGORIES,
    build_change_bundle,
    classify_categories,
    normalize_change_bundle,
    normalize_change,
)
from .contract import SCHEMA_VERSION, validate_record
from .episodes import Episode, EpisodeConfig, segment_events, segment_store
from .errors import (
    AnnotationError,
    ContractError,
    EpisodeError,
    EvaluationError,
    ImpactError,
    ImportConflictError,
    LabelConflictError,
    ObservatoryError,
    PolicyError,
    RegistryConflictError,
    RegistryError,
    RequestByteCapExceeded,
    RequestError,
    ResponseError,
    SchemaVersionError,
    TaxonomyError,
)
from .evaluation import (
    EvaluationConfig,
    HoldoutSplit,
    Prediction,
    audit_split,
    evaluate_gold_set,
    grouped_temporal_split,
    load_predictions,
    report_json,
)
from .exposure import (
    CommitGraph,
    attach_session_fingerprints,
    build_ledger,
    evaluate_change,
    session_evidence_from_store,
)
from .impact import (
    ATTRIBUTION_GRADES,
    IMPACT_BUNDLE_VERSION,
    ImpactConfig,
    ImpactDataset,
    build_impact_report,
    load_impact_bundle,
    normalize_impact_bundle,
    observed_evidence_from_store,
)
from .jev import (
    REQUEST_BYTE_CAP,
    JevRequest,
    ResponseImport,
    build_request,
    import_response,
    validate_response,
)
from .policy import (
    CANDIDATE_KINDS,
    DECISIONS,
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_GATE_FLAGS,
    ELIGIBILITIES,
    FALLBACK_PATHS,
    POLICY_BUNDLE_VERSION,
    POLICY_CATALOG_SCHEMA_VERSION,
    POLICY_REPORT_KIND,
    POLICY_REPORT_VERSION,
    Candidate,
    ClassificationRecord,
    Feature,
    PolicyCatalog,
    PolicyConfig,
    RecommendationBundle,
    as_of_features,
    build_shadow_report,
    decision_inputs,
    load_catalog,
    load_recommendation_bundle,
    normalize_catalog,
    normalize_recommendation_bundle,
    recommendation_rows,
    recommendations_for_record,
    temporal_audit,
)
from .report import build_report
from .store import ObservatoryStore
from .taxonomy import load_taxonomy

__all__ = [
    "__version__",
    "SCHEMA_VERSION",
    "BUNDLE_SCHEMA_VERSION",
    "REQUEST_BYTE_CAP",
    "OPTIMIZATION_CATEGORIES",
    "ObservatoryStore",
    "JevRequest",
    "ResponseImport",
    "CommitGraph",
    "build_request",
    "import_response",
    "validate_response",
    "build_report",
    "load_taxonomy",
    "validate_record",
    "GoldEpisode",
    "GoldSet",
    "load_gold_set",
    "Episode",
    "EpisodeConfig",
    "segment_events",
    "segment_store",
    "EvaluationConfig",
    "HoldoutSplit",
    "Prediction",
    "audit_split",
    "evaluate_gold_set",
    "grouped_temporal_split",
    "load_predictions",
    "report_json",
    "normalize_change",
    "normalize_change_bundle",
    "build_change_bundle",
    "classify_categories",
    "attach_session_fingerprints",
    "build_ledger",
    "evaluate_change",
    "session_evidence_from_store",
    "ATTRIBUTION_GRADES",
    "IMPACT_BUNDLE_VERSION",
    "ImpactConfig",
    "ImpactDataset",
    "build_impact_report",
    "load_impact_bundle",
    "normalize_impact_bundle",
    "observed_evidence_from_store",
    "POLICY_BUNDLE_VERSION",
    "POLICY_CATALOG_SCHEMA_VERSION",
    "POLICY_REPORT_VERSION",
    "POLICY_REPORT_KIND",
    "CANDIDATE_KINDS",
    "DECISIONS",
    "ELIGIBILITIES",
    "FALLBACK_PATHS",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "DEFAULT_GATE_FLAGS",
    "Candidate",
    "PolicyCatalog",
    "Feature",
    "ClassificationRecord",
    "RecommendationBundle",
    "PolicyConfig",
    "normalize_catalog",
    "load_catalog",
    "normalize_recommendation_bundle",
    "load_recommendation_bundle",
    "as_of_features",
    "temporal_audit",
    "decision_inputs",
    "recommendations_for_record",
    "build_shadow_report",
    "recommendation_rows",
    "ObservatoryError",
    "AnnotationError",
    "ContractError",
    "EpisodeError",
    "EvaluationError",
    "ImpactError",
    "ImportConflictError",
    "LabelConflictError",
    "PolicyError",
    "RegistryError",
    "RegistryConflictError",
    "SchemaVersionError",
    "TaxonomyError",
    "RequestError",
    "RequestByteCapExceeded",
    "ResponseError",
]
