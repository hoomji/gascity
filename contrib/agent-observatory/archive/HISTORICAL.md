# Historical panel documentation (retired)

The following describes the previous research workflow. These commands are unavailable in the production CLI; see README.md for offline reproduction.

### Silver reference (no hand labelling)

Owner decision 2026-09-25: the human gold set is replaced by a machine-built
**silver** reference (`silver.py`). `silver-build` samples candidate episodes
deterministically, stratified by provider, and asks a configurable set of
independent judges to label `primary_intent` from the **same stripped transcript
text** with the **same taxonomy criteria** at temperature 0 and JSON output. The
judge prompt is fixed and versioned (`JUDGE_PROMPT_VERSION`). The default set is
still the two Uniblock prod gateway judges — GLM 5.3 Flash
(`uniblock-prod/fireworks-ai/glm-5p3-flash`) and DeepSeek V4 Flash
(`uniblock-prod/deepseek/deepseek-flash`). `--judge JUDGE ...` (judge id, model
slug, or `backend:model`) and `--judges-file JUDGES.json` (a list of
`{judge_id, model, api_model, backend}`) add or replace judges. Each judge is
bound to a **pluggable backend**: `gateway` (OpenAI-compatible HTTP; the
historical path), `codex-cli` (the owner's Codex subscription via
`codex exec -m gpt-6-luna`, read-only sandbox, ephemeral) or `agy-cli` (the
owner's Antigravity subscription via
`agy --model gemini-3.8-flash-medium --json-schema`). Model names are never
guessed — they are resolved from each CLI's own `--version`/model list and
recorded per label.

Adjudication reuses the existing annotation vocabulary: judges that all agree make
an `adjudicated` silver label; any disagreement is recorded as `disagreement`,
excluded from the primary score and reported. Each episode records the per-judge
labels, confidences, model ids, backend, CLI version, prompt version and text
hash; the gold set keeps `annotator="silver-judges"` and is strictly separate
from Jev predictions. `silver-build` reports every pairwise Cohen's kappa,
Fleiss' kappa across the whole judge set, and each judge's label distribution and
`unknown` rate. `silver-evaluate` reports Jev accuracy/macro-F1 against each
judge alone, the majority label and the unanimous label. **Kappa below
`KAPPA_TRUST_FLOOR` (0.6) means the silver set is not trustworthy and the gate is
not claimed** — the trust value is the minimum pairwise kappa, so every judge
pair must clear the floor. A sample with fewer than `MIN_SILVER_SAMPLE_SIZE` (4)
episodes is also untrusted regardless of kappa, because kappa over a handful of
items is degenerate. Those floors are enforced, not just reported: `silver-build`
refuses to write the gold set and exits nonzero on a below-floor kappa or sample
(the agreement report is still written as evidence), and the loader/
`silver-evaluate` refuse to consume a silver set marked `silver_trustworthy:
false`. The explicit `--allow-untrusted` flag is the only override.

The limitation is explicit: silver labels measure agreement between the judge
set, not ground truth. A Jev error shared by every judge is invisible. See
`MEASUREMENT-SILVER.md`.

Reproducibility: the report pins `taxonomy_version`, `facet_hash`,
`question_hash`, `model`, `gold_set_version`, `gold_set_hash` and an
`evaluation_hash` over the split, provenance and every prediction hash, so a
model or question revision produces a different, replayable report. The pinned
`tests/fixtures/gold/` fixture proves the evaluator mechanics; the silver path
above is how real per-class quality is established without hand labelling.

### Collapse re-score of a multi-judge checkpoint

When more judges do not lift agreement, the taxonomy is the suspect. `collapse.py`
re-scores an already-recorded multi-judge checkpoint under a coarser
`primary_intent` mapping **without any judge call**: a versioned
`LabelCollapse` (original label -> collapsed label, one disambiguating
definition per collapsed label), pairwise confusion matrices, disagreement
episodes grouped by the labels that compete, pairwise Cohen's and Fleiss' kappa,
unanimous and 3-of-4 reference labels, and Jev accuracy/macro-F1 against those
references. `unknown` can be kept as a label or treated as an abstention
(complete-case per pair/reference); both are reported.

```
agent-observatory silver-collapse \
  --checkpoint judge_checkpoint_4judge.json \
  [--report silver_report.json] --jev-predictions jev_predictions.json \
  [--collapse primary_intent_collapse_v1] [--judge JUDGE ...] [--majority 3] \
  --out collapse_report.json [--require-clear]
```

The gate is the conservative minimum pairwise Cohen's kappa (matching
`kappa_over_judges`); Fleiss' kappa is secondary. `baseline` in the report is the
identity collapse, `diagnostics` holds the confusion/disagreement evidence, and
`clears_floor` is true only for a design that reaches `KAPPA_TRUST_FLOOR` on a
sample of at least `MIN_SILVER_SAMPLE_SIZE`. `build_collapsed_judge_prompt` builds
the fresh-sample prompt for a confirmation run, which is authorised only after a
collapse clears the floor. On the four-judge checkpoint the proposed
`primary_intent_collapse_v1` raises minimum pair kappa from 0.199 to 0.236
(0.317 -> 0.482 with `unknown` as abstain) and reaches Fleiss 0.554, so **no
design clears 0.6 and no fresh run was spent**. A search over every partition of
the nine labels into at least three classes of at most three source labels finds
no minimum pair kappa above 0.243 (label) or 0.482 (abstain). See
`MEASUREMENT-SILVER.md`.

