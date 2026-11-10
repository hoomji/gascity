package git

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// hardenedTestFile writes a file under dir and fails the test on error.
func hardenedTestFile(t *testing.T, dir, name, content string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o644); err != nil {
		t.Fatalf("write %s: %v", name, err)
	}
}

// TestHardenedStatus pins the read shape: branch plus per-file porcelain
// entries, with renames carrying their origin path.
func TestHardenedStatus(t *testing.T) {
	dir := initTestRepo(t)
	branch := strings.TrimSpace(runGit(t, dir, "branch", "--show-current"))
	hardenedTestFile(t, dir, "tracked.txt", "v1\n")
	runGit(t, dir, "add", "tracked.txt")
	runGit(t, dir, "commit", "-m", "add tracked")
	hardenedTestFile(t, dir, "tracked.txt", "v2\n")
	hardenedTestFile(t, dir, "new.txt", "new\n")

	st, err := Hardened{Dir: dir}.Status(context.Background())
	if err != nil {
		t.Fatalf("Status: %v", err)
	}
	if st.Branch != branch || st.Detached {
		t.Errorf("branch = %q detached %v, want %q attached", st.Branch, st.Detached, branch)
	}
	if st.Upstream != "" || st.Ahead != 0 || st.Behind != 0 {
		t.Errorf("upstream = %q ahead %d behind %d, want no upstream", st.Upstream, st.Ahead, st.Behind)
	}
	byPath := map[string]HardenedStatusEntry{}
	for _, e := range st.Entries {
		byPath[e.Path] = e
	}
	if e, ok := byPath["tracked.txt"]; !ok || e.Index != " " || e.Worktree != "M" {
		t.Errorf("tracked.txt = %+v, want unstaged modification", byPath["tracked.txt"])
	}
	if e, ok := byPath["new.txt"]; !ok || e.Index != "?" || e.Worktree != "?" {
		t.Errorf("new.txt = %+v, want untracked", byPath["new.txt"])
	}
}

// TestParseBranchLine pins the ## header shapes: plain branch, branch with
// upstream, ahead/behind counts, gone upstream, headless, and detached.
func TestParseBranchLine(t *testing.T) {
	cases := []struct {
		line          string
		branch        string
		detached      bool
		upstream      string
		ahead, behind int
	}{
		{"## main", "main", false, "", 0, 0},
		{"## main...origin/main", "main", false, "origin/main", 0, 0},
		{"## main...origin/main [ahead 2]", "main", false, "origin/main", 2, 0},
		{"## main...origin/main [behind 3]", "main", false, "origin/main", 0, 3},
		{"## main...origin/main [ahead 1, behind 2]", "main", false, "origin/main", 1, 2},
		{"## feature...origin/feature [gone]", "feature", false, "origin/feature", 0, 0},
		{"## HEAD (no branch)", "", true, "", 0, 0},
		{"## No commits yet on main", "main", false, "", 0, 0},
	}
	for _, tc := range cases {
		t.Run(tc.line, func(t *testing.T) {
			branch, detached, upstream, ahead, behind := parseBranchLine(tc.line)
			if branch != tc.branch || detached != tc.detached || upstream != tc.upstream || ahead != tc.ahead || behind != tc.behind {
				t.Errorf("parseBranchLine(%q) = %q %v %q %d %d, want %q %v %q %d %d",
					tc.line, branch, detached, upstream, ahead, behind,
					tc.branch, tc.detached, tc.upstream, tc.ahead, tc.behind)
			}
		})
	}
}

