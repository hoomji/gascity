package session

import "github.com/gastownhall/gascity/internal/beads"

// ExportMetadata is a persisted session projection with an optional historical
// close timestamp. The timestamp is blank when the original bead did not stamp it.
type ExportMetadata struct {
	Info     Info
	ClosedAt string
}

// ExportAllMetadata is a cold, explicit history read, not a reconciliation feed.
// It uses one union scan rather than issuing a Get for each historical bead.
func (s *Store) ExportAllMetadata() ([]ExportMetadata, error) {
	items, err := s.listAllBeads(ListAllOptions{IncludeClosed: true, Sort: beads.SortCreatedAsc})
	if err != nil {
		return nil, err
	}
	rows := make([]ExportMetadata, 0, len(items))
	for _, b := range items {
		closedAt := ""
		if b.Status == "closed" {
			closedAt = b.Metadata["closed_at"]
		}
		rows = append(rows, ExportMetadata{Info: infoFromPersistedBead(b), ClosedAt: closedAt})
	}
	return rows, nil
}
