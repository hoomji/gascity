"""Command-line interface for the offline agent observatory.

Only explicitly supplied files are read. There is no automatic live collector
and no home-directory crawling.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .canonical import sha256_bytes
from .errors import ObservatoryError
from .jev import REQUEST_BYTE_CAP, build_request, import_response, persist_request
from .report import build_report
from .store import ObservatoryStore
from .taxonomy import load_taxonomy
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


def _resolve_snapshot_hash(args: argparse.Namespace) -> str:
    """Resolve a subject snapshot hash from an explicit value or the projection."""
    if args.snapshot_hash:
        return args.snapshot_hash
    if not args.db or not args.session:
        raise ObservatoryError(
            "provide --snapshot-hash, or --db and --session so the subject snapshot can be computed"
        )
    session_key = _session_key(args.session)
    with ObservatoryStore(args.db) as store:
        if args.subject_kind == "event":
            if not args.event_id:
                raise ObservatoryError("--event-id is required for --subject-kind event")
            return store.event_snapshot(session_key, args.event_id)
        return store.session_snapshot(session_key)


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
    with ObservatoryStore(args.db) as store:
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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
