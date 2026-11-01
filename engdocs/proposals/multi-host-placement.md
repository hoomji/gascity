---
title: "Multi-Host Placement"
---

Status: proposal (design only, no implementation)
Depends on: "Resource Classes and Host Memory Budgets"
(`engdocs/proposals/resource-budgets.md`, proposed separately), the host and
budget model this proposal extends to many hosts

## Problem

A city runs one controller, one bead store and, today, one place where
sessions run. The session provider is chosen per city (`[session]
provider`), with two narrow exceptions: `acp` per agent, and `hybrid`,
which sends sessions whose *name* contains a substring to Kubernetes.
The `ssh:` runtime (`internal/runtime/ssh`) can drive tmux on another
machine, but only as the city's single provider: every session goes to that
one box.

Operators with more than one machine work around this by running **one city
per machine** and stitching the cities together from outside gc:

- a dispatcher picks a machine for each job by its own capacity rules and
  files the work into *that* machine's city;
- each machine has its own bead store, so bead ids, mail and parents do not
  cross machines, and results are copied back as files over ssh;
- each machine needs its own gc and bd upgrades, order config and store
  retention, and drifts when one is missed;
- capacity rules (light-only machines, per-machine memory, which agent CLIs
  are installed where) live in scripts gc cannot see, so `gc status`,
  `gc doctor` and the reconciler know nothing about them.

The federation is the cost. The work itself only needs one thing gc does not
have: **choose which machine a session starts on, then run it there.**

## Goals

1. A city declares **hosts**: machines that can run its sessions, each with a
   transport, a capacity, and labels and classes it accepts or refuses.
2. The reconciler **places** each new session on one host, using the
   agent's constraints and the resource-budget fit on that host, in a stated
   preference order. The placement is recorded on the session bead.
3. A placed session runs on its host through the existing `ssh` runtime and
   tmux carrier, and reaches the **city's one bead store**, its rig checkout
   and its worktree on that host.
4. A host that is down or partitioned never causes a duplicate session, a
   reaped live session, or reopened work, by default.
5. Hosts, placements and refusals are visible in `gc status`, events,
   traces and `gc doctor`.
6. No `[hosts]` table means exactly today's behaviour.

## Non-goals

- **Provisioning machines.** Hosts pre-exist. gc does not create VMs,
  install agent CLIs or clone repositories in v1.
- **Migration.** A session never moves between hosts while it has a
  runtime or a worktree. A host loss is handled by releasing work, not by
  moving the session.
- **Multiple controllers.** One controller, one store. This is not HA and not
  cross-city federation.
- **Measured load.** As in resource-budgets v1, admission uses declared
  reservations. CPU, PSI and swap are out of scope.
- **A sandbox.** A worker host is trusted with the city's store and with
  whatever credentials its operator installs there. See *Security*.
- **Kubernetes.** `k8s` and `hybrid` stay as they are. A k8s leg in the
  placement router is a possible follow-up, not part of this proposal.

## What exists today

