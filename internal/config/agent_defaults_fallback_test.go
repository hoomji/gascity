package config

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/gastownhall/gascity/internal/fsys"
)

// defaultsFallbackCity is fallbackCity with [agent_defaults]
// fallback_providers = ["codex"] and a second agent, "other", that sets
// nothing.
func defaultsFallbackCity(agentLines, tail string) string {
	return fallbackCity(agentLines, `
[[agent]]
name = "other"

[agent_defaults]
fallback_providers = ["codex"]
`+tail)
}

func findAgentNamed(t *testing.T, cfg *City, qn string) Agent {
	t.Helper()
	for _, a := range cfg.Agents {
		if a.QualifiedName() == qn {
			return a
		}
	}
	t.Fatalf("no agent %q", qn)
	return Agent{}
}

// Kills: agent_defaults.fallback_providers not parsed or never applied to an
// agent that leaves its own list unset.
func TestAgentDefaultsFallbackProviders_InheritedWhenUnset(t *testing.T) {
	fs := fsys.NewFake()
	fs.Files["/city/city.toml"] = []byte(defaultsFallbackCity("", ""))
	cfg, _, err := LoadWithIncludes(fs, "/city/city.toml")
	if err != nil {
		t.Fatalf("LoadWithIncludes: %v", err)
	}
	for _, name := range []string{"worker", "other"} {
		a := findAgentNamed(t, cfg, name)
		if want := []string{"codex"}; !slices.Equal(a.FallbackProviders, want) {
			t.Fatalf("%s FallbackProviders = %v, want inherited %v", name, a.FallbackProviders, want)
		}
		if !a.FallbackProvidersFromDefaults {
			t.Fatalf("%s FallbackProvidersFromDefaults = false, want true", name)
		}
	}
}

// Kills: the default list merged into, or replacing, an agent's own list.
func TestAgentDefaultsFallbackProviders_AgentOwnListWins(t *testing.T) {
	fs := fsys.NewFake()
	fs.Files["/city/city.toml"] = []byte(defaultsFallbackCity(`fallback_providers = ["claude"]`, ""))
	cfg, _, err := LoadWithIncludes(fs, "/city/city.toml")
	if err != nil {
		t.Fatalf("LoadWithIncludes: %v", err)
	}
	a := findWorker(t, cfg)
	if want := []string{"claude"}; !slices.Equal(a.FallbackProviders, want) {
		t.Fatalf("FallbackProviders = %v, want own %v", a.FallbackProviders, want)
	}
	if a.FallbackProvidersFromDefaults {
		t.Fatal("FallbackProvidersFromDefaults = true for an agent with its own list")
	}
}

// Kills: an explicit empty list treated as unset, so an agent cannot opt out
// of the default list.
func TestAgentDefaultsFallbackProviders_ExplicitEmptyOptsOut(t *testing.T) {
	fs := fsys.NewFake()
	fs.Files["/city/city.toml"] = []byte(defaultsFallbackCity(`fallback_providers = []`, ""))
	cfg, _, err := LoadWithIncludes(fs, "/city/city.toml")
	if err != nil {
		t.Fatalf("LoadWithIncludes: %v", err)
	}
	if a := findWorker(t, cfg); len(a.FallbackProviders) != 0 {
		t.Fatalf("worker FallbackProviders = %v, want empty (opted out)", a.FallbackProviders)
	}
	if a := findAgentNamed(t, cfg, "other"); !slices.Equal(a.FallbackProviders, []string{"codex"}) {
		t.Fatalf("other FallbackProviders = %v, want inherited [codex]", a.FallbackProviders)
	}
}

// Kills: [[patches.agent]] fallback_providers (including an empty opt-out)
// overwritten by the default list.
func TestAgentDefaultsFallbackProviders_PatchWins(t *testing.T) {
	fs := fsys.NewFake()
	fs.Files["/city/city.toml"] = []byte(defaultsFallbackCity("", `
[[patches.agent]]
name = "worker"
fallback_providers = ["claude"]

[[patches.agent]]
name = "other"
fallback_providers = []
`))
	cfg, _, err := LoadWithIncludes(fs, "/city/city.toml")
	if err != nil {
		t.Fatalf("LoadWithIncludes: %v", err)
	}
	if a := findWorker(t, cfg); !slices.Equal(a.FallbackProviders, []string{"claude"}) {
		t.Fatalf("worker FallbackProviders = %v, want patched [claude]", a.FallbackProviders)
	}
	if a := findAgentNamed(t, cfg, "other"); len(a.FallbackProviders) != 0 {
		t.Fatalf("other FallbackProviders = %v, want empty (patched opt-out)", a.FallbackProviders)
	}
}

