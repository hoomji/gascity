//go:build windows

package main

import (
	"errors"
	"os"

	"golang.org/x/sys/windows"
)

// tryFlockExclusive attempts a non-blocking exclusive lock on the first byte of
// f. Windows has no flock; LockFileEx with LOCKFILE_FAIL_IMMEDIATELY is the
// equivalent. A lock violation means the stream is already served.
func tryFlockExclusive(f *os.File) error {
	var overlapped windows.Overlapped
	err := windows.LockFileEx(windows.Handle(f.Fd()),
		windows.LOCKFILE_EXCLUSIVE_LOCK|windows.LOCKFILE_FAIL_IMMEDIATELY,
		0, 1, 0, &overlapped)
	if err == nil {
		return nil
	}
	if errors.Is(err, windows.ERROR_LOCK_VIOLATION) {
		return errControlServeLockBusy
	}
	return err
}

// unlockControlServeLock releases the byte-range lock taken by
// tryFlockExclusive.
func unlockControlServeLock(f *os.File) error {
	var overlapped windows.Overlapped
	return windows.UnlockFileEx(windows.Handle(f.Fd()), 0, 1, 0, &overlapped)
}
