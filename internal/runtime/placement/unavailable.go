package placement

import (
	"context"
	"fmt"
	"time"

	"github.com/gastownhall/gascity/internal/runtime"
)

// unavailable is the leg of a session whose host is not known. Every verb
// fails with [runtime.ErrRuntimeUnavailable]. The error-bearing liveness and
// attachment observers fail too, so a caller that can tell "unknown" from
// "stopped" never reads an unrouted session as dead. Only the bool-only
// probes of the core interface (IsRunning, IsAttached, ProcessAlive) can
// answer false, as every provider's do on a failed observation.
type unavailable struct{}

var (
	_ runtime.Provider                    = unavailable{}
	_ runtime.LivenessObserverWithError   = unavailable{}
	_ runtime.AttachmentObserverWithError = unavailable{}
	_ runtime.DeadRuntimeSessionChecker   = unavailable{}
)

func errUnrouted(name string) error {
	return fmt.Errorf("%w: session %q has no known host", runtime.ErrRuntimeUnavailable, name)
}

func (unavailable) Start(_ context.Context, name string, _ runtime.Config) error {
	return errUnrouted(name)
}
func (unavailable) Stop(name string) error                  { return errUnrouted(name) }
func (unavailable) Interrupt(name string) error             { return errUnrouted(name) }
func (unavailable) IsRunning(string) bool                   { return false }
func (unavailable) IsAttached(string) bool                  { return false }
func (unavailable) Attach(name string) error                { return errUnrouted(name) }
func (unavailable) ProcessAlive(string, []string) bool      { return false }
func (unavailable) SetMeta(name, _, _ string) error         { return errUnrouted(name) }
func (unavailable) GetMeta(name, _ string) (string, error)  { return "", errUnrouted(name) }
func (unavailable) RemoveMeta(name, _ string) error         { return errUnrouted(name) }
func (unavailable) Peek(name string, _ int) (string, error) { return "", errUnrouted(name) }
func (unavailable) ClearScrollback(name string) error       { return errUnrouted(name) }
func (unavailable) CopyTo(name, _, _ string) error          { return errUnrouted(name) }
func (unavailable) SendKeys(name string, _ ...string) error { return errUnrouted(name) }
func (unavailable) RunLive(name string, _ runtime.Config) error {
	return errUnrouted(name)
}

func (unavailable) Nudge(name string, _ []runtime.ContentBlock) error {
	return errUnrouted(name)
}

func (unavailable) GetLastActivity(name string) (time.Time, error) {
	return time.Time{}, errUnrouted(name)
}

func (unavailable) ListRunning(string) ([]string, error) {
	return nil, fmt.Errorf("%w: unrouted leg has no listing", runtime.ErrRuntimeUnavailable)
}

func (unavailable) Capabilities() runtime.ProviderCapabilities {
	return runtime.ProviderCapabilities{}
}

func (unavailable) ObserveLivenessWithError(name string, _ []string) (runtime.Liveness, error) {
	return runtime.Liveness{}, errUnrouted(name)
}

func (unavailable) IsAttachedWithError(name string) (bool, error) {
	return false, errUnrouted(name)
}

func (unavailable) IsDeadRuntimeSession(name string) (bool, error) {
	return false, errUnrouted(name)
}
