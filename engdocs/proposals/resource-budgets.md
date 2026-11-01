---
title: "Resource Classes and Host Memory Budgets"
---

Status: proposal + v1 implementation
Scope: single host (the host the controller runs on)

## Problem

`max_active_sessions` caps how *many* sessions run. It cannot say how *big*
they are. A city that mixes light agents (a chat or triage loop, well under
1 GiB) with heavy ones (a builder that runs a compiler and a test suite,
6-10 GiB) has two bad choices today:

- set the cap for the heavy case, and leave most of the host idle when the
  work is light; or
- set the cap for the light case, and let a burst of heavy sessions exhaust
  memory, at which point the kernel OOM killer (or systemd-oomd) picks a
  victim — often the controller itself or an unrelated desktop process.

Operators who hit this end up writing an admission layer outside gc: a
wrapper that queues commands by declared memory and refuses to start what
does not fit. gc is the component that decides when a session starts, so it
is the right place for the check.

## Goals

1. Agents declare a **resource class**; classes declare a **memory
   reservation**.
2. The host declares a **memory budget** and a **reserve** held back for
   the host itself.
3. gc does not start a session whose reservation does not fit in what is
   left. The session waits, with a visible reason, and starts when capacity
   frees up.
4. No starvation: a large session is not overtaken forever by a stream of
   small ones.
5. No `[resources]` table means exactly today's behaviour.

## Non-goals (v1)

- **Live measurement.** v1 admits on declared reservations. It does not read
  RSS, cgroup `memory.current`, or PSI. See *Declared vs measured*.
- **Enforcement inside the session.** gc does not set cgroup limits on the
  session. A provider or start command may still do so. Exporting the
  class to the session environment is a follow-up: adding an env key
  changes the launch fingerprint, which would restart every classed
  session the first time a city adopts budgets.
- **Preemption.** A running session is never stopped to make room.
- **Multi-host placement.** See *Multi-host*.
- **CPU.** The class table reserves room for a `cpus` key later; v1 rejects
  unknown keys rather than silently ignoring them.

## Config surface

```toml
[resources]
# Memory gc may hand out as reservations on this host. A size ("48GiB",
# "512MiB", "1.5GB"), or "auto" for the host's physical memory. Unset: no
# memory budget.
memory = "auto"
# Held back from `memory` for the OS, the controller and anything not run by
# gc. Default 0.
reserve = "4GiB"
# Class for agents that do not name one. Unset: such agents reserve nothing
# and are never deferred.
default_class = "light"

[resources.classes.light]
memory = "1GiB"

[resources.classes.heavy]
memory = "8GiB"
# Optional: at most this many sessions of the class at once, on this host.
# 0 means the class never starts here.
max_active_sessions = 2

[[agent]]
name = "builder"
resource_class = "heavy"
```

`resource_class` is an ordinary agent field, so it can also be set from
`[[patches.agent]]` and from a rig's agent overrides. That keeps the split
clean: a pack defines agents and may suggest classes; the city that runs the
pack decides the numbers.

Sizes accept `B`, `K/KB/KiB`, `M/MB/MiB`, `G/GB/GiB`, `T/TB/TiB`, all
binary multiples (matching the existing doctor size knobs), with an optional
decimal fraction.

**Capacity** is `memory - reserve`. A class whose reservation exceeds
capacity, or whose `max_active_sessions` is 0, *never fits*: gc refuses it
on this host without blocking anything else.

### Validation

Load fails on: an unparseable size; a negative reserve, reservation or
class `max_active_sessions`; `memory` set to zero; `default_class`, or an
agent's `resource_class`, naming a class the `[resources]` table does not
define; a class with no `memory` and no `max_active_sessions`. Load warns on:
`reserve` at or above `memory`; a class that never fits while an agent uses
it; `resource_class` set when the city has no `[resources]` table (the field
is then inert, so a pack can name classes that a city has not adopted).

## What holds a reservation

A session holds its class reservation while it is *resident*: its runtime is
alive, or its state is `creating`, `start-pending`, `active`, `awake` or
`draining`. `asleep`, `drained`, `suspended`, `failed-create`, `archived`
and closed sessions hold nothing. Draining sessions still count: they are
still using memory until they exit.

The class is resolved from the session's template at decision time, so a
config change applies to running sessions on the next tick. A session whose
template no longer exists holds nothing.

## Where the check runs

There are two gates, both driven by one pure ledger (`resourceLedger`), so
they cannot disagree about arithmetic.

1. **Desired state (pool demand).** After the existing nested
   `max_active_sessions` caps admit a pool's *new* demand or
   `min_active_sessions` floor, the ledger checks it against capacity. A
   request that does not fit is not desired, so no session bead is created
   for it. This avoids creating a bead that would sit in `creating` until
   its pending-create lease rolls it back. Concrete requests (sessions that
   already exist, or are being resumed for assigned work) are always kept
   and are charged to the ledger first.
