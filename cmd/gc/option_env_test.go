package main

import (
	"bytes"
	"log"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/gastownhall/gascity/internal/beads"
	"github.com/gastownhall/gascity/internal/config"
	"github.com/gastownhall/gascity/internal/runtime"
	sessionpkg "github.com/gastownhall/gascity/internal/session"
)

// optionEnvProvider mirrors the dsh effort schema: each choice carries both a
// --patch FlagArgs and a GC_EFFORT Env, so the harness can learn the tier even
// on a path where only the env lands.
func optionEnvProvider() *config.ResolvedProvider {
	return &config.ResolvedProvider{
		Name:    "dsh-deepseek-flash",
		Command: "dsh-minimal",
		OptionsSchema: []config.ProviderOption{
			{
				Key: "effort",
				Choices: []config.OptionChoice{
					{Value: "low", FlagArgs: []string{"--patch", "/tmp/effort-low.yml"}, Env: map[string]string{"GC_EFFORT": "low"}},
					{Value: "high", FlagArgs: []string{"--patch", "/tmp/effort-high.yml"}, Env: map[string]string{"GC_EFFORT": "high"}},
				},
			},
		},
		EffectiveDefaults: map[string]string{},
	}
}

func TestApplySchemaOptionOverridesForLaunchMergesChoiceEnv(t *testing.T) {
	rp := optionEnvProvider()
	tp := &TemplateParams{ResolvedProvider: rp}
	cfg := &runtime.Config{
		Command: "dsh-minimal",
		Env:     map[string]string{"GC_EFFORT": "provider-default", "OTHER": "kept"},
	}

	applySchemaOptionOverridesForLaunch(cfg, tp, "sess-env", map[string]string{"effort": "low"})

	if got := cfg.Env["GC_EFFORT"]; got != "low" {
		t.Fatalf("GC_EFFORT = %q, want low (choice env must win over provider env)", got)
	}
	if got := cfg.Env["OTHER"]; got != "kept" {
		t.Fatalf("OTHER = %q, want kept (unrelated provider env preserved)", got)
	}
	if !strings.Contains(cfg.Command, "--patch /tmp/effort-low.yml") {
		t.Fatalf("command = %q, want the low choice's flag args", cfg.Command)
	}
}

func TestBuildPreparedStartResolvesOptionFromTriggerBead(t *testing.T) {
	store := beads.NewMemStore()
	candidate := newOptionSessionCandidate(t, store, nil, nil)

	// The snapshot the reconciler captured before the sling ranks a different
	// bead newest for this assignee and asks for high.
	snapshot := []beads.Bead{{
		ID:        "snapshot-winner",
		Status:    "in_progress",
		Assignee:  "worker",
		CreatedAt: time.Now().UTC().Add(time.Hour),
		Metadata:  map[string]string{"opt_effort": "high"},
	}}

	// The triggering bead the session was actually routed to asks for low.
	trigger, err := store.Create(beads.Bead{
		Title:    "trigger work",
		Type:     "task",
		Assignee: "worker",
		Metadata: map[string]string{"opt_effort": "low"},
	})
	if err != nil {
		t.Fatalf("Create(trigger): %v", err)
	}
	inProgress := "in_progress"
	if err := store.Update(trigger.ID, beads.UpdateOpts{Status: &inProgress}); err != nil {
		t.Fatalf("mark trigger in_progress: %v", err)
	}
	candidate.info.TriggerBeadID = trigger.ID

	prepared, _, err := buildPreparedStartWithWorkDirResolver(
		candidate, "", &config.City{}, store, nil,
		newAssignedTaskOptionResolver(snapshot),
		nil,
	)
	if err != nil {
		t.Fatalf("buildPreparedStartWithWorkDirResolver: %v", err)
	}
	if !strings.Contains(prepared.cfg.Command, "--effort low") {
		t.Fatalf("command = %q, want the trigger bead's --effort low", prepared.cfg.Command)
	}
	if strings.Contains(prepared.cfg.Command, "--effort high") {
		t.Fatalf("command = %q, snapshot winner's --effort high must not apply", prepared.cfg.Command)
	}
}

