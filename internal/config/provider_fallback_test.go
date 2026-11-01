package config

import (
	"errors"
	"slices"
	"strings"
	"testing"

	"github.com/gastownhall/gascity/internal/fsys"
)

// fallbackCity is a city whose catalog holds claude and codex, with one
// worker agent carrying the given extra TOML lines.
func fallbackCity(agentLines, tail string) string {
	return `
[workspace]
provider = "claude"

[providers.claude]
base = "builtin:claude"

[providers.codex]
base = "builtin:codex"

[[agent]]
name = "worker"
` + agentLines + "\n" + tail
}

// Kills: fallback_providers not parsed, or parsed under another key.
func TestLoadWithIncludes_FallbackProvidersParsed(t *testing.T) {
	fs := fsys.NewFake()
	fs.Files["/city/city.toml"] = []byte(fallbackCity(`fallback_providers = ["codex", "claude"]`, ""))
	cfg, _, err := LoadWithIncludes(fs, "/city/city.toml")
	if err != nil {
		t.Fatalf("LoadWithIncludes: %v", err)
	}
	a := findAgentNamed(t, cfg, "worker")
	if want := []string{"codex", "claude"}; !slices.Equal(a.FallbackProviders, want) {
		t.Fatalf("FallbackProviders = %v, want %v", a.FallbackProviders, want)
	}
}

// Kills: a fallback_providers name missing from [providers] accepted at
// load, only to fail on the first red tick.
func TestLoadWithIncludes_FallbackProvidersUnknownNameFails(t *testing.T) {
	fs := fsys.NewFake()
	fs.Files["/city/city.toml"] = []byte(fallbackCity(`fallback_providers = ["codex", "gemini"]`, ""))
	_, _, err := LoadWithIncludes(fs, "/city/city.toml")
	if err == nil {
		t.Fatal("LoadWithIncludes accepted an unknown fallback provider")
	}
	var catalogErr *ProviderCatalogError
	if !errors.As(err, &catalogErr) {
		t.Fatalf("error = %v, want a ProviderCatalogError", err)
	}
	if !strings.Contains(err.Error(), `agent "worker": fallback_providers entry "gemini"`) {
		t.Fatalf("error = %q, want it to name the fallback entry", err.Error())
	}
	if strings.Contains(err.Error(), `"codex"`) {
		t.Fatalf("error = %q names codex, which is in the catalog", err.Error())
	}
}

// Kills: [[patches.agent]] fallback_providers ignored, or appended instead
// of replacing the list.
func TestLoadWithIncludes_PatchReplacesFallbackProviders(t *testing.T) {
	fs := fsys.NewFake()
	fs.Files["/city/city.toml"] = []byte(fallbackCity(`fallback_providers = ["codex", "claude"]`, `
[[patches.agent]]
name = "worker"
fallback_providers = ["codex"]
`))
	cfg, _, err := LoadWithIncludes(fs, "/city/city.toml")
	if err != nil {
		t.Fatalf("LoadWithIncludes: %v", err)
	}
	a := findAgentNamed(t, cfg, "worker")
	if want := []string{"codex"}; !slices.Equal(a.FallbackProviders, want) {
		t.Fatalf("FallbackProviders after patch = %v, want %v", a.FallbackProviders, want)
	}
}

// Kills: a patch without fallback_providers clearing the agent's list.
func TestApplyPatches_FallbackProvidersUnsetKeepsList(t *testing.T) {
	cfg := &City{Agents: []Agent{{Name: "worker", Provider: "claude", FallbackProviders: []string{"codex"}}}}
	if err := ApplyPatches(cfg, Patches{Agents: []AgentPatch{{Name: "worker", Provider: ptrStr("claude")}}}); err != nil {
		t.Fatalf("ApplyPatches: %v", err)
	}
	if want := []string{"codex"}; !slices.Equal(cfg.Agents[0].FallbackProviders, want) {
		t.Fatalf("FallbackProviders = %v, want %v", cfg.Agents[0].FallbackProviders, want)
	}
	empty := []string{}
	if err := ApplyPatches(cfg, Patches{Agents: []AgentPatch{{Name: "worker", FallbackProviders: &empty}}}); err != nil {
		t.Fatalf("ApplyPatches: %v", err)
	}
	if len(cfg.Agents[0].FallbackProviders) != 0 {
		t.Fatalf("FallbackProviders after empty patch = %v, want cleared", cfg.Agents[0].FallbackProviders)
	}
}

func findAgentNamed(t *testing.T, cfg *City, name string) Agent {
	t.Helper()
	for _, a := range cfg.Agents {
		if a.Name == name {
			return a
		}
	}
	t.Fatalf("no agent %q", name)
	return Agent{}
}