| Piece | Where | What it gives this design |
|---|---|---|
| ssh runtime | `internal/runtime/ssh/ssh.go` (`Endpoint`, `Conn.Exec`, `sshArgs`), `provider.go` (`Provider.Start`, line 218) | tmux-in-a-box over one-shot `ssh` calls, driven by the shared carrier (`runtime.NewTmuxCarrier`). Secret env is staged into 0600 files over stdin, never argv (`session_env_file.go`). |
| `ssh:` selection | `cmd/gc/runtime_registry.go` (`RegisterPrefix("ssh:")`); RUNTIME-SEL-013 | Anonymous form only. Keys, users and known_hosts come from the operator's `~/.ssh/config`; there is deliberately no structured ssh form. |
| Composite routing | `internal/runtime/router.go` (`Router`, `ResolveBackend`), `hybrid/hybrid.go`, `auto/auto.go` | N-backend composition already works: per-session routes, `BackendsProvider`, `BackendListingProvider`, `ListingAttestation`. `auto` seeds its route table from session beads (`SeedRoutes`, via `seedACPRoutesFromSnapshot`, `cmd/gc/providers.go:231`). |
| Partial listing | `runtime.MergeBackendListResults` (`provider_core.go:90`), `PartialListError`, `ListRunningAttested` | One unreachable backend yields a partial listing, not "zero sessions", so nothing is reaped as dead. |
| Unknown is not gone | `runtime.ErrRuntimeUnavailable` (`runtime.go:170`) vs `IsSessionGone` | A transport failure is "cannot tell". ssh exit 255 already maps to it. |
| Pod env projection | `internal/runtime/k8s/pod.go` (`projectedPodDoltEnv`, line 172), `staging.go` (`stageFiles`, line 26) | Precedent for rewriting controller-local Dolt and path env for a remote box. |
| Work-dir export | `gc runtime workdir-export`, `internal/citylayout/export.go` | A fixed denylist of secrets, stores, sockets and other agents' state; what may be copied to a box. |
| Site binding | `.gc/site.toml` (`internal/config/site_binding.go:167`) | Machine-local rig paths and identity. A host needs its own copy with its own rig paths. |
| Ownerless runtimes | `cmd/gc/doctor_ownerless_runtimes.go` | Runtimes whose `GC_SESSION_ID` names no row are found and, when dead, reaped. Works per backend. |
| Resource budgets | `engdocs/proposals/resource-budgets.md`, `cmd/gc/resource_budget.go` (`resourceLedger`) | Classes, reservations, per-class caps, head-of-line fairness. Single host today. |

The missing pieces are small: a host table, a placement decision, an N-way
router keyed by host, per-host env and path projection, a store link, and
host health.

## Host model

```toml
# The controller's own machine is always the host "local". Its budget is the
# top-level [resources] table from resource-budgets; it needs no entry here.

[hosts.host-a]
transport = "ssh"
# An ssh destination, resolved by the operator's ~/.ssh/config exactly as the
# `ssh:` prefix does today. No key paths, users or ports in gc config.
target = "host-a"
# Host-side root gc owns: the city mirror, the store link socket, staged env.
root = "/srv/gc/city-1"
labels = ["linux", "browser"]
# Placement order among otherwise equal hosts: higher first. Default 0;
# "local" defaults to 0 too.
priority = 10
# Session count cap on this host, independent of memory. Optional.
max_active_sessions = 8

[hosts.host-a.resources]
memory = "15GiB"
reserve = "2GiB"

# Per-host class override: only max_active_sessions. 0 means the class never
# starts here (the resource-budgets "never fits" rule), which is how a host
# refuses a class.
[hosts.host-a.resources.classes.heavy]
max_active_sessions = 2

# Where each rig is checked out on this host. A rig with no entry cannot be
# placed here. Paths are host paths; gc verifies but never clones (v1).
[hosts.host-a.rigs]
web = "/srv/src/web"

# Session env this host may receive. Secret-classified values are withheld
# from remote hosts unless named here (see Security).
[hosts.host-a.env]
forward = []
```

Rules:

- **Classes are city-wide.** `[resources.classes.<name>].memory` is the
  reservation of the workload, wherever it runs. A host overrides only the
  class's `max_active_sessions`. Reservations do not vary by host.
- **`local` is implicit** and uses the top-level `[resources]`. An explicit
  `[hosts.local]` may set `labels`, `priority` and `max_active_sessions`, but
  not `transport`, `target`, `root` or `rigs` (local rigs come from
  `.gc/site.toml` as today).
- **`memory = "auto"` is local only in v1.** A remote host states a size; a
  remote `auto` is a load error until the probe reads `/proc/meminfo` over the
  transport (phase 2).
- **Transport** is `ssh` in v1. The field exists so a host agent (phase 3)
  is a new value, not a new table.
