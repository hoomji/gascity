---
name: verify-provider-fallback
description: Drive and prove gc provider fallback (fallback_providers, [agent_defaults] fallback_providers inheritance and [] opt-out, fallback_option_defaults, fallback_strategy/headroom, provider-health.json ttl_s/until/reason, gc.provider_override, the provider.fallback event) in an isolated scratch Gas City with fake providers. Use when changing provider_fallback.go, provider_health_gate.go, the pool create path, provider resolution, or before promoting a gc build that touches them; also when a live city reports a provider-red refusal and you need a clean repro.
---

# Verify provider fallback

The surface is the `gc` CLI plus its supervisor, as an operator sees them. The proof runs in a **scratch city on its own supervisor**, so no live city is touched and no provider quota is spent. Its providers are fake: `fake-agent` writes down the provider name, session and argv it was launched with, then sleeps. Every step goes through `scripts/gcv`.

```sh
GCV=.claude/skills/verify-provider-fallback/scripts/gcv   # from the repo root
```

## Launch

```sh
$GCV launch                  # uses `gc` on PATH
$GCV launch --gc ./bin/gc    # or the binary you just built: make build (or go build -o bin/gc ./cmd/gc)
```

What it does:
1. Makes `/var/tmp/gcv-<RUN_ID>/`. The path is short on purpose, because unix sockets have a length limit.
2. Seeds `city.toml` with `--file`, so `gc init` never asks which coding agent to use. There are three fake providers, `fake-primary`, `fake-fallback` and `fake-third`. Each has an `effort` option (low/medium/high) that becomes `--effort <v>`.
3. Drops the always-on mayor.
4. Adds the agent `probe`: `provider = fake-primary`, `fallback_providers = [fake-fallback, fake-third]`, and `fallback_option_defaults.fake-fallback.effort = "high"`.
5. Sets `[agent_defaults] fallback_providers = ["fake-third"]` in `city.toml` and adds two agents on `fake-primary`: `heir` (no list, so it inherits the default) and `optout` (`fallback_providers = []`).
6. Starts a private supervisor on a free port, with `GC_HOME` and `XDG_RUNTIME_DIR` set to the run dir. `HOME` stays real, because gc's platform supervisor refuses a `HOME` override.
7. Runs `gc start` and prints `RUN_ID=… scratch=… evidence=… port=…`.

**Ready** means `gcv doctor` prints `DOCTOR OK`. `launch` runs it last. A cold `gc init` (bd and Dolt bootstrap) takes 2–5 minutes, so give `launch` a timeout of at least 600 s.

The run id is kept in `~/.cache/gc-verify/current`. Set `GCV_RUN=<id>` to drive a different run.

Preconditions:
- `dolt config --global --get user.name` must be set. `launch` stops and says so if it isn't.
- `bd` must be on PATH.
- Never set `HOME` yourself.

## Doctor

```sh
$GCV doctor
```

It runs read-only checks:
- this run's supervisor PID is alive
- `scratch-<id>` answers `gc status`
- `probe` shows `fallback_providers` in `gc config explain`, which proves the binary has the feature; an old binary fails here
- the supervisor is **not** the live `gascity-supervisor.service` PID

Run it first whenever something looks off. If it fails, don't drive this run.

## Drive

| Command | Effect |
|---|---|
| `$GCV hold PROVIDER [SECS] [--headroom N] [--reason TEXT]` | Writes an unhealthy record to the scratch city's `.gc/cache/provider-health.json`: `ttl_s` = SECS (at least 60), `until` = now + SECS |
| `$GCV healthy PROVIDER [--headroom N]` | Writes a healthy record |
| `$GCV clear` | Removes the health file. No file means fail-open: everything counts as healthy |
| `$GCV sling TITLE` | Routes one plain bead to the agent (`gc sling <agent> --stdin --no-formula --no-convoy`) |
| `$GCV recycle` | Closes the agent's sessions and clears all launch records. Run it before each case, because a reused session never changes provider |
| `$GCV wait-launch [SECS]` | Waits for the agent's launch record and prints it (`provider=`, `session=`, `agent=`, `argv=--effort …`) |
| `$GCV launch-count` | Counts the agent's launch records. Use it for every "no new launch" assertion |
| `$GCV session` | Shows each of the agent's sessions: state, `provider`, `gc.provider_override` and stored `command` |
| `$GCV explain [AGENT…]` | Prints each agent's `fallback_providers` row with its source (default: probe, heir, optout, core.control-dispatcher); `(none)` means no list |
| `$GCV live-check [CITY]` | **Read-only** on the live city (default `~/city`): runs only `gc config explain` and checks the mayor and control dispatcher show no list and an agent shows `# agent_defaults in city.toml`. Needs no run |
| `$GCV events [N]` | Shows the scratch city's `provider.*` events |
| `$GCV gc ARGS…` | Runs any gc command against the scratch city with the isolated env |

Agent-scoped commands (`sling`, `recycle`, `wait-launch`, `launch-count`, `session`) act on `GCV_AGENT`, default `probe`; set `GCV_AGENT=heir` or `optout` for the inherited-list cases.

Each case is: `recycle` → set the health state → `sling` → `wait-launch` (or expect none) → `session` / `events` → `proof NAME`. The recipes are in [features/](features/README.md).

## Evidence

```sh
$GCV proof NAME     # -> ~/.cache/gc-verify/<RUN_ID>/NAME/
```

It copies these files:
- the launch records (what the provider process was actually started with)
- `provider-health.json` (the input)
- `session.txt` (bead metadata, including `gc.provider_override` and `command`)
- `events.jsonl` (`provider.fallback`)
- `supervisor-provider-lines.txt` (the `provider red` refusals)
- `identity.txt` (time, gc version, run id)

Proof standards:
- Drive through `gc sling`, which is the real create path. Don't write session beads or call internal helpers.
- Prove both the action and the result. A launch record that names the provider and argv beats a bead field alone. A refusal is shown as no launch plus the supervisor's `provider red` line.
- The health file is the production boundary. Writing it by hand stands in for the `quota-watch` pack, which writes the same file. That's the only stand-in.

## Cleanup

Record the verdict before cleanup. `launch` already wrote the binary's commit and sha256 to `build.txt` in the evidence dir; `verdict` appends them with the result to `~/.cache/gc-verify/verified.tsv`, which `gc-promote` reads to warn when a build was never verified.

```sh
$GCV verdict pass "all features proven"   # or: fail "<feature>: <what broke>"
$GCV cleanup
```

It removes only what this run started:
- `gc stop` on the scratch city, which also stops its bd-owned Dolt
- `tmux -L scratch-<id> kill-server`
- the recorded supervisor PID
- any process whose argv names `/var/tmp/gcv-<id>/`, never this shell or its parents
- then the scratch dir

**Evidence stays** in `~/.cache/gc-verify/<RUN_ID>/`, along with `init.log`, `start.log` and `supervisor.log`. Never kill by process name. Never run `gc stop` without `--city`, and never inside a dir named `city`: a scratch city named `city` shares `/tmp/tmux-<uid>/city` with a live city, and that killed live sessions once (2026-11-01).

After a failed iteration, run `cleanup` anyway, so ports and Dolt servers aren't left behind.

## Helpers

`scripts/gcv` is the only helper (bash, executable). `gcv` with no arguments prints its usage.
