package main

import (
	"sync"
	"testing"
	"time"

	"github.com/gastownhall/gascity/internal/beads"
	"github.com/gastownhall/gascity/internal/config"
)

// readGate parks each Ready until a second reader is inside, and records the
// peak number of readers in flight, so a serial caller can never show a peak
// above one: its lone reader waits out the timeout and the peak stays at one.
type readGate struct {
	mu   sync.Mutex
	in   int
	peak int
	met  chan struct{}
	once sync.Once
}

func (g *readGate) enter() {
	g.mu.Lock()
	g.in++
	if g.in > g.peak {
		g.peak = g.in
	}
	if g.in >= 2 {
		g.once.Do(func() { close(g.met) })
	}
	g.mu.Unlock()
	select {
	case <-g.met:
	case <-time.After(2 * time.Second):
	}
	g.mu.Lock()
	g.in--
	g.mu.Unlock()
}

type gatedReadyStore struct {
	*beads.MemStore
	gate *readGate
}

func (s *gatedReadyStore) Ready(query ...beads.ReadyQuery) ([]beads.Bead, error) {
	s.gate.enter()
	return s.MemStore.Ready(query...)
}

func TestReadControllerDemandReadyOverlapsStoreReads(t *testing.T) {
	gate := &readGate{met: make(chan struct{})}
	var stores []beads.Store
	for i := 0; i < 3; i++ {
		mem := beads.NewMemStore()
		if _, err := mem.Create(beads.Bead{Title: "work"}); err != nil {
			t.Fatal(err)
		}
		stores = append(stores, &gatedReadyStore{MemStore: mem, gate: gate})
	}

	got := readControllerDemandReady(nil, stores)

	if len(got) != 3 {
		t.Fatalf("results = %d, want one per store", len(got))
	}
	if gate.peak < 2 {
		t.Errorf("peak concurrent reads = %d, want the store reads to overlap", gate.peak)
	}
	for i, r := range got {
		if r.err != nil || len(r.rows) != 1 {
			t.Errorf("store %d: rows=%d err=%v, want its own single ready bead", i, len(r.rows), r.err)
		}
	}
}

type gatedListStore struct {
	*beads.MemStore
	gate *readGate
}

func (s *gatedListStore) List(query beads.ListQuery) ([]beads.Bead, error) {
	s.gate.enter()
	return s.MemStore.List(query)
}

func TestCollectAssignedWorkOverlapsOneStoresListReads(t *testing.T) {
	gate := &readGate{met: make(chan struct{})}
	mem := beads.NewMemStore()
	created, err := mem.Create(beads.Bead{Title: "active", Assignee: "worker-1"})
	if err != nil {
		t.Fatal(err)
	}
	inProgress := "in_progress"
	if err := mem.Update(created.ID, beads.UpdateOpts{Status: &inProgress}); err != nil {
		t.Fatal(err)
	}

	got, _, _, _, _ := collectAssignedWorkBeadsWithStores("", &config.City{}, &gatedListStore{MemStore: mem, gate: gate}, nil, nil, nil)

	if gate.peak < 2 {
		t.Errorf("peak concurrent list reads = %d, want one store's list reads to overlap", gate.peak)
	}
	var found bool
	for _, b := range got {
		if b.ID == created.ID {
			found = true
		}
	}
	if !found {
		t.Errorf("assigned in-progress bead %s missing from snapshot %v", created.ID, got)
	}
}
