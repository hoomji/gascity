# Agent Observatory (offline foundation slice)

A dependency-free Python 3 CLI and library for **normalized evidence import,
classification storage, and deterministic reporting** over Gas City agent
activity. It is the first bounded slice of the Jev-powered historical/live chat
analysis effort.

This is deliberately **not** the whole product and **not** production-ready:

- **No network transport.** The Jev request builder only emits a `/v1/systemone`
  request body; nothing is sent. Saved responses are validated offline.
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

Requires Python 3.9+ (stdlib only; tested on CPython 3.14). No new pip
dependencies.

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
| `timestamp` | yes | string | ISO-8601. |
| `kind` | yes | string | Event kind (`command`, `tool_call`, `tool_result`, ...). |
| `title` | no | string | Human title. Never part of identity. |
| `text` | no | string | Observed text. Never part of identity; treated only as data. |
| `tool_name` | no | string | Tool/program name. |
| `tool_call_id` | no | string | Links a result to its invocation. |
| `command` | no | string | Command text. Never executed. |
| `exit_code` | no | integer | Observed exit code; missing means unknown. |
| `duration_ms` | no | integer | Observed duration. |
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
  as an integer; `timestamp` must parse as ISO-8601.
- Optional fields, when present and non-null, must have the declared type.
- **Unknown keys are dropped** and stay NULL in the projection. There is no
  column for secrets or raw chain-of-thought, and none is stored by default.
- **Canonical event identity is `city_id/host_id/provider/session_id/event_id`**
  -- never `title` or `text`. The same event id under a different provider or
  host is a different event.
- Source `source_path`, `source_sha256`, and 1-based `source_line` are preserved
  for every event.
- Instruction-like text inside `text` or payloads is stored and reported only as
  data; it never becomes instructions to any model.

## 2. SQLite projection

- Schema version is stored in `PRAGMA user_version` and `schema_meta`. Opening a
  store with an unknown/future version raises `SchemaVersionError`.
- Import is **per-file atomic**: the whole file is parsed and type-checked
  before writing, and all writes happen in one transaction. A malformed or
  truncated line reports `path:line` and commits nothing.
- Import is **idempotent**: re-importing an identical file is skipped, and
  appending to a file imports only new events. Repeated files never duplicate
  events or usage.
- A **conflicting reuse of a canonical event identity** (same identity,
  different payload) raises `ImportConflictError` and rolls back rather than
  silently overwriting.
- Tables: `events`, `event_usage`, `sessions`, `imported_files`, `jev_requests`,
  `classifications`, `classification_answers`.
- Classifications are **immutable**. They are keyed by subject (`event`/`session`
  snapshot hash), taxonomy version, question hash, and model version. Replaying
  an identical response deduplicates; a different response for the same key is
  rejected (`LabelConflictError`) rather than overwriting. Complete probability
  distributions and response provenance are stored.

### Subject snapshot hashes

`event` snapshots hash the canonical identity plus the event payload hash;
`session` snapshots hash the session identity plus its ordered event payload
hashes. Both are deterministic.

## 3. Deterministic report

`report` emits JSON with sorted keys and no timestamps, so runs over the same
projection are byte-identical. It contains:

- `coverage`: `sessions`, `events`, `by_provider`, `by_kind`, and
  `missing_fields` NULL counts.
- `tool_counts`: events per `tool_name`.
- `command_categories`: counts for every category across distinct
  command-bearing invocations. Result events are never counted as invocations.
  Recognition is conservative and non-executing; compound commands may have
  multiple categories; anything unrecognized falls back to `unknown`. Categories:
  `search`, `read`, `edit`, `test`, `lint`, `typecheck`, `build`, `package`,
  `git`, `worktree`, `ci`, `review`, `dispatch`, `wait`, `unknown`.
- `observed_outcomes`: `success`/`failure`/`unknown` by category and in total.
  Missing `exit_code` is `unknown`.
- `test`: `invocations` and `results` (`passed`/`failed`/`unknown`) are reported
  separately. Result events linked by `(session, tool_call_id)` are counted once;
  duplicate result events never inflate invocation counts.
- `session_steps`: per session key, ordered `sequence` of kinds and
  `transitions` counts.

The report includes explicit notes that counts are observational and do not
establish effectiveness, causality, or cost.

## 4. Jev request builder

`build-request` produces a `POST /v1/systemone` body from an **explicitly
supplied sanitized state JSON file**:

```json
{
  "model": "jev-1.13.0",
  "instructions": "<explicit taxonomy instructions>",
  "state": { "...": "caller-supplied only" },
  "questions": { "<id>": { "type": "Choice|Noul", "description": "...", "options": [...] } }
}
```

- `primary_intent` is a `Choice` with an `unknown` option and the categories
  `bugfix`, `implementation`, `pr_review`, `adversarial_review`,
  `planning_spec`, `test_lint_build_ci`, `dependency_worktree_agent_ops`,
  `research_docs`.
- `scope` (`small`/`large`/`unknown`) is judged **independently of intent**.
- Overlapping labels are `Noul` questions.
- Taxonomy definitions live in the versioned `taxonomy/jev_taxonomy_v1.json`.
- **Question map keys are identifiers, not model instructions**; instructions
  are carried explicitly in `instructions`.
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
- Question ids and types must match exactly (no missing/extra answers).
- `Choice`: `value` must be one of the options; `probabilities` must cover every
  option with finite values in `[0,1]` summing to ~1; `confidence` finite in
  `[0,1]`.
- `Noul`: one finite value in `[0,1]` and **no confidence field**.
- Non-finite JSON (`NaN`/`Infinity`) is rejected.
- Nothing is fabricated: missing labels, missing confidences, and live calls are
  all errors, not defaults.

## CLI reference

```
agent-observatory import-jsonl --db DB FILE [FILE ...]
agent-observatory report --db DB [--out FILE]
agent-observatory build-request --state STATE.json
    [--db DB] [--subject-kind event|session] [--session city|host|provider|session]
    [--event-id ID] [--snapshot-hash HASH] [--taxonomy PATH] [--out FILE]
agent-observatory import-response --db DB --response RESP.json --request-hash HASH
agent-observatory --version
```

## Tests

```bash
python3 -m unittest discover -s contrib/agent-observatory/tests -v
```

The suite covers duplicate replay, same-session different providers/hosts,
conflict rollback, truncated input, nonzero vs unknown exit codes, tokens missing
vs zero, the request byte cap, ambiguous commands, instruction-like text treated
as data, and invalid/NaN responses. `examples/demo.sh` runs the CLI end to end
against the synthetic fixture; replay preserves event and classification counts.

## Limitations and next adapter requirements

- Import adapters for real provider formats (codex, claude, dsh) are **not**
  implemented; callers must supply already-normalized records. Each adapter must
  map provider events into this contract, compute stable event ids, and keep raw
  text out of default storage.
- Session snapshots grow with session length; large sessions may need chunked
  subjects before the 24 KB request cap matters.
- The byte cap approximates wire size, not model tokens; a tokenizer-aware cap is
  future work.
- No transport, retry, rate limiting, or cost accounting is implemented.
- Classification is append-only by key; re-labelling with a new taxonomy or
  model version is how labels evolve.
- Scope docs are maintained separately by the Mayor in
  `city/plans/jev-agent-observatory`.
