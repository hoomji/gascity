//go:build linux

package tmux

import (
	"os"
	"strings"
)

// processExeDeletedImpl reads /proc/<pid>/exe and reports whether the kernel
// marked the target as removed (the target string ends with " (deleted)").
// Any read failure is reported as "not deleted": an unreadable /proc entry is
// an unobservable process, not a confirmed stale one, and must never make a
// live agent look dead.
func processExeDeletedImpl(pid string) bool {
	if pid == "" {
		return false
	}
	target, err := os.Readlink("/proc/" + pid + "/exe")
	if err != nil {
		return false
	}
	return strings.HasSuffix(target, " (deleted)")
}
