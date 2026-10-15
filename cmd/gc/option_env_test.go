package main

import (
	"strings"
	"testing"

	"github.com/gastownhall/gascity/internal/beads"
	"github.com/gastownhall/gascity/internal/config"
	"github.com/gastownhall/gascity/internal/runtime"
)

// optionEnvProvider mirrors an effort schema for a harness whose only effort
// lever is an env var: each choice carries both FlagArgs and an Env entry.
func optionEnvProvider() *config.ResolvedProvider {
	return &config.ResolvedProvider{
		Name:    "dsh",
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
	tp := &TemplateParams{ResolvedProvider: optionEnvProvider()}
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

// TestBuildPreparedStartAppliesTriggerBeadOptionEnv pins the dispatch path: a
// trigger bead's opt_effort reaches the launch env, not just the command line.
func TestBuildPreparedStartAppliesTriggerBeadOptionEnv(t *testing.T) {
	store := beads.NewMemStore()
	candidate, _ := newTriggerOptionSessionCandidate(t, store, map[string]string{"effort": "high"}, nil, "")
	candidate.tp.ResolvedProvider = optionEnvProvider()
	candidate.tp.Command = "dsh-minimal"
	candidate.tp.Env = map[string]string{"GC_EFFORT": "provider-default"}

	prepared, _, err := buildPreparedStart(candidate, &config.City{}, store)
	if err != nil {
		t.Fatalf("buildPreparedStart: %v", err)
	}
	if got := prepared.cfg.Env["GC_EFFORT"]; got != "high" {
		t.Fatalf("launch env GC_EFFORT = %q, want high from the trigger bead's opt_effort", got)
	}
	if !strings.Contains(prepared.cfg.Command, "--patch /tmp/effort-high.yml") {
		t.Fatalf("command = %q, want the high choice's flag args", prepared.cfg.Command)
	}
}

// TestSessionCoreConfigForHashInfoIncludesTemplateOverrideEnv keeps the drift
// hash in step with the launch: a template_overrides choice's env lands in
// the hashed config just as applySchemaOptionOverridesForLaunch sets it.
func TestSessionCoreConfigForHashInfoIncludesTemplateOverrideEnv(t *testing.T) {
	store := beads.NewMemStore()
	candidate := newOptionSessionCandidate(t, store, nil, map[string]string{"effort": "low"})
	candidate.tp.ResolvedProvider = optionEnvProvider()
	candidate.tp.Command = "dsh-minimal"

	hashed := sessionCoreConfigForHashInfo(candidate.tp, candidate.info)
	if got := hashed.Env["GC_EFFORT"]; got != "low" {
		t.Fatalf("hashed env GC_EFFORT = %q, want low from template_overrides", got)
	}
	prepared, _, err := buildPreparedStart(candidate, &config.City{}, store)
	if err != nil {
		t.Fatalf("buildPreparedStart: %v", err)
	}
	if got := prepared.cfg.Env["GC_EFFORT"]; got != "low" {
		t.Fatalf("launch env GC_EFFORT = %q, want low from template_overrides", got)
	}
}
