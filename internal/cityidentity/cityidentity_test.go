package cityidentity

import (
	"errors"
	"os"
	"path/filepath"
	"sync"
	"syscall"
	"testing"

	"github.com/google/uuid"

	"github.com/gastownhall/gascity/internal/fsys"
)

func testCity(t testing.TB) string {
	t.Helper()
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, ".gc"), 0o755); err != nil {
		t.Fatal(err)
	}
	return dir
}

func TestEnsureMintsValidID(t *testing.T) {
	// Umask is process-global. This test is not parallel and restores the
	// previous mask, so the 0644 assertion is deterministic.
	previous := syscall.Umask(0o022)
	defer syscall.Umask(previous)
	dir := testCity(t)
	id, err := Ensure(dir)
	if err != nil {
		t.Fatalf("Ensure: %v", err)
	}
	if _, err := uuid.Parse(id); err != nil {
		t.Errorf("Ensure minted %q, want a UUID: %v", id, err)
	}
	raw, err := os.ReadFile(Path(dir))
	if err != nil {
		t.Fatalf("read back: %v", err)
	}
	if string(raw) != id+"\n" {
		t.Errorf("file = %q, want %q", raw, id+"\n")
	}
	fi, err := os.Stat(Path(dir))
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	if perm := fi.Mode().Perm(); perm != 0o644 {
		t.Errorf("perm = %o, want 644", perm)
	}
}

func TestEnsureNeverRewrites(t *testing.T) {
	dir := testCity(t)
	first, err := Ensure(dir)
	if err != nil {
		t.Fatalf("Ensure: %v", err)
	}
	second, err := Ensure(dir)
	if err != nil {
		t.Fatalf("Ensure again: %v", err)
	}
	if second != first {
		t.Errorf("second Ensure = %q, want the minted %q", second, first)
	}
}

func TestEnsureKeepsExistingID(t *testing.T) {
	dir := testCity(t)
	const want = "33333333-3333-4333-8333-333333333333"
	if err := os.WriteFile(Path(dir), []byte(want+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := Ensure(dir)
	if err != nil {
		t.Fatalf("Ensure: %v", err)
	}
	if got != want {
		t.Errorf("Ensure = %q, want existing %q", got, want)
	}
}

func TestLoadMissingIsNotMinted(t *testing.T) {
	dir := testCity(t)
	if _, err := Load(dir); !errors.Is(err, ErrNotMinted) {
		t.Errorf("Load = %v, want ErrNotMinted", err)
	}
}

func TestMalformedFailsLoudly(t *testing.T) {
	dir := testCity(t)
	if err := os.WriteFile(Path(dir), []byte("not-a-uuid\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(dir); err == nil {
		t.Error("Load = nil, want a loud malformed error")
	}
	if _, err := Ensure(dir); err == nil {
		t.Error("Ensure = nil, want a loud malformed error, never a silent re-mint")
	}
	raw, err := os.ReadFile(Path(dir))
	if err != nil {
		t.Fatal(err)
	}
	if string(raw) != "not-a-uuid\n" {
		t.Errorf("file = %q, want the malformed bytes untouched", raw)
	}
}

func TestEnsureFSOverFakeMemory(t *testing.T) {
	fs := fsys.NewFake()
	fs.Dirs["/city"] = true
	fs.Dirs["/city/.gc"] = true
	id, err := EnsureFS(fs, "/city")
	if err != nil {
		t.Fatalf("EnsureFS: %v", err)
	}
	if _, err := uuid.Parse(id); err != nil {
		t.Errorf("EnsureFS minted %q, want a UUID: %v", id, err)
	}
	again, err := EnsureFS(fs, "/city")
	if err != nil {
		t.Fatalf("EnsureFS again: %v", err)
	}
	if again != id {
		t.Errorf("second EnsureFS = %q, want the minted %q", again, id)
	}
}

func TestEnsureConcurrentMintElectsOneID(t *testing.T) {
	dir := testCity(t)
	const racers = 16
	ids := make([]string, racers)
	var wg sync.WaitGroup
	for i := range ids {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			id, err := Ensure(dir)
			if err != nil {
				t.Errorf("Ensure: %v", err)
				return
			}
			ids[i] = id
		}(i)
	}
	wg.Wait()
	for i, id := range ids {
		if id == "" {
			t.Fatalf("racer %d got no id", i)
		}
		if id != ids[0] {
			t.Fatalf("racer %d = %q, want the single winner %q", i, id, ids[0])
		}
	}
}
