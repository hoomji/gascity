"""Read-only native transcript adapters.

Three providers are covered: Claude Code (``claude``), Codex (``codex``) and
dsh (``dsh``). OpenCode, pi and remote hosts are explicitly unsupported; see
:mod:`agent_observatory.inventory` for the manifest reasons.

Adapters read explicit paths only. They never crawl a home directory, never
mutate a source, and never execute transcript content.
"""

from __future__ import annotations

from pathlib import Path

from ..canonical import sha256_bytes
from ..contract import validate_record
from ..errors import ContractError
from .base import (
    ADAPTER_CONTRACT_VERSION,
    AdapterContext,
    AdapterError,
    AdapterResult,
    SourceAdapter,
    TitleRevision,
)
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .dsh import DshAdapter

_ORDERED_ADAPTERS: tuple[SourceAdapter, ...] = (
    ClaudeAdapter(),
    CodexAdapter(),
    DshAdapter(),
)

ADAPTERS: dict[str, SourceAdapter] = {adapter.provider: adapter for adapter in _ORDERED_ADAPTERS}
SUPPORTED_PROVIDERS: tuple[str, ...] = tuple(adapter.provider for adapter in _ORDERED_ADAPTERS)


def get_adapter(provider: str) -> SourceAdapter:
    """Return the adapter for *provider*, or raise :class:`AdapterError`."""

    try:
        return ADAPTERS[provider]
    except KeyError as exc:
        raise AdapterError(
            f"unsupported provider {provider!r}; supported: {', '.join(SUPPORTED_PROVIDERS)}"
        ) from exc


def adapter_for_path(source_path: str) -> SourceAdapter | None:
    """Return the first adapter that recognizes *source_path*, if any."""

    for adapter in _ORDERED_ADAPTERS:
        if adapter.detect(str(source_path)):
            return adapter
    return None


def load_source_data(source_path: str, *, provider: str | None = None) -> tuple[SourceAdapter, bytes, str]:
    """Read and decompress one transcript.

    Returns ``(adapter, logical_bytes, sha256)`` so callers can decide the
    logical source generation before parsing (needed for rewrite detection).
    """

    path = Path(source_path)
    adapter = get_adapter(provider) if provider is not None else adapter_for_path(source_path)
    if adapter is None:
        raise AdapterError(f"no adapter recognizes source {source_path!r}", source_path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AdapterError(f"cannot read source: {exc}", source_path) from exc
    data = adapter.decompress(raw, source_path)
    return adapter, data, sha256_bytes(data)


def read_source(
    source_path: str,
    *,
    context: AdapterContext,
    provider: str | None = None,
    generation: int = 1,
) -> AdapterResult:
    """Read one transcript and return validated normalized records.

    *generation* is the logical source generation (1 for a first read). It is
    only used for events that lack a provider-native id and for manifest
    supersession, not for events with stable native identity. It must be a
    positive integer; an invalid value is refused rather than embedded in
    fallback event ids.
    """

    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise AdapterError(f"generation must be a positive integer, got {generation!r}")

    adapter, data, digest = load_source_data(source_path, provider=provider)
    result = adapter.parse(
        data,
        context=context,
        generation=generation,
        source_path=source_path,
        source_sha256=digest,
    )
    result.source_size_bytes = len(data)
    result.records = validated_records(result.records, source_path, result)
    return result


def validated_records(records: list[dict], source_path: str, result: AdapterResult | None = None) -> list[dict]:
    """Return contract-validated copies of adapter-produced *records*.

    A record that violates the normalized contract is isolated: it is counted
    as ``invalid_record`` on *result* (with the underlying reason recorded in
    its errors) and skipped, so one provider quirk cannot erase a whole
    session's evidence. The remaining records are returned.
    """

    validated: list[dict] = []
    for record in records:
        try:
            validated.append(_validated(record, source_path))
        except AdapterError as exc:
            if result is not None:
                # Prefer the underlying ContractError reason; ``_validated`` only
                # wraps it so a caller that wants a hard failure still can.
                reason = str(exc.__cause__) if exc.__cause__ is not None else str(exc)
                result.note_skip("invalid_record", reason)
            continue
    return validated


def _validated(record: dict, source_path: str) -> dict:
    try:
        normalized = validate_record(record, source_path)
    except ContractError as exc:
        raise AdapterError(f"adapter produced an invalid record: {exc}", source_path) from exc
    normalized.pop("observed_timestamp", None)
    return normalized


__all__ = [
    "ADAPTER_CONTRACT_VERSION",
    "ADAPTERS",
    "SUPPORTED_PROVIDERS",
    "AdapterContext",
    "AdapterError",
    "AdapterResult",
    "SourceAdapter",
    "TitleRevision",
    "adapter_for_path",
    "get_adapter",
    "load_source_data",
    "read_source",
    "validated_records",
]
