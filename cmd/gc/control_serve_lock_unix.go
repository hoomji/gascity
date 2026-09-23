//go:build !windows

package main

import (
	"errors"
	"os"
	"syscall"
)

// tryFlockExclusive attempts a non-blocking exclusive flock. It returns
// errControlServeLockBusy when another process (or another open file
// description in this process) already holds the lock.
func tryFlockExclusive(f *os.File) error {
	err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
	if err == nil {
		return nil
	}
	if errors.Is(err, syscall.EWOULDBLOCK) || errors.Is(err, syscall.EAGAIN) {
		return errControlServeLockBusy
	}
	return err
}

// unlockControlServeLock releases a flock taken by tryFlockExclusive.
func unlockControlServeLock(f *os.File) error {
	return syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
}
