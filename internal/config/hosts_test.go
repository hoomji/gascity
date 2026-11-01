package config

import (
	"slices"
	"strings"
	"testing"

	"github.com/gastownhall/gascity/internal/fsys"
)

func loadHostsCity(t *testing.T, body string, frags map[string]string) (*City, *Provenance, error) {
	t.Helper()
	fs := fsys.NewFake()
	fs.Files["/city/city.toml"] = []byte(body)
	for path, data := range frags {
		fs.Files[path] = []byte(data)
	}
	return LoadWithIncludes(fs, "/city/city.toml")
}

const hostsCityBase = `
[workspace]
name = "test"

[[rigs]]
name = "web"
path = "/src/web"

[hosts.host-a]
transport = "ssh"
target = "host-a"
root = "/srv/gc/city-1"
labels = ["linux", "browser"]
priority = 10
max_active_sessions = 8
store = "direct"
[hosts.host-a.rigs]
web = "/srv/src/web"
[hosts.host-a.env]
forward = ["GH_TOKEN"]

[hosts.local]
labels = ["linux"]

[placement]
version_policy = "minor"

[[agent]]
name = "builder"
[agent.placement]
hosts = ["host-a", "local"]
require_labels = ["browser"]
prefer = ["host-a"]
`

func TestHostsConfigParsesAndComposes(t *testing.T) {
	cfg, prov, err := loadHostsCity(t, hostsCityBase, nil)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	h, ok := cfg.Hosts["host-a"]
	if !ok {
		t.Fatalf("hosts.host-a missing: %#v", cfg.Hosts)
	}
	if h.Target != "host-a" || h.Root != "/srv/gc/city-1" || h.Priority != 10 || h.MaxActiveSessions == nil || *h.MaxActiveSessions != 8 {
		t.Fatalf("host-a = %#v", h)
	}
	if h.StoreMode() != HostStoreDirect || h.Rigs["web"] != "/srv/src/web" || !slices.Equal(h.Env.Forward, []string{"GH_TOKEN"}) {
		t.Fatalf("host-a store/rigs/env = %#v", h)
	}
	if cfg.Placement.EffectiveVersionPolicy() != VersionPolicyMinor {
		t.Fatalf("version policy = %q", cfg.Placement.VersionPolicy)
	}
	var builder *Agent
	for i := range cfg.Agents {
		if cfg.Agents[i].Name == "builder" {
			builder = &cfg.Agents[i]
		}
	}
	if builder == nil || builder.Placement == nil {
		t.Fatalf("builder placement missing")
	}
	if got := AgentHosts(cfg, builder); !slices.Equal(got, []string{"host-a", "local"}) {
		t.Fatalf("AgentHosts = %v", got)
	}
	for _, w := range prov.Warnings {
		if strings.Contains(w, "host") || strings.Contains(w, "placement") {
			t.Errorf("unexpected warning: %s", w)
		}
	}
}

func TestAgentHostsDefaults(t *testing.T) {
	if got := AgentHosts(&City{}, &Agent{Name: "a"}); !slices.Equal(got, []string{LocalHostName}) {
		t.Fatalf("default = %v", got)
	}
	cfg := &City{Placement: PlacementConfig{DefaultHosts: []string{"h", "local"}}}
	if got := AgentHosts(cfg, &Agent{Name: "a"}); !slices.Equal(got, []string{"h", "local"}) {
		t.Fatalf("city default = %v", got)
	}
	if (PlacementConfig{}).EffectiveVersionPolicy() != VersionPolicyExact {
		t.Fatal("version policy default is not exact")
	}
}