// TestHardenedBranch pins branch, detached, and sha on and off a branch.
func TestHardenedBranch(t *testing.T) {
	dir := initTestRepo(t)
	branch := strings.TrimSpace(runGit(t, dir, "branch", "--show-current"))
	sha := strings.TrimSpace(runGit(t, dir, "rev-parse", "HEAD"))

	got, err := Hardened{Dir: dir}.Branch(context.Background())
	if err != nil {
		t.Fatalf("Branch: %v", err)
	}
	if got.Branch != branch || got.Detached || got.SHA != sha {
		t.Errorf("Branch() = %+v, want branch %q sha %q attached", got, branch, sha)
	}

	runGit(t, dir, "checkout", "--detach", "HEAD")
	got, err = Hardened{Dir: dir}.Branch(context.Background())
	if err != nil {
		t.Fatalf("Branch detached: %v", err)
	}
	if !got.Detached || got.SHA != sha {
		t.Errorf("detached Branch() = %+v, want detached sha %q", got, sha)
	}
}

// TestHardenedDiffUncommitted pins the uncommitted shape: tracked changes
// carry patch plus before/after blobs, untracked files carry after only.
func TestHardenedDiffUncommitted(t *testing.T) {
	dir := initTestRepo(t)
	hardenedTestFile(t, dir, "tracked.txt", "one\n")
	runGit(t, dir, "add", "tracked.txt")
	runGit(t, dir, "commit", "-m", "add tracked")
	hardenedTestFile(t, dir, "tracked.txt", "two\n")
	hardenedTestFile(t, dir, "new.txt", "hello\n")

	diff, err := Hardened{Dir: dir}.DiffUncommitted(context.Background(), nil, 1<<20)
	if err != nil {
		t.Fatalf("DiffUncommitted: %v", err)
	}
	if diff.Mode != "uncommitted" || diff.HeadSHA == "" {
		t.Errorf("mode = %q head = %q, want uncommitted with a head sha", diff.Mode, diff.HeadSHA)
	}
	byPath := map[string]HardenedDiffFile{}
	for _, f := range diff.Files {
		byPath[f.Path] = f
	}
	mod, ok := byPath["tracked.txt"]
	if !ok {
		t.Fatalf("no tracked.txt in %+v", diff.Files)
	}
	if mod.Status != "modified" || mod.Truncated {
		t.Errorf("tracked.txt status = %q truncated %v", mod.Status, mod.Truncated)
	}
	if !strings.Contains(mod.Patch, "-one") || !strings.Contains(mod.Patch, "+two") {
		t.Errorf("tracked.txt patch missing the change:\n%s", mod.Patch)
	}
	if mod.Before == nil || mod.Before.Content != "one\n" {
		t.Errorf("tracked.txt before = %+v, want the HEAD blob", mod.Before)
	}
	if mod.After == nil || mod.After.Content != "two\n" {
		t.Errorf("tracked.txt after = %+v, want the worktree file", mod.After)
	}
	added, ok := byPath["new.txt"]
	if !ok {
		t.Fatalf("no new.txt in %+v", diff.Files)
	}
	if added.Status != "untracked" || added.Before != nil || added.Patch != "" {
		t.Errorf("new.txt = %+v, want untracked with after only", added)
	}
	if added.After == nil || added.After.Content != "hello\n" {
		t.Errorf("new.txt after = %+v, want the worktree file", added.After)
	}
}

