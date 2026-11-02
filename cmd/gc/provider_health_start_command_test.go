package main

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/gastownhall/gascity/internal/beads"
	"github.com/gastownhall/gascity/internal/config"
	sessionpkg "github.com/gastownhall/gascity/internal/session"
)

// An agent that runs a start_command launches that command, not a provider
// (config.ResolveProvider's escape hatch). Its provider field may still be
// set (the built-in control-dispatcher shape), but the provider's health
// never gates it: not its fresh pool creates, not its named creates, not its
// respawn, start or progress-stall recycle. These tests pin each gate with a
// red entry for the agent's provider field, and a control that launches the
// provider and is still gated.

// startCommandGateAgent is a "claude-haiku" agent, launching its provider or
// (startCommand) a command of its own.
func startCommandGateAgent(name string, startCommand bool) config.Agent {
	a := config.Agent{Name: name, Provider: "claude-haiku"}
	if startCommand {
		a.StartCommand = "run-dispatcher --serve"
	}
	return a
}

var haikuRed = map[string]bool{"claude-haiku": false}

// Kills: a v2 fresh pool create of a start_command agent refused because
// its provider field is red, and a provider-launching agent's create let
// through under the same red entry.
func TestAllocator_PoolCreateOfStartCommandAgentIgnoresProviderHealth(t *testing.T) {
	for _, tc := range []struct {
		name         string
		startCommand bool
		wantSlots    string
	}{
		{name: "start_command agent creates while its provider is red", startCommand: true, wantSlots: "[1]"},
		{name: "provider agent is still refused", wantSlots: "[]"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := startCommandGateAgent("worker", tc.startCommand)
			agent.MaxActiveSessions = intPtr(3)
			cfg := &config.City{Agents: []config.Agent{agent}}
			f := newAllocFixture(t, cfg).demand("worker", "w-1")
			f.in.ProviderHealth = healthSnap(haikuRed)
			f.in.Endpoints = map[endpointKey]endpointView{"provider:claude-haiku": {Gate: gateClosed}}
			d := f.decide()
			if got := fmt.Sprint(planSlots(d, "worker")); got != tc.wantSlots {
				t.Fatalf("plans %s trace %v, want %s", got, d.Trace, tc.wantSlots)
			}
			if red := traceHas(d, gateProviderRed); red != (tc.wantSlots == "[]") {
				t.Fatalf("trace %v: provider-red traced = %v", d.Trace, red)
			}
		})
	}
}

// Kills: a named session of a start_command agent held by namedGate because
// its provider field is red (the control-dispatcher could not be created or
// re-created), and a provider-launching named session let through.
func TestAllocator_NamedCreateOfStartCommandAgentIgnoresProviderHealth(t *testing.T) {
	for _, tc := range []struct {
		name         string
		startCommand bool
		wantPlan     bool
	}{
		{name: "start_command named session is created while its provider is red", startCommand: true, wantPlan: true},
		{name: "provider named session is still held", wantPlan: false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			cfg := chatCity("always")
			cfg.Agents[0] = startCommandGateAgent("chat", tc.startCommand)
			f := newAllocFixture(t, cfg).sessions()
			f.in.ProviderHealth = healthSnap(haikuRed)
			f.in.Endpoints = map[endpointKey]endpointView{"provider:claude-haiku": {Gate: gateClosed}}
			d := f.decide()
			if got := hasNamedPlan(d); got != tc.wantPlan {
				t.Fatalf("named plan = %v (plans %+v trace %v), want %v", got, d.Plans, d.Trace, tc.wantPlan)
			}
			if red := traceHas(d, gateProviderRed); red == tc.wantPlan {
				t.Fatalf("trace %v: provider-red traced = %v", d.Trace, red)
			}
		})
	}
}

