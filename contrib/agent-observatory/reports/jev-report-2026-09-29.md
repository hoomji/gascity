# Jev Observatory — 2026-09-29 live projection rerun

Generated: `2026-09-29T18:14:21Z` · SQLite schema 6 · audit status: **BLOCKED**.

This is an aggregate-only rerun of the `ci-d13cgg` receipt (`e7370b3a`) after
updating the local Agent Observatory runtime from `origin/main`. It is execution
evidence, not acceptance. No transcript text, session identifiers, source paths,
raw command contents, or credentials are included. No classification/model calls,
routing changes, deployment, or merge were made.

## Runtime and protected baseline

- The runtime used source SHA `b398c706419e95e6777c8e065e35e5fa0ced0c1d`
  (`agent-observatory` version `0.8.0`), including the session Git-evidence and
  usage-backfill implementation, schema-6 bindings, and the worktree/commit-output
  parser fix. The later mainline update during this rerun changed this report only;
  the Agent Observatory runtime code is unchanged.
- Backed up the live projection before the rerun. Backup label:
  `obs.db.backup-20260929T174318Z-d7403255cbce196dd3fb.bak`; SHA-256
  `91fb51a1ae88407c097764a1ffbacf4dad1cd32b75d3808bb5bb220d87d54380`;
  SQLite integrity check: `ok`.
- The protected baseline was already schema 6, so no migration was needed.
  Baseline totals: 925,327 events, 7,895 sessions, 295,551 usage rows,
  62,877 commit fingerprints, and 1,984 registered changes.

## Enrichment rerun

The current-main collector invalidated 8,095 existing source checkpoints and ran
a bounded pass over three explicit transcript roots, with a 400 MB per-source
cap and queue enqueueing disabled. It completed with status `ok`: 7,785 sources
were imported, one was debounced, one had a source error, and 7,359,182,750
source bytes were read. The pass inserted **0** events, found 703,018 duplicate
identities and 175,235 conflicting immutable payload identities, and added 517
session commit fingerprints. It enqueued no sessions. Previously missing sources
were not rediscovered by this pass.

The enriched projection therefore has 63,394 commit-SHA session fingerprints,
but still has **0/925,369 events** with `repo` and **0/925,369** with
`commit_sha`. Re-import did not rewrite conflicting event payloads. Final counts
also reflect activity from the already-running incremental collector: compared
with the protected backup, the live projection has 42 additional events and 16
additional usage rows; those deltas are not credited to the forced full pass or
to `backfill-usage`.

## Usage backfill

The backfill completed with status `ok` using the current-main backfill routine,
a 100 MB per-source bound, and a temporary `(provider, source_path, source_line)`
lookup index. The index was removed after the pass. Of 629,802 candidate events,
0 matched, 629,802 remained unmatched, and 0 usage rows were inserted. The pass
read 7,741 of 8,047 seen sources; 306 sources were missing, with no deferred,
parse-error, or unmapped sources reported. The final projection has 295,567
`event_usage` rows. No missing token values were synthesized.

## M5 — change screening and exposure

The refreshed change bundle covers `2026-08-24T00:00:00Z` through
`2026-09-29T17:43:18Z`, using merged-PR metadata and default-branch commit-parent
edges. It screened 1,992 PRs: 39 for `hoomji/gascity` and 1,953 for
`uniblock-dev/gateway-llm`; 157 were screened as optimization candidates and
1,835 remained unknown. Sync inserted eight new changes, eight activations, and
16 commit-parent edges; the previous 1,984 changes were deduplicated.

Exposure result: **157 unknown; 0 exposed; 0 unexposed; 0 persisted exposure
rows**. This is not evidence of non-exposure. Repository-bound event evidence is
still absent, so the conservative `unknown` verdict is the only supported
result.

## M6 — accepted-task impact

The projection-only impact report had no accepted-task/cohort input bundle:
0 accepted and 0 eligible work items. Attribution is **unmeasurable** with
conclusion `no_work_items`; the report makes no causal claim and no cost-per-
accepted-task claim.

## M7 — shadow policy

M7 was **not run**, as specified by the scope: a production route catalog and a
time-stamped current-routing bundle are still required and were not available.
No production catalog was inferred from the synthetic test fixture. The live
recommendation table remains empty; this is an outstanding acceptance gate, not
a zero-disagreement result.

## Aggregate token and classification evidence

The final projection contains 925,369 events, of which 295,567 have usage rows
and 629,802 have missing usage. The 122 intent × role × model buckets have no
role coverage (0/7,895 sessions); role is therefore reported as unknown.

| Token field | Sum of known measurements | Known events | Missing events |
|---|---:|---:|---:|
| Input | 11,119,460,457 | 295,567 | 629,802 |
| Output | 153,746,768 | 295,567 | 629,802 |
| Cache read | 34,468,967,827 | 295,567 | 629,802 |
| Cache write | 434,966,174 | 255,446 | 669,923 |
| Total | 35,347,273,148 | 295,567 | 629,802 |

There are **0 priced events** and 925,369 events with unknown USD cost under the
available price seed. Unknown cost is not reported as `$0`.

| Primary intent | Events |
|---|---:|
| `adversarial_review` | 16,278 |
| `bugfix` | 209,380 |
| `dependency_worktree_agent_ops` | 278,780 |
| `implementation` | 76,634 |
| `planning_spec` | 4,933 |
| `pr_review` | 169,942 |
| `research_docs` | 55,930 |
| `test_lint_build_ci` | 54,254 |
| `unknown` | 59,238 |

Classification bindings remain 7,865/7,881; 16 classifications remain unbound.

## Remaining acceptance gates

1. Provide an immutable-safe way to bind repository/commit evidence to existing
events, plus a trusted session-role mapping. Until then, M5 exposure stays
unknown and role-based conclusions are unavailable.
2. Supply the approved production M7 catalog and time-stamped current-routing
bundle; then run the shadow analysis and report eligibility, disagreement, and
temporal-leak checks.
3. Supply accepted-work and cohort-assignment evidence for M6 before claiming an
effect or cost per accepted task.
4. Recover the 306 missing source files and establish why the 629,802 usage
candidates did not match before treating usage coverage as complete.

**Verdict: BLOCKED.** The requested rerun and aggregate audit are delivered, but
the projection does not yet support M5 exposure attribution, M6 accepted-task
impact, or M7 shadow-policy acceptance.

Artifacts from this run are retained outside the checkout under the run label
`jev-m5m6m7-rerun-20260929T174318Z-d7403255cbce196dd3fb`.
