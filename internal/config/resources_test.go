package config

import (
	"strings"
	"testing"
)

func TestParseMemorySize(t *testing.T) {
	cases := map[string]int64{
		"1024":     1024,
		"8GiB":     8 << 30,
		"8G":       8 << 30,
		"8gb":      8 << 30,
		"1.5 GiB":  3 << 29,
		"512MiB":   512 << 20,
		"512M":     512 << 20,
		"64KiB":    64 << 10,
		"1TiB":     1 << 40,
		" 2 GB ":   2 << 30,
		"0.25GiB":  1 << 28,
		"100B":     100,
		"1.25 gib": 5 << 28,
	}
	for in, want := range cases {
		got, err := ParseMemorySize(in)
		if err != nil || got != want {
			t.Errorf("ParseMemorySize(%q) = %d, %v; want %d", in, got, err, want)
		}
	}
	for _, bad := range []string{"", "GiB", "-1GiB", "eight", "1.5XB", "NaN", "Inf"} {
		if _, err := ParseMemorySize(bad); err == nil {
			t.Errorf("ParseMemorySize(%q) succeeded, want error", bad)
		}
	}
}

func parseResourcesCity(t *testing.T, doc string) *City {
	t.Helper()
	cfg, err := Parse([]byte(doc))
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	return cfg
}

func TestParseResourcesTable(t *testing.T) {
	cfg := parseResourcesCity(t, `
[workspace]
name = "x"

[resources]
memory = "auto"
reserve = "4GiB"
default_class = "light"

[resources.classes.light]
memory = "1GiB"

[resources.classes.heavy]
memory = "8GiB"
max_active_sessions = 2

[[agent]]
name = "builder"
resource_class = "heavy"

[[agent]]
name = "chat"
`)
	r := cfg.Resources
	if !r.Enabled() || !r.MemoryIsAuto() || r.ReserveBytes() != 4<<30 {
		t.Fatalf("resources = %+v", r)
	}
	if got := r.Classes["heavy"]; got.ResourceClassBytes() != 8<<30 || got.MaxActiveSessions == nil || *got.MaxActiveSessions != 2 {
		t.Fatalf("heavy = %+v", got)
	}
	if c := r.EffectiveResourceClass(&cfg.Agents[0]); c != "heavy" {
		t.Fatalf("builder class = %q", c)
	}
	if c := r.EffectiveResourceClass(&cfg.Agents[1]); c != "light" {
		t.Fatalf("chat class = %q (default_class)", c)
	}
	warnings, err := ValidateResources(cfg, "city.toml")
	if err != nil || len(warnings) != 0 {
		t.Fatalf("ValidateResources = %v, %v", warnings, err)
	}
}

func TestValidateResources(t *testing.T) {
	zero := 0
	neg := -1
	agent := func(class string) Agent { return Agent{Name: "a", ResourceClass: class} }
	cases := []struct {
		name     string
		cfg      City
		wantErr  string
		wantWarn string
	}{
		{name: "empty", cfg: City{}},
		{name: "inert class without table", cfg: City{Agents: []Agent{agent("heavy")}}, wantWarn: "has no effect"},
		{name: "reserve without budget", cfg: City{Resources: ResourcesConfig{Reserve: "1GiB"}}, wantErr: "need memory or classes"},
		{name: "bad memory", cfg: City{Resources: ResourcesConfig{Memory: "lots"}}, wantErr: "memory"},
		{name: "zero memory", cfg: City{Resources: ResourcesConfig{Memory: "0"}}, wantErr: "greater than zero"},
		{name: "bad reserve", cfg: City{Resources: ResourcesConfig{Memory: "8GiB", Reserve: "x"}}, wantErr: "reserve"},
		{name: "reserve too big", cfg: City{Resources: ResourcesConfig{Memory: "8GiB", Reserve: "8GiB"}}, wantWarn: "not below memory"},
		{name: "empty class", cfg: City{Resources: ResourcesConfig{Memory: "8GiB", Classes: map[string]ResourceClass{"c": {}}}}, wantErr: "set memory"},
		{name: "bad class memory", cfg: City{Resources: ResourcesConfig{Memory: "8GiB", Classes: map[string]ResourceClass{"c": {Memory: "big"}}}}, wantErr: "classes.c] memory"},
		{name: "negative class max", cfg: City{Resources: ResourcesConfig{Classes: map[string]ResourceClass{"c": {MaxActiveSessions: &neg}}}}, wantErr: ">= 0"},
		{name: "unknown default", cfg: City{Resources: ResourcesConfig{Memory: "8GiB", DefaultClass: "nope"}}, wantErr: "default_class"},
		{name: "unknown agent class", cfg: City{Resources: ResourcesConfig{Memory: "8GiB", Classes: map[string]ResourceClass{"c": {Memory: "1GiB"}}}, Agents: []Agent{agent("nope")}}, wantErr: "not defined"},
		{name: "never fits", cfg: City{Resources: ResourcesConfig{Memory: "8GiB", Reserve: "4GiB", Classes: map[string]ResourceClass{"c": {Memory: "6GiB"}}}, Agents: []Agent{agent("c")}}, wantWarn: "never start"},
		{name: "class refused", cfg: City{Resources: ResourcesConfig{Classes: map[string]ResourceClass{"c": {MaxActiveSessions: &zero}}}, Agents: []Agent{agent("c")}}, wantWarn: "max_active_sessions = 0"},
		{name: "unused never-fit class is quiet", cfg: City{Resources: ResourcesConfig{Memory: "8GiB", Classes: map[string]ResourceClass{"c": {Memory: "16GiB"}}}}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			warnings, err := ValidateResources(&tc.cfg, "city.toml")
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("err = %v, want containing %q", err, tc.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected err %v", err)
			}
			joined := strings.Join(warnings, "\n")
			if tc.wantWarn == "" && joined != "" {
				t.Fatalf("unexpected warnings %q", joined)
			}
			if !strings.Contains(joined, tc.wantWarn) {
				t.Fatalf("warnings %q, want containing %q", joined, tc.wantWarn)
			}
		})
	}
}

func TestAgentPatchAndOverrideSetResourceClass(t *testing.T) {
	heavy := "heavy"
	a := Agent{Name: "builder"}
	applyAgentPatchFields(&a, &AgentPatch{ResourceClass: &heavy})
	if a.ResourceClass != "heavy" {
		t.Fatalf("patch: resource_class = %q", a.ResourceClass)
	}
	b := Agent{Name: "builder"}
	applyAgentOverride(&b, &AgentOverride{Agent: "builder", ResourceClass: &heavy})
	if b.ResourceClass != "heavy" {
		t.Fatalf("override: resource_class = %q", b.ResourceClass)
	}
}
