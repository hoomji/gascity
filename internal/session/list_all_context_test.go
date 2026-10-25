package session

import (
	"context"
	"errors"
	"testing"

	"github.com/gastownhall/gascity/internal/beads"
)

func TestListAllSessionBeadsContextCanceled(t *testing.T) {
	store := beads.NewMemStore()
	if _, err := store.Create(beads.Bead{Title: "s", Type: BeadType}); err != nil {
		t.Fatalf("Create: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	_, err := ListAllSessionBeadsContext(ctx, store, beads.ListQuery{})
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("ListAllSessionBeadsContext on canceled ctx = %v, want context.Canceled", err)
	}
}

func TestListAllWithResponsesContextMatchesPlain(t *testing.T) {
	store := beads.NewMemStore()
	if _, err := store.Create(beads.Bead{Title: "one", Type: BeadType, Labels: []string{LabelSession}}); err != nil {
		t.Fatalf("Create: %v", err)
	}
	if _, err := store.Create(beads.Bead{Title: "two", Type: BeadType}); err != nil {
		t.Fatalf("Create: %v", err)
	}
	front := NewStore(beads.SessionStore{Store: store})
	opts := ListAllOptions{Sort: beads.SortCreatedDesc, CacheFirst: true}

	want, err := front.ListAllWithResponses(opts)
	if err != nil {
		t.Fatalf("ListAllWithResponses: %v", err)
	}
	got, err := front.ListAllWithResponsesContext(context.Background(), opts)
	if err != nil {
		t.Fatalf("ListAllWithResponsesContext: %v", err)
	}
	if len(got) != len(want) {
		t.Fatalf("ListAllWithResponsesContext returned %d rows, plain returned %d", len(got), len(want))
	}
	for i := range want {
		if got[i].Info.ID != want[i].Info.ID {
			t.Fatalf("row %d = %s, want %s", i, got[i].Info.ID, want[i].Info.ID)
		}
	}
}

func TestListAllContextCanceled(t *testing.T) {
	store := beads.NewMemStore()
	if _, err := store.Create(beads.Bead{Title: "s", Type: BeadType}); err != nil {
		t.Fatalf("Create: %v", err)
	}
	front := NewStore(beads.SessionStore{Store: store})
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	_, err := front.ListAllContext(ctx, ListAllOptions{Sort: beads.SortCreatedDesc})
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("ListAllContext on canceled ctx = %v, want context.Canceled", err)
	}
}
