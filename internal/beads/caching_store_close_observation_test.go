package beads

import (
	"context"
	"encoding/json"
	"testing"
)

// A live read must not consume the terminal transition before the background
// reconciler can publish it: convoy completion subscribes to this notification.
func TestCachingStoreReadPublishesObservedClose(t *testing.T) {
	for _, mode := range []string{"live-history", "live-active", "parent-history", "parent-active", "dirty-get"} {
		t.Run(mode, func(t *testing.T) {
			backing := NewMemStore()
			b, err := backing.Create(Bead{Title: "external close", ParentID: "parent"})
			if err != nil {
				t.Fatal(err)
			}
			var got []string
			cache := NewCachingStore(backing, func(kind, id, _, _, _ string, _ *[]string, _ json.RawMessage) {
				got = append(got, kind+":"+id)
			})
			if err := cache.Prime(context.Background()); err != nil {
				t.Fatal(err)
			}
			if err := backing.Close(b.ID); err != nil {
				t.Fatal(err)
			}
			read := func() {
				var err error
				switch mode {
				case "dirty-get":
					cache.mu.Lock()
					cache.dirty[b.ID] = struct{}{}
					cache.mu.Unlock()
					_, err = cache.Get(b.ID)
				default:
					q := ListQuery{AllowScan: true, Live: true}
					if mode == "live-history" || mode == "parent-history" {
						q.IncludeClosed = true
					}
					if mode == "parent-history" || mode == "parent-active" {
						q.ParentID = "parent"
						q.Live = false
					}
					_, err = cache.List(q)
				}
				if err != nil {
					t.Fatal(err)
				}
			}
			read()
			read()
			cache.runReconciliation()
			if len(got) != 1 || got[0] != "bead.closed:"+b.ID {
				t.Fatalf("notifications = %v, want one bead.closed for %s", got, b.ID)
			}
		})
	}
}
