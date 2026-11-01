package placement

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"sync/atomic"
	"testing"

	"github.com/gastownhall/gascity/internal/runtime"
	"github.com/gastownhall/gascity/internal/runtime/runtimetest"
)

func newTestProvider(t *testing.T, legs ...Leg) (*Provider, *runtime.Fake) {
	t.Helper()
	local := runtime.NewFake()
	p, err := New(local, legs...)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return p, local
}

// A seeded placement provider behaves as a provider for sessions routed
// nowhere in particular (they are local).
func TestPlacementConformance(t *testing.T) {
	var counter int64
	runtimetest.RunProviderTests(t, func(t *testing.T) (runtime.Provider, runtime.Config, string) {
		p, _ := newTestProvider(t, Leg{Host: "host-a", Provider: runtime.NewFake()})
		p.SeedRoutes(nil)
		return p, runtime.Config{}, fmt.Sprintf("placement-conform-%d", atomic.AddInt64(&counter, 1))
	})
}

// Kills: an unseeded unrouted session defaulting to local (it may be running
// on a remote host the table has not learned yet) or answering "not running".
func TestUnseededSessionIsUnavailable(t *testing.T) {
	p, local := newTestProvider(t, Leg{Host: "host-a", Provider: runtime.NewFake()})
	r := p.RouteFor("s1")
	if r.Known || r.Label != UnavailableLabel {
		t.Fatalf("RouteFor before seed = %+v, want unknown unavailable", r)
	}
	if err := p.Start(context.Background(), "s1", runtime.Config{}); !errors.Is(err, runtime.ErrRuntimeUnavailable) {
		t.Fatalf("Start = %v, want ErrRuntimeUnavailable", err)
	}
	if err := p.Stop("s1"); !errors.Is(err, runtime.ErrRuntimeUnavailable) {
		t.Fatalf("Stop = %v, want ErrRuntimeUnavailable", err)
	}
	if _, err := p.ObserveLivenessWithError("s1", nil); !errors.Is(err, runtime.ErrRuntimeUnavailable) {
		t.Fatalf("ObserveLivenessWithError = %v, want ErrRuntimeUnavailable", err)
	}
	if _, err := p.IsAttachedWithError("s1"); !errors.Is(err, runtime.ErrRuntimeUnavailable) {
		t.Fatalf("IsAttachedWithError = %v, want ErrRuntimeUnavailable", err)
	}
	if _, err := p.IsDeadRuntimeSession("s1"); !errors.Is(err, runtime.ErrRuntimeUnavailable) {
		t.Fatalf("IsDeadRuntimeSession = %v, want ErrRuntimeUnavailable", err)
	}
	if len(local.Calls) != 0 {
		t.Fatalf("local backend was called for an unrouted session: %+v", local.Calls)
	}
	// An explicit local route is known before seeding.
	if err := p.RouteHost("s1", LocalHost); err != nil {
		t.Fatal(err)
	}
	if r := p.RouteFor("s1"); !r.Known || r.Provider != local {
		t.Fatalf("RouteFor after RouteHost(local) = %+v", r)
	}
}

// Kills: routing by anything but the table, and projecting for the wrong host.
func TestRoutedStartLandsOnHostWithProjection(t *testing.T) {
	remote := runtime.NewFake()
	project := func(_ string, cfg runtime.Config) (runtime.Config, error) {
		cfg.WorkDir = "/srv/src/web" + cfg.WorkDir[len("/src/web"):]
		return cfg, nil
	}
	p, local := newTestProvider(t, Leg{Host: "host-a", Provider: remote, Project: project})
	p.SeedRoutes(map[string]string{"r1": "host-a", "l1": ""})

	if err := p.Start(context.Background(), "r1", runtime.Config{WorkDir: "/src/web/wt"}); err != nil {
		t.Fatal(err)
	}
	if err := p.Start(context.Background(), "l1", runtime.Config{WorkDir: "/src/web/wt"}); err != nil {
		t.Fatal(err)
	}
	if len(remote.Calls) != 1 || remote.Calls[0].Name != "r1" || remote.Calls[0].Config.WorkDir != "/srv/src/web/wt" {
		t.Fatalf("remote calls = %+v, want projected r1", remote.Calls)
	}
	if len(local.Calls) != 1 || local.Calls[0].Name != "l1" || local.Calls[0].Config.WorkDir != "/src/web/wt" {
		t.Fatalf("local calls = %+v, want unprojected l1", local.Calls)
	}
	if host, known := p.HostOf("r1"); host != "host-a" || !known {
		t.Fatalf("HostOf(r1) = %q %v", host, known)
	}
	if !p.IsRunning("r1") || !p.IsRunning("l1") {
		t.Fatal("IsRunning did not follow routes")
	}
}