- **Validation** fails load on: an unknown transport; a missing `target` or
  `root` on a remote host; a relative `root` or rig path; a rig name the city
  does not define; a class override for an undefined class; a host named
  `local` with remote-only keys. It warns on a host no agent can be placed
  on, and on an agent whose placement names no host that has its rig.

Config types go in `internal/config/hosts.go` (`HostConfig`,
`HostResources`, `HostEnv`), hung off `City` (`internal/config/config.go:221`)
as `Hosts map[string]HostConfig`. Hosts are city config, not pack config: a
pack may suggest `placement` on its agents, the city decides which machines
exist.

## Agent placement

```toml
[[agent]]
name = "builder"
resource_class = "heavy"
[agent.placement]
# Hosts this agent may run on. Default: ["local"].
hosts = ["host-a", "host-b", "local"]
# Every label must be on the host.
require_labels = ["browser"]
# Tried first, in order, before the hosts' own priority order.
prefer = ["host-b"]
```

- **Default is local.** An agent with no `placement` runs on `local`, so
  declaring hosts moves nothing until an agent opts in. A city may set
  `[placement] default_hosts = [...]` to change the default for all agents.
- `placement` is an ordinary agent field, so `[[patches.agent]]` and rig
  agent overrides can set it. A pack can say "needs label browser"; the city
  says which hosts exist.
- **ACP agents are local-only in v1** (the ACP transport speaks stdio to a
  local process). Placing one remotely is a load error.
- A **named session** is placed like any other. To pin one (a coordinator,
  say), give it a single host.

## Placement

Placement is one pure function, `placeSession`, in `cmd/gc/placement.go`:

```
placeSession(req, hosts, ledgers, health, sticky) -> (host, verdict)
```

1. **Sticky first.** If the session bead already records a host
   (`gc.host`), that is the only candidate. A session never changes host
   while its bead lives: its worktree, uncommitted work and provider
   conversation are on that host. If the host cannot take it now, the
   session waits (`placement_sticky_wait`). A closed pool session's
   successor is a new bead and is placed fresh.
2. **Eligible hosts** are the agent's `placement.hosts` (or the default),
   minus any host that: is not `up` (see *Health*); lacks a label in
   `require_labels`; has no checkout for the agent's rig; refuses the
   agent's class (class `max_active_sessions = 0` on that host); has not
   reported the agent's provider command on its last probe; or fails the
   version policy.
3. **Order:** the agent's `prefer` list in order, then host `priority`
   descending, then most free reservation (capacity minus reserved), then
   name. Most-free-last spreads load among equals without making the order
   depend on anything but config and the ledger.
4. **Fit:** walk the order and take the first host whose resource ledger
   admits the request (resource-budgets arithmetic, unchanged) and whose
   host `max_active_sessions` is not reached.
5. **No fit:** defer, with the most specific reason across hosts.
   Head-of-line holds **per host**: the deferred request blocks its *home*
   host (the first host in its order where it could ever fit) and no other.
   Smaller requests keep placing on other hosts; nothing jumps ahead of the
   big one on its home host, so it is not starved.

Fallback is the order itself. A city that wants "remote first, local when
remote is full" lists `local` last. gc never places on a host the agent did
not list, so there is no silent fallback.

### Where it hooks

- **Desired state (pool demand).** `applyNestedCaps`
  (`cmd/gc/pool_desired_state.go:973`) already admits new demand through the
  resource ledger on the resource-budgets branch. With hosts, it calls
  `placeSession` instead of the single-ledger check, and records the chosen
  host on the request (a new `SessionRequest.Host`, `pool_desired_state.go:21`).
  The v2 decide reuses this pure pool code, so both reconcilers get it.
- **Create.** The create effect (`cmd/gc/allocator_create.go`,
  `createEffects.run` / `verifiedMetadata`) writes `gc.host` with the other
  session-bead metadata. A session bead with no `gc.host` is `local`, so
  existing beads need no migration.
