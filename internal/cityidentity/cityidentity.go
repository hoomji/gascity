// Package cityidentity owns the per-city incarnation id: a 128-bit random
// id (UUIDv4) minted once per city directory and never rewritten. It lets
// a client prove the city it writes to is the city it read from — a city
// replaced on disk (rm -rf + gc init at the same path) mints a new id,
// so holders of the old id fail closed instead of writing into a
// stranger's state.
//
// Layout: <city>/.gc/city-id holds the bare id. gc init mints it; a
// supervisor or standalone controller mints it on first load when an
// older city lacks one. A malformed file fails the load loudly — it is
// never silently re-minted, since re-minting would fork the city's
// identity under clients holding the old one.
//
// Inherent limit, shared with any marker stored in the directory: a
// copy, clone, or restore of the city dir carries the id with it, so a
// copy is not detectable from the directory alone.
package cityidentity

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/google/uuid"

	"github.com/gastownhall/gascity/internal/citylayout"
	"github.com/gastownhall/gascity/internal/fsys"
)

// FileName is the incarnation file's name inside the city's .gc dir.
const FileName = "city-id"

// ErrNotMinted reports that the city has no incarnation file yet.
// Callers that can mint (init, first load) treat it as the signal to
// create the file; callers that only read treat it as unknown identity.
var ErrNotMinted = errors.New("city has no incarnation id")

// Path returns the incarnation file's path for a city root.
func Path(cityPath string) string {
	return filepath.Join(cityPath, citylayout.RuntimeRoot, FileName)
}

// Generate returns a fresh random incarnation id.
func Generate() string {
	return uuid.NewString()
}

// Validate reports whether id parses as an incarnation id.
func Validate(id string) error {
	if _, err := uuid.Parse(strings.TrimSpace(id)); err != nil {
		return fmt.Errorf("invalid city incarnation id %q: %w", id, err)
	}
	return nil
}

// Load reads the city's incarnation id from the OS filesystem. A missing
// file is ErrNotMinted; a malformed file is a loud error — never
// silently re-minted.
func Load(cityPath string) (string, error) {
	return LoadFS(fsys.OSFS{}, cityPath)
}

// LoadFS is Load over an abstract filesystem.
func LoadFS(filesys fsys.FS, cityPath string) (string, error) {
	path := Path(cityPath)
	data, err := filesys.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return "", fmt.Errorf("%w at %s", ErrNotMinted, path)
		}
		return "", fmt.Errorf("reading city incarnation id: %w", err)
	}
	id := strings.TrimSpace(string(data))
	if err := Validate(id); err != nil {
		return "", fmt.Errorf("reading city incarnation id: %w", err)
	}
	return id, nil
}

// Ensure returns the city's incarnation id, minting it with an exclusive
// create when the city has none. The mint never rewrites an existing
// file: a racing loser re-reads the winner's id. A malformed file fails
// loudly instead of being replaced.
func Ensure(cityPath string) (string, error) {
	return EnsureFS(fsys.OSFS{}, cityPath)
}

// EnsureFS is Ensure over an abstract filesystem.
func EnsureFS(filesys fsys.FS, cityPath string) (string, error) {
	id, err := LoadFS(filesys, cityPath)
	if err == nil {
		return id, nil
	}
	if !errors.Is(err, ErrNotMinted) {
		return "", err
	}
	id = Generate()
	if createErr := filesys.CreateExclusive(Path(cityPath), []byte(id+"\n"), 0o644); createErr != nil {
		if errors.Is(createErr, os.ErrExist) {
			// Lost the mint race: the winner's id is the city's.
			return LoadFS(filesys, cityPath)
		}
		return "", fmt.Errorf("minting city incarnation id: %w", createErr)
	}
	return id, nil
}
