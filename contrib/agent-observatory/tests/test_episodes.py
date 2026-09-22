"""Tests for deterministic episode segmentation."""

from __future__ import annotations

import os
import tempfile
import unittest

try:
    from . import support
except ImportError:  # pragma: no cover
    import support

from agent_observatory.episodes import (
    EpisodeConfig,
    lineage_root_key,
    segment_events,
    segment_session,
    segment_store,
)
from agent_observatory.store import ObservatoryStore

SESSION = ("city-a", "host-a", "codex", "session-1")


def event(event_id, kind, *, timestamp, **overrides):
    overrides.setdefault("session_id", SESSION[3])
    record = support.make_record(
        event_id=event_id,
        kind=kind,
        timestamp=timestamp,
        **overrides,
    )
    return record


class EpisodeSegmentationTest(unittest.TestCase):
    def test_splits_at_task_boundaries_and_covers_every_event(self):
        events = [
            event("e1", "user", timestamp="2026-09-01T00:00:00Z"),
            event("e2", "command", timestamp="2026-09-01T00:01:00Z"),
            event("e3", "user", timestamp="2026-09-01T00:02:00Z"),
            event("e4", "command", timestamp="2026-09-01T00:03:00Z"),
            event("e5", "command", timestamp="2026-09-01T00:04:00Z"),
        ]
        episodes = segment_events(SESSION, events)
        self.assertEqual(len(episodes), 2)
        self.assertEqual([ep.event_ids for ep in episodes], [("e1", "e2"), ("e3", "e4", "e5")])
        flattened = [event_id for episode in episodes for event_id in episode.event_ids]
        self.assertEqual(flattened, ["e1", "e2", "e3", "e4", "e5"])

    def test_session_without_boundary_is_one_episode(self):
        events = [
            event("e1", "command", timestamp="2026-09-01T00:00:00Z"),
            event("e2", "command", timestamp="2026-09-01T00:01:00Z"),
        ]
        episodes = segment_events(SESSION, events)
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0].event_ids, ("e1", "e2"))

    def test_empty_session_yields_no_episodes(self):
        self.assertEqual(segment_events(SESSION, []), ())

    def test_episode_ids_are_stable_and_collision_free(self):
        events = [event("e1", "user", timestamp="2026-09-01T00:00:00Z")]
        first = segment_events(SESSION, events)[0]
        second = segment_events(SESSION, events)[0]
        self.assertEqual(first.episode_id, second.episode_id)
        other_session = ("city-a", "host-a", "codex", "session-2")
        other = segment_events(other_session, [event("e1", "user", timestamp="2026-09-01T00:00:00Z")])[0]
        self.assertNotEqual(first.episode_id, other.episode_id)

    def test_events_are_sorted_before_segmentation(self):
        events = [
            event("e3", "user", timestamp="2026-09-01T00:02:00Z"),
            event("e1", "user", timestamp="2026-09-01T00:00:00Z"),
            event("e2", "command", timestamp="2026-09-01T00:01:00Z"),
        ]
        episodes = segment_events(SESSION, events)
        self.assertEqual([ep.event_ids for ep in episodes], [("e1", "e2"), ("e3",)])

    def test_work_anchor_splitting_is_opt_in(self):
        events = [
            event("e1", "command", timestamp="2026-09-01T00:00:00Z", bead_id="gl-1"),
            event("e2", "command", timestamp="2026-09-01T00:01:00Z", bead_id="gl-2"),
        ]
        self.assertEqual(len(segment_events(SESSION, events)), 1)
        split = segment_events(
            SESSION, events, EpisodeConfig(split_on_work_anchor_change=True)
        )
        self.assertEqual([ep.event_ids for ep in split], [("e1",), ("e2",)])
        self.assertEqual(split[0].work_anchor, "bead:gl-1")

    def test_group_key_prefers_bead_anchor(self):
        events = [
            event("e1", "user", timestamp="2026-09-01T00:00:00Z", bead_id="gl-9"),
            event("e2", "command", timestamp="2026-09-01T00:01:00Z", bead_id="gl-9"),
        ]
        episode = segment_events(SESSION, events)[0]
        self.assertEqual(episode.group_key, "bead:gl-9")

    def test_lineage_root_walks_parent_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            with ObservatoryStore(os.path.join(tmp, "p.db")) as store:
                store.import_jsonl(
                    support.write_jsonl(
                        os.path.join(tmp, "parent.jsonl"),
                        [event("e1", "command", timestamp="2026-09-01T00:00:00Z")],
                    )
                )
                store.import_jsonl(
                    support.write_jsonl(
                        os.path.join(tmp, "child.jsonl"),
                        [
                            event(
                                "e2",
                                "command",
                                timestamp="2026-09-01T00:01:00Z",
                                session_id="session-2",
                                parent_session_id="session-1",
                            )
                        ],
                    )
                )
                root = lineage_root_key(store, ("city-a", "host-a", "codex", "session-2"))
                self.assertEqual(root, SESSION)
                # A missing parent stops the walk.
                self.assertEqual(
                    lineage_root_key(store, ("city-a", "host-a", "codex", "session-9")),
                    ("city-a", "host-a", "codex", "session-9"),
                )

    def test_segment_session_groups_by_lineage_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            with ObservatoryStore(os.path.join(tmp, "p.db")) as store:
                store.import_jsonl(
                    support.write_jsonl(
                        os.path.join(tmp, "parent.jsonl"),
                        [event("e1", "user", timestamp="2026-09-01T00:00:00Z")],
                    )
                )
                store.import_jsonl(
                    support.write_jsonl(
                        os.path.join(tmp, "child.jsonl"),
                        [
                            event(
                                "e2",
                                "user",
                                timestamp="2026-09-01T00:01:00Z",
                                session_id="session-2",
                                parent_session_id="session-1",
                            )
                        ],
                    )
                )
                episodes = segment_session(store, ("city-a", "host-a", "codex", "session-2"))
                self.assertEqual(len(episodes), 1)
                self.assertEqual(
                    episodes[0].group_key,
                    segment_events(SESSION, [event("e1", "user", timestamp="2026-09-01T00:00:00Z")])[0].group_key,
                )
                # session-1's own group key matches the lineage root key.
                self.assertIn("session-1", episodes[0].group_key)

    def test_segment_store_returns_sorted_episodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            with ObservatoryStore(os.path.join(tmp, "p.db")) as store:
                store.import_jsonl(
                    support.write_jsonl(
                        os.path.join(tmp, "events.jsonl"),
                        [
                            event("e1", "user", timestamp="2026-09-01T00:00:00Z"),
                            event(
                                "e2",
                                "user",
                                timestamp="2026-08-01T00:00:00Z",
                                session_id="session-2",
                            ),
                        ],
                    )
                )
                episodes = segment_store(store)
                self.assertEqual(len(episodes), 2)
                self.assertLess(episodes[0].started_at, episodes[1].started_at)


class EpisodesCliTest(unittest.TestCase):
    def test_cli_emits_episode_candidates(self):
        import json

        from agent_observatory.cli import main
        from agent_observatory.store import ObservatoryStore

        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "p.db")
            source = support.write_jsonl(
                os.path.join(tmp, "events.jsonl"),
                [
                    event("e1", "user", timestamp="2026-09-01T00:00:00Z"),
                    event("e2", "command", timestamp="2026-09-01T00:01:00Z"),
                    event("e3", "user", timestamp="2026-09-01T00:02:00Z"),
                ],
            )
            with ObservatoryStore(db) as store:
                store.import_jsonl(source)
            out = os.path.join(tmp, "episodes.json")
            status = main(["episodes", "--db", db, "--out", out])
            self.assertEqual(status, 0)
            with open(out, encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertEqual(payload["kind"], "episode_candidates")
            self.assertEqual(len(payload["episodes"]), 2)


if __name__ == "__main__":
    unittest.main()

