package main

import (
	"context"
	"strings"
	"testing"

	"github.com/gastownhall/gascity/internal/beadmeta"
	"github.com/gastownhall/gascity/internal/beads"
	"github.com/gastownhall/gascity/internal/config"
	"github.com/gastownhall/gascity/internal/runtime"
	sessionpkg "github.com/gastownhall/gascity/internal/session"
)

// TestAlivePoolSessionNeedsTriggerOptionCycle pins the decision that lets a
// min_active_sessions floor lane be recycled when a sling binds a trigger whose
// opt_<key> overrides were not on the floor lane's launch line.
func TestAlivePoolSessionNeedsTriggerOptionCycle(t *testing.T) {
	store := beads.NewMemStore()
	trigger, err := store.Create(beads.Bead{
		Title: "sling work", Type: "task", Status: "open",
		Metadata: map[string]string{"opt_effort": "low"},
	})
	if err != nil {
		t.Fatalf("Create(trigger): %v", err)
	}
	plain, err := store.Create(beads.Bead{Title: "no options", Type: "task", Status: "open"})
	if err != nil {
		t.Fatalf("Create(plain): %v", err)
	}

	rp := optionEnvProvider()
	base := func() sessionpkg.Info {
		return sessionpkg.Info{
			ID:          "sess-floor",
			WakeMode:    "fresh",
			PoolManaged: true,
		}
	}

	cases := []struct {
		name      string
		info      sessionpkg.Info
		provider  *config.ResolvedProvider
		assigned  string
		wantCycle bool
	}{
		{
			name:      "idle floor lane with a trigger option cycles",
			info:      func() sessionpkg.Info { i := base(); i.TriggerBeadID = trigger.ID; return i }(),
			provider:  rp,
			wantCycle: true,
		},
		{
			name:      "trigger option resolved from the assigned anchor cycles",
			info:      base(),
			provider:  rp,
			assigned:  trigger.ID,
			wantCycle: true,
		},
		{
			name: "already processing the trigger does not cycle",
			info: func() sessionpkg.Info {
				i := base()
				i.TriggerBeadID = trigger.ID
				i.CurrentlyProcessingBeadID = trigger.ID
				return i
			}(),
			provider:  rp,
			wantCycle: false,
		},
		{
			name:      "resume lane keeps its conversation",
			info:      func() sessionpkg.Info { i := base(); i.WakeMode = "resume"; i.TriggerBeadID = trigger.ID; return i }(),
			provider:  rp,
			wantCycle: false,
		},
		{
			name: "named session is not a floor lane",
			info: func() sessionpkg.Info {
				i := base()
				i.ConfiguredNamedSession = true
				i.TriggerBeadID = trigger.ID
				return i
			}(),
			provider:  rp,
			wantCycle: false,
		},
		{
			name:      "non-pool session is not a floor lane",
			info:      func() sessionpkg.Info { i := base(); i.PoolManaged = false; i.TriggerBeadID = trigger.ID; return i }(),
			provider:  rp,
			wantCycle: false,
		},
		{
			name:      "trigger without options does not cycle",
			info:      func() sessionpkg.Info { i := base(); i.TriggerBeadID = plain.ID; return i }(),
			provider:  rp,
			wantCycle: false,
		},
		{
			name: "template override already pins the value",
			info: func() sessionpkg.Info {
				i := base()
				i.TriggerBeadID = trigger.ID
				i.TemplateOverrides = `{"effort":"low"}`
				return i
			}(),
			provider:  rp,
			wantCycle: false,
		},
		{
			name:      "provider without an options schema does not cycle",
			info:      func() sessionpkg.Info { i := base(); i.TriggerBeadID = trigger.ID; return i }(),
			provider:  nil,
			wantCycle: false,
		},
		{
			name:      "no trigger and no assigned anchor does not cycle",
			info:      base(),
			provider:  rp,
			wantCycle: false,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := alivePoolSessionNeedsTriggerOptionCycle(
				store, TemplateParams{ResolvedProvider: tc.provider}, tc.info, tc.assigned, nil,
			)
			if got != tc.wantCycle {
				t.Fatalf("alivePoolSessionNeedsTriggerOptionCycle = %v, want %v", got, tc.wantCycle)
			}
		})
	}
}

// reconcileWithAssignedWork drives one reconciler tick for a pool env, threading
// the assigned-work snapshot so ComputeAwakeSet anchors the claimed trigger.
func reconcilePoolWithAssignedWork(env *reconcilerTestEnv, sessions []beads.Bead, assignedWork []beads.Bead) int {
	poolDesired := make(map[string]int)
	for _, tp := range env.desiredState {
		if tp.TemplateName != "" {
			poolDesired[tp.TemplateName]++
		}
	}
	cfgNames := configuredSessionNames(env.cfg, "", env.store)
	return reconcileSessionBeads(
		context.Background(),
		sessions,
		env.desiredState,
		cfgNames,
		env.cfg,
		env.sp,
		env.store,
		nil,
		assignedWork,
		nil,
		env.dt,
		poolDesired,
		false,
		nil,
		"",
		nil,
		env.clk,
		env.rec,
		0,
		0,
		&env.stdout,
		&env.stderr,
		env.startOptions...,
	)
}