- **Start boundary.** Every start re-checks fit on the recorded host:
  legacy at the resource-budgets gate before `executePlannedStartsTraced`
  (`cmd/gc/session_reconciler.go:5234`); v2 in `admit`
  (`cmd/gc/reconcile_admit.go:257`), as a per-host cap class next to the
  endpoint gate, so a deferred start spends no wake budget.
- **Launch.** `startLaunch` (`cmd/gc/reconcile_effect_launch.go:308`) already
  resolves the leaf with `runtime.ResolveBackend`. With the placement router
  installed, that leaf is the host's leg. No launch code changes beyond
  registering the route before `Start`, as it does today for ACP
  (`RouteACP`).

## Transport

### Placement router

A new composite, `internal/runtime/placement`, generalises `hybrid` from two
legs chosen by name substring to N legs chosen by a route table:

- **Legs:** `local` is the city's base provider (tmux by default). Each
  remote host is `ssh.NewSeamBacked(ep)` with `ep` built from `target`.
- **Routes:** session name to host. Seeded from session beads' `gc.host` on
  every snapshot (the same path as `seedACPRoutesFromSnapshot`), and set at
  create and launch (`RouteHost(name, host)`). An unseeded name answers
  `Route{Known: false}`; every verb on it returns `ErrRuntimeUnavailable`,
  never "not found".
- **Capabilities:** implements `Router`, `BackendsProvider`,
  `BackendListingProvider` and `ListingAttestation` (attested only when every
  leg attests and every leg answered). It forwards the optional interfaces the
  way `hybrid` does.
- **Construction:** `resolveSessionTransportProvider`
  (`cmd/gc/providers.go:297`) wraps the base provider when `cfg.Hosts` is
  non-empty, before the ACP `auto` wrapper, so ACP keeps routing locally.

### Per-host projection

The ssh provider passes `cfg.WorkDir` and `cfg.Env` to the box verbatim. A
remote session needs host paths and a host-reachable store. The placement
router applies a projection to `runtime.Config` before calling the leg's
`Start` (and `Relaunch`, `RunLive`), so every call path gets it:

- **Paths.** `GC_CITY_PATH`, `GC_CITY_ROOT`, `GC_CITY_RUNTIME_DIR` map to the
  host's city mirror (`<root>/city`); `GC_RIG_ROOT` and `WorkDir` map
  from the controller's rig path to the host's rig path
  (`[hosts.X.rigs]`). A path under neither root is a start error
  (`placement_unmapped_path`), never a silent pass-through to a path that
  does not exist on the host.
- **Store.** `GC_DOLT_*` and `BEADS_DOLT_SERVER_*` point at the host side of
  the store link (below), as `projectedPodDoltEnv` does for pods.
- **Binary.** `GC_BIN` is the host's gc (`command -v gc` from the probe, or
  the pushed copy in phase 2).
- **Secrets.** Withheld unless forwarded (see *Security*).

### City mirror

Session-side `gc` and `bd` commands load `city.toml`, `.gc/site.toml` and
the prompt, skill and script snapshots. Each host keeps a mirror at
`<root>/city`:

- Contents are exactly what `gc runtime workdir-export` lists for the city
  dir, so secrets, stores, sockets, logs and other agents' work dirs never
  leave the controller.
- `.gc/site.toml` is rewritten with the host's rig paths.
- The controller syncs it as a tar over ssh stdin (the `execScript` path in
  `ssh.go`) before the first start on a host and whenever the config
  revision changes, and records the revision in `<root>/city/.gc/host-sync.json`.
  A start on a host whose mirror is behind waits (`placement_mirror_stale`).

### Repositories and worktrees

- Each host has its own clone of each rig it serves (`[hosts.X.rigs]`). gc
  checks that the path is a git checkout with the rig's default branch on the
  probe, and never writes to it except through `gc worktree`.
