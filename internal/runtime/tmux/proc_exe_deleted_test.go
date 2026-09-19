package tmux

import "testing"

// TestProcessAliveRejectsDeletedPaneExecutable pins the upgrade-hygiene guard:
// a process whose name matches but whose executable was unlinked (the Linux
// " (deleted)" image left behind when the gc binary is replaced in place) must
// read as NOT alive. Otherwise name-only liveness keeps the stale session
// occupying its stream/slot forever and the reconciler never recycles it.
func TestProcessAliveRejectsDeletedPaneExecutable(t *testing.T) {
	restore := stubProcessExeDeleted(func(pid string) bool { return pid == "101" })
	defer restore()

	snapshot := newProcessSnapshot([]processRuntimeState{
		{PID: "101", PPID: "1", Command: "gc", Args: "gc convoy control --serve --follow gateway-llm/core.control-dispatcher"},
	})
	pane := paneRuntimeState{Command: "gc", PID: "101"}
	if pane.processAlive(processNameSet([]string{"gc"}), snapshot) {
		t.Fatal("processAlive = true for a deleted-exe pane, want false")
	}

	processExeDeleted = func(string) bool { return false }
	if !pane.processAlive(processNameSet([]string{"gc"}), snapshot) {
		t.Fatal("processAlive = false for a live pane, want true")
	}
}

// TestProcessAliveRejectsDeletedDescendantExecutable covers the shell-wrapped
// case: the pane root is a live shell but the matching agent descendant is a
// deleted image, so the session must still read as dead.
func TestProcessAliveRejectsDeletedDescendantExecutable(t *testing.T) {
	restore := stubProcessExeDeleted(func(pid string) bool { return pid == "102" })
	defer restore()

	snapshot := newProcessSnapshot([]processRuntimeState{
		{PID: "101", PPID: "1", Command: "bash", Args: "bash -lc gc convoy control --serve"},
		{PID: "102", PPID: "101", Command: "gc", Args: "gc convoy control --serve"},
	})
	pane := paneRuntimeState{Command: "bash", PID: "101"}
	if pane.processAlive(processNameSet([]string{"gc"}), snapshot) {
		t.Fatal("processAlive = true for a deleted-exe descendant, want false")
	}
}

func stubProcessExeDeleted(fn func(string) bool) func() {
	prev := processExeDeleted
	processExeDeleted = fn
	return func() { processExeDeleted = prev }
}
