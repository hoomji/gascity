package main

import (
	"io"
	"testing"
	"time"

	"github.com/gastownhall/gascity/internal/beadmeta"
	"github.com/gastownhall/gascity/internal/beads"
	"github.com/gastownhall/gascity/internal/config"
)

// The POOL-DEMAND half of reader agreement (#6019), adapted for city-1.5.x:
// upstream's version drives buildDemandTargets (#6961), which this base does
// not have, so these drive the whole desired-state build instead.

func seedRoutedWork(t *testing.T, store beads.Store, target, title string) beads.Bead {
	t.Helper()
	b, err := store.Create(beads.Bead{
		Title:    title,
		Type:     "task",
		Status:   "open",
		Metadata: map[string]string{beadmeta.RoutedToMetadataKey: target},
	})
	if err != nil {
		t.Fatalf("seed routed work: %v", err)
	}
	return b
}

func poolDemandCountsForEnv(e legAgreementEnv) map[string]int {
	state := buildDesiredStateWithSessionBeads(
		e.cityName, e.cityPath, time.Now(), e.cfg, &localMockProvider{},
		e.leading(), e.rigs, &sessionBeadSnapshot{}, nil, io.Discard,
	)
	return state.ScaleCheckCounts
}

func TestPoolScaleCheckCountsRoutedWorkInTheCityWorkLedger(t *testing.T) {
	e := newLegAgreementEnv(t, true)
	e.cfg.Agents = []config.Agent{{Name: "worker", StartCommand: "true", MaxActiveSessions: intPtr(5)}}
	seedRoutedWork(t, e.work, "worker", "work-ledger row")
	seedRoutedWork(t, e.binding, "worker", "binding row")
	if got := poolDemandCountsForEnv(e)["worker"]; got != 2 {
		t.Fatalf("pool demand for worker = %d, want 2 (work-ledger row and binding row are both claimable)", got)
	}
}

func TestPoolScaleCheckDedupsACoResidentRow(t *testing.T) {
	e := newLegAgreementEnv(t, true)
	e.cfg.Agents = []config.Agent{{Name: "worker", StartCommand: "true", MaxActiveSessions: intPtr(5)}}
	e.work.(*beads.MemStore).HonorExplicitIDs = true
	e.binding.(*beads.MemStore).HonorExplicitIDs = true
	for _, store := range []beads.Store{e.work, e.binding} {
		if _, err := store.Create(beads.Bead{
			ID:       "gc-co-1",
			Title:    "co-resident",
			Type:     "task",
			Metadata: map[string]string{beadmeta.RoutedToMetadataKey: "worker"},
		}); err != nil {
			t.Fatalf("seed: %v", err)
		}
	}
	if got := poolDemandCountsForEnv(e)["worker"]; got != 1 {
		t.Fatalf("pool demand for worker = %d, want 1 (one id in both city legs is one unit of demand)", got)
	}
}