- Worktrees are created where they are today: by the session's own
  `pre_start` (`gc worktree ensure`, `internal/worktree.Ensure`), which runs
  on the host, under the host's rig root. Nothing is copied back. Results
  leave the host the way they leave any worktree: `git push`, bead updates
  and mail, all through the one store.
- The closed-bead worktree reaper runs against each host by `exec` of
  `gc worktree cleanup` on that host (phase 2). Until then a host's worktree
  upkeep is the operator's.

### Store link

The city's store is a loopback Dolt (or bd proxy) on the controller host
(`docs/runbooks/managed-city-endpoints.md`). A remote session reaches it
through a **store link**: one long-lived ssh connection per host, owned by
the controller:

```
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 \
    -R <root>/run/store.sock:<controller store endpoint> <target>
```

- The remote end is a unix socket in a 0700 directory, not a TCP port, so
  other users on the host cannot reach the store. Session env uses
  `BEADS_DOLT_SERVER_SOCKET`.
- The link is also the host's heartbeat (see *Health*).
- A city whose store is already reachable on the network
  (`gc.endpoint_origin = city_canonical`, or a bd HTTP target) sets
  `[hosts.X] store = "direct"` and gets no link; its env is passed through.
- Session-side commands that dial the controller socket
  (`.gc/controller.sock`) get a second forward on the same link,
  `<root>/run/controller.sock`.

The link carries every bead read and write of every remote session to one
Dolt. That is the point (one store, no federation), and also the main load
cost; see *Open questions*.

## Health and failure

Each remote host has a state, kept by the controller and re-evaluated each
tick from the link and a probe:

| State | Meaning | Placement | Existing sessions |
|---|---|---|---|
| `up` | link up, probe passed within `probe_interval` (default 30s) | yes | normal |
| `degraded` | link or probe failing for less than `down_after` (default 2m) | no | verbs return `ErrRuntimeUnavailable`; nothing reaped |
| `down` | failing for `down_after` or more | no | as degraded; `host.down` event |
| `draining` | operator ran `gc host drain` (phase 2) | no | run to completion |
| `skewed` | reachable, version policy failed | no | normal |

The probe is one `exec` per host: `gc version --json`, `bd version`,
`tmux -V`, `command -v` for each provider command placed there, the rig
checkouts, and the mirror revision. Results are cached for the tick.

**Partition.** A host that stops answering is *unknown*, not empty. The
leg's `ListRunning` error makes the router's listing partial
(`MergeBackendListResults`), and `ListingAttestation` is false, so the
reconciler does not mark its sessions dead and does not reap them. Work
release is gated on confirmed non-liveness
(`releaseOrphanedPoolAssignmentsWhenSnapshotsComplete`, called from
`cmd/gc/city_runtime.go`; `releaseConfirmedOrphanSessionWork` in
`session_reconciler.go`), and a leg that answers `ErrRuntimeUnavailable`
cannot confirm it. Work assigned to those sessions stays assigned. v1 ships
a test that a partitioned leg releases no work and closes no session. This
is the safe default: a partitioned session may still be running and pushing.

**Declaring a host lost.** `gc host forget <host>` (explicit, logged,
`host.forgotten` event) marks the host's open sessions failed, which
releases their assigned work through the normal stranded-claim path. An
automatic `forget_after` exists as a config key but is **off by default**.

**Return.** When a forgotten host comes back, its tmux sessions carry
`GC_SESSION_ID` values that name failed or closed rows. They are ownerless
runtimes (`doctor_ownerless_runtimes.go`), found per leg, and the existing
reap rule (dead and unattached only) applies. A live one is reported, not
killed.

**Controller restart.** Remote tmux sessions survive. The store link drops
until the controller is back, so remote sessions see the store as down for
that window, as local sessions do when the controller host's Dolt restarts.
The router re-seeds routes from `gc.host` on the first snapshot.

## Binaries and version skew

A remote session runs the host's gc, bd, tmux and agent CLIs.

