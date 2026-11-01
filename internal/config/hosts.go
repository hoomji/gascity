package config

import (
	"errors"
	"fmt"
	"path/filepath"
	"slices"
	"sort"
	"strings"
)

// LocalHostName is the implicit host for the controller's own machine.
const LocalHostName = "local"

// HostTransportSSH is the only remote transport in v1.
const HostTransportSSH = "ssh"

// Host store modes.
const (
	// HostStoreLink reaches the city store through a controller-owned ssh
	// reverse forward to a unix socket under the host root (default).
	HostStoreLink = "link"
	// HostStoreDirect passes the session's store env through unchanged, for a
	// store already reachable on the network.
	HostStoreDirect = "direct"
)

// Placement version policies.
const (
	VersionPolicyExact = "exact"
	VersionPolicyMinor = "minor"
	VersionPolicyAny   = "any"
)

// HostConfig declares one machine sessions may be placed on. Parsed from
// [hosts.<name>] in city.toml. Hosts are city config, not pack config.
// See engdocs/proposals/multi-host-placement.md.
type HostConfig struct {
	// Transport reaches the host. Only "ssh" in v1; empty on "local".
	Transport string `toml:"transport,omitempty" jsonschema:"enum=ssh"`
	// Target is the ssh destination, resolved by the operator's ~/.ssh/config.
	// No key paths, users or ports live in gc config.
	Target string `toml:"target,omitempty"`
	// Root is the absolute host-side directory gc owns (city mirror, store
	// link socket, staged env).
	Root string `toml:"root,omitempty"`
	// Labels are matched against an agent's placement.require_labels.
	Labels []string `toml:"labels,omitempty"`
	// Priority orders otherwise equal hosts, higher first. Default 0.
	Priority int `toml:"priority,omitempty"`
	// MaxActiveSessions caps sessions on this host, independent of memory.
	MaxActiveSessions *int `toml:"max_active_sessions,omitempty"`
	// Store selects how sessions on this host reach the city store:
	// "link" (default) or "direct".
	Store string `toml:"store,omitempty" jsonschema:"enum=link,enum=direct"`
	// Rigs maps rig name to the absolute checkout path on this host. A rig
	// with no entry cannot be placed here.
	Rigs map[string]string `toml:"rigs,omitempty"`
	// Env controls which session env reaches this host.
	Env HostEnv `toml:"env,omitempty"`
}

// HostEnv controls session env delivery to a remote host.
type HostEnv struct {
	// Forward names secret-classified env keys this host may receive.
	// Secret values not named here are withheld from remote hosts.
	Forward []string `toml:"forward,omitempty"`
}

// IsRemote reports whether the host is reached over a transport.
func (h HostConfig) IsRemote() bool { return h.Transport != "" }

// StoreMode returns the effective store mode.
func (h HostConfig) StoreMode() string {
	if h.Store == "" {
		return HostStoreLink
	}
	return h.Store
}

// PlacementConfig holds city-wide placement settings ([placement]).
type PlacementConfig struct {
	// DefaultHosts is the host list for agents with no placement.hosts.
	// Default: ["local"].
	DefaultHosts []string `toml:"default_hosts,omitempty"`
	// VersionPolicy decides which remote gc builds may take placements:
	// "exact" (default), "minor" or "any".
	VersionPolicy string `toml:"version_policy,omitempty" jsonschema:"enum=exact,enum=minor,enum=any"`
}

// EffectiveVersionPolicy returns the version policy with its default.
func (p PlacementConfig) EffectiveVersionPolicy() string {
	if p.VersionPolicy == "" {
		return VersionPolicyExact
	}
	return p.VersionPolicy
}

// AgentPlacement restricts where an agent's sessions may run.
type AgentPlacement struct {
	// Hosts this agent may run on. Default: [placement].default_hosts, else
	// ["local"].
	Hosts []string `toml:"hosts,omitempty"`
	// RequireLabels must all be on a host for it to be eligible.
	RequireLabels []string `toml:"require_labels,omitempty"`
	// Prefer lists hosts tried first, in order, before host priority order.
	Prefer []string `toml:"prefer,omitempty"`
}

// CloneAgentPlacement deep-copies p.
func CloneAgentPlacement(p *AgentPlacement) *AgentPlacement {
	if p == nil {
		return nil
	}
	return &AgentPlacement{
		Hosts:         slices.Clone(p.Hosts),
		RequireLabels: slices.Clone(p.RequireLabels),
		Prefer:        slices.Clone(p.Prefer),
	}
}

// AgentHosts returns the hosts an agent may be placed on, applying the
// city default. The result is never empty.
func AgentHosts(cfg *City, a *Agent) []string {
	if a != nil && a.Placement != nil && len(a.Placement.Hosts) > 0 {
		return slices.Clone(a.Placement.Hosts)
	}
	if cfg != nil && len(cfg.Placement.DefaultHosts) > 0 {
		return slices.Clone(cfg.Placement.DefaultHosts)
	}
	return []string{LocalHostName}
}

// HostByName returns the named host. "local" always exists; an explicit
// [hosts.local] contributes labels, priority and max_active_sessions.
func HostByName(cfg *City, name string) (HostConfig, bool) {
	if cfg != nil {
		if h, ok := cfg.Hosts[name]; ok {
			return h, true
		}
	}
	if name == LocalHostName {
		return HostConfig{}, true
	}
	return HostConfig{}, false
}

