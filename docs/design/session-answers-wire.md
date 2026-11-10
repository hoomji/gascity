---
title: "Session answers: open wire design"
description: Proposed wire shapes for the six session-answers gaps the T3 spike left open — job-scoped approvals, questions, attachments, adapter options, model passing, and adapter supply.
---

The T3 spike proved the session-answers surface end to end: approvals
surface on pending, answers go through respond, and the transcript carries
the record. This lane already closed the three build-ready rows — typed
replies beside an answer, bead lineage on requests, and the Claude ACP
guide fix. The six rows below need design rulings before anyone builds
them. For each: the recommended shape, what it costs, and what it needs
from the provider side. The decision asks for the mayor are collected at
the end.

Background: the spike's decision map sends `allow_once` to `approve`,
`allow_always` to `approve_always`, `deny` to `deny`, and `cancel` to
`cancel`, always with the request id. Transports are tmux (gc drives the
harness's terminal) and [ACP](/reference/exec-session-provider) (JSON-RPC
over stdio through an adapter; see
[configuring an agent](/guides/configuring-an-agent) for the adapter
setup). Everything proposed here is additive to the current wire.

## At a glance

| Gap | Today | Recommended shape |
|---|---|---|
| `allow_job` answer kind | 409 unknown action | `allow_job` approves and records for the job; recurrences carry the record |
| Question requests | tmux answers single-select only; ACP surfaces none | one `select` shape with `option_ids` / `text` on both transports |
| Attached files in answers | no files on respond | inline `files` where the transport takes them, 422 where it cannot |
| Codex approval/sandbox options | adapter CLI flags only | `_meta.codex` on session/new rendered from provider options |
| Model passing | `--model` CLI flag; `opencode acp` rejects it | model in session/new for ACP transports |
| Adapter supply | cities install floating versions | pin-by-default with checksums; vendor later |

## `allow_job` answer kind

**Today.** `allow_job` answers 409 unknown action. The decision map has
approve (once), approve-always (session), deny, and cancel — no job
scope, so a helper that wants "yes for this assignment" has no word for
it.

**Recommended shape.** `action: "allow_job"` approves the pending request
and records the approval for the job named by `metadata.job_id`,
defaulting to the session's active bead (the pending `lead`). When an
identical request recurs in the same job, gc annotates it with the prior
record (`prior_decision: allow_job` on the pending shape) instead of
surfacing it cold, and the client applies its own policy — record in gc,
decision in the client. The annotation is additive; clients that ignore it
see today's behavior.

**What it costs.** A job-scoped approval record with a lifetime (the job's
lifetime, then drop), a request-signature function per transport (exact
tool-call identity for ACP, the existing prompt hash for tmux), one
additive pending field, and an audit event when a record is written and
when one annotates a recurrence.

**What it needs from the provider.** Stable request identity across
recurrences. ACP request ids are per-request, so the match key must come
from the permission payload itself (tool call and arguments) — the
adapter has to send enough to match on. tmux already hashes the prompt.

**Open.** Is the active bead the right default job id, and should gc
itself auto-answer exact-signature recurrences, or stay with
record-and-annotate? See ask 1.

## Question requests with an answer path

**Today.** tmux answers single-select text questions (`select`, with text
naming the option). Multi-select, previews, free-text inputs, and chat
pages are terminal-only — and ACP adapters surface no questions at all,
with no terminal to fall back to. Structured history already models
questions (question, header, options, multi-select); the wire has no
matching answer path.

**Recommended shape.** One answer shape on both transports:
`action: "select"` with `option_ids: [...]` for choice questions (one or
many) and `text` for free-text answers, against a `question`-kind pending
that carries a `questions` block shaped like the structured-history
question. tmux extends its dialog parsing to multi-select and free-text
inputs; ACP gains the same answer shape once adapters emit question
requests.

**What it costs.** tmux dialog parsing per harness — each new dialog shape
is parser work plus fixtures — and additive wire fields on the pending
shape. The history projection already exists.

**What it needs from the provider.** Harness dialog stability on tmux.
On ACP, the gap is emission: no adapter emits question requests today, so
this needs upstream ACP question tooling or a gc-defined extension the
adapters adopt before any ACP question can be answered at all.

**Open.** How does a free-text answer name its question inside a
multi-question prompt — a `question_id` alongside `text`? See ask 2.

## Attached files in answers

**Today.** Respond carries action, metadata, request id, and text — no
files. An answer cannot bring evidence with it.

**Recommended shape.** Inline `files: [{name, media_type, content}]` on
respond, bounded in size, delivered where the transport takes them. ACP
prompts accept images and embedded context, so adapters can take
attachments; tmux cannot attach to a session, so it refuses with a named
422 — the same deliver-or-refuse contract typed reply text follows.

