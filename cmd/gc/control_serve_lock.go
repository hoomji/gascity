package main

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// errControlServeLockBusy is the platform flock "already held" signal. It is
// declared here (not in the platform files) so the acquisition loop stays
// platform-agnostic.
var errControlServeLockBusy = errors.New("control serve lock busy")

// controlServeLock is the single-server lock for one convoy-control stream.
//
// `gc convoy control --serve --follow <agent>` is a long-running loop, and the
// dispatcher it feeds is not safe to run twice for one stream: two servers race
// the same ready beads, double-dispatch control work, and (as observed) one of
// them can keep serving from a gc binary that `gc install` already rotated out
// from under it. Before this lock, nothing prevented a second `--serve` from
// starting; the only thing that removed a stale server was an unrelated
// teardown happening to kill it.
//
// The lock is an exclusive advisory flock on a per-stream file under
// .gc/runtime/. It is held for the life of the process and released by the
// kernel on exit, so a crashed holder never leaves a wedged lock.
type controlServeLock struct {
	file *os.File
	path string
}

// errControlServeLocked reports that another live server already owns the
// stream. The acquisition loop waits out a short grace window first (to ride
// out the stop/start gap when the reconciler replaces a controller) and only
// surfaces this once that window expires.
type errControlServeLocked struct {
	Stream    string
	LockPath  string
	HolderPID int
	HolderExe string
}

func (e *errControlServeLocked) Error() string {
	holder := "another gc convoy control --serve server"
	if e.HolderPID > 0 {
		holder = fmt.Sprintf("pid=%d", e.HolderPID)
		if e.HolderExe != "" {
			holder += " exe=" + e.HolderExe
		}
	}
	return fmt.Sprintf("stream %q is already served by %s (lock %s); refusing to run a second server", e.Stream, holder, e.LockPath)
}

var (
	// controlServeLockAcquire is the acquisition seam used by the serve loop;
	// tests substitute it to prove the loop refuses without touching the disk.
	controlServeLockAcquire = acquireControlServeLock
	// controlServeLockWait bounds how long a starting server waits for the
	// current holder to exit before refusing. It covers the gap between the
	// reconciler stopping a stale controller and that process actually dying,
	// without letting a genuinely duplicated start hang forever.
	controlServeLockWait = 10 * time.Second
	// controlServeLockPoll is the re-try cadence inside that window.
	controlServeLockPoll = 100 * time.Millisecond
)

// controlServeLockPath derives the lock file for a stream. The name is a
// digest of the stream identity so a qualified name with slashes or dots can
// never produce an invalid or surprising file name.
//
// The city path is canonicalized first: two servers started with different
// strings for the same city (a symlinked home, a relative path) must agree on
// one lock file or neither is excluded.
func controlServeLockPath(cityPath, stream string) string {
	sum := sha256.Sum256([]byte(strings.TrimSpace(stream)))
	return filepath.Join(canonicalControlServeCityPath(cityPath), ".gc", "runtime", "control-serve-"+hex.EncodeToString(sum[:16])+".lock")
}

// canonicalControlServeCityPath resolves as much of cityPath as exists so
// symlink-equivalent names collapse to one lock location. It falls back to the
// cleaned absolute path when resolution fails.
//
// Known limit (F3, deliberately not fixed here): this collapses symlinks and
// relative paths, but not bind mounts. On container hosts where the host path
// (/srv/city) and the in-container path (/mnt/city) name the same directory
// through different mounts, the two strings are not equal and do not resolve
// through EvalSymlinks, so the derived lock files differ and two --serve
// processes can still both acquire a lock. Comparing the city directory's
// st_dev/st_ino would close that gap; until then, run a city's servers from one
// path shape.
func canonicalControlServeCityPath(cityPath string) string {
	cityPath = strings.TrimSpace(cityPath)
	if resolved, err := filepath.EvalSymlinks(cityPath); err == nil && resolved != "" {
		return resolved
	}
	if abs, err := filepath.Abs(cityPath); err == nil {
		return abs
	}
	return filepath.Clean(cityPath)
}

// acquireControlServeLock takes the single-server lock for stream, waiting up
// to controlServeLockWait for an incumbent holder to exit. On success the
// caller owns the lock until Release (or process exit).
func acquireControlServeLock(cityPath, stream string) (*controlServeLock, error) {
	cityPath = strings.TrimSpace(cityPath)
	stream = strings.TrimSpace(stream)
	if cityPath == "" {
		return nil, fmt.Errorf("control serve lock: city path is required")
	}
	if stream == "" {
		return nil, fmt.Errorf("control serve lock: stream identity is required")
	}
	path := controlServeLockPath(cityPath, stream)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, fmt.Errorf("control serve lock: preparing %s: %w", filepath.Dir(path), err)
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o644)
	if err != nil {
		return nil, fmt.Errorf("control serve lock: opening %s: %w", path, err)
	}

	deadline := time.Now().Add(controlServeLockWait)
	for {
		err := tryFlockExclusive(f)
		if err == nil {
			// Holder identity is best-effort diagnostics only; a failure to
			// record it must not fail the acquisition.
			_ = writeControlServeLockHolder(f)
			return &controlServeLock{file: f, path: path}, nil
		}
		if !errors.Is(err, errControlServeLockBusy) {
			_ = f.Close()
			return nil, fmt.Errorf("control serve lock: locking %s: %w", path, err)
		}
		if !time.Now().Before(deadline) {
			holderPID, holderExe := readControlServeLockHolder(path)
			_ = f.Close()
			return nil, &errControlServeLocked{Stream: stream, LockPath: path, HolderPID: holderPID, HolderExe: holderExe}
		}
		time.Sleep(controlServeLockPoll)
	}
}

// Release drops the stream lock. Safe on a nil receiver so callers can defer it
// unconditionally.
func (l *controlServeLock) Release() {
	if l == nil || l.file == nil {
		return
	}
	_ = unlockControlServeLock(l.file)
	_ = l.file.Close()
	l.file = nil
}

// writeControlServeLockHolder records this process's identity in the lock file
// so a refused starter can name the incumbent. The flock still provides the
// mutual exclusion; the file contents are advisory diagnostics.
func writeControlServeLockHolder(f *os.File) error {
	if err := f.Truncate(0); err != nil {
		return err
	}
	if _, err := f.Seek(0, 0); err != nil {
		return err
	}
	exe, _ := os.Executable()
	_, err := fmt.Fprintf(f, "%d\n%s\n", os.Getpid(), exe)
	return err
}

// readControlServeLockHolder reads back the PID and executable recorded by the
// current holder. Missing or malformed content yields zero values, never an
// error: this is diagnostics on the refusal path.
func readControlServeLockHolder(path string) (pid int, exe string) {
	data, err := os.ReadFile(path)
	if err != nil {
		return 0, ""
	}
	lines := strings.Split(strings.TrimRight(string(data), "\n"), "\n")
	if len(lines) > 0 {
		pid, _ = strconv.Atoi(strings.TrimSpace(lines[0]))
	}
	if len(lines) > 1 {
		exe = strings.TrimSpace(lines[1])
	}
	return pid, exe
}
