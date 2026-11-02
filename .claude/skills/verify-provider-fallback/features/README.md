# Provider fallback verification map

This is the maintained source for verifying provider fallback in gc. Read this index, then use the matching feature file as the recipe. Every recipe runs in a `gcv` scratch city (see `../SKILL.md`).

## Baseline preconditions

- `gcv launch` succeeded and `gcv doctor` prints `DOCTOR OK`.
- The `probe` agent is on `fake-primary` with fallbacks `[fake-fallback, fake-third]` and `fallback_option_defaults.fake-fallback.effort = "high"`. Every fake provider defaults to `--effort low`.
- No health file, unless the recipe writes one (`gcv clear`).
- Never drive a city that `gcv launch` didn't start in this run.

## Driving conventions

- Start every case with `gcv recycle`. A reused session never changes provider, by design, so a stale session hides the case under test.
- Run `gcv clear` before any `gcv hold`. Holds merge into the health file, so an earlier case's holds carry over.
- Assert on the launch record (`provider=`, `argv=`), not only on bead metadata.
- Take a proof (`gcv proof <feature-id>`) after each case's assertion.

## Features

| Feature | File | Proven |
|---|---|---|
| Fallback on a red primary | [fallback-on-red.md](fallback-on-red.md) | 2026-11-02, gc bae017018 (run pf1102032727) |
| Health record freshness (`ttl_s`, `until`, fail-open) | [health-freshness.md](health-freshness.md) | not yet |
| Per-fallback options (`fallback_option_defaults`) | [fallback-options.md](fallback-options.md) | 2026-11-02, gc bae017018 (run pf1102032727, fallback-taken overlay) |
| Headroom balancing (`fallback_strategy = "headroom"`) | [headroom-strategy.md](headroom-strategy.md) | not yet: needs the build with fallback_strategy |

## Proof and skip reporting

- Report each feature ID with its proof path (`~/.cache/gc-verify/<run>/<feature-id>/`).
- If a recipe can't run (for example, the binary lacks `fallback_strategy`), report the command tried and the unmet precondition. Never weaken the assertion to make it pass.
