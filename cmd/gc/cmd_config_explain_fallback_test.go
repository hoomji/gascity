package main

import (
	"bytes"
	"strings"
	"testing"

	"github.com/gastownhall/gascity/internal/config"
	"github.com/gastownhall/gascity/internal/fsys"
)

// Kills: gc config explain omitting a fallback_providers list inherited from
// [agent_defaults], or attributing it to the agent's own definition.
func TestExplainAgentShowsInheritedFallbackProviders(t *testing.T) {
	fs := fsys.NewFake()
	fs.Files["/city/city.toml"] = []byte(`
[workspace]
provider = "claude"

[providers.claude]
base = "builtin:claude"

[providers.codex]
base = "builtin:codex"

[agent_defaults]
fallback_providers = ["codex"]

[[agent]]
name = "worker"

[[agent]]
name = "pinned"
fallback_providers = ["claude"]
`)
	cfg, prov, err := config.LoadWithIncludes(fs, "/city/city.toml")
	if err != nil {
		t.Fatalf("LoadWithIncludes: %v", err)
	}
	explain := func(name string) string {
		for i := range cfg.Agents {
			if cfg.Agents[i].Name == name {
				var buf bytes.Buffer
				explainAgent(&buf, &cfg.Agents[i], prov)
				return buf.String()
			}
		}
		t.Fatalf("no agent %q", name)
		return ""
	}
	line := func(out string) string {
		for _, l := range strings.Split(out, "\n") {
			if strings.Contains(l, "fallback_providers") {
				return l
			}
		}
		return ""
	}

	got := line(explain("worker"))
	if !strings.Contains(got, "codex") || !strings.Contains(got, "agent_defaults") {
		t.Fatalf("worker fallback_providers row = %q, want value codex with source agent_defaults", got)
	}
	got = line(explain("pinned"))
	if !strings.Contains(got, "claude") || strings.Contains(got, "agent_defaults") {
		t.Fatalf("pinned fallback_providers row = %q, want its own value without agent_defaults source", got)
	}
}
