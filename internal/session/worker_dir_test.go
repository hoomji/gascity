package session

import (
	"testing"

	"github.com/gastownhall/gascity/internal/beadmeta"
	"github.com/gastownhall/gascity/internal/beads"
	"github.com/gastownhall/gascity/internal/beads/contract"
)

// TestWorkerDirFromInfoMatchesContract is the reprojection oracle for the
// Info.WorkerDir field-add: WorkerDirFromInfo(infoFromPersistedBead(b)) must be
// byte-identical to contract.WorkerDirFromMetadata(b.Metadata) across the
// canonical/legacy/both/neither/whitespace corpus. It is load-bearing: it fails
// if WorkerDir stops mirroring the canonical worker_dir key, if the legacy
// fallback drops Info.WorkDir, or if the TrimSpace normalization diverges (mutate
// any of those and a fixture row breaks).
func TestWorkerDirFromInfoMatchesContract(t *testing.T) {
	cases := []struct {
		name string
		meta map[string]string
		want string
	}{
		{"canonical-only", map[string]string{beadmeta.WorkerDirMetadataKey: "/w/canon"}, "/w/canon"},
		{"legacy-only", map[string]string{"work_dir": "/w/legacy"}, "/w/legacy"},
		{"both-canonical-wins", map[string]string{beadmeta.WorkerDirMetadataKey: "/w/canon", "work_dir": "/w/legacy"}, "/w/canon"},
		{"neither", map[string]string{}, ""},
		{"canonical-whitespace-falls-back", map[string]string{beadmeta.WorkerDirMetadataKey: "   ", "work_dir": "/w/legacy"}, "/w/legacy"},
		{"canonical-trimmed", map[string]string{beadmeta.WorkerDirMetadataKey: "  /w/canon  "}, "/w/canon"},
		{"legacy-trimmed", map[string]string{"work_dir": "  /w/legacy  "}, "/w/legacy"},
		{"both-whitespace", map[string]string{beadmeta.WorkerDirMetadataKey: "  ", "work_dir": "  "}, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			b := beads.Bead{ID: "s-1", Type: BeadType, Status: "open", Labels: []string{LabelSession}, Metadata: tc.meta}
			info := infoFromPersistedBead(b)
			got := WorkerDirFromInfo(info)
			wantContract := contract.WorkerDirFromMetadata(b.Metadata)
			if got != wantContract {
				t.Fatalf("WorkerDirFromInfo diverged from contract.WorkerDirFromMetadata: got=%q contract=%q", got, wantContract)
			}
			if got != tc.want {
				t.Fatalf("WorkerDirFromInfo(%v) = %q, want %q", tc.meta, got, tc.want)
			}
		})
	}
}

// TestWorkspaceDirFromInfoPrecedence pins the supervisor's folder rule: the
// routed worktree (gc.work_dir) first, then the agent-process cwd
// (worker_dir), then the legacy work_dir — each trimmed, first non-empty
// wins, empty when nothing is recorded. It runs through
// infoFromPersistedBead so the Info mirrors are covered, not just the
// precedence.
func TestWorkspaceDirFromInfoPrecedence(t *testing.T) {
	cases := []struct {
		name string
		meta map[string]string
		want string
	}{
		{"gc-only", map[string]string{beadmeta.WorkDirMetadataKey: "/w/gc"}, "/w/gc"},
		{"worker-only", map[string]string{beadmeta.WorkerDirMetadataKey: "/w/worker"}, "/w/worker"},
		{"legacy-only", map[string]string{"work_dir": "/w/legacy"}, "/w/legacy"},
		{"all-gc-wins", map[string]string{beadmeta.WorkDirMetadataKey: "/w/gc", beadmeta.WorkerDirMetadataKey: "/w/worker", "work_dir": "/w/legacy"}, "/w/gc"},
		{"worker-beats-legacy", map[string]string{beadmeta.WorkerDirMetadataKey: "/w/worker", "work_dir": "/w/legacy"}, "/w/worker"},
		{"gc-whitespace-falls-through", map[string]string{beadmeta.WorkDirMetadataKey: "  ", beadmeta.WorkerDirMetadataKey: "/w/worker", "work_dir": "/w/legacy"}, "/w/worker"},
		{"worker-whitespace-falls-through", map[string]string{beadmeta.WorkerDirMetadataKey: "  ", "work_dir": "/w/legacy"}, "/w/legacy"},
		{"gc-trimmed", map[string]string{beadmeta.WorkDirMetadataKey: "  /w/gc  "}, "/w/gc"},
		{"neither", map[string]string{}, ""},
		{"all-whitespace", map[string]string{beadmeta.WorkDirMetadataKey: " ", beadmeta.WorkerDirMetadataKey: " ", "work_dir": " "}, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			b := beads.Bead{ID: "s-1", Type: BeadType, Status: "open", Labels: []string{LabelSession}, Metadata: tc.meta}
			if got := WorkspaceDirFromInfo(infoFromPersistedBead(b)); got != tc.want {
				t.Fatalf("WorkspaceDirFromInfo(%v) = %q, want %q", tc.meta, got, tc.want)
			}
		})
	}
}
