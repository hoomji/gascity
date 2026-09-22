"""Optimization change registry (M5).

Requirement R3 and ``measurement.md`` ask for a registry of every screened PR and
non-PR intervention (config, model, runtime, prompt, toolchain, pack, host),
with the evidence needed to join sessions to the revision they actually used.
This module implements the *offline* half:

* a strict, versioned JSON bundle contract (``BUNDLE_SCHEMA_VERSION``) for
  explicitly supplied repository/PR/fleet evidence -- there is no live PR
  crawler and no network access here;
* a deterministic, conservative optimization screen over changed paths and
  caller-supplied text. A category is only assigned when the path/keyword
  evidence is present, and the evidence string is retained so a reviewer can see
  *why* a change was screened in. Conversely, a missing category is never
  reported as ``non_optimization`` without positive evidence: it stays
  ``unknown`` and remains in the screening denominator;
* immutable, content-hashed change and activation records. One PR may contain
  several interventions and one drop may carry several activations.

The companion :mod:`agent_observatory.exposure` module performs the evidence
join; nothing here decides whether a change was exposed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .canonical import canonical_hash
from .contract import normalize_timestamp
from .errors import RegistryConflictError, RegistryError

# Versions are stored with every record so a report can be reproduced after the
# screen or the wire contract changes.
CHANGE_REGISTRY_VERSION = "1.0"
CHANGE_TAXONOMY_VERSION = "1.0"
SCREENING_VERSION = "1.0"
BUNDLE_SCHEMA_VERSION = "1.0"

CHANGE_KINDS = (
    "pr",
    "config",
    "model",
    "runtime",
    "prompt",
    "toolchain",
    "pack",
    "host",
)
NON_PR_KINDS = tuple(kind for kind in CHANGE_KINDS if kind != "pr")

SCREENING_STATUSES = ("optimization", "non_optimization", "unknown")

# Categories come from implementation-plan.md ("Optimization accounting"): a PR
# is screened against every one of them. Several may apply to one change.
OPTIMIZATION_CATEGORIES = (
    "package_resolution",
    "dependency_footprint",
    "test_selection",
    "test_fixtures_parallelism",
    "lint_typecheck",
    "build_cache_bundle",
    "ci_runner_cache",
    "worktree_lifecycle",
    "agent_runtime",
    "host_io_network",
    "telemetry_overhead",
)

# Activation mechanisms and fingerprint types. A fingerprint is the immutable
# artifact/config/model identity an exposure join matches against observed
# session evidence.
ACTIVATION_MECHANISMS = (
    "merge",
    "deploy",
    "config_toggle",
    "model_switch",
    "runtime_switch",
    "prompt_switch",
    "package_upgrade",
    "toolchain_upgrade",
    "host_tuning",
)
FINGERPRINT_TYPES = (
    "commit_sha",
    "artifact_digest",
    "config_digest",
    "model",
    "toolchain",
    "package",
    "host",
)

# Labels that positively identify a non-optimization change. Only used when the
# change has no optimization category; uncertain changes stay ``unknown`` so
# they remain visible in the screening denominator.
NON_OPTIMIZATION_LABELS = frozenset(
    {"documentation", "docs", "bug", "bugfix", "feature", "enhancement", "chore"}
)

# Non-PR intervention kinds are interventions by construction, so each maps to a
# default category when the caller supplies no path/keyword classification. This
# is what keeps config/model/toolchain changes from being dropped as "unknown"
# merely because they do not touch a source path. A reviewed ``classification``
# or ``category_hint`` still overrides the default.
_KIND_DEFAULT_CATEGORIES = {
    "config": ("agent_runtime",),
    "model": ("agent_runtime",),
    "runtime": ("agent_runtime",),
    "prompt": ("agent_runtime",),
    "toolchain": ("dependency_footprint",),
    "pack": ("dependency_footprint",),
    "host": ("host_io_network",),
}

_ALLOWED_BUNDLE_KEYS = frozenset(
    {
        "schema_version",
        "generated_by",
        "transaction",
        "changes",
        "activations",
        "commit_graph",
        "session_fingerprints",
    }
)
_ALLOWED_CHANGE_KEYS = frozenset(
    {
        "repo",
        "kind",
        "pr",
        "source_ref",
        "title",
        "body",
        "labels",
        "author",
        "base_sha",
        "head_sha",
        "merge_sha",
        "merged_at",
        "changed_paths",
        "hypothesis",
        "rollback",
        "artifact_digest",
        "category_hint",
        "classification",
        "non_optimization_evidence",
        "baseline",
        "prices",
    }
)
_ALLOWED_ACTIVATION_KEYS = frozenset(
    {
        "change_ref",
        "change_id",
        "mechanism",
        "target",
        "activated_at",
        "deactivated_at",
        "pending",
        "fingerprint",
        "evidence",
    }
)
_ALLOWED_GRAPH_KEYS = frozenset({"commits"})
_ALLOWED_COMMIT_KEYS = frozenset({"sha", "parents"})
_ALLOWED_FINGERPRINT_KEYS = frozenset({"type", "value"})
_ALLOWED_BASELINE_KEYS = frozenset({"metric", "value", "unit", "source", "window"})
_ALLOWED_SESSION_FINGERPRINT_KEYS = frozenset(
    {"session", "type", "value", "observed_at", "evidence"}
)


@dataclass(frozen=True)
class _CategoryRule:
    category: str
    paths: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()


# Path patterns are matched case-insensitively against changed paths. When a
# rule declares both paths and keywords, *both* must be present: merely touching
# a test or workflow file is not evidence of a test/CI optimization. Path-only
# rules are reserved for files whose purpose is the optimization itself (lock
# files, linter configs, the worktree subsystem).
_CATEGORY_RULES: tuple[_CategoryRule, ...] = (
    _CategoryRule(
        "package_resolution",
        paths=(
            r"(^|/)(go\.sum|package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|"
            r"pipfile\.lock|cargo\.lock|composer\.lock|gemfile\.lock|deps\.env)$",
            r"(^|/)(renovate\.json|dependabot\.ya?ml)$",
        ),
    ),
    _CategoryRule(
        "dependency_footprint",
        paths=(
            r"(^|/)(go\.mod|package\.json|requirements[^/]*\.txt|pipfile|cargo\.toml|"
            r"composer\.json|gemfile|pyproject\.toml)$",
            r"(^|/)(vendor|node_modules)/",
        ),
    ),
    _CategoryRule(
        "test_selection",
        paths=(
            r"(_test\.go|_test\.py|\.test\.(ts|tsx|js|jsx)|\.spec\.(ts|tsx|js|jsx))$",
            r"(^|/)(test|tests|testing|spec|specs)/",
        ),
        keywords=(
            "test selection",
            "test sharding",
            "shard",
            "parallel test",
            "test parallelism",
            "test runtime",
            "test time",
            "run fewer tests",
            "affected tests",
            "test impact",
            "skip test",
            "retry test",
            "flaky",
            "flakiness",
            "select tests",
        ),
    ),
    _CategoryRule(
        "test_fixtures_parallelism",
        paths=(r"(^|/)(testdata|fixtures?)/",),
        keywords=("fixture", "parallel", "shard", "flaky", "golden"),
    ),
    _CategoryRule(
        "lint_typecheck",
        paths=(
            r"(^|/)(\.golangci\.ya?ml|ruff\.toml|\.flake8|mypy\.ini|pyrightconfig\.json|"
            r"staticcheck\.conf|\.eslintrc[^/]*|tsconfig[^/]*\.json|\.pre-commit-config\.yaml)$",
        ),
    ),
    _CategoryRule(
        "build_cache_bundle",
        paths=(
            r"(^|/)(makefile|gnumakefile|dockerfile[^/]*|\.goreleaser\.ya?ml|\.dockerignore|"
            r"build|build\.bazel|workspace|workspace\.bazel|meson\.build|cmakelists\.txt)$",
            r"(^|/)build/",
        ),
        keywords=(
            "cache",
            "bundle",
            "build time",
            "build speed",
            "incremental",
            "parallel build",
            "ccache",
            "sccache",
        ),
    ),
    _CategoryRule(
        "ci_runner_cache",
        paths=(
            r"(^|/)\.github/workflows/[^/]+\.ya?ml$",
            r"(^|/)\.github/actions/",
            r"(^|/)\.gitlab-ci\.ya?ml$",
            r"(^|/)(ci|\.ci)/",
        ),
        keywords=(
            "runner",
            "concurrency",
            "queue",
            "artifact",
            "cache",
            "matrix",
            "timeout",
            "ci time",
            "ci minutes",
        ),
    ),
    _CategoryRule(
        "worktree_lifecycle",
        paths=(r"worktree", r"(^|/)internal/fleet/"),
    ),
    _CategoryRule(
        "agent_runtime",
        paths=(
            r"(^|/)(schemas|roles|formulas|prompts|skills)/",
            r"(^|/)internal/(agent|prompt|formula|skill|dispatch|context)/",
            r"(^|/)agent_observatory/(adapters|taxonomy)/",
        ),
        keywords=(
            "model",
            "effort",
            "prompt",
            "skill",
            "context bundle",
            "formula",
            "dispatch",
            "provider",
            "temperature",
            "reasoning",
            "token",
        ),
    ),
    _CategoryRule(
        "host_io_network",
        keywords=(
            "host tuning",
            "disk pressure",
            "network latency",
            "i/o throughput",
            "io throughput",
            "sysctl",
            "ulimit",
            "cpu pinning",
            "memory pressure",
            "io scheduler",
        ),
    ),
    _CategoryRule(
        "telemetry_overhead",
        paths=(r"(^|/)internal/telemetry/", r"(^|/)(telemetry|metrics|observability)/"),
        keywords=("overhead", "sampling", "cardinality", "volume", "cost"),
    ),
)

_COMPILED_RULES: tuple[tuple[_CategoryRule, tuple[re.Pattern[str], ...]], ...] = tuple(
    (rule, tuple(re.compile(pattern, re.IGNORECASE) for pattern in rule.paths))
    for rule in _CATEGORY_RULES
)


# -- small validation helpers ----------------------------------------------


def _require_mapping(raw: Any, what: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RegistryError(f"{what} must be a JSON object")
    return raw


def _reject_unknown_keys(raw: dict[str, Any], allowed: frozenset[str], what: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise RegistryError(f"{what} has unknown key(s): {', '.join(unknown)}")


def _require_str(raw: dict[str, Any], field: str, what: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value:
        raise RegistryError(f"{what} field {field!r} must be a non-empty string")
    return value


def _optional_str(raw: dict[str, Any], field: str, what: str) -> str | None:
    value = raw.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RegistryError(f"{what} field {field!r} must be a string or null")
    return value or None


def _optional_str_list(raw: dict[str, Any], field: str, what: str) -> tuple[str, ...]:
    value = raw.get(field)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RegistryError(f"{what} field {field!r} must be a list of strings")
    return tuple(value)


def _optional_timestamp(raw: dict[str, Any], field: str, what: str) -> str | None:
    value = raw.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RegistryError(f"{what} field {field!r} must be a timestamp string or null")
    try:
        return normalize_timestamp(value)
    except Exception as exc:  # ContractError carries the reason
        raise RegistryError(f"{what} field {field!r} is not a valid timestamp: {exc}") from exc


def _normalize_fingerprint(raw: Any, what: str) -> dict[str, str] | None:
    if raw is None:
        return None
    fingerprint = _require_mapping(raw, what)
    _reject_unknown_keys(fingerprint, _ALLOWED_FINGERPRINT_KEYS, what)
    kind = _require_str(fingerprint, "type", what)
    if kind not in FINGERPRINT_TYPES:
        raise RegistryError(
            f"{what} fingerprint type {kind!r} is not one of {', '.join(FINGERPRINT_TYPES)}"
        )
    value = _require_str(fingerprint, "value", what)
    return {"type": kind, "value": value}


# -- screening --------------------------------------------------------------


def classify_categories(
    *,
    changed_paths: Sequence[str],
    title: str = "",
    body: str = "",
    labels: Sequence[str] = (),
    category_hint: Sequence[str] = (),
) -> tuple[list[str], list[str]]:
    """Return ``(categories, evidence)`` for one change.

    Deterministic and conservative. A rule that declares keywords requires a
    matching keyword *and* a matching path; a rule with only paths fires on the
    path alone. Explicit ``category_hint`` entries are reviewed evidence and are
    accepted after validation.
    """
    categories: set[str] = set()
    evidence: list[str] = []

    for hint in category_hint:
        if hint not in OPTIMIZATION_CATEGORIES:
            raise RegistryError(
                f"category_hint {hint!r} is not one of {', '.join(OPTIMIZATION_CATEGORIES)}"
            )
        categories.add(hint)
        evidence.append(f"{hint}:hint")

    lowered_paths = [path.lower() for path in changed_paths]
    text = " ".join([title, body, *labels]).lower()

    for rule, patterns in _COMPILED_RULES:
        if rule.category in categories:
            continue
        path_hit = next(
            (path for path in lowered_paths if any(pattern.search(path) for pattern in patterns)),
            None,
        )
        keyword_hit = next((word for word in rule.keywords if word in text), None)

        if rule.paths and rule.keywords:
            if path_hit is None or keyword_hit is None:
                continue
            evidence.append(f"{rule.category}:path={path_hit},keyword={keyword_hit}")
        elif rule.paths:
            if path_hit is None:
                continue
            evidence.append(f"{rule.category}:path={path_hit}")
        else:
            if keyword_hit is None or not lowered_paths:
                continue
            evidence.append(f"{rule.category}:keyword={keyword_hit}")
        categories.add(rule.category)

    return sorted(categories), evidence


def _screen(
    *,
    kind: str,
    title: str | None,
    body: str | None,
    labels: Sequence[str],
    changed_paths: Sequence[str],
    category_hint: Sequence[str],
    classification: str | None,
    non_optimization_evidence: str | None,
) -> tuple[str, list[str], list[str]]:
    categories, evidence = classify_categories(
        changed_paths=changed_paths,
        title=title or "",
        body=body or "",
        labels=labels,
        category_hint=category_hint,
    )

    if classification is not None:
        if classification not in SCREENING_STATUSES:
            raise RegistryError(
                f"classification {classification!r} is not one of {', '.join(SCREENING_STATUSES)}"
            )
        evidence.insert(0, f"{classification}:explicit")
        return classification, categories, evidence

    for default_category in _KIND_DEFAULT_CATEGORIES.get(kind, ()):
        if default_category not in categories:
            categories.append(default_category)
            evidence.append(f"{default_category}:kind={kind}")
    categories.sort()

    if categories:
        return "optimization", categories, evidence

    hit_labels = sorted(set(labels) & NON_OPTIMIZATION_LABELS)
    if hit_labels:
        evidence.append(f"non_optimization:label={','.join(hit_labels)}")
        return "non_optimization", categories, evidence
    if non_optimization_evidence:
        evidence.append(f"non_optimization:evidence={non_optimization_evidence}")
        return "non_optimization", categories, evidence
    evidence.append("unknown:no_optimization_category")
    return "unknown", categories, evidence


# -- normalization ----------------------------------------------------------


def change_identity(
    *,
    repo: str,
    kind: str,
    pr: int | None = None,
    artifact_digest: str | None = None,
    source_ref: str | None = None,
) -> str:
    """Return the stable identity of a change.

    PRs are identified by repository and number; non-PR interventions by their
    immutable artifact/config digest. The identity never depends on mutable
    metadata (title, labels, branches), so re-syncing cannot duplicate a change.
    """
    if kind == "pr":
        if pr is None and not source_ref:
            raise RegistryError("a pr change needs a 'pr' number or a 'source_ref'")
        return canonical_hash(["change", repo, "pr", str(pr) if pr is not None else source_ref])
    if not artifact_digest:
        raise RegistryError(
            f"a {kind!r} change needs an immutable 'artifact_digest' so its activation can be joined"
        )
    return canonical_hash(["change", repo, kind, artifact_digest, source_ref or ""])


def normalize_change(raw: Any) -> dict[str, Any]:
    """Validate and normalize one change record from a bundle."""
    raw = _require_mapping(raw, "change")
    _reject_unknown_keys(raw, _ALLOWED_CHANGE_KEYS, "change")

    repo = _require_str(raw, "repo", "change")
    kind = _require_str(raw, "kind", "change")
    if kind not in CHANGE_KINDS:
        raise RegistryError(f"change kind {kind!r} is not one of {', '.join(CHANGE_KINDS)}")

    pr = raw.get("pr")
    if pr is not None and (not isinstance(pr, int) or isinstance(pr, bool) or pr <= 0):
        raise RegistryError("change field 'pr' must be a positive integer or null")
    artifact_digest = _optional_str(raw, "artifact_digest", "change")
    source_ref = _optional_str(raw, "source_ref", "change")

    if kind == "pr":
        if artifact_digest is not None:
            raise RegistryError("a pr change must not carry an artifact_digest")
        identity = change_identity(repo=repo, kind=kind, pr=pr, source_ref=source_ref)
    else:
        if pr is not None:
            raise RegistryError(f"a {kind!r} change must not carry a 'pr' number")
        identity = change_identity(
            repo=repo, kind=kind, artifact_digest=artifact_digest, source_ref=source_ref
        )

    base_sha = _optional_str(raw, "base_sha", "change")
    head_sha = _optional_str(raw, "head_sha", "change")
    merge_sha = _optional_str(raw, "merge_sha", "change")
    merged_at = _optional_timestamp(raw, "merged_at", "change")
    title = _optional_str(raw, "title", "change")
    body = _optional_str(raw, "body", "change")
    author = _optional_str(raw, "author", "change")
    hypothesis = _optional_str(raw, "hypothesis", "change")
    rollback = _optional_str(raw, "rollback", "change")
    labels = _optional_str_list(raw, "labels", "change")
    changed_paths = _optional_str_list(raw, "changed_paths", "change")
    category_hint = _optional_str_list(raw, "category_hint", "change")
    classification = _optional_str(raw, "classification", "change")
    non_optimization_evidence = _optional_str(raw, "non_optimization_evidence", "change")

    baseline = raw.get("baseline")
    if baseline is not None:
        baseline = _require_mapping(baseline, "change baseline")
        _reject_unknown_keys(baseline, _ALLOWED_BASELINE_KEYS, "change baseline")
    prices = raw.get("prices")
    if prices is not None:
        prices = _require_mapping(prices, "change prices")

    status, categories, screening_evidence = _screen(
        kind=kind,
        title=title,
        body=body,
        labels=labels,
        changed_paths=changed_paths,
        category_hint=category_hint,
        classification=classification,
        non_optimization_evidence=non_optimization_evidence,
    )

    record: dict[str, Any] = {
        "change_id": identity,
        "repo": repo,
        "kind": kind,
        "pr": pr,
        "source_ref": source_ref,
        "title": title,
        "body": body,
        "labels": list(labels),
        "author": author,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "merge_sha": merge_sha,
        "merged_at": merged_at,
        "changed_paths": list(changed_paths),
        "hypothesis": hypothesis,
        "rollback": rollback,
        "artifact_digest": artifact_digest,
        "classification": status,
        "categories": categories,
        "classification_evidence": screening_evidence,
        "baseline": baseline,
        "prices": prices,
        "screening_version": SCREENING_VERSION,
        "taxonomy_version": CHANGE_TAXONOMY_VERSION,
    }
    record["change_hash"] = canonical_hash(
        {key: value for key, value in record.items() if key != "change_id"}
    )
    return record


def _build_activation(
    *,
    change_id: str,
    mechanism: str,
    target: str | None,
    activated_at: str | None,
    deactivated_at: str | None,
    pending: bool,
    fingerprint: dict[str, str] | None,
    evidence: str | None,
) -> dict[str, Any]:
    if deactivated_at is not None and activated_at is not None and deactivated_at < activated_at:
        raise RegistryError(
            f"activation deactivated_at {deactivated_at!r} precedes activated_at {activated_at!r}"
        )
    record: dict[str, Any] = {
        "change_id": change_id,
        "mechanism": mechanism,
        "target": target,
        "activated_at": activated_at,
        "deactivated_at": deactivated_at,
        "pending": pending,
        "fingerprint": fingerprint,
        "evidence": evidence,
    }
    record["activation_id"] = canonical_hash(
        [
            "activation",
            change_id,
            mechanism,
            target or "",
            activated_at or "",
            deactivated_at or "",
            pending,
            fingerprint or {},
        ]
    )
    record["activation_hash"] = canonical_hash(record)
    return record


def normalize_activation(raw: Any, *, changes_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Validate and normalize one activation record from a bundle."""
    raw = _require_mapping(raw, "activation")
    _reject_unknown_keys(raw, _ALLOWED_ACTIVATION_KEYS, "activation")

    change_id = _optional_str(raw, "change_id", "activation")
    change_ref = raw.get("change_ref")
    if change_id is None and change_ref is not None:
        ref = _require_mapping(change_ref, "activation change_ref")
        repo = _require_str(ref, "repo", "activation change_ref")
        kind = _require_str(ref, "kind", "activation change_ref")
        ref_pr = ref.get("pr")
        ref_digest = _optional_str(ref, "artifact_digest", "activation change_ref")
        change_id = change_identity(
            repo=repo,
            kind=kind,
            pr=ref_pr if isinstance(ref_pr, int) and not isinstance(ref_pr, bool) else None,
            artifact_digest=ref_digest,
            source_ref=_optional_str(ref, "source_ref", "activation change_ref"),
        )
    if change_id is None:
        raise RegistryError("an activation needs 'change_id' or a 'change_ref'")
    if change_id not in changes_by_id:
        raise RegistryError(f"activation references unknown change {change_id}")

    mechanism = _require_str(raw, "mechanism", "activation")
    if mechanism not in ACTIVATION_MECHANISMS:
        raise RegistryError(
            f"activation mechanism {mechanism!r} is not one of {', '.join(ACTIVATION_MECHANISMS)}"
        )
    target = _optional_str(raw, "target", "activation")
    activated_at = _optional_timestamp(raw, "activated_at", "activation")
    deactivated_at = _optional_timestamp(raw, "deactivated_at", "activation")
    pending_raw = raw.get("pending")
    if pending_raw is not None and not isinstance(pending_raw, bool):
        raise RegistryError("activation field 'pending' must be a boolean or null")
    pending = bool(pending_raw) if pending_raw is not None else activated_at is None
    fingerprint = _normalize_fingerprint(raw.get("fingerprint"), "activation")
    evidence = _optional_str(raw, "evidence", "activation")

    return _build_activation(
        change_id=change_id,
        mechanism=mechanism,
        target=target,
        activated_at=activated_at,
        deactivated_at=deactivated_at,
        pending=pending,
        fingerprint=fingerprint,
        evidence=evidence,
    )


