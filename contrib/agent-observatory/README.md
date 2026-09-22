# Agent Observatory (offline foundation slice)

A dependency-free Python 3 CLI and library for **normalized evidence import,
classification storage, and deterministic reporting** over Gas City agent
activity. It is the first bounded slice of the Jev-powered historical/live chat
analysis effort.

This is deliberately **not** the whole product and **not** production-ready:

- **Transport is explicit.** The request builder only emits a `/v1/systemone`
  request body; nothing is sent by `build-request`. The `classify` subcommand
  performs a live, bounded send (retries, budgets, circuit breaker) and records
  provenance; saved responses are otherwise validated offline.
- **No live collector.** Import reads explicit files only. There is no
  home-directory crawling and no provider-transcript scraping.
- **No paid API calls, real transcript uploads, publishing, or merging.**
- **No orchestration truth.** Beads and events remain authoritative. The SQLite
  file is a local, rebuildable analytical projection.
- All policy and vendor specifics live here under `contrib/agent-observatory/`;
  no core Go SDK, runtime, config, or routing code is touched.

## Layout

```
contrib/agent-observatory/
  agent_observatory/
    __init__.py        public API and version
    __main__.py        python3 -m agent_observatory
    cli.py             argparse CLI
    contract.py        versioned normalized JSONL contract + strict validation
    canonical.py       canonical JSON, hashing, event/session snapshot identity
    store.py           SQLite schema versioning, transactional import, classifications
    commands.py        conservative, non-executing command categorization
    changes.py         M5 optimization change registry + conservative screen
    exposure.py        M5 commit/fingerprint exposure join + optimization ledger
    report.py          deterministic report JSON
    taxonomy.py        versioned question taxonomy loader
    taxonomy/jev_taxonomy_v1.json
    jev.py             /v1/systemone request builder + saved-response validation
  examples/
    synthetic_events.jsonl   synthetic fixture (no real data)
    state.json               explicit sanitized state for the request builder
    response.json            synthetic saved Jev response
    demo.sh                  end-to-end CLI demo into a temp SQLite file
  tests/                     unittest suite
    fixtures/jev_smoke_contract.json  synthetic-only pinned real wire exchange
  README.md
```

## Quick start

```bash
cd contrib/agent-observatory
bash examples/demo.sh

# or manually
python3 -m agent_observatory import-jsonl --db /tmp/obs.db examples/synthetic_events.jsonl
python3 -m agent_observatory report --db /tmp/obs.db
```

Requires Python 3.11+ (stdlib only; tested on CPython 3.14). Arbitrary
fractional-second ISO-8601 parsing uses `datetime.fromisoformat` support added
in 3.11. No new pip dependencies.

## 1. Normalized JSONL import contract

Each non-empty line is one JSON object. `schema_version` is `"1.0"`.

| Field | Required | Type | Meaning |
| --- | --- | --- | --- |
| `schema_version` | yes | string | Contract version (`"1.0"`). |
| `city_id` | yes | string | City that observed the event. |
| `host_id` | yes | string | Host that observed the event. |
| `provider` | yes | string | Runtime/provider, e.g. `codex`, `claude`, `dsh`. |
| `session_id` | yes | string | Provider session id. |
| `event_id` | yes | string | Event id, unique within the canonical identity. |
| `timestamp` | yes | string | Timezone-aware ISO-8601; normalized to UTC microseconds. |
| `kind` | yes | string | Event kind (`command`, `tool_call`, `tool_result`, ...). |
| `title` | no | string | Human title. Never part of identity. |
| `text` | no | string | Observed text. Never part of identity; treated only as data. |
| `tool_name` | no | string | Tool/program name. |
| `tool_call_id` | no | string | Links a result to its invocation. |
| `command` | no | string | Command text. Never executed. |
| `exit_code` | no | integer | Observed exit code; missing means unknown. |
| `duration_ms` | no | integer | Observed duration; must be nonnegative. |
| `model` | no | string | Model that produced the event. |
| `repo` | no | string | Repository. |
| `commit_sha` | no | string | Commit. |
| `parent_session_id` | no | string | Parent session, if any. |
| `bead_id` | no | string | Related bead. |
| `formula_id` | no | string | Related formula. |
| `usage` | no | object/null | Nullable token counts. |

`usage` accepts `input_tokens`, `output_tokens`, `cache_read_tokens`,
`cache_write_tokens`, `total_tokens`; each is a nonnegative integer or null.
**Missing is distinct from zero**: absent usage stays NULL, and
`{"input_tokens": 0}` stores `0`.

