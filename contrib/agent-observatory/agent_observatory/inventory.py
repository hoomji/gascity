"""Source discovery, coverage-manifest construction, and normalized export.

The plan requires one manifest entry per physical source carrying city/host/
provider/root/repo scope, realpath, size and mtime, a content generation hash,
discovery status, adapter version, checkpoint, error reason and coverage. This
module builds that manifest from **explicit roots only** -- it never crawls a
home directory on its own.

Generation handling: a first observation is generation 1. Re-reading identical
bytes is ``unchanged``; a strict byte-prefix extension is ``appended`` and keeps
the generation (so native and fallback event ids stay stable and replay is
idempotent); anything else is ``rewritten`` and increments the generation,
recording the superseded hash. OpenCode, pi and remote hosts are reported as
unsupported with a reason.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import __version__
from .adapters import (
    ADAPTER_CONTRACT_VERSION,
    ADAPTERS,
    SUPPORTED_PROVIDERS,
    AdapterContext,
    AdapterError,
    adapter_for_path,
    load_source_data,
    validated_records,
)
from .canonical import canonical_json, sha256_bytes, sha256_text
from .errors import ObservatoryError

MANIFEST_VERSION = "1.0"

# Providers deliberately out of scope for the M1 native adapters. Each is kept
# visible in every manifest so coverage gaps are explicit rather than silent.
UNSUPPORTED_PROVIDERS: tuple[dict[str, str], ...] = (
    {
        "provider": "opencode",
        "reason": (
            "OpenCode stores per-lane SQLite transcripts; the M1 adapters are "
            "file-based and do not read a live WAL-less database."
        ),
    },
    {
        "provider": "pi",
        "reason": (
            "pi session transcripts are not covered by the M1 adapters; a "
            "provider-native reader is required before export."
        ),
    },
    {
        "provider": "remote",
        "reason": (
            "remote-host transcripts must be adapted near the source and "
            "transferred with authenticated host access; no remote transport is "
            "implemented in this slice."
        ),
    },
)


@dataclass(frozen=True)
class SourceRoot:
    """One explicit discovery root and an optional forced provider."""

    path: str
    provider: str | None = None


@dataclass(frozen=True)
class DiscoveredSource:
    """One recognized physical transcript file."""

    root: str
    provider: str
    path: str
    realpath: str


def discover_sources(
    roots: Iterable[SourceRoot],
) -> tuple[list[DiscoveredSource], list[dict[str, Any]]]:
    """Return ``(sources, unsupported)`` for the explicit *roots*.

    Directories are walked without following symlinked directories; each file's
    realpath deduplicates physical sources. Roots that do not exist raise
    :class:`ObservatoryError`.
    """

    found: list[DiscoveredSource] = []
    unsupported: list[dict[str, Any]] = []
    seen: set[str] = set()

    for root in roots:
        root_real = os.path.realpath(root.path)
        if not os.path.exists(root_real):
            raise ObservatoryError(f"source root does not exist: {root.path!r}")
        if root.provider is not None and root.provider not in ADAPTERS:
            unsupported.append(_unsupported_entry(root.provider, root.path, root.provider))
            continue
        if os.path.isfile(root_real):
            candidates = [root_real]
        else:
            candidates = []
            for dirpath, _dirnames, filenames in os.walk(root_real, followlinks=False):
                for name in sorted(filenames):
                    candidates.append(os.path.join(dirpath, name))

        for candidate in sorted(candidates):
            if root.provider is None:
                unsupported_kind = _unsupported_kind(candidate)
                if unsupported_kind is not None:
                    unsupported.append(_unsupported_entry(unsupported_kind, candidate, root.path))
                    continue
            provider = root.provider or _provider_for(candidate)
            if provider is None:
                continue
            real = os.path.realpath(candidate)
            if real in seen:
                continue
            seen.add(real)
            found.append(DiscoveredSource(root=root.path, provider=provider, path=candidate, realpath=real))

    found.sort(key=lambda source: source.realpath)
    return found, _dedupe_unsupported(unsupported)


def build_manifest(
    roots: Iterable[SourceRoot],
    *,
    city_id: str,
    host_id: str,
    repo: str | None = None,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic coverage manifest for the explicit *roots*."""

    if not city_id or not host_id:
        raise ObservatoryError("city_id and host_id are required for a source manifest")
    context = AdapterContext(city_id=city_id, host_id=host_id, repo=repo)
    sources, detected_unsupported = discover_sources(roots)
    previous_index = _previous_index(previous)
    entries = [_build_entry(source, context, previous_index.get(source.realpath)) for source in sources]
    unsupported = _merge_unsupported(detected_unsupported)
    return {
        "manifest_version": MANIFEST_VERSION,
        "adapter_contract_version": ADAPTER_CONTRACT_VERSION,
        "generated_by": f"agent-observatory/{__version__}",
        "city_id": city_id,
        "host_id": host_id,
        "repo": repo,
        "supported_providers": list(SUPPORTED_PROVIDERS),
        "sources": entries,
        "unsupported": unsupported,
        "totals": _totals(entries, unsupported),
    }


