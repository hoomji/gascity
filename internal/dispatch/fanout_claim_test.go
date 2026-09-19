package dispatch

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gastownhall/gascity/internal/beads"
	"github.com/gastownhall/gascity/internal/formulatest"
)

// TestProcessFanoutClaimLosesToConcurrentSpawn pins the CAS loser path: a
// controller holding a pre-claim snapshot whose gc.fanout_state is still ""
// must NOT expand the fanout once another controller has claimed it
// ("spawning"). Before the claim-before-acting guard this path wrote
// SetMetadataBatch unconditionally and every controller that observed the empty
// state duplicated the whole child fragment set — the duplicate-dispatcher
// incident where two convoy serve processes ran one stream for 19h.
func TestProcessFanoutClaimLosesToConcurrentSpawn(t *testing.T) {
	formulatest.EnableV2ForTest(t)

	dir := t.TempDir()
	expansion := `
formula = "expansion-review"
type = "expansion"
version = 2
contract = "graph.v2"

[[template]]
id = "{target}.review"
title = "Review {reviewer}"
`
	if err := os.WriteFile(filepath.Join(dir, "expansion-review.toml"), []byte(expansion), 0o644); err != nil {
		t.Fatalf("write expansion formula: %v", err)
	}

	store := beads.NewMemStore()
	workflow := mustCreateWorkflowBead(t, store, beads.Bead{
		Title: "workflow",
		Type:  "task",
		Metadata: map[string]string{
			"gc.kind":             "workflow",
			"gc.formula_contract": "graph.v2",
		},
	})
	source := mustCreateWorkflowBead(t, store, beads.Bead{
		Title:  "survey",
		Type:   "task",
		Status: "closed",
		Metadata: map[string]string{
			"gc.root_bead_id": workflow.ID,
			"gc.step_ref":     "demo.survey",
			"gc.outcome":      "pass",
			"gc.output_json":  `{"items":[{"name":"claude"}]}`,
		},
	})
	fanout := mustCreateWorkflowBead(t, store, beads.Bead{
		Title: "Expand fanout for survey",
		Type:  "task",
		Metadata: map[string]string{
			"gc.kind":         "fanout",
			"gc.root_bead_id": workflow.ID,
			"gc.control_for":  "demo.survey",
			"gc.for_each":     "output.items",
			"gc.bond":         "expansion-review",
			"gc.bond_vars":    `{"reviewer":"{item.name}"}`,
			"gc.fanout_mode":  "parallel",
		},
	})
	mustDepAdd(t, store, fanout.ID, source.ID, "blocks")

	// Snapshot BEFORE the concurrent controller claims, so this controller's
	// bead still reads gc.fanout_state == "".
	stale := mustGetBead(t, store, fanout.ID)
	if err := store.SetMetadataBatch(fanout.ID, map[string]string{"gc.fanout_state": "spawning"}); err != nil {
		t.Fatalf("simulate concurrent claim: %v", err)
	}

	_, err := ProcessControl(store, stale, ProcessOptions{FormulaSearchPaths: []string{dir}})
	if !errors.Is(err, ErrControlPending) {
		t.Fatalf("ProcessControl(stale fanout) error = %v, want %v (loser must skip)", err, ErrControlPending)
	}

	after := mustGetBead(t, store, fanout.ID)
	if got := after.Metadata["gc.fanout_state"]; got != "spawning" {
		t.Fatalf("fanout state after losing claim = %q, want spawning (untouched)", got)
	}
	members, err := beads.DirectMembers(store, workflow.ID)
	if err != nil {
		t.Fatalf("DirectMembers: %v", err)
	}
	for _, member := range members {
		if member.ID == source.ID || member.ID == fanout.ID {
			continue
		}
		if strings.HasSuffix(member.Metadata["gc.step_ref"], ".review") {
			t.Fatalf("loser spawned child %s (%s); the claim must prevent duplicate expansion", member.ID, member.Metadata["gc.step_ref"])
		}
	}
}
