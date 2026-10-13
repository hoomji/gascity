package main

import (
	"io"
	"sync"
	"testing"

	"github.com/gastownhall/gascity/internal/beadmeta"
	"github.com/gastownhall/gascity/internal/beads"
	"github.com/gastownhall/gascity/internal/orders"
)

// rootReadCountStore records every List that names a gc.root_bead_id
// membership filter, and whether it asked for closed history. Each such query
// is one `bd list --metadata-field gc.root_bead_id=<id>` subprocess on the
// production bd store, and IncludeClosed adds `--all` over the closed rows.
type rootReadCountStore struct {
	beads.Store
	mu          sync.Mutex
	rootReads   int
	closedReads int
}

func (s *rootReadCountStore) List(q beads.ListQuery) ([]beads.Bead, error) {
	if _, ok := q.Metadata[beadmeta.RootBeadIDMetadataKey]; ok {
		s.mu.Lock()
		s.rootReads++
		if q.IncludeClosed {
			s.closedReads++
		}
		s.mu.Unlock()
	}
	return s.Store.List(q)
}

func seedWispRoot(t *testing.T, store beads.Store, scoped string, openStep bool) {
	t.Helper()
	root, err := store.Create(beads.Bead{
		Title:    "wisp " + scoped,
		Labels:   []string{orders.RunLabel(scoped)},
		Metadata: map[string]string{beadmeta.KindMetadataKey: beadmeta.KindWorkflow},
	})
	if err != nil {
		t.Fatalf("create root: %v", err)
	}
	step, err := store.Create(beads.Bead{
		Title:    "step",
		Metadata: map[string]string{beadmeta.RootBeadIDMetadataKey: root.ID},
	})
	if err != nil {
		t.Fatalf("create step: %v", err)
	}
	if !openStep {
		if err := store.Close(step.ID); err != nil {
			t.Fatalf("close step: %v", err)
		}
	}
}

// A root that already has an open stamped member is answered from the tick's
// single non-closed corpus read: zero per-root membership reads, however many
// roots the order has in flight. A wrong implementation that still issues one
// membership read per root fails the rootReads == 0 assertion.
func TestOrderGateOpenRootsAnsweredFromIndexWithoutPerRootReads(t *testing.T) {
	store := &rootReadCountStore{Store: beads.NewMemStore()}
	const scoped = "feeder"
	for i := 0; i < 5; i++ {
		seedWispRoot(t, store, scoped, true)
	}
	disp := &memoryOrderDispatcher{}
	idx := newOrderDispatchTrackingIndex(io.Discard)

	open, err := idx.hasOpenWork([]beads.Store{store}, []string{"city"}, scoped, disp.wispRootHasOpenWork, nil)
	if err != nil {
		t.Fatalf("hasOpenWork: %v", err)
	}
	if !open {
		t.Fatal("hasOpenWork = false, want true: roots have open steps")
	}
	if store.rootReads != 0 {
		t.Errorf("per-root membership reads = %d, want 0 (answered from the index)", store.rootReads)
	}
}

// A root whose members are all closed still pays the authoritative per-root
// read, but that read must be status-bounded: asking for closed history walks
// the store's whole closed corpus (21k+ rows) for rows the caller discards.
func TestOrderGateRootMembershipReadIsStatusBounded(t *testing.T) {
	store := &rootReadCountStore{Store: beads.NewMemStore()}
	const scoped = "feeder"
	seedWispRoot(t, store, scoped, false)
	disp := &memoryOrderDispatcher{}
	idx := newOrderDispatchTrackingIndex(io.Discard)

	open, err := idx.hasOpenWork([]beads.Store{store}, []string{"city"}, scoped, disp.wispRootHasOpenWork, nil)
	if err != nil {
		t.Fatalf("hasOpenWork: %v", err)
	}
	if open {
		t.Fatal("hasOpenWork = true, want false: every step is closed")
	}
	if store.rootReads == 0 {
		t.Fatal("expected the authoritative per-root read for a root with no open member")
	}
	if store.closedReads != 0 {
		t.Errorf("membership reads with IncludeClosed = %d, want 0 (bd list --all over closed history)", store.closedReads)
	}
}
