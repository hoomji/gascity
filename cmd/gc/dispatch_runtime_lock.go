package main

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"syscall"

	"github.com/gastownhall/gascity/internal/citylayout"
)

// workflowServeFollowLocks keeps every acquired per-stream lock file reachable
// for the process lifetime. os.File carries a runtime finalizer that closes the
// descriptor once the value is unreachable; closing it would drop the flock, so
// the holder is pinned here rather than in a local. The lock is intentionally
// never released: a `gc convoy control --serve --follow` process owns its
// stream until it exits.
var workflowServeFollowLocks []*os.File

// acquireWorkflowServeFollowLock takes an exclusive, non-blocking flock on the
// control-dispatcher stream lock for qualifiedName and holds it for the process
// lifetime. It returns held=false (nil error) when another live server already
// owns the stream; the caller logs one line and exits 0 so a second server
// neither starts nor looks like a crash. Any other error (unwritable runtime
// dir, unexpected flock failure) is returned and treated as fatal.
func acquireWorkflowServeFollowLock(cityPath, qualifiedName string) (bool, error) {
	lockPath := citylayout.ControlDispatcherLockPathFor(cityPath, qualifiedName)
	if err := os.MkdirAll(filepath.Dir(lockPath), 0o755); err != nil {
		return false, fmt.Errorf("creating control-dispatcher lock dir: %w", err)
	}
	f, err := os.OpenFile(lockPath, os.O_CREATE|os.O_RDWR, 0o644)
	if err != nil {
		return false, fmt.Errorf("opening control-dispatcher stream lock %s: %w", lockPath, err)
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		_ = f.Close()
		if errors.Is(err, syscall.EWOULDBLOCK) || errors.Is(err, syscall.EAGAIN) {
			return false, nil
		}
		return false, fmt.Errorf("locking control-dispatcher stream %s: %w", lockPath, err)
	}
	// Record the holder for diagnosis. Best-effort: a failed write must never
	// drop the lock we already own.
	if err := f.Truncate(0); err == nil {
		_, _ = f.WriteAt([]byte(strconv.Itoa(os.Getpid())+"\n"), 0)
	}
	workflowServeFollowLocks = append(workflowServeFollowLocks, f)
	return true, nil
}