// TestReconcileSessionBeads_FloorLaneRecyclesForTriggerOption is the regression
// test for the min_active_sessions>=1 tier bug: a floor lane is restored with no
// trigger bead and runs at the provider default. When a sling later binds a
// trigger carrying opt_effort=low, the alive idle lane must be recycled so the
// next launch line renders the low choice's flag args and env — warm reuse must
// not hand the bead to the untiered process. It also proves the recycled lane's
// next start carries GC_EFFORT=low.
func TestReconcileSessionBeads_FloorLaneRecyclesForTriggerOption(t *testing.T) {
	env := newReconcilerTestEnv()
	env.cfg = &config.City{
		Agents: []config.Agent{{
			Name:              "implementation-worker",
			StartCommand:      "dsh-minimal",
			MinActiveSessions: intPtr(1),
			MaxActiveSessions: intPtr(3),
			WakeMode:          "fresh",
		}},
	}
	env.desiredState["lane"] = TemplateParams{
		Command:          "dsh-minimal",
		SessionName:      "lane",
		TemplateName:     "implementation-worker",
		ResolvedProvider: optionEnvProvider(),
	}

	session := env.createSessionBead("lane", "implementation-worker")
	env.setSessionMetadata(&session, map[string]string{
		"state":        "active",
		"wake_mode":    "fresh",
		"pool_managed": "true",
	})
	// The floor lane is already alive and running at the provider default.
	if err := env.sp.Start(context.Background(), "lane", runtime.Config{Command: "dsh-minimal"}); err != nil {
		t.Fatalf("start floor lane: %v", err)
	}

	// The sling/claim: trigger bead with opt_effort=low, assignee = lane id.
	trigger, err := env.store.Create(beads.Bead{
		Title:    "sling work",
		Type:     "task",
		Assignee: session.ID,
		Metadata: map[string]string{
			"opt_effort":   "low",
			"gc.routed_to": "gateway-llm/gc.implementation-worker",
		},
	})
	if err != nil {
		t.Fatalf("Create(trigger): %v", err)
	}
	inProgress := "in_progress"
	if err := env.store.Update(trigger.ID, beads.UpdateOpts{Status: &inProgress}); err != nil {
		t.Fatalf("mark trigger in_progress: %v", err)
	}
	trigger, err = env.store.Get(trigger.ID)
	if err != nil {
		t.Fatalf("Get(trigger): %v", err)
	}

	// The desired-state build stamps the trigger onto the lane; the lane's
	// process still predates it (currently_processing_bead_id is empty).
	env.setSessionMetadata(&session, map[string]string{
		beadmeta.TriggerBeadIDMetadataKey: trigger.ID,
	})
	session, err = env.store.Get(session.ID)
	if err != nil {
		t.Fatalf("Get(session): %v", err)
	}

	// Tick 1: the alive idle floor lane is recycled instead of warm-reused.
	reconcilePoolWithAssignedWork(env, []beads.Bead{session}, []beads.Bead{trigger})

	if env.sp.IsRunning("lane") {
		t.Fatal("idle floor lane was warm-reused; want it recycled so the trigger tier lands")
	}
	session, err = env.store.Get(session.ID)
	if err != nil {
		t.Fatalf("Get(session after cycle): %v", err)
	}
	if got := session.Metadata[sessionpkg.CurrentBeadIDKey]; got != trigger.ID {
		t.Fatalf("%s = %q, want %q", sessionpkg.CurrentBeadIDKey, got, trigger.ID)
	}
	if got := session.Metadata["continuation_reset_pending"]; got != "true" {
		t.Fatalf("continuation_reset_pending = %q, want true so the next wake is a fresh tiered launch", got)
	}

	// Tick 2: the recycled lane wakes on the trigger; its launch line must
	// carry the low choice's flag args and env.
	reconcilePoolWithAssignedWork(env, []beads.Bead{session}, []beads.Bead{trigger})

	startCfg := env.sp.LastStartConfig("lane")
	if startCfg == nil {
		t.Fatalf("recycled floor lane was not restarted; stderr=%s", env.stderr.String())
	}
	if got := startCfg.Env["GC_EFFORT"]; got != "low" {
		t.Fatalf("GC_EFFORT = %q, want low from the trigger bead's opt_effort; command=%q", got, startCfg.Command)
	}
	if !strings.Contains(startCfg.Command, "--patch /tmp/effort-low.yml") {
		t.Fatalf("command = %q, want the low choice's flag args", startCfg.Command)
	}
}
