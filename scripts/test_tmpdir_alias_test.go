package scripts_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// Symlinked homes (and macOS /var) must not turn canonical store comparisons
// into failures. The gate must also retain its ambient-runtime env isolation.
func TestGoTestShardCanonicalizesTMPDIRAndScrubsRuntimeEnv(t *testing.T) {
	tests := []struct {
		name         string
		platform     string
		physicalPath string
	}{
		{name: "darwin private var root", platform: "Darwin", physicalPath: "/private/var"},
		{name: "darwin private var child", platform: "Darwin", physicalPath: "/private/var/folders/test dir"},
		{name: "darwin private tmp root", platform: "Darwin", physicalPath: "/private/tmp"},
		{name: "darwin private tmp child", platform: "Darwin", physicalPath: "/private/tmp/test"},
		{name: "darwin var near miss", platform: "Darwin", physicalPath: "/private/variable"},
		{name: "darwin tmp near miss", platform: "Darwin", physicalPath: "/private/tmp-other"},
		{name: "linux private var", platform: "Linux", physicalPath: "/private/var/folders/test dir"},
		{name: "linux private tmp", platform: "Linux", physicalPath: "/private/tmp/test"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			f := newGoTestShardFixtureWithExit(t, 0)
			// Simulate BSD realpath rejecting GNU options even on Linux. The runner
			// must canonicalize with portable shell builtins, not invoke this command.
			if err := os.WriteFile(filepath.Join(f.tmpDir, "bin", "realpath"), []byte("#!/bin/sh\necho 'realpath: illegal option -- e' >&2\nexit 1\n"), 0o755); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(f.binDir, "uname"), []byte("#!/bin/sh\nprintf '%s\\n' '"+test.platform+"'\n"), 0o755); err != nil {
				t.Fatal(err)
			}
			bashEnv := filepath.Join(f.tmpDir, "bash-env")
			bashEnvScript := `pwd() {
  if [[ "${1:-}" == "-P" ]]; then
    local calls=0
    if [[ -f "$GC_TEST_PWD_COUNT" ]]; then
      IFS= read -r calls < "$GC_TEST_PWD_COUNT"
    fi
    calls=$((calls + 1))
    printf '%s\n' "$calls" > "$GC_TEST_PWD_COUNT"
    if (( calls == 1 )); then
      builtin pwd -P
    else
      printf '%s\n' "$GC_TEST_PHYSICAL_PATH"
    fi
  else
    builtin pwd "$@"
  fi
}`
			if err := os.WriteFile(bashEnv, []byte(bashEnvScript), 0o600); err != nil {
				t.Fatal(err)
			}
			alias := filepath.Join(t.TempDir(), "alias")
			if err := os.Symlink(f.tmpDir, alias); err != nil {
				t.Fatal(err)
			}
			cmd := f.command(
				"TMPDIR="+alias,
				"BASH_ENV="+bashEnv,
				"GC_TEST_PWD_COUNT="+filepath.Join(f.tmpDir, "pwd-count"),
				"GC_TEST_PHYSICAL_PATH="+test.physicalPath,
				"GC_CITY_PATH=/live/city",
				"GC_DOLT_HOST=live-host",
				"GC_DOLT_PORT=3306",
				"BEADS_DIR=/live/beads",
				"BEADS_DOLT_SERVER_PORT=3306",
			)
			if code, out := runShardCommand(t, cmd); code != 0 {
				t.Fatalf("shard exit %d: %s", code, out)
			}
			env := fixtureEnvironment(t, readFixtureFile(t, f.productEnvFile))
			wantTMPDIR, err := filepath.EvalSymlinks(f.tmpDir)
			if err != nil {
				t.Fatal(err)
			}
			if test.platform == "Darwin" && (wantTMPDIR == "/private/var" || strings.HasPrefix(wantTMPDIR, "/private/var/") || wantTMPDIR == "/private/tmp" || strings.HasPrefix(wantTMPDIR, "/private/tmp/")) {
				wantTMPDIR = strings.TrimPrefix(wantTMPDIR, "/private")
			}
			if env["TMPDIR"] != wantTMPDIR {
				t.Errorf("TMPDIR = %q, want canonical %q", env["TMPDIR"], wantTMPDIR)
			}
			wantGitConfigRoot := test.physicalPath
			if test.platform == "Darwin" && (wantGitConfigRoot == "/private/var" || strings.HasPrefix(wantGitConfigRoot, "/private/var/") || wantGitConfigRoot == "/private/tmp" || strings.HasPrefix(wantGitConfigRoot, "/private/tmp/")) {
				wantGitConfigRoot = strings.TrimPrefix(wantGitConfigRoot, "/private")
			}
			if gitConfig := env["GIT_CONFIG_GLOBAL"]; !strings.HasPrefix(gitConfig, wantGitConfigRoot+"/gc-test-gitconfig-") {
				t.Errorf("GIT_CONFIG_GLOBAL = %q, want path under canonical root %q", gitConfig, wantGitConfigRoot)
			}
			for _, name := range []string{"GC_CITY_PATH", "GC_DOLT_HOST", "GC_DOLT_PORT", "BEADS_DIR", "BEADS_DOLT_SERVER_PORT"} {
				if value, ok := env[name]; ok {
					t.Errorf("ambient %s leaked into shard: %q", name, value)
				}
			}
		})
	}
}