func TestHostsValidationErrors(t *testing.T) {
	cases := []struct {
		name, toml, want string
	}{
		{"unknown transport", `[hosts.h]
transport = "telnet"
target = "h"
root = "/r"`, `unknown transport "telnet"`},
		{"missing transport", `[hosts.h]
target = "h"
root = "/r"`, "transport is required"},
		{"missing target", `[hosts.h]
transport = "ssh"
root = "/r"`, "target is required"},
		{"option-like target", `[hosts.h]
transport = "ssh"
target = "-oProxyCommand=x"
root = "/r"`, "must not start with '-'"},
		{"relative root", `[hosts.h]
transport = "ssh"
target = "h"
root = "r"`, "must be absolute"},
		{"unknown rig", `[hosts.h]
transport = "ssh"
target = "h"
root = "/r"
[hosts.h.rigs]
nope = "/x"`, `rig "nope" is not defined`},
		{"relative rig path", `[[rigs]]
name = "web"
path = "/src/web"
[hosts.h]
transport = "ssh"
target = "h"
root = "/r"
[hosts.h.rigs]
web = "x"`, `path "x" must be absolute`},
		{"local with remote keys", `[hosts.local]
target = "x"`, "local host takes only"},
		{"bad store", `[hosts.h]
transport = "ssh"
target = "h"
root = "/r"
store = "nfs"`, `unknown store "nfs"`},
		{"bad version policy", `[placement]
version_policy = "latest"`, `unknown version_policy "latest"`},
		{"unknown default host", `[placement]
default_hosts = ["ghost"]`, `unknown host "ghost"`},
		{"agent unknown host", `[[agent]]
name = "a"
[agent.placement]
hosts = ["ghost"]`, `placement.hosts: unknown host "ghost"`},
		{"prefer outside hosts", `[hosts.h]
transport = "ssh"
target = "h"
root = "/r"
[[agent]]
name = "a"
[agent.placement]
hosts = ["local"]
prefer = ["h"]`, `placement.prefer: host "h" is not in placement.hosts`},
		{"acp remote", `[hosts.h]
transport = "ssh"
target = "h"
root = "/r"
[[agent]]
name = "a"
session = "acp"
[agent.placement]
hosts = ["h"]`, "ACP agents are local-only"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, _, err := loadHostsCity(t, "[workspace]\nname = \"test\"\n"+tc.toml+"\n", nil)
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("err = %v, want containing %q", err, tc.want)
			}
		})
	}
}

func TestHostsValidationWarnings(t *testing.T) {
	_, prov, err := loadHostsCity(t, `[workspace]
name = "test"
[[rigs]]
name = "web"
path = "/src/web"
[hosts.idle]
transport = "ssh"
target = "idle"
root = "/r"
[hosts.norig]
transport = "ssh"
target = "norig"
root = "/r"
[[agent]]
name = "w"
dir = "web"
[agent.placement]
hosts = ["norig"]
`, nil)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	joined := strings.Join(prov.Warnings, "\n")
	for _, want := range []string{
		"hosts.idle: no agent can be placed on this host",
		`no host in its placement has a checkout of rig "web"`,
	} {
		if !strings.Contains(joined, want) {
			t.Errorf("warnings missing %q:\n%s", want, joined)
		}
	}
}

func TestHostsMergeFromFragmentAndPatch(t *testing.T) {
	cfg, prov, err := loadHostsCity(t, `include = ["hosts.toml"]
[workspace]
name = "test"
[[agent]]
name = "a"
[[patches.agent]]
name = "a"
[patches.agent.placement]
hosts = ["frag"]
`, map[string]string{"/city/hosts.toml": `
[hosts.frag]
transport = "ssh"
target = "frag"
root = "/r"
[placement]
default_hosts = ["frag", "local"]
`})
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if _, ok := cfg.Hosts["frag"]; !ok {
		t.Fatalf("fragment host not merged: %#v", cfg.Hosts)
	}
	if !slices.Equal(cfg.Placement.DefaultHosts, []string{"frag", "local"}) {
		t.Fatalf("default_hosts = %v", cfg.Placement.DefaultHosts)
	}
	for _, a := range cfg.Agents {
		if a.Name == "a" {
			if a.Placement == nil || !slices.Equal(a.Placement.Hosts, []string{"frag"}) {
				t.Fatalf("patched placement = %#v", a.Placement)
			}
		}
	}
	_ = prov
}

func TestCloneAgentPlacementIsDeep(t *testing.T) {
	src := &AgentPlacement{Hosts: []string{"a"}, RequireLabels: []string{"l"}, Prefer: []string{"a"}}
	dst := CloneAgentPlacement(src)
	dst.Hosts[0], dst.RequireLabels[0], dst.Prefer[0] = "x", "x", "x"
	if src.Hosts[0] != "a" || src.RequireLabels[0] != "l" || src.Prefer[0] != "a" {
		t.Fatalf("clone aliased source: %#v", src)
	}
	if CloneAgentPlacement(nil) != nil {
		t.Fatal("nil clone")
	}
}
