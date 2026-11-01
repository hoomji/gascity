package ssh

import (
	"context"
	"errors"
	"fmt"

	"github.com/gastownhall/gascity/internal/runtime"
)

var _ runtime.FencedInputProvider = (*Provider)(nil)

// InputFencing reports atomic: every session this provider starts is a tmux
// session on the box whose agent pane carries the incarnation's fence
// (published by Start and Relaunch), and fenced input runs inside the box
// tmux carrier's guard on that pane (RUNTIME-CONTRACT-009), the same guard
// exec boxes use. It is a static trait.
func (p *Provider) InputFencing(string) runtime.InputFencing {
	return runtime.InputFencingAtomic
}

// fencedCarrier resolves the session's agent pane for one fenced operation.
// A box tmux that answers with no such session (or no server) is an absent
// miss; a connection that does not answer is ErrRuntimeUnavailable, never a
// miss, since it cannot tell absence from an outage.
func (p *Provider) fencedCarrier(ctx context.Context, name string) (runtime.Carrier, error) {
	if err := checkName(name); err != nil {
		return nil, err
	}
	c, err := p.carrier(ctx, name)
	switch {
	case err == nil:
		return c, nil
	case errors.Is(err, runtime.ErrSessionNotFound):
		return nil, &runtime.FenceMismatchError{Session: name, Found: runtime.FenceFoundAbsent}
	default:
		return nil, fmt.Errorf("%w: ssh session %q: %v", runtime.ErrRuntimeUnavailable, name, err)
	}
}

// boxOccupancy reports whether the box holds session name, and whether that
// answer is definite: tmux has-session exit 0 is present, exit 1 (no such
// session, or no server) is absent, and anything else, including a
// connection that did not answer, is not definite.
func (p *Provider) boxOccupancy(ctx context.Context, name string) (occupied, definite bool) {
	_, code, err := p.tmux(ctx, name, "has-session", "-t", sessionTarget(name))
	switch {
	case err != nil:
		return false, false
	case code == 0:
		return true, true
	case code == 1:
		return false, true
	default:
		return false, false
	}
}

// fencedResult turns a carrier error the box did not answer into an absent
// miss when has-session definitely finds no session, keeping its partial
// flag; every other answer is returned as is.
func (p *Provider) fencedResult(ctx context.Context, name string, err error) error {
	var unread *runtime.CarrierFenceUnreadError
	if !errors.As(err, &unread) {
		return err
	}
	if occupied, definite := p.boxOccupancy(ctx, name); definite && !occupied {
		return &runtime.FenceMismatchError{Session: name, Found: runtime.FenceFoundAbsent, Partial: unread.Partial}
	}
	return err
}

// NudgeFenced delivers content as Nudge does, only to the incarnation n.Fence
// names. The ssh provider has no idle wait and does not deduplicate, so
// n.Immediate and n.DeliveryKey change nothing.
func (p *Provider) NudgeFenced(ctx context.Context, name string, content []runtime.ContentBlock, n runtime.FencedNudge) error {
	c, err := p.fencedCarrier(ctx, name)
	if err != nil {
		return err
	}
	return p.fencedResult(ctx, name, c.NudgeFenced(ctx, name, content, n.Fence))
}

// SendKeysFenced sends keys as SendKeys does, only to fence's incarnation.
func (p *Provider) SendKeysFenced(ctx context.Context, name string, fence runtime.InputFence, keys ...string) error {
	c, err := p.fencedCarrier(ctx, name)
	if err != nil {
		return err
	}
	return p.fencedResult(ctx, name, c.SendKeysFenced(ctx, name, fence, keys...))
}

// InterruptFenced interrupts as Interrupt does, only fence's incarnation.
func (p *Provider) InterruptFenced(ctx context.Context, name string, fence runtime.InputFence) error {
	c, err := p.fencedCarrier(ctx, name)
	if err != nil {
		return err
	}
	return p.fencedResult(ctx, name, c.InterruptFenced(ctx, name, fence))
}

// publishInputFence records the incarnation cfg.Env names on the agent pane
// Start just created. A pane left without its fence would be unproven to
// every fenced operation, so a failed publish fails the start. An environment
// that names no incarnation publishes nothing.
func (p *Provider) publishInputFence(ctx context.Context, name string, cfg runtime.Config) error {
	f, ok := runtime.InputFenceFromEnv(cfg.Env)
	if !ok {
		return nil
	}
	c, err := p.carrier(ctx, name)
	if err != nil {
		return fmt.Errorf("publishing input fence: %w", err)
	}
	if err := c.PublishInputFence(ctx, name, f); err != nil {
		return fmt.Errorf("publishing input fence: %w", err)
	}
	return nil
}

// launchFenceArgs is the tmux command, joined to a respawn-pane as one
// command list, that sets the respawned agent pane's fence to the
// incarnation cfg.Env names, or clears it when cfg.Env names none: a
// respawned pane keeps its options, so its predecessor's fence must never
// name the new agent.
func launchFenceArgs(target string, cfg runtime.Config) []string {
	if f, ok := runtime.InputFenceFromEnv(cfg.Env); ok {
		return append([]string{";"}, runtime.TmuxPublishInputFenceArgs(target, f)...)
	}
	return append([]string{";"}, runtime.TmuxClearInputFenceArgs(target)...)
}
