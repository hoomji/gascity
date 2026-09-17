// Package auto provides a composite [runtime.Provider] that routes
// sessions to a default backend (typically tmux), ACP, or a configured
// per-session runtime. Sessions are registered before [Provider.Start] is called.
package auto

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/gastownhall/gascity/internal/runtime"
)

// Provider routes session operations to a default, ACP, or configured
// custom runtime backend based on per-session registration.
type Provider struct {
	defaultSP runtime.Provider
	acpSP     runtime.Provider

	mu     sync.RWMutex
	routes map[string]bool // true = ACP
	// seeded is set once SeedRoutes has loaded the route table from the
	// session beads, so a session without an ACP route is known to be default.
	seeded         bool
	custom         map[string]runtime.Backend // session name -> custom backend
	customBackends map[string]runtime.Backend // unique custom backends, keyed by label
}

var (
	_ runtime.Provider                      = (*Provider)(nil)
	_ runtime.DeadRuntimeSessionChecker     = (*Provider)(nil)
	_ runtime.InteractionProvider           = (*Provider)(nil)
	_ runtime.IdleSnapshotProvider          = (*Provider)(nil)
	_ runtime.InterruptBoundaryWaitProvider = (*Provider)(nil)
	_ runtime.InterruptedTurnResetProvider  = (*Provider)(nil)
	_ runtime.TransportCapabilityProvider   = (*Provider)(nil)
	_ runtime.RelaunchProvider              = (*Provider)(nil)
	_ runtime.LivenessObserver              = (*Provider)(nil)
	_ runtime.LivenessObserverWithError     = (*Provider)(nil)
	_ runtime.AttachmentObserverWithError   = (*Provider)(nil)
	_ runtime.SessionEventProvider          = (*Provider)(nil)
	_ runtime.BackendListingProvider        = (*Provider)(nil)
	_ runtime.BackendsProvider              = (*Provider)(nil)
	_ runtime.ListingAttestation            = (*Provider)(nil)
	_ runtime.Router                        = (*Provider)(nil)
)

// New creates a composite provider. defaultSP handles sessions not
// registered as ACP. acpSP handles sessions registered via RouteACP.
func New(defaultSP, acpSP runtime.Provider) *Provider {
	return &Provider{
		defaultSP:      defaultSP,
		acpSP:          acpSP,
		routes:         make(map[string]bool),
		custom:         make(map[string]runtime.Backend),
		customBackends: make(map[string]runtime.Backend),
	}
}

// RouteACP registers a session name to use the ACP backend.
// Must be called before Start for that session.
func (p *Provider) RouteACP(name string) {
	p.mu.Lock()
	p.routes[name] = true
	p.mu.Unlock()
}

// RouteProvider registers a session name to use a specific runtime provider.
// runtimeName is used as the stable backend label shared by sessions using the
// same configured runtime.
func (p *Provider) RouteProvider(name, runtimeName string, sp runtime.Provider) {
	if sp == nil {
		return
	}
	label := "runtime:" + strings.TrimSpace(runtimeName)
	if label == "runtime:" {
		label += name
	}

	p.mu.Lock()
	if p.custom == nil {
		p.custom = make(map[string]runtime.Backend)
	}
	if p.customBackends == nil {
		p.customBackends = make(map[string]runtime.Backend)
	}
	previous, hadPrevious := p.custom[name]
	if hadPrevious && previous.Label != label {
		delete(p.custom, name)
		p.removeCustomBackendIfUnusedLocked(previous.Label)
	}
	backend, ok := p.customBackends[label]
	if !ok || backend.Provider == nil {
		backend = runtime.Backend{Label: label, Provider: sp}
		p.customBackends[label] = backend
	}
	p.custom[name] = backend
	p.mu.Unlock()
}

func (p *Provider) removeCustomBackendIfUnusedLocked(label string) {
	for _, backend := range p.custom {
		if backend.Label == label {
			return
		}
	}
	delete(p.customBackends, label)
}

// Unroute removes a session's routing entry. Called on Stop to avoid
// leaking entries for destroyed sessions.
func (p *Provider) Unroute(name string) {
	p.mu.Lock()
	delete(p.routes, name)
	if backend, ok := p.custom[name]; ok {
		delete(p.custom, name)
		p.removeCustomBackendIfUnusedLocked(backend.Label)
	}
	p.mu.Unlock()
}

// SeedRoutes registers every name as an ACP session and marks the route
// table seeded: the caller derived names from the complete set of session
// beads, so any other session routes to the default backend. Routes already
// registered are kept.
func (p *Provider) SeedRoutes(names []string) {
	p.mu.Lock()
	for _, name := range names {
		p.routes[name] = true
	}
	p.seeded = true
	p.mu.Unlock()
}

