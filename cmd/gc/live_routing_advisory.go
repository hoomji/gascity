package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"time"

	"github.com/gastownhall/gascity/internal/config"
)

// liveRoutingDispatch is the advisory payload the routing call site sends to
// the operator-configured command on stdin. It mirrors the Python
// `canary-live-route` single-dispatch request.
type liveRoutingDispatch struct {
	DispatchID  string `json:"dispatch_id"`
	BeadID      string `json:"bead_id"`
	ActualRoute string `json:"actual_route"`
	RecordedAt  string `json:"recorded_at"`
	Target      string `json:"target,omitempty"`
	CityName    string `json:"city_name,omitempty"`
}

// recordLiveRoutingAdvisory invokes the M8b advisory live-routing hook.
//
// It is intentionally tiny and powerless: the operator-configured command
// records Jev's primary_intent and the route the M7 shadow policy would suggest
// next to the route that was actually taken, and its output is discarded. The
// hook never returns an error and never mutates the route.
//
// When the [observatory.live_routing] command is unset (the default) this
// function returns immediately: no subprocess, no output, no timing change, so
// the routing path is byte-identical to a build without the hook. When it is
// set, a command failure or timeout is fail-open and only logged to stderr.
func recordLiveRoutingAdvisory(cfg *config.City, dispatch liveRoutingDispatch) {
	if cfg == nil {
		return
	}
	live := cfg.Observatory.LiveRouting
	if !live.Enabled() {
		return
	}
	if strings.TrimSpace(dispatch.DispatchID) == "" {
		dispatch.DispatchID = dispatch.BeadID
	}
	if strings.TrimSpace(dispatch.RecordedAt) == "" {
		dispatch.RecordedAt = time.Now().UTC().Format(time.RFC3339Nano)
	}
	payload, err := json.Marshal(dispatch)
	if err != nil {
		fmt.Fprintf(os.Stderr, "gc: observatory live-routing advisory payload failed (ignored): %v\n", err) //nolint:errcheck // advisory stderr line
		return
	}

	ctx, cancel := context.WithTimeout(context.Background(), live.TimeoutDuration())
	defer cancel()
	cmd := exec.CommandContext(ctx, "sh", "-c", live.Command)
	cmd.Stdin = bytes.NewReader(payload)
	// WaitDelay bounds a child that ignores cancellation so a wedged advisor
	// cannot strand the routing call site even momentarily.
	cmd.WaitDelay = 500 * time.Millisecond
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		// Fail-open: the route is already durably written and this hook is
		// advisory, so a broken advisor must never surface as a route failure.
		fmt.Fprintf(os.Stderr, "gc: observatory live-routing advisory failed (ignored): %v\n", err) //nolint:errcheck // advisory stderr line
	}
}
