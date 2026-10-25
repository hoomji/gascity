package beads

import (
	"context"
	"errors"
	"testing"
)

func TestSQLiteStoreListContextMatchesList(t *testing.T) {
	store := newReadySQLiteStore(t)
	seedReadyCorpus(t, store)
	query := ListQuery{AllowScan: true, Sort: SortCreatedDesc}

	want, err := store.List(query)
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	got, err := store.ListContext(context.Background(), query)
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

func TestSQLiteStoreListContextCanceled(t *testing.T) {
	store := newReadySQLiteStore(t)
	seedReadyCorpus(t, store)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	_, err := store.ListContext(ctx, ListQuery{AllowScan: true})
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("ListContext on canceled ctx = %v, want context.Canceled", err)
	}
}