- **Version policy** (`[placement] version_policy`): `exact` (default: the
  host's `gc version` build must equal the controller's), `minor`, or
  `any`. A failing host is `skewed`: no new placements, trace reason
  `placement_version_skew`, `gc doctor` names both versions.
- **bd** is reported and checked against the city's bd floor
  (`bdMinVersion`); below the floor is `skewed`.
- **tmux** must be 3.2 or later (the ssh provider passes inert env with
  `-e`).
- **Agent CLIs** are not versioned by gc. A provider whose command is missing
  on a host removes that host from that agent's eligible set
  (`placement_provider_missing`), which replaces hand-kept per-host "this CLI
  is not installed here" lists.
- **Phase 2: push.** `[placement] push_gc = true` copies the controller's own
  gc binary (same GOOS/GOARCH only) to `<root>/bin/gc-<sha256>` over the
  transport and points `GC_BIN` and `PATH` at it. bd and Dolt are not
  pushed: they are coupled to the store and stay operator-installed.

## Security

- **No credentials in gc config.** A host names an ssh destination; keys,
  users, ports and `known_hosts` stay in the operator's `~/.ssh/config`, as
  RUNTIME-SEL-013 requires for `ssh:`. The hosts table adds structure
  (labels, capacity, rigs), not key material.
- **Strict host keys.** Hosts use `StrictHostKeyChecking=yes`, not the
  `accept-new` the anonymous `ssh:` form uses. An unknown host key is a
  doctor failure, fixed by the operator.
