//go:build linux

package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
)

const procExeTestScript = "sleep 60; true"

// startCopiedLongLivedProcess starts a private copy of bash running a script
// that stays alive for the test, and waits until the child has exec'd the copy
// so /proc/<pid>/exe names it rather than the test binary.
//
// bash is used rather than `sleep` because the test must cp a standalone
// executable: on some distributions `sleep` is a coreutils multi-call binary
// that refuses to run under a renamed copy.
func startCopiedLongLivedProcess(t *testing.T) (*exec.Cmd, string) {
	t.Helper()
	shell, err := exec.LookPath("bash")
	if err != nil {
		t.Skipf("bash not available: %v", err)
	}
	dir := t.TempDir()
	bin := filepath.Join(dir, "shell-copy")
	data, err := os.ReadFile(shell)
	if err != nil {
		t.Fatalf("read %s: %v", shell, err)
	}
	if err := os.WriteFile(bin, data, 0o755); err != nil {
		t.Fatalf("write %s: %v", bin, err)
	}

	cmd := exec.Command(bin, "-c", procExeTestScript)
	if err := cmd.Start(); err != nil {
		t.Fatalf("start %s: %v", bin, err)
	}
	t.Cleanup(func() {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
	})

	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if path, _, err := readProcExe(cmd.Process.Pid); err == nil && samePath(path, bin) {
			return cmd, bin
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("child pid %d never exec'd %s", cmd.Process.Pid, bin)
	return nil, ""
}

func TestRuntimeBinaryStaleReportsFreshCopyAsCurrent(t *testing.T) {
	cmd, _ := startCopiedLongLivedProcess(t)
	if runtimeBinaryStale(cmd.Process.Pid) {
		t.Fatalf("freshly started copy reported stale")
	}
}

func TestRuntimeBinaryStaleDetectsUnlinkedExecutable(t *testing.T) {
	cmd, bin := startCopiedLongLivedProcess(t)
	if err := os.Remove(bin); err != nil {
		t.Fatalf("remove %s: %v", bin, err)
	}
	if !runtimeBinaryStale(cmd.Process.Pid) {
		t.Fatalf("unlinked executable was not reported stale")
	}
	path, deleted, err := readProcExe(cmd.Process.Pid)
	if err != nil {
		t.Fatalf("readProcExe: %v", err)
	}
	if !deleted {
		t.Fatalf("readProcExe deleted = false, want true (path=%q)", path)
	}
	if !samePath(path, bin) {
		t.Fatalf("readProcExe path = %q, want %q", path, bin)
	}
}

func TestRuntimeBinaryStaleDetectsReplacedExecutable(t *testing.T) {
	cmd, bin := startCopiedLongLivedProcess(t)

	shell, err := exec.LookPath("bash")
	if err != nil {
		t.Skipf("bash not available: %v", err)
	}
	replacement := bin + ".new"
	data, err := os.ReadFile(shell)
	if err != nil {
		t.Fatalf("read %s: %v", shell, err)
	}
	if err := os.WriteFile(replacement, data, 0o755); err != nil {
		t.Fatalf("write replacement: %v", err)
	}
	if err := os.Rename(replacement, bin); err != nil {
		t.Fatalf("replace %s: %v", bin, err)
	}
	if !runtimeBinaryStale(cmd.Process.Pid) {
		t.Fatalf("replaced executable was not reported stale")
	}
}

// TestRuntimeBinaryStaleTreatsUnreadableStatAsNotStale pins the stat branch that
// the " (deleted)" short-circuit above does not reach: when the executable path
// is still linked but cannot be stat'd (EACCES), the probe must report "cannot
// tell" (false), never stale, because a false positive makes the reconciler
// restart a healthy controller.
func TestRuntimeBinaryStaleTreatsUnreadableStatAsNotStale(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("running as root: directory permissions do not produce EACCES")
	}
	cmd, bin := startCopiedLongLivedProcess(t)

	dir := filepath.Dir(bin)
	if err := os.Chmod(dir, 0o000); err != nil {
		t.Fatalf("chmod %s: %v", dir, err)
	}
	defer func() {
		if err := os.Chmod(dir, 0o755); err != nil {
			t.Errorf("restore %s permissions: %v", dir, err)
		}
	}()

	// The unlinked short-circuit must not fire: readlink still reports the
	// path, and the kernel did not decorate it with " (deleted)".
	path, deleted, err := readProcExe(cmd.Process.Pid)
	if err != nil {
		t.Fatalf("readProcExe: %v", err)
	}
	if deleted {
		t.Fatalf("readProcExe deleted = true, want false (path=%q)", path)
	}
	if _, statErr := os.Stat(bin); statErr == nil || os.IsNotExist(statErr) {
		t.Fatalf("os.Stat(%q) error = %v, want a non-ENOENT failure such as EACCES", bin, statErr)
	}

	if runtimeBinaryStale(cmd.Process.Pid) {
		t.Fatalf("unreadable on-disk executable reported stale; want false (cannot tell)")
	}
}