// TestBuildPreparedStartResolvesRigTriggerBeadOptionOnEmptyPool pins the
// phase-4b regression: a pool woken from zero (empty assigned-work snapshot) is
// slung a rig-prefixed work bead that lives in the rig store, not the leading
// store. The trigger-bead option read must route through the store that owns the
// id prefix so the choice's flag args AND env reach the launch line.
func TestBuildPreparedStartResolvesRigTriggerBeadOptionOnEmptyPool(t *testing.T) {
	cityStore := beads.NewMemStore()
	candidate := newOptionSessionCandidate(t, cityStore, nil, nil)
	candidate.tp.ResolvedProvider = optionEnvProvider()

	rigBacking := beads.NewMemStore()
	rigBacking.HonorExplicitIDs = true
	rigStore := prefixDeclaringStore{Store: rigBacking, prefix: "gl"}
	trigger, err := rigStore.Create(beads.Bead{
		ID:       "gl-u91r0y",
		Title:    "rig-routed work",
		Type:     "task",
		Status:   "open",
		Metadata: map[string]string{"opt_effort": "low", "gc.routed_to": "worker"},
	})
	if err != nil {
		t.Fatalf("Create(rig trigger): %v", err)
	}
	candidate.info.TriggerBeadID = trigger.ID

	// No snapshot resolver: an empty pool has no assignedWorkBeads, which is the
	// wiring gap this test exercises. The rig stores are the only path to the bead.
	prepared, _, err := buildPreparedStartWithWorkDirResolver(
		candidate, "", &config.City{}, cityStore, nil, nil,
		map[string]beads.Store{"gateway-llm": rigStore},
	)
	if err != nil {
		t.Fatalf("buildPreparedStartWithWorkDirResolver: %v", err)
	}
	if !strings.Contains(prepared.cfg.Command, "--patch /tmp/effort-low.yml") {
		t.Fatalf("command = %q, want the rig trigger bead's low flag args", prepared.cfg.Command)
	}
	if got := prepared.cfg.Env["GC_EFFORT"]; got != "low" {
		t.Fatalf("GC_EFFORT = %q, want low from the rig trigger bead's choice env", got)
	}
}

func TestNewAssignedTaskOptionResolverPrefersTriggerBead(t *testing.T) {
	now := time.Now().UTC()
	snapshot := []beads.Bead{
		{
			ID:        "trigger",
			Status:    "in_progress",
			Assignee:  "worker",
			CreatedAt: now,
			Metadata:  map[string]string{"opt_effort": "low"},
		},
		{
			ID:        "newer",
			Status:    "in_progress",
			Assignee:  "worker",
			CreatedAt: now.Add(time.Minute),
			Metadata:  map[string]string{"opt_effort": "high"},
		},
	}
	resolver := newAssignedTaskOptionResolver(snapshot)
	candidate := startCandidate{info: sessionpkg.Info{
		ID:                  "sess-trigger",
		SessionNameMetadata: "worker",
		TriggerBeadID:       "trigger",
	}}

	got := resolver(candidate, &config.City{}, optionSchemaProvider())
	want := map[string]string{"effort": "low"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("resolver = %v, want %v (trigger bead outranks the snapshot winner)", got, want)
	}
}

func TestResolveTaskOptionOverridesInvalidValueLogsAndSkips(t *testing.T) {
	store := beads.NewMemStore()
	work, err := store.Create(beads.Bead{
		Title:    "active step",
		Type:     "task",
		Assignee: "worker-session",
		Metadata: map[string]string{
			"opt_model":  "sonnet",
			"opt_effort": "nonsense",
		},
	})
	if err != nil {
		t.Fatalf("Create(work): %v", err)
	}
	inProgress := "in_progress"
	if err := store.Update(work.ID, beads.UpdateOpts{Status: &inProgress}); err != nil {
		t.Fatalf("mark in_progress: %v", err)
	}

	var buf bytes.Buffer
	prevWriter, prevFlags := log.Writer(), log.Flags()
	log.SetOutput(&buf)
	log.SetFlags(0)
	defer func() {
		log.SetOutput(prevWriter)
		log.SetFlags(prevFlags)
	}()

	got := resolveTaskOptionOverrides(store, optionSchemaProvider(), "worker-session")
	if want := map[string]string{"model": "sonnet"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("resolveTaskOptionOverrides = %v, want %v", got, want)
	}
	if !strings.Contains(buf.String(), `ignoring opt_effort="nonsense"`) {
		t.Fatalf("log = %q, want the existing ignoring opt_effort line", buf.String())
	}
}
