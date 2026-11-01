package ssh

import (
	"context"

	"github.com/gastownhall/gascity/internal/runtime"
)

// seamBackedProvider serves the legacy [runtime.Provider] through the
// de-conflated seams (via [runtime.NewProviderFromSeams]), passing SleepCapability
// through to the underlying *Provider. The early cut-over for the ssh provider.
//
// ExecProvider is not passed through: ssh's Exec (over the connection) is what
// the carrier drives over internally; no production caller type-asserts it.
type seamBackedProvider struct {
	runtime.Provider
	raw *Provider
}

var (
	_ runtime.Provider                = (*seamBackedProvider)(nil)
	_ runtime.SleepCapabilityProvider = (*seamBackedProvider)(nil)
	_ runtime.RelaunchProvider        = (*seamBackedProvider)(nil)
	_ runtime.FencedInputProvider     = (*seamBackedProvider)(nil)
)

// NewSeamBacked constructs an ssh provider for ep served through the seams.
func NewSeamBacked(ep Endpoint) runtime.Provider {
	raw := NewProvider(ep)
	rt, tp := raw.Seams()
	return &seamBackedProvider{Provider: runtime.NewProviderFromSeams(rt, tp), raw: raw}
}

// SleepCapability passes through to the underlying provider (non-seam).
func (s *seamBackedProvider) SleepCapability(name string) runtime.SessionSleepCapability {
	return s.raw.SleepCapability(name)
}

// Relaunch passes through to the underlying provider's warm-box relaunch
// (respawn-pane over the ssh Conn; B2, RelaunchProvider).
func (s *seamBackedProvider) Relaunch(ctx context.Context, name string, cfg runtime.Config) error {
	return s.raw.Relaunch(ctx, name, cfg)
}

// InputFencing passes through to the underlying provider (non-seam).
func (s *seamBackedProvider) InputFencing(name string) runtime.InputFencing {
	return s.raw.InputFencing(name)
}

// NudgeFenced passes through to the underlying provider's carrier guard.
func (s *seamBackedProvider) NudgeFenced(ctx context.Context, name string, content []runtime.ContentBlock, n runtime.FencedNudge) error {
	return s.raw.NudgeFenced(ctx, name, content, n)
}

// SendKeysFenced passes through to the underlying provider's carrier guard.
func (s *seamBackedProvider) SendKeysFenced(ctx context.Context, name string, fence runtime.InputFence, keys ...string) error {
	return s.raw.SendKeysFenced(ctx, name, fence, keys...)
}

// InterruptFenced passes through to the underlying provider's carrier guard.
func (s *seamBackedProvider) InterruptFenced(ctx context.Context, name string, fence runtime.InputFence) error {
	return s.raw.InterruptFenced(ctx, name, fence)
}