// RouteFor implements [runtime.Router]. Explicit ACP and custom runtime routes
// are known; the default route is known only once SeedRoutes has run.
func (p *Provider) RouteFor(name string) runtime.Route {
	p.mu.RLock()
	custom := p.custom[name]
	isACP := p.routes[name]
	seeded := p.seeded
	p.mu.RUnlock()
	if custom.Provider != nil {
		return runtime.Route{Backend: custom, Known: true}
	}
	if isACP {
		if p.acpSP != nil {
			return runtime.Route{Backend: p.acpBackend(), Known: true}
		}
		return runtime.Route{Backend: p.defaultBackend(), Known: false}
	}
	return runtime.Route{Backend: p.defaultBackend(), Known: seeded}
}

func (p *Provider) defaultBackend() runtime.Backend {
	return runtime.Backend{Label: "default", Provider: p.defaultSP}
}

func (p *Provider) acpBackend() runtime.Backend {
	return runtime.Backend{Label: "acp", Provider: p.acpSP}
}

func (p *Provider) route(name string) runtime.Provider {
	return p.RouteFor(name).Provider
}

// fallbackBackends returns other configured providers to inspect when a route's
// primary backend does not confirm a session. A custom runtime may have stale
// state on the default or ACP backend, so check both when available.
func (p *Provider) fallbackBackends(name string) []runtime.Backend {
	p.mu.RLock()
	_, isCustom := p.custom[name]
	isACP := p.routes[name]
	p.mu.RUnlock()

	var fallbacks []runtime.Backend
	add := func(backend runtime.Backend) {
		if backend.Provider != nil {
			fallbacks = append(fallbacks, backend)
		}
	}
	switch {
	case isCustom:
		add(p.defaultBackend())
		if p.acpSP != nil {
			add(p.acpBackend())
		}
	case isACP:
		add(p.defaultBackend())
	default:
		if p.acpSP != nil {
			add(p.acpBackend())
		}
	}
	return fallbacks
}

// SupportsTransport reports whether this provider can route the requested
// session transport.
func (p *Provider) SupportsTransport(transport string) bool {
	if transport != "acp" {
		return true
	}
	if provider, ok := p.acpSP.(runtime.TransportCapabilityProvider); ok {
		return provider.SupportsTransport(transport)
	}
	return false
}

// DetectTransport reports the backend currently hosting the named session.
// It returns "acp" for ACP-backed sessions and "" for default or unknown.
func (p *Provider) DetectTransport(name string) string {
	if p.defaultSP != nil && p.defaultSP.IsRunning(name) {
		return ""
	}
	if p.acpSP != nil && p.acpSP.IsRunning(name) {
		return "acp"
	}
	return ""
}

// Start delegates to the routed backend.
func (p *Provider) Start(ctx context.Context, name string, cfg runtime.Config) error {
	return p.route(name).Start(ctx, name, cfg)
}

// Stop delegates to the routed backend and cleans up the route entry
// only on success. If the routed backend fails, tries the other backend
// to handle stale/missing route entries (e.g., after controller restart).
func (p *Provider) Stop(name string) error {
	primary := p.route(name)
	primaryLabel := "default"
	otherLabel := "acp"
	primaryRunning := primary.IsRunning(name)
	p.mu.RLock()
	custom := p.custom[name]
	primaryExplicitRoute := p.routes[name] || custom.Provider != nil
	p.mu.RUnlock()
	err := primary.Stop(name)
	if err == nil && primaryRunning {
		p.Unroute(name)
		return nil
	}
	// Fall through to the other backend in case the route is stale.
	var other runtime.Provider
	p.mu.RLock()
	switch {
	case custom.Provider != nil:
		primaryLabel = custom.Label
		otherLabel = "default"
		other = p.defaultSP
	case p.routes[name]:
		primaryLabel = "acp"
		otherLabel = "default"
		other = p.defaultSP
	default:
		other = p.acpSP
	}
	p.mu.RUnlock()
	if other == nil {
		if err == nil {
			p.Unroute(name)
			return nil
		}
		return err
	}
	otherRunning := other.IsRunning(name)
	if err == nil {
		if primaryExplicitRoute {
			if otherRunning {
				return fmt.Errorf("%s backend: stop succeeded without liveness confirmation while %s backend still reports the session running", primaryLabel, otherLabel)
			}
			p.Unroute(name)
			return nil
		}
		err = fmt.Errorf("%w: %q", runtime.ErrSessionNotFound, name)
	}
	otherErr := other.Stop(name)
	if otherErr == nil {
		if !otherRunning {
			otherErr = fmt.Errorf("%w: %q", runtime.ErrSessionNotFound, name)
		} else if (primaryRunning || primaryExplicitRoute) && !runtime.IsSessionGone(err) {
			return fmt.Errorf("%s backend: %w", primaryLabel, err)
		}
	}
	mergedErr := runtime.MergeBackendStopErrors(
		runtime.BackendError{Label: primaryLabel, Err: err},
		runtime.BackendError{Label: otherLabel, Err: otherErr},
	)
	if mergedErr == nil {
		p.Unroute(name)
		return nil
	}
	return mergedErr
}

