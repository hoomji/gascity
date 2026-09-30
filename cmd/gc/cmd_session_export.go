package main

import (
	"encoding/json"
	"fmt"
	"io"
	"time"

	"github.com/gastownhall/gascity/internal/beads"
)

// sessionMetadataExport is intentionally a metadata-only allowlist: never commands,
// environment variables, prompts or transcript paths.
type sessionMetadataExport struct {
	ID         string    `json:"id"`
	Template   string    `json:"template"`
	Provider   string    `json:"provider"`
	SessionKey string    `json:"session_key"`
	WorkDir    string    `json:"work_dir"`
	CreatedAt  time.Time `json:"created_at"`
	Closed     bool      `json:"closed"`
	ClosedAt   string    `json:"closed_at,omitempty"`
}

func exportSessionMetadata(store beads.Store, out io.Writer) error {
	// This explicit, cold export includes history without changing the hot
	// reconciliation snapshot's open-only contract.
	infos, err := sessionFrontDoor(store).ExportAllMetadata()
	if err != nil {
		return err
	}
	rows := make([]sessionMetadataExport, 0, len(infos))
	for _, metadata := range infos {
		info := metadata.Info
		closedAt := metadata.ClosedAt
		rows = append(rows, sessionMetadataExport{
			ID: info.ID, Template: info.Template,
			Provider: info.Provider, SessionKey: info.SessionKey, WorkDir: info.WorkDir,
			CreatedAt: info.CreatedAt, Closed: info.Closed, ClosedAt: closedAt,
		})
	}
	return json.NewEncoder(out).Encode(struct {
		Sessions []sessionMetadataExport `json:"sessions"`
	}{rows})
}

func runSessionMetadataExport(stdout, stderr io.Writer) error {
	store, _ := openCityStore(stderr, "gc session list --export")
	if store == nil {
		return errExit
	}
	ctx := loadSessionProviderContext()
	if err := exportSessionMetadata(cliSessionStore(store, ctx.cfg, ctx.cityPath), stdout); err != nil {
		fmt.Fprintf(stderr, "gc session list --export: %v\n", err) //nolint:errcheck // best-effort diagnostic
		return errExit
	}
	return nil
}
