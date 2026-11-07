// Package fsys defines a minimal filesystem interface for testability.
//
// Production code uses [OSFS] which delegates to the os package.
// Tests use [Fake] which provides an in-memory filesystem with spy
// capabilities and error injection — following the same pattern as
// [session.Provider] / [session.Fake].
package fsys

import (
	"os"
)

// FS abstracts the filesystem operations used by CLI commands. Implementations
// share the portable namespace contract in internal/fsys/fsystest.
type FS interface {
	// MkdirAll creates a directory path and all parents that do not exist. It
	// returns an error when the path or one of its ancestors is a file.
	MkdirAll(path string, perm os.FileMode) error

	// WriteFile writes data to the named file, creating it if necessary. The
	// parent directory must exist, directories cannot be overwritten, and the
	// mode of an existing file is preserved.
	WriteFile(name string, data []byte, perm os.FileMode) error

	// CreateExclusive writes data to the named file, creating it only when
	// it does not already exist. The parent directory must exist. An
	// existing file (or directory) is never overwritten: the call fails
	// with an error matching os.ErrExist so the caller can re-read the
	// winner's contents. The create is atomic — concurrent creators elect
	// exactly one winner.
	CreateExclusive(name string, data []byte, perm os.FileMode) error

	// ReadFile reads the named file and returns its contents.
	ReadFile(name string) ([]byte, error)

	// Stat returns file info for the named file.
	Stat(name string) (os.FileInfo, error)

	// Lstat returns file info for the named file without following symlinks.
	// Callers that must reject symlinked targets should call Lstat and check
	// the mode's ModeSymlink bit before touching the path.
	Lstat(name string) (os.FileInfo, error)

	// ReadDir reads the named directory and returns its entries. Missing paths
	// and non-directory paths return errors.
	ReadDir(name string) ([]os.DirEntry, error)

	// Rename renames (moves) oldpath to newpath. The destination parent must
	// exist, and moving a directory moves its complete subtree.
	Rename(oldpath, newpath string) error

	// Remove removes the named file or empty directory.
	Remove(name string) error

	// Chmod changes the mode of an existing file or directory.
	Chmod(name string, mode os.FileMode) error
}

// OSFS implements [FS] by delegating to the os package.
type OSFS struct{}

// MkdirAll delegates to [os.MkdirAll].
func (OSFS) MkdirAll(path string, perm os.FileMode) error {
	return os.MkdirAll(path, perm)
}

// WriteFile delegates to [os.WriteFile].
func (OSFS) WriteFile(name string, data []byte, perm os.FileMode) error {
	return os.WriteFile(name, data, perm)
}

// CreateExclusive creates the named file with O_EXCL: an existing path
// fails with os.ErrExist and is never overwritten.
func (OSFS) CreateExclusive(name string, data []byte, perm os.FileMode) error {
	f, err := os.OpenFile(name, os.O_WRONLY|os.O_CREATE|os.O_EXCL, perm)
	if err != nil {
		return err
	}
	if _, err := f.Write(data); err != nil {
		_ = f.Close()
		// The file is ours alone (exclusive create just succeeded), so a
		// torn write is removed instead of left for a later load to trip on.
		_ = os.Remove(name)
		return err
	}
	return f.Close()
}

// ReadFile delegates to [os.ReadFile].
func (OSFS) ReadFile(name string) ([]byte, error) {
	return os.ReadFile(name)
}

// Stat delegates to [os.Stat].
func (OSFS) Stat(name string) (os.FileInfo, error) {
	return os.Stat(name)
}

// Lstat delegates to [os.Lstat].
func (OSFS) Lstat(name string) (os.FileInfo, error) {
	return os.Lstat(name)
}

// Readlink delegates to [os.Readlink].
func (OSFS) Readlink(name string) (string, error) {
	return os.Readlink(name)
}

// Symlink delegates to [os.Symlink].
func (OSFS) Symlink(oldname, newname string) error {
	return os.Symlink(oldname, newname)
}

// ReadDir delegates to [os.ReadDir].
func (OSFS) ReadDir(name string) ([]os.DirEntry, error) {
	return os.ReadDir(name)
}

// Rename delegates to [os.Rename].
func (OSFS) Rename(oldpath, newpath string) error {
	return os.Rename(oldpath, newpath)
}

// Remove delegates to [os.Remove].
func (OSFS) Remove(name string) error {
	return os.Remove(name)
}

// Chmod delegates to [os.Chmod].
func (OSFS) Chmod(name string, mode os.FileMode) error {
	return os.Chmod(name, mode)
}