// Interrupt delegates to the routed backend.
func (p *Provider) Interrupt(name string) error {
	return p.route(name).Interrupt(name)
}

// IsRunning checks the routed backend first. If it reports not running,
// falls through to other configured backends to handle stale route tables.
func (p *Provider) IsRunning(name string) bool {
	if p.route(name).IsRunning(name) {
		return true
	}
	for _, backend := range p.fallbackBackends(name) {
		if backend.Provider.IsRunning(name) {
			return true
		}
	}
	return false
}

// IsDeadRuntimeSession checks both backends for a positive dead-artifact
// report because ListRunning is also merged across both backends.
func (p *Provider) IsDeadRuntimeSession(name string) (bool, error) {
	primary := p.route(name)
	if dead, err := providerDeadRuntimeSession(primary, name); dead || err != nil {
		return dead, err
	}
	for _, backend := range p.fallbackBackends(name) {
		if dead, err := providerDeadRuntimeSession(backend.Provider, name); dead || err != nil {
			return dead, err
		}
	}
	return false, nil
}

func providerDeadRuntimeSession(sp runtime.Provider, name string) (bool, error) {
	checker, ok := sp.(runtime.DeadRuntimeSessionChecker)
	if !ok {
		return false, nil
	}
	return checker.IsDeadRuntimeSession(name)
}

// IsAttached delegates to the routed backend.
func (p *Provider) IsAttached(name string) bool {
	return p.route(name).IsAttached(name)
}

// IsAttachedWithError forwards the error-bearing attachment probe to the
// routed backend, so a probe failure is not lost behind the bool. A backend
// without the capability answers through its IsAttached with a nil error.
func (p *Provider) IsAttachedWithError(name string) (bool, error) {
	return runtime.IsAttachedWithError(p.route(name), name)
}

// Attach delegates to the routed backend. ACP sessions return an error.
func (p *Provider) Attach(name string) error {
	p.mu.RLock()
	isACP := p.routes[name]
	p.mu.RUnlock()
	if isACP {
		return fmt.Errorf("agent %q uses ACP transport (no terminal to attach to)", name)
	}
	return p.route(name).Attach(name)
}

// ProcessAlive delegates to the routed backend.
func (p *Provider) ProcessAlive(name string, processNames []string) bool {
	return p.route(name).ProcessAlive(name, processNames)
}

// ObserveLiveness delegates to the routed backend through runtime.ObserveLiveness
// so the backend's native LivenessObserver fast-path is preserved — e.g. herdr's
// agent-status liveness. Without this, wrapping a LivenessObserver backend in an
// auto router would silently collapse it to the generic IsRunning+ProcessAlive
// fold (the fragile process-table walk), reintroducing the singleton
// restart-loop for any city that also routes some sessions to ACP.
func (p *Provider) ObserveLiveness(name string, processNames []string) runtime.Liveness {
	observed := runtime.ObserveLiveness(p.route(name), name, processNames)
	if observed.Running {
		return observed
	}
	for _, backend := range p.fallbackBackends(name) {
		observed = runtime.ObserveLiveness(backend.Provider, name, processNames)
		if observed.Running {
			return observed
		}
	}
	return observed
}

// ObserveLivenessWithError preserves routed-backend observation failures. A
// confirmed absence still falls through to the other backend so stale route
// recovery matches IsRunning and ObserveLiveness without collapsing an
// unavailable primary into absence.
func (p *Provider) ObserveLivenessWithError(name string, processNames []string) (runtime.Liveness, error) {
	observed, err := runtime.ObserveLivenessWithError(p.route(name), name, processNames)
	if err != nil || observed.Running {
		return observed, err
	}
	for _, backend := range p.fallbackBackends(name) {
		observed, err = runtime.ObserveLivenessWithError(backend.Provider, name, processNames)
		if err != nil || observed.Running {
			return observed, err
		}
	}
	return observed, nil
}

