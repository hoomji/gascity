package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"testing"
	"time"

	"github.com/gastownhall/gascity/internal/beadmeta"
	"github.com/gastownhall/gascity/internal/beads"
	"github.com/gastownhall/gascity/internal/config"
	"github.com/gastownhall/gascity/internal/events"
	"github.com/gastownhall/gascity/internal/fsys"
	"github.com/gastownhall/gascity/internal/session"
)

func healthSnap(entries map[string]bool) *providerHealthSnapshot {
	return &providerHealthSnapshot{present: true, entries: entries}
}

// Kills: a fallback taken while the primary is not red, a red or
// skipped-over candidate chosen, the primary re-chosen from the list, an
// all-red create admitted, and a start_command agent switched (its command
// does not depend on the provider).
func TestSelectPoolCreateProvider(t *testing.T) {
	ws := &config.Workspace{Provider: "claude"}
	for _, tc := range []struct {
		name      string
		agent     config.Agent
		health    map[string]bool
		reasons   map[string]string
		wantTo    string
		wantErr   bool
		wantReasn string
	}{
		{name: "primary green", agent: config.Agent{Name: "w", FallbackProviders: []string{"codex"}}, health: map[string]bool{"claude": true, "codex": true}},
		{name: "primary unknown fails open", agent: config.Agent{Name: "w", FallbackProviders: []string{"codex"}}, health: map[string]bool{"codex": true}},
		{name: "primary red takes first non-red", agent: config.Agent{Name: "w", FallbackProviders: []string{"codex", "gemini"}}, health: map[string]bool{"claude": false, "codex": true, "gemini": true}, reasons: map[string]string{"claude": "quota"}, wantTo: "codex", wantReasn: "quota"},
		{name: "red first fallback skipped, unknown second taken", agent: config.Agent{Name: "w", FallbackProviders: []string{"codex", "gemini"}}, health: map[string]bool{"claude": false, "codex": false}, wantTo: "gemini"},
		{name: "primary listed as fallback is skipped", agent: config.Agent{Name: "w", FallbackProviders: []string{"claude", "codex"}}, health: map[string]bool{"claude": false}, wantTo: "codex"},
		{name: "agent provider wins over workspace", agent: config.Agent{Name: "w", Provider: "codex", FallbackProviders: []string{"claude"}}, health: map[string]bool{"codex": false, "claude": true}, wantTo: "claude"},
		{name: "all red refuses", agent: config.Agent{Name: "w", FallbackProviders: []string{"codex", "gemini"}}, health: map[string]bool{"claude": false, "codex": false, "gemini": false}, wantErr: true},
		{name: "no fallback list refuses as before", agent: config.Agent{Name: "w"}, health: map[string]bool{"claude": false}, wantErr: true},
		{name: "no fallback list green as before", agent: config.Agent{Name: "w"}, health: map[string]bool{"claude": true}},
		{name: "start_command agent never switches", agent: config.Agent{Name: "w", StartCommand: "run.sh", FallbackProviders: []string{"codex"}}, health: map[string]bool{"claude": false}, wantErr: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			snap := healthSnap(tc.health)
			snap.reasons = tc.reasons
			choice, err := selectPoolCreateProvider(snap, &tc.agent, ws)
			if tc.wantErr {
				if !errors.Is(err, errPoolSessionCreateProviderRed) || choice != nil {
					t.Fatalf("got (%+v, %v), want errPoolSessionCreateProviderRed", choice, err)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error %v", err)
			}
			if tc.wantTo == "" {
				if choice != nil {
					t.Fatalf("choice = %+v, want none (primary)", choice)
				}
				return
			}
			if choice == nil || choice.To != tc.wantTo || choice.Reason != tc.wantReasn {
				t.Fatalf("choice = %+v, want to=%s reason=%q", choice, tc.wantTo, tc.wantReasn)
			}
		})
	}
}

