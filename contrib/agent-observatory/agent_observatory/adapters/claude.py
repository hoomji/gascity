"""Adapter for Claude Code transcript files.

Layout: ``~/.claude/projects/<cwd-key>/<sessionId>.jsonl``. Each line is one
event. ``sessionId`` is the current session (and the file stem); the snake-case
``session_id`` carried on API records names the parent/root session the current
one was resumed from, which becomes ``parent_session_id``. ``parentUuid`` links
records into a DAG but has no normalized column, so it is not exported.

Only observable, non-reasoning content is exported: text, tool requests, tool
results and usage. ``thinking``/``redacted_thinking`` blocks and provider meta
records (mode, attachments, file history) are counted as skipped, never stored.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..contract import SCHEMA_VERSION
from .base import (
    AdapterContext,
    AdapterResult,
    SourceAdapter,
    TitleRevision,
    extract_command,
    fallback_event_id,
    split_jsonl,
)
from .redaction import elide_large_text, redact_and_bound, redact_text

# Block types that carry model reasoning and must never be stored by default.
_REASONING_BLOCKS = frozenset({"thinking", "redacted_thinking"})
# Provider housekeeping records with no transcript content.
_META_TYPES = frozenset(
    {
        "mode",
        "last-prompt",
        "atis-latch",
        "worktree-state",
        "file-history-delta",
        "file-history-snapshot",
        "relocated",
        "pr-link",
        "summary",
        "result",
    }
)


class ClaudeAdapter(SourceAdapter):
    """Read-only adapter for Claude Code JSONL transcripts."""

    provider = "claude"
    adapter_version = "1.0.0"

    def detect(self, source_path: str) -> bool:
        parts = Path(source_path).parts
        return source_path.endswith(".jsonl") and ".claude" in parts and "projects" in parts

    def parse(
        self,
        data: bytes,
        *,
        context: AdapterContext,
        generation: int,
        source_path: str,
        source_sha256: str,
    ) -> AdapterResult:
        decoded, partial, errors = split_jsonl(data, source_path)
        result = AdapterResult(
            provider=self.provider,
            adapter_version=self.adapter_version,
            source_path=source_path,
            source_sha256=source_sha256,
            source_size_bytes=len(data),
            session_id=Path(source_path).stem or "unknown",
            parent_session_id=None,
            errors=list(errors),
            partial_trailing_line=partial,
            line_count=len(decoded),
        )

        # Resolve session identity from the first records that carry it.
        for _line, obj in decoded:
            if not isinstance(obj, dict):
                continue
            camel = obj.get("sessionId")
            if result.parent_session_id is None:
                snake = obj.get("session_id")
                if isinstance(camel, str) and isinstance(snake, str) and snake and snake != camel:
                    result.parent_session_id = snake
            if result.session_id == Path(source_path).stem and isinstance(camel, str) and camel:
                result.session_id = camel

        # Claude emits one JSONL record per content block while repeating the
        # same ``message.id`` and usage block, so usage is attached at most once
        # per distinct message id.
        seen_usage: set[str] = set()
        for line_number, obj in decoded:
            if not isinstance(obj, dict):
                result.note_skip("non_object")
                continue
            kind = obj.get("type")
            if kind == "assistant":
                self._parse_assistant(obj, line_number, context, generation, seen_usage, result)
            elif kind == "user":
                self._parse_user(obj, line_number, context, generation, result)
            elif kind == "system":
                self._parse_system(obj, line_number, context, generation, result)
            elif kind == "ai-title":
                title = obj.get("aiTitle")
                if isinstance(title, str) and title.strip():
                    result.title_revisions.append(
                        TitleRevision(
                            title=redact_text(title),
                            position=line_number,
                            observed_timestamp=obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else None,
                            source="ai-title",
                        )
                    )
                else:
                    result.note_skip("title_empty")
            elif kind in _META_TYPES:
                result.note_skip(f"meta:{kind}")
            else:
                result.note_skip(f"type:{kind}")

        return result

    # -- record plumbing ---------------------------------------------------

    def _record(
        self,
        context: AdapterContext,
        session_id: str,
        parent_session_id: str | None,
        *,
        event_id: str,
        timestamp: str,
        kind: str,
        model: str | None = None,
        usage: dict[str, Any] | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        record = {
            "schema_version": SCHEMA_VERSION,
            "city_id": context.city_id,
            "host_id": context.host_id,
            "provider": self.provider,
            "session_id": session_id,
            "event_id": event_id,
            "timestamp": timestamp,
            "kind": kind,
            "title": None,
            "text": None,
            "tool_name": None,
            "tool_call_id": None,
            "command": None,
            "exit_code": None,
            "duration_ms": None,
            "model": model,
            "repo": context.repo,
            "commit_sha": None,
            "parent_session_id": parent_session_id,
            "bead_id": None,
            "formula_id": None,
            "usage": usage,
        }
        record.update(fields)
        return record

    def _finalize(self, raw: dict[str, Any], result: AdapterResult) -> None:
        result.records.append(raw)

    # -- content handlers --------------------------------------------------

    def _parse_assistant(
        self,
        obj: dict[str, Any],
        line_number: int,
        context: AdapterContext,
        generation: int,
        seen_usage: set[str],
        result: AdapterResult,
    ) -> None:
        timestamp = obj.get("timestamp")
        if not isinstance(timestamp, str) or not timestamp:
            result.note_skip("no_timestamp")
            return
        message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        model = message.get("model") if isinstance(message.get("model"), str) else None
        usage = _claude_usage(message.get("usage"))
        message_id = message.get("id") if isinstance(message.get("id"), str) else None
        attach_usage = usage is not None and (message_id is None or message_id not in seen_usage)
        uuid = obj.get("uuid") if isinstance(obj.get("uuid"), str) else None
        pending: list[tuple[dict[str, Any], int]] = []

        blocks = _content_blocks(message.get("content"))
        for index, block in enumerate(blocks):
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    pending.append(
                        (
                            self._record(
                                context,
                                result.session_id,
                                result.parent_session_id,
                                event_id=_claude_event_id(uuid, f"text:{index}", self.provider, generation, line_number, "message", block),
                                timestamp=timestamp,
                                kind="message",
                                model=model,
                                text=redact_and_bound(text),
                            ),
                            line_number,
                        )
                    )
            elif block_type == "tool_use":
                tool_id = block.get("id") if isinstance(block.get("id"), str) else None
                tool_name = block.get("name") if isinstance(block.get("name"), str) else None
                arguments = block.get("input")
                command = extract_command(tool_name, arguments)
                tool_text = _json_text(arguments)
                pending.append(
                    (
                        self._record(
                            context,
                            result.session_id,
                            result.parent_session_id,
                            event_id=tool_id or _claude_event_id(uuid, f"tool_use:{index}", self.provider, generation, line_number, "tool_call", block),
                            timestamp=timestamp,
                            kind="tool_call",
                            model=model,
                            tool_name=tool_name,
                            tool_call_id=tool_id,
                            command=elide_large_text(command) if command else None,
                            text=elide_large_text(tool_text) if tool_text else None,
                        ),
                        line_number,
                    )
                )
            elif block_type in _REASONING_BLOCKS:
                result.note_skip("reasoning")
            else:
                result.note_skip(f"block:{block_type}")

        if not pending and attach_usage and uuid:
            pending.append(
                (
                    self._record(
                        context,
                        result.session_id,
                        result.parent_session_id,
                        event_id=_claude_event_id(uuid, "message", self.provider, generation, line_number, "assistant_message", obj),
                        timestamp=timestamp,
                        kind="assistant_message",
                        model=model,
                    ),
                    line_number,
                )
            )

        if pending:
            if attach_usage and usage is not None:
                pending[0][0]["usage"] = usage
                if message_id is not None:
                    seen_usage.add(message_id)
            for raw, _line in pending:
                self._finalize(raw, result)

    def _parse_user(
        self,
        obj: dict[str, Any],
        line_number: int,
        context: AdapterContext,
        generation: int,
        result: AdapterResult,
    ) -> None:
        timestamp = obj.get("timestamp")
        if not isinstance(timestamp, str) or not timestamp:
            result.note_skip("no_timestamp")
            return
        message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        uuid = obj.get("uuid") if isinstance(obj.get("uuid"), str) else None
        tool_use_result = obj.get("toolUseResult") if isinstance(obj.get("toolUseResult"), dict) else {}
        exit_code = _int_or_none(tool_use_result.get("exitCode", tool_use_result.get("exit_code")))
        duration_ms = _int_or_none(tool_use_result.get("durationMs", tool_use_result.get("duration_ms")))
        content = message.get("content")
        blocks = _content_blocks(content)
        if not blocks and isinstance(content, str):
            blocks = [{"type": "text", "text": content}]

        pending: list[dict[str, Any]] = []
        for index, block in enumerate(blocks):
            block_type = block.get("type")
            if block_type == "tool_result":
                tool_call_id = block.get("tool_use_id") if isinstance(block.get("tool_use_id"), str) else None
                text = _tool_result_text(block, tool_use_result)
                pending.append(
                    self._record(
                        context,
                        result.session_id,
                        result.parent_session_id,
                        event_id=_claude_event_id(uuid, f"tool_result:{index}", self.provider, generation, line_number, "tool_result", block),
                        timestamp=timestamp,
                        kind="tool_result",
                        tool_name=None,
                        tool_call_id=tool_call_id,
                        exit_code=exit_code,
                        duration_ms=duration_ms,
                        text=elide_large_text(text) if text else None,
                    )
                )
            elif block_type == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    pending.append(
                        self._record(
                            context,
                            result.session_id,
                            result.parent_session_id,
                            event_id=_claude_event_id(uuid, f"text:{index}", self.provider, generation, line_number, "message", block),
                            timestamp=timestamp,
                            kind="message",
                            text=redact_and_bound(text),
                        )
                    )
            elif block_type == "tool_use":
                tool_id = block.get("id") if isinstance(block.get("id"), str) else None
                tool_name = block.get("name") if isinstance(block.get("name"), str) else None
                arguments = block.get("input")
                command = extract_command(tool_name, arguments)
                pending.append(
                    self._record(
                        context,
                        result.session_id,
                        result.parent_session_id,
                        event_id=tool_id or _claude_event_id(uuid, f"tool_use:{index}", self.provider, generation, line_number, "tool_call", block),
                        timestamp=timestamp,
                        kind="tool_call",
                        tool_name=tool_name,
                        tool_call_id=tool_id,
                        command=elide_large_text(command) if command else None,
                    )
                )
            elif block_type in _REASONING_BLOCKS:
                result.note_skip("reasoning")
            elif block_type in {"image", "document"}:
                result.note_skip(f"block:{block_type}")
            else:
                result.note_skip(f"block:{block_type}")
        for raw in pending:
            self._finalize(raw, result)

    def _parse_system(
        self,
        obj: dict[str, Any],
        line_number: int,
        context: AdapterContext,
        generation: int,
        result: AdapterResult,
    ) -> None:
        timestamp = obj.get("timestamp")
        if not isinstance(timestamp, str) or not timestamp:
            result.note_skip("no_timestamp")
            return
        text = _flatten_text(obj.get("content"))
        if not text:
            result.note_skip("system_empty")
            return
        uuid = obj.get("uuid") if isinstance(obj.get("uuid"), str) else None
        raw = self._record(
            context,
            result.session_id,
            result.parent_session_id,
            event_id=_claude_event_id(uuid, "system", self.provider, generation, line_number, "system", obj),
            timestamp=timestamp,
            kind="system",
            text=redact_and_bound(text),
        )
        self._finalize(raw, result)


def _claude_event_id(
    uuid: str | None,
    suffix: str,
    provider: str,
    generation: int,
    line_number: int,
    kind: str,
    raw: Any,
) -> str:
    if uuid:
        return f"{uuid}:{suffix}"
    return fallback_event_id(provider, generation, line_number, kind, raw)


def _content_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def _tool_result_text(block: dict[str, Any], tool_use_result: dict[str, Any]) -> str:
    text = _flatten_text(block.get("content"))
    if text:
        return text
    stdout = tool_use_result.get("stdout")
    stderr = tool_use_result.get("stderr")
    parts = [part for part in (stdout, stderr) if isinstance(part, str) and part]
    return "\n".join(parts)


def _flatten_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return ""


def _json_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _claude_usage(usage: Any) -> dict[str, int | None] | None:
    if not isinstance(usage, dict):
        return None
    input_tokens = _int_or_none(usage.get("input_tokens"))
    output_tokens = _int_or_none(usage.get("output_tokens"))
    cache_read = _int_or_none(usage.get("cache_read_input_tokens"))
    cache_write = _int_or_none(usage.get("cache_creation_input_tokens"))
    components = [value for value in (input_tokens, output_tokens, cache_read, cache_write) if value is not None]
    if not components:
        return None
    total = sum(components)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "total_tokens": total,
    }


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