### Evidence semantics

Records are **observed evidence**. A tool request (`tool_call`) does not prove
the tool executed, and does not prove success. Only a result event with an
`exit_code` carries an observed outcome; a missing `exit_code` is reported as
`unknown`, never as success.

### Strictness and safety

- Required fields must be present, non-empty strings; `bool` is never accepted
  as an integer.
- **Timestamps must be timezone-aware** (an explicit offset or `Z`). Naive
  timestamps are rejected because their timezone is ambiguous. The stored
  `timestamp` is normalized to UTC at microsecond precision so plain string
  ordering equals chronological ordering across mixed offsets and mixed
  fractional widths; `duration_ms`, when present, must be nonnegative.
- Optional fields, when present and non-null, must have the declared type.
- **Unknown keys are dropped** and stay NULL in the projection. There is no
  column for secrets or raw chain-of-thought, and none is stored by default.
- **Canonical event identity is `city_id/host_id/provider/session_id/event_id`**
  -- never `title` or `text`. The same event id under a different provider or
  host is a different event.
- Source `source_path`, `source_sha256`, and 1-based `source_line` are preserved
  for every event. The exact input `timestamp` is retained separately as
  `observed_timestamp` provenance; it is not part of the payload hash.
- Identity components are unrestricted strings, so reports and CLI records use a
  collision-free JSON-array encoding of the identity tuple (for example
  `["city-a","host-a","codex","session-1"]`) rather than a delimiter join.
- Instruction-like text inside `text` or payloads is stored and reported only as
  data; it never becomes instructions to any model.

## 2. SQLite projection

- Schema version is stored in `PRAGMA user_version` and `schema_meta`. Opening a
  store with an unknown/future version raises `SchemaVersionError`. The current
  version is **3**; the payload hash no longer covers identity fields, so a
  version-2 projection must be rebuilt rather than reused.
- Import is **per-file atomic**: the whole file is parsed and type-checked
  before writing, and all writes happen in one transaction. A malformed or
  truncated line reports `path:line` and commits nothing. Records are split on
  **LF only** (a trailing CR is stripped), so U+2028/U+2029/U+0085 characters
  that are legal unescaped inside JSON strings never split a record or shift
  reported line numbers.
- Import is **idempotent**: re-importing an identical file is skipped, and
  appending to a file imports only new events. Repeated files never duplicate
  events or usage.
- A **conflicting reuse of a canonical event identity** (same identity,
  different payload) raises `ImportConflictError` and rolls back rather than
  silently overwriting.
- Each `sessions` row records `first_timestamp` as the **minimum** observed
  timestamp; importing an earlier event later lowers it.
- Tables: `events`, `event_usage`, `sessions`, `imported_files`, `jev_requests`,
  `classifications`, `classification_answers`.
- Classifications are **immutable**. They are keyed by subject (`event`/`session`
  snapshot hash), taxonomy version, question hash, and model version. Replaying
  an identical response deduplicates; a different response for the same key is
  rejected (`LabelConflictError`) rather than overwriting. The existence check
  and the insert run in one `BEGIN IMMEDIATE` transaction, so a concurrent
  UNIQUE collision is folded into the same dedupe/conflict outcomes. Complete
  probability distributions and response provenance are stored.

### Subject snapshot hashes

`event` snapshots hash the canonical identity plus the event payload hash;
`session` snapshots hash the session identity plus its ordered event payload
hashes. Both are deterministic. The **payload hash covers normalized content
fields only**: canonical identity (`city_id`/`host_id`/`provider`/`session_id`/
`event_id`) is matched separately and is covered by the event snapshot hash.

## 3. Deterministic report

`report` emits JSON with sorted keys and no timestamps, so runs over the same
projection are byte-identical. It contains:

- `coverage`: `sessions`, `events`, `by_provider`, `by_kind`, and
  `missing_fields` NULL counts.
- `tool_counts`: **invocation-only** counts per `tool_name`. A result event that
  repeats a tool name never adds a count. An invocation with no `tool_name` is
  counted under `unknown`, so `tool_counts` and `observed_outcomes` describe the
  same set of invocations.
- `command_categories`: counts for every category across distinct invocations.
  Result events and arbitrary prose records are never promoted to invocations,
  even when they contain a command. Recognition is conservative and
  non-executing; compound commands may have multiple categories; anything
  unrecognized falls back to `unknown`. Categories: `search`, `read`, `edit`,
  `test`, `lint`, `typecheck`, `build`, `package`, `git`, `worktree`, `ci`,
  `review`, `dispatch`, `wait`, `unknown`.