// Kills: the legacy fresh pool create path (selectOrCreatePoolSessionBead)
// refusing a start_command agent whose provider field is red.
func TestSelectOrCreatePoolSessionBead_StartCommandAgentIgnoresProviderHealth(t *testing.T) {
	for _, tc := range []struct {
		name         string
		startCommand bool
		wantErr      bool
	}{
		{name: "start_command agent creates", startCommand: true},
		{name: "provider agent is refused", wantErr: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			store := beads.NewMemStore()
			cfgAgent := startCommandGateAgent("worker", tc.startCommand)
			cfgAgent.MinActiveSessions = intPtr(0)
			cfgAgent.MaxActiveSessions = intPtr(5)
			bp := &agentBuildParams{
				cityPath:               t.TempDir(),
				beadStore:              store,
				sessionBeads:           &sessionBeadSnapshot{},
				agents:                 []config.Agent{cfgAgent},
				workspace:              &config.Workspace{},
				providerHealthSnapshot: healthSnap(haikuRed),
			}
			info, _, err := selectOrCreatePoolSessionBead(bp, &cfgAgent, "worker", nil, map[string]bool{}, map[int]bool{})
			if tc.wantErr {
				if err == nil || !strings.Contains(err.Error(), "provider red") {
					t.Fatalf("err = %v, want the provider-red refusal", err)
				}
				return
			}
			if err != nil || info.ID == "" {
				t.Fatalf("selectOrCreatePoolSessionBead = (%+v, %v), want a created bead", info, err)
			}
		})
	}
}

// startCommandGateParams is fallbackResolveParams with a claude-haiku
// provider in the catalog.
func startCommandGateParams(t *testing.T) *agentBuildParams {
	t.Helper()
	bp := fallbackResolveParams(t)
	bp.providers["claude-haiku"] = config.ProviderSpec{Command: "haiku-cli", PromptMode: "none"}
	return bp
}

// Kills: the v2 start gate (decideRow) holding a start_command session's
// start or respawn because its agent's provider field is red. The row's
// template is resolved by the real seam, so the test also fails if the
// resolution starts naming the provider for a start_command agent.
func TestDecideRowStartGate_StartCommandSessionIgnoresProviderHealth(t *testing.T) {
	woke := gatherNow.Add(-time.Hour).Format(time.RFC3339)
	for _, tc := range []struct {
		name         string
		startCommand bool
		want         string
	}{
		{name: "start_command session starts while its provider is red", startCommand: true, want: decideStart},
		{name: "provider session is held", want: decideStartProviderRed},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := startCommandGateAgent("worker", tc.startCommand)
			c := newHealCase(t, livenessGone, desireWake, "state", "asleep", "last_woke_at", woke)
			info := c.w.Census.Rows[c.k].Info
			tp, err := resolveTemplateForSessionBeadInfo(startCommandGateParams(t), &agent, "worker", nil, info)
			if err != nil {
				t.Fatalf("resolveTemplateForSessionBeadInfo: %v", err)
			}
			c.w.ProviderHealth = healthSnap(haikuRed)
			c.w.Templates = &templateMemo{entries: map[templateMemoKey]templateResolution{templateMemoKeyOf(info): {TP: tp}}}
			if it := c.decide(); it.Reason != tc.want {
				t.Fatalf("decideRow reason = %q (kind %q), want %q", it.Reason, it.Kind, tc.want)
			}
		})
	}
}

// resolvedForGateAgent is the TemplateParams' ResolvedProvider the real
// resolution gives agent over startCommandGateParams' catalog.
func resolvedForGateAgent(t *testing.T, agent config.Agent) *config.ResolvedProvider {
	t.Helper()
	bp := startCommandGateParams(t)
	resolved, err := config.ResolveProvider(&agent, bp.workspace, bp.providers, bp.lookPath)
	if err != nil {
		t.Fatalf("ResolveProvider: %v", err)
	}
	return resolved
}

