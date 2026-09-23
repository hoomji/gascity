package main

import (
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gastownhall/gascity/internal/config"
)

// zeroControlServeLockWait removes the acquisition grace window so a contended
// lock refuses immediately instead of stalling the test for the production
// timeout.
func zeroControlServeLockWait(t *testing.T) {
	t.Helper()
	prevWait, prevPoll := controlServeLockWait, controlServeLockPoll
	controlServeLockWait = 0
	controlServeLockPoll = time.Millisecond
	t.Cleanup(func() {
		controlServeLockWait, controlServeLockPoll = prevWait, prevPoll
	})
}

func TestControlServeLockExcludesSecondHolderForSameStream(t *testing.T) {
	zeroControlServeLockWait(t)
	cityDir := t.TempDir()
	stream := "gateway-llm/core.control-dispatcher"

	first, err := acquireControlServeLock(cityDir, stream)
	if err != nil {
		t.Fatalf("first acquire: %v", err)
	}

	_, err = acquireControlServeLock(cityDir, stream)
	if err == nil {
		t.Fatalf("second acquire on the same stream succeeded; want refusal")
	}
	var locked *errControlServeLocked
	if !errors.As(err, &locked) {
		t.Fatalf("second acquire error = %v (%T), want *errControlServeLocked", err, err)
	}
	if locked.HolderPID != os.Getpid() {
		t.Fatalf("lock holder pid = %d, want %d", locked.HolderPID, os.Getpid())
	}

	// Releasing the incumbent must free the stream for the replacement.
	first.Release()
	reacquired, err := acquireControlServeLock(cityDir, stream)
	if err != nil {
		t.Fatalf("acquire after release: %v", err)
	}
	reacquired.Release()
}

func TestControlServeLockKeepsDistinctStreamsIndependent(t *testing.T) {
	zeroControlServeLockWait(t)
	cityDir := t.TempDir()

	a, err := acquireControlServeLock(cityDir, "gateway-llm/core.control-dispatcher")
	if err != nil {
		t.Fatalf("acquire first stream: %v", err)
	}
	defer a.Release()

	b, err := acquireControlServeLock(cityDir, "other-rig/core.control-dispatcher")
	if err != nil {
		t.Fatalf("distinct stream contended with the first: %v", err)
	}
	b.Release()
}

func TestControlServeLockCanonicalizesSymlinkedCityPath(t *testing.T) {
	zeroControlServeLockWait(t)
	realCity := t.TempDir()
	aliasCity := filepath.Join(t.TempDir(), "city-link")
	if err := os.Symlink(realCity, aliasCity); err != nil {
		t.Skipf("symlink unavailable: %v", err)
	}
	stream := "core.control-dispatcher"

	first, err := acquireControlServeLock(realCity, stream)
	if err != nil {
		t.Fatalf("acquire on real path: %v", err)
	}
	defer first.Release()

	_, err = acquireControlServeLock(aliasCity, stream)
	if err == nil {
		t.Fatalf("symlink-equivalent city path did not contend; want refusal")
	}
	var locked *errControlServeLocked
	if !errors.As(err, &locked) {
		t.Fatalf("error = %v (%T), want *errControlServeLocked", err, err)
	}
}

// setupWorkflowServeTestCity writes the minimal control-dispatcher city the
// serve-loop integration tests need and points GC_CITY at it.
func setupWorkflowServeTestCity(t *testing.T) {
	t.Helper()
	clearGCEnv(t)
	disableManagedDoltRecoveryForTest(t)

	cityDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(cityDir, "city.toml"), []byte("[workspace]\nname = \"test-city\"\n\n[daemon]\nformula_v2 = true\n"+testControlDispatcherAgentTOML("")), 0o644); err != nil {
		t.Fatalf("write city.toml: %v", err)
	}
	t.Setenv("GC_CITY", cityDir)
}

// TestRunWorkflowServeRefusesWhenStreamLocked pins the integration: the serve
// loop must acquire the stream lock before it drains anything and surface the
// refusal as the typed error a caller can recognize.
func TestRunWorkflowServeRefusesWhenStreamLocked(t *testing.T) {
	setupWorkflowServeTestCity(t)

	prevCityFlag := cityFlag
	prevAcquire := controlServeLockAcquire
	cityFlag = ""
	var gotStream string
	controlServeLockAcquire = func(_, stream string) (*controlServeLock, error) {
		gotStream = stream
		return nil, &errControlServeLocked{Stream: stream}
	}
	t.Cleanup(func() {
		cityFlag = prevCityFlag
		controlServeLockAcquire = prevAcquire
	})

	err := runWorkflowServe("", false, io.Discard, io.Discard)
	if err == nil {
		t.Fatalf("runWorkflowServe on a locked stream succeeded; want refusal")
	}
	var locked *errControlServeLocked
	if !errors.As(err, &locked) {
		t.Fatalf("runWorkflowServe error = %v (%T), want *errControlServeLocked", err, err)
	}
	if gotStream == "" {
		t.Fatalf("serve loop acquired the stream lock with an empty stream identity")
	}
}

// TestRunWorkflowServeAcquiresLockBeforeDrain pins the positive ordering: on a
// free stream the one-shot serve path must own the lock before it drains, not
// merely refuse when the lock is taken. Both seams are substituted so the test
// observes the order directly and never touches a real stream.
func TestRunWorkflowServeAcquiresLockBeforeDrain(t *testing.T) {
	setupWorkflowServeTestCity(t)

	prevCityFlag := cityFlag
	prevAcquire := controlServeLockAcquire
	prevDrain := drainWorkflowServe
	cityFlag = ""
	var order []string
	controlServeLockAcquire = func(_, _ string) (*controlServeLock, error) {
		order = append(order, "acquire")
		return &controlServeLock{}, nil
	}
	drainWorkflowServe = func(config.Agent, string, string, string, map[string]string, io.Writer) (workflowServeDrainResult, error) {
		order = append(order, "drain")
		return workflowServeDrainResult{}, nil
	}
	t.Cleanup(func() {
		cityFlag = prevCityFlag
		controlServeLockAcquire = prevAcquire
		drainWorkflowServe = prevDrain
	})

	if err := runWorkflowServe("", false, io.Discard, io.Discard); err != nil {
		t.Fatalf("runWorkflowServe: %v", err)
	}
	if got := strings.Join(order, ","); got != "acquire,drain" {
		t.Fatalf("serve call order = %q, want %q", got, "acquire,drain")
	}
}