// TestHardenedDiffUncommittedExpandsUntrackedDirs pins that an untracked
// directory diffs as its files (ignored files excluded), not as one dir
// entry the reader would choke on.
func TestHardenedDiffUncommittedExpandsUntrackedDirs(t *testing.T) {
	dir := initTestRepo(t)
	sub := filepath.Join(dir, "sub")
	if err := os.MkdirAll(filepath.Join(sub, "nested"), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	hardenedTestFile(t, dir, "sub/a.txt", "a\n")
	hardenedTestFile(t, dir, "sub/nested/b.txt", "b\n")
	hardenedTestFile(t, dir, ".gitignore", "ignored.txt\n")
	hardenedTestFile(t, dir, "sub/ignored.txt", "ignored\n")

	diff, err := Hardened{Dir: dir}.DiffUncommitted(context.Background(), nil, 1<<20)
	if err != nil {
		t.Fatalf("DiffUncommitted: %v", err)
	}
	byPath := map[string]HardenedDiffFile{}
	for _, f := range diff.Files {
		byPath[f.Path] = f
	}
	for _, want := range []string{"sub/a.txt", "sub/nested/b.txt"} {
		f, ok := byPath[want]
		if !ok || f.Status != "untracked" || f.After == nil {
			t.Errorf("%s = %+v, want untracked with after", want, byPath[want])
		}
	}
	for _, p := range []string{"sub/", "sub/ignored.txt"} {
		if _, ok := byPath[p]; ok {
			t.Errorf("diff holds %q, want neither dir entries nor ignored files", p)
		}
	}
}

// TestHardenedDiffSinceBase pins the since-a-base shape: the merge-base diff,
// with before/after blobs from the two commits.
func TestHardenedDiffSinceBase(t *testing.T) {
	dir := initTestRepo(t)
	hardenedTestFile(t, dir, "f.txt", "base\n")
	runGit(t, dir, "add", "f.txt")
	runGit(t, dir, "commit", "-m", "base file")
	base := strings.TrimSpace(runGit(t, dir, "rev-parse", "HEAD"))
	runGit(t, dir, "checkout", "-b", "feature")
	hardenedTestFile(t, dir, "f.txt", "feature\n")
	runGit(t, dir, "commit", "-am", "feature change")

	diff, err := Hardened{Dir: dir}.DiffSinceBase(context.Background(), base, nil, 1<<20)
	if err != nil {
		t.Fatalf("DiffSinceBase: %v", err)
	}
	if diff.Mode != "base" {
		t.Errorf("mode = %q, want base", diff.Mode)
	}
	if len(diff.Files) != 1 || diff.Files[0].Path != "f.txt" {
		t.Fatalf("files = %+v, want only f.txt", diff.Files)
	}
	f := diff.Files[0]
	if f.Status != "modified" || !strings.Contains(f.Patch, "+feature") {
		t.Errorf("f.txt = %+v, want the feature patch", f)
	}
	if f.Before == nil || f.Before.Content != "base\n" || f.After == nil || f.After.Content != "feature\n" {
		t.Errorf("f.txt before/after = %+v/%+v", f.Before, f.After)
	}
}

// TestHardenedDiffCommit pins the one-commit shape: first-parent diff.
func TestHardenedDiffCommit(t *testing.T) {
	dir := initTestRepo(t)
	hardenedTestFile(t, dir, "f.txt", "v1\n")
	runGit(t, dir, "add", "f.txt")
	runGit(t, dir, "commit", "-m", "one")
	hardenedTestFile(t, dir, "f.txt", "v2\n")
	runGit(t, dir, "commit", "-am", "two")
	sha := strings.TrimSpace(runGit(t, dir, "rev-parse", "HEAD"))

	diff, err := Hardened{Dir: dir}.DiffCommit(context.Background(), sha, nil, 1<<20)
	if err != nil {
		t.Fatalf("DiffCommit: %v", err)
	}
	if diff.Mode != "commit" {
		t.Errorf("mode = %q, want commit", diff.Mode)
	}
	if len(diff.Files) != 1 || diff.Files[0].Path != "f.txt" {
		t.Fatalf("files = %+v, want only f.txt", diff.Files)
	}
	f := diff.Files[0]
	if !strings.Contains(f.Patch, "+v2") || f.Before == nil || f.Before.Content != "v1\n" {
		t.Errorf("commit diff = %+v, want the v1->v2 change", f)
	}
}

// TestHardenedRefNotFound pins unknown and malformed refs.
func TestHardenedRefNotFound(t *testing.T) {
	dir := initTestRepo(t)
	h := Hardened{Dir: dir}
	if _, err := h.DiffSinceBase(context.Background(), "no-such-branch", nil, 1<<20); !errors.Is(err, ErrNoSuchRef) {
		t.Errorf("unknown base err = %v, want ErrNoSuchRef", err)
	}
	if _, err := h.DiffCommit(context.Background(), "deadbee", nil, 1<<20); !errors.Is(err, ErrNoSuchRef) {
		t.Errorf("unknown commit err = %v, want ErrNoSuchRef", err)
	}
	if _, err := h.DiffSinceBase(context.Background(), "--output=/tmp/x", nil, 1<<20); !errors.Is(err, ErrBadRef) {
		t.Errorf("leading-dash base err = %v, want ErrBadRef", err)
	}
}

// TestHardenedNotARepo pins the plain-directory shape.
func TestHardenedNotARepo(t *testing.T) {
	h := Hardened{Dir: t.TempDir()}
	if _, err := h.Status(context.Background()); !errors.Is(err, ErrNoRepo) {
		t.Errorf("Status err = %v, want ErrNoRepo", err)
	}
	if _, err := h.Branch(context.Background()); !errors.Is(err, ErrNoRepo) {
		t.Errorf("Branch err = %v, want ErrNoRepo", err)
	}
}

// TestHardenedRefusesUnsafeRepoConfig pins TB-AD-12's precheck: every listed
// key family, when set in local config, refuses every call.
func TestHardenedRefusesUnsafeRepoConfig(t *testing.T) {
	keys := []string{
		"core.fsmonitor",
		"core.hooksPath",
		"core.sshCommand",
		"core.pager",
		"core.editor",
		"core.askPass",
		"core.alternateRefsCommand",
		"sequence.editor",
		"diff.external",
		"diff.foo.command",
		"foo.textconv",
		"filter.foo.clean",
		"merge.foo.driver",
		"gpg.format",
		"credential.helper",
		"include.path",
		"includeIf.gitdir:/tmp/x.path",
		"uploadpack.allowReachableSHA1InWant",
		"remote.origin.uploadpack",
		"remote.origin.receivepack",
	}
	for _, key := range keys {
		t.Run(key, func(t *testing.T) {
			dir := initTestRepo(t)
			runGit(t, dir, "config", "--local", key, "/tmp/planted")
			if _, err := (Hardened{Dir: dir}).Status(context.Background()); !errors.Is(err, ErrUnsafeConfig) {
				t.Errorf("Status with %s set: err = %v, want ErrUnsafeConfig", key, err)
			}
			if _, err := (Hardened{Dir: dir}).Branch(context.Background()); !errors.Is(err, ErrUnsafeConfig) {
				t.Errorf("Branch with %s set: err = %v, want ErrUnsafeConfig", key, err)
			}
		})
	}
}

// TestHardenedRefusesUnsafeKeyThroughInclude pins the included-config arm: an
// unsafe key smuggled in through an include file refuses, whatever scope git
// reports for the included line.
func TestHardenedRefusesUnsafeKeyThroughInclude(t *testing.T) {
	dir := initTestRepo(t)
	extra := filepath.Join(dir, "extra.inc")
	if err := os.WriteFile(extra, []byte("[core]\n\tpager = /tmp/planted\n"), 0o644); err != nil {
		t.Fatalf("write include: %v", err)
	}
	runGit(t, dir, "config", "--local", "include.path", extra)
	if _, err := (Hardened{Dir: dir}).Status(context.Background()); !errors.Is(err, ErrUnsafeConfig) {
		t.Errorf("Status with an unsafe key through include: err = %v, want ErrUnsafeConfig", err)
	}
}

// TestHardenedAllowsSafeRepoConfig pins the precheck's other side: ordinary
// local keys (identity, branch layout) never trip it.
func TestHardenedAllowsSafeRepoConfig(t *testing.T) {
	dir := initTestRepo(t)
	runGit(t, dir, "config", "--local", "user.email", "local@test.com")
	runGit(t, dir, "config", "--local", "branch.main.remote", "origin")
	if _, err := (Hardened{Dir: dir}).Status(context.Background()); err != nil {
		t.Errorf("Status with safe local config: %v", err)
	}
}

// TestHardenedHookNeverExecutes pins that reads run no hooks: a planted hook
// fires on no call, and pointing hooksPath at it refuses instead.
func TestHardenedHookNeverExecutes(t *testing.T) {
	dir := initTestRepo(t)
	marker := filepath.Join(dir, "hook-marker")
	hookDir := filepath.Join(dir, ".git", "hooks")
	hook := "#!/bin/sh\ntouch " + marker + "\n"
	if err := os.WriteFile(filepath.Join(hookDir, "post-checkout"), []byte(hook), 0o755); err != nil {
		t.Fatalf("plant hook: %v", err)
	}
	if _, err := (Hardened{Dir: dir}).Status(context.Background()); err != nil {
		t.Fatalf("Status with a planted hook: %v", err)
	}
	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Errorf("hook marker exists: reads must not execute hooks")
	}
	runGit(t, dir, "config", "--local", "core.hooksPath", hookDir)
	if _, err := (Hardened{Dir: dir}).Status(context.Background()); !errors.Is(err, ErrUnsafeConfig) {
		t.Errorf("Status with hooksPath set: err = %v, want ErrUnsafeConfig", err)
	}
	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Errorf("hook marker exists after the refused call")
	}
}

