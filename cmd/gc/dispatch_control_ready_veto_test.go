package main

import (
	"sync"
	"testing"

	"github.com/gastownhall/gascity/internal/beadmeta"
	"github.com/gastownhall/gascity/internal/beads"
	"github.com/gastownhall/gascity/internal/config"
)

// readyQueryStore records the ReadyQuery of every Ready read, and stands in for
// a store that applies the work-outcome veto by dropping vetoed ids from the
// answers that do not ask to skip it.
type readyQueryStore struct {
	*beads.MemStore
	mu      sync.Mutex
	queries []beads.ReadyQuery
	vetoed  map[string]bool
}

func (s *readyQueryStore) Ready(q ...beads.ReadyQuery) ([]beads.Bead, error) {
	var query beads.ReadyQuery
	if len(q) > 0 {
		query = q[0]
	}
	s.mu.Lock()
	s.queries = append(s.queries, query)
	s.mu.Unlock()
	rows, err := s.MemStore.Ready(q...)
	if err != nil || query.SkipWorkOutcomeVeto {
		return rows, err
	}
	kept := rows[:0:0]
	for _, b := range rows {
		if !s.vetoed[b.ID] {
			kept = append(kept, b)
		}
	}
	return kept, nil
}

func (s *readyQueryStore) recorded() []beads.ReadyQuery {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]beads.ReadyQuery(nil), s.queries...)
}

func scanWithReadyQueryStore(t *testing.T, store *readyQueryStore) []hookBead {
	t.Helper()
	installControlReadyLegSourcesFn(t, func(_, _ string, _ *config.City) ([]beads.Store, []beads.Store, error) {
		return []beads.Store{store}, nil, nil
	})
	dir := t.TempDir()
	query := workflowServeControlReadyQuery(config.Agent{Name: config.ControlDispatcherAgentName, Dir: "gascity"})
	queue, handled, err := tryControlReadyScan(query, dir, nil)
	if err != nil || !handled {
		t.Fatalf("tryControlReadyScan = handled %t, err %v; want handled, nil", handled, err)
	}
	return queue
}

// An idle scan is one Ready read that leaves out the work-outcome veto: the veto
// only removes rows, so an empty answer without it needs no blocker reads.
func TestControlReadyScanWithNoControlWorkReadsOnceWithoutTheVeto(t *testing.T) {
	store := &readyQueryStore{MemStore: beads.NewMemStore()}
	if _, err := store.Create(beads.Bead{Title: "a session heartbeat nobody routed here", Type: "task"}); err != nil {
		t.Fatal(err)
	}

	queue := scanWithReadyQueryStore(t, store)

	if len(queue) != 0 {
		t.Fatalf("queue = %+v, want empty", queue)
	}
	got := store.recorded()
	if len(got) != 1 || !got[0].SkipWorkOutcomeVeto {
		t.Fatalf("Ready reads = %+v, want exactly one, with SkipWorkOutcomeVeto", got)
	}
}

// A scan that finds work reads again with the veto and answers from that read,
// so a bead whose blocker closed as blocked is never handed to the dispatcher.
func TestControlReadyScanVetoesBeforeOfferingWork(t *testing.T) {
	store := &readyQueryStore{MemStore: beads.NewMemStore(), vetoed: map[string]bool{}}
	target := "gascity/control-dispatcher"
	kept, err := store.Create(beads.Bead{Metadata: map[string]string{beadmeta.RoutedToMetadataKey: target}})
	if err != nil {
		t.Fatal(err)
	}
	blocked, err := store.Create(beads.Bead{Metadata: map[string]string{beadmeta.RoutedToMetadataKey: target}})
	if err != nil {
		t.Fatal(err)
	}
	store.vetoed[blocked.ID] = true

	queue := scanWithReadyQueryStore(t, store)

	if len(queue) != 1 || queue[0].ID != kept.ID {
		t.Fatalf("queue = %+v, want only %s: %s is vetoed", queue, kept.ID, blocked.ID)
	}
	got := store.recorded()
	if len(got) != 2 || !got[0].SkipWorkOutcomeVeto || got[1].SkipWorkOutcomeVeto {
		t.Fatalf("Ready reads = %+v, want a read without the veto, then one with it", got)
	}
}
