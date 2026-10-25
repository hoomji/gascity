package beads

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// cancelGateStore plays a backing whose per-ID read is a slow subprocess.
// GetContext blocks until ctx dies (the child the cancellable overlay kills
// on disconnect); the context-blind Get instead sleeps a fixed delay and
// answers anyway (the uncancellable bd show the old overlay rode to
// completion). Call counts prove no further backing read starts after cancel.
type cancelGateStore struct {
	Store
	entered   chan struct{}
	enterOnce sync.Once
	mu        sync.Mutex
	getIDs    []string
	getCtxIDs []string
	depIDs    []string
	depCtxIDs []string
	sawCancel atomic.Bool
	// uncancellableDelay is the fixed cost of one context-blind Get. It runs
	// only on code that ignores the context-aware path; the fixed overlay
	// never calls Get.
	uncancellableDelay time.Duration
}

func (s *cancelGateStore) Get(id string) (Bead, error) {
	s.mu.Lock()
	s.getIDs = append(s.getIDs, id)
	s.mu.Unlock()
	time.Sleep(s.uncancellableDelay)
	return s.Store.Get(id)
}

func (s *cancelGateStore) GetContext(ctx context.Context, id string) (Bead, error) {
	s.mu.Lock()
	s.getCtxIDs = append(s.getCtxIDs, id)
	s.mu.Unlock()
	s.enterOnce.Do(func() { close(s.entered) })
	<-ctx.Done()
	s.sawCancel.Store(true)
	return Bead{}, ctx.Err()
}

func (s *cancelGateStore) DepList(id, direction string) ([]Dep, error) {
	s.mu.Lock()
	s.depIDs = append(s.depIDs, id)
	s.mu.Unlock()
	return s.Store.DepList(id, direction)
}

func (s *cancelGateStore) DepListContext(ctx context.Context, id, direction string) ([]Dep, error) {
	s.mu.Lock()
	s.depCtxIDs = append(s.depCtxIDs, id)
	s.mu.Unlock()
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return s.Store.DepList(id, direction)
}

// resetCallCounts drops the recorded per-ID traffic so the test below counts
// only the reads the canceled list itself issues, not the prime's.
func (s *cancelGateStore) resetCallCounts() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.getIDs = nil
	s.getCtxIDs = nil
	s.depIDs = nil
	s.depCtxIDs = nil
}

// TestOverlayListContextCancelStopsDirtyRefresh proves a disconnect mid-
// overlay releases the list promptly: the in-flight dirty-ID read observes
// the cancellation (the abandoned bd show the old path left running past its
// budget), no further dirty read starts, and the caller gets ctx.Err() rather
// than cached rows.
func TestOverlayListContextCancelStopsDirtyRefresh(t *testing.T) {
	t.Parallel()
	const stuckRead = 1500 * time.Millisecond
	backing := &cancelGateStore{
		Store:              NewMemStore(),
		entered:            make(chan struct{}),
		uncancellableDelay: stuckRead,
	}
	var ids []string
	for i := 0; i < 3; i++ {
		created, err := backing.Create(Bead{Title: "bead", Status: "open"})
		if err != nil {
			t.Fatalf("create: %v", err)
		}
		ids = append(ids, created.ID)
	}
	store := NewCachingStoreForTest(backing, nil)
	if err := store.Prime(context.Background()); err != nil {
		t.Fatalf("prime: %v", err)
	}
	markDirtyForTest(store, ids[0], ids[1])
	backing.resetCallCounts()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	defer close(done)
	go func() {
		select {
		case <-backing.entered:
			cancel()
		case <-done:
		}
	}()

	type listResult struct {
		rows []Bead
		err  error
	}
	resCh := make(chan listResult, 1)
	start := time.Now()
	go func() {
		rows, err := store.ListContext(ctx, ListQuery{Status: "open"})
		resCh <- listResult{rows: rows, err: err}
	}()
	select {
	case res := <-resCh:
		elapsed := time.Since(start)
		if !errors.Is(res.err, context.Canceled) {
			t.Fatalf("ListContext after disconnect = %d rows, err %v; want no rows and context.Canceled", len(res.rows), res.err)
		}
		if len(res.rows) != 0 {
			t.Fatalf("ListContext after disconnect returned %d cached rows; want none", len(res.rows))
		}
		if elapsed >= stuckRead {
			t.Fatalf("ListContext held its slot %s after disconnect; want release before one stuck read (%s)", elapsed, stuckRead)
		}
		backing.mu.Lock()
		defer backing.mu.Unlock()
		if len(backing.getCtxIDs) != 1 {
			t.Fatalf("GetContext calls = %d (%v); want exactly 1: no further dirty read may start after cancel", len(backing.getCtxIDs), backing.getCtxIDs)
		}
		if len(backing.getIDs) != 0 {
			t.Fatalf("context-blind Get calls = %d; the cancellable path must not use them", len(backing.getIDs))
		}
		if n := len(backing.depCtxIDs) + len(backing.depIDs); n != 0 {
			t.Fatalf("DepList calls = %d after cancel; want none", n)
		}
		if !backing.sawCancel.Load() {
			t.Fatal("in-flight backing read never observed cancellation; its child would outlive the caller")
		}
	case <-time.After(15 * time.Second):
		t.Fatal("ListContext did not return after disconnect; the overlay never saw the cancellation")
	}
}

// slowMergeStore plays a backing whose closed-history list finishes just as
// the caller gives up: it waits out the cancellation, then answers rows with
// a nil error. The cache-side merge must still surface the cancellation.
type slowMergeStore struct {
	Store
	entered   chan struct{}
	enterOnce sync.Once
}

func (s *slowMergeStore) ListContext(ctx context.Context, query ListQuery) ([]Bead, error) {
	if !query.Live {
		return s.List(query)
	}
	s.enterOnce.Do(func() { close(s.entered) })
	<-ctx.Done()
	return s.List(query)
}

// TestOverlayListContextMergeRechecksCancel proves the include-closed merge
// does not return rows gathered for a caller that already left: when the
// backing history read answers (nil error) after cancellation, ListContext
// still reports ctx.Err().
func TestOverlayListContextMergeRechecksCancel(t *testing.T) {
	t.Parallel()
	backing := &slowMergeStore{Store: NewMemStore(), entered: make(chan struct{})}
	for _, status := range []string{"open", "open", "closed"} {
		if _, err := backing.Create(Bead{Title: "bead", Status: status}); err != nil {
			t.Fatalf("create: %v", err)
		}
	}
	store := NewCachingStoreForTest(backing, nil)
	if err := store.Prime(context.Background()); err != nil {
		t.Fatalf("prime: %v", err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	defer close(done)
	go func() {
		select {
		case <-backing.entered:
			cancel()
		case <-done:
		}
	}()

	type listResult struct {
		rows []Bead
		err  error
	}
	resCh := make(chan listResult, 1)
	go func() {
		rows, err := store.ListContext(ctx, ListQuery{AllowScan: true, IncludeClosed: true})
		resCh <- listResult{rows: rows, err: err}
	}()
	select {
	case res := <-resCh:
		if !errors.Is(res.err, context.Canceled) {
			t.Fatalf("ListContext after cancel = %d rows, err %v; want no rows and context.Canceled", len(res.rows), res.err)
		}
		if len(res.rows) != 0 {
			t.Fatalf("ListContext after cancel returned %d rows; want none", len(res.rows))
		}
	case <-time.After(15 * time.Second):
		t.Fatal("ListContext did not return after cancel")
	}
}
