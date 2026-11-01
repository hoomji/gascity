package config

import (
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"
)

// ResourceMemoryAuto is the [resources] memory value that budgets the host's
// total physical memory.
const ResourceMemoryAuto = "auto"

// ResourcesConfig is the [resources] table: a per-host memory budget and the
// resource classes agents reserve against it. An empty table disables
// resource admission entirely (today's behaviour).
type ResourcesConfig struct {
	// Memory is the memory gc may hand out as session reservations on this
	// host: a size such as "48GiB" or "1.5GB", or "auto" for the host's total
	// physical memory. Empty means no memory budget.
	Memory string `toml:"memory,omitempty"`
	// Reserve is held back from Memory for the OS, the controller and
	// anything not started by gc. Defaults to 0.
	Reserve string `toml:"reserve,omitempty"`
	// DefaultClass is the class of agents that set no resource_class. Empty
	// means such agents reserve nothing and are never deferred.
	DefaultClass string `toml:"default_class,omitempty"`
	// Classes maps a class name to its reservation.
	Classes map[string]ResourceClass `toml:"classes,omitempty"`
}

// ResourceClass declares what one session of an agent in this class
// reserves.
type ResourceClass struct {
	// Memory is the memory one session of this class reserves against the
	// host budget, as a size such as "8GiB".
	Memory string `toml:"memory,omitempty"`
	// MaxActiveSessions caps how many sessions of this class may be resident
	// on this host at once. Nil means no class cap; 0 means the class never
	// starts on this host.
	MaxActiveSessions *int `toml:"max_active_sessions,omitempty"`
}

// Enabled reports whether the table configures any admission at all.
func (r ResourcesConfig) Enabled() bool {
	return strings.TrimSpace(r.Memory) != "" || len(r.Classes) > 0
}