- **No forwarding of the operator's agent or display.** `-o
  ForwardAgent=no -o ForwardX11=no` on every ssh call and the link.
- **Secret env is withheld from remote hosts by default.** The session env
  is split with `runtime.SplitEnvByArgvSafety` (`envsecret.go:189`). On a
  remote host, secret values not named in `[hosts.X.env] forward` are
  dropped, and the start records `secret_env_withheld` with the key names
  only. A host gets its credentials from its own operator (its CLI logins,
  or an env file under `<root>` that gc sources but never writes). Named
  forwards still travel the existing staged-file path, never argv.
- **The mirror carries no secrets.** It is the work-dir export set, whose
  denylist excludes `.gc/secrets`, `.gc/controller.token`, credential files
  and every store.
- **The store socket is private** (0700 directory, unix socket).
- **Trust boundary.** A worker host has read and write access to the whole
  bead store and to every repository its clones can push. It is a trusted
  machine of the city, not an isolated sandbox. The docs say so.

## Observability

- **Session bead:** `gc.host` on every placed session; `gc session list
  --json` shows it.
- **`gc host list` / `gc host status <host>`** (new, `cmd/gc/cmd_host.go`):
  state, link, last probe, versions, sessions, reserved / capacity per
  class, mirror revision, last refusal reason.
- **`gc status`:** a host column on session lines and one summary line per
  host (`city_status_snapshot.go`, `renderCityStatusText`). The API
  `StatusView` gains the same fields (phase 2, schema change).
- **Traces:** each deferral records a reconciler trace decision with the host
  (`placement_no_eligible_host`, `placement_sticky_wait`,
  `placement_host_down`, `placement_version_skew`,
  `placement_provider_missing`, `placement_mirror_stale`, plus the
  resource-budgets reasons carrying `host`).
- **Events:** `host.up`, `host.degraded`, `host.down`, `host.skewed`,
  `host.forgotten`, and `session.placed` (host, reason it won). One line per
  transition, not per tick. Typed payloads for these are phase 2, as
  resource-budgets defers `session.resource_deferred`.
- **Doctor:** a `hosts` check (registered in `buildDoctorChecks`,
  `cmd/gc/cmd_doctor.go:246`): reachability, strict host key present, version
  policy, tmux version, rig checkouts, provider commands, mirror freshness,
  link up. `ownerless-runtimes` lists per host.

## Prerequisites

1. **Resource budgets keyed by host.** See *Changes to resource-budgets*.
2. **Input fencing on ssh.** The ssh backend reports `InputFencingNone`
   (RUNTIME-CONTRACT-009), so fenced input to an ssh session is refused. The
   box carrier's atomic guard (`runtime.TmuxGuardedInputArgs`) already serves
   exec boxes; the ssh provider needs the same wiring before remote sessions
   are first-class.
3. **ssh options.** `Endpoint` gains strict host-key and no-forwarding options
   (`sshArgs`, `ssh.go`), defaulting to today's behaviour for `ssh:`.

## Changes to resource-budgets

The resource-budgets model extends to many hosts with these changes; none
alters its single-host behaviour:

1. **Budget per host.** `newResourceBudget(cfg)` becomes
   `newResourceBudget(cfg, host)`. For `local` it reads the top-level
   `[resources]`; for a remote host, `[hosts.X.resources]`. Class
   reservations stay city-wide; class `max_active_sessions` may be overridden
   per host.
2. **`memory = "auto"` reads the local machine only.** A remote host needs an
   explicit size until the probe can read its memory (phase 2).
3. **Ledger per host.** `newResourceLedgerFromSessions` partitions resident
   sessions by `gc.host` (missing means `local`) into one `resourceLedger`
   per host. `charge` takes the host.
4. **Head-of-line per host.** The ledger's single `blocked` flag becomes per
   host, held by the deferred request's home host (see *Placement*).
   Otherwise one large request on a full host stops placement on every host.
5. **Trace payloads** gain `host`.
6. **v2 start boundary.** The start-boundary gate sits in the legacy
   reconciler (`reconcileSessionBeadsTracedWithNamedDemand`, before
   `executePlannedStartsTraced`). v2 is the default reconciler
   (`cmd/gc/reconcile_mode.go:106`), and its starts go through `admit`
   (`cmd/gc/reconcile_admit.go:257`) and `startLaunch`, which do not call the
   gate. Pool demand is covered (v2 reuses `computePoolDesiredStatesAt`), but
   named-session starts and resumes are not budget-checked under v2. The gate
   belongs in `admit` as a cap class; placement reuses that seam.

## Example: a city with four machines

A controller machine and three workers: two 15 GiB machines that take
anything but heavy work in moderation, and one desktop that takes only light
work.

```toml
[resources]                 # the controller machine ("local")
memory = "auto"
reserve = "8GiB"
default_class = "light"

[resources.classes.light]
memory = "1600MiB"
[resources.classes.test]
memory = "8GiB"
[resources.classes.build]
memory = "6GiB"

[placement]
default_hosts = ["local", "host-a", "host-b", "host-c"]
version_policy = "exact"

[hosts.local]
priority = 100              # local first whenever it fits
labels = ["browser"]

[hosts.host-a]
transport = "ssh"
target = "host-a"
root = "/srv/gc/city-1"
labels = ["browser"]
max_active_sessions = 8
[hosts.host-a.resources]
memory = "15GiB"
reserve = "2GiB"
[hosts.host-a.resources.classes.test]
max_active_sessions = 1
[hosts.host-a.rigs]
web = "/srv/src/web"

[hosts.host-b]              # same shape as host-a
transport = "ssh"
target = "host-b"
root = "/srv/gc/city-1"
labels = ["browser"]
max_active_sessions = 8
[hosts.host-b.resources]
memory = "15GiB"
reserve = "2GiB"
[hosts.host-b.resources.classes.test]
max_active_sessions = 1
[hosts.host-b.rigs]
web = "/srv/src/web"

[hosts.host-c]              # light only: no toolchain for builds or suites
transport = "ssh"
target = "host-c"
root = "/srv/gc/city-1"
priority = -10              # last among equals
max_active_sessions = 6
[hosts.host-c.resources]
memory = "30GiB"
reserve = "4GiB"
[hosts.host-c.resources.classes.build]
max_active_sessions = 0
[hosts.host-c.resources.classes.test]
max_active_sessions = 0
[hosts.host-c.rigs]
web = "/srv/src/web"

