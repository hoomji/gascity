package main

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gastownhall/gascity/internal/beads"
	"github.com/gastownhall/gascity/internal/config"
	"github.com/gastownhall/gascity/internal/sling"
)

func liveRoutingConfig(command, timeout string) *config.City {
	cfg := &config.City{}
	cfg.Observatory.LiveRouting.Command = command
	cfg.Observatory.LiveRouting.Timeout = timeout
	return cfg
}

func TestRecordLiveRoutingAdvisoryDisabledIsNoOp(t *testing.T) {
	dir := t.TempDir()
	witness := filepath.Join(dir, "witness")
	cfg := liveRoutingConfig("", "")
	recordLiveRoutingAdvisory(cfg, liveRoutingDispatch{BeadID: "gl-1", ActualRoute: "agent/x"})
	if _, err := os.Stat(witness); !os.IsNotExist(err) {
		t.Fatalf("disabled hook wrote output: stat %s = %v", witness, err)
	}
}

func TestRecordLiveRoutingAdvisorySendsDispatchJSON(t *testing.T) {
	dir := t.TempDir()
	witness := filepath.Join(dir, "payload.json")
	cfg := liveRoutingConfig("cat > "+witness, "5s")
	recordLiveRoutingAdvisory(cfg, liveRoutingDispatch{
		BeadID:      "gl-abc123",
		ActualRoute: "agent/planner",
		Target:      "agent/planner",
		CityName:    "testcity",
	})
	raw, err := os.ReadFile(witness)
	if err != nil {
		t.Fatalf("hook did not run: %v", err)
	}
	var payload map[string]string
	if err := json.Unmarshal(raw, &payload); err != nil {
		t.Fatalf("payload is not JSON: %v (%s)", err, raw)
	}
	if payload["bead_id"] != "gl-abc123" || payload["actual_route"] != "agent/planner" {
		t.Fatalf("unexpected payload: %v", payload)
	}
	if payload["dispatch_id"] != "gl-abc123" {
		t.Fatalf("dispatch_id should default to the bead id, got %q", payload["dispatch_id"])
	}
	if payload["recorded_at"] == "" {
		t.Fatalf("recorded_at must be stamped: %v", payload)
	}
}

func TestRecordLiveRoutingAdvisoryFailureIsFailOpen(t *testing.T) {
	cfg := liveRoutingConfig("exit 7", "5s")
	done := make(chan struct{})
	go func() {
		defer close(done)
		recordLiveRoutingAdvisory(cfg, liveRoutingDispatch{BeadID: "gl-1", ActualRoute: "agent/x"})
	}()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("advisory hook did not return after a failing command")
	}
}

func TestRecordLiveRoutingAdvisoryTimeoutIsBounded(t *testing.T) {
	cfg := liveRoutingConfig("sleep 5", "150ms")
	start := time.Now()
	recordLiveRoutingAdvisory(cfg, liveRoutingDispatch{BeadID: "gl-1", ActualRoute: "agent/x"})
	if elapsed := time.Since(start); elapsed > 3*time.Second {
		t.Fatalf("advisory hook exceeded its timeout: %s", elapsed)
	}
}

func TestLiveRoutingConfigTimeoutDefaultsAndFloors(t *testing.T) {
	if got := (config.LiveRoutingConfig{}).TimeoutDuration(); got != config.DefaultObservatoryLiveRoutingTimeout {
		t.Fatalf("default timeout = %s, want %s", got, config.DefaultObservatoryLiveRoutingTimeout)
	}
	floored := config.LiveRoutingConfig{Timeout: "1ms"}.TimeoutDuration()
	if floored < 100*time.Millisecond {
		t.Fatalf("tiny timeout was not floored: %s", floored)
	}
	if !(config.LiveRoutingConfig{Command: " true "}).Enabled() {
		t.Fatal("a set command should enable the hook")
	}
	if (config.LiveRoutingConfig{Command: "  "}).Enabled() {
		t.Fatal("a whitespace-only command should stay disabled")
	}
}

func TestCLIBeadRouterAdvisoryNeverChangesTheRoute(t *testing.T) {
	store := beads.NewMemStore()
	bead, err := store.Create(beads.Bead{Title: "route me"})
	if err != nil {
		t.Fatalf("create bead: %v", err)
	}

	dir := t.TempDir()
	witness := filepath.Join(dir, "payload.json")
	router := cliBeadRouter{deps: &slingDeps{
		Cfg:   liveRoutingConfig("cat > "+witness, "5s"),
		Store: store,
	}}
	if err := router.Route(context.Background(), sling.RouteRequest{BeadID: bead.ID, Target: "agent/planner"}); err != nil {
		t.Fatalf("route: %v", err)
	}
	stored, err := store.Get(bead.ID)
	if err != nil {
		t.Fatalf("get bead: %v", err)
	}
	if got := stored.Metadata["gc.routed_to"]; got != "agent/planner" {
		t.Fatalf("gc.routed_to = %q, want agent/planner", got)
	}
	raw, err := os.ReadFile(witness)
	if err != nil {
		t.Fatalf("advisory hook did not run: %v", err)
	}
	var payload map[string]string
	if err := json.Unmarshal(raw, &payload); err != nil {
		t.Fatalf("payload is not JSON: %v", err)
	}
	if payload["actual_route"] != "agent/planner" {
		t.Fatalf("advisory actual_route = %q, want the persisted route", payload["actual_route"])
	}
}

func TestCLIBeadRouterDisabledAdvisoryLeavesRouteIdentical(t *testing.T) {
	store := beads.NewMemStore()
	bead, err := store.Create(beads.Bead{Title: "route me"})
	if err != nil {
		t.Fatalf("create bead: %v", err)
	}
	router := cliBeadRouter{deps: &slingDeps{
		Cfg:   liveRoutingConfig("", ""),
		Store: store,
	}}
	if err := router.Route(context.Background(), sling.RouteRequest{BeadID: bead.ID, Target: "agent/planner"}); err != nil {
		t.Fatalf("route: %v", err)
	}
	stored, err := store.Get(bead.ID)
	if err != nil {
		t.Fatalf("get bead: %v", err)
	}
	if got := stored.Metadata["gc.routed_to"]; got != "agent/planner" {
		t.Fatalf("gc.routed_to = %q, want agent/planner", got)
	}
	if strings.TrimSpace(stored.Metadata["gc.execution_routed_to"]) != "" {
		t.Fatalf("disabled advisory added metadata: %v", stored.Metadata)
	}
}