// Nudge delegates to the routed backend.
func (p *Provider) Nudge(name string, content []runtime.ContentBlock) error {
	return p.route(name).Nudge(name, content)
}

// WaitForIdle delegates to the routed backend when it supports explicit
// idle-boundary waiting.
func (p *Provider) WaitForIdle(ctx context.Context, name string, timeout time.Duration) error {
	if wp, ok := p.route(name).(runtime.IdleWaitProvider); ok {
		return wp.WaitForIdle(ctx, name, timeout)
	}
	return runtime.ErrInteractionUnsupported
}

// SnapshotIdle delegates to the routed backend when it can take a
// point-in-time idle observation. Without this the composite would hide a
// tmux-backed session's SnapshotIdle from every caller as soon as any agent in
// the city selects the ACP transport, because this Provider enumerates the
// optional interfaces it forwards rather than embedding a backend.
func (p *Provider) SnapshotIdle(name string) (bool, error) {
	if sp, ok := p.route(name).(runtime.IdleSnapshotProvider); ok {
		return sp.SnapshotIdle(name)
	}
	return false, runtime.ErrInteractionUnsupported
}

// NudgeNow delegates to the routed backend when it supports immediate
// injection without an internal wait-idle step.
func (p *Provider) NudgeNow(name string, content []runtime.ContentBlock) error {
	if np, ok := p.route(name).(runtime.ImmediateNudgeProvider); ok {
		return np.NudgeNow(name, content)
	}
	return p.route(name).Nudge(name, content)
}

// ResetInterruptedTurn delegates to the routed backend when it supports
// provider-native interrupted-turn discard semantics.
func (p *Provider) ResetInterruptedTurn(ctx context.Context, name string) error {
	if rp, ok := p.route(name).(runtime.InterruptedTurnResetProvider); ok {
		return rp.ResetInterruptedTurn(ctx, name)
	}
	return runtime.ErrInteractionUnsupported
}

// Relaunch forwards a warm-box agent relaunch to the routed backend when it
// supports one, so the reconciler's RelaunchProvider type-assert is not masked
// by the auto router.
func (p *Provider) Relaunch(ctx context.Context, name string, cfg runtime.Config) error {
	if rp, ok := p.route(name).(runtime.RelaunchProvider); ok {
		return rp.Relaunch(ctx, name, cfg)
	}
	return runtime.ErrRelaunchUnsupported
}

// WaitForInterruptBoundary delegates to the routed backend when it can confirm
// a provider-native interrupt boundary before the next turn is injected.
func (p *Provider) WaitForInterruptBoundary(ctx context.Context, name string, since time.Time, timeout time.Duration) error {
	if wp, ok := p.route(name).(runtime.InterruptBoundaryWaitProvider); ok {
		return wp.WaitForInterruptBoundary(ctx, name, since, timeout)
	}
	return runtime.ErrInteractionUnsupported
}

// Pending delegates to the routed backend when it supports structured
// interactions.
func (p *Provider) Pending(name string) (*runtime.PendingInteraction, error) {
	if ip, ok := p.route(name).(runtime.InteractionProvider); ok {
		return ip.Pending(name)
	}
	return nil, runtime.ErrInteractionUnsupported
}

// Respond delegates to the routed backend when it supports structured
// interactions.
func (p *Provider) Respond(name string, response runtime.InteractionResponse) error {
	if ip, ok := p.route(name).(runtime.InteractionProvider); ok {
		return ip.Respond(name, response)
	}
	return runtime.ErrInteractionUnsupported
}

// SetMeta delegates to the routed backend.
func (p *Provider) SetMeta(name, key, value string) error {
	return p.route(name).SetMeta(name, key, value)
}

// GetMeta delegates to the routed backend.
func (p *Provider) GetMeta(name, key string) (string, error) {
	return p.route(name).GetMeta(name, key)
}

// RemoveMeta delegates to the routed backend.
func (p *Provider) RemoveMeta(name, key string) error {
	return p.route(name).RemoveMeta(name, key)
}

// Peek delegates to the routed backend.
func (p *Provider) Peek(name string, lines int) (string, error) {
	return p.route(name).Peek(name, lines)
}

// ListRunning queries both backends and returns best-effort results plus a
// partial-list error when one backend fails.
func (p *Provider) ListRunning(prefix string) ([]string, error) {
	return runtime.MergeBackendListings(p.ListRunningByBackend(prefix))
}

