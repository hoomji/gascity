package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"runtime"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestBDReadGuardAcquireRelease(t *testing.T) {
	g := newBDReadGuard(1)
	release, err := g.acquire(context.Background())
	if err != nil {
		t.Fatalf("acquire: %v", err)
	}
	release()
	if _, err := g.acquire(context.Background()); err != nil {
		t.Fatalf("acquire after release: %v", err)
	}
}

func TestBDReadGuardSaturatedFailsFast(t *testing.T) {
	g := newBDReadGuard(1)
	hold, err := g.acquire(context.Background())
	if err != nil {
		t.Fatalf("acquire: %v", err)
	}
	defer hold()

	if _, err := g.acquire(context.Background()); err == nil {
		t.Fatal("saturated acquire = nil, want a 503 overload error")
	}
}

func TestBDReadGuardAcquireHonorsCancel(t *testing.T) {
	g := newBDReadGuard(1)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	_, err := g.acquire(ctx)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("acquire on canceled ctx = %v, want context.Canceled", err)
	}
}

func TestBDReadListsSaturatedReturn503WithRetryAfter(t *testing.T) {
	state := newFakeState(t)
	srv := New(state)
	srv.bdRead = newBDReadGuard(1)
	hold, err := srv.bdRead.acquire(context.Background())
	if err != nil {
		t.Fatalf("acquire: %v", err)
	}
	defer hold()
	h := newTestCityHandlerWith(t, state, srv)

	for _, path := range []string{"/beads", "/sessions"} {
		req := httptest.NewRequest("GET", cityURL(state, path), nil)
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != http.StatusServiceUnavailable {
			t.Errorf("GET %s saturated = %d, want 503", path, rec.Code)
		}
		if got := rec.Header().Get("Retry-After"); got == "" {
			t.Errorf("GET %s saturated: missing Retry-After header", path)
		}
	}
}

func TestListSingleflightCoalescesConcurrentReads(t *testing.T) {
	state := newFakeState(t)
	srv := New(state)
	var runs atomic.Int32
	const callers = 10
	// The shared fn stays open until every caller has JOINED it: an
	// instant fn could complete between joins and prove nothing. Join
	// progress is observed white-box (the guarded waiter count) purely as
	// synchronization — the assertions below stay behavioral (one run,
	// every caller served). No wall-time wait: the flight cannot close
	// before release, and no waiter can leave a live flight, so observing
	// all ten joined deterministically precedes the release.
	release := make(chan struct{})
	started := make(chan struct{})
	var startOnce sync.Once
	var wg sync.WaitGroup
	errs := make([]error, callers)
	for i := 0; i < callers; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			v, err := srv.coalesceList(context.Background(), "test:key", func(context.Context) (any, error) {
				runs.Add(1)
				startOnce.Do(func() { close(started) })
				<-release
				return "shared", nil
			})
			if err != nil {
				errs[i] = err
				return
			}
			if v.(string) != "shared" {
				errs[i] = fmt.Errorf("got %v, want shared", v)
			}
		}(i)
	}
	<-started
	joinDeadline := time.After(10 * time.Second)
	for {
		srv.bdRead.mu.Lock()
		call := srv.bdRead.calls["test:key"]
		waiters := -1
		if call != nil {
			waiters = call.waiters
		}
		srv.bdRead.mu.Unlock()
		if waiters == callers {
			break
		}
		select {
		case <-joinDeadline:
			t.Fatalf("only %d of %d callers joined the flight", waiters, callers)
		default:
		}
		runtime.Gosched()
	}
	close(release)
	wg.Wait()
	for i, err := range errs {
		if err != nil {
			t.Fatalf("caller %d: %v", i, err)
		}
	}
	if runs.Load() != 1 {
		t.Fatalf("shared fn ran %d times for %d concurrent callers, want 1", runs.Load(), callers)
	}
}

func TestListSingleflightHonorsFollowerCancel(t *testing.T) {
	state := newFakeState(t)
	srv := New(state)
	started := make(chan struct{})
	release := make(chan struct{})
	var once sync.Once
	leaderDone := make(chan struct{})
	leaderVal := make(chan any, 1)
	go func() {
		defer close(leaderDone)
		v, err := srv.coalesceList(context.Background(), "test:slow", func(context.Context) (any, error) {
			once.Do(func() { close(started) })
			<-release
			return "slow", nil
		})
		if err != nil {
			t.Errorf("leader: %v", err)
			return
		}
		leaderVal <- v
	}()
	<-started
	// A follower that cancels mid-wait leaves without disturbing the leader.
	ctx, cancel := context.WithCancel(context.Background())
	followerDone := make(chan error, 1)
	go func() {
		_, err := srv.coalesceList(ctx, "test:slow", func(context.Context) (any, error) {
			return "follower", nil
		})
		followerDone <- err
	}()
	cancel()
	if err := <-followerDone; !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled follower = %v, want context.Canceled", err)
	}
	close(release)
	<-leaderDone
	if v := <-leaderVal; v.(string) != "slow" {
		t.Fatalf("leader = %v, want slow (follower cancel must not fail the shared read)", v)
	}
}

