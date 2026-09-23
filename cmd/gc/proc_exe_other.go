//go:build !linux

package main

import "fmt"

// readProcExe is unavailable off Linux: there is no /proc/<pid>/exe.
func readProcExe(pid int) (string, bool, error) {
	return "", false, fmt.Errorf("readProcExe: /proc is unavailable on this platform (pid %d)", pid)
}

// runtimeBinaryStale cannot observe a process executable off Linux, so it
// reports false ("not known to be stale").
func runtimeBinaryStale(pid int) bool {
	return false
}
