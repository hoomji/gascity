# Headroom balancing

With `fallback_strategy = "headroom"`, a new session goes to the healthy candidate with the most `headroom` (from the health file). It stays on the main provider unless another candidate beats it by more than `fallback_margin` (default 15). With the default strategy, `"first"`, nothing changes.

## Sub-features

- `first-unchanged`: without `fallback_strategy`, headroom values are ignored.
- `stay-within-margin`: main provider headroom 50, fallback 60, so the session stays on the main provider.
- `switch-beyond-margin`: main provider headroom 40, fallback 70, so the session uses the fallback, and `provider.fallback` carries `strategy=headroom` and the headroom values.
- `unknown-never-wins`: a candidate without a headroom value never beats one that has a value.
- `red-primary-max-headroom`: main provider held, fake-fallback headroom 20, fake-third headroom 80, so fake-third is used.

## How to get to it (user POV)

The operator sets `fallback_strategy = "headroom"` (and optionally `fallback_margin`) on an agent. `quota-watch` writes `headroom` on each healthy record.

## Driving it with gcv

Precondition: the binary supports `fallback_strategy`. `gcv gc config explain` must not reject it. It's on branch `fix/health-gate-start-command` until it's promoted. Launch with `gcv launch --gc <that build>`.

Add `fallback_strategy = "headroom"` to `/var/tmp/gcv-<id>/city/agents/probe/agent.toml`, then run `gcv gc reload`.

- **stay-within-margin.** Run `gcv recycle; gcv healthy fake-primary --headroom 50; gcv healthy fake-fallback --headroom 60; gcv sling s1; gcv wait-launch`. Expect `provider=fake-primary`.
- **switch-beyond-margin.** The same with headroom 40 and 70. Expect `provider=fake-fallback`, and `gcv events 1` shows `strategy` with both values.
- **unknown-never-wins.** Give fake-primary a red hold, fake-fallback no record, and fake-third `--headroom 10`. Expect `provider=fake-third`.
- **red-primary-max-headroom.** Hold fake-primary, give fake-fallback headroom 20 and fake-third headroom 80. Expect `provider=fake-third`.
- **first-unchanged.** Remove `fallback_strategy`, reload, and rerun switch-beyond-margin. Expect `provider=fake-primary`.

## Gotchas

- Not yet proven. The strategy is still being built (gc-fallback-followup, Task 3). If the binary rejects the key, report the feature as unreachable with the error. Don't drop the strategy line to make it pass.
