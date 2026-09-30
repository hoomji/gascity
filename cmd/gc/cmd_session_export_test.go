package main

import (
	"bytes"
	"encoding/json"
	"testing"

	"github.com/gastownhall/gascity/internal/beads"
	"github.com/gastownhall/gascity/internal/session"
)

func TestSessionExportIncludesClosedWithoutBroadeningSnapshot(t *testing.T) {
	store := beads.NewMemStore()
	b, err := store.Create(beads.Bead{Title: "history", Type: session.BeadType, Labels: []string{session.LabelSession}, Metadata: map[string]string{
		"template": "worker", "provider": "dsh-luna", "session_key": "synthetic-key", "work_dir": "/synthetic/repo", "command": "must-not-export",
	}})
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Close(b.ID); err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	if err := exportSessionMetadata(store, &out); err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Sessions []sessionMetadataExport `json:"sessions"`
	}
	if err := json.Unmarshal(out.Bytes(), &doc); err != nil {
		t.Fatal(err)
	}
	if len(doc.Sessions) != 1 || !doc.Sessions[0].Closed || doc.Sessions[0].SessionKey != "synthetic-key" || doc.Sessions[0].Provider != "dsh-luna" {
		t.Fatalf("unexpected export: %+v", doc)
	}
	if bytes.Contains(out.Bytes(), []byte("must-not-export")) {
		t.Fatal("export leaked command")
	}
	snap, err := loadSessionBeadSnapshot(store)
	if err != nil {
		t.Fatal(err)
	}
	if len(snap.openInfos) != 0 {
		t.Fatal("history broadened runtime snapshot")
	}
}
