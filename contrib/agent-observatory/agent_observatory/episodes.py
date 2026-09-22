"""Deterministic episode segmentation over normalized evidence.

A *session* is a provider-native conversation; an *episode* is one bounded task
inside it. Sessions accumulate unrelated tasks, resume, branch, compact and move
between hosts, so a single session label loses the task transitions that
requirements R1 calls out. Segmentation splits ``events`` into episodes at
explicit task boundaries and assigns each episode a stable identity and a
*continuation group* used by leak-free evaluation splits.

Segmentation is deliberately rule-based and inspectable, not semantic:

* an episode starts at an event whose ``kind`` is a configured task-boundary
  kind (``user``/``user_request``/``user_excerpt``/``task`` by default) and runs
  until the next boundary (inclusive of that boundary's event);
* a session with no boundary event is a single episode, never silently dropped;
* when the caller asks for it, a change of ``bead_id``/``formula_id`` also starts
  a new episode.

The episode id is a hash of the session identity plus the first event id, so
re-segmenting the same evidence is stable and two sessions can never collide.
Titles and free text are never part of identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .canonical import canonical_hash, identity_key
from .contract import normalize_timestamp
from .errors import EpisodeError

# Kinds that mark the start of a new user task request by default. GC boot
# prompts and orchestration nudges can also arrive as ``user`` events; callers
# that can distinguish them should pass an explicit narrower ``boundary_kinds``
# or pre-filter before segmenting.
DEFAULT_BOUNDARY_KINDS = ("user", "user_request", "user_excerpt", "task")

_SESSION_IDENTITY_WIDTH = 4


@dataclass(frozen=True)
class EpisodeConfig:
    """Rules that decide where a new episode begins."""

    boundary_kinds: tuple[str, ...] = DEFAULT_BOUNDARY_KINDS
    split_on_work_anchor_change: bool = False


@dataclass(frozen=True)
class Episode:
    """One bounded task inside a session."""

    episode_id: str
    session_key: tuple[str, str, str, str]
    group_key: str
    start_event_id: str
    end_event_id: str
    event_ids: tuple[str, ...]
    started_at: str
    ended_at: str
    provider: str
    repo: str | None = None
    work_anchor: str | None = None

    @property
    def event_count(self) -> int:
        return len(self.event_ids)


def _ordered(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    materialized = list(events)
    for event in materialized:
        if not isinstance(event, dict):
            raise EpisodeError("episode events must be normalized event objects")
        if not event.get("event_id") or not event.get("timestamp"):
            raise EpisodeError("episode events require event_id and timestamp")
    return sorted(materialized, key=lambda event: (event["timestamp"], event["event_id"]))


def _work_anchor(events: Sequence[dict[str, Any]]) -> str | None:
    """Return a shared bead/formula anchor, or None when the window is mixed.

    Partial evidence is allowed: exactly one distinct non-null bead id (or, when
    no bead id is present, formula id) anchors the episode. Two conflicting ids
    make the window mixed rather than silently picking one.
    """

    for field, prefix in (("bead_id", "bead:"), ("formula_id", "formula:")):
        values = {event.get(field) for event in events if event.get(field) is not None}
        if len(values) == 1:
            return prefix + next(iter(values))
    return None


def _make_episode(
    session_key: Sequence[str],
    events: Sequence[dict[str, Any]],
    *,
    group_key: str | None = None,
) -> Episode:
    key = tuple(session_key)  # type: ignore[assignment]
    if len(key) != _SESSION_IDENTITY_WIDTH:
        raise EpisodeError(
            f"session identity must have {_SESSION_IDENTITY_WIDTH} components, got {len(key)}"
        )
    ordered = list(events)
    start = ordered[0]
    end = ordered[-1]
    repo = next((event.get("repo") for event in ordered if event.get("repo")), None)
    anchor = _work_anchor(ordered)
    if group_key is None:
        if anchor is not None:
            group_key = anchor
        else:
            group_key = "session:" + identity_key(key)
    episode_id = canonical_hash(["episode", list(key), start["event_id"]])
    return Episode(
        episode_id=episode_id,
        session_key=key,  # type: ignore[arg-type]
        group_key=group_key,
        start_event_id=start["event_id"],
        end_event_id=end["event_id"],
        event_ids=tuple(event["event_id"] for event in ordered),
        started_at=start["timestamp"],
        ended_at=end["timestamp"],
        provider=key[2],
        repo=repo,
        work_anchor=anchor,
    )


def segment_events(
    session_key: Sequence[str],
    events: Iterable[dict[str, Any]],
    config: EpisodeConfig | None = None,
) -> tuple[Episode, ...]:
    """Split *events* into episodes using *config*; never drops an event.

    ``events`` must come from one session and are sorted defensively by
    ``(timestamp, event_id)``. Every input event lands in exactly one episode.
    """
    rules = config or EpisodeConfig()
    ordered = _ordered(events)
    if not ordered:
        return ()

    episodes: list[Episode] = []
    current: list[dict[str, Any]] = []
    current_anchor: str | None = None

    for event in ordered:
        anchor = event.get("bead_id") or event.get("formula_id")
        starts_task = event.get("kind") in rules.boundary_kinds
        anchor_changed = (
            rules.split_on_work_anchor_change
            and current
            and anchor != current_anchor
            and (anchor is not None or current_anchor is not None)
        )
        if current and (starts_task or anchor_changed):
            episodes.append(_make_episode(session_key, current))
            current = []
        current.append(event)
        current_anchor = anchor

    if current:
        episodes.append(_make_episode(session_key, current))
    return tuple(episodes)


def _session_key_tuple(session_key: Sequence[str]) -> tuple[str, str, str, str]:
    key = tuple(session_key)
    if len(key) != _SESSION_IDENTITY_WIDTH or not all(isinstance(part, str) and part for part in key):
        raise EpisodeError("session identity must be four non-empty strings")
    return key  # type: ignore[return-value]


def lineage_root_key(store: Any, session_key: Sequence[str]) -> tuple[str, str, str, str]:
    """Resolve the oldest known ancestor session for *session_key*.

    Resumed/subagent sessions must not leak across an evaluation split. The
    parent link is only a session id, so it is resolved within the same
    city/host/provider namespace. Unknown parents stop the walk; a cycle is
    detected and rejected.
    """
    key = _session_key_tuple(session_key)
    seen: set[tuple[str, str, str, str]] = set()
    while True:
        if key in seen:
            raise EpisodeError(f"session parent cycle detected at {identity_key(key)}")
        seen.add(key)
        row = store.conn.execute(
            "SELECT parent_session_id FROM sessions WHERE city_id = ? AND host_id = ? "
            "AND provider = ? AND session_id = ?",
            key,
        ).fetchone()
        parent = row["parent_session_id"] if row is not None else None
        if not parent:
            return key
        candidate = (key[0], key[1], key[2], parent)
        exists = store.conn.execute(
            "SELECT 1 FROM sessions WHERE city_id = ? AND host_id = ? AND provider = ? "
            "AND session_id = ?",
            candidate,
        ).fetchone()
        if exists is None:
            return key
        key = candidate


def segment_session(
    store: Any,
    session_key: Sequence[str],
    config: EpisodeConfig | None = None,
) -> tuple[Episode, ...]:
    """Segment one session using the projection, grouping by lineage root.

    Only the supplied session's events become episodes; the lineage root is used
    solely as the continuation-group key so a resumed/subagent session cannot
    leak across a later evaluation split.
    """
    root = lineage_root_key(store, session_key)
    events = store.session_events(session_key)
    episodes = segment_events(session_key, events, config)
    group_key = "session:" + identity_key(root)
    return tuple(
        Episode(
            episode_id=episode.episode_id,
            session_key=episode.session_key,
            group_key=group_key,
            start_event_id=episode.start_event_id,
            end_event_id=episode.end_event_id,
            event_ids=episode.event_ids,
            started_at=episode.started_at,
            ended_at=episode.ended_at,
            provider=episode.provider,
            repo=episode.repo,
            work_anchor=episode.work_anchor,
        )
        for episode in episodes
    )


def segment_store(store: Any, config: EpisodeConfig | None = None) -> tuple[Episode, ...]:
    """Segment every session in the projection, ordered deterministically."""
    episodes: list[Episode] = []
    for session_key in store.session_keys():
        episodes.extend(segment_session(store, session_key, config))
    return tuple(
        sorted(episodes, key=lambda episode: (episode.started_at, episode.episode_id))
    )


def normalize_observed_at(value: str) -> str:
    """Normalize an episode/annotation timestamp with the import rules."""
    return normalize_timestamp(value)
