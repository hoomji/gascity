package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gastownhall/gascity/internal/citylayout"
	"github.com/gastownhall/gascity/internal/config"
	"github.com/gastownhall/gascity/internal/orders"
)

// writePackStateDirProbe creates a pack at packDir with one command whose
// run.sh prints GC_PACK_STATE_DIR and, when present, the contents of
// $GC_PACK_STATE_DIR/last.json.
func writePackStateDirProbe(t *testing.T, packDir, packName string) config.DiscoveredCommand {
	t.Helper()
	sourceDir := filepath.Join(packDir, "commands", "show")
	if err := os.MkdirAll(sourceDir, 0o755); err != nil {
		t.Fatal(err)
	}
	script := `#!/bin/sh
echo "state=$GC_PACK_STATE_DIR"
if [ -f "$GC_PACK_STATE_DIR/last.json" ]; then
  echo "last=$(cat "$GC_PACK_STATE_DIR/last.json")"
fi
`
	scriptPath := filepath.Join(sourceDir, "run.sh")
	if err := os.WriteFile(scriptPath, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	return config.DiscoveredCommand{
		Name:        "show",
		BindingName: filepath.Base(packDir),
		PackName:    packName,
		Command:     []string{"show"},
		RunScript:   scriptPath,
		PackDir:     packDir,
		SourceDir:   sourceDir,
	}
}

func runPackStateDirProbe(t *testing.T, entry config.DiscoveredCommand, cityDir string) map[string]string {
	t.Helper()
	var stdout, stderr bytes.Buffer
	if code := runDiscoveredCommand(entry, cityDir, "testcity", nil, strings.NewReader(""), &stdout, &stderr); code != 0 {
		t.Fatalf("runDiscoveredCommand exit %d, stderr=%s", code, stderr.String())
	}
	got := map[string]string{}
	for _, line := range strings.Split(strings.TrimSpace(stdout.String()), "\n") {
		if key, value, ok := strings.Cut(line, "="); ok {
			got[key] = value
		}
	}
	return got
}

func orderPackStateDir(t *testing.T, cityDir, packDir string) string {
	t.Helper()
	target := execStoreTarget{ScopeRoot: cityDir, ScopeKind: "city", Prefix: "ct"}
	a := orders.Order{
		Name:         "measure",
		Trigger:      "manual",
		Exec:         "true",
		Source:       filepath.Join(packDir, "orders", "measure.toml"),
		FormulaLayer: filepath.Join(packDir, "formulas"),
	}
	envSlice, err := orderExecEnvWithError(cityDir, nil, target, a, nil)
	if err != nil {
		t.Fatalf("orderExecEnvWithError: %v", err)
	}
	for _, entry := range envSlice {
		if key, value, ok := strings.Cut(entry, "="); ok && key == "GC_PACK_STATE_DIR" {
			return value
		}
	}
	t.Fatalf("order env has no GC_PACK_STATE_DIR: %v", envSlice)
	return ""
}

// TestPackStateDir_OrderAndCommandAgree proves an exec order and a command of
// the same pack see one GC_PACK_STATE_DIR (pack-spec §1.2.12: "The pack's state
// directory for this city"), including a pack imported from a remote cache
// whose [pack].name is scoped (hoomji/quota-watch) and differs from its
// directory name.
func TestPackStateDir_OrderAndCommandAgree(t *testing.T) {
	cases := []struct {
		name     string
		packRel  string
		packName string
		wantKey  string
	}{
		{name: "name matches dir", packRel: "packs/mypack", packName: "mypack", wantKey: "mypack"},
		{name: "scoped remote import", packRel: "gchome/cache/repos/0837c13e2080614bd986a56b9cb63c2631d85ef68323325de752e7248cb8dd3e/quota-watch", packName: "hoomji/quota-watch", wantKey: "quota-watch"},
		{name: "single-segment name differs from dir", packRel: "packs/k8s", packName: "k8s-example", wantKey: "k8s"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			root := t.TempDir()
			cityDir := filepath.Join(root, "city")
			if err := os.MkdirAll(cityDir, 0o755); err != nil {
				t.Fatal(err)
			}
			packDir := filepath.Join(root, tc.packRel)
			entry := writePackStateDirProbe(t, packDir, tc.packName)

			want := citylayout.PackStateDir(cityDir, tc.wantKey)
			if got := orderPackStateDir(t, cityDir, packDir); got != want {
				t.Fatalf("order GC_PACK_STATE_DIR = %q, want %q", got, want)
			}
			if got := runPackStateDirProbe(t, entry, cityDir)["state"]; got != want {
				t.Fatalf("command GC_PACK_STATE_DIR = %q, want %q (the order's dir)", got, want)
			}
		})
	}
}

