// Package placement provides a composite [runtime.Provider] that serves each
// session from the backend of the host it was placed on. It generalises
// package hybrid from two legs chosen by name to N legs chosen by a route
// table: "local" (the city's base provider) plus one leg per remote host.
//
// Routes come from session beads (gc.host), seeded on every snapshot and set
// at create and launch. Before the table is seeded, a session with no
// explicit route is unknown: every verb on it answers through the
// unavailable leg ([runtime.ErrRuntimeUnavailable]), never "not found". Once
// seeded, a session with no explicit route is local, because a session bead
// without gc.host is local.
//
// See engdocs/proposals/multi-host-placement.md.
package placement

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"sync"

	"github.com/gastownhall/gascity/internal/runtime"
)

// LocalHost is the label of the controller's own backend.
const LocalHost = "local"

// UnavailableLabel is the route label of a session whose host is unknown.
const UnavailableLabel = "unavailable"

// ErrUnknownHost reports a route to a host the provider has no leg for.
var ErrUnknownHost = errors.New("placement: unknown host")

// Projector rewrites a start config for a host: host paths, store endpoint,
// gc binary, withheld secrets. It runs before Start, Relaunch and RunLive.
type Projector func(name string, cfg runtime.Config) (runtime.Config, error)

// Leg is one remote host's backend.
type Leg struct {
	// Host is the host name, also the route label.
	Host string
	// Provider serves sessions placed on the host.
	Provider runtime.Provider
	// Project, when set, rewrites start configs for the host.
	Project Projector
}

// Provider routes session operations to a host's backend.
type Provider struct {
	local runtime.Provider
	legs  map[string]Leg
	order []string // remote host names, sorted

	mu     sync.RWMutex
	routes map[string]string
	seeded bool
}

var (
	_ runtime.Provider                        = (*Provider)(nil)
	_ runtime.DeadRuntimeSessionChecker       = (*Provider)(nil)
	_ runtime.InteractionProvider             = (*Provider)(nil)
	_ runtime.IdleSnapshotProvider            = (*Provider)(nil)
	_ runtime.InterruptBoundaryWaitProvider   = (*Provider)(nil)
	_ runtime.TurnInterruptCapabilityProvider = (*Provider)(nil)
	_ runtime.InterruptedTurnResetProvider    = (*Provider)(nil)
	_ runtime.InputClearProvider              = (*Provider)(nil)
	_ runtime.BackgroundWorkStopper           = (*Provider)(nil)
	_ runtime.RelaunchProvider                = (*Provider)(nil)
	_ runtime.LivenessObserver                = (*Provider)(nil)
	_ runtime.UnattendedSessionStopper        = (*Provider)(nil)
	_ runtime.LivenessObserverWithError       = (*Provider)(nil)
	_ runtime.AttachmentObserverWithError     = (*Provider)(nil)
	_ runtime.SessionEventProvider            = (*Provider)(nil)
	_ runtime.BackendListingProvider          = (*Provider)(nil)
	_ runtime.BackendsProvider                = (*Provider)(nil)
	_ runtime.ListingAttestation              = (*Provider)(nil)
	_ runtime.Router                          = (*Provider)(nil)
	_ runtime.ServerDeathConfirmer            = (*Provider)(nil)
	_ runtime.DialogProvider                  = (*Provider)(nil)
	_ runtime.EnvironmentBatchProvider        = (*Provider)(nil)
	_ runtime.ServerLifecycleProvider         = (*Provider)(nil)
	_ runtime.SessionRosterProvider           = (*Provider)(nil)
	_ runtime.SessionObjectKiller             = (*Provider)(nil)
	_ runtime.TurnEventProvider               = (*Provider)(nil)
	_ runtime.TurnFenceProvider               = (*Provider)(nil)
	_ runtime.ProcessTableScanner             = (*Provider)(nil)
	_ runtime.ConditionalProcessTableScanner  = (*Provider)(nil)
)

// New creates a placement provider over the local backend and one leg per
// remote host. A leg named "local", empty, nil or duplicated is an error.
func New(local runtime.Provider, legs ...Leg) (*Provider, error) {
	if local == nil {
		return nil, errors.New("placement: nil local provider")
	}
	p := &Provider{local: local, legs: make(map[string]Leg, len(legs)), routes: map[string]string{}}
	for _, leg := range legs {
		switch {
		case leg.Host == "" || leg.Host == LocalHost || leg.Host == UnavailableLabel:
			return nil, fmt.Errorf("placement: invalid host name %q", leg.Host)
		case leg.Provider == nil:
			return nil, fmt.Errorf("placement: host %q has no provider", leg.Host)
		}
		if _, dup := p.legs[leg.Host]; dup {
			return nil, fmt.Errorf("placement: host %q declared twice", leg.Host)
		}
		p.legs[leg.Host] = leg
		p.order = append(p.order, leg.Host)
	}
	sort.Strings(p.order)
	return p, nil
}

