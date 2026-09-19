package main

import (
	"bytes"
	"io"
	"strings"
	"testing"

	"github.com/gastownhall/gascity/internal/config"
	"github.com/gastownhall/gascity/internal/events"
)

// TestWorkflowServeFollowLockExcludesSecondServer pins the exclusive lifetime
// flock: a second acquire on the same stream conflicts, while a different
// stream or a different city is independent. The lock is never released by
// design, so the holder stays pinned for the process lifetime.
func TestWorkflowServeFollowLockExcludesSecondServer(t *testing.T) {
	city := t.TempDir()

	held, err := acquireWorkflowServeFollowLock(city, "control-dispatcher")
	if err != nil || !held {
		t.Fatalf("first acquire = (%v, %v), want held", held, err)
	}
	held, err = acquireWorkflowServeFollowLock(city, "control-dispatcher")
	if err != nil {
		t.Fatalf("second acquire error = %v", err)
	}
	if held {
		t.Fatal("second acquire on the same stream = held, want conflict")
	}

	// A rig-qualified dispatcher is a different stream in the same city.
	if held, err := acquireWorkflowServeFollowLock(city, "app/control-dispatcher"); err != nil || !held {
		t.Fatalf("rig stream acquire = (%v, %v), want held", held, err)
	}
	// The same dispatcher name in another city is a different stream.
	if held, err := acquireWorkflowServeFollowLock(t.TempDir(), "control-dispatcher"); err != nil || !held {
		t.Fatalf("other-city acquire = (%v, %v), want held", held, err)
	}
}

// TestRunWorkflowServeFollowSecondServerExitsCleanly covers the conflict path
// end to end: with the stream already held, runWorkflowServeFollow must not
// open the event provider, must not drain, must write exactly one line, and
// must return nil so the process exits 0 rather than looking like a crash.
func TestRunWorkflowServeFollowSecondServerExitsCleanly(t *testing.T) {
	city := t.TempDir()
	if held, err := acquireWorkflowServeFollowLock(city, "control-dispatcher"); err != nil || !held {
		t.Fatalf("prime lock = (%v, %v), want held", held, err)
	}

	prevProvider := workflowServeOpenEventsProvider
	t.Cleanup(func() { workflowServeOpenEventsProvider = prevProvider })
	opened := false
	workflowServeOpenEventsProvider = func(io.Writer) (events.Provider, error) {
		opened = true
		return nil, io.EOF
	}

	var stderr bytes.Buffer
	err := runWorkflowServeFollow(
		config.Agent{Name: "control-dispatcher"},
		city,
		t.TempDir(),
		"",
		nil,
		&stderr,
	)
	if err != nil {
		t.Fatalf("runWorkflowServeFollow on a contended stream error = %v, want nil (exit 0)", err)
	}
	if opened {
		t.Fatal("contended server opened the event provider; it must not start")
	}
	if got := strings.Count(strings.TrimRight(stderr.String(), "\n"), "\n"); got != 0 {
		t.Fatalf("stderr = %q, want exactly one line", stderr.String())
	}
	if !strings.Contains(stderr.String(), "already owns the stream lock") {
		t.Fatalf("stderr = %q, want the one-line conflict notice", stderr.String())
	}
}
