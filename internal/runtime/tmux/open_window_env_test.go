package tmux

import (
	"strings"
	"testing"
	"time"

	"github.com/gastownhall/gascity/internal/beads"
)

// A tmux client started while a native Dolt open window is projecting its env
// must not inherit the projection: the tmux server it may start would keep it
// as the global environment of every later session.
func TestTmuxCommandEnvExcludesNativeDoltOpenWindow(t *testing.T) {
	t.Setenv("BEADS_DOLT_SERVER_DATABASE", "rig-db")

	release, err := beads.HoldNativeDoltOpenEnvWindow(map[string]string{
		"BEADS_DOLT_SERVER_DATABASE": "hq",
	})
	if err != nil {
		t.Fatal(err)
	}
	released := false
	defer func() {
		if !released {
			release()
		}
	}()

	envs := make(chan []string, 1)
	go func() { envs <- tmuxCommand("list-sessions").Environ() }()

	// The correct build waits for the window to close; a build that ignores
	// the window has already read the projected value by now.
	time.Sleep(100 * time.Millisecond)
	release()
	released = true

	var env []string
	select {
	case env = <-envs:
	case <-time.After(5 * time.Second):
		t.Fatal("tmuxCommand never returned after the open window closed")
	}
	for _, kv := range env {
		if strings.HasPrefix(kv, "BEADS_DOLT_SERVER_DATABASE=") && kv != "BEADS_DOLT_SERVER_DATABASE=rig-db" {
			t.Fatalf("tmux child inherited the open window projection: %q", kv)
		}
	}
	if !contains(env, "BEADS_DOLT_SERVER_DATABASE=rig-db") {
		t.Fatalf("tmux child lost the ambient value; env has no BEADS_DOLT_SERVER_DATABASE=rig-db")
	}
}

func contains(env []string, want string) bool {
	for _, kv := range env {
		if kv == want {
			return true
		}
	}
	return false
}
