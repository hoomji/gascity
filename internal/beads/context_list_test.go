package beads_test

import (
	"context"
	"encoding/json"
	"errors"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gastownhall/gascity/internal/beads"
)

// contextListFake is a beads.Store that also implements the optional
// context-aware list capability, recording whether ListContext was used.
type contextListFake struct {
	beads.Store
	listCtxCalls atomic.Int32
	rows         []beads.Bead
}

func (f *contextListFake) ListContext(ctx context.Context, _ beads.ListQuery) ([]beads.Bead, error) {
	f.listCtxCalls.Add(1)
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return f.rows, nil
}

func TestListWithContextPrefersContextLister(t *testing.T) {
	mem := beads.NewMemStore()
	if _, err := mem.Create(beads.Bead{Title: "backing row", Type: "task"}); err != nil {
		t.Fatalf("Create: %v", err)
	}
	fake := &contextListFake{Store: mem, rows: []beads.Bead{{ID: "ctx-1", Title: "ctx row"}}}

	got, err := beads.ListWithContext(context.Background(), fake, beads.ListQuery{AllowScan: true})
	if err != nil {
		t.Fatalf("ListWithContext: %v", err)
	}
	if fake.listCtxCalls.Load() != 1 {
		t.Fatalf("ListContext calls = %d, want 1 (must prefer the context-aware path)", fake.listCtxCalls.Load())
	}
	if len(got) != 1 || got[0].ID != "ctx-1" {
		t.Fatalf("ListWithContext = %+v, want the ListContext rows", got)
	}
}

func TestListWithContextFallsBackToList(t *testing.T) {
	mem := beads.NewMemStore()
	created, err := mem.Create(beads.Bead{Title: "plain row", Type: "task"})
	if err != nil {
		t.Fatalf("Create: %v", err)
	}

	got, err := beads.ListWithContext(context.Background(), mem, beads.ListQuery{AllowScan: true})
	if err != nil {
		t.Fatalf("ListWithContext: %v", err)
	}
	if len(got) != 1 || got[0].ID != created.ID {
		t.Fatalf("ListWithContext = %+v, want the MemStore rows", got)
	}
}

func TestListWithContextCanceledSkipsStore(t *testing.T) {
	mem := beads.NewMemStore()
	if _, err := mem.Create(beads.Bead{Title: "row", Type: "task"}); err != nil {
		t.Fatalf("Create: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	_, err := beads.ListWithContext(ctx, mem, beads.ListQuery{AllowScan: true})
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("ListWithContext on canceled ctx = %v, want context.Canceled", err)
	}
}

func TestBdStoreListContextCanceledSkipsRunner(t *testing.T) {
	var calls atomic.Int32
	runner := func(_ string, _ string, _ ...string) ([]byte, error) {
		calls.Add(1)
		return []byte("[]"), nil
	}
	store := beads.NewBdStore(t.TempDir(), runner)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	_, err := store.ListContext(ctx, beads.ListQuery{AllowScan: true})
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("ListContext on canceled ctx = %v, want context.Canceled", err)
	}
	if calls.Load() != 0 {
		t.Fatalf("runner calls = %d, want 0 (canceled reads must not spawn bd)", calls.Load())
	}
}

// TestBdStoreListContextKillsSlowRead proves a canceled request stops waiting
// for a stuck bd child instead of riding it to the 120s subprocess timeout.
// The fake context runner plays the stuck child: it blocks until ctx dies.
func TestBdStoreListContextKillsSlowRead(t *testing.T) {
	plainRunner := func(_ string, _ string, _ ...string) ([]byte, error) {
		t.Error("plain runner must not run when a context runner is configured")
		return nil, errors.New("wrong runner")
	}
	ctxRunner := func(ctx context.Context, _ string, _ string, _ ...string) ([]byte, error) {
		<-ctx.Done()
		return nil, ctx.Err()
	}
	store := beads.NewBdStore(t.TempDir(), plainRunner, beads.WithBdStoreContextRunner(ctxRunner))

	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(50 * time.Millisecond)
		cancel()
	}()
	start := time.Now()
	done := make(chan error, 1)
	go func() {
		_, err := store.ListContext(ctx, beads.ListQuery{AllowScan: true})
		done <- err
	}()
	select {
	case err := <-done:
		elapsed := time.Since(start)
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("ListContext on canceled read = %v, want context.Canceled", err)
		}
		if elapsed > 10*time.Second {
			t.Fatalf("ListContext waited %s for a canceled read, want prompt return", elapsed)
		}
	case <-time.After(15 * time.Second):
		t.Fatal("ListContext did not return after cancel; the context likely never reached the child")
	}
}

func TestCachingStoreListContextCanceledSkipsBacking(t *testing.T) {
	var calls atomic.Int32
	backing := &countingListStore{Store: beads.NewMemStore(), calls: &calls}
	cache := beads.NewCachingStoreForTest(backing, func(string, string, json.RawMessage) {})
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	_, err := cache.ListContext(ctx, beads.ListQuery{AllowScan: true})
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("ListContext on canceled ctx = %v, want context.Canceled", err)
	}
	if calls.Load() != 0 {
		t.Fatalf("backing List calls = %d, want 0", calls.Load())
	}
}

func TestCachingStoreListContextMatchesList(t *testing.T) {
	mem := beads.NewMemStore()
	if _, err := mem.Create(beads.Bead{Title: "a", Type: "task"}); err != nil {
		t.Fatalf("Create: %v", err)
	}
	if _, err := mem.Create(beads.Bead{Title: "b", Type: "task"}); err != nil {
		t.Fatalf("Create: %v", err)
	}
	cache := beads.NewCachingStoreForTest(mem, func(string, string, json.RawMessage) {})
	query := beads.ListQuery{AllowScan: true, Sort: beads.SortCreatedDesc}

	want, err := cache.List(query)
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	got, err := cache.ListContext(context.Background(), query)
	if err != nil {
		t.Fatalf("ListContext: %v", err)
	}
	if len(got) != len(want) {
		t.Fatalf("ListContext returned %d rows, List returned %d", len(got), len(want))
	}
	for i := range want {
		if got[i].ID != want[i].ID {
			t.Fatalf("ListContext row %d = %s, List row %d = %s", i, got[i].ID, i, want[i].ID)
		}
	}
}

// countingListStore counts backing List calls.
type countingListStore struct {
	beads.Store
	calls *atomic.Int32
}

func (s *countingListStore) List(q beads.ListQuery) ([]beads.Bead, error) {
	s.calls.Add(1)
	return s.Store.List(q)
}
