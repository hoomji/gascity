# Measurement note: the machine-built silver reference

Owner decision 2026-09-25 replaced human gold annotation with a **silver**
reference for the `primary_intent` gate. This note states the method and, more
importantly, what the resulting numbers do and do not mean.

## Why silver

The live `obs.db` projection had `primary_intent=unknown` for all 53 live Jev
classifications, so the M7 shadow policy had zero eligible rows and the M8
canary could not run. The cause was the classification **data scope**, not the
taxonomy: every live request carried `state_kind=session_metadata` (event/tool
counts only), and a metadata-only state cannot express a task's intent. Jev
answered `unknown` consistently because the rubric says to choose `unknown` when
the evidence does not support exactly one category.

The fix is the owner-approved transcript-text scope with Gas City framework
payloads stripped (`framework.py`), plus `queue-drain` defaulting to that scope.
Re-classifying the same 53 sessions in the text scope produced non-`unknown`
`primary_intent` labels (see the execution report for the measured counts).

## Method

`python3 -m agent_observatory silver-build`:

1. **Sample.** Candidate episodes come from a candidate CSV
   (`episode_id`, `group_key`, `provider`, `observed_at`). Sampling is
   deterministic for a seed and proportional across providers by largest
   remainder. A candidate is skipped and reported when its session is absent
   from the projection or has no task text after framework stripping.
2. **Strip.** Text is read through the collector's text state, which drops
   `[city] <agent> • <time>` role prompts, runtime-context snapshots,
   `<system-reminder>` wake/deferred reminders and skill payloads. The filter is
   versioned (`FRAMEWORK_FILTER_VERSION`) and recorded on every state.
3. **Judge.** Two independent judges label `primary_intent` from the **same
   stripped text** with the **same taxonomy criteria text**, at temperature 0
   with JSON output:
   - GLM 5.3 Flash — `uniblock-prod/fireworks-ai/glm-5p3-flash`
   - DeepSeek V4 Flash — `uniblock-prod/deepseek/deepseek-flash`

   Both go through the Uniblock prod gateway (OpenAI-compatible
   `/chat/completions`). The fixed prompt is versioned
   (`JUDGE_PROMPT_VERSION`). The bearer key is read from an environment variable
   at call time and is never printed or committed. A stable user agent is
   required: the gateway rejects the default `Python-urllib` signature with
   HTTP 403 (`error code: 1010`).
4. **Adjudicate.** Agreement between the two judges becomes an `adjudicated`
   silver label. Disagreement becomes `disagreement`, is excluded from the
   primary score and is reported. Per-judge labels, confidences, model ids,
   prompt version, text hash and the `gold_set_hash` are recorded. Silver is
   kept strictly separate from Jev predictions (`annotator="silver-judges"`).
5. **Report.** `silver-evaluate` reports judge-judge Cohen's kappa and
   Jev-vs-silver accuracy/macro-F1 on agreed items.
6. **Enforce.** The kappa floor and the minimum sample size are enforced, not
   merely reported. `silver-build` refuses to write `--out-gold` when kappa is
   below `0.6` (`KAPPA_TRUST_FLOOR`) or when the sample has fewer than
   `MIN_SILVER_SAMPLE_SIZE = 4` episodes, and exits nonzero; the agreement report
   is still written as evidence. A kappa over a handful of episodes is
   degenerate (one agreed episode yields kappa `1.0` by construction), so the
   sample floor is checked independently of kappa. `load_gold_set` (and therefore
   `silver-evaluate`) refuses a silver set marked `silver_trustworthy: false`,
   and `silver-evaluate` also refuses to claim a gate whose recomputed kappa is
   below the floor or whose sample is below the floor. Both refusals are lifted
   only by the explicit `--allow-untrusted` flag. The gold set records the marker
   top-level as `"silver": true` / `"silver_trustworthy": <bool>`.

Budget: the judge-call ceiling is `2 x 150` (two judges over 150 episodes); the
tool refuses to exceed the configured `--max-judge-requests`.

## What the gate does and does not establish

