package config

import (
	"strings"
	"time"
)

// DefaultObservatoryLiveRoutingTimeout bounds the advisory M8b live-routing
// hook. It is deliberately short: the hook is fail-open, so a slow advisor must
// never delay a real route decision.
const DefaultObservatoryLiveRoutingTimeout = 2 * time.Second

// observatoryLiveRoutingFloor is the smallest timeout the hook will honor. A
// tiny timeout would make the subprocess never start and turn an operator typo
// into a permanently silent hook.
const observatoryLiveRoutingFloor = 100 * time.Millisecond

// ObservatoryConfig configures advisory agent-observatory integrations.
//
// Every integration here is opt-in and advisory: the routing path must behave
// byte-identically to a build without the integration when the corresponding
// key is unset.
type ObservatoryConfig struct {
	// LiveRouting configures the M8b advisory live-routing hook.
	LiveRouting LiveRoutingConfig `toml:"live_routing,omitempty"`
}

// LiveRoutingConfig configures the M8b advisory live-routing hook.
//
// The hook records Jev's primary_intent and the route the M7 shadow policy
// would suggest next to each routed dispatch. It never changes the route: the
// command's output is discarded and any failure or timeout is fail-open.
type LiveRoutingConfig struct {
	// Command is the operator-configured advisory command. It is run with
	// `sh -c`, receives the dispatch JSON on stdin, and its output is ignored.
	// Empty (the default) disables the hook entirely: the routing call site
	// performs no subprocess and produces no extra output.
	Command string `toml:"command,omitempty"`
	// Timeout bounds the advisory command as a duration string (e.g. "2s").
	// Defaults to 2s; values below 100ms are raised to the floor.
	Timeout string `toml:"timeout,omitempty" jsonschema:"default=2s"`
}

// Enabled reports whether the advisory live-routing hook is configured.
func (l LiveRoutingConfig) Enabled() bool {
	return strings.TrimSpace(l.Command) != ""
}

// TimeoutDuration returns the bounded timeout for the advisory command.
func (l LiveRoutingConfig) TimeoutDuration() time.Duration {
	return durationFloorOr(l.Timeout, DefaultObservatoryLiveRoutingTimeout, observatoryLiveRoutingFloor)
}