def _implicit_merge_activation(change: dict[str, Any]) -> dict[str, Any]:
    """Return the activation implied by a merged PR's merge commit.

    A merged PR is evidence of a merge activation even when the bundle does not
    spell one out. It is recorded explicitly so exposure evaluation never has to
    special-case it, and so the join still requires a matching observed commit
    (a merge alone is not exposure).
    """
    return _build_activation(
        change_id=change["change_id"],
        mechanism="merge",
        target=None,
        activated_at=change.get("merged_at"),
        deactivated_at=None,
        pending=change.get("merged_at") is None and change.get("merge_sha") is None,
        fingerprint=(
            {"type": "commit_sha", "value": change["merge_sha"]}
            if change.get("merge_sha")
            else None
        ),
        evidence="implicit merge activation from merge_sha/merged_at",
    )


def normalize_change_bundle(raw: Any) -> dict[str, Any]:
    """Validate a whole bundle and return normalized changes/activations/evidence."""
    raw = _require_mapping(raw, "bundle")
    _reject_unknown_keys(raw, _ALLOWED_BUNDLE_KEYS, "bundle")

    schema_version = raw.get("schema_version")
    if schema_version != BUNDLE_SCHEMA_VERSION:
        raise RegistryError(
            f"bundle schema_version {schema_version!r} is not supported; "
            f"expected {BUNDLE_SCHEMA_VERSION!r}"
        )

    raw_changes = raw.get("changes", [])
    if not isinstance(raw_changes, list):
        raise RegistryError("bundle field 'changes' must be a list")
    changes = [normalize_change(item) for item in raw_changes]

    seen_ids: set[str] = set()
    changes_by_id: dict[str, dict[str, Any]] = {}
    for change in changes:
        if change["change_id"] in seen_ids:
            raise RegistryError(
                f"bundle contains two changes with the same identity {change['change_id']}"
            )
        seen_ids.add(change["change_id"])
        changes_by_id[change["change_id"]] = change

    raw_activations = raw.get("activations", [])
    if not isinstance(raw_activations, list):
        raise RegistryError("bundle field 'activations' must be a list")
    activations = [
        normalize_activation(item, changes_by_id=changes_by_id) for item in raw_activations
    ]

    # Add the implicit merge activation for any merged PR that lacks one, so the
    # registry is self-sufficient while still requiring observed-commit evidence.
    explicit_merge = {
        activation["change_id"]
        for activation in activations
        if activation["mechanism"] == "merge"
    }
    for change in changes:
        if (
            change["kind"] == "pr"
            and change.get("merge_sha")
            and change["change_id"] not in explicit_merge
        ):
            activations.append(_implicit_merge_activation(change))

    _reject_duplicate_activations(activations)

    graph = _normalize_graph(raw.get("commit_graph"))
    fingerprints = _normalize_session_fingerprints(raw.get("session_fingerprints"))

    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "generated_by": raw.get("generated_by"),
        "transaction": raw.get("transaction"),
        "changes": changes,
        "activations": activations,
        "commit_graph": graph,
        "session_fingerprints": fingerprints,
    }


