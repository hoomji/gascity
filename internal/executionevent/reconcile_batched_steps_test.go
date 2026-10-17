package executionevent

import (
	"reflect"
	"sort"
	"testing"

	"github.com/gastownhall/gascity/internal/beadmeta"
	"github.com/gastownhall/gascity/internal/beads"
	"github.com/gastownhall/gascity/internal/beads/beadstest"
	"github.com/gastownhall/gascity/internal/events"
)

// hidingCapabilityStore embeds the Store interface, which hides every optional
// capability of the store it wraps: it is the per-root path's fixture, the shape
// a store without MetadataAnyLister presents.
type hidingCapabilityStore struct{ beads.Store }

// stepListings counts the listings a sweep issued that read step rows, whether
// asked for one root at a time or for a whole chunk at once.
func stepListings(ops []beadstest.RecordedOp) (perRoot, batched int) {
	for _, op := range ops {
		if op.Kind != beadstest.OpList {
			continue
		}
		if _, ok := op.Query.Metadata[beadmeta.RootBeadIDMetadataKey]; ok {
			perRoot++
		}
		if op.Query.MetadataAny.Key == beadmeta.RootBeadIDMetadataKey {
			batched++
		}
	}
	return perRoot, batched
}

// mixedCompletionCorpus is closed roots with varied step shapes: a closed step,
// a still-open step, a row that is not a projected step (no step id), and a
// root with no step rows at all. Both paths must treat each the same way.
func mixedCompletionCorpus(t *testing.T, roots int) (beads.Store, []string) {
	t.Helper()
	backing := beads.NewMemStore()
	closed := "closed"
	ids := make([]string, 0, roots)
	for r := range roots {
		root := mustCreateProjectionRoot(t, backing, "")
		ids = append(ids, root.ID)
		if err := backing.Update(root.ID, beads.UpdateOpts{Status: &closed}); err != nil {
			t.Fatalf("close root %s: %v", root.ID, err)
		}
		switch r % 4 {
		case 0:
			step := mustCreateProjectionStep(t, backing, stepBeadID(r, 0), root.ID, "build", "[]")
			closeStepWithSession(t, backing, step.ID)
		case 1:
			step := mustCreateProjectionStep(t, backing, stepBeadID(r, 0), root.ID, "build", "[]")
			closeStepWithSession(t, backing, step.ID)
			mustCreateProjectionStep(t, backing, stepBeadID(r, 1), root.ID, "test", "[]")
		case 2:
			step := mustCreateProjectionStep(t, backing, stepBeadID(r, 0), root.ID, "build", "[]")
			closeStepWithSession(t, backing, step.ID)
			mustCreateProjectionBead(t, backing, beads.Bead{
				ID:       stepBeadID(r, 1),
				Metadata: map[string]string{beadmeta.RootBeadIDMetadataKey: root.ID},
			})
		}
	}
	return backing, ids
}

func closeStepWithSession(t *testing.T, store beads.Store, id string) {
	t.Helper()
	closed := "closed"
	if err := store.Update(id, beads.UpdateOpts{
		Status:   &closed,
		Metadata: map[string]string{beadmeta.SessionIDMetadataKey: "gcs-session"},
	}); err != nil {
		t.Fatalf("close step %s: %v", id, err)
	}
}

func subjectsOf(journal *events.Fake) []string {
	subjects := make([]string, 0, len(journal.Events))
	for _, event := range journal.Events {
		subjects = append(subjects, event.Type+" "+event.Subject+" "+event.RunID+" "+event.StepID)
	}
	sort.Strings(subjects)
	return subjects
}

func stampsOf(t *testing.T, store beads.Store, rootIDs []string) map[string]bool {
	t.Helper()
	stamps := make(map[string]bool, len(rootIDs))
	for _, id := range rootIDs {
		root, err := store.Get(id)
		if err != nil {
			t.Fatal(err)
		}
		stamps[id] = root.Metadata[beadmeta.CompletionFactsConvergedMetadataKey] != ""
	}
	return stamps
}

