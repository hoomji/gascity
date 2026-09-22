//go:build linux

package main

import (
	"fmt"
	"os"
	"strings"
)

// readProcExe returns the executable a process is running, resolved through
// /proc/<pid>/exe. The kernel decorates an unlinked target with a literal
// " (deleted)" suffix; that suffix is stripped from the returned path and
// reported separately as deleted.
//
// The PID must belong to a process this user can inspect. A permission error
// (a different uid) is returned so the caller can back off rather than guess.
func readProcExe(pid int) (path string, deleted bool, err error) {
	if pid <= 1 {
		return "", false, fmt.Errorf("readProcExe: invalid pid %d", pid)
	}
	target, err := os.Readlink(fmt.Sprintf("/proc/%d/exe", pid))
	if err != nil {
		return "", false, err
	}
	deleted = strings.HasSuffix(target, " (deleted)")
	return strings.TrimSuffix(target, " (deleted)"), deleted, nil
}

// runtimeBinaryStale reports whether the process at pid is running a gc binary
// that no longer matches the binary on disk.
//
// This is the `gc install` defect: the installer writes the new binary at the
// same path (typically by rename), which unlinks the inode the old server still
// has mapped. The server keeps serving stale code until something unrelated
// kills it. Two signals cover it:
//
//   - the kernel reports the executable as deleted (" (deleted)"), or
//   - the inode the process is running differs from the file now at the same
//     path (an in-place replace that did not unlink the old inode).
//
// An observation failure (permission, vanished process) returns false: "cannot
// tell" must never be reported as "stale", because the caller restarts on true.
func runtimeBinaryStale(pid int) bool {
	exePath, deleted, err := readProcExe(pid)
	if err != nil || exePath == "" {
		return false
	}
	if deleted {
		return true
	}
	procInfo, err := os.Stat(fmt.Sprintf("/proc/%d/exe", pid))
	if err != nil {
		return false
	}
	diskInfo, err := os.Stat(exePath)
	if err != nil {
		// The running binary has no on-disk name anymore.
		return true
	}
	return !os.SameFile(procInfo, diskInfo)
}