// ClassNames returns the defined class names in sorted order.
func (r ResourcesConfig) ClassNames() []string {
	names := make([]string, 0, len(r.Classes))
	for name := range r.Classes {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

// EffectiveResourceClass returns the class an agent reserves against:
// its own resource_class, else the table's default_class, else "".
func (r ResourcesConfig) EffectiveResourceClass(a *Agent) string {
	if a != nil {
		if c := strings.TrimSpace(a.ResourceClass); c != "" {
			return c
		}
	}
	return strings.TrimSpace(r.DefaultClass)
}

// ParseMemorySize parses a memory size such as "8GiB", "1.5 GB", "512M" or
// "1048576" into bytes. All unit suffixes are binary multiples (K = 1024),
// matching the doctor size knobs. A decimal fraction is allowed.
func ParseMemorySize(s string) (int64, error) {
	orig := s
	s = strings.TrimSpace(strings.ToUpper(s))
	if s == "" {
		return 0, fmt.Errorf("empty size")
	}
	units := []struct {
		suffix string
		mult   float64
	}{
		{"TIB", 1 << 40}, {"GIB", 1 << 30}, {"MIB", 1 << 20}, {"KIB", 1 << 10},
		{"TB", 1 << 40}, {"GB", 1 << 30}, {"MB", 1 << 20}, {"KB", 1 << 10},
		{"T", 1 << 40}, {"G", 1 << 30}, {"M", 1 << 20}, {"K", 1 << 10},
		{"B", 1},
	}
	mult := 1.0
	for _, u := range units {
		if strings.HasSuffix(s, u.suffix) {
			mult = u.mult
			s = strings.TrimSpace(strings.TrimSuffix(s, u.suffix))
			break
		}
	}
	n, err := strconv.ParseFloat(s, 64)
	if err != nil || math.IsNaN(n) || math.IsInf(n, 0) {
		return 0, fmt.Errorf("invalid size %q", orig)
	}
	if n < 0 {
		return 0, fmt.Errorf("negative size %q", orig)
	}
	bytes := n * mult
	if bytes > math.MaxInt64 {
		return 0, fmt.Errorf("size %q overflows", orig)
	}
	return int64(bytes), nil
}

// ResourceClassBytes returns the parsed reservation of a class (0 when the
// class sets no memory). Callers should run ValidateResources first; an
// unparseable value reads as 0.
func (c ResourceClass) ResourceClassBytes() int64 {
	if strings.TrimSpace(c.Memory) == "" {
		return 0
	}
	n, err := ParseMemorySize(c.Memory)
	if err != nil {
		return 0
	}
	return n
}

// ReserveBytes returns the parsed reserve (0 when unset or invalid).
func (r ResourcesConfig) ReserveBytes() int64 {
	if strings.TrimSpace(r.Reserve) == "" {
		return 0
	}
	n, err := ParseMemorySize(r.Reserve)
	if err != nil {
		return 0
	}
	return n
}

// MemoryIsAuto reports whether the budget is the host's physical memory.
func (r ResourcesConfig) MemoryIsAuto() bool {
	return strings.EqualFold(strings.TrimSpace(r.Memory), ResourceMemoryAuto)
}

// ValidateResources checks the [resources] table and agent resource_class
// references. It returns load warnings and a hard error for configurations
// that cannot be admitted meaningfully.
func ValidateResources(cfg *City, source string) ([]string, error) {
	if cfg == nil {
		return nil, nil
	}
	r := cfg.Resources
	var warnings []string
	if !r.Enabled() {
		if strings.TrimSpace(r.Reserve) != "" || strings.TrimSpace(r.DefaultClass) != "" {
			return nil, fmt.Errorf("%s: [resources]: reserve and default_class need memory or classes", source)
		}
		for _, a := range cfg.Agents {
			if strings.TrimSpace(a.ResourceClass) != "" {
				warnings = append(warnings, fmt.Sprintf(
					"%s: agent %q: resource_class %q has no effect without a [resources] table",
					source, a.QualifiedName(), a.ResourceClass))
			}
		}
		return warnings, nil
	}

	var capacity int64 = -1
	if mem := strings.TrimSpace(r.Memory); mem != "" && !r.MemoryIsAuto() {
		n, err := ParseMemorySize(mem)
		if err != nil {
			return nil, fmt.Errorf("%s: [resources] memory: %w", source, err)
		}
		if n == 0 {
			return nil, fmt.Errorf("%s: [resources] memory must be greater than zero", source)
		}
		capacity = n
	}
	if res := strings.TrimSpace(r.Reserve); res != "" {
		n, err := ParseMemorySize(res)
		if err != nil {
			return nil, fmt.Errorf("%s: [resources] reserve: %w", source, err)
		}
		if capacity >= 0 {
			if n >= capacity {
				warnings = append(warnings, fmt.Sprintf(
					"%s: [resources] reserve %s is not below memory %s; no class with a memory reservation can start",
					source, r.Reserve, r.Memory))
			}
			capacity -= n
		}
	}
	for _, name := range r.ClassNames() {
		class := r.Classes[name]
		if strings.TrimSpace(name) == "" {
			return nil, fmt.Errorf("%s: [resources.classes]: empty class name", source)
		}
		if strings.TrimSpace(class.Memory) == "" && class.MaxActiveSessions == nil {
			return nil, fmt.Errorf("%s: [resources.classes.%s]: set memory, max_active_sessions, or both", source, name)
		}
		if strings.TrimSpace(class.Memory) != "" {
			if _, err := ParseMemorySize(class.Memory); err != nil {
				return nil, fmt.Errorf("%s: [resources.classes.%s] memory: %w", source, name, err)
			}
		}
		if class.MaxActiveSessions != nil && *class.MaxActiveSessions < 0 {
			return nil, fmt.Errorf("%s: [resources.classes.%s] max_active_sessions must be >= 0", source, name)
		}
	}
	if dc := strings.TrimSpace(r.DefaultClass); dc != "" {
		if _, ok := r.Classes[dc]; !ok {
			return nil, fmt.Errorf("%s: [resources] default_class %q is not defined in [resources.classes]", source, dc)
		}
	}
	used := make(map[string][]string)
	for i := range cfg.Agents {
		a := &cfg.Agents[i]
		name := r.EffectiveResourceClass(a)
		if name == "" {
			continue
		}
		if _, ok := r.Classes[name]; !ok {
			return nil, fmt.Errorf("%s: agent %q: resource_class %q is not defined in [resources.classes]",
				source, a.QualifiedName(), name)
		}
		used[name] = append(used[name], a.QualifiedName())
	}
	for _, name := range r.ClassNames() {
		agents := used[name]
		if len(agents) == 0 {
			continue
		}
		class := r.Classes[name]
		switch {
		case class.MaxActiveSessions != nil && *class.MaxActiveSessions == 0:
			warnings = append(warnings, fmt.Sprintf(
				"%s: [resources.classes.%s] max_active_sessions = 0: agents %s never start on this host",
				source, name, strings.Join(agents, ", ")))
		case capacity >= 0 && class.ResourceClassBytes() > capacity:
			warnings = append(warnings, fmt.Sprintf(
				"%s: [resources.classes.%s] memory %s exceeds the budget left after reserve: agents %s never start on this host",
				source, name, class.Memory, strings.Join(agents, ", ")))
		}
	}
	return warnings, nil
}