- `observed_outcomes`: `success`/`failure`/`unknown` by category and in total,
  counted once per invocation. An invocation's outcome is paired to a result by
  `(session, tool_call_id)`. Invocations with no `tool_call_id` are keyed by
  their own event id in a **separate namespace**, so a result whose
  `tool_call_id` happens to equal an invocation's event id cannot pair with it;
  a result with no `tool_call_id` likewise cannot pair. An invocation still
  inherits its own categories even when the result carries no command. A
  request-only event stays `unknown` even if it carries a claimed exit code; a
  directly observed `command` may use its own exit code. Missing `exit_code` is
  `unknown`, never success. Orphan result events (no matching invocation) are
  still observed once, but never become invocations.
- `test`: `invocations` and `results` (`passed`/`failed`/`unknown`) are reported
  separately. Duplicate or conflicting result events never inflate invocations;
  conflicting exit codes for one invocation stay `unknown`.
- `session_steps`: per collision-free session identity key, ordered `sequence` of
  kinds and `transitions` counts.

The report includes explicit notes that counts are observational and do not
establish effectiveness, causality, or cost.

## 4. Jev request builder

`build-request` produces a `POST /v1/systemone` body from an **explicitly
supplied sanitized state JSON file**. The wire shape follows the real TypeSafe
API reference:

```json
{
  "model": "jev-1.13.0",
  "state": { "...": "caller-supplied only" },
  "questions": {
    "primary_intent": {
      "type": "choice",
      "instructions": "Classify the primary requested software task ...",
      "criteria": {
        "bugfix": "Correct an existing defect.",
        "implementation": "Add new behavior ...",
        "unknown": "Insufficient evidence ..."
      }
    },
    "requests_test": {
      "type": "noul",
      "instructions": "What is the probability that ...?",
      "criteria": { "true": "...", "false": "..." }
    }
  }
}
```

- There is **no top-level `instructions` field**, no per-question `description`,
  and no `options` list. Question `type` is lowercase `choice`/`noul`; a choice
  question's `criteria` keys are its options, and each question carries its own
  `instructions`.
- `primary_intent` is a `choice` whose criteria include an `unknown` option and
  the categories `bugfix`, `implementation`, `pr_review`, `adversarial_review`,
  `planning_spec`, `test_lint_build_ci`, `dependency_worktree_agent_ops`,
  `research_docs`.
- `scope` (`small`/`large`/`unknown`) is judged **independently of intent**.
- Overlapping labels are `noul` yes/no probability questions with explicit
  `true`/`false` criteria (a probability, not an unanchored degree score).
- Taxonomy definitions live in the versioned `taxonomy/jev_taxonomy_v1.json`
  (taxonomy version `1.1.0`). The question hash covers the taxonomy version,
  model, and every effective instruction and criterion, so any rubric change
  produces a new classification key.
- **Question map keys are identifiers, not model instructions**; instructions
  are carried in each question's `instructions` value.
- The serialized body is capped at **24 KB of UTF-8** (`REQUEST_BYTE_CAP`).
  Exceeding it raises `RequestByteCapExceeded` instead of silently truncating.
  This is a **byte safety cap, not tokenizer proof**.
- The builder never reads imported event text. An empty state with no caller
  input yields an empty `state`, so untrusted transcript text cannot leak into a
  request implicitly.

## 5. Saved response validation

`import-response` validates a saved response against the stored request:

- `model` must match; `usage.input_tokens`/`output_tokens` must be nonnegative
  integers.
- Question ids and lowercase types must match exactly (no missing/extra answers).
- `choice` answers: the `choice` field must be one of the question's criteria
  keys; `probabilities` must cover every criterion with finite values in `[0,1]`
  summing to ~1; `confidence` finite in `[0,1]`. The previously invented
  `value` field is not accepted.
- `noul` answers: exactly one finite `noul` probability in `[0,1]` and **no
  confidence field**.
- Each answer object accepts only its wire keys (`type` plus `choice`/
  `confidence`/`probabilities` for choice, or `noul` for noul). Any **unknown
  key is rejected** with a `ContractError` naming it; it is never silently
  dropped, so two responses with identical answers but different junk keys can
  never be mistaken for a label conflict.
- Non-finite JSON (`NaN`/`Infinity`) is rejected.
- Nothing is fabricated: missing labels, missing confidences, and live calls are
  all errors, not defaults. `tests/fixtures/jev_smoke_contract.json` pins a
  synthetic-only real exchange so the wire shape cannot silently regress.

