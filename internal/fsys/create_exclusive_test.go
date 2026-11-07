package fsys_test

import (
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/gastownhall/gascity/internal/fsys"
)

func TestCreateExclusiveElectsOneWinner(t *testing.T) {
	// OSFS: real exclusive create.
	dir := t.TempDir()
	path := filepath.Join(dir, "id")
	if err := (fsys.OSFS{}).CreateExclusive(path, []byte("first\n"), 0o644); err != nil {
		t.Fatalf("OSFS first create: %v", err)
	}
	if err := (fsys.OSFS{}).CreateExclusive(path, []byte("second\n"), 0o644); !errors.Is(err, os.ErrExist) {
		t.Errorf("OSFS second create = %v, want os.ErrExist", err)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(raw) != "first\n" {
		t.Errorf("OSFS file = %q, want the winner's bytes", raw)
	}

	// Fake: same contract in memory.
	fake := fsys.NewFake()
	fake.Dirs["/city/.gc"] = true
	fakePath := "/city/.gc/city-id"
	if err := fake.CreateExclusive(fakePath, []byte("first\n"), 0o644); err != nil {
		t.Fatalf("Fake first create: %v", err)
	}
	if err := fake.CreateExclusive(fakePath, []byte("second\n"), 0o644); !errors.Is(err, os.ErrExist) {
		t.Errorf("Fake second create = %v, want os.ErrExist", err)
	}
	if got := string(fake.Files[fakePath]); got != "first\n" {
		t.Errorf("Fake file = %q, want the winner's bytes", got)
	}
	if got := fake.Modes[fakePath]; got != 0o644 {
		t.Errorf("Fake mode = %o, want 644", got)
	}
}