// Kills: a projection error passed over (starting with controller paths).
func TestProjectionErrorRefusesStart(t *testing.T) {
	remote := runtime.NewFake()
	boom := errors.New("placement_unmapped_path")
	p, _ := newTestProvider(t, Leg{Host: "host-a", Provider: remote, Project: func(string, runtime.Config) (runtime.Config, error) {
		return runtime.Config{}, boom
	}})
	if err := p.RouteHost("r1", "host-a"); err != nil {
		t.Fatal(err)
	}
	for _, call := range []func() error{
		func() error { return p.Start(context.Background(), "r1", runtime.Config{}) },
		func() error { return p.RunLive("r1", runtime.Config{}) },
		func() error { return p.Relaunch(context.Background(), "r1", runtime.Config{}) },
	} {
		if err := call(); !errors.Is(err, boom) {
			t.Fatalf("err = %v, want projection error", err)
		}
	}
	if len(remote.Calls) != 0 {
		t.Fatalf("remote called despite projection error: %+v", remote.Calls)
	}
}

// Kills: a session on a host gc no longer has a leg for being read as local.
func TestSeedRoutesUnknownHostStaysUnavailable(t *testing.T) {
	p, _ := newTestProvider(t)
	unknown := p.SeedRoutes(map[string]string{"gone": "host-z", "l": "local"})
	if !slices.Equal(unknown, []string{"gone"}) {
		t.Fatalf("unknown = %v", unknown)
	}
	if r := p.RouteFor("gone"); r.Known || r.Label != UnavailableLabel {
		t.Fatalf("RouteFor(gone) = %+v", r)
	}
	if r := p.RouteFor("never-seen"); !r.Known || r.Label != LocalHost {
		t.Fatalf("RouteFor(never-seen) after seed = %+v, want local", r)
	}
	if err := p.RouteHost("x", "host-z"); !errors.Is(err, ErrUnknownHost) {
		t.Fatalf("RouteHost unknown = %v", err)
	}
	// Re-seeding replaces stale explicit routes.
	p.SeedRoutes(map[string]string{})
	if r := p.RouteFor("gone"); r.Label != LocalHost {
		t.Fatalf("after reseed RouteFor(gone) = %+v", r)
	}
}

func TestNewRejectsBadLegs(t *testing.T) {
	f := runtime.NewFake()
	for _, legs := range [][]Leg{
		{{Host: "", Provider: f}},
		{{Host: LocalHost, Provider: f}},
		{{Host: UnavailableLabel, Provider: f}},
		{{Host: "a", Provider: nil}},
		{{Host: "a", Provider: f}, {Host: "a", Provider: f}},
	} {
		if _, err := New(f, legs...); err == nil {
			t.Errorf("New(%+v) succeeded", legs)
		}
	}
	if _, err := New(nil); err == nil {
		t.Error("New(nil) succeeded")
	}
}

// Kills: a listing that skips a host, misorders backends, or attests a
// partial listing.
func TestListingCoversEveryHost(t *testing.T) {
	a, b := runtime.NewFake(), runtime.NewFake()
	p, local := newTestProvider(t, Leg{Host: "host-b", Provider: b}, Leg{Host: "host-a", Provider: a})
	p.SeedRoutes(map[string]string{"sa": "host-a", "sb": "host-b"})
	ctx := context.Background()
	for _, n := range []string{"sa", "sb", "sl"} {
		if err := p.Start(ctx, n, runtime.Config{}); err != nil {
			t.Fatal(err)
		}
	}
	var labels []string
	for _, be := range p.Backends() {
		labels = append(labels, be.Label)
	}
	if !slices.Equal(labels, []string{"local", "host-a", "host-b"}) {
		t.Fatalf("Backends = %v", labels)
	}
	got, err := p.ListRunning("")
	if err != nil {
		t.Fatal(err)
	}
	slices.Sort(got)
	if !slices.Equal(got, []string{"sa", "sb", "sl"}) {
		t.Fatalf("ListRunning = %v", got)
	}
	if !p.ListRunningComplete() {
		t.Fatal("complete listing not attested")
	}
	b.ListingUnattested = true
	if p.ListRunningComplete() {
		t.Fatal("ListRunningComplete true with an unattested host")
	}
	_ = local
}

// Kills: capabilities taken from one leg instead of intersected.
func TestCapabilitiesIntersectAllLegs(t *testing.T) {
	weak := &capsProvider{Provider: runtime.NewFake(), caps: runtime.ProviderCapabilities{CanReportActivity: true, NeedsClaimBackstop: true}}
	p, _ := newTestProvider(t, Leg{Host: "a", Provider: runtime.NewFake()}, Leg{Host: "b", Provider: weak})
	c := p.Capabilities()
	if c.CanReportAttachment || c.CanAttachTTY || !c.CanReportActivity || !c.NeedsClaimBackstop {
		t.Fatalf("Capabilities = %+v", c)
	}
}

type capsProvider struct {
	runtime.Provider
	caps runtime.ProviderCapabilities
}

func (c *capsProvider) Capabilities() runtime.ProviderCapabilities { return c.caps }
