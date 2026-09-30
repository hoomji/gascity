package session

import (
	"testing"

	"github.com/gastownhall/gascity/internal/beads"
)

func TestExportAllMetadataIncludesPersistedCloseTimestamp(t *testing.T) {
	raw := beads.NewMemStore()
	b, err := raw.Create(beads.Bead{Type: BeadType, Labels: []string{LabelSession}, Metadata: map[string]string{"closed_at": "2026-09-29T20:15:00Z", "provider": "dsh-luna", "template": "worker"}})
	if err != nil {
		t.Fatal(err)
	}
	if err := raw.Close(b.ID); err != nil {
		t.Fatal(err)
	}
	rows, err := NewStore(beads.SessionStore{Store: raw}).ExportAllMetadata()
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 || !rows[0].Info.Closed || rows[0].ClosedAt != "2026-09-29T20:15:00Z" {
		t.Fatalf("unexpected metadata: %+v", rows)
	}
}