## 6. Live transport credential

`classify` reads the bearer token from the runtime environment only (never from
argv, the repository, or a config file):

| Source | Precedence | Notes |
| --- | --- | --- |
| `TYPESAFE_API_KEY` | 1 (preferred) | Primary variable name. |
| `JEV_API_KEY` | 2 (alias) | Accepted for backward compatibility. |
| `JEV_KEY_FILE` | 3 | Path to a key file. |

A key file is decoded as `utf-8-sig` (a UTF-8 BOM is stripped per line) and
parsed under a strict contract; the first accepted line wins:

- `[export ]NAME=value` or `NAME: value` where `NAME` is `KEY`, `API_KEY`,
  `APIKEY`, `SECRET`, `TOKEN`, `TYPESAFE_API_KEY`, `JEV_API_KEY`, or any name
  ending in `KEY`, and `value` is a single `[A-Za-z0-9_.-]{16,}` token after
  stripping surrounding quotes/backticks;
- `Bearer <token>`;
- a single bare token line.

Blank lines and `#` comments are skipped. Prose, labels, note pointers
(`API key: (see vault)`), and markdown labels are **never** returned as a
credential: if no accepted line exists, `load_credential` raises a
`CredentialError` naming every skipped line number.

Other transport rules worth knowing: `timeout_seconds` is capped at 300 s;
usage is validated before it is charged and only the validated integer token
fields are exposed; response bodies are read in bounded chunks on both the 2xx
and error paths; the circuit breaker persists its state and reserves a single
half-open probe.

## 7. Optimization change and exposure registry

`changes-sync` imports an explicit, versioned **change bundle** (there is no live
PR crawler and no network access). Every screened change is retained, whether or
not it looks like an optimization, so the screening denominator is never
silently reduced.

- **Screening.** Changes are screened against the versioned optimization
  categories from `implementation-plan.md` (`package_resolution`,
  `dependency_footprint`, `test_selection`, `test_fixtures_parallelism`,
  `lint_typecheck`, `build_cache_bundle`, `ci_runner_cache`,
  `worktree_lifecycle`, `agent_runtime`, `host_io_network`,
  `telemetry_overhead`). The screen is deliberately conservative: a category is
  assigned only with retained path/keyword evidence, and a change with no
  category and no positive non-optimization evidence stays `unknown`. Both
  `non_optimization` and `unknown` remain in the denominator; only
  `optimization` changes become registered interventions. Non-PR intervention
  kinds (`config`/`model`/`runtime`/`prompt`/`toolchain`/`pack`/`host`) default
  to their natural category because they are interventions by construction;
  reviewed `classification`/`category_hint` still overrides.
- **Immutable records.** A PR is identified by repository and number; a non-PR
  intervention (config, model, runtime, prompt, toolchain, pack, host) by its
  immutable `artifact_digest`. Re-importing identical content deduplicates;
  re-importing the same identity with different content is refused
  (`RegistryConflictError`) rather than silently overwritten.
- **Activations.** A change can have several activation records (merge, deploy,
  config toggle, model/runtime/prompt switch, package/toolchain upgrade, host
  tuning), each with an optional scope `target`, validity interval, and a
  fingerprint (`commit_sha`, `artifact_digest`, `config_digest`, `model`,
  `toolchain`, `package`, `host`). A merged PR without an explicit activation
  gets an implicit merge activation from its merge commit, so the registry is
  self-sufficient while still requiring observed evidence.

`exposure` joins registered changes to observed session evidence -- `commit_sha`
and `model` values from the projection plus explicitly supplied
`session_fingerprints` -- and emits the deterministic optimization ledger. The
join never treats a merge as exposure:

- `exposed` requires positive evidence: an exact observed commit, commit
  ancestry from the change to an observed commit, or an observed
  artifact/config/model fingerprint inside the activation's validity interval.
- `unexposed` requires a decision: a known observed commit that provably does
  not contain the change, or a session that predates activation / follows
  deactivation, or an observed conflicting fingerprint.
- anything else is `unknown`. An incomplete commit graph, a session with no
  commit evidence, and an unobserved fingerprint are all ambiguous, never
  success. A pre-merge worktree (commit is an ancestor of the merge) is
  `unexposed`; a delayed deployment (activation still pending) is `unexposed`.

Each ledger entry carries `baseline` and `price` as `present`/`unknown`, and
`missingness` counts unknown baselines and prices. They are never synthesized;
effect estimates and cost accounting are M6, not this slice.

Bundle contract (schema version `1.0`):