// Kills: the planner refusing a red primary that has a usable fallback, a
// fallback create without gc.provider_override, a primary create with one,
// and a fallback create gated on the primary's endpoint (v2 allocator).
func TestAllocator_PoolPlanTakesFallbackProvider(t *testing.T) {
	for _, tc := range []struct {
		name      string
		fallbacks []string
		health    map[string]bool
		wantSlots string
		wantOver  string
	}{
		{name: "primary green", fallbacks: []string{"codex"}, health: map[string]bool{"claude": true}, wantSlots: "[1]"},
		{name: "primary red, fallback taken", fallbacks: []string{"codex"}, health: map[string]bool{"claude": false}, wantSlots: "[1]", wantOver: "codex"},
		{name: "all red", fallbacks: []string{"codex"}, health: map[string]bool{"claude": false, "codex": false}, wantSlots: "[]"},
		{name: "no fallbacks, red", health: map[string]bool{"claude": false}, wantSlots: "[]"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := allocPoolAgent("worker", 3)
			agent.FallbackProviders = tc.fallbacks
			cfg := &config.City{Agents: []config.Agent{agent}, Workspace: config.Workspace{Provider: "claude"}}
			f := newAllocFixture(t, cfg).demand("worker", "w-1")
			f.in.ProviderHealth = healthSnap(tc.health)
			f.in.Endpoints = map[endpointKey]endpointView{"provider:claude": {Gate: gateClosed}, "provider:codex": {Gate: gateClosed}}
			d := f.decide()
			if got := fmt.Sprint(planSlots(d, "worker")); got != tc.wantSlots {
				t.Fatalf("plans %s trace %v, want %s", got, d.Trace, tc.wantSlots)
			}
			if tc.wantSlots == "[]" {
				if !traceHas(d, gateProviderRed) {
					t.Fatalf("trace %v, want %s", d.Trace, gateProviderRed)
				}
				return
			}
			p := d.Plans[0]
			got, has := p.Plan.metadata[beadmeta.ProviderOverrideMetadataKey]
			if tc.wantOver == "" {
				if has || p.Plan.providerFallback != nil {
					t.Fatalf("primary create carries override %q / %+v", got, p.Plan.providerFallback)
				}
				if p.Endpoint != "provider:claude" {
					t.Fatalf("endpoint = %q, want provider:claude", p.Endpoint)
				}
				return
			}
			if got != tc.wantOver || p.Plan.providerFallback == nil || p.Plan.providerFallback.From != "claude" {
				t.Fatalf("override = %q fallback %+v, want %s from claude", got, p.Plan.providerFallback, tc.wantOver)
			}
			if p.Endpoint != endpointKey("provider:"+tc.wantOver) {
				t.Fatalf("endpoint = %q, want provider:%s", p.Endpoint, tc.wantOver)
			}
			if p.Plan.metadata[beadmeta.TriggerBeadIDMetadataKey] != "w-1" {
				t.Fatalf("metadata %v lost the trigger", p.Plan.metadata)
			}
		})
	}
}

