package beads

import (
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/gastownhall/gascity/internal/fsys"
)

// metadataAnyStores returns every in-tree store that answers
// ListByMetadataAny, each holding the same rows.
func metadataAnyStores(t *testing.T) map[string]MetadataAnyLister {
	t.Helper()
	fileStore, err := OpenFileStore(fsys.OSFS{}, t.TempDir()+"/beads.json")
	if err != nil {
		t.Fatalf("OpenFileStore: %v", err)
	}
	sqlite, err := OpenSQLiteStore(t.TempDir())
	if err != nil {
		t.Fatalf("OpenSQLiteStore: %v", err)
	}
	t.Cleanup(func() { _ = sqlite.(*SQLiteStore).CloseStore() })
	cachedBacking := NewMemStore()
	stores := map[string]MetadataAnyLister{
		"mem":     NewMemStore(),
		"file":    fileStore,
		"sqlite":  sqlite.(MetadataAnyLister),
		"caching": NewCachingStoreForTest(cachedBacking, nil),
	}
	for name, store := range stores {
		seedMetadataAny(t, name, store.(Store))
	}
	return stores
}

func seedMetadataAny(t *testing.T, name string, store Store) {
	t.Helper()
	rows := []struct {
		id     string
		root   string
		closed bool
	}{
		{"x-a1", "root-a", false},
		{"x-a2", "root-a", true},
		{"x-b1", "root-b", false},
		{"x-c1", "root-c", false},
		{"x-none", "", false}, // holds no root key at all
	}
	for _, row := range rows {
		bead := Bead{ID: row.id, Title: row.id, Metadata: map[string]string{"other": "root-a"}}
		if row.root != "" {
			bead.Metadata["gc.root_bead_id"] = row.root
		}
		created, err := store.Create(bead)
		if err != nil {
			t.Fatalf("%s: create %s: %v", name, row.id, err)
		}
		if row.closed {
			closed := "closed"
			if err := store.Update(created.ID, UpdateOpts{Status: &closed}); err != nil {
				t.Fatalf("%s: close %s: %v", name, row.id, err)
			}
		}
	}
}

func metadataAnySortedIDs(rows []Bead) []string {
	ids := make([]string, 0, len(rows))
	for _, row := range rows {
		ids = append(ids, row.Title)
	}
	sort.Strings(ids)
	return ids
}

// TestListByMetadataAnyIsExactOnEveryStore: the batch returns the beads
// holding the key with one of the values, closed ones only on request, never a
// bead holding the value under a different key, and nothing for an empty value
// set. A store that returned the key-presence superset, or ignored the values,
// fails the first case; one that widened an empty set into a scan fails the last.
func TestListByMetadataAnyIsExactOnEveryStore(t *testing.T) {
	for name, store := range metadataAnyStores(t) {
		t.Run(name, func(t *testing.T) {
			got, err := store.ListByMetadataAny("gc.root_bead_id", []string{"root-a", "root-c", "root-absent"}, 0, IncludeClosed)
			if err != nil {
				t.Fatal(err)
			}
			if want := []string{"x-a1", "x-a2", "x-c1"}; !reflect.DeepEqual(metadataAnySortedIDs(got), want) {
				t.Fatalf("with closed = %v, want %v", metadataAnySortedIDs(got), want)
			}

			got, err = store.ListByMetadataAny("gc.root_bead_id", []string{"root-a", "root-c"}, 0)
			if err != nil {
				t.Fatal(err)
			}
			if want := []string{"x-a1", "x-c1"}; !reflect.DeepEqual(metadataAnySortedIDs(got), want) {
				t.Fatalf("without closed = %v, want %v", metadataAnySortedIDs(got), want)
			}

			got, err = store.ListByMetadataAny("gc.root_bead_id", []string{"root-a", "root-b", "root-c"}, 2, IncludeClosed)
			if err != nil {
				t.Fatal(err)
			}
			if len(got) != 2 {
				t.Fatalf("limit 2 returned %d rows", len(got))
			}

			got, err = store.ListByMetadataAny("gc.root_bead_id", nil, 0, IncludeClosed)
			if err != nil {
				t.Fatal(err)
			}
			if len(got) != 0 {
				t.Fatalf("an empty value set returned %v, want nothing", metadataAnySortedIDs(got))
			}
		})
	}
}

// TestBdStoreListByMetadataAnyPushesKeyPresenceAndCutsValues: bd can filter on
// key presence only, so the one bd call must carry --has-metadata-key, must not
// fan out per value, and the rows it returns for other values must be cut.
func TestBdStoreListByMetadataAnyPushesKeyPresenceAndCutsValues(t *testing.T) {
	var calls [][]string
	runner := func(_, _ string, args ...string) ([]byte, error) {
		calls = append(calls, args)
		return []byte(`[
 {"id":"x-a","title":"x-a","status":"open","issue_type":"task","metadata":{"gc.root_bead_id":"root-a"}},
 {"id":"x-z","title":"x-z","status":"open","issue_type":"task","metadata":{"gc.root_bead_id":"root-z"}}
]`), nil
	}
	s := NewBdStore("/city", runner)
	got, err := s.ListByMetadataAny("gc.root_bead_id", []string{"root-a", "root-b"}, 0, IncludeClosed)
	if err != nil {
		t.Fatal(err)
	}
	if want := []string{"x-a"}; !reflect.DeepEqual(metadataAnySortedIDs(got), want) {
		t.Fatalf("rows = %v, want %v (root-z is not in the value set)", metadataAnySortedIDs(got), want)
	}
	if len(calls) != 1 {
		t.Fatalf("issued %d bd calls, want 1: %v", len(calls), calls)
	}
	args := strings.Join(calls[0], " ")
	for _, want := range []string{"--has-metadata-key gc.root_bead_id", "--all"} {
		if !strings.Contains(args, want) {
			t.Fatalf("args = %q, want %q", args, want)
		}
	}
	if strings.Contains(args, "--metadata-field") || strings.Contains(args, "--limit 50") {
		t.Fatalf("args = %q: the superset read must carry no per-value filter and no default page", args)
	}
}

// TestNativeListRequestPushesOnlyKeyPresenceForMetadataAny: the native request
// has no value-set member, so it carries key presence and no limit, and the
// listing stays a keyed (narrowed) read rather than a full walk.
func TestNativeListRequestPushesOnlyKeyPresenceForMetadataAny(t *testing.T) {
	query := ListByMetadataAnyQuery("gc.root_bead_id", []string{"root-a"}, 5, IncludeClosed, WithBothTiers)
	req := nativeListRequestFromListQuery(query, nil)
	if req.HasMetadataKey != "gc.root_bead_id" {
		t.Fatalf("HasMetadataKey = %q, want the key", req.HasMetadataKey)
	}
	if req.Limit == nil || *req.Limit != 0 {
		t.Fatalf("Limit = %v, want 0: a backing limit would cut rows before the value cut", req.Limit)
	}
	if len(req.MetadataFields) != 0 {
		t.Fatalf("MetadataFields = %v, want none", req.MetadataFields)
	}
	if !NativeListRequestKeyed(req) {
		t.Fatal("a key-presence request must count as keyed, not as a whole-ledger walk")
	}
	if nativeDoltCountSupported(query, nil) {
		t.Fatal("Count must not be answered by the raw count, which cannot apply the value set")
	}
}
