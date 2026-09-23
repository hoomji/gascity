package main

import (
	"bytes"
	"errors"
	"testing"

	"github.com/gastownhall/gascity/internal/beads"
	"github.com/gastownhall/gascity/internal/runtime"
)

// stubStaleBinary forces the stale-binary probe true for the duration of a test.
func stubStaleBinary(t *testing.T, stale bool) {
	t.Helper()
	prev := processRuntimeBinaryStale
	processRuntimeBinaryStale = func(int) bool { return stale }
	t.Cleanup(func() { processRuntimeBinaryStale = prev })
}

func staleControllerStore(t *testing.T, template string) (beads.Store, beads.Bead) {
	t.Helper()
	store := beads.NewMemStoreFrom(0, []beads.Bead{
		{ID: "gm-controller", Status: "open", Metadata: map[string]string{"template": template}},
	}, nil)
	bead, err := store.Get("gm-controller")
	if err != nil {
		t.Fatalf("store.Get: %v", err)
	}
	return store, bead
}

// TestRequestStaleControllerRuntimeRestartSkipsRestartWhenTerminateFails pins the
// F2 fix: an untracked stale controller that could not be terminated must not
// have restart_requested set, or the reconciler would start a replacement while
// the duplicate keeps serving the stream.
func TestRequestStaleControllerRuntimeRestartSkipsRestartWhenTerminateFails(t *testing.T) {
	stubStaleBinary(t, true)
	store, bead := staleControllerStore(t, "core.control-dispatcher")

	live := runtime.LiveRuntime{SessionID: "gm-controller", PID: 4101, IsTracked: false}
	sp := newProcessTableSweepProvider(live)
	sp.terminateErr[live.PID] = errors.New("terminate failed")

	var stderr bytes.Buffer
	requestStaleControllerRuntimeRestart(sp, store, live, bead, &stderr)

	got, err := store.Get("gm-controller")
	if err != nil {
		t.Fatalf("store.Get: %v", err)
	}
	if got.Metadata["restart_requested"] == "true" {
		t.Fatalf("restart_requested = true after a failed terminate; want unset; stderr=%q", stderr.String())
	}
	if len(sp.terminated) != 0 {
		t.Fatalf("terminated = %v, want none", sp.terminated)
	}
}

// TestRequestStaleControllerRuntimeRestartSetsRestartWhenTerminateSucceeds keeps
// the positive arm honest: a terminated duplicate still gets the restart request.
func TestRequestStaleControllerRuntimeRestartSetsRestartWhenTerminateSucceeds(t *testing.T) {
	stubStaleBinary(t, true)
	store, bead := staleControllerStore(t, "gateway-llm/core.control-dispatcher")

	live := runtime.LiveRuntime{SessionID: "gm-controller", PID: 4102, IsTracked: false}
	sp := newProcessTableSweepProvider(live)

	var stderr bytes.Buffer
	requestStaleControllerRuntimeRestart(sp, store, live, bead, &stderr)

	got, err := store.Get("gm-controller")
	if err != nil {
		t.Fatalf("store.Get: %v", err)
	}
	if got.Metadata["restart_requested"] != "true" {
		t.Fatalf("restart_requested = %q, want true; stderr=%q", got.Metadata["restart_requested"], stderr.String())
	}
	if ids := terminatedSessionIDs(sp.terminated); ids != "gm-controller" {
		t.Fatalf("terminated = %q, want gm-controller", ids)
	}
}

// TestRequestStaleControllerRuntimeRestartKeepsTrackedRuntime pins that a
// provider-owned controller is never terminated here; only the restart is
// requested so the reconciler swaps it on the current binary.
func TestRequestStaleControllerRuntimeRestartKeepsTrackedRuntime(t *testing.T) {
	stubStaleBinary(t, true)
	store, bead := staleControllerStore(t, "core.control-dispatcher")

	live := runtime.LiveRuntime{SessionID: "gm-controller", PID: 4103, IsTracked: true}
	sp := newProcessTableSweepProvider(live)

	var stderr bytes.Buffer
	requestStaleControllerRuntimeRestart(sp, store, live, bead, &stderr)

	if len(sp.terminated) != 0 {
		t.Fatalf("tracked controller terminated = %v, want none", sp.terminated)
	}
	got, err := store.Get("gm-controller")
	if err != nil {
		t.Fatalf("store.Get: %v", err)
	}
	if got.Metadata["restart_requested"] != "true" {
		t.Fatalf("restart_requested = %q, want true; stderr=%q", got.Metadata["restart_requested"], stderr.String())
	}
}

// TestRequestStaleControllerRuntimeRestartIgnoresHealthyController ensures the
// fresh-binary path never mutates the bead.
func TestRequestStaleControllerRuntimeRestartIgnoresHealthyController(t *testing.T) {
	stubStaleBinary(t, false)
	store, bead := staleControllerStore(t, "core.control-dispatcher")

	live := runtime.LiveRuntime{SessionID: "gm-controller", PID: 4104, IsTracked: true}
	sp := newProcessTableSweepProvider(live)

	var stderr bytes.Buffer
	requestStaleControllerRuntimeRestart(sp, store, live, bead, &stderr)

	got, err := store.Get("gm-controller")
	if err != nil {
		t.Fatalf("store.Get: %v", err)
	}
	if got.Metadata["restart_requested"] == "true" {
		t.Fatalf("healthy controller got restart_requested; want unset")
	}
	if len(sp.terminated) != 0 {
		t.Fatalf("healthy controller terminated = %v, want none", sp.terminated)
	}
}
