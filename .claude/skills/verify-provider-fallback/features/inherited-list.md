# Inherited fallback list (`[agent_defaults] fallback_providers`)

`[agent_defaults] fallback_providers` in `city.toml` is a default fallback list. An agent that leaves its own `fallback_providers` unset inherits it. An agent that sets `fallback_providers = []` opts out and never falls back. An agent's own list (or a `[[patches.agent]]` / `[[rigs.overrides]]` list) beats the default and is never merged with it. The core control dispatcher is exempt. `gc config explain` labels an inherited row `# agent_defaults in city.toml`. Only the city's own `[agent_defaults]` applies; a pack's `[agent_defaults]` list is not inherited.

## Sub-features

- `inherits-default`: an agent with no list (`heir`) falls back through the default list when its main provider is red.
- `empty-opts-out`: an agent with `fallback_providers = []` (`optout`) gets no fallback; a red main provider refuses the create.
- `own-list-wins`: an agent with its own list (`probe`) uses it, not the default.
- `dispatcher-exempt`: `core.control-dispatcher` shows no fallback list even with a default set.
- `live-explain`: on the live city (read-only), the mayor (patched to `[]`) shows no list, the control dispatcher shows none, and an agent with no list of its own shows `# agent_defaults in city.toml`.

## How to get to it (user POV)

The operator writes `[agent_defaults] fallback_providers = [...]` in `city.toml` and `fallback_providers = []` on any agent that must never fall back (the live city does this for the mayor through a patch). They see where each agent's list comes from in `gc config explain`, and which provider ran in the launch, `gc bd show <session>` and the `provider.fallback` event.

## Driving it with gcv

`gcv launch` seeds `[agent_defaults] fallback_providers = ["fake-third"]` and two extra agents on `fake-primary`: `heir` (no list) and `optout` (`[]`). The default list differs from probe's own `[fake-fallback, fake-third]`, so each case lands on a different provider. Agent-scoped commands take `GCV_AGENT=<agent>` (default `probe`).

- **inherits-default.** Run `export GCV_AGENT=heir; gcv recycle; gcv clear; gcv hold fake-primary 600 --reason inherit; gcv sling inherit; gcv wait-launch`. Expect `provider=fake-third`, `agent=heir`, argv `--effort low`. `gcv session` shows `override=fake-third`. `gcv events 1` is `provider.fallback` with `agent=heir`, `from=fake-primary`, `to=fake-third`. `gcv explain heir` prints `heir fake-third  # agent_defaults in city.toml`. Then `gcv proof inherits-default`.
- **empty-opts-out.** Control first, so the refusal can't be a dead agent: `export GCV_AGENT=optout; gcv recycle; gcv clear; gcv sling ctl; gcv wait-launch` must show `provider=fake-primary`. Then `gcv recycle; gcv clear; gcv hold fake-primary 600 --reason optout; gcv sling optout`. `gcv wait-launch 60` must fail, `gcv launch-count` must print `0`, and `gcv proof empty-opts-out` must hold `pool "optout" request: pool session create skipped: provider red` in `supervisor-provider-lines.txt` and no `provider.fallback` event for `optout`. `gcv explain optout` prints `optout (none)`.
- **own-list-wins.** Run `export GCV_AGENT=probe; gcv recycle; gcv clear; gcv hold fake-primary; gcv sling own; gcv wait-launch`. Expect `provider=fake-fallback`, argv `--effort high`. If the default replaced the own list it would be `fake-third`; if it were merged in front it would also be `fake-third`. `gcv explain probe` shows the own list with no `agent_defaults` source. Then `gcv proof own-list-wins`.
- **dispatcher-exempt.** `gcv explain core.control-dispatcher` prints `(none)`. `MISSING-AGENT` means the scratch city lost the core pack, not a pass.
- **live-explain.** `gcv live-check` (default `~/city`). It runs only `gc --city <city> config explain`, outside the scratch env, and prints `LIVE-CHECK OK` when the mayor and control dispatcher show `(none)` and at least one agent shows `# agent_defaults in city.toml`. The inheriting row is the positive control: it proves the parser sees fallback rows, so the mayor's `(none)` is not a blind read.

Last proven: run `pf1102062107` (all five cases plus `gcv live-check`), 2026-11-02, gc 2eba3dc4d.

## Gotchas

- `gc config explain` prints probe's own list with source `# pack.toml` (agent.toml files compose into the pack); the inherited row is the only one that says `agent_defaults`.
- Patch and rig-override precedence (`[[patches.agent]]`, `[[rigs.overrides]]`) and the pack-`[agent_defaults]`-not-inherited rule are covered by the unit tests in `internal/config/agent_defaults_fallback_test.go`, not driven here. The live mayor's `[]` comes from a patch, so `live-explain` is the one live proof of a patched opt-out.
- `dispatcher-exempt` is proven at config level only: the scratch dispatcher is suspended and never launches.
- `fallback_option_defaults` keyed by an inherited fallback validates against the inherited list; an opted-out agent with such an entry fails config load. Unit-tested only.