// Kills: the legacy create path dropping the override from the bead, or
// not emitting provider.fallback (or emitting it for a primary create).
func TestSelectOrCreatePoolSessionBead_FallbackStampsBeadAndEmitsEvent(t *testing.T) {
	for _, tc := range []struct {
		name     string
		health   map[string]bool
		wantOver string
	}{
		{name: "primary green", health: map[string]bool{"claude": true}},
		{name: "primary red", health: map[string]bool{"claude": false}, wantOver: "codex"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			rec := &capturingRecorder{}
			prev := providerFallbackRecorder
			providerFallbackRecorder = func(string) events.Recorder { return rec }
			t.Cleanup(func() { providerFallbackRecorder = prev })

			store := beads.NewMemStore()
			cfgAgent := config.Agent{Name: "worker", MinActiveSessions: intPtr(0), MaxActiveSessions: intPtr(5), FallbackProviders: []string{"codex"}}
			snap := healthSnap(tc.health)
			snap.reasons = map[string]string{"claude": "weekly quota"}
			bp := &agentBuildParams{
				cityPath:               t.TempDir(),
				beadStore:              store,
				sessionBeads:           &sessionBeadSnapshot{},
				agents:                 []config.Agent{cfgAgent},
				workspace:              &config.Workspace{Provider: "claude"},
				providerHealthSnapshot: snap,
			}
			info, _, err := selectOrCreatePoolSessionBead(bp, &cfgAgent, "worker", nil, map[string]bool{}, map[int]bool{})
			if err != nil {
				t.Fatalf("selectOrCreatePoolSessionBead: %v", err)
			}
			b, err := store.Get(info.ID)
			if err != nil {
				t.Fatal(err)
			}
			got := b.Metadata[beadmeta.ProviderOverrideMetadataKey]
			if got != tc.wantOver {
				t.Fatalf("bead %s = %q, want %q", beadmeta.ProviderOverrideMetadataKey, got, tc.wantOver)
			}
			if tc.wantOver == "" {
				if len(rec.events) != 0 {
					t.Fatalf("events %+v, want none for a primary create", rec.events)
				}
				return
			}
			if len(rec.events) != 1 || rec.events[0].Type != events.ProviderFallback || rec.events[0].Subject != info.ID {
				t.Fatalf("events %+v, want one provider.fallback for %s", rec.events, info.ID)
			}
			var payload map[string]string
			if err := json.Unmarshal(rec.events[0].Payload, &payload); err != nil {
				t.Fatal(err)
			}
			want := map[string]string{"agent": "worker", "session": info.ID, "from": "claude", "to": "codex", "reason": "weekly quota"}
			for k, v := range want {
				if payload[k] != v {
					t.Fatalf("payload %v, want %s=%q", payload, k, v)
				}
			}
		})
	}
}

// fallbackResolveParams is a build over two standalone providers whose
// command and env differ: alpha (the primary) and beta.
func fallbackResolveParams(t *testing.T) *agentBuildParams {
	t.Helper()
	cityPath := t.TempDir()
	writeTemplateResolveCityConfig(t, cityPath, "file")
	return &agentBuildParams{
		cityName:  "city",
		cityPath:  cityPath,
		workspace: &config.Workspace{Provider: "alpha"},
		providers: map[string]config.ProviderSpec{
			"alpha": {Command: "alpha-cli", PromptMode: "none", Env: map[string]string{"ALPHA_ONLY": "1"}},
			"beta":  {Command: "beta-cli", PromptMode: "none", Env: map[string]string{"BETA_ONLY": "1"}},
		},
		lookPath:   func(string) (string, error) { return "/bin/echo", nil },
		fs:         fsys.OSFS{},
		beaconTime: time.Unix(0, 0),
		beadNames:  make(map[string]string),
		stderr:     io.Discard,
	}
}

func fallbackSessionInfo(override string) session.Info {
	meta := map[string]string{"template": "worker", "agent_name": "worker", "session_name": "s-worker"}
	if override != "" {
		meta[beadmeta.ProviderOverrideMetadataKey] = override
	}
	return seedSessionInfo(beads.Bead{ID: "gc-1", Metadata: meta})
}