// TestReconcileRootsReadsAChunkOfStepsInOneListing is the startup-sweep cost:
// a chunk of 64 roots was 64 listings, each a full metadata scan on a ledger
// that cannot index the value. The negative is the listing count; the control
// is the emitted-fact count, which a pass that read nothing would fail.
func TestReconcileRootsReadsAChunkOfStepsInOneListing(t *testing.T) {
	const roots = 64
	backing, rootIDs := mixedCompletionCorpus(t, roots)
	graph := beadstest.NewOpRecordingStore(backing)
	rootBeads, err := backing.List(beads.ListQuery{IDs: rootIDs, IncludeClosed: true, TierMode: beads.TierBoth})
	if err != nil || len(rootBeads) != roots {
		t.Fatalf("loading roots: %v (%d found)", err, len(rootBeads))
	}

	var index CompletedFactIndex
	journal := events.NewFake()
	if !index.warm(journal) {
		t.Fatal("warm the empty journal")
	}
	emitted := reconcileRoots(journal, beads.GraphStore{Store: graph}, rootBeads, &index, "execution-reconcile")

	// roots 0 mod 4 carry one closed step, 1 and 2 mod 4 one closed step beside
	// an open or non-step row; 3 mod 4 carries none.
	if want := roots * 3 / 4; emitted != want {
		t.Fatalf("emitted %d completion facts, want %d: the listing count below would be met by a pass that read nothing", emitted, want)
	}
	perRoot, batched := stepListings(graph.Ops())
	if batched != 1 || perRoot != 0 {
		t.Fatalf("a %d-root chunk issued %d per-root and %d batched step listing(s), want 0 and 1", roots, perRoot, batched)
	}
}

// TestReconcileRootsBatchedListingMatchesPerRoot holds the semantics fixed: the
// same corpus swept through a store that offers the batch and one that hides
// it must emit the same facts and write the same stamps, across the sweeps
// that emit, verify and finally skip.
func TestReconcileRootsBatchedListingMatchesPerRoot(t *testing.T) {
	const roots = 70
	run := func(wrap func(beads.Store) beads.Store) ([][]string, []map[string]bool, *beadstest.OpRecordingStore) {
		backing, rootIDs := mixedCompletionCorpus(t, roots)
		recording := beadstest.NewOpRecordingStore(backing)
		stores := []beads.GraphStore{{Store: wrap(recording)}}
		journal := events.NewFake()
		var facts [][]string
		var stamps []map[string]bool
		for range 3 {
			backstop := &CompletionBackstop{ChunkSize: 64, VisitStamped: true}
			for {
				if result := backstop.Pass(journal, stores, "execution-reconcile"); result.SweepComplete {
					break
				}
			}
			facts = append(facts, subjectsOf(journal))
			stamps = append(stamps, stampsOf(t, backing, rootIDs))
		}
		return facts, stamps, recording
	}

	batchedFacts, batchedStamps, batchedOps := run(func(s beads.Store) beads.Store { return s })
	perRootFacts, perRootStamps, perRootOps := run(func(s beads.Store) beads.Store { return hidingCapabilityStore{s} })

	if !reflect.DeepEqual(batchedFacts, perRootFacts) {
		t.Fatalf("emitted facts differ between the batched and per-root paths:\nbatched  %v\nper-root %v", batchedFacts, perRootFacts)
	}
	if !reflect.DeepEqual(batchedStamps, perRootStamps) {
		t.Fatalf("stamps differ between the batched and per-root paths:\nbatched  %v\nper-root %v", batchedStamps, perRootStamps)
	}
	// Controls: the corpus really exercised emitting and stamping, and the two
	// paths really were different paths.
	if len(batchedFacts[0]) == 0 || len(batchedFacts[0]) != len(batchedFacts[2]) {
		t.Fatalf("facts per sweep = %d then %d, want the first sweep to emit everything and the rest to add none", len(batchedFacts[0]), len(batchedFacts[2]))
	}
	stamped := 0
	for _, isStamped := range batchedStamps[2] {
		if isStamped {
			stamped++
		}
	}
	if stamped == 0 {
		t.Fatal("no root was stamped by the third sweep, so the stamp comparison above proved nothing")
	}
	if perRoot, batched := stepListings(batchedOps.Ops()); perRoot != 0 || batched == 0 {
		t.Fatalf("capability store issued %d per-root and %d batched listing(s), want only batched", perRoot, batched)
	}
	if perRoot, batched := stepListings(perRootOps.Ops()); perRoot == 0 || batched != 0 {
		t.Fatalf("capability-hidden store issued %d per-root and %d batched listing(s), want only per-root", perRoot, batched)
	}
}
