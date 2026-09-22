"""Command-line interface for the offline agent observatory.

Only explicitly supplied files are read. There is no automatic live collector
and no home-directory crawling.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .adapters import AdapterContext, read_source
from .canonical import sha256_bytes
from .errors import ObservatoryError
from .inventory import (
    SourceRoot,
    build_manifest,
    discover_sources,
    load_manifest,
    manifest_json,
    records_to_jsonl,
)
from .jev import REQUEST_BYTE_CAP, build_request, import_response, persist_request
from .report import build_report
from .store import ObservatoryStore
from .taxonomy import load_taxonomy


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


def _cmd_build_request(args: argparse.Namespace) -> int:
    taxonomy = load_taxonomy(args.taxonomy)
    state = _read_json_object(args.state, "state")

    if args.snapshot_hash:
        snapshot_hash = args.snapshot_hash
    else:
        if not args.db or not args.session:
            raise ObservatoryError(
                "provide --snapshot-hash, or --db and --session so the subject snapshot can be computed"
            )
        session_key = _session_key(args.session)
        with ObservatoryStore(args.db) as store:
            if args.subject_kind == "event":
                if not args.event_id:
                    raise ObservatoryError("--event-id is required for --subject-kind event")
                snapshot_hash = store.event_snapshot(session_key, args.event_id)
            else:
                snapshot_hash = store.session_snapshot(session_key)

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

    payloads = []
    summaries = []
    for source in inputs:
        result = read_source(source, context=context, provider=args.provider, generation=args.generation)
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
    export_parser.add_argument("--generation", type=int, default=1, help="logical source generation for fallback ids")
    export_parser.add_argument("--out", default=None, help="write JSONL to this path instead of stdout")
    export_parser.add_argument("--db", default=None, help="import the exported JSONL into this projection")
    export_parser.set_defaults(func=_cmd_export)

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
