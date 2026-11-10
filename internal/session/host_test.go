package session

import (
	"context"
	"errors"
	"os"
	"strings"
	"testing"

	"github.com/gastownhall/gascity/internal/beads"
	"github.com/gastownhall/gascity/internal/beads/beadstest"
	"github.com/gastownhall/gascity/internal/runtime"
)

// TestLocalSessionHostMatchesHostname pins LocalSessionHost to its source of
// truth: the trimmed machine hostname.
func TestLocalSessionHostMatchesHostname(t *testing.T) {
	want, err := os.Hostname()
	if err != nil {
		t.Skipf("os.Hostname: %v", err)
	}
	if got := LocalSessionHost(); got != strings.TrimSpace(want) {
		t.Errorf("LocalSessionHost() = %q, want %q", got, strings.TrimSpace(want))
	}
}

// TestLocalSessionHostEmptyOnError pins the fail-open read: when the host
// cannot be determined, LocalSessionHost reports "" rather than guessing.
func TestLocalSessionHostEmptyOnError(t *testing.T) {
	prev := localSessionHostname
	localSessionHostname = func() (string, error) { return "", errors.New("no hostname") }
	defer func() { localSessionHostname = prev }()
	if got := LocalSessionHost(); got != "" {
		t.Errorf("LocalSessionHost() = %q, want empty on hostname error", got)
	}
}

// TestStampHostRecordsCreatingHost pins StampHost: an unnamed map gains the
// creating host, and an explicitly named host is never overwritten.
func TestStampHostRecordsCreatingHost(t *testing.T) {
	want := LocalSessionHost()
	if want == "" {
		t.Skip("no hostname in this environment; nothing to stamp")
	}
	meta := map[string]string{"template": "worker"}
	StampHost(meta)
	if meta[HostMetadataKey] != want {
		t.Errorf("StampHost meta[%q] = %q, want %q", HostMetadataKey, meta[HostMetadataKey], want)
	}
	explicit := map[string]string{HostMetadataKey: "other-host"}
	StampHost(explicit)
	if explicit[HostMetadataKey] != "other-host" {
		t.Errorf("StampHost overwrote explicit host: %q", explicit[HostMetadataKey])
	}
}

// TestManagerCreateStampsHost pins the record half of the session host field:
// every Manager create path stamps the creating host on the row and projects
// it onto Info.Host.
func TestManagerCreateStampsHost(t *testing.T) {
	want := LocalSessionHost()
	if want == "" {
		t.Skip("no hostname in this environment; nothing to stamp")
	}
	store := beads.NewMemStore()
	mgr := NewManagerWithOptions(beadstest.Sessions(store), runtime.NewFake(), WithCityPath(t.TempDir()))
	info, err := mgr.CreateSession(context.Background(), CreateOptions{
		Template: "worker", Title: "Worker", Provider: "fake", BeadOnly: true,
	})
	if err != nil {
		t.Fatalf("CreateSession: %v", err)
	}
	if info.Host != want {
		t.Errorf("Info.Host = %q, want %q", info.Host, want)
	}
	created, err := store.Get(info.ID)
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if created.Metadata[HostMetadataKey] != want {
		t.Errorf("row %q = %q, want %q", HostMetadataKey, created.Metadata[HostMetadataKey], want)
	}
}