// Kills: the legacy respawn gate holding an asleep start_command session
// because its agent's provider field is red, or treating it as a provider
// with an absent registry entry (a per-tick "registry unavailable" line and
// a gate episode for a provider it never launches); and the gate no longer
// holding a provider-launching session.
func TestReconcileSessionBeads_RespawnGateIgnoresStartCommandSession(t *testing.T) {
	for _, tc := range []struct {
		name         string
		startCommand bool
		wantRunning  bool
	}{
		{name: "start_command session respawns while its provider is red", startCommand: true, wantRunning: true},
		{name: "provider session is held", wantRunning: false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			env := newReconcilerTestEnv(t)
			agent := startCommandGateAgent("worker", tc.startCommand)
			env.cfg = &config.City{Agents: []config.Agent{agent}}
			env.addDesired("worker", "worker", false)
			tp := env.desiredState["worker"]
			tp.ResolvedProvider = resolvedForGateAgent(t, agent)
			env.desiredState["worker"] = tp
			session := env.createSessionBead("worker", "worker")
			env.markSessionCreating(&session)
			// The registry is present, so a gate keyed on the start_command
			// session's empty provider name reports it as unavailable.
			writeHealthCache(t, env.city, "claude-haiku", "unhealthy", nowSecs())
			gate := newProviderHealthGate()

			cfgNames := configuredSessionNames(env.cfg, "", env.store)
			snap := newSessionBeadSnapshotFromReconcileRows(sessionpkg.ReconcileRowsFromBeads([]beads.Bead{session}))
			reconcileSessionBeadsAtPathWithNamedDemand(
				context.Background(), env.city, snap.OpenForReconcile(), snap, env.desiredState, cfgNames, env.cfg, env.sp, env.store,
				testSeatWork(env.city, env.cfg, env.store, nil), nil, nil, nil, nil, env.dt, gate,
				map[string]int{"worker": 1}, nil, nil, false, nil, "", nil, env.clk, env.rec, 0, 0, &env.stdout, &env.stderr,
				env.startOptions...,
			)

			if got := env.sp.IsRunning("worker"); got != tc.wantRunning {
				t.Fatalf("running = %v, want %v; stderr=%s", got, tc.wantRunning, env.stderr.String())
			}
			gate.mu.Lock()
			episodes := len(gate.episodes)
			gate.mu.Unlock()
			if tc.startCommand {
				if strings.Contains(env.stderr.String(), "provider-health registry unavailable") || episodes != 0 {
					t.Fatalf("start_command session was gated (episodes %d); stderr=%s", episodes, env.stderr.String())
				}
				return
			}
			if episodes != 1 {
				t.Fatalf("provider session: gate episodes = %d, want 1", episodes)
			}
		})
	}
}

// Kills: the progress-stall recycler treating a start_command session as
// exempt because its agent's provider field is red; it recycles a stalled
// claim holder exactly as a session with no health entry. The control is
// TestReconcileSessionBeads_ClaimHolderStallDoesNotRestartIntoRedProvider.
func TestReconcileSessionBeads_ClaimHolderStallRecyclesStartCommandSessionWhileProviderRed(t *testing.T) {
	env, session, sessionName := newProgressStallTestEnv(t)
	env.cfg.Session.ProgressStallTimeout = ""
	env.cfg.Session.ClaimHolderStallTimeout = "20m"
	agent := env.cfg.Agents[0]
	agent.Provider = "claude-haiku"
	env.cfg.Agents[0] = agent
	tp := env.desiredState[sessionName]
	tp.ResolvedProvider = resolvedForGateAgent(t, agent)
	env.desiredState[sessionName] = tp

	work, err := env.store.Create(beads.Bead{Title: "claimed work", Type: "task", Assignee: sessionName})
	if err != nil {
		t.Fatalf("Create(work): %v", err)
	}
	status := "in_progress"
	if err := env.store.Update(work.ID, beads.UpdateOpts{Status: &status}); err != nil {
		t.Fatalf("Update(work): %v", err)
	}
	cityPath := t.TempDir()
	writeHealthCache(t, cityPath, "claude-haiku", "unhealthy", nowSecs())

	env.reconcileAtPath(cityPath, []beads.Bead{session})

	if !strings.Contains(env.stderr.String(), "claim-holder-stalled") {
		t.Fatalf("stderr = %q, want the claim-holder stall recycle for a start_command session", env.stderr.String())
	}
}