def _reject_duplicate_activations(activations: list[dict[str, Any]]) -> None:
    seen: dict[str, dict[str, Any]] = {}
    for activation in activations:
        existing = seen.get(activation["activation_id"])
        if existing is not None:
            if existing["activation_hash"] != activation["activation_hash"]:
                raise RegistryConflictError(
                    f"two activations share identity {activation['activation_id']} "
                    "with different content"
                )
            raise RegistryError(
                f"bundle contains a duplicate activation {activation['activation_id']}"
            )
        seen[activation["activation_id"]] = activation


def _normalize_graph(raw: Any) -> dict[str, list[str]]:
    if raw is None:
        return {}
    raw = _require_mapping(raw, "commit_graph")
    _reject_unknown_keys(raw, _ALLOWED_GRAPH_KEYS, "commit_graph")
    commits = raw.get("commits", [])
    if not isinstance(commits, list):
        raise RegistryError("commit_graph field 'commits' must be a list")
    parents: dict[str, list[str]] = {}
    for item in commits:
        entry = _require_mapping(item, "commit_graph commit")
        _reject_unknown_keys(entry, _ALLOWED_COMMIT_KEYS, "commit_graph commit")
        sha = _require_str(entry, "sha", "commit_graph commit")
        raw_parents = entry.get("parents", [])
        if not isinstance(raw_parents, list) or not all(
            isinstance(parent, str) and parent for parent in raw_parents
        ):
            raise RegistryError("commit_graph commit 'parents' must be a list of non-empty strings")
        if sha in parents:
            raise RegistryError(f"commit_graph lists commit {sha!r} twice")
        parents[sha] = list(raw_parents)
    return parents


