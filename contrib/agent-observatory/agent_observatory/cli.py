"""Command-line interface for the offline agent observatory.

Only explicitly supplied files and roots are read; there is no implicit
home-directory crawling. ``collect`` is the bounded, checkpointed collector over
explicit roots.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .adapters import AdapterContext, read_source
from .annotations import load_gold_set, save_gold_annotations
from .canonical import sha256_bytes
from .canary import (
    MAX_ALLOWED_REQUESTS,
    build_canary_report,
    load_canary_bundle,
    load_registration,
    normalize_registration,
)
from .changes import normalize_change_bundle
from .collector import (
    DEFAULT_MAX_SOURCE_BYTES,
    STATE_MODE_METADATA,
    STATE_MODE_TEXT,
    CollectorConfig,
    collect_once,
    collect_watch,
    collector_status,
    default_kill_switch_path,
    drain_queue,
    set_kill_switch,
)
from .episodes import segment_store
from .errors import ObservatoryError
from .evaluation import (
    DEFAULT_MULTI_LABEL_FACETS,
    EvaluationConfig,
    evaluate_gold_set,
    load_predictions,
    report_json,
)
from .exposure import (
    CommitGraph,
    attach_session_fingerprints,
    build_ledger,
    session_evidence_from_store,
)
from .inventory import (
    SourceRoot,
    build_manifest,
    discover_sources,
    load_manifest,
    manifest_json,
    records_to_jsonl,
)
from .impact import (
    DEFAULT_MATCH_ON,
    ImpactConfig,
    ImpactDataset,
    build_impact_report,
    file_sha256,
    load_impact_bundle,
    observed_evidence_from_store,
)
from .jev import REQUEST_BYTE_CAP, build_request, import_response, persist_request
from .policy import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    PolicyConfig,
    build_shadow_report,
    load_catalog,
    load_recommendation_bundle,
    recommendation_rows,
)
from .report import build_report
from .store import ObservatoryStore
from .taxonomy import DEFAULT_TAXONOMY_PATH, load_taxonomy
from .transport import TransportConfig, classify


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant {value!r} is not allowed")


def _read_json_object(path: str, what: str) -> Any:
    raw = Path(path).read_text(encoding="utf-8")
    try:
        value = json.loads(raw, parse_constant=_reject_json_constant)
    except ValueError as exc:
        raise ObservatoryError(f"{what} {path} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ObservatoryError(f"{what} {path} must contain a JSON object")
    return value


def _session_key(value: str) -> tuple[str, str, str, str]:
    """Parse a session identity from a collision-free JSON array or a legacy pipe form.

    Identity components may contain any character, so the pipe form cannot
    represent a component that itself contains ``|``. The JSON-array form always
    can (and matches the collision-free keys emitted in reports).
    """
    raw = value.strip()
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise ObservatoryError(f"--session JSON is not valid: {exc}") from exc
        if not isinstance(parsed, list):
            raise ObservatoryError("--session JSON form must be an array of four strings")
        parts = parsed
    else:
        parts = raw.split("|")
    if len(parts) != 4 or not all(isinstance(part, str) and part for part in parts):
        raise ObservatoryError(
            "--session must be a JSON array of four non-empty strings "
            '(["city_id","host_id","provider","session_id"]) or the legacy '
            "'city_id|host_id|provider|session_id' form"
        )
    return parts[0], parts[1], parts[2], parts[3]


def _validated_generation(value: Any) -> int:
    """Return a positive integer source generation or raise a clean error.

    ``argparse`` would reject a non-integer with a usage dump; validating here
    keeps ``--generation`` inside the documented ``error: ...`` exit-1 contract
    and bounds the value so a negative generation cannot reach fallback ids.
    """

    if isinstance(value, bool):
        raise ObservatoryError(f"--generation must be a positive integer, got {value!r}")
    if isinstance(value, int):
        generation = value
    elif isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        # A single optional sign only: ``lstrip("+-")`` would accept ``--5`` and
        # ``+-5`` here and then hand an int() ValueError to the caller.
        generation = int(value.strip())
    else:
        raise ObservatoryError(f"--generation must be a positive integer, got {value!r}")
    if generation < 1:
        raise ObservatoryError(f"--generation must be a positive integer, got {value!r}")
    return generation


def _write_output(payload: str, out_path: str | None) -> None:
    if out_path:
        Path(out_path).write_text(payload if payload.endswith("\n") else payload + "\n", encoding="utf-8")
    else:
        sys.stdout.write(payload if payload.endswith("\n") else payload + "\n")


def _cmd_import_jsonl(args: argparse.Namespace) -> int:
    with ObservatoryStore(args.db) as store:
        summaries = []
        for source in args.files:
            result = store.import_jsonl(source)
            summaries.append(
                {
                    "source": result.source_path,
                    "sha256": result.source_sha256,
                    "lines_read": result.lines_read,
                    "inserted": result.inserted,
                    "duplicates": result.duplicates,
                    "skipped_conflicts": result.skipped_conflicts,
                    "notes": result.notes,
                    "skipped_identical_file": result.skipped_identical_file,
                    "events": store.event_count(),
                    "sessions": store.session_count(),
                }
            )
        print(json.dumps({"imports": summaries}, indent=2, sort_keys=True))
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    with ObservatoryStore(args.db) as store:
        report = build_report(store)
    _write_output(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False), args.out)
    return 0


def _open_store(path: str) -> ObservatoryStore:
    """Open the projection, surfacing a local sqlite failure as a clean error."""
    try:
        return ObservatoryStore(path)
    except sqlite3.Error as exc:
        raise ObservatoryError(f"sqlite error: {exc}") from exc


def _resolve_snapshot_hash(args: argparse.Namespace) -> str:
    """Resolve a subject snapshot hash from an explicit value or the projection.

    When an explicit ``--snapshot-hash`` is supplied *and* the subject can be
    resolved from the projection (``--db`` plus ``--session``), the explicit
    value is cross-checked against the store and refused on mismatch rather than
    silently classifying a phantom subject.
    """
    explicit = args.snapshot_hash
    if not args.db or not args.session:
        if explicit:
            return explicit
        raise ObservatoryError(
            "provide --snapshot-hash, or --db and --session so the subject snapshot can be computed"
        )
    session_key = _session_key(args.session)
    with _open_store(args.db) as store:
        if args.subject_kind == "event":
            if not args.event_id:
                raise ObservatoryError("--event-id is required for --subject-kind event")
            computed = store.event_snapshot(session_key, args.event_id)
        else:
            computed = store.session_snapshot(session_key)
    if explicit and explicit != computed:
        raise ObservatoryError(
            f"explicit --snapshot-hash {explicit!r} does not match the projection "
            f"snapshot {computed!r} for the supplied session; refusing to classify "
            "a phantom subject"
        )
    return computed


def _cmd_build_request(args: argparse.Namespace) -> int:
    taxonomy = load_taxonomy(args.taxonomy)
    state = _read_json_object(args.state, "state")
    snapshot_hash = _resolve_snapshot_hash(args)

    request = build_request(
        state,
        taxonomy,
        snapshot_hash=snapshot_hash,
        subject_kind=args.subject_kind,
    )

    stored = False
    if args.db:
        with ObservatoryStore(args.db) as store:
            stored = persist_request(store, request, source_path=args.state)
            if args.out is None:
                # Re-serialize through the store to prove round-trip stability.
                record = store.get_request(request.request_hash)
                if record is None or record["request_json"] != request.serialized:
                    raise ObservatoryError("stored request failed round-trip verification")

    _write_output(request.serialized, args.out)
    print(
        json.dumps(
            {
                "request_hash": request.request_hash,
                "snapshot_hash": request.snapshot_hash,
                "subject_kind": request.subject_kind,
                "taxonomy_version": request.taxonomy_version,
                "question_hash": request.question_hash,
                "model": request.model,
                "bytes": request.byte_length,
                "byte_cap": REQUEST_BYTE_CAP,
                "stored": stored,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 0


def _transport_config_from_args(args: argparse.Namespace) -> TransportConfig:
    """Merge an optional JSON config file with explicit CLI overrides."""
    data: dict[str, Any] = {}
    if args.config:
        data = _read_json_object(args.config, "config")
    config = TransportConfig.from_mapping(data)

    if args.timeout is not None:
        config = replace(config, timeout_seconds=args.timeout)
    if args.max_attempts is not None:
        config = replace(config, retry=replace(config.retry, max_attempts=args.max_attempts))

    budget = config.budget
    if args.max_requests is not None:
        budget = replace(budget, max_requests=args.max_requests)
    if args.max_tokens is not None:
        budget = replace(budget, max_tokens=args.max_tokens)
    if args.max_cost_usd is not None:
        budget = replace(budget, max_cost_usd=args.max_cost_usd)
    if args.price_per_million_input_usd is not None:
        budget = replace(
            budget, price_per_million_input_usd=args.price_per_million_input_usd
        )
    if args.price_per_million_output_usd is not None:
        budget = replace(
            budget, price_per_million_output_usd=args.price_per_million_output_usd
        )
    if budget.max_cost_usd is not None and (
        budget.price_per_million_input_usd is None or budget.price_per_million_output_usd is None
    ):
        # Without both prices the cost is unknown and the ceiling never trips.
        raise ObservatoryError(
            "--max-cost-usd needs --price-per-million-input-usd and --price-per-million-output-usd"
        )
    if budget is not config.budget:
        config = replace(config, budget=budget)

    if args.allow_model_drift:
        config = replace(config, allow_model_drift=True)
    return config


def _cmd_classify(args: argparse.Namespace) -> int:
    """Send one built request with the bounded transport and record the outcome.

    The credential is read at runtime from ``JEV_API_KEY`` or ``JEV_KEY_FILE``;
    a missing credential or any transport/validation failure is recorded as
    ``pending``/unknown rather than raised.
    """
    taxonomy = load_taxonomy(args.taxonomy)
    state = _read_json_object(args.state, "state")
    snapshot_hash = _resolve_snapshot_hash(args)
    request = build_request(
        state,
        taxonomy,
        snapshot_hash=snapshot_hash,
        subject_kind=args.subject_kind,
    )
    config = _transport_config_from_args(args)
    with _open_store(args.db) as store:
        result = classify(store, request, config=config)
    _write_output(
        json.dumps(result.to_dict(), indent=2, sort_keys=True, ensure_ascii=False),
        args.out,
    )
    # A pending/unknown classification is a delivered, recorded outcome rather
    # than a crash; the nonzero status tells an operator to look at the failure.
    return 0 if result.outcome == "classified" else 1


def _cmd_import_response(args: argparse.Namespace) -> int:
    response = _read_json_object(args.response, "response")
    source_sha = sha256_bytes(Path(args.response).read_bytes())
    with ObservatoryStore(args.db) as store:
        result = import_response(
            store,
            response,
            request_hash=args.request_hash,
            source_path=args.response,
            source_sha256=source_sha,
        )
    print(
        json.dumps(
            {
                "classification_id": result.classification_id,
                "deduplicated": result.deduplicated,
                "response_hash": result.response_hash,
                "model": result.model,
                "taxonomy_version": result.taxonomy_version,
                "snapshot_hash": result.snapshot_hash,
                "answers": result.answers,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _cmd_inventory(args: argparse.Namespace) -> int:
    roots = [SourceRoot(path=root, provider=args.provider) for root in args.root]
    previous = load_manifest(args.previous) if args.previous else None
    manifest = build_manifest(
        roots,
        city_id=args.city,
        host_id=args.host,
        repo=args.repo,
        previous=previous,
    )
    _write_output(manifest_json(manifest), args.out)
    print(
        json.dumps(
            {
                "sources": manifest["totals"]["sources"],
                "events": manifest["totals"]["events"],
                "sessions": manifest["totals"]["sessions"],
                "unsupported_providers": manifest["totals"]["unsupported_providers"],
                "out": args.out,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    context = AdapterContext(city_id=args.city, host_id=args.host, repo=args.repo)
    inputs: list[str] = list(args.input or [])
    if args.root:
        sources, _unsupported = discover_sources(
            [SourceRoot(path=root, provider=args.provider) for root in args.root]
        )
        inputs.extend(source.path for source in sources)
    if not inputs:
        raise ObservatoryError("export requires at least one --input or --root")

    generation = _validated_generation(args.generation)
    payloads = []
    summaries = []
    for source in inputs:
        result = read_source(source, context=context, provider=args.provider, generation=generation)
        payloads.append(records_to_jsonl(result.records))
        summaries.append(
            {
                "source": str(source),
                "session_id": result.session_id,
                "parent_session_id": result.parent_session_id,
                "records": len(result.records),
                "partial_trailing_line": result.partial_trailing_line,
                "errors": result.errors,
            }
        )
    payload = "".join(payloads)
    inserted: int | None = None
    duplicate_count: int | None = None

    if args.db:
        if args.out:
            import_path = args.out
            Path(import_path).write_text(payload, encoding="utf-8")
            with ObservatoryStore(args.db) as store:
                imported = store.import_jsonl(import_path)
                inserted, duplicate_count = imported.inserted, imported.duplicates
        else:
            handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
            try:
                handle.write(payload)
                handle.close()
                with ObservatoryStore(args.db) as store:
                    imported = store.import_jsonl(handle.name)
                    inserted, duplicate_count = imported.inserted, imported.duplicates
            finally:
                os.unlink(handle.name)
    elif args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)

    print(
        json.dumps(
            {
                "records": sum(item["records"] for item in summaries),
                "sources": len(summaries),
                "inserted": inserted,
                "duplicates": duplicate_count,
                "out": args.out,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 0


def _cmd_episodes(args: argparse.Namespace) -> int:
    """Emit deterministic episode candidates from the projection."""
    with ObservatoryStore(args.db) as store:
        episodes = segment_store(store)
    payload = {
        "report_version": "1",
        "kind": "episode_candidates",
        "episodes": [
            {
                "episode_id": episode.episode_id,
                "group_key": episode.group_key,
                "session": list(episode.session_key),
                "provider": episode.provider,
                "repo": episode.repo,
                "work_anchor": episode.work_anchor,
                "started_at": episode.started_at,
                "ended_at": episode.ended_at,
                "event_count": episode.event_count,
            }
            for episode in episodes
        ],
    }
    _write_output(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False), args.out)
    print(
        json.dumps({"episodes": len(episodes), "groups": len({ep.group_key for ep in episodes})}, sort_keys=True),
        file=sys.stderr,
    )
    return 0


def _cmd_annotate(args: argparse.Namespace) -> int:
    """Validate a gold set and append it to the projection (append-only)."""
    taxonomy = load_taxonomy(args.taxonomy)
    gold_set = load_gold_set(args.gold, taxonomy)
    with ObservatoryStore(args.db) as store:
        inserted = save_gold_annotations(store, gold_set)
        total = store.gold_annotation_count()
    print(
        json.dumps(
            {
                "gold_set_version": gold_set.gold_set_version,
                "episodes": len(gold_set.episodes),
                "inserted": inserted,
                "stored_annotations": total,
                "gold_set_hash": gold_set.gold_set_hash(),
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 0


def _cmd_changes_sync(args: argparse.Namespace) -> int:
    """Import an explicit change/exposure registry bundle (M5).

    There is no live PR crawler: the bundle is the caller-supplied, versioned
    evidence, and importing the same identity with different content is refused
    rather than silently overwritten.
    """
    bundle = normalize_change_bundle(_read_json_object(args.input, "change bundle"))
    with _open_store(args.db) as store:
        result = store.import_registry(bundle)
        summary = {
            "changes_inserted": result.changes_inserted,
            "changes_deduplicated": result.changes_deduplicated,
            "activations_inserted": result.activations_inserted,
            "activations_deduplicated": result.activations_deduplicated,
            "commit_parents_inserted": result.commit_parents_inserted,
            "session_fingerprints_inserted": result.session_fingerprints_inserted,
            "changes": store.change_count(),
            "activations": store.activation_count(),
        }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _cmd_exposure(args: argparse.Namespace) -> int:
    """Join observed sessions to registered changes and emit the ledger."""
    with _open_store(args.db) as store:
        changes = list(store.iter_changes())
        activations = list(store.iter_activations())
        graph = CommitGraph(store.load_commit_graph())
        sessions = attach_session_fingerprints(
            session_evidence_from_store(store), store.load_session_fingerprints()
        )
        ledger = build_ledger(
            changes,
            activations,
            sessions,
            graph=graph,
            generated_by=f"agent-observatory/{__version__}",
        )
        exposure_rows = []
        for entry in ledger["changes"]:
            exposure = entry.get("exposure")
            if not exposure:
                continue
            for row in exposure["rows"]:
                exposure_rows.append(
                    {
                        "change_id": entry["change_id"],
                        "session": row["session"],
                        "status": row["status"],
                        "evidence": row["evidence"],
                    }
                )
        stored = store.replace_exposures(exposure_rows)
    _write_output(json.dumps(ledger, indent=2, sort_keys=True, ensure_ascii=False), args.out)
    print(
        json.dumps(
            {
                "screened": ledger["screening"]["screened"],
                "interventions": ledger["screening"]["optimization"],
                "exposure_totals": ledger["exposure_totals"],
                "exposure_rows": stored,
                "out": args.out,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 0


def _cmd_impact(args: argparse.Namespace) -> int:
    """Build an accepted-task impact report from an explicit bundle and/or a projection.

    ``--input`` supplies the work items, cohort assignment and classifier
    overhead. ``--db`` adds descriptive real-projection evidence and never
    fabricates acceptance or cohort evidence the projection does not carry.
    """
    if not args.input and not args.db:
        raise ObservatoryError("impact requires --input BUNDLE.json and/or --db DB")

    if args.input:
        dataset = load_impact_bundle(args.input)
    else:
        dataset = ImpactDataset(
            work_items=(),
            classifier_overhead=(),
            evidence={},
            match_on=DEFAULT_MATCH_ON,
            bundle_schema_version=None,
            generated_by=f"agent-observatory/{__version__}",
        )

    observed = None
    if args.db:
        with _open_store(args.db) as store:
            observed = observed_evidence_from_store(
                store,
                source_label=Path(args.db).name,
                source_hash=file_sha256(args.db),
            )

    config = ImpactConfig(
        primary_outcome=args.primary_outcome,
        bootstrap_resamples=args.bootstrap_resamples,
    )
    report = build_impact_report(dataset, config, observed_evidence=observed)
    _write_output(report_json(report), args.out)
    attribution = report["attribution"]
    print(
        json.dumps(
            {
                "eligible": report["accepted_tasks"]["eligible"],
                "accepted": report["accepted_tasks"]["accepted"],
                "censored": report["accepted_tasks"]["censored"],
                "attribution_grade": attribution["grade"],
                "conclusion": attribution["conclusion"],
                "confounded": attribution["confounded"],
                "report_hash": report["report_hash"],
                "out": args.out,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 0


def _cmd_shadow(args: argparse.Namespace) -> int:
    """Build shadow-policy recommendations without modifying routing (M7).

    ``--catalog`` is the current configured candidate set and ``--input`` is the
    bundle of validated classifications with their as-of provenance. ``--db``
    optionally persists the recommendations (append-only, content-hashed); the
    report is always written and no routing, dispatch or config is touched.
    """
    catalog = load_catalog(args.catalog)
    bundle = load_recommendation_bundle(args.input)
    config = PolicyConfig(confidence_threshold=args.confidence_threshold)
    report = build_shadow_report(
        bundle,
        catalog,
        config,
        generated_by=f"agent-observatory/{__version__}",
    )
    # Persist exactly the report that is emitted (stable float precision) so a
    # stored payload and the written report cannot disagree on the numbers.
    serialized = report_json(report)
    stored: int | None = None
    deduplicated: int | None = None
    if args.db:
        rows = recommendation_rows(json.loads(serialized))
        with _open_store(args.db) as store:
            result = store.save_recommendations(rows)
            stored = result.inserted
            deduplicated = result.deduplicated
    _write_output(serialized, args.out)
    shadow = report["shadow"]
    print(
        json.dumps(
            {
                "catalog_version": report["provenance"]["catalog_version"],
                "episodes": shadow["episodes"],
                "recommendations": shadow["totals"]["recommendations"],
                "recommended": shadow["totals"]["recommended"],
                "agree": shadow["totals"]["agree"],
                "fallback": shadow["totals"]["fallback"],
                "disagreements": shadow["totals"]["disagreements"],
                "leak_free": shadow["leak_free"],
                "stored": stored,
                "deduplicated": deduplicated,
                "report_hash": report["report_hash"],
                "out": args.out,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 0


def _cmd_canary_register(args: argparse.Namespace) -> int:
    """Validate a canary spec and write the pre-registered artifact (M8).

    The registration is the durable, pre-run record of the policy, seed, sample
    size, guardrails and request cap. Nothing is applied here; the artifact is
    the caller's proof that the design was fixed before the run.
    """
    raw = _read_json_object(args.input, "canary spec")
    registration = normalize_registration(raw)
    serialized = json.dumps(registration.content(), indent=2, sort_keys=True, ensure_ascii=False)
    _write_output(serialized, args.out)
    print(
        json.dumps(
            {
                "registration_id": registration.registration_id,
                "policy_id": registration.policy_id,
                "policy_kind": registration.policy_kind,
                "seed": registration.seed,
                "sample_size": registration.sample_size,
                "max_requests": registration.max_requests,
                "registration_hash": registration.registration_hash(),
                "out": args.out,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 0


def _cmd_canary(args: argparse.Namespace) -> int:
    """Evaluate a pre-registered, opt-in canary without changing routing (M8).

    ``--enable`` opts in (the default is the prior policy). A present
    ``--kill-switch`` file rolls an enabled canary back to the prior policy.
    ``--max-requests`` bounds live classification requests to at most the
    owner-approved cap. No routing, dispatch or config is written.
    """
    registration = load_registration(args.registration)
    bundle = load_canary_bundle(args.input)
    catalog = load_catalog(args.catalog)
    kill_switch = bool(args.kill_switch) and Path(args.kill_switch).exists()
    report = build_canary_report(
        bundle,
        catalog,
        registration,
        enabled=bool(args.enable),
        kill_switch=kill_switch,
        max_requests=args.max_requests,
        generated_by=f"agent-observatory/{__version__}",
    )
    serialized = report_json(report)
    _write_output(serialized, args.out)
    canary = report["canary"]
    print(
        json.dumps(
            {
                "policy_id": canary["policy_id"],
                "mode": canary["mode"],
                "enabled": canary["enabled"],
                "kill_switch": canary["kill_switch"],
                "eligible_units": canary["exposure"]["eligible_units"],
                "assigned_control": canary["exposure"]["assigned_control"],
                "assigned_treatment": canary["exposure"]["assigned_treatment"],
                "requests_used": canary["classification_budget"]["requests_used"],
                "max_requests_effective": canary["classification_budget"]["max_requests_effective"],
                "requests_capped": canary["classification_budget"]["requests_capped"],
                "conclusion": canary["net_effect"]["conclusion"],
                "improvement_claim": canary["net_effect"]["improvement_claim"],
                "stop_recommended": canary["stop_recommended"],
                "report_hash": report["report_hash"],
                "out": args.out,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 0


def _cmd_evaluate(args: argparse.Namespace) -> int:
    taxonomy = load_taxonomy(args.taxonomy)
    gold_set = load_gold_set(args.gold, taxonomy)
    predictors = {
        "jev": load_predictions(args.predictions, taxonomy, primary_facet=args.primary_facet),
    }
    multi_label_facets = (
        tuple(args.multi_label_facet)
        if args.multi_label_facet
        else DEFAULT_MULTI_LABEL_FACETS
    )
    config = EvaluationConfig(
        primary_facet=args.primary_facet,
        multi_label_facets=multi_label_facets,
        holdout_fraction=args.holdout_fraction,
        confidence_threshold=args.confidence_threshold,
        min_class_support=args.min_class_support,
        calibration_bins=args.calibration_bins,
    )
    report = evaluate_gold_set(gold_set, predictors, taxonomy, config)
    _write_output(report_json(report), args.out)
    split = report["split"]
    print(
        json.dumps(
            {
                "gold_episodes": report["gold"]["episodes"],
                "gold_groups": report["gold"]["groups"],
                "tuning_episodes": split["tuning_episodes"],
                "holdout_episodes": split["holdout_episodes"],
                "leak_free": split["leak_free"],
                "evaluation_hash": report["evaluation_hash"],
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 0 if split["leak_free"] else 1


def _kill_switch_path(args: argparse.Namespace) -> str:
    return args.kill_switch or default_kill_switch_path(args.db)


def _print_json(value: Any) -> None:
    sys.stdout.write(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _cmd_collect(args: argparse.Namespace) -> int:
    """Run one bounded collection pass, or repeat it with ``--watch``."""
    config = CollectorConfig(
        roots=tuple(SourceRoot(path=root, provider=args.provider) for root in args.root),
        city_id=args.city,
        host_id=args.host,
        repo=args.repo,
        debounce_seconds=args.debounce_seconds,
        max_debounce_seconds=max(args.max_debounce_seconds, args.debounce_seconds),
        max_sources_per_run=args.max_sources,
        max_bytes_per_run=args.max_bytes,
        max_source_bytes=args.max_source_bytes,
        max_db_bytes=args.max_db_bytes,
        kill_switch_path=_kill_switch_path(args),
        spool_dir=args.spool_dir,
        enqueue=not args.no_enqueue,
    )
    with _open_store(args.db) as store:
        if args.watch:
            runs = collect_watch(
                store,
                config,
                interval_seconds=args.interval,
                iterations=args.iterations,
                on_run=lambda run: _print_json(run.to_dict()),
            )
            last = runs[-1] if runs else None
        else:
            last = collect_once(store, config)
            _print_json(last.to_dict())
    # ``locked`` means another collector is running: not an error, but not work.
    return 0 if last is None or last.status in {"ok", "disabled", "locked"} else 1


def _cmd_collect_status(args: argparse.Namespace) -> int:
    with _open_store(args.db) as store:
        status = collector_status(store, kill_switch_path=_kill_switch_path(args))
    _write_output(json.dumps(status, indent=2, sort_keys=True, ensure_ascii=False), args.out)
    return 0


def _cmd_collector_switch(args: argparse.Namespace) -> int:
    path = _kill_switch_path(args)
    disabled = set_kill_switch(path, disabled=args.state == "off")
    _print_json({"collector": "disabled" if disabled else "enabled", "kill_switch": path})
    return 0


def _cmd_queue_drain(args: argparse.Namespace) -> int:
    """Classify due queued sessions within an explicit request ceiling."""
    if args.max_requests is None:
        raise ObservatoryError("queue-drain requires --max-requests (the per-run spend ceiling)")
    taxonomy = load_taxonomy(args.taxonomy)
    config = _transport_config_from_args(args)
    with _open_store(args.db) as store:
        result = drain_queue(
            store,
            taxonomy,
            transport_config=config,
            max_items=args.max_items if args.max_items is not None else args.max_requests,
            max_attempts=args.max_item_attempts,
            retry_backoff_seconds=args.retry_backoff,
            kill_switch_path=_kill_switch_path(args),
            state_mode=STATE_MODE_TEXT if args.text_state else STATE_MODE_METADATA,
        )
    _write_output(json.dumps(result.to_dict(), indent=2, sort_keys=True, ensure_ascii=False), args.out)
    return 0 if result.status in {"ok", "disabled", "locked"} else 1


def _add_transport_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        default=None,
        help="JSON transport config (timeout/retry/budget/circuit); unknown keys are rejected",
    )
    parser.add_argument("--timeout", type=float, default=None, help="HTTP timeout in seconds (default 30)")
    parser.add_argument("--max-attempts", type=int, default=None, help="maximum HTTP attempts")
    parser.add_argument("--max-requests", type=int, default=None, help="per-run request cap")
    parser.add_argument("--max-tokens", type=int, default=None, help="per-run token budget ceiling")
    parser.add_argument("--max-cost-usd", type=float, default=None, help="per-run dollar budget ceiling")
    parser.add_argument(
        "--price-per-million-input-usd", type=float, default=None, help="input token price (USD per million)"
    )
    parser.add_argument(
        "--price-per-million-output-usd", type=float, default=None, help="output token price (USD per million)"
    )
    parser.add_argument(
        "--allow-model-drift", action="store_true", help="allow a model other than the pinned jev-1.13.0"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-observatory",
        description="Offline Jev-powered agent observatory (import, report, request/response).",
    )
    parser.add_argument("--version", action="version", version=f"agent-observatory {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import-jsonl", help="import normalized JSONL into the projection")
    import_parser.add_argument("--db", required=True, help="SQLite projection path")
    import_parser.add_argument("files", nargs="+", help="normalized JSONL files (explicit paths only)")
    import_parser.set_defaults(func=_cmd_import_jsonl)

    report_parser = subparsers.add_parser("report", help="emit a deterministic report JSON")
    report_parser.add_argument("--db", required=True, help="SQLite projection path")
    report_parser.add_argument("--out", default=None, help="write report to this path instead of stdout")
    report_parser.set_defaults(func=_cmd_report)

    request_parser = subparsers.add_parser("build-request", help="build a Jev /v1/systemone request body")
    request_parser.add_argument("--db", default=None, help="SQLite projection path (optional; used to store request)")
    request_parser.add_argument("--state", required=True, help="explicit sanitized state JSON file")
    request_parser.add_argument("--subject-kind", choices=("event", "session"), default="session")
    request_parser.add_argument(
        "--session",
        default=None,
        help='JSON array ["city_id","host_id","provider","session_id"], or legacy city_id|host_id|provider|session_id',
    )
    request_parser.add_argument("--event-id", default=None, help="event id for --subject-kind event")
    request_parser.add_argument("--snapshot-hash", default=None, help="explicit subject snapshot hash")
    request_parser.add_argument("--taxonomy", default=None, help="taxonomy JSON path")
    request_parser.add_argument("--out", default=None, help="write request body to this path instead of stdout")
    request_parser.set_defaults(func=_cmd_build_request)

    response_parser = subparsers.add_parser("import-response", help="validate and store a saved Jev response")
    response_parser.add_argument("--db", required=True, help="SQLite projection path")
    response_parser.add_argument("--response", required=True, help="saved response JSON file")
    response_parser.add_argument("--request-hash", required=True, help="request hash the response answers")
    response_parser.set_defaults(func=_cmd_import_response)

    inventory_parser = subparsers.add_parser(
        "inventory",
        help="scan explicit source roots and emit a coverage manifest",
    )
    inventory_parser.add_argument(
        "--root",
        action="append",
        required=True,
        help="explicit transcript root (repeatable; no home-directory crawling)",
    )
    inventory_parser.add_argument(
        "--provider",
        default=None,
        help="force one provider instead of detecting it from each path",
    )
    inventory_parser.add_argument("--city", required=True, help="city id recorded in the manifest")
    inventory_parser.add_argument("--host", required=True, help="host id recorded in the manifest")
    inventory_parser.add_argument("--repo", default=None, help="optional repository scope")
    inventory_parser.add_argument(
        "--previous",
        default=None,
        help="previous manifest JSON used to detect append vs rewrite generations",
    )
    inventory_parser.add_argument("--out", default=None, help="write manifest JSON to this path instead of stdout")
    inventory_parser.set_defaults(func=_cmd_inventory)

    export_parser = subparsers.add_parser(
        "export",
        help="export native provider transcripts as normalized JSONL",
    )
    export_parser.add_argument(
        "--provider",
        required=True,
        help="provider adapter to use (claude, codex, dsh)",
    )
    export_parser.add_argument(
        "--input",
        action="append",
        default=None,
        help="explicit transcript file (repeatable)",
    )
    export_parser.add_argument(
        "--root",
        action="append",
        default=None,
        help="explicit root to discover transcripts under (repeatable)",
    )
    export_parser.add_argument("--city", required=True, help="city id for emitted records")
    export_parser.add_argument("--host", required=True, help="host id for emitted records")
    export_parser.add_argument("--repo", default=None, help="optional repository scope")
    export_parser.add_argument(
        "--generation",
        type=str,
        default="1",
        help="logical source generation for fallback ids (positive integer; default 1)",
    )
    export_parser.add_argument("--out", default=None, help="write JSONL to this path instead of stdout")
    export_parser.add_argument("--db", default=None, help="import the exported JSONL into this projection")
    export_parser.set_defaults(func=_cmd_export)

    classify_parser = subparsers.add_parser(
        "classify", help="send a built Jev request with the bounded live transport"
    )
    classify_parser.add_argument("--db", required=True, help="SQLite projection path")
    classify_parser.add_argument("--state", required=True, help="explicit sanitized state JSON file")
    classify_parser.add_argument("--taxonomy", default=None, help="taxonomy JSON path")
    classify_parser.add_argument("--subject-kind", choices=("event", "session"), default="session")
    classify_parser.add_argument(
        "--session",
        default=None,
        help='JSON array ["city_id","host_id","provider","session_id"], or legacy city_id|host_id|provider|session_id',
    )
    classify_parser.add_argument("--event-id", default=None, help="event id for --subject-kind event")
    classify_parser.add_argument("--snapshot-hash", default=None, help="explicit subject snapshot hash")
    classify_parser.add_argument(
        "--config",
        default=None,
        help="JSON transport config (timeout/retry/budget/circuit); unknown keys are rejected",
    )
    classify_parser.add_argument("--timeout", type=float, default=None, help="HTTP timeout in seconds (default 30)")
    classify_parser.add_argument("--max-attempts", type=int, default=None, help="maximum HTTP attempts")
    classify_parser.add_argument("--max-requests", type=int, default=None, help="per-run request cap")
    classify_parser.add_argument("--max-tokens", type=int, default=None, help="per-run token budget ceiling")
    classify_parser.add_argument("--max-cost-usd", type=float, default=None, help="per-run dollar budget ceiling")
    classify_parser.add_argument(
        "--price-per-million-input-usd", type=float, default=None, help="input token price (USD per million)"
    )
    classify_parser.add_argument(
        "--price-per-million-output-usd", type=float, default=None, help="output token price (USD per million)"
    )
    classify_parser.add_argument(
        "--allow-model-drift", action="store_true", help="allow a model other than the pinned jev-1.13.0"
    )
    classify_parser.add_argument("--out", default=None, help="write the result JSON to this path instead of stdout")
    classify_parser.set_defaults(func=_cmd_classify)

    evaluate_parser = subparsers.add_parser(
        "evaluate",
        help="evaluate predictions against a pinned gold set with a leak-free split",
    )
    evaluate_parser.add_argument("--gold", required=True, help="gold annotation JSON file")
    evaluate_parser.add_argument(
        "--predictions",
        required=True,
        help="prediction JSON file for the semantic model (title/metadata baselines are added automatically)",
    )
    evaluate_parser.add_argument(
        "--taxonomy",
        default=str(DEFAULT_TAXONOMY_PATH.with_name("jev_taxonomy_v2.json")),
        help="taxonomy JSON path (defaults to the versioned facet taxonomy)",
    )
    evaluate_parser.add_argument("--primary-facet", default="primary_intent", help="single-valued facet to score")
    evaluate_parser.add_argument(
        "--multi-label-facet",
        action="append",
        default=None,
        help="many-valued facet to score (repeatable; replaces the default set)",
    )
    evaluate_parser.add_argument("--holdout-fraction", type=float, default=0.4, help="fraction of groups held out")
    evaluate_parser.add_argument(
        "--confidence-threshold", type=float, default=0.9, help="minimum confidence for the automation gate"
    )
    evaluate_parser.add_argument(
        "--min-class-support", type=int, default=2, help="tuning support below which a class is report-only"
    )
    evaluate_parser.add_argument("--calibration-bins", type=int, default=5, help="reliability bin count")
    evaluate_parser.add_argument("--out", default=None, help="write the evaluation report to this path")
    evaluate_parser.set_defaults(func=_cmd_evaluate)

    impact_parser = subparsers.add_parser(
        "impact",
        help="accepted-task impact report from an explicit bundle and/or a projection (M6)",
    )
    impact_parser.add_argument("--db", default=None, help="SQLite projection path (read-only evidence)")
    impact_parser.add_argument(
        "--input",
        default=None,
        help="explicit versioned impact bundle JSON (work items, cohorts, classifier overhead)",
    )
    impact_parser.add_argument(
        "--primary-outcome",
        default="time_to_accepted_seconds",
        help="outcome whose matched effect drives the conclusion",
    )
    impact_parser.add_argument(
        "--bootstrap-resamples", type=int, default=2000, help="deterministic bootstrap resamples"
    )
    impact_parser.add_argument("--out", default=None, help="write the impact report to this path")
    impact_parser.set_defaults(func=_cmd_impact)

    shadow_parser = subparsers.add_parser(
        "shadow",
        help="shadow policy recommendations against current routing (M7, advisory only)",
    )
    shadow_parser.add_argument(
        "--catalog",
        required=True,
        help="current configured candidate catalog JSON (the only recommendable set)",
    )
    shadow_parser.add_argument(
        "--input",
        required=True,
        help="versioned recommendation bundle JSON (as-of classifications)",
    )
    shadow_parser.add_argument(
        "--db",
        default=None,
        help="SQLite projection path (optional; persists recommendations append-only)",
    )
    shadow_parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=DEFAULT_CONFIDENCE_THRESHOLD,
        help="minimum classification confidence for an eligible recommendation",
    )
    shadow_parser.add_argument("--out", default=None, help="write the shadow report to this path")
    shadow_parser.set_defaults(func=_cmd_shadow)

    canary_register_parser = subparsers.add_parser(
        "canary-register",
        help="validate and write a pre-registered policy-canary artifact (M8, no run)",
    )
    canary_register_parser.add_argument(
        "--input", required=True, help="canary design spec JSON (policy, seed, guardrails, cap)"
    )
    canary_register_parser.add_argument(
        "--out", default=None, help="write the frozen registration JSON to this path"
    )
    canary_register_parser.set_defaults(func=_cmd_canary_register)

    canary_parser = subparsers.add_parser(
        "canary",
        help="opt-in seeded randomized policy canary with a kill switch (M8, advisory)",
    )
    canary_parser.add_argument(
        "--registration", required=True, help="pre-registered canary artifact JSON"
    )
    canary_parser.add_argument(
        "--catalog", required=True, help="current configured candidate catalog JSON"
    )
    canary_parser.add_argument(
        "--input", required=True, help="canary bundle JSON (units, strata, optional outcomes)"
    )
    canary_parser.add_argument(
        "--enable",
        action="store_true",
        help="opt in to treatment assignment (default: prior policy for every unit)",
    )
    canary_parser.add_argument(
        "--kill-switch",
        default=None,
        help="rollback file; if it exists the prior policy is restored for every unit",
    )
    canary_parser.add_argument(
        "--max-requests",
        type=int,
        default=None,
        help=(
            "live classification request budget for this run "
            f"(default and hard ceiling {MAX_ALLOWED_REQUESTS})"
        ),
    )
    canary_parser.add_argument("--out", default=None, help="write the canary report to this path")
    canary_parser.set_defaults(func=_cmd_canary)

    episodes_parser = subparsers.add_parser(
        "episodes",
        help="segment the projection into task-episode candidates",
    )
    episodes_parser.add_argument("--db", required=True, help="SQLite projection path")
    episodes_parser.add_argument("--out", default=None, help="write episode candidates to this path")
    episodes_parser.set_defaults(func=_cmd_episodes)

    annotate_parser = subparsers.add_parser(
        "annotate",
        help="validate a gold annotation set and append it to the projection",
    )
    annotate_parser.add_argument("--db", required=True, help="SQLite projection path")
    annotate_parser.add_argument("--gold", required=True, help="gold annotation JSON file")
    annotate_parser.add_argument(
        "--taxonomy",
        default=str(DEFAULT_TAXONOMY_PATH.with_name("jev_taxonomy_v2.json")),
        help="taxonomy JSON path (defaults to the versioned facet taxonomy)",
    )
    annotate_parser.set_defaults(func=_cmd_annotate)

    changes_parser = subparsers.add_parser(
        "changes-sync",
        help="import an explicit change/exposure registry bundle (M5)",
    )
    changes_parser.add_argument("--db", required=True, help="SQLite projection path")
    changes_parser.add_argument(
        "--input",
        required=True,
        help="explicit change bundle JSON (no live PR crawler)",
    )
    changes_parser.set_defaults(func=_cmd_changes_sync)

    exposure_parser = subparsers.add_parser(
        "exposure",
        help="join sessions to registered changes and emit the optimization ledger",
    )
    exposure_parser.add_argument("--db", required=True, help="SQLite projection path")
    exposure_parser.add_argument(
        "--out", default=None, help="write the ledger JSON to this path instead of stdout"
    )
    exposure_parser.set_defaults(func=_cmd_exposure)

    collect_parser = subparsers.add_parser(
        "collect",
        help="checkpointed, debounced, bounded collection from explicit roots (M4)",
    )
    collect_parser.add_argument("--db", required=True, help="SQLite projection path")
    collect_parser.add_argument(
        "--root", action="append", required=True, help="explicit transcript root (repeatable)"
    )
    collect_parser.add_argument("--provider", default=None, help="force one provider for every root")
    collect_parser.add_argument("--city", required=True, help="city id for emitted records")
    collect_parser.add_argument("--host", required=True, help="host id for emitted records")
    collect_parser.add_argument("--repo", default=None, help="optional repository scope")
    collect_parser.add_argument(
        "--debounce-seconds", type=float, default=30.0, help="quiet period before a changed file is read"
    )
    collect_parser.add_argument(
        "--max-debounce-seconds",
        type=float,
        default=600.0,
        help="read a never-quiet file anyway after this long since its last import",
    )
    collect_parser.add_argument("--max-sources", type=int, default=None, help="changed sources read per run")
    collect_parser.add_argument("--max-bytes", type=int, default=None, help="source bytes read per run")
    collect_parser.add_argument(
        "--max-source-bytes",
        type=int,
        default=DEFAULT_MAX_SOURCE_BYTES,
        help=(
            "defer any single source larger than this, including a decompressed "
            f".zstd source (default {DEFAULT_MAX_SOURCE_BYTES}; adapters parse whole files in memory)"
        ),
    )
    collect_parser.add_argument(
        "--max-db-bytes", type=int, default=None, help="defer imports once the projection reaches this size"
    )
    collect_parser.add_argument(
        "--kill-switch", default=None, help="kill switch file (default DB.collector-disabled)"
    )
    collect_parser.add_argument("--spool-dir", default=None, help="normalized JSONL spool (default DB.collector-spool)")
    collect_parser.add_argument(
        "--no-enqueue", action="store_true", help="do not queue changed sessions for classification"
    )
    collect_parser.add_argument("--watch", action="store_true", help="repeat until the kill switch engages")
    collect_parser.add_argument("--interval", type=float, default=60.0, help="seconds between --watch passes")
    collect_parser.add_argument("--iterations", type=int, default=None, help="stop --watch after N passes")
    collect_parser.set_defaults(func=_cmd_collect)

    status_parser = subparsers.add_parser(
        "collect-status", help="collector coverage, lag and classification queue health"
    )
    status_parser.add_argument("--db", required=True, help="SQLite projection path")
    status_parser.add_argument("--kill-switch", default=None, help="kill switch file (default DB.collector-disabled)")
    status_parser.add_argument("--out", default=None, help="write status JSON to this path instead of stdout")
    status_parser.set_defaults(func=_cmd_collect_status)

    switch_parser = subparsers.add_parser("collector-switch", help="engage (off) or release (on) the kill switch")
    switch_parser.add_argument("--db", required=True, help="SQLite projection path")
    switch_parser.add_argument("state", choices=("on", "off"), help="on enables collection, off disables it")
    switch_parser.add_argument("--kill-switch", default=None, help="kill switch file (default DB.collector-disabled)")
    switch_parser.set_defaults(func=_cmd_collector_switch)

    drain_parser = subparsers.add_parser(
        "queue-drain", help="classify queued sessions within a request ceiling (metadata-only unless --text-state)"
    )
    drain_parser.add_argument("--db", required=True, help="SQLite projection path")
    drain_parser.add_argument("--taxonomy", default=None, help="taxonomy JSON path")
    drain_parser.add_argument(
        "--text-state",
        action="store_true",
        help="opt in to redacted transcript text in the classification state (default: metadata only)",
    )
    drain_parser.add_argument("--max-items", type=int, default=None, help="queue items to consider (default --max-requests)")
    drain_parser.add_argument(
        "--max-item-attempts", type=int, default=3, help="failed attempts before an item moves to unknown"
    )
    drain_parser.add_argument(
        "--retry-backoff", type=float, default=300.0, help="base seconds before a failed item is retried"
    )
    drain_parser.add_argument("--kill-switch", default=None, help="kill switch file (default DB.collector-disabled)")
    drain_parser.add_argument("--out", default=None, help="write the drain result to this path instead of stdout")
    _add_transport_args(drain_parser)
    drain_parser.set_defaults(func=_cmd_queue_drain)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ObservatoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        # File IO at the CLI boundary is unchecked on purpose: turn a missing or
        # unreadable path into the documented ``error: <path>: <reason>`` contract
        # instead of a traceback.
        reason = exc.strerror or str(exc)
        if exc.filename:
            print(f"error: {exc.filename}: {reason}", file=sys.stderr)
        else:
            print(f"error: {reason}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        # A projection that cannot be opened or written is an operator error, not
        # a crash: report it as ``error: ...`` and exit 1 with no traceback.
        print(f"error: sqlite error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
