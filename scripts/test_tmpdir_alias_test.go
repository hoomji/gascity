package scripts_test

import (
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// Inject physical pwd output so Darwin aliases are covered on Linux without
// creating directories under /private. cd still validates a real directory.
func TestCanonicalizeTestTMPDIRPlatformAliases(t *testing.T) {
	for _, platform := range []string{"Darwin", "Linux"} {
		for _, path := range []string{"/private/var", "/private/var/folders/test dir", "/private/tmp", "/private/tmp/test", "/private/variable", "/private/tmp-other", "/private/other", "/var/tmp"} {
			t.Run(platform+path, func(t *testing.T) {
				cmd := exec.Command("bash", "-c", `source lib/common.sh
uname() { printf '%s\n' "$TEST_PLATFORM"; }
pwd() { printf '%s\n' "$PHYSICAL_PATH"; }
canonicalize_test_tmpdir "$EXISTING_DIR"`)
				cmd.Env = append(os.Environ(), "TEST_PLATFORM="+platform, "PHYSICAL_PATH="+path, "EXISTING_DIR="+t.TempDir())
				out, err := cmd.CombinedOutput()
				if err != nil {
					t.Fatalf("helper: %v: %s", err, out)
				}
				want := path
				if platform == "Darwin" && (path == "/private/var" || strings.HasPrefix(path, "/private/var/") || path == "/private/tmp" || strings.HasPrefix(path, "/private/tmp/")) {
					want = strings.TrimPrefix(path, "/private")
				}
				if got := strings.TrimSpace(string(out)); got != want {
					t.Errorf("got %q, want %q", got, want)
				}
			})
		}
	}
}

// Symlinked homes (and macOS /var) must not turn canonical store comparisons
// into failures. The gate must also retain its ambient-runtime env isolation.
func TestGoTestShardCanonicalizesTMPDIRAndScrubsRuntimeEnv(t *testing.T) {
	f := newGoTestShardFixtureWithExit(t, 0)
	// Simulate BSD realpath rejecting GNU options even on Linux. The runner
	// must canonicalize with portable shell builtins, not invoke this command.
	if err := os.WriteFile(filepath.Join(f.tmpDir, "bin", "realpath"), []byte("#!/bin/sh\necho 'realpath: illegal option -- e' >&2\nexit 1\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	alias := filepath.Join(t.TempDir(), "alias")
	if err := os.Symlink(f.tmpDir, alias); err != nil {
		t.Fatal(err)
	}
	cmd := f.command("TMPDIR="+alias, "GC_CITY_PATH=/live/city", "GC_DOLT_HOST=live-host", "GC_DOLT_PORT=3306", "BEADS_DIR=/live/beads", "BEADS_DOLT_SERVER_PORT=3306")
	if code, out := runShardCommand(t, cmd); code != 0 {
		t.Fatalf("shard exit %d: %s", code, out)
	}
	env := fixtureEnvironment(t, readFixtureFile(t, f.productEnvFile))
	want, err := filepath.EvalSymlinks(f.tmpDir)
	if err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS == "darwin" && (want == "/private/var" || strings.HasPrefix(want, "/private/var/") || want == "/private/tmp" || strings.HasPrefix(want, "/private/tmp/")) {
		want = strings.TrimPrefix(want, "/private")
	}
	if env["TMPDIR"] != want {
		t.Errorf("TMPDIR = %q, want canonical %q", env["TMPDIR"], want)
	}
	for _, name := range []string{"GC_CITY_PATH", "GC_DOLT_HOST", "GC_DOLT_PORT", "BEADS_DIR", "BEADS_DOLT_SERVER_PORT"} {
		if value, ok := env[name]; ok {
			t.Errorf("ambient %s leaked into shard: %q", name, value)
		}
	}
}