// Kills: rig-scoped pack agents skipped by the default, or a rig override's
// fallback_providers (including an empty opt-out) overwritten by it.
func TestAgentDefaultsFallbackProviders_RigOverrideWins(t *testing.T) {
	dir := t.TempDir()
	writeFile := func(rel, data string) {
		path := filepath.Join(dir, rel)
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatalf("MkdirAll(%s): %v", path, err)
		}
		if err := os.WriteFile(path, []byte(data), 0o644); err != nil {
			t.Fatalf("WriteFile(%s): %v", path, err)
		}
	}
	writeFile("city.toml", `
[workspace]
name = "test"
provider = "claude"

[providers.claude]
base = "builtin:claude"

[providers.codex]
base = "builtin:codex"

[agent_defaults]
fallback_providers = ["codex"]

[[rigs]]
name = "plain"
path = "plain"
includes = ["packs/base"]

[[rigs]]
name = "pinned"
path = "pinned"
includes = ["packs/base"]

  [[rigs.overrides]]
  agent = "worker"
  fallback_providers = ["claude"]

[[rigs]]
name = "optout"
path = "optout"
includes = ["packs/base"]

  [[rigs.overrides]]
  agent = "worker"
  fallback_providers = []
`)
	writeFile("packs/base/pack.toml", `
[pack]
name = "base"
schema = 1

[[agent]]
name = "worker"
scope = "rig"
`)
	cfg, _, err := LoadWithIncludes(fsys.OSFS{}, filepath.Join(dir, "city.toml"))
	if err != nil {
		t.Fatalf("LoadWithIncludes: %v", err)
	}
	if a := findAgentNamed(t, cfg, "plain/worker"); !slices.Equal(a.FallbackProviders, []string{"codex"}) {
		t.Fatalf("plain/worker FallbackProviders = %v, want inherited [codex]", a.FallbackProviders)
	}
	if a := findAgentNamed(t, cfg, "pinned/worker"); !slices.Equal(a.FallbackProviders, []string{"claude"}) {
		t.Fatalf("pinned/worker FallbackProviders = %v, want rig override [claude]", a.FallbackProviders)
	}
	if a := findAgentNamed(t, cfg, "optout/worker"); len(a.FallbackProviders) != 0 {
		t.Fatalf("optout/worker FallbackProviders = %v, want empty (rig override opt-out)", a.FallbackProviders)
	}
}

// Kills: fallback_option_defaults validated against the agent's own (unset)
// list instead of the effective list after agent_defaults applies.
func TestAgentDefaultsFallbackProviders_OptionDefaultsValidateAgainstInherited(t *testing.T) {
	city := strings.Replace(optionCity(`[agent.fallback_option_defaults.beta]
model = "beta-large"`, `
[agent_defaults]
fallback_providers = ["beta"]
`), "fallback_providers = [\"beta\"]\n", "", 1)
	if strings.Count(city, "fallback_providers") != 1 {
		t.Fatalf("test city should set fallback_providers only in agent_defaults:\n%s", city)
	}
	fs := fsys.NewFake()
	fs.Files["/city/city.toml"] = []byte(city)
	cfg, _, err := LoadWithIncludes(fs, "/city/city.toml")
	if err != nil {
		t.Fatalf("LoadWithIncludes rejected fallback_option_defaults keyed by an inherited fallback: %v", err)
	}
	if a := findWorker(t, cfg); !slices.Equal(a.FallbackProviders, []string{"beta"}) {
		t.Fatalf("FallbackProviders = %v, want inherited [beta]", a.FallbackProviders)
	}

	// An agent that opts out must still be rejected.
	fs.Files["/city/city.toml"] = []byte(strings.Replace(city, "[agent.fallback_option_defaults.beta]", "fallback_providers = []\n[agent.fallback_option_defaults.beta]", 1))
	if _, _, err := LoadWithIncludes(fs, "/city/city.toml"); err == nil || !strings.Contains(err.Error(), "not listed in fallback_providers") {
		t.Fatalf("opted-out agent with fallback_option_defaults: err = %v, want not-listed rejection", err)
	}
}

// Kills: a typo in agent_defaults.fallback_providers accepted at load.
func TestAgentDefaultsFallbackProviders_UnknownNameFails(t *testing.T) {
	fs := fsys.NewFake()
	fs.Files["/city/city.toml"] = []byte(strings.Replace(defaultsFallbackCity("", ""), `fallback_providers = ["codex"]`, `fallback_providers = ["gemini"]`, 1))
	if _, _, err := LoadWithIncludes(fs, "/city/city.toml"); err == nil || !strings.Contains(err.Error(), `"gemini"`) {
		t.Fatalf("err = %v, want an unknown-provider error naming gemini", err)
	}
}