2. **Start boundary.** Every start candidate — pool, named, resumed — is
   checked immediately before the reconciler's planned-start pass. This is
   the guarantee: whatever path produced the candidate, gc will not start a
   session that does not fit. A deferred candidate is skipped without
   writes and without spending the per-tick wake budget, exactly like the
   endpoint-capacity gate.

   Under reconcile v2 (the default reconciler) the start boundary is
   `admit` (`cmd/gc/reconcile_admit.go`): the resource budget is one more
   check on every start and create intent, after its city, endpoint and
   create caps (`cmd/gc/resource_admit.go`). Pool, named and resumed starts
   and named creates all go through it. A deferred intent carries the cause
   `resource/<trace reason>`, makes no write and spends no token. Resident
   there means a census row that is active, awake or draining, a row with a
   start in flight, or an in-flight create no row carries yet. The legacy
   reconciler keeps its gate before `executePlannedStartsTraced`.

## Ordering and fairness

The start boundary orders candidates oldest-first with the existing wake
fairness key (`last_woke_at`, then `slept_at`, then creation time). The
desired-state gate keeps the nested-cap order (bead priority, then
resume-like tier).

Both gates are **head-of-line**: once a request does not fit on memory,
every later request in that pass is deferred too, even a smaller one that
would fit. Without that, a steady supply of 1 GiB sessions keeps capacity
just under 8 GiB forever and the 8 GiB session never starts. With it, the
head waits only for capacity to free, which it will, because nothing new is
admitted in front of it. Requests that *never fit* and requests stopped by
their class's own `max_active_sessions` do not block the line: they cannot
be helped by waiting, or are bounded by a separate limit.

## Interaction with `max_active_sessions`

Independent and conjunctive. The nested caps (agent, rig, workspace) run
first and decide *whether a session is wanted*; the budget decides *whether
it fits*. A session must pass both. A class `max_active_sessions` is a
fourth, per-class cap evaluated by the ledger. Nothing in the budget changes
how the existing caps count.

## Visibility

- Each deferral records a session-reconciler trace decision
  (`resource_budget_full`, `resource_queued_behind`,
  `resource_class_full`, `resource_never_fits`) with the class, the
  reservation, used and capacity.
- The controller logs one line when a session *starts* waiting and one when
  it is admitted, not one per tick.
- `gc doctor` gains a `resource-budget` check: it reports the capacity
  and the class table with the agents in each class, and warns on classes
  that never fit while agents use them.

A typed event (`session.resource_deferred`), a `gc status` line with the
current reservation, and the class in the session environment are natural
follow-ups; the first two need new event payload types and API schema, so
they are left out of v1 to keep it small.

## Declared vs measured

v1 uses declared reservations only. They are predictable (a decision can be
explained from config), testable without a real host, and the operator who
writes them already has the measurements. The one host measurement v1 makes
is `memory = "auto"`, which reads total physical memory once per tick
(`/proc/meminfo` `MemTotal` on Linux); on other platforms `auto` is a load
error.

A later proposal can add a measured mode: admit on
`min(capacity - reserved, MemAvailable - reserve)`, so a session that uses
less than its reservation leaves room and memory used outside gc is seen.
The ledger already isolates the "how much is left" question, so that is a
change to one function.

## Multi-host

v1 budgets the host the controller runs on, but the model is already keyed
by host so multi-host placement (`engdocs/proposals/multi-host-placement.md`)
extends it without a rewrite:

- `newResourceBudget(cfg, host)` returns a host's budget; only `local`
  (the top-level `[resources]`) has one today. `resourceBudgets` maps host
  name to budget.
- `resourceLedgers` holds one ledger per host; `charge` and `try` take the
  host, and head-of-line blocking is per host. A host without a ledger
  admits everything.
- `resourceHostOf(info)` names the host a session reserves on: `local` until
  placement records one on the session bead. Unplaced creates and pool
  requests reserve on `local`.
- Trace payloads carry `host`.

Placement then adds `[hosts.X.resources]` budgets and calls the per-host
ledger once per candidate host.

## Backwards compatibility

No `[resources]` table: the ledger is disabled, both gates return their
input unchanged, and no new trace or log output is produced. A
`resource_class` with no `[resources]` table is inert and draws a load
warning, so packs can ship class names before a city adopts budgets.

## Implementation shape (v1)

- `internal/config/resources.go`: `ResourcesConfig`, `ResourceClass`, size
  parsing, validation.
- `Agent.ResourceClass`, plus `AgentPatch` and `AgentOverride` fields.
- `cmd/gc/resource_budget.go`: budget resolution, the ledger, and the two
  gate functions.
- `cmd/gc/pool_desired_state.go`: desired-state gate after the nested caps.
- `cmd/gc/session_reconciler.go`: start-boundary gate before the planned
  start pass (legacy reconciler).
- `cmd/gc/resource_admit.go`, `cmd/gc/reconcile_admit.go`: the v2 gate in
  `admit`, and its input built by the planner (`reconcile_pass.go`).
- `cmd/gc/doctor_resource_budget.go`: the doctor check.
