package sling

import (
	"testing"

	"github.com/gastownhall/gascity/internal/beadmeta"
	"github.com/gastownhall/gascity/internal/beads"
	"github.com/gastownhall/gascity/internal/config"
	"github.com/gastownhall/gascity/internal/runtime"
)

// slingWorkDirCity builds a single-rig city whose rig checkout the lane agent
// runs in: the Entry-3 shape (lane sessions in the shared rig checkout).
func slingWorkDirCity(rigDir string) *config.City {
	return &config.City{
		Workspace: config.Workspace{Name: "test"},
		Rigs:      []config.Rig{{Name: "testrig", Path: rigDir, Prefix: "gc"}},
	}
}

// TestDoSlingStampsAgentWorkDir pins the sling half of the gc.work_dir stamp:
// a bead routed to a lane whose agent runs in the rig checkout carries that
// checkout as gc.work_dir once routed.
func TestDoSlingStampsAgentWorkDir(t *testing.T) {
	rigDir := t.TempDir()
	cfg := slingWorkDirCity(rigDir)
	a := config.Agent{Name: "lane-a", Dir: rigDir}
	deps := testDeps(cfg, runtime.NewFake(), newFakeRunner().run)

	b, err := deps.Store.Create(beads.Bead{Title: "work", Type: "task"})
	if err != nil {
		t.Fatalf("create bead: %v", err)
	}
	if _, err := DoSling(SlingOpts{Target: a, BeadOrFormula: b.ID}, deps, deps.Store); err != nil {
		t.Fatalf("DoSling: %v", err)
	}
	routed, err := deps.Store.Get(b.ID)
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if got := routed.Metadata[beadmeta.WorkDirMetadataKey]; got != rigDir {
		t.Errorf("gc.work_dir = %q, want the lane checkout %q", got, rigDir)
	}
}

// TestDoSlingKeepsRecordedWorkDir pins the stamp a fallback, never a rewrite:
// a bead that already records a checkout keeps it across a re-sling.
func TestDoSlingKeepsRecordedWorkDir(t *testing.T) {
	rigDir := t.TempDir()
	cfg := slingWorkDirCity(rigDir)
	a := config.Agent{Name: "lane-a", Dir: rigDir}
	deps := testDeps(cfg, runtime.NewFake(), newFakeRunner().run)

	b, err := deps.Store.Create(beads.Bead{Title: "work", Type: "task", Metadata: beads.StringMap{
		beadmeta.WorkDirMetadataKey: "/worktrees/recorded",
	}})
	if err != nil {
		t.Fatalf("create bead: %v", err)
	}
	if _, err := DoSling(SlingOpts{Target: a, BeadOrFormula: b.ID}, deps, deps.Store); err != nil {
		t.Fatalf("DoSling: %v", err)
	}
	routed, err := deps.Store.Get(b.ID)
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if got := routed.Metadata[beadmeta.WorkDirMetadataKey]; got != "/worktrees/recorded" {
		t.Errorf("gc.work_dir = %q, want the recorded checkout kept", got)
	}
}

// TestDoSlingSkipsWorkDirStampOutsideRig pins the prediction boundary: a lane
// whose agent runs outside the rig checkout (per-slot dirs, custom dirs)
// names no folder at sling time, and the claim stamps the actual session dir.
func TestDoSlingSkipsWorkDirStampOutsideRig(t *testing.T) {
	rigDir := t.TempDir()
	cfg := slingWorkDirCity(rigDir)
	a := config.Agent{Name: "lane-slots", Dir: t.TempDir()}
	deps := testDeps(cfg, runtime.NewFake(), newFakeRunner().run)

	b, err := deps.Store.Create(beads.Bead{Title: "work", Type: "task"})
	if err != nil {
		t.Fatalf("create bead: %v", err)
	}
	if _, err := DoSling(SlingOpts{Target: a, BeadOrFormula: b.ID}, deps, deps.Store); err != nil {
		t.Fatalf("DoSling: %v", err)
	}
	routed, err := deps.Store.Get(b.ID)
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if got, ok := routed.Metadata[beadmeta.WorkDirMetadataKey]; ok {
		t.Errorf("gc.work_dir = %q, want no stamp outside the rig checkout", got)
	}
}

// TestDoSlingSkipsWorkDirStampOnPartialEvidence pins the ga-ryeij1.1 Decision
// (b) boundary on the sling stamp, as the claim stamp observes it: a bead
// carrying some but not all worktree-ownership keys must not gain a path,
// which is what first exposes it to worktreeSpecForBead's missing-key error.
func TestDoSlingSkipsWorkDirStampOnPartialEvidence(t *testing.T) {
	rigDir := t.TempDir()
	cfg := slingWorkDirCity(rigDir)
	a := config.Agent{Name: "lane-a", Dir: rigDir}
	deps := testDeps(cfg, runtime.NewFake(), newFakeRunner().run)

	b, err := deps.Store.Create(beads.Bead{Title: "work", Type: "task", Metadata: beads.StringMap{
		beadmeta.WorktreeRepoMetadataKey: "/rigs/some-repo",
	}})
	if err != nil {
		t.Fatalf("create bead: %v", err)
	}
	if _, err := DoSling(SlingOpts{Target: a, BeadOrFormula: b.ID}, deps, deps.Store); err != nil {
		t.Fatalf("DoSling: %v", err)
	}
	routed, err := deps.Store.Get(b.ID)
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if got, ok := routed.Metadata[beadmeta.WorkDirMetadataKey]; ok {
		t.Errorf("gc.work_dir = %q, want no stamp on partial worktree evidence", got)
	}
}

// TestWorktreeEvidenceWholeOrAbsent pins the shared guard: zero or all eight
// ownership keys pass; anything between is half-published evidence.
func TestWorktreeEvidenceWholeOrAbsent(t *testing.T) {
	if !WorktreeEvidenceWholeOrAbsent(nil) {
		t.Error("nil metadata: want whole-or-absent")
	}
	if !WorktreeEvidenceWholeOrAbsent(map[string]string{beadmeta.WorkDirMetadataKey: "/w"}) {
		t.Error("path without ownership keys: want whole-or-absent")
	}
	partial := map[string]string{beadmeta.WorktreeRepoMetadataKey: "/r"}
	if WorktreeEvidenceWholeOrAbsent(partial) {
		t.Error("one ownership key: want half-published")
	}
	full := map[string]string{}
	for _, k := range beadmeta.WorktreeOwnershipEvidenceKeys {
		full[k] = "v"
	}
	if !WorktreeEvidenceWholeOrAbsent(full) {
		t.Error("all ownership keys: want whole-or-absent")
	}
}
