# Impact report artifacts

This directory holds committed impact-report artifacts. Reports are aggregates;
no transcript text, command line, session id or credential is stored here.

## `real-obsdb-20260922.json`

A real historical report generated from a **read-only copy** of the local
projection at `~/.local/share/agent-observatory/obs.db` (schema version 4). The
owner ruling is to read a copy, never the original, so the projection was copied
first and the copy was only read afterwards.

- Generator: `python3 -m agent_observatory impact --db <copy-of-obs.db>`
- Source hash (sha256 of the copy) is recorded in
  `observed_evidence.source_hash`; the report itself records a `report_hash`
  over its own content.
- Observed at generation time: 7,354 sessions, 779,149 events, 247,819 usage
  rows, 53 classified and 1 pending Jev requests, 7,301 queue items pending,
  event range 2026-08-25 to 2026-09-22.

What the real report does and does not establish:

- It **does** report the measurable real evidence: per-provider and per-model
  token totals, missing vs explicit-zero usage, observed duration distribution,
  classifier request/latency/retry/cost coverage, and collector backlog.
- It **does not** claim an accepted-task effect. The projection carries no
  acceptance, work-link or cohort evidence, so the accepted-task section has no
  eligible work items, an `unmeasurable` attribution grade and an undefined cost
  per accepted task. That is missingness, not zero, and the report says so.

The known-effect / no-effect / confounded replay evidence lives as fixtures in
`tests/fixtures/impact/` and is exercised by `tests/test_impact.py`; those
fixtures prove the matched-cohort, uncertainty and censoring semantics that the
real projection cannot yet supply.

### Replay artifacts

The committed reports below were generated from the fixtures with the same
command, so the semantics can be inspected without running the suite:

- `replay-known-effect.json` — matched observational, `observed_improvement`,
  matched 95% interval entirely below zero.
- `replay-no-effect.json` — matched observational, `no_observed_effect`, matched
  interval straddling zero.
- `replay-confounded.json` — `descriptive`, `confounded: true`, no matched
  strata (the arms do not overlap), so the large naive difference is not treated
  as an effect.

```
python3 -m agent_observatory impact --input tests/fixtures/impact/known_effect.json \
    --out reports/replay-known-effect.json
```

Real historical report:

```
python3 -m agent_observatory impact --db <copy-of-obs.db> \
    --out reports/real-obsdb-20260922.json
```