// TestReadOnlyEnvAllowlist pins the environment contract: PATH and HOME pass
// through, the TB-AD-12 locks are set, everything else (including every
// GIT_* locating variable) is dropped.
func TestReadOnlyEnvAllowlist(t *testing.T) {
	got := ReadOnlyEnv([]string{
		"PATH=/usr/bin:/bin",
		"HOME=/home/op",
		"GIT_DIR=/tmp/evil",
		"GIT_WORK_TREE=/tmp/evil",
		"GIT_PAGER=less",
		"EDITOR=vim",
		"LC_ALL=en_US.UTF-8",
		"SNEAKY=1",
	})
	want := map[string]string{
		"PATH":                "/usr/bin:/bin",
		"HOME":                "/home/op",
		"LC_ALL":              "C",
		"GIT_TERMINAL_PROMPT": "0",
		"GIT_PAGER":           "cat",
		"GIT_EDITOR":          "true",
		"GIT_ASKPASS":         "/bin/false",
		"GIT_OPTIONAL_LOCKS":  "0",
	}
	seen := map[string]string{}
	for _, e := range got {
		k, v, _ := strings.Cut(e, "=")
		seen[k] = v
	}
	if len(seen) != len(want) {
		t.Errorf("env has %d vars, want %d: %v", len(seen), len(want), got)
	}
	for k, v := range want {
		if seen[k] != v {
			t.Errorf("%s = %q, want %q", k, seen[k], v)
		}
	}
}

