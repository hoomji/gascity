# Provider fallback model

A TLA+ model of provider fallback, checked with TLC through the `statechart` skill. The charts in this folder are its output for gc city-next f2d8d5dbb.

| Chart | Config | View | Result |
|---|---|---|---|
| `agent.mmd`: which provider new work starts on | `Agent.cfg` | `viewAgent.ts` | all rules hold (120 states) |
| `dispatcher-now.mmd`: the start_command gap | `DispatcherNow.cfg` | `viewDisp.ts` | `DispatcherNeverBlockedByQuota` breaks in 2 steps |
| `dispatcher-fixed.mmd`: with fix/health-gate-start-command | `DispatcherFixed.cfg` | `viewDisp.ts` | all rules hold |

Regenerate with the TLC version the statechart skill pins (tla2tools 1.7.4, Java 11 or later):

```sh
TLA2TOOLS=~/.local/share/tla/tla2tools-1.7.4.jar JAVA=~/.local/share/tla/jre/bin/java \
  <statechart skill>/scripts/statechart ProviderFallback.tla --config Agent.cfg --view viewAgent.ts --out /tmp/pf-chart-agent
```

The model leaves out:
- the `headroom` strategy, which isn't built yet
- the difference between "no record" (fail-open) and "usable"; the two behave the same
- `ttl_s`/`until` timing; a hold is modelled as the reading quota-watch writes
- reuse of an asleep session
- named singleton agents
- more than one session per agent