// RouteHost records that name runs on host. Routing to "local" is explicit
// too, so a local session is known before the table is seeded.
func (p *Provider) RouteHost(name, host string) error {
	if host != LocalHost {
		if _, ok := p.legs[host]; !ok {
			return fmt.Errorf("%w %q for session %q", ErrUnknownHost, host, name)
		}
	}
	p.mu.Lock()
	p.routes[name] = host
	p.mu.Unlock()
	return nil
}

// Unroute forgets name's explicit route.
func (p *Provider) Unroute(name string) {
	p.mu.Lock()
	delete(p.routes, name)
	p.mu.Unlock()
}

// SeedRoutes records the host of every session bead and marks the table
// seeded: the caller derived hosts from the complete set of session beads,
// so a session absent from it is local. Hosts the provider has no leg for
// are skipped and returned, so their sessions stay unavailable rather than
// being mistaken for local ones. Seeding replaces earlier routes.
func (p *Provider) SeedRoutes(hosts map[string]string) (unknown []string) {
	routes := make(map[string]string, len(hosts))
	for name, host := range hosts {
		if host == "" {
			host = LocalHost
		}
		if host != LocalHost {
			if _, ok := p.legs[host]; !ok {
				unknown = append(unknown, name)
				routes[name] = UnavailableLabel
				continue
			}
		}
		routes[name] = host
	}
	sort.Strings(unknown)
	p.mu.Lock()
	p.routes = routes
	p.seeded = true
	p.mu.Unlock()
	return unknown
}

// HostOf returns name's host and whether it is known.
func (p *Provider) HostOf(name string) (string, bool) {
	r := p.RouteFor(name)
	return r.Label, r.Known
}

// RouteFor implements [runtime.Router].
func (p *Provider) RouteFor(name string) runtime.Route {
	p.mu.RLock()
	host, explicit := p.routes[name]
	seeded := p.seeded
	p.mu.RUnlock()
	switch {
	case explicit && host == UnavailableLabel:
		return runtime.Route{Backend: unavailableBackend(), Known: false}
	case explicit && host == LocalHost:
		return runtime.Route{Backend: p.localBackend(), Known: true}
	case explicit:
		return runtime.Route{Backend: runtime.Backend{Label: host, Provider: p.legs[host].Provider}, Known: true}
	case seeded:
		return runtime.Route{Backend: p.localBackend(), Known: true}
	default:
		return runtime.Route{Backend: unavailableBackend(), Known: false}
	}
}

func (p *Provider) localBackend() runtime.Backend {
	return runtime.Backend{Label: LocalHost, Provider: p.local}
}

func unavailableBackend() runtime.Backend {
	return runtime.Backend{Label: UnavailableLabel, Provider: unavailable{}}
}

func (p *Provider) route(name string) runtime.Provider {
	return p.RouteFor(name).Provider
}

// projected returns name's backend and cfg rewritten for its host.
func (p *Provider) projected(name string, cfg runtime.Config) (runtime.Provider, runtime.Config, error) {
	r := p.RouteFor(name)
	if leg, ok := p.legs[r.Label]; ok && leg.Project != nil {
		out, err := leg.Project(name, cfg)
		if err != nil {
			return nil, cfg, fmt.Errorf("placement: projecting %q for host %q: %w", name, r.Label, err)
		}
		return r.Provider, out, nil
	}
	return r.Provider, cfg, nil
}

// StopUnattendedSession forwards the bound unattended stop only to the backend
// name routes to. It never falls through to another backend.
func (p *Provider) StopUnattendedSession(name, expectedToken string) error {
	r := p.RouteFor(name)
	stopper, ok := r.Provider.(runtime.UnattendedSessionStopper)
	if !ok {
		return fmt.Errorf("placement %s backend does not support unattended-session stop for %q", r.Label, name)
	}
	if err := stopper.StopUnattendedSession(name, expectedToken); err != nil {
		return fmt.Errorf("placement %s backend stopping unattended session %q: %w", r.Label, name, err)
	}
	return nil
}

// Backends implements [runtime.BackendsProvider]: local first, then remote
// hosts by name.
func (p *Provider) Backends() []runtime.Backend {
	out := make([]runtime.Backend, 0, 1+len(p.order))
	out = append(out, p.localBackend())
	for _, host := range p.order {
		out = append(out, runtime.Backend{Label: host, Provider: p.legs[host].Provider})
	}
	return out
}