def manifest_json(manifest: dict[str, Any]) -> str:
    """Serialize a manifest deterministically."""

    return json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def load_manifest(path: str) -> dict[str, Any]:
    """Load a previously written manifest, or raise :class:`ObservatoryError`."""

    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ObservatoryError(f"cannot read manifest {path!r}: {exc}") from exc
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ObservatoryError(f"manifest {path!r} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ObservatoryError(f"manifest {path!r} must contain a JSON object")
    return value


def records_to_jsonl(records: Iterable[dict[str, Any]]) -> str:
    """Render normalized records as canonical JSONL with a trailing newline."""

    lines = [canonical_json(record) for record in records]
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


# -- internals -------------------------------------------------------------


def _provider_for(path: str) -> str | None:
    adapter = adapter_for_path(path)
    return adapter.provider if adapter is not None else None


def _unsupported_kind(path: str) -> str | None:
    name = Path(path).name.lower()
    parts = {part.lower() for part in Path(path).parts}
    if name == "opencode.db" or "opencode-transcripts" in parts or ".opencode" in parts:
        return "opencode"
    if ".pi" in parts:
        return "pi"
    return None


def _unsupported_entry(provider: str, path: str, root: str) -> dict[str, Any]:
    reason = next(
        (entry["reason"] for entry in UNSUPPORTED_PROVIDERS if entry["provider"] == provider),
        f"provider {provider!r} is not supported by the M1 adapters",
    )
    return {"provider": provider, "path": path, "root": root, "reason": reason}


def _merge_unsupported(detected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = [dict(entry) for entry in UNSUPPORTED_PROVIDERS]
    seen = {(entry["provider"], entry.get("path")) for entry in merged}
    for entry in detected:
        key = (entry["provider"], entry.get("path"))
        if key not in seen:
            seen.add(key)
            merged.append(entry)
    return merged


def _dedupe_unsupported(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, Any]] = set()
    result = []
    for entry in entries:
        key = (entry.get("provider"), entry.get("path"))
        if key in seen:
            continue
        seen.add(key)
        result.append(entry)
    return result


def _previous_index(previous: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(previous, dict):
        return {}
    index: dict[str, dict[str, Any]] = {}
    for entry in previous.get("sources", []) or []:
        if isinstance(entry, dict) and isinstance(entry.get("realpath"), str):
            index[entry["realpath"]] = entry
    return index


def _build_entry(
    source: DiscoveredSource,
    context: AdapterContext,
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    adapter_version = ADAPTERS[source.provider].adapter_version
    try:
        stat = os.stat(source.realpath)
        adapter, data, digest = load_source_data(source.path, provider=source.provider)
    except (AdapterError, OSError) as exc:
        return _unreadable_entry(source, adapter_version, exc)

    generation, status, supersedes = _decide_generation(previous, data, digest)
    try:
        result = adapter.parse(
            data,
            context=context,
            generation=generation,
            source_path=source.path,
            source_sha256=digest,
        )
        result.records = validated_records(result.records, source.path)
    except AdapterError as exc:
        return _errored_entry(source, context, adapter_version, data, digest, generation, status, supersedes, stat, exc)

    last_position = result.line_count
    return {
        "source_id": sha256_text(source.realpath)[:32],
        "city_id": context.city_id,
        "host_id": context.host_id,
        "provider": source.provider,
        "root": source.root,
        "path": source.path,
        "realpath": source.realpath,
        "repo": context.repo,
        "scope": "local",
        "size_bytes": len(data),
        "raw_size_bytes": stat.st_size,
        "mtime": _iso_mtime(stat.st_mtime),
        "content_generation": digest,
        "generation": generation,
        "discovery_status": status,
        "adapter_version": f"{source.provider}/{adapter_version}",
        "checkpoint": {
            "generation": generation,
            "generation_sha256": digest,
            "records": len(result.records),
            "last_position": last_position,
        },
        "error_reason": None,
        "coverage": result.coverage(),
        "partial_trailing_line": result.partial_trailing_line,
        "supersedes": supersedes,
    }


def _errored_entry(
    source: DiscoveredSource,
    context: AdapterContext,
    adapter_version: str,
    data: bytes,
    digest: str,
    generation: int,
    status: str,
    supersedes: str | None,
    stat: os.stat_result,
    exc: Exception,
) -> dict[str, Any]:
    return {
        "source_id": sha256_text(source.realpath)[:32],
        "city_id": context.city_id,
        "host_id": context.host_id,
        "provider": source.provider,
        "root": source.root,
        "path": source.path,
        "realpath": source.realpath,
        "repo": context.repo,
        "scope": "local",
        "size_bytes": len(data),
        "raw_size_bytes": stat.st_size,
        "mtime": _iso_mtime(stat.st_mtime),
        "content_generation": digest,
        "generation": generation,
        "discovery_status": "error",
        "adapter_version": f"{source.provider}/{adapter_version}",
        "checkpoint": None,
        "error_reason": str(exc),
        "coverage": None,
        "partial_trailing_line": None,
        "supersedes": supersedes,
    }


def _unreadable_entry(source: DiscoveredSource, adapter_version: str, exc: Exception) -> dict[str, Any]:
    try:
        stat = os.stat(source.realpath)
        size: int | None = stat.st_size
        mtime: str | None = _iso_mtime(stat.st_mtime)
    except OSError:
        size = None
        mtime = None
    return {
        "source_id": sha256_text(source.realpath)[:32],
        "city_id": None,
        "host_id": None,
        "provider": source.provider,
        "root": source.root,
        "path": source.path,
        "realpath": source.realpath,
        "repo": None,
        "scope": "local",
        "size_bytes": size,
        "raw_size_bytes": size,
        "mtime": mtime,
        "content_generation": None,
        "generation": None,
        "discovery_status": "unreadable",
        "adapter_version": f"{source.provider}/{adapter_version}",
        "checkpoint": None,
        "error_reason": str(exc),
        "coverage": None,
        "partial_trailing_line": None,
        "supersedes": None,
    }


def _decide_generation(
    previous: dict[str, Any] | None,
    data: bytes,
    digest: str,
) -> tuple[int, str, str | None]:
    if not isinstance(previous, dict) or not previous.get("content_generation"):
        return 1, "new", None
    previous_generation = previous.get("generation")
    generation = previous_generation if isinstance(previous_generation, int) and previous_generation > 0 else 1
    if previous.get("content_generation") == digest:
        return generation, "unchanged", None
    previous_size = previous.get("size_bytes")
    if isinstance(previous_size, int) and previous_size > 0 and len(data) > previous_size:
        if previous.get("content_generation") == sha256_bytes(data[:previous_size]):
            return generation, "appended", None
    return generation + 1, "rewritten", previous.get("content_generation")


def _iso_mtime(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _totals(entries: list[dict[str, Any]], unsupported: list[dict[str, Any]]) -> dict[str, Any]:
    by_provider: dict[str, int] = {}
    events = sessions = tool_calls = tool_results = usage_events = title_revisions = 0
    partial = 0
    unreadable = 0
    errors = 0
    for entry in entries:
        coverage = entry.get("coverage")
        if not isinstance(coverage, dict):
            unreadable += 1
            continue
        provider = entry.get("provider") or "unknown"
        count = int(coverage.get("records_emitted") or 0)
        by_provider[provider] = by_provider.get(provider, 0) + count
        events += count
        sessions += int(coverage.get("sessions") or 0)
        tool_calls += int(coverage.get("tool_calls") or 0)
        tool_results += int(coverage.get("tool_results") or 0)
        usage_events += int(coverage.get("usage_events") or 0)
        title_revisions += int(coverage.get("title_revisions") or 0)
        if coverage.get("partial_trailing_line"):
            partial += 1
        errors += len(coverage.get("errors") or [])
    return {
        "sources": len(entries),
        "readable_sources": len(entries) - unreadable,
        "unreadable_sources": unreadable,
        "events": events,
        "sessions": sessions,
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "usage_events": usage_events,
        "title_revisions": title_revisions,
        "partial_trailing_lines": partial,
        "errors": errors,
        "by_provider": dict(sorted(by_provider.items())),
        "unsupported_providers": len(unsupported),
    }