// ValidateHosts checks [hosts], [placement] and every agent's placement
// against the composed config. It returns load errors and warnings.
func ValidateHosts(cfg *City) (warnings []string, err error) {
	if cfg == nil {
		return nil, nil
	}
	var errs []error
	rigNames := make(map[string]bool, len(cfg.Rigs))
	for _, r := range cfg.Rigs {
		rigNames[r.Name] = true
	}
	names := make([]string, 0, len(cfg.Hosts))
	for name := range cfg.Hosts {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		h := cfg.Hosts[name]
		where := fmt.Sprintf("hosts.%s", name)
		if strings.TrimSpace(name) == "" {
			errs = append(errs, errors.New("hosts: empty host name"))
			continue
		}
		if h.MaxActiveSessions != nil && *h.MaxActiveSessions < 0 {
			errs = append(errs, fmt.Errorf("%s: max_active_sessions must be >= 0", where))
		}
		if name == LocalHostName {
			if h.Transport != "" || h.Target != "" || h.Root != "" || len(h.Rigs) > 0 || h.Store != "" || len(h.Env.Forward) > 0 {
				errs = append(errs, fmt.Errorf("%s: the local host takes only labels, priority and max_active_sessions", where))
			}
			continue
		}
		switch h.Transport {
		case HostTransportSSH:
		case "":
			errs = append(errs, fmt.Errorf("%s: transport is required (\"ssh\")", where))
		default:
			errs = append(errs, fmt.Errorf("%s: unknown transport %q", where, h.Transport))
		}
		if strings.TrimSpace(h.Target) == "" {
			errs = append(errs, fmt.Errorf("%s: target is required", where))
		} else if strings.HasPrefix(strings.TrimSpace(h.Target), "-") {
			errs = append(errs, fmt.Errorf("%s: target must not start with '-'", where))
		}
		if h.Root == "" {
			errs = append(errs, fmt.Errorf("%s: root is required", where))
		} else if !filepath.IsAbs(h.Root) {
			errs = append(errs, fmt.Errorf("%s: root %q must be absolute", where, h.Root))
		}
		switch h.Store {
		case "", HostStoreLink, HostStoreDirect:
		default:
			errs = append(errs, fmt.Errorf("%s: unknown store %q", where, h.Store))
		}
		rigs := make([]string, 0, len(h.Rigs))
		for rig := range h.Rigs {
			rigs = append(rigs, rig)
		}
		sort.Strings(rigs)
		for _, rig := range rigs {
			if !rigNames[rig] {
				errs = append(errs, fmt.Errorf("%s.rigs: rig %q is not defined by the city", where, rig))
			}
			if !filepath.IsAbs(h.Rigs[rig]) {
				errs = append(errs, fmt.Errorf("%s.rigs.%s: path %q must be absolute", where, rig, h.Rigs[rig]))
			}
		}
	}

	switch cfg.Placement.VersionPolicy {
	case "", VersionPolicyExact, VersionPolicyMinor, VersionPolicyAny:
	default:
		errs = append(errs, fmt.Errorf("placement: unknown version_policy %q", cfg.Placement.VersionPolicy))
	}
	for _, name := range cfg.Placement.DefaultHosts {
		if _, ok := HostByName(cfg, name); !ok {
			errs = append(errs, fmt.Errorf("placement.default_hosts: unknown host %q", name))
		}
	}

	used := map[string]bool{}
	for i := range cfg.Agents {
		a := &cfg.Agents[i]
		hosts := AgentHosts(cfg, a)
		known := hosts[:0:0]
		for _, name := range hosts {
			if _, ok := HostByName(cfg, name); !ok {
				errs = append(errs, fmt.Errorf("agent %q: placement.hosts: unknown host %q", a.QualifiedName(), name))
				continue
			}
			known = append(known, name)
			used[name] = true
		}
		if a.Placement != nil {
			for _, name := range a.Placement.Prefer {
				if !slices.Contains(hosts, name) {
					errs = append(errs, fmt.Errorf("agent %q: placement.prefer: host %q is not in placement.hosts", a.QualifiedName(), name))
				}
			}
		}
		if a.Session == "acp" {
			for _, name := range known {
				if h, _ := HostByName(cfg, name); h.IsRemote() {
					errs = append(errs, fmt.Errorf("agent %q: ACP agents are local-only; placement names remote host %q", a.QualifiedName(), name))
				}
			}
		}
		if a.Dir != "" && rigNames[a.Dir] {
			reachable := false
			for _, name := range known {
				h, _ := HostByName(cfg, name)
				if !h.IsRemote() || h.Rigs[a.Dir] != "" {
					reachable = true
					break
				}
			}
			if !reachable && len(known) > 0 {
				warnings = append(warnings, fmt.Sprintf("agent %q: no host in its placement has a checkout of rig %q", a.QualifiedName(), a.Dir))
			}
		}
	}
	for _, name := range names {
		if name != LocalHostName && !used[name] {
			warnings = append(warnings, fmt.Sprintf("hosts.%s: no agent can be placed on this host", name))
		}
	}
	return warnings, errors.Join(errs...)
}

// mergeHosts adds fragment hosts to base, warning on redefinition.
func mergeHosts(base, fragment *City, fragPath string, prov *Provenance) {
	if len(fragment.Hosts) == 0 {
		return
	}
	if base.Hosts == nil {
		base.Hosts = make(map[string]HostConfig, len(fragment.Hosts))
	}
	for name, h := range fragment.Hosts {
		if _, exists := base.Hosts[name]; exists && prov != nil {
			prov.Warnings = append(prov.Warnings, fmt.Sprintf("host %q redefined by %q", name, fragPath))
		}
		base.Hosts[name] = h
	}
}
