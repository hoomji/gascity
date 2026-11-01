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
	a := findWorker(t, cfg)
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
	a := findWorker(t, cfg)
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

func findWorker(t *testing.T, cfg *City) Agent {
	t.Helper()
	const name = "worker"
	for _, a := range cfg.Agents {
		if a.Name == name {
			return a
		}
	}
	t.Fatalf("no agent %q", name)
	return Agent{}
}

// optionCity has a primary alpha and a fallback beta whose closed options
// are model (beta-small, beta-large) and effort (low, high).
func optionCity(agentLines, tail string) string {
	return `
[workspace]
provider = "alpha"

[providers.alpha]
command = "alpha-cli"

[providers.beta]
command = "beta-cli"

[[providers.beta.options_schema]]
key = "model"
type = "select"
choices = [{ value = "beta-small", flag_args = ["--model", "beta-small"] }, { value = "beta-large", flag_args = ["--model", "beta-large"] }]

[[providers.beta.options_schema]]
key = "effort"
type = "select"
choices = [{ value = "low", flag_args = ["--effort", "low"] }, { value = "high", flag_args = ["--effort", "high"] }]

[[agent]]
name = "worker"
fallback_providers = ["beta"]
` + agentLines + "\n" + tail
}

// Kills: fallback_option_defaults not parsed under its TOML key.
func TestLoadWithIncludes_FallbackOptionDefaultsParsed(t *testing.T) {
	fs := fsys.NewFake()
	fs.Files["/city/city.toml"] = []byte(optionCity(`
[agent.fallback_option_defaults.beta]
model = "beta-large"
effort = "high"
`, ""))
	cfg, _, err := LoadWithIncludes(fs, "/city/city.toml")
	if err != nil {
		t.Fatalf("LoadWithIncludes: %v", err)
	}
	got := findWorker(t, cfg).FallbackOptionDefaults["beta"]
	if got["model"] != "beta-large" || got["effort"] != "high" || len(got) != 2 {
		t.Fatalf("FallbackOptionDefaults[beta] = %v", got)
	}
}

// Kills: load accepting an overlay for a provider the agent cannot fall
// back to, an option key outside the provider's schema, or a value that is
// not one of its choices.
func TestLoadWithIncludes_FallbackOptionDefaultsRejected(t *testing.T) {
	for _, tc := range []struct {
		name, lines, want string
	}{
		{name: "key not in fallback_providers", lines: "[agent.fallback_option_defaults.alpha]\nmodel = \"x\"\n", want: `fallback_option_defaults key "alpha" is not listed in fallback_providers`},
		{name: "bad option value", lines: "[agent.fallback_option_defaults.beta]\neffort = \"extreme\"\n", want: `fallback_option_defaults.beta: option_defaults key "effort": value "extreme" is not a valid choice`},
		{name: "unknown option key", lines: "[agent.fallback_option_defaults.beta]\nshade = \"red\"\n", want: `fallback_option_defaults.beta: option_defaults key "shade" is not in the options schema`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			fs := fsys.NewFake()
			fs.Files["/city/city.toml"] = []byte(optionCity(tc.lines, ""))
			_, _, err := LoadWithIncludes(fs, "/city/city.toml")
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("error = %v, want it to contain %q", err, tc.want)
			}
		})
	}
}

// Kills: a patch replacing fallback_option_defaults wholesale instead of
// merging per provider and per option key.
func TestLoadWithIncludes_PatchMergesFallbackOptionDefaults(t *testing.T) {
	fs := fsys.NewFake()
	fs.Files["/city/city.toml"] = []byte(optionCity(`
[agent.fallback_option_defaults.beta]
model = "beta-small"
effort = "low"
`, `
[[patches.agent]]
name = "worker"
[patches.agent.fallback_option_defaults.beta]
effort = "high"
`))
	cfg, _, err := LoadWithIncludes(fs, "/city/city.toml")
	if err != nil {
		t.Fatalf("LoadWithIncludes: %v", err)
	}
	got := findWorker(t, cfg).FallbackOptionDefaults["beta"]
	if got["model"] != "beta-small" || got["effort"] != "high" || len(got) != 2 {
		t.Fatalf("FallbackOptionDefaults[beta] after patch = %v, want model kept and effort patched", got)
	}
}