- **It is an agreement measure, not ground truth.** A silver label is the label
  two LLMs produced, not the label a careful human would produce.
- **A Jev error shared by both judges is invisible.** If both judges and Jev
  make the same mistake, the silver accuracy is 1.0 for a wrong label.
- **Judge bias is shared.** Two models from the same gateway and prompt style
  may share systematic biases (for example, treating any tool-heavy session as
  `dependency_worktree_agent_ops`).
- **Kappa gates trust and is enforced; a minimum sample is required.** If
  Cohen's kappa is below `0.6`, the judges disagree too much for the silver set
  to be a trustworthy reference. Independently, a sample with fewer than
  `MIN_SILVER_SAMPLE_SIZE = 4` episodes is untrusted regardless of kappa because
  kappa over so few items is degenerate. The report says so, `silver-build`
  refuses to write the gold set and exits nonzero, and the loader/evaluator
  refuse to consume an untrusted silver file unless the caller passes
  `--allow-untrusted`. An untrusted set is never treated as ground truth by
  accident.
- **The comparison is descriptive.** These numbers describe classifier
  agreement. They do not establish orchestration benefit or causality.
- **Coverage is partial.** Candidates whose sessions are missing from the
  projection, or whose text is entirely injected framework payload, are skipped
  and reported rather than fabricated.

Any M7/M8 decision that cites these numbers must also cite the limitation that
the reference is machine agreement, and must keep `unknown`/low-confidence/rare
cases out of automatic routing as the evaluator's automation gate already does.

## Measured outcome, 2026-09-25

The first real run used 150 episodes sampled from the 262 candidates whose
transcripts are in the live projection (80 `claude-mayor`, 70
`dsh-minimal-deepseek-flash`; the 120 `dsh-minimal-glm-5.3-flash` candidates have
no transcript text in the projection — their collector root `~/.dsh-glm/sessions`
was never collected — so they were skipped and reported, not fabricated).

- Judge calls: 300 (150 per judge), 0 parse failures.
- Agreement: 112 `adjudicated`, 38 `disagreement` (agreement rate 0.747).
- **Cohen's kappa: 0.406**, below the 0.6 trust floor → **the silver set is not
  trustworthy and the gate was not claimed.** With the enforced gate,
  `silver-build` refuses to write this gold set and exits nonzero; reproducing
  the measured artifact requires the explicit `--allow-untrusted` opt-in, and
  any pre-enforcement file is treated as untrusted on load.
- Jev vs silver on the 110 agreed items that also had a Jev prediction:
  accuracy 0.636, macro-F1 0.511, 77/110 (70.0%) non-`unknown`; 2 agreed
  episodes had no stored Jev classification.
- The agreed labels were heavily skewed to `dependency_worktree_agent_ops`
  (97 of 112), which is consistent with the shared-judge-bias limitation above:
  tool-heavy agent sessions look alike to both judges even after framework
  payloads are stripped.

The root-cause re-run is reported separately in the execution report: the same
53 sessions that were `primary_intent=unknown` under the metadata scope produced
43 non-`unknown` labels under the stripped-text scope (34
`dependency_worktree_agent_ops`, 4 `pr_review`, 2 `research_docs`, 1 each
`bugfix`/`planning_spec`/`implementation`, 7 still `unknown`, 3 with no stored
text label). The classifier fix is confirmed; the two-judge silver gate is not
acceptable as a pass on this corpus.

## Reproducing

```bash
python3 -m agent_observatory silver-build \
  --candidates observatory/gold_candidates_400.csv \
  --db obs.db --sample-size 150 --seed silver-v1 \
  --out-gold silver_gold_150.json --out-report silver_report_150.json \
  --jev-predictions-out jev_predictions_150.json \
  --checkpoint judge_checkpoint.json --max-judge-requests 300
python3 -m agent_observatory silver-evaluate \
  --gold silver_gold_150.json --predictions jev_predictions_150.json \
  --out silver_eval_150.json
```

`--checkpoint` makes the 300-call run resumable; a partial build replays the
answers already on disk instead of re-spending them.