### Two-stage abstention split

The collapse work showed the dominant boundary is abstention calibration, so the
owner chose a reference design that separates it from the taxonomy. `two_stage.py`
splits `primary_intent` agreement into two independent, separately-scored
stages:

- **Stage 1 — evidence gate.** Each judge answers only `known` vs `unknown`:
  "is there enough evidence to name exactly one primary task?" Agreement is
  scored on its own (pairwise Cohen's and Fleiss' kappa).
- **Stage 2 — intent.** Only on episodes a majority called `known`, each judge
  picks one of the eight substantive labels; there is **no `unknown` option**.
  Agreement is scored on its own on the original labels and projected through
  `primary_intent_collapse_v1`.

```
agent-observatory silver-two-stage \
  (--checkpoint single_pass_checkpoint.json |
   --stage1-checkpoint stage1.json [--stage2-checkpoint stage2.json]) \
  --jev-predictions jev_predictions.json --out two_stage_report.json \
  [--judge JUDGE ...] [--majority 3] [--require-clear]

agent-observatory silver-two-stage-run --episodes-file episodes.json \
  --out-report two_stage_report.json \
  [--stage1-checkpoint stage1.json] [--stage2-checkpoint stage2.json] \
  [--jev-predictions jev_predictions.json] [--judge JUDGE ...] [--majority 3] \
  [--evidence-prompt-version V] [--intent-prompt-version V] [--require-clear]
```

`--checkpoint` is the **zero-call approximation**: stage 1 is the recorded
`unknown` vs non-`unknown` decision, and stage 2 uses the non-`unknown` votes on
episodes a majority called non-`unknown`. It is a preview only, because a judge
that abstained has no stage-2 answer, so its stage-2 overlap is lower than a live
run's. `--stage1-checkpoint`/`--stage2-checkpoint` score recorded live answers,
and `silver-two-stage-run` drives the live pass through the same judge backends
as `silver-build` (the two stages use separate checkpoints so a stage-2 call
never replays a cached stage-1 answer). The gate is fail-closed: a pass requires
the conservative minimum pairwise Cohen's kappa to clear `KAPPA_TRUST_FLOOR` on a
large-enough sample whose judge pairs clear the usable-overlap floor, for stage 1
**and** the original stage-2 labels; `clears_floor_all_scorings` additionally
requires the collapsed projection. No `--allow-untrusted` pass is claimable. Jev
is scored against the stage-1 abstention reference and each stage-2 reference
(accuracy / macro-F1). The prompt pair is versioned
(`TWO_STAGE_PROMPT_VERSION` plus the stage-specific versions). The measured
outcome is in `MEASUREMENT-SILVER.md`.


## Former command syntax

```text
agent-observatory silver-build --candidates CANDIDATES.csv --db DB
    --out-gold GOLD.json --out-report REPORT.json
    [--sample-size N] [--seed S] [--judge JUDGE ...] [--judges-file JUDGES.json]
    [--jev-predictions-out FILE]
    [--base-url URL] [--api-key-env ENV] [--max-judge-requests N]
    [--taxonomy PATH] [--excerpt-bytes N] [--text-bytes N] [--judge-text-bytes N]
    [--gold-set-version V] [--prompt-version V] [--judge-timeout S]
    [--cli-judge-timeout S] [--tolerate-judge-failures]
agent-observatory silver-evaluate --gold GOLD.json --predictions PRED.json
    [--taxonomy PATH] [--full-evaluator] [--out FILE]
agent-observatory silver-collapse (--checkpoint CKPT.json | --report REPORT.json)
    --jev-predictions PRED.json --out FILE
    [--collapse ID] [--judge JUDGE ...] [--majority N] [--taxonomy PATH]
    [--require-clear]
agent-observatory silver-two-stage (--checkpoint CKPT.json |
    --stage1-checkpoint S1.json [--stage2-checkpoint S2.json])
    --jev-predictions PRED.json --out FILE
    [--judge JUDGE ...] [--majority N] [--taxonomy PATH] [--require-clear]
agent-observatory silver-two-stage-run --episodes-file EPISODES.json
    --out-report REPORT.json [--jev-predictions PRED.json]
    [--stage1-checkpoint S1.json] [--stage2-checkpoint S2.json]
    [--judge JUDGE ...] [--judges-file JUDGES.json] [--majority N]
    [--evidence-prompt-version V] [--intent-prompt-version V]
    [--pair-prompt-version V] [--taxonomy PATH]
    [--base-url URL] [--api-key-env ENV] [--max-judge-requests N]
    [--judge-timeout S] [--cli-judge-timeout S] [--judge-text-bytes N]
    [--tolerate-judge-failures] [--require-clear]
```
