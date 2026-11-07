package cityinit

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/google/uuid"

	"github.com/gastownhall/gascity/internal/citylayout"
	"github.com/gastownhall/gascity/internal/fsys"
)

func TestEnsureCityScaffoldFSReturnsEventLogWriteError(t *testing.T) {
	eventsPath := filepath.Join("/city", citylayout.RuntimeRoot, "events.jsonl")
	writeErr := errors.New("read-only filesystem")
	f := writeErrorFS{
		Fake: fsys.NewFake(),
		path: eventsPath,
		err:  writeErr,
	}

	err := EnsureCityScaffoldFS(f, "/city")
	if !errors.Is(err, writeErr) {
		t.Fatalf("EnsureCityScaffoldFS error = %v, want %v", err, writeErr)
	}
}

func TestEnsureCityScaffoldFSMintsIncarnationID(t *testing.T) {
	f := fsys.NewFake()
	if err := EnsureCityScaffoldFS(f, "/city"); err != nil {
		t.Fatalf("EnsureCityScaffoldFS: %v", err)
	}
	raw, err := f.ReadFile(filepath.Join("/city", citylayout.RuntimeRoot, "city-id"))
	if err != nil {
		t.Fatalf("read city-id: %v", err)
	}
	first := strings.TrimSpace(string(raw))
	if _, err := uuid.Parse(first); err != nil {
		t.Errorf("city-id %q is not a UUID: %v", first, err)
	}
	// A re-ensure (resume path) never rewrites the id.
	if err := EnsureCityScaffoldFS(f, "/city"); err != nil {
		t.Fatalf("EnsureCityScaffoldFS again: %v", err)
	}
	raw, err = f.ReadFile(filepath.Join("/city", citylayout.RuntimeRoot, "city-id"))
	if err != nil {
		t.Fatalf("read city-id again: %v", err)
	}
	if second := strings.TrimSpace(string(raw)); second != first {
		t.Errorf("city-id after re-ensure = %q, want the minted %q", second, first)
	}
}

func TestEnsureCityScaffoldFSReturnsEventLogStatError(t *testing.T) {
	eventsPath := filepath.Join("/city", citylayout.RuntimeRoot, "events.jsonl")
	statErr := errors.New("permission denied")
	f := statErrorFS{
		Fake: fsys.NewFake(),
		path: eventsPath,
		err:  statErr,
	}

	err := EnsureCityScaffoldFS(f, "/city")
	if !errors.Is(err, statErr) {
		t.Fatalf("EnsureCityScaffoldFS error = %v, want %v", err, statErr)
	}
}

type writeErrorFS struct {
	*fsys.Fake
	path string
	err  error
}

func (f writeErrorFS) WriteFile(name string, data []byte, perm os.FileMode) error {
	if name == f.path {
		return f.err
	}
	return f.Fake.WriteFile(name, data, perm)
}

type statErrorFS struct {
	*fsys.Fake
	path string
	err  error
}

func (f statErrorFS) Stat(name string) (os.FileInfo, error) {
	if name == f.path {
		return nil, f.err
	}
	return f.Fake.Stat(name)
}