// ListRunningByBackend implements [runtime.BackendListingProvider]: one
// ListRunning call per backend, default first.
func (p *Provider) ListRunningByBackend(prefix string) []runtime.BackendListing {
	return runtime.ListBackends(p.Backends(), prefix)
}

// Backends implements [runtime.BackendsProvider] without listing. Custom
// runtime backends are included once per configured runtime label.
func (p *Provider) Backends() []runtime.Backend {
	backends := []runtime.Backend{p.defaultBackend()}
	if p.acpSP != nil {
		backends = append(backends, p.acpBackend())
	}

	p.mu.RLock()
	labels := make([]string, 0, len(p.customBackends))
	for label, backend := range p.customBackends {
		if backend.Provider != nil {
			labels = append(labels, label)
		}
	}
	sort.Strings(labels)
	for _, label := range labels {
		backends = append(backends, p.customBackends[label])
	}
	p.mu.RUnlock()
	return backends
}

// ListRunningComplete implements [runtime.ListingAttestation]: the merged
// listing is complete only when every configured backend attests theirs.
func (p *Provider) ListRunningComplete() bool {
	for _, backend := range p.Backends() {
		if !runtime.ListRunningAttested(backend.Provider) {
			return false
		}
	}
	return true
}

// GetLastActivity delegates to the routed backend.
func (p *Provider) GetLastActivity(name string) (time.Time, error) {
	return p.route(name).GetLastActivity(name)
}

// ClearScrollback delegates to the routed backend.
func (p *Provider) ClearScrollback(name string) error {
	return p.route(name).ClearScrollback(name)
}

// CopyTo delegates to the routed backend.
func (p *Provider) CopyTo(name, src, relDst string) error {
	return p.route(name).CopyTo(name, src, relDst)
}

// SendKeys delegates to the routed backend.
func (p *Provider) SendKeys(name string, keys ...string) error {
	return p.route(name).SendKeys(name, keys...)
}

// RunLive delegates to the routed backend.
func (p *Provider) RunLive(name string, cfg runtime.Config) error {
	return p.route(name).RunLive(name, cfg)
}

// Capabilities returns the intersection of every configured backend's
// capabilities. NeedsClaimBackstop is a need, not an ability, so it unions
// across backends.
func (p *Provider) Capabilities() runtime.ProviderCapabilities {
	backends := p.Backends()
	if len(backends) == 0 || backends[0].Provider == nil {
		return runtime.ProviderCapabilities{}
	}
	caps := backends[0].Provider.Capabilities()
	for _, backend := range backends[1:] {
		other := backend.Provider.Capabilities()
		caps.CanReportAttachment = caps.CanReportAttachment && other.CanReportAttachment
		caps.CanReportActivity = caps.CanReportActivity && other.CanReportActivity
		caps.CanStream = caps.CanStream && other.CanStream
		caps.CanAttachTTY = caps.CanAttachTTY && other.CanAttachTTY
		caps.NeedsClaimBackstop = caps.NeedsClaimBackstop || other.NeedsClaimBackstop
	}
	return caps
}

// SleepCapability reports idle sleep capability for the routed backend,
// derived from its capabilities when it does not report one itself.
func (p *Provider) SleepCapability(name string) runtime.SessionSleepCapability {
	routed := p.route(name)
	if scp, ok := routed.(runtime.SleepCapabilityProvider); ok {
		return scp.SleepCapability(name)
	}
	return runtime.SleepCapabilityFromCapabilities(routed.Capabilities())
}

// SubscribeSessionEvents forwards the session-event streams of all configured
// backends that implement runtime.SessionEventProvider. A nested composite
// without an event-capable backend is skipped; any other subscribe error fails
// the whole subscription so a backend's events are not silently lost.
func (p *Provider) SubscribeSessionEvents(ctx context.Context) (<-chan runtime.SessionEvent, error) {
	var streams []<-chan runtime.SessionEvent
	for _, backend := range p.Backends() {
		provider, ok := backend.Provider.(runtime.SessionEventProvider)
		if !ok {
			continue
		}
		stream, err := provider.SubscribeSessionEvents(ctx)
		if errors.Is(err, runtime.ErrNoSessionEventSource) {
			continue
		}
		if err != nil {
			return nil, fmt.Errorf("%s backend: %w", backend.Label, err)
		}
		streams = append(streams, stream)
	}
	switch len(streams) {
	case 0:
		return nil, runtime.ErrNoSessionEventSource
	case 1:
		return streams[0], nil
	default:
		merged := streams[0]
		for _, stream := range streams[1:] {
			merged = runtime.MergeSessionEvents(ctx, merged, stream)
		}
		return merged, nil
	}
}