// Kills: a session created on a fallback starting (or resuming, which
// resolves the same way) with its agent's primary command and env, the
// primary's agent args leaking into the fallback's command, and an
// override naming a provider no longer in the catalog failing the session.
func TestResolveTemplateForSessionBeadInfo_ProviderOverride(t *testing.T) {
	agent := &config.Agent{Name: "worker", Args: []string{"--alpha-flag"}, Env: map[string]string{"AGENT_ENV": "kept"}}
	for _, tc := range []struct {
		name      string
		override  string
		wantName  string
		wantCmd   string
		wantEnv   string
		wantNoEnv string
		wantArg   bool
	}{
		{name: "no override uses the primary", wantName: "alpha", wantCmd: "alpha-cli", wantEnv: "ALPHA_ONLY", wantNoEnv: "BETA_ONLY", wantArg: true},
		{name: "override launches the fallback", override: "beta", wantName: "beta", wantCmd: "beta-cli", wantEnv: "BETA_ONLY", wantNoEnv: "ALPHA_ONLY"},
		{name: "override out of the catalog falls back to the primary", override: "gone", wantName: "alpha", wantCmd: "alpha-cli", wantEnv: "ALPHA_ONLY", wantNoEnv: "BETA_ONLY", wantArg: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			tp, err := resolveTemplateForSessionBeadInfo(fallbackResolveParams(t), agent, "worker", nil, fallbackSessionInfo(tc.override))
			if err != nil {
				t.Fatalf("resolveTemplateForSessionBeadInfo: %v", err)
			}
			if tp.ResolvedProvider == nil || tp.ResolvedProvider.Name != tc.wantName {
				t.Fatalf("ResolvedProvider = %+v, want %s", tp.ResolvedProvider, tc.wantName)
			}
			if !strings.HasPrefix(tp.Command, tc.wantCmd) {
				t.Fatalf("Command = %q, want %s", tp.Command, tc.wantCmd)
			}
			if tp.Env[tc.wantEnv] != "1" || tp.Env[tc.wantNoEnv] != "" {
				t.Fatalf("Env = %v, want %s set and %s unset", tp.Env, tc.wantEnv, tc.wantNoEnv)
			}
			if tp.Env["AGENT_ENV"] != "kept" {
				t.Fatalf("Env = %v, want the agent's env kept", tp.Env)
			}
			if got := strings.Contains(tp.Command, "--alpha-flag"); got != tc.wantArg {
				t.Fatalf("Command = %q, agent arg present %v, want %v", tp.Command, got, tc.wantArg)
			}
		})
	}
	if agent.Provider != "" || len(agent.Args) != 1 {
		t.Fatalf("resolution mutated the configured agent: %+v", agent)
	}
}

// Kills: the v2 start gate keying off the agent's primary instead of the
// session's own provider: a fallback session must start while its primary
// is red and hold while its own provider is red. The row's template is
// resolved by the real seam, not a hand-built TemplateParams.
func TestDecideRowStartGate_KeysOffSessionProvider(t *testing.T) {
	woke := gatherNow.Add(-time.Hour).Format(time.RFC3339)
	agent := &config.Agent{Name: "worker"}
	for _, tc := range []struct {
		name   string
		health map[string]bool
		want   string
	}{
		{name: "primary red, own provider green starts", health: map[string]bool{"alpha": false, "beta": true}, want: decideStart},
		{name: "own provider red holds", health: map[string]bool{"alpha": true, "beta": false}, want: decideStartProviderRed},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c := newHealCase(t, livenessGone, desireWake, "state", "asleep", "last_woke_at", woke, beadmeta.ProviderOverrideMetadataKey, "beta")
			c.a.Snapshot.Entries[c.k].Endpoint = "provider:beta"
			info := c.w.Census.Rows[c.k].Info
			tp, err := resolveTemplateForSessionBeadInfo(fallbackResolveParams(t), agent, "worker", nil, info)
			if err != nil {
				t.Fatalf("resolveTemplateForSessionBeadInfo: %v", err)
			}
			c.w.ProviderHealth = healthSnap(tc.health)
			c.w.Templates = &templateMemo{entries: map[templateMemoKey]templateResolution{templateMemoKeyOf(info): {TP: tp}}}
			if it := c.decide(); it.Reason != tc.want {
				t.Fatalf("decideRow reason = %q (kind %q), want %q", it.Reason, it.Kind, tc.want)
			}
		})
	}
}

// Kills: a fallback session counted and gated on its agent's primary
// endpoint.
func TestEndpointKeyForAgent_ProviderOverride(t *testing.T) {
	cfg := &config.City{Workspace: config.Workspace{Provider: "claude"}}
	agent := &config.Agent{Name: "worker"}
	if got := endpointKeyForAgent(cfg, agent, session.Info{}); got != "provider:claude" {
		t.Fatalf("primary endpoint = %q", got)
	}
	if got := endpointKeyForAgent(cfg, agent, session.Info{ProviderOverride: "codex"}); got != "provider:codex" {
		t.Fatalf("override endpoint = %q, want provider:codex", got)
	}
	up := &config.Agent{Name: "worker", Upstream: "bedrock"}
	if got := endpointKeyForAgent(cfg, up, session.Info{ProviderOverride: "codex"}); got != "upstream:bedrock" {
		t.Fatalf("upstream endpoint = %q, want the upstream to win", got)
	}
}