// TestHardenedTruncates pins the byte cap: a file past maxBytes truncates
// with the flag set, and the returned content never exceeds the cap.
func TestHardenedTruncates(t *testing.T) {
	dir := initTestRepo(t)
	big := strings.Repeat("x\n", 10000)
	hardenedTestFile(t, dir, "big.txt", "seed\n")
	runGit(t, dir, "add", "big.txt")
	runGit(t, dir, "commit", "-m", "seed big")
	hardenedTestFile(t, dir, "big.txt", big)
	runGit(t, dir, "add", "big.txt")

	diff, err := Hardened{Dir: dir}.DiffUncommitted(context.Background(), nil, 1024)
	if err != nil {
		t.Fatalf("DiffUncommitted: %v", err)
	}
	if len(diff.Files) != 1 {
		t.Fatalf("files = %+v, want only big.txt", diff.Files)
	}
	f := diff.Files[0]
	if !f.Truncated {
		t.Errorf("Truncated = false for a 20KB file under a 1KB cap")
	}
	if int64(len(f.Patch)) > 1024 {
		t.Errorf("patch is %d bytes, want at most the 1024 cap", len(f.Patch))
	}
	if f.After == nil || int64(len(f.After.Content)) > 1024 {
		t.Errorf("after is %+v, want capped content", f.After)
	}
}