[[agent]]
name = "coordinator"
[agent.placement]
hosts = ["local"]           # pinned

[[agent]]
name = "browser-walker"
resource_class = "build"
[agent.placement]
require_labels = ["browser"]
```

Which agent CLIs each machine has is not written down: the probe finds out,
and an agent whose CLI is missing on a machine is not placed there.

## What this replaces

In a city that federates machines today:

| Out-of-tree piece | Replaced by |
|---|---|
| One city, store and bead prefix per machine | One city; hosts are runtime legs; one store |
| Dispatcher host choice (capacity, "light only", preference order) | `placeSession` with per-host budgets, class refusals, `priority`, `prefer` |
| Remote RPC to ask a machine for free slots and to launch | The reconciler's own ledger and the placement router's `Start` |
| Copying results back from each machine's store or outbox | Nothing to copy: the session writes the city store directly |
| "Bead not found" across store boundaries | Gone: one store |
| Per-machine helper scripts (file, comment, peek over ssh) | `gc` commands; `gc session peek` routes through the router |
| Per-machine "CLI not installed" lists | Probe of provider commands |
| Per-machine gc/bd upgrades and drift | Version policy, `skewed` state, doctor; phase 2 gc push |
| Per-machine order config and store retention | Gone with the per-machine stores |
| Remote worktree reaping scripts | Phase 2 per-host reaper |

Still out of tree after this proposal: in-session command admission (a
build inside a session waiting for memory), measured-load vetoes, and
credential distribution to hosts.

## Phases

**Phase 0: prerequisites.** Resource budgets keyed by host and gated in v2
`admit`; ssh input fencing; ssh strict-host-key and no-forwarding options.

**Phase 1: v1.**
- `[hosts]`, `[placement]`, `agent.placement` config and validation.
- `placeSession` in desired state and start boundary; `gc.host` on session
  beads; sticky placement.
- `internal/runtime/placement` router with route seeding from `gc.host`.
- Per-host path, store and binary projection; secret withholding.
- City mirror sync from the work-dir export set.
- Store link (ssh `-R` to a unix socket) and `store = "direct"`.
- Host health (`up`, `degraded`, `down`, `skewed`) from link and probe;
  partitions are unknown; `gc host forget`.
- `gc host list|status`, `gc status` host column, trace reasons, untyped
  events, doctor `hosts` check.
- Remote hosts need gc (exact version), bd, tmux 3.2+, rig clones and agent
  CLIs installed by the operator.

**Phase 2.** gc push; per-host worktree reaper; `gc host drain`; remote
`memory = "auto"` and measured mode per host; typed host events and API
`StatusView` fields; optional `forget_after`.

**Phase 3.** A host agent transport (one multiplexed connection, `gc host
serve` on the host) to replace per-verb ssh round-trips; a bd HTTP store
target instead of the Dolt link; a k8s leg in the router.

## Open questions

1. **Store load.** Every remote session's bead traffic crosses the link to
   one Dolt. Federated setups split that load across machines. Is one store
   acceptable at the target session count, or does v1 need a measured
   ceiling first?
2. **Partition policy.** v1 never releases a partitioned host's work by
   itself. Is manual `gc host forget` acceptable, or is an automatic
   `forget_after` needed in v1, with the duplicate-work risk it brings?
3. **Version policy default.** `exact` makes every upgrade a fleet-wide
   event (until phase 2 push). Is `minor` a safer default?
4. **Structured ssh.** RUNTIME-SEL-013 rejected a structured ssh form. This
   proposal adds one for hosts while keeping key material out. Does that
   satisfy the reason for the rule?
5. **Controller socket.** Which session-side commands dial
   `.gc/controller.sock` rather than the store, and is a forwarded socket the
   right answer for them, or should they move to the API?
