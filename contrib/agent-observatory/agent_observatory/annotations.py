"""Versioned gold annotations, kept strictly separate from model predictions.

Human labels are independent annotations, not a rewrite of model output. They
live in their own append-only records keyed by content, so a correction creates a
new versioned row instead of overwriting the old label or any prediction. Every
annotation records the taxonomy version, the annotator, the adjudication mode
and explicit flags (``uncertain``, ``injected``, ``contested``, ``rare``) that
keep ambiguous or adversarial cases out of automatic routing.

The on-disk gold set is a JSON document::

    {
      "schema_version": "1.0",
      "gold_set_version": "pilot-1",
      "taxonomy_version": "2.0.0",
      "episodes": [
        {
          "episode_id": "...",
          "group_key": "bead:gl-123",
          "observed_at": "2026-09-21T19:20:30.904000Z",
          "provider": "codex",
          "repo": "gascity",
          "title": "Fix flaky scheduler test",
          "labels": {"primary_intent": ["bugfix"], "target": ["tests"]},
          "flags": [],
          "annotator": "mayor",
          "adjudication": "single",
          "metadata": {"bead_kind": "bug"}
        }
      ]
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .canonical import canonical_hash, canonical_json
from .contract import normalize_timestamp
from .errors import AnnotationError
from .taxonomy import Taxonomy

GOLD_SCHEMA_VERSION = "1.0"
SUPPORTED_GOLD_SCHEMA_VERSIONS = ("1.0",)
KNOWN_FLAGS = frozenset(
    {"uncertain", "injected", "rare", "contested", "non_english", "synthetic", "changed_intent"}
)
ADJUDICATIONS = ("single", "adjudicated", "disagreement")


@dataclass(frozen=True)
class GoldEpisode:
    """One human-annotated episode."""

    episode_id: str
    group_key: str
    observed_at: str
    provider: str
    labels: Mapping[str, tuple[str, ...]]
    annotator: str
    adjudication: str = "single"
    repo: str | None = None
    title: str | None = None
    flags: frozenset[str] = frozenset()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def label_set(self, facet_id: str) -> tuple[str, ...]:
        return tuple(self.labels.get(facet_id, ()))

    def primary(self, facet_id: str = "primary_intent") -> str | None:
        values = self.label_set(facet_id)
        return values[0] if values else None

    def content(self) -> dict[str, Any]:
        """Canonical content used for the annotation hash (no provenance)."""
        return {
            "episode_id": self.episode_id,
            "group_key": self.group_key,
            "observed_at": self.observed_at,
            "provider": self.provider,
            "repo": self.repo,
            "title": self.title,
            "labels": {key: list(value) for key, value in sorted(self.labels.items())},
            "flags": sorted(self.flags),
        }

    def annotation_hash(self) -> str:
        return canonical_hash(["annotation", self.content()])


@dataclass(frozen=True)
class GoldSet:
    """A pinned, versioned set of gold annotations."""

    gold_set_version: str
    taxonomy_version: str
    facet_hash: str
    episodes: tuple[GoldEpisode, ...]
    schema_version: str = GOLD_SCHEMA_VERSION

    def by_id(self) -> dict[str, GoldEpisode]:
        return {episode.episode_id: episode for episode in self.episodes}

    def group_keys(self) -> tuple[str, ...]:
        return tuple(sorted({episode.group_key for episode in self.episodes}))

    def gold_set_hash(self) -> str:
        return canonical_hash(
            {
                "schema_version": self.schema_version,
                "gold_set_version": self.gold_set_version,
                "taxonomy_version": self.taxonomy_version,
                "facet_hash": self.facet_hash,
                "episodes": [episode.content() for episode in self.episodes],
            }
        )

    def to_json(self) -> str:
        return canonical_json(
            {
                "schema_version": self.schema_version,
                "gold_set_version": self.gold_set_version,
                "taxonomy_version": self.taxonomy_version,
                "episodes": [
                    {
                        **episode.content(),
                        "annotator": episode.annotator,
                        "adjudication": episode.adjudication,
                        "metadata": dict(episode.metadata),
                    }
                    for episode in self.episodes
                ],
            }
        )


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant {value!r} is not allowed")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AnnotationError(message)


def _parse_labels(
    raw: Any, taxonomy: Taxonomy, episode_id: str
) -> dict[str, tuple[str, ...]]:
    _require(isinstance(raw, dict), f"episode {episode_id!r} labels must be an object")
    facets = taxonomy.facet_by_id()
    _require(bool(facets), "gold annotations require a taxonomy with facets")
    labels: dict[str, tuple[str, ...]] = {}
    for facet_id, values in raw.items():
        _require(
            facet_id in facets,
            f"episode {episode_id!r} names unknown facet {facet_id!r}",
        )
        _require(
            isinstance(values, list) and all(isinstance(value, str) for value in values),
            f"episode {episode_id!r} facet {facet_id!r} must be a list of strings",
        )
        _require(
            len(set(values)) == len(values),
            f"episode {episode_id!r} facet {facet_id!r} contains duplicate labels",
        )
        allowed = taxonomy.label_keys(facet_id)
        unknown = sorted(set(values) - allowed)
        _require(
            not unknown,
            f"episode {episode_id!r} facet {facet_id!r} has unknown labels: "
            + ", ".join(unknown),
        )
        if facets[facet_id].cardinality == "one":
            _require(
                len(values) == 1,
                f"episode {episode_id!r} facet {facet_id!r} is single-valued and "
                f"requires exactly one label",
            )
        labels[facet_id] = tuple(values)
    return labels


def _parse_episode(raw: Any, taxonomy: Taxonomy) -> GoldEpisode:
    _require(isinstance(raw, dict), "each gold episode must be an object")
    episode_id = raw.get("episode_id")
    _require(isinstance(episode_id, str) and episode_id, "episode_id is required")
    group_key = raw.get("group_key")
    _require(
        isinstance(group_key, str) and group_key,
        f"episode {episode_id!r} group_key is required",
    )
    observed_at = raw.get("observed_at")
    _require(
        isinstance(observed_at, str) and observed_at,
        f"episode {episode_id!r} observed_at is required",
    )
    normalized_time = normalize_timestamp(observed_at)
    provider = raw.get("provider")
    _require(
        isinstance(provider, str) and provider,
        f"episode {episode_id!r} provider is required",
    )
    annotator = raw.get("annotator")
    _require(
        isinstance(annotator, str) and annotator,
        f"episode {episode_id!r} annotator is required",
    )
    adjudication = raw.get("adjudication", "single")
    _require(
        adjudication in ADJUDICATIONS,
        f"episode {episode_id!r} adjudication must be one of "
        + ", ".join(ADJUDICATIONS),
    )
    flags = raw.get("flags", [])
    _require(
        isinstance(flags, list) and all(isinstance(flag, str) for flag in flags),
        f"episode {episode_id!r} flags must be a list of strings",
    )
    _require(
        len(set(flags)) == len(flags),
        f"episode {episode_id!r} flags contain duplicates",
    )
    unknown_flags = sorted(set(flags) - KNOWN_FLAGS)
    _require(
        not unknown_flags,
        f"episode {episode_id!r} has unknown flags: " + ", ".join(unknown_flags),
    )
    repo = raw.get("repo")
    _require(
        repo is None or isinstance(repo, str),
        f"episode {episode_id!r} repo must be a string or null",
    )
    title = raw.get("title")
    _require(
        title is None or isinstance(title, str),
        f"episode {episode_id!r} title must be a string or null",
    )
    metadata = raw.get("metadata", {})
    _require(
        isinstance(metadata, dict),
        f"episode {episode_id!r} metadata must be an object",
    )
    labels = _parse_labels(raw.get("labels"), taxonomy, episode_id)
    return GoldEpisode(
        episode_id=episode_id,
        group_key=group_key,
        observed_at=normalized_time,
        provider=provider,
        labels=labels,
        annotator=annotator,
        adjudication=adjudication,
        repo=repo,
        title=title,
        flags=frozenset(flags),
        metadata=metadata,
    )


def load_gold_set(
    path: str | Path,
    taxonomy: Taxonomy,
    *,
    expected_taxonomy_version: str | None = None,
) -> GoldSet:
    """Load and validate a pinned gold annotation set."""
    gold_path = Path(path)
    try:
        raw = json.loads(gold_path.read_text(encoding="utf-8"), parse_constant=_reject_json_constant)
    except OSError as exc:
        raise AnnotationError(f"cannot read gold set {gold_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AnnotationError(f"gold set {gold_path} is not valid JSON: {exc}") from exc
    except ValueError as exc:
        raise AnnotationError(f"gold set {gold_path} contains a non-finite number: {exc}") from exc

    _require(isinstance(raw, dict), "gold set must be a JSON object")
    schema_version = raw.get("schema_version")
    _require(
        schema_version in SUPPORTED_GOLD_SCHEMA_VERSIONS,
        f"unsupported gold schema_version {schema_version!r}; supported: "
        + ", ".join(SUPPORTED_GOLD_SCHEMA_VERSIONS),
    )
    gold_set_version = raw.get("gold_set_version")
    _require(
        isinstance(gold_set_version, str) and gold_set_version,
        "gold_set_version is required",
    )
    taxonomy_version = raw.get("taxonomy_version")
    _require(
        isinstance(taxonomy_version, str) and taxonomy_version,
        "taxonomy_version is required",
    )
    _require(
        taxonomy_version == taxonomy.taxonomy_version,
        f"gold taxonomy_version {taxonomy_version!r} does not match taxonomy "
        f"{taxonomy.taxonomy_version!r}",
    )
    if expected_taxonomy_version is not None:
        _require(
            taxonomy_version == expected_taxonomy_version,
            f"gold taxonomy_version {taxonomy_version!r} does not match pinned "
            f"{expected_taxonomy_version!r}",
        )
    raw_episodes = raw.get("episodes")
    _require(isinstance(raw_episodes, list) and raw_episodes, "gold set requires episodes")
    episodes = tuple(_parse_episode(entry, taxonomy) for entry in raw_episodes)
    ids = [episode.episode_id for episode in episodes]
    duplicates = sorted({value for value in ids if ids.count(value) > 1})
    _require(not duplicates, "duplicate gold episode ids: " + ", ".join(duplicates))

    return GoldSet(
        gold_set_version=gold_set_version,
        taxonomy_version=taxonomy_version,
        facet_hash=taxonomy.facet_hash(),
        episodes=episodes,
    )


def save_gold_annotations(store: Any, gold_set: GoldSet) -> int:
    """Append gold annotations to the projection; returns rows newly stored.

    Annotations are append-only and content-keyed. Re-saving an identical set is
    a no-op; a correction for the same episode is stored as an additional row so
    the prior label remains visible.
    """
    return store.save_gold_annotations(gold_set)


def iter_gold_annotations(store: Any) -> Iterator[dict[str, Any]]:
    """Yield stored annotation rows ordered by insertion."""
    return store.iter_gold_annotations()
