//go:build !linux

package tmux

// processExeDeletedImpl is a no-op on platforms without /proc; deleted-image
// detection is a Linux upgrade-hygiene guard.
func processExeDeletedImpl(string) bool { return false }
