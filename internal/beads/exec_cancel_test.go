package beads

import (
	"context"
	"os"
	"testing"
	"time"
)

// TestRunExecCommandKillsChildOnCancel proves the exec layer kills the child
// process when the caller's context dies: `sleep 30` must end within seconds
// of cancel with an error, not run its 30s course. This is the mechanism the
// request-scoped ListContext path relies on to stop abandoned bd children.
func TestRunExecCommandKillsChildOnCancel(t *testing.T) {
	if _, err := os.Stat("/bin/sleep"); err != nil {
		t.Skip("no /bin/sleep to kill")
	}
	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(200 * time.Millisecond)
		cancel()
	}()
	start := time.Now()
	done := make(chan error, 1)
	go func() {
		_, err := runExecCommand(ctx, t.TempDir(), "sleep", []string{"30"}, nil, false, os.Environ)
		done <- err
	}()
	select {
	case err := <-done:
		elapsed := time.Since(start)
		if err == nil {
			t.Fatal("canceled sleep returned nil error, want a kill/cancel failure")
		}
		if elapsed > 15*time.Second {
			t.Fatalf("canceled sleep ran %s, want the child killed promptly", elapsed)
		}
	case <-time.After(20 * time.Second):
		t.Fatal("sleep 30 survived cancel; the exec layer is not killing the child")
	}
}