**What it costs.** Size limits and validation, a storage and redaction
story (do attachments persist in transcripts and session logs, or drop
after delivery?), and per-transport delivery or refusal.

**What it needs from the provider.** Adapter intake for images and
context parts on ACP (the protocol supports it; each adapter wires it).
Nothing on tmux — refusal is the correct behavior there.

**Open.** Size cap and retention: transcript, bead, or drop after
delivery? See ask 3.

## Codex approval and sandbox options through `_meta.codex`

**Today.** Codex approval and sandbox settings travel as adapter CLI
flags (`--sandbox`, `--codex-args approval_policy=...`). That works —
the spike ran it — but it lives outside gascity options, so provider
defaults cannot express it.

**Recommended shape.** `_meta: {codex: {approval, sandbox}}` on
session/new, rendered from gascity provider options (`option_defaults`),
with CLI flags kept as the interim path until the adapter reads `_meta`.
Precedence during the dual-path period: explicit flags beat rendered
`_meta`, and the resolved session records which won.

**What it costs.** options_schema additions for the approval and sandbox
enums, the session/new extension, and the dual-path period with
precedence rules and resolved-session reporting.

**What it needs from the provider.** This is the crux: codex-acp is a
third-party prebuilt binary whose surface today is CLI flags only — its
launcher shim passes flags through and reads no `_meta`. Somebody has to
teach it `_meta.codex`: upstream adoption, a maintained wrapper, or a
fork. Until then the flags remain the only path.

**Open.** Who owns the adapter relationship? See ask 4.

## Model through session/new instead of flags

**Today.** The model renders from options into a `--model` CLI flag on
the launch command. `opencode acp` rejects `--model`, so ACP sessions
fall back to the config default with no per-session override.

**Recommended shape.** For ACP transports the model travels in
session/new rather than on the command line; tmux CLIs keep the flag.
options_schema gains transport scoping: each option renders to flag,
session/new, or both, per transport. Whether the session/new field is a
top-level `model` or `_meta.model` is ask 5 — top-level reads cleaner,
`_meta` is the established extension point.

**What it costs.** Transport-scoped option rendering, the session/new
extension, and a per-adapter support matrix, since no standard ACP model
parameter exists — this is extension by convention, adapter by adapter.

**What it needs from the provider.** Each ACP adapter must honor the
session/new model: opencode, Claude, and Codex adapter work, in that
order of pain (opencode is blocked today; the others have working flag
or default paths).

**Not recommended.** A per-command flag denylist (drop `--model` for
`opencode acp`) is cheap but whack-a-mole: it bakes harness specifics
into gc, one flag at a time.

## Bundle, vendor, and pin for ACP adapters

**Today.** Cities install adapters at setup (npm into an `adapters/`
directory); versions float unless the city pins them.

**Evidence.** Floating already bit: the Claude adapter was renamed from
`@zed-industries/claude-code-acp` to
`@agentclientprotocol/claude-agent-acp`, and the new line's latest
(0.102.0, checked 2026-11-10) does not install — a missing dependency.
The last verified pin is `@zed-industries/claude-code-acp@0.16.2`.
Meanwhile `codex-acp@1.11.0` is a JS shim over a third-party prebuilt
binary per platform (`@baldaworks/codex-acp-<os>-<cpu>`).

**Recommended shape.** Pin by default: `gc init` writes pinned adapter
versions with checksums, lockfile-style, and cities fetch at setup.
Revisit vendoring or bundling into gc once the adapter lines stabilize —
floating plus a rename just broke installs, so hermetic can wait for
calm.

**What it costs.** Template, lock, and checksum verification now;
per-platform release plumbing later if gc ever vendors (the prebuilt
codex binary makes that the expensive half).

**What it needs from the provider.** Release stability under any option.
Vendoring additionally needs a build story per platform.

**Open — the trust question.** The codex binary is prebuilt by a third
party. What provenance does gc require before it pins or bundles it —
checksums, reproducible builds, signatures? Until the mayor sets the
bar: pin plus checksum, with the gap documented. See ask 6.

## Decision asks

1. `allow_job`: is the active bead the default job id, and does gc
   auto-answer exact-signature recurrences or record-and-annotate?
2. Questions: `question_id` for free-text answers, and whose extension
   defines ACP question emission?
3. Attachments: size cap and retention — transcript, bead, or drop after
   delivery?
4. `_meta.codex`: upstream PR, wrapper, or fork — who owns the adapter
   relationship?
5. Model: top-level session/new `model` or `_meta.model`?
6. Adapters: pin-by-default now, and what trust bar for the prebuilt
   codex binary?