func TestListSingleflightLastWaiterOutKillsSharedRead(t *testing.T) {
	state := newFakeState(t)
	srv := New(state)
	started := make(chan struct{})
	var once sync.Once
	fnCtxDone := make(chan struct{})
	waiterDone := make(chan error, 1)
	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		_, err := srv.coalesceList(ctx, "test:lone", func(fnCtx context.Context) (any, error) {
			once.Do(func() { close(started) })
			<-fnCtx.Done()
			close(fnCtxDone)
			return nil, fnCtx.Err()
		})
		waiterDone <- err
	}()
	<-started
	cancel()
	if err := <-waiterDone; !errors.Is(err, context.Canceled) {
		t.Fatalf("lone waiter = %v, want context.Canceled", err)
	}
	select {
	case <-fnCtxDone:
	case <-time.After(5 * time.Second):
		t.Fatal("shared read context was not canceled when the last waiter left")
	}
}

func TestHumaHandleBeadListCanceledContext(t *testing.T) {
	state := newFakeState(t)
	srv := New(state)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	_, err := srv.humaHandleBeadList(ctx, &BeadListInput{})
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("humaHandleBeadList on canceled ctx = %v, want context.Canceled", err)
	}
}

func TestHumaHandleSessionListCanceledContext(t *testing.T) {
	state := newFakeState(t)
	srv := New(state)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	_, err := srv.humaHandleSessionList(ctx, &SessionListInput{})
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("humaHandleSessionList on canceled ctx = %v, want context.Canceled", err)
	}
}

func TestListSingleflightKeysDifferByParams(t *testing.T) {
	a := beadListFlightKey("city", &BeadListInput{Status: "open"}, false)
	b := beadListFlightKey("city", &BeadListInput{Status: "closed"}, false)
	if a == b {
		t.Fatal("bead keys with different status are identical, want distinct coalescing keys")
	}
	c := sessionListFlightKey("city", &SessionListInput{State: "active"})
	d := sessionListFlightKey("city", &SessionListInput{State: "closed"})
	if c == d {
		t.Fatal("session keys with different state are identical, want distinct coalescing keys")
	}
	e := beadListFlightKey("city-a", &BeadListInput{}, false)
	f := beadListFlightKey("city-b", &BeadListInput{}, false)
	if e == f {
		t.Fatal("bead keys for different cities are identical, want city-scoped keys")
	}
	// blocking shapes the body (cache bypass), so a blocking caller must
	// never share a flight with a non-blocking one: it would receive a
	// cached body that predates the event it waited for.
	g := beadListFlightKey("city", &BeadListInput{All: true}, false)
	h := beadListFlightKey("city", &BeadListInput{All: true}, true)
	if g == h {
		t.Fatal("bead keys for blocking and non-blocking callers are identical, want distinct coalescing keys")
	}
}

func TestListSingleflightFreshFlightAfterAllWaitersLeave(t *testing.T) {
	state := newFakeState(t)
	srv := New(state)
	started := make(chan struct{})
	// releaseFlight1 holds the departed flight's goroutine open after
	// cancel, so a rejoining caller deterministically lands on the dead
	// flight when detach is broken (and blocks), instead of racing its
	// cleanup. The timeout below only fires on failure.
	releaseFlight1 := make(chan struct{})
	var once sync.Once
	ctx, cancel := context.WithCancel(context.Background())
	waiterDone := make(chan error, 1)
	go func() {
		_, err := srv.coalesceList(ctx, "test:rejoin", func(fnCtx context.Context) (any, error) {
			once.Do(func() { close(started) })
			<-fnCtx.Done()
			<-releaseFlight1
			return nil, fnCtx.Err()
		})
		waiterDone <- err
	}()
	<-started
	cancel()
	if err := <-waiterDone; !errors.Is(err, context.Canceled) {
		t.Fatalf("departed waiter = %v, want context.Canceled", err)
	}
	defer close(releaseFlight1)
	// The canceled flight is detached from the key: a caller that arrives
	// after the mass disconnect starts a fresh read and gets its own
	// result, never the departed flight's cancellation error.
	type rejoinResult struct {
		v   any
		err error
	}
	rejoined := make(chan rejoinResult, 1)
	go func() {
		v, err := srv.coalesceList(context.Background(), "test:rejoin", func(context.Context) (any, error) {
			return "fresh", nil
		})
		rejoined <- rejoinResult{v: v, err: err}
	}()
	select {
	case r := <-rejoined:
		if r.err != nil {
			t.Fatalf("rejoining caller = %v, want a fresh result (it joined a canceled flight)", r.err)
		}
		if r.v.(string) != "fresh" {
			t.Fatalf("rejoining caller = %v, want fresh", r.v)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("rejoining caller blocked on the departed flight instead of starting a fresh read")
	}
}
