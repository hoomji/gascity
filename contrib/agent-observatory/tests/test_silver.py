"""Silver reference builder: sampling, judging, adjudication, kappa, evaluation."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

try:
    from . import support
except ImportError:  # pragma: no cover
    import support

from agent_observatory.annotations import load_gold_set
from agent_observatory.errors import SilverError
from agent_observatory.evaluation import Prediction, load_predictions
from agent_observatory.silver import (
    JUDGE_DEEPSEEK,
    JUDGE_GLM,
    JUDGE_PROMPT_VERSION,
    HTTP_USER_AGENT,
    HTTPJudgeClient,
    build_judge_prompt,
    build_silver_episodes,
    build_silver_predictions_document,
    build_silver_result,
    cohen_kappa,
    evaluate_silver_vs_jev,
    load_candidates_csv,
    parse_judge_answer,
    predictions_from_store,
    render_transcript_text,
    sample_stratified,
    session_key_from_group_key,
)
from agent_observatory.store import ObservatoryStore
from agent_observatory.taxonomy import load_taxonomy

HERE = os.path.dirname(os.path.abspath(__file__))
PACKAGE_ROOT = os.path.dirname(HERE)
V2_PATH = os.path.join(PACKAGE_ROOT, "agent_observatory", "taxonomy", "jev_taxonomy_v2.json")
CANDIDATES = os.path.join(HERE, "fixtures", "silver", "candidates.csv")
ANSWERS = os.path.join(HERE, "fixtures", "silver", "recorded_judge_answers.json")


class RecordedJudgeClient:
    """Replay recorded judge answers keyed by judge id and episode id."""

    def __init__(self, judge_id, recorded):
        self.judge_id = judge_id
        self.recorded = recorded

    def label(self, prompt, *, episode_id):
        try:
            return self.recorded[self.judge_id][episode_id]
        except KeyError as exc:  # pragma: no cover - test bug
            raise AssertionError(f"no recorded answer for {self.judge_id}/{episode_id}") from exc


def _recorded():
    with open(ANSWERS, encoding="utf-8") as handle:
        return json.load(handle)


def _episodes(candidates, text_by_id):
    from agent_observatory.silver import SilverEpisode

    return tuple(
        SilverEpisode(
            episode_id=candidate.episode_id,
            group_key=candidate.group_key,
            provider=candidate.provider,
            observed_at=candidate.observed_at,
            text=text_by_id[candidate.episode_id],
        )
        for candidate in candidates
    )


class SilverSamplingTests(unittest.TestCase):
    def setUp(self):
        self.taxonomy = load_taxonomy(V2_PATH)

    def test_load_candidates_and_session_key(self):
        candidates = load_candidates_csv(CANDIDATES)
        self.assertEqual(len(candidates), 6)
        self.assertEqual(candidates[0].provider, "codex")
        key = session_key_from_group_key(candidates[0].group_key)
        self.assertEqual(key, ("city-a", "host-a", "codex", "s-1"))
        self.assertIsNone(session_key_from_group_key("bead:g1"))

    def test_sample_is_deterministic_and_proportional(self):
        candidates = load_candidates_csv(CANDIDATES)
        first = sample_stratified(candidates, 4, seed="s")
        second = sample_stratified(list(reversed(candidates)), 4, seed="s")
        self.assertEqual([ep.episode_id for ep in first], [ep.episode_id for ep in second])
        counts = {}
        for episode in first:
            counts[episode.provider] = counts.get(episode.provider, 0) + 1
        self.assertEqual(sum(counts.values()), 4)
        self.assertIn("codex", counts)

    def test_sample_larger_than_pool_is_rejected(self):
        candidates = load_candidates_csv(CANDIDATES)
        with self.assertRaises(SilverError):
            sample_stratified(candidates, 99)


class SilverPromptTests(unittest.TestCase):
    def setUp(self):
        self.taxonomy = load_taxonomy(V2_PATH)

    def test_prompt_contains_criteria_and_version(self):
        prompt = build_judge_prompt("Please fix the bug", self.taxonomy)
        self.assertIn(JUDGE_PROMPT_VERSION, prompt)
        self.assertIn("bugfix", prompt)
        self.assertIn("Correct an existing defect", prompt)
        self.assertIn("Please fix the bug", prompt)
        self.assertIn("unknown", prompt)
        self.assertIn("ONE JSON object", prompt)

    def test_prompt_bounds_text(self):
        prompt = build_judge_prompt("x" * 5000, self.taxonomy, max_bytes=64)
        self.assertIn("[truncated:", prompt)

    def test_parse_answer_accepts_code_fences_and_alias(self):
        allowed = ["bugfix", "unknown"]
        self.assertEqual(parse_judge_answer('```json\n{"primary_intent": "bugfix"}\n```', allowed), ("bugfix", None))
        self.assertEqual(parse_judge_answer('{"label": "unknown", "confidence": 0.4}', allowed), ("unknown", 0.4))

    def test_parse_answer_rejects_unknown_label_and_bad_confidence(self):
        with self.assertRaises(SilverError):
            parse_judge_answer('{"primary_intent": "nonsense"}', ["bugfix"])
        with self.assertRaises(SilverError):
            parse_judge_answer('{"primary_intent": "bugfix", "confidence": 1.5}', ["bugfix"])
        with self.assertRaises(SilverError):
            parse_judge_answer("not json", ["bugfix"])


class KappaTests(unittest.TestCase):
    def test_kappa_perfect_and_chance(self):
        self.assertEqual(cohen_kappa([("a", "a"), ("b", "b")]), 1.0)
        self.assertEqual(cohen_kappa([("a", "a"), ("a", "b"), ("b", "a"), ("b", "b")]), 0.0)
        self.assertIsNone(cohen_kappa([(None, "a")]))

    def test_kappa_known_value(self):
        # 8 agreements out of 10 with balanced marginals: po=0.8, pe=0.5 -> 0.6
        pairs = [("a", "a")] * 4 + [("b", "b")] * 4 + [("a", "b"), ("b", "a")]
        self.assertAlmostEqual(cohen_kappa(pairs), 0.6, places=6)


class SilverBuildTests(unittest.TestCase):
    def setUp(self):
        self.taxonomy = load_taxonomy(V2_PATH)
        self.candidates = load_candidates_csv(CANDIDATES)
        self.recorded = _recorded()

    def _judges(self):
        return [
            (JUDGE_GLM, RecordedJudgeClient(JUDGE_GLM.judge_id, self.recorded)),
            (JUDGE_DEEPSEEK, RecordedJudgeClient(JUDGE_DEEPSEEK.judge_id, self.recorded)),
        ]

    def _sample(self):
        sample = sample_stratified(self.candidates, 4, seed="silver-v1")
        text = {episode.episode_id: f"Work for {episode.episode_id}." for episode in sample}
        return _episodes(sample, text)

    def test_build_adjudicates_and_reports_kappa(self):
        episodes = self._sample()
        result = build_silver_result(episodes, self.taxonomy, self._judges())
        by_id = result.gold_set.by_id()
        # ep-bugfix-1 / ep-impl-2 / ep-review-3 / ep-plan-4 agree; the others may
        # be sampled or not depending on the deterministic draw.
        agreed = [ep for ep in result.gold_set.episodes if ep.adjudication == "adjudicated"]
        self.assertGreaterEqual(len(agreed), 1)
        for episode in agreed:
            self.assertEqual(episode.annotator, "silver-judges")
            self.assertEqual(len(episode.label_set("primary_intent")), 1)
            self.assertIsNotNone(episode.metadata["judge_labels"])
        self.assertEqual(result.report["judge_calls"]["total"], 2 * len(episodes))
        self.assertIn("cohen_kappa", result.report["agreement"])
        self.assertTrue(by_id)

    def test_gold_set_round_trips_through_loader(self):
        episodes = self._sample()
        result = build_silver_result(episodes, self.taxonomy, self._judges())
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "silver.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(result.gold_set.to_json())
            loaded = load_gold_set(path, self.taxonomy)
        self.assertEqual(loaded.gold_set_hash(), result.gold_set.gold_set_hash())
        self.assertEqual(loaded.taxonomy_version, "2.0.0")

    def test_result_requires_two_judges(self):
        episodes = self._sample()
        with self.assertRaises(SilverError):
            build_silver_result(episodes, self.taxonomy, self._judges()[:1])

    def test_evaluate_silver_vs_jev_scores_agreed_items(self):
        episodes = self._sample()
        result = build_silver_result(episodes, self.taxonomy, self._judges())
        agreed = [ep for ep in result.gold_set.episodes if ep.adjudication == "adjudicated"]
        self.assertGreaterEqual(len(agreed), 1)
        predictions = []
        for index, episode in enumerate(agreed):
            label = episode.primary("primary_intent")
            # Miss the second item so accuracy is not trivially 1.0 when possible.
            if index == 1:
                label = "unknown"
            predictions.append(
                Prediction(episode_id=episode.episode_id, predictor="jev", labels={"primary_intent": (label,)})
            )
        report = evaluate_silver_vs_jev(result.gold_set, predictions, self.taxonomy)
        self.assertEqual(report["jev"]["predicted"], len(agreed))
        self.assertEqual(report["jev"]["missing"], [])
        self.assertGreater(report["jev"]["accuracy"], 0.0)
        self.assertIsNotNone(report["agreement"]["cohen_kappa"])
        self.assertIn("silver_trustworthy", report["gate"])

    def test_full_evaluator_runs_on_agreed_items(self):
        episodes = self._sample()
        result = build_silver_result(episodes, self.taxonomy, self._judges())
        agreed = [ep for ep in result.gold_set.episodes if ep.adjudication == "adjudicated"]
        predictions = [
            Prediction(
                episode_id=episode.episode_id,
                predictor="jev",
                labels={"primary_intent": (episode.primary("primary_intent"),)},
            )
            for episode in agreed
        ]
        report = evaluate_silver_vs_jev(result.gold_set, predictions, self.taxonomy, run_full_evaluator=True)
        self.assertIn("evaluation", report)
        self.assertIn("jev", report["evaluation"]["evaluations"])

    def test_predictions_document_round_trips(self):
        predictions = [
            Prediction(episode_id="e1", predictor="jev", labels={"primary_intent": ("bugfix",)}),
            Prediction(episode_id="e2", predictor="jev", labels={"primary_intent": ("implementation",)}, confidence=0.5),
        ]
        document = build_silver_predictions_document(predictions)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "preds.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(document, handle)
            loaded = load_predictions(path, self.taxonomy)
        self.assertEqual([p.episode_id for p in loaded], ["e1", "e2"])


class HTTPJudgeClientTests(unittest.TestCase):
    class _Response:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return json.dumps(self._payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def test_sends_user_agent_temperature_and_json_mode(self):
        captured = {}

        def opener(request, timeout=None):
            captured["headers"] = {key.lower(): value for key, value in request.headers.items()}
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return self._Response({"choices": [{"message": {"content": '{"primary_intent":"bugfix"}'}}]})

        os.environ["UNIBLOCK_PROD_KEY"] = "test-key-not-real"
        self.addCleanup(os.environ.pop, "UNIBLOCK_PROD_KEY", None)
        client = HTTPJudgeClient(JUDGE_GLM, opener=opener, max_requests=1)
        content = client.label("prompt", episode_id="e1")
        self.assertEqual(content, '{"primary_intent":"bugfix"}')
        self.assertEqual(captured["headers"]["user-agent"], HTTP_USER_AGENT)
        self.assertEqual(captured["headers"]["authorization"], "Bearer test-key-not-real")
        self.assertTrue(captured["url"].endswith("/chat/completions"))
        self.assertEqual(captured["body"]["temperature"], 0)
        self.assertEqual(captured["body"]["response_format"], {"type": "json_object"})
        with self.assertRaises(SilverError):
            client.label("prompt", episode_id="e2")

    def test_missing_credential_is_an_error(self):
        os.environ.pop("UNIBLOCK_PROD_KEY", None)
        client = HTTPJudgeClient(JUDGE_GLM)
        with self.assertRaises(SilverError):
            client.label("prompt", episode_id="e1")

    def test_retries_transient_timeout_then_succeeds(self):
        attempts = {"n": 0}

        def opener(request, timeout=None):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise TimeoutError("read timed out")
            return self._Response({"choices": [{"message": {"content": "{\"primary_intent\":\"bugfix\"}"}}]})

        os.environ["UNIBLOCK_PROD_KEY"] = "test-key-not-real"
        self.addCleanup(os.environ.pop, "UNIBLOCK_PROD_KEY", None)
        client = HTTPJudgeClient(
            JUDGE_GLM, opener=opener, max_attempts=3, retry_backoff_seconds=0, max_requests=5
        )
        self.assertEqual(client.label("prompt", episode_id="e1"), '{"primary_intent":"bugfix"}')
        self.assertEqual(client.requests_made, 2)

    def test_does_not_retry_validation_status(self):
        import urllib.error

        def opener(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 403, "forbidden", {}, None)

        os.environ["UNIBLOCK_PROD_KEY"] = "test-key-not-real"
        self.addCleanup(os.environ.pop, "UNIBLOCK_PROD_KEY", None)
        client = HTTPJudgeClient(JUDGE_GLM, opener=opener, max_attempts=3, retry_backoff_seconds=0)
        with self.assertRaises(SilverError):
            client.label("prompt", episode_id="e1")
        self.assertEqual(client.requests_made, 1)

    def test_checkpoint_replays_without_respending(self):
        from agent_observatory.silver import CheckpointJudgeClient

        class Counting:
            def __init__(self):
                self.calls = 0

            def label(self, prompt, *, episode_id):
                self.calls += 1
                return '{"primary_intent":"bugfix"}'

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "checkpoint.json")
            inner = Counting()
            first = CheckpointJudgeClient("glm", inner, path)
            self.assertEqual(first.label("p", episode_id="e1"), '{"primary_intent":"bugfix"}')
            self.assertEqual(inner.calls, 1)
            second_inner = Counting()
            second = CheckpointJudgeClient("glm", second_inner, path)
            self.assertEqual(second.label("p", episode_id="e1"), '{"primary_intent":"bugfix"}')
            self.assertEqual(second_inner.calls, 0)
            self.assertEqual(second.cache_hits, 1)

    def test_checkpoint_merges_two_judge_sections(self):
        from agent_observatory.silver import CheckpointJudgeClient

        class Constant:
            def __init__(self, value):
                self.value = value

            def label(self, prompt, *, episode_id):
                return self.value

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "checkpoint.json")
            glm = CheckpointJudgeClient("glm", Constant('{"primary_intent":"bugfix"}'), path)
            deepseek = CheckpointJudgeClient("deepseek", Constant('{"primary_intent":"implementation"}'), path)
            glm.label("p", episode_id="e1")
            deepseek.label("p", episode_id="e1")
            with open(path, encoding="utf-8") as handle:
                stored = json.load(handle)
        self.assertEqual(set(stored), {"glm", "deepseek"})
        self.assertEqual(stored["glm"]["e1"], '{"primary_intent":"bugfix"}')
        self.assertEqual(stored["deepseek"]["e1"], '{"primary_intent":"implementation"}')


class SilverProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "proj.db")
        self.taxonomy = load_taxonomy(V2_PATH)
        records = [
            support.make_record(
                event_id="e1",
                kind="message",
                timestamp="2026-09-01T00:00:00Z",
                text=(
                    "[city] codex-worker-1 • 2026-09-01T00:00:00\n\n# GC Role Worker\n\n"
                    "You are a role worker. gc hook --claim"
                ),
            ),
            support.make_record(
                event_id="e2",
                kind="message",
                timestamp="2026-09-01T00:00:01Z",
                text="Please fix the flaky scheduler test",
            ),
            support.make_record(
                event_id="e3",
                kind="tool_call",
                timestamp="2026-09-01T00:00:02Z",
                tool_name="Bash",
                command="pytest tests/test_scheduler.py",
            ),
        ]
        path = support.write_jsonl(os.path.join(self.tmp.name, "s.jsonl"), records)
        self.store = ObservatoryStore(self.db)
        self.addCleanup(self.store.close)
        self.store.import_jsonl(path)

    def test_build_silver_episodes_drops_framework_and_keeps_task_text(self):
        candidates = load_candidates_csv(CANDIDATES)[:1]
        group = candidates[0].group_key.replace('"s-1"', '"session-1"')
        from agent_observatory.silver import CandidateEpisode

        candidate = CandidateEpisode(
            episode_id="ep-1",
            group_key=group,
            provider="codex",
            observed_at="2026-09-01T00:00:00Z",
        )
        episodes, skipped = build_silver_episodes(self.store, [candidate])
        self.assertEqual(skipped, [])
        self.assertEqual(len(episodes), 1)
        text = episodes[0].text
        self.assertIn("Please fix the flaky scheduler test", text)
        self.assertIn("pytest tests/test_scheduler.py", text)
        self.assertNotIn("GC Role Worker", text)
        self.assertNotIn("gc hook --claim", text)

    def test_render_transcript_text_is_bounded(self):
        state = {"excerpts": [{"kind": "message", "text": "x" * 5000}]}
        rendered = render_transcript_text(state, max_bytes=100)
        self.assertIn("[truncated:", rendered)

    def test_predictions_from_store_reads_metadata_classification(self):
        key = ("city-a", "host-a", "codex", "session-1")
        snapshot = self.store.session_snapshot(key)
        self.store.save_classification(
            subject_kind="session",
            snapshot_hash=snapshot,
            taxonomy_version="1.1.0",
            question_hash="q" * 64,
            model_version="jev-1.13.0",
            request_hash="r" * 64,
            response_hash="h" * 64,
            answers=[
                {
                    "question_id": "primary_intent",
                    "question_type": "choice",
                    "answer": {"choice": "bugfix", "confidence": 0.9, "probabilities": {"bugfix": 1.0}},
                }
            ],
        )
        from agent_observatory.silver import SilverEpisode

        episode = SilverEpisode(
            episode_id="ep-1",
            group_key='session:["city-a","host-a","codex","session-1"]',
            provider="codex",
            observed_at="2026-09-01T00:00:00Z",
            text="Please fix the flaky scheduler test",
        )
        predictions = predictions_from_store(self.store, [episode])
        self.assertEqual(len(predictions), 1)
        self.assertEqual(predictions[0].primary("primary_intent"), "bugfix")


if __name__ == "__main__":
    unittest.main()