// TestPackStateDir_CommandAdoptsScopedLegacyDir proves state a command wrote
// under the old scoped-name key (packs/hoomji/quota-watch) moves to the
// canonical dir on the next command run when the canonical dir does not exist,
// so the command still finds it.
func TestPackStateDir_CommandAdoptsScopedLegacyDir(t *testing.T) {
	root := t.TempDir()
	cityDir := filepath.Join(root, "city")
	packDir := filepath.Join(root, "cache", "quota-watch")
	entry := writePackStateDirProbe(t, packDir, "hoomji/quota-watch")

	legacy := citylayout.PackStateDir(cityDir, "hoomji/quota-watch")
	if err := os.MkdirAll(legacy, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(legacy, "last.json"), []byte(`{"from":"legacy"}`), 0o600); err != nil {
		t.Fatal(err)
	}

	got := runPackStateDirProbe(t, entry, cityDir)
	canonical := citylayout.PackStateDir(cityDir, "quota-watch")
	if got["state"] != canonical {
		t.Fatalf("command GC_PACK_STATE_DIR = %q, want %q", got["state"], canonical)
	}
	if got["last"] != `{"from":"legacy"}` {
		t.Fatalf("command did not see migrated state: last=%q", got["last"])
	}
	if _, err := os.Stat(legacy); !os.IsNotExist(err) {
		t.Fatalf("legacy dir %s still present after migration (err=%v)", legacy, err)
	}
	if _, err := os.Stat(filepath.Dir(legacy)); !os.IsNotExist(err) {
		t.Fatalf("empty legacy parent %s left behind (err=%v)", filepath.Dir(legacy), err)
	}
}

// TestPackStateDir_CommandKeepsCanonicalWhenBothExist proves the canonical dir
// wins when both exist (the order already wrote there), and the legacy dir is
// left untouched rather than merged or deleted.
func TestPackStateDir_CommandKeepsCanonicalWhenBothExist(t *testing.T) {
	root := t.TempDir()
	cityDir := filepath.Join(root, "city")
	packDir := filepath.Join(root, "cache", "quota-watch")
	entry := writePackStateDirProbe(t, packDir, "hoomji/quota-watch")

	legacy := citylayout.PackStateDir(cityDir, "hoomji/quota-watch")
	canonical := citylayout.PackStateDir(cityDir, "quota-watch")
	for dir, body := range map[string]string{legacy: `{"from":"legacy"}`, canonical: `{"from":"order"}`} {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, "last.json"), []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}

	got := runPackStateDirProbe(t, entry, cityDir)
	if got["state"] != canonical || got["last"] != `{"from":"order"}` {
		t.Fatalf("command state=%q last=%q, want %q with the order's data", got["state"], got["last"], canonical)
	}
	data, err := os.ReadFile(filepath.Join(legacy, "last.json"))
	if err != nil || string(data) != `{"from":"legacy"}` {
		t.Fatalf("legacy dir was modified: data=%q err=%v", data, err)
	}
}

// TestPackStateDir_CommandLeavesUnscopedLegacyDir proves a single-segment
// legacy name is never moved: packs/<name> may be another pack's canonical
// dir (a pack named "dolt" in dir "my-dolt" must not take the system dolt
// pack's state).
func TestPackStateDir_CommandLeavesUnscopedLegacyDir(t *testing.T) {
	root := t.TempDir()
	cityDir := filepath.Join(root, "city")
	packDir := filepath.Join(root, "packs", "my-dolt")
	entry := writePackStateDirProbe(t, packDir, "dolt")

	legacy := citylayout.PackStateDir(cityDir, "dolt")
	if err := os.MkdirAll(legacy, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(legacy, "last.json"), []byte(`{"owner":"system-dolt"}`), 0o600); err != nil {
		t.Fatal(err)
	}

	got := runPackStateDirProbe(t, entry, cityDir)
	if want := citylayout.PackStateDir(cityDir, "my-dolt"); got["state"] != want {
		t.Fatalf("command GC_PACK_STATE_DIR = %q, want %q", got["state"], want)
	}
	if got["last"] != "" {
		t.Fatalf("command read another pack's state: last=%q", got["last"])
	}
	if _, err := os.Stat(filepath.Join(legacy, "last.json")); err != nil {
		t.Fatalf("unscoped legacy dir was moved: %v", err)
	}
}
