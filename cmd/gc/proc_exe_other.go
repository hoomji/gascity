//go:build !linux

package main

// runtimeBinaryStale cannot observe a process executable off Linux, so it
// reports false ("not known to be stale"). readProcExe has no non-Linux
// caller: only the linux-tagged implementation and its test use it.
func runtimeBinaryStale(_ int) bool {
	return false
}
