# Per-fallback options

`fallback_option_defaults.<provider>` gives a fallback its own option values, such as model or effort. They apply only when that fallback is chosen. The agent's own `option_defaults` and `args` never reach a fallback, and the overlay never reaches the main provider.

## Sub-features

- `overlay-applied`: the chosen fallback launches with its overlay (fake-fallback, `--effort high`).
- `no-entry-uses-preset`: a fallback without an entry launches with its preset defaults (fake-third, `--effort low`).
- `primary-untouched`: the main provider launches with its own defaults (`--effort low`).
- `bad-value-rejected`: an invalid overlay value fails config load.

## How to get to it (user POV)

The operator writes `[agent.fallback_option_defaults.<provider>]` in the city config, or `[fallback_option_defaults.<provider>]` in an `agent.toml`. They see the effect in the session's stored `command` and in the process argv.

## Driving it with gcv

- **overlay-applied.** Run the `fallback-taken` steps from fallback-on-red.md. The launch record `argv=` starts with `--effort high`, and `gcv session` shows `command=…fake-agent --effort high`.
- **no-entry-uses-preset.** Run the `skip-red-fallback` steps. The argv starts with `--effort low`.
- **primary-untouched.** Run the `baseline-primary` steps. The argv starts with `--effort low`.
- **bad-value-rejected.** Edit `/var/tmp/gcv-<id>/city/agents/probe/agent.toml` to `effort = "extreme"`, then run `gcv gc config explain`. It must fail with `fallback_option_defaults.fake-fallback: option_defaults key "effort": value "extreme" is not a valid choice`. Restore `"high"` afterwards.

## Gotchas

- `gc config explain` doesn't print `fallback_option_defaults`. Prove the overlay through the launch argv instead.
- Option `env` from a choice isn't applied for defaults in this build. Assert on `flag_args` (argv), not env.