def _normalize_session_fingerprints(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RegistryError("bundle field 'session_fingerprints' must be a list")
    records: list[dict[str, Any]] = []
    for item in raw:
        entry = _require_mapping(item, "session_fingerprint")
        _reject_unknown_keys(entry, _ALLOWED_SESSION_FINGERPRINT_KEYS, "session_fingerprint")
        session = entry.get("session")
        if (
            not isinstance(session, list)
            or len(session) != 4
            or not all(isinstance(part, str) and part for part in session)
        ):
            raise RegistryError(
                "session_fingerprint 'session' must be a four-element array of non-empty strings"
            )
        kind = _require_str(entry, "type", "session_fingerprint")
        if kind not in FINGERPRINT_TYPES:
            raise RegistryError(
                f"session_fingerprint type {kind!r} is not one of {', '.join(FINGERPRINT_TYPES)}"
            )
        value = _require_str(entry, "value", "session_fingerprint")
        observed_at = _optional_timestamp(entry, "observed_at", "session_fingerprint")
        records.append(
            {
                "session": list(session),
                "type": kind,
                "value": value,
                "observed_at": observed_at,
                "evidence": _optional_str(entry, "evidence", "session_fingerprint"),
            }
        )
    return records


def build_change_bundle(
    *,
    changes: Iterable[dict[str, Any]],
    activations: Iterable[dict[str, Any]] = (),
    commit_graph: dict[str, list[str]] | None = None,
    session_fingerprints: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Validate already-assembled parts through the bundle contract.

    This is the programmatic entry point used by tests and by callers that build
    a bundle in memory instead of reading JSON.
    """
    raw = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "changes": list(changes),
        "activations": list(activations),
        "commit_graph": {
            "commits": [
                {"sha": sha, "parents": list(parents)}
                for sha, parents in (commit_graph or {}).items()
            ]
        },
        "session_fingerprints": list(session_fingerprints),
    }
    return normalize_change_bundle(raw)


__all__ = [
    "ACTIVATION_MECHANISMS",
    "BUNDLE_SCHEMA_VERSION",
    "CHANGE_KINDS",
    "CHANGE_REGISTRY_VERSION",
    "CHANGE_TAXONOMY_VERSION",
    "FINGERPRINT_TYPES",
    "NON_PR_KINDS",
    "OPTIMIZATION_CATEGORIES",
    "SCREENING_STATUSES",
    "SCREENING_VERSION",
    "build_change_bundle",
    "change_identity",
    "classify_categories",
    "normalize_activation",
    "normalize_change",
    "normalize_change_bundle",
]