func (p *Provider) providers() []runtime.Provider {
	backends := p.Backends()
	out := make([]runtime.Provider, len(backends))
	for i, b := range backends {
		out[i] = b.Provider
	}
	return out
}

// ServerConfirmedDead forwards to the backends that confirm server death.
func (p *Provider) ServerConfirmedDead() bool {
	return runtime.ServersConfirmedDead(p.providers()...)
}

// ListRunningComplete implements [runtime.ListingAttestation]: the merged
// listing is complete only when every backend attests its own.
func (p *Provider) ListRunningComplete() bool {
	for _, sp := range p.providers() {
		if !runtime.ListRunningAttested(sp) {
			return false
		}
	}
	return true
}

// Capabilities returns the intersection of every backend's capabilities.
// NeedsClaimBackstop is a need, not an ability, so it unions.
func (p *Provider) Capabilities() runtime.ProviderCapabilities {
	out := p.local.Capabilities()
	for _, host := range p.order {
		c := p.legs[host].Provider.Capabilities()
		out.CanReportAttachment = out.CanReportAttachment && c.CanReportAttachment
		out.CanReportActivity = out.CanReportActivity && c.CanReportActivity
		out.CanStream = out.CanStream && c.CanStream
		out.CanAttachTTY = out.CanAttachTTY && c.CanAttachTTY
		out.NeedsClaimBackstop = out.NeedsClaimBackstop || c.NeedsClaimBackstop
	}
	return out
}

// SubscribeSessionEvents merges the session-event streams of every backend
// that publishes them, with the same rules as
// [runtime.SubscribeSessionEventSources].
func (p *Provider) SubscribeSessionEvents(ctx context.Context) (<-chan runtime.SessionEvent, error) {
	var merged <-chan runtime.SessionEvent
	for _, b := range p.Backends() {
		sep, ok := b.Provider.(runtime.SessionEventProvider)
		if !ok {
			continue
		}
		ch, err := sep.SubscribeSessionEvents(ctx)
		if errors.Is(err, runtime.ErrNoSessionEventSource) {
			continue
		}
		if err != nil {
			return nil, fmt.Errorf("%s backend: %w", b.Label, err)
		}
		if merged == nil {
			merged = ch
		} else {
			merged = runtime.MergeSessionEvents(ctx, merged, ch)
		}
	}
	if merged == nil {
		return nil, fmt.Errorf("%w (placement)", runtime.ErrNoSessionEventSource)
	}
	return merged, nil
}

// SubscribeTurnEvents merges the turn-event streams of every backend that
// publishes them, as SubscribeSessionEvents does.
func (p *Provider) SubscribeTurnEvents(ctx context.Context) (<-chan runtime.TurnEvent, error) {
	var merged <-chan runtime.TurnEvent
	for _, b := range p.Backends() {
		tep, ok := b.Provider.(runtime.TurnEventProvider)
		if !ok {
			continue
		}
		ch, err := tep.SubscribeTurnEvents(ctx)
		if errors.Is(err, runtime.ErrNoTurnEventSource) {
			continue
		}
		if err != nil {
			return nil, fmt.Errorf("%s backend: %w", b.Label, err)
		}
		if merged == nil {
			merged = ch
		} else {
			merged = runtime.MergeTurnEvents(ctx, merged, ch)
		}
	}
	if merged == nil {
		return nil, fmt.Errorf("%w (placement)", runtime.ErrNoTurnEventSource)
	}
	return merged, nil
}

// The process-table scanner is the local backend's alone: a remote host's
// runtimes are not in this host's process table.

// CanScanProcessTable implements [runtime.ConditionalProcessTableScanner].
func (p *Provider) CanScanProcessTable() bool {
	_, ok := runtime.AsProcessTableScanner(p.local)
	return ok
}

// FindRuntimesBySessionID implements [runtime.ProcessTableScanner] with the
// local backend's scan.
func (p *Provider) FindRuntimesBySessionID(id string) ([]runtime.LiveRuntime, error) {
	scanner, ok := runtime.AsProcessTableScanner(p.local)
	if !ok {
		return nil, nil
	}
	return scanner.FindRuntimesBySessionID(id)
}

// TerminateRuntime implements [runtime.ProcessTableScanner] with the local
// backend's scanner.
func (p *Provider) TerminateRuntime(r runtime.LiveRuntime) error {
	scanner, ok := runtime.AsProcessTableScanner(p.local)
	if !ok {
		return fmt.Errorf("placement: no backend can terminate runtime PID %d for session %s", r.PID, r.SessionID)
	}
	return scanner.TerminateRuntime(r)
}