```json
{
  "schema_version": "1.0",
  "changes": [
    {"repo": "gascity", "kind": "pr", "pr": 6, "title": "perf: cache the build",
     "merge_sha": "<sha>", "merged_at": "2026-09-21T10:00:00Z",
     "changed_paths": ["Makefile"], "baseline": {"metric": "test_s", "value": 12.0, "unit": "s"}}
  ],
  "activations": [
    {"change_ref": {"repo": "gascity", "kind": "pr", "pr": 6}, "mechanism": "deploy",
     "target": "gateway-llm", "activated_at": "2026-09-22T00:00:00Z",
     "fingerprint": {"type": "commit_sha", "value": "<sha>"}}
  ],
  "commit_graph": {"commits": [{"sha": "<sha>", "parents": ["<parent>"]}]},
  "session_fingerprints": [
    {"session": ["city", "host", "codex", "session-1"], "type": "config_digest",
     "value": "<digest>", "observed_at": "2026-09-22T00:30:00Z"}
  ]
}
```

## CLI reference

```
agent-observatory import-jsonl --db DB FILE [FILE ...]
agent-observatory report --db DB [--out FILE]
agent-observatory build-request --state STATE.json
    [--db DB] [--subject-kind event|session]
    [--session '["city","host","provider","session"]' | city|host|provider|session]
    [--event-id ID] [--snapshot-hash HASH] [--taxonomy PATH] [--out FILE]
agent-observatory import-response --db DB --response RESP.json --request-hash HASH
agent-observatory classify --db DB --state STATE.json
    [--subject-kind event|session]
    [--session '["city","host","provider","session"]' | city|host|provider|session]
    [--event-id ID] [--snapshot-hash HASH] [--taxonomy PATH]
    [--config CONFIG.json] [--timeout SECONDS] [--max-attempts N]
    [--max-requests N] [--max-tokens N] [--max-cost-usd USD]
    [--price-per-million-input-usd USD] [--price-per-million-output-usd USD]
    [--allow-model-drift] [--out FILE]
agent-observatory changes-sync --db DB --input BUNDLE.json
agent-observatory exposure --db DB [--out FILE]
agent-observatory --version
```

When `--snapshot-hash` is supplied together with `--db` and `--session`, the
explicit value is cross-checked against the projection and a mismatch is refused
rather than classifying a phantom subject.

A missing or unreadable input/output path is reported on stderr as
`error: <path>: <reason>` with exit status 1, never a traceback. A local SQLite
failure (for example `--db /dev/null`) is reported the same way.

## Tests

```bash
python3 -m unittest discover -s contrib/agent-observatory/tests -v
```

The suite covers duplicate replay, same-session different providers/hosts and
delimiter-colliding identities, conflict rollback, truncated input and LF-only
line numbering (including records whose text contains U+2028/U+2029/U+0085),
sessions `first_timestamp` as a running minimum, the classification UNIQUE race,
nonzero vs unknown exit codes, invocation/result pairing (including that a
result call id can never pair with an invocation's fallback event id), orphan
and duplicate and conflicting results, request-only vs directly observed
outcomes, unnamed-tool invocations counted under `unknown`, tokens missing
vs zero, timezone-aware timestamp normalization and chronological ordering across
offsets and fractional precision, the request byte cap, the real lowercase
Jev question/answer wire shape and unknown-answer-key rejection, the
missing-input-file CLI error contract, payload-hash identity exclusion, ambiguous
commands, instruction-like text treated as data, question-hash sensitivity to
instructions/criteria, and invalid/NaN responses. `examples/demo.sh` runs the CLI
end to end against the synthetic fixture; replay preserves event and
classification counts.

## Limitations and next adapter requirements

- Import adapters for real provider formats (codex, claude, dsh) are **not**
  implemented; callers must supply already-normalized records. Each adapter must
  map provider events into this contract, compute stable event ids, and keep raw
  text out of default storage.
- Session snapshots grow with session length; large sessions may need chunked
  subjects before the 24 KB request cap matters.
- The byte cap approximates wire size, not model tokens; a tokenizer-aware cap is
  future work.
- Live transport implements bounded retries, rate limiting via budgets, and cost
  accounting (see section 6), but there is no provider-side rate-limit discovery
  beyond `Retry-After`, and the token/dollar ceilings can overshoot by at most
  one request because usage is only known after a response.
- Classification is append-only by key; re-labelling with a new taxonomy or
  model version is how labels evolve.
- Scope docs are maintained separately by the Mayor in
  `city/plans/jev-agent-observatory`.
