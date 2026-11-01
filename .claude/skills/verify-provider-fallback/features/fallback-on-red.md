# Fallback on a red primary

When an agent's main provider is marked unhealthy, a new pool session starts on the first fallback that isn't unhealthy. The choice is recorded on the session and announced with an event. When the main provider recovers, new sessions go back to it. When every candidate is unhealthy, no session starts.

## Sub-features

- `baseline-primary`: no health file, so the session starts on the main provider and no override is recorded.
- `fallback-taken`: main provider held, so the session starts on the first non-red fallback. `gc.provider_override` is set and a `provider.fallback` event carries the reason.
- `skip-red-fallback`: main provider and first fallback both held, so the second fallback is used.
- `return-to-primary`: hold cleared and session recycled, so the next session is back on the main provider with no override.
- `reuse-keeps-provider`: a session started on a fallback keeps it for later work, even after the hold clears.
- `all-red-refused`: every candidate held, so no session starts and the supervisor logs `pool session create skipped: provider red`.

## How to get to it (user POV)

An operator, or the `quota-watch` pack, writes `.gc/cache/provider-health.json`. Work arrives with `gc sling <agent> …`. The operator sees which provider ran in `gc session list`, `gc bd show <session>` and the events log.

## Driving it with gcv

- **baseline-primary.** Run `gcv recycle; gcv clear; gcv sling base; gcv wait-launch`. Expect `provider=fake-primary`, argv `--effort low`. `gcv session` shows `override=None`. Then `gcv proof baseline-primary`.
- **fallback-taken.** Run `gcv recycle; gcv hold fake-primary 600 --reason "verify"; gcv sling fb; gcv wait-launch`. Expect `provider=fake-fallback`. `gcv session` shows `override=fake-fallback`. `gcv events 1` is `provider.fallback` with `from=fake-primary`, `to=fake-fallback` and the reason. Then `gcv proof fallback-taken`.
- **skip-red-fallback.** Run `gcv recycle; gcv hold fake-primary; gcv hold fake-fallback; gcv sling skip; gcv wait-launch`. Expect `provider=fake-third`, argv `--effort low` (fake-third has no per-fallback options).
- **return-to-primary.** Run `gcv recycle; gcv clear; gcv sling back; gcv wait-launch`. Expect `provider=fake-primary` and `override=None`.
- **reuse-keeps-provider.** After `fallback-taken`, without recycling, run `gcv clear; gcv sling again`. Expect no new launch record (`gcv wait-launch 30` fails), and `gcv session` still shows `override=fake-fallback`.
- **all-red-refused.** Run `gcv recycle`, `gcv hold` on all three providers, then `gcv sling none`. `gcv wait-launch 60` must fail. `gcv proof all-red-refused` must contain `supervisor-provider-lines.txt` with `pool "probe" request: pool session create skipped: provider red`.

Last proven: run `pf1101143805` (baseline-primary, fallback-taken, return-to-primary, all-red-refused) and run `pf1101145557` (fallback-taken, full launch-to-cleanup pass), 2026-11-01, gc 1.5.1-next f2d8d5dbb.

## Gotchas

- `gcv recycle` can race a session that's still starting. It retries the close for about 30 s and prints the error if it still fails. Without the recycle, a new bead goes to the existing session and keeps its provider. That's correct behaviour, but it isn't the case under test.
- The gate also covers agents that never launch their provider. In the scratch city `bd.dog` runs on the default provider and is refused when `fake-primary` is held. In a real city, `core.control-dispatcher` runs a `start_command` and is refused the same way. Branch `fix/health-gate-start-command` addresses that, so re-prove this feature on that build.
- A held provider with `until` in the past and an expired `ttl_s` counts as healthy (fail-open). Hold for at least 600 s while proving.
