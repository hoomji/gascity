package api

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gastownhall/gascity/internal/api/apierr"
	"github.com/gastownhall/gascity/internal/beads"
	"github.com/gastownhall/gascity/internal/session"
)

// workspaceGit runs git in dir and fails the test on error.
func workspaceGit(t *testing.T, dir string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Dir = dir
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %s: %s: %v", strings.Join(args, " "), out, err)
	}
	return string(out)
}

// workspaceTestRepo builds a temp git repo with one commit and returns its
// path and branch.
func workspaceTestRepo(t *testing.T) (dir, branch string) {
	t.Helper()
	dir = t.TempDir()
	workspaceGit(t, dir, "init")
	workspaceGit(t, dir, "config", "user.email", "ws@test.com")
	workspaceGit(t, dir, "config", "user.name", "WS")
	if err := os.WriteFile(filepath.Join(dir, "f.txt"), []byte("v1\n"), 0o644); err != nil {
		t.Fatalf("write f.txt: %v", err)
	}
	workspaceGit(t, dir, "add", "f.txt")
	workspaceGit(t, dir, "commit", "-qm", "one")
	branch = strings.TrimSpace(workspaceGit(t, dir, "branch", "--show-current"))
	return dir, branch
}

// seedWorkspaceSession creates a session bead carrying metadata and returns
// its id.
func seedWorkspaceSession(t *testing.T, fs *fakeState, metadata map[string]string) string {
	t.Helper()
	meta := map[string]string{"session_name": "s-ws-" + strings.ReplaceAll(t.Name(), "/", "_"), "state": "active"}
	for k, v := range metadata {
		meta[k] = v
	}
	b, err := fs.cityBeadStore.Create(beads.Bead{
		Title:    "ws session",
		Type:     session.BeadType,
		Labels:   []string{session.LabelSession},
		Metadata: meta,
	})
	if err != nil {
		t.Fatalf("create session bead: %v", err)
	}
	return b.ID
}

// workspaceErrCode extracts the apierr code from a handler error.
func workspaceErrCode(t *testing.T, err error) (string, int) {
	t.Helper()
	if err == nil {
		t.Fatalf("err = nil, want a problem error")
	}
	var em *apierr.ErrorModel
	if !errors.As(err, &em) {
		t.Fatalf("err type %T, want *apierr.ErrorModel (%v)", err, err)
	}
	return em.Code, em.Status
}

// TestResolveWorkspaceFolderGates pins the folder gate table: remote host,
// missing folder, relative folder, missing dir, file-not-dir, and forbidden
// folders refuse with the catalog's codes; a plain folder resolves.
func TestResolveWorkspaceFolderGates(t *testing.T) {
	repo, _ := workspaceTestRepo(t)
	plain := t.TempDir()
	file := filepath.Join(plain, "file.txt")
	if err := os.WriteFile(file, []byte("x"), 0o644); err != nil {
		t.Fatalf("write file: %v", err)
	}
	rootlink := filepath.Join(plain, "rootlink")
	if err := os.Symlink("/", rootlink); err != nil {
		t.Fatalf("symlink: %v", err)
	}
	home, homeErr := os.UserHomeDir()
	local := session.LocalSessionHost()
	if local == "" {
		t.Skip("no local hostname; cannot pin the host gate")
	}

	cases := []struct {
		name     string
		meta     map[string]string
		wantCode string
		wantOK   bool
	}{
		{"remote-host", map[string]string{"host": "remote-host.invalid", "gc.work_dir": repo}, "unsupported_runtime", false},
		{"no-folder", map[string]string{"host": local}, "unsupported_runtime", false},
		{"relative-folder", map[string]string{"host": local, "gc.work_dir": "rel/path"}, "unsupported_runtime", false},
		{"missing-folder", map[string]string{"host": local, "gc.work_dir": filepath.Join(plain, "nope")}, "unsupported_runtime", false},
		{"file-not-dir", map[string]string{"host": local, "gc.work_dir": file}, "unsupported_runtime", false},
		{"root-forbidden", map[string]string{"host": local, "gc.work_dir": "/"}, "forbidden", false},
		{"symlink-to-root-forbidden", map[string]string{"host": local, "gc.work_dir": rootlink}, "forbidden", false},
		{"legacy-work-dir-ok", map[string]string{"host": local, "work_dir": repo}, "", true},
		{"repo-ok", map[string]string{"host": local, "gc.work_dir": repo}, "", true},
		{"absent-host-reads-local", map[string]string{"gc.work_dir": repo}, "", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			fs := newSessionFakeState(t)
			srv := New(fs)
			id := seedWorkspaceSession(t, fs, tc.meta)
			folder, err := srv.resolveWorkspaceFolder(fs.Stores().Sessions(), id)
			if tc.wantOK {
				if err != nil {
					t.Fatalf("resolveWorkspaceFolder: %v", err)
				}
				if same, _ := filepath.EvalSymlinks(repo); folder != same && tc.meta["gc.work_dir"] == repo {
					t.Errorf("folder = %q, want resolved %q", folder, same)
				}
				return
			}
			code, _ := workspaceErrCode(t, err)
			if code != tc.wantCode {
				t.Errorf("code = %q, want %q (err %v)", code, tc.wantCode, err)
			}
		})
	}

	if homeErr == nil && home != "" && home != "/" {
		for name, folder := range map[string]string{"home-forbidden": home, "home-parent-forbidden": filepath.Dir(home)} {
			t.Run(name, func(t *testing.T) {
				fs := newSessionFakeState(t)
				srv := New(fs)
				id := seedWorkspaceSession(t, fs, map[string]string{"host": local, "gc.work_dir": folder})
				_, err := srv.resolveWorkspaceFolder(fs.Stores().Sessions(), id)
				code, _ := workspaceErrCode(t, err)
				if code != "forbidden" {
					t.Errorf("code = %q, want forbidden (err %v)", code, err)
				}
			})
		}
	}

	t.Run("unknown-session-404", func(t *testing.T) {
		fs := newSessionFakeState(t)
		srv := New(fs)
		_, err := srv.resolveWorkspaceFolder(fs.Stores().Sessions(), "no-such-session")
		_, status := workspaceErrCode(t, err)
		if status != http.StatusNotFound {
			t.Errorf("status = %d, want 404 (err %v)", status, err)
		}
	})
}

// TestWorkspaceGitStatus pins the status shape over a seeded session: branch,
// entries with states, and the ok path through the folder gate.
func TestWorkspaceGitStatus(t *testing.T) {
	repo, branch := workspaceTestRepo(t)
	if err := os.WriteFile(filepath.Join(repo, "f.txt"), []byte("v2\n"), 0o644); err != nil {
		t.Fatalf("modify f.txt: %v", err)
	}
	if err := os.WriteFile(filepath.Join(repo, "new.txt"), []byte("new\n"), 0o644); err != nil {
		t.Fatalf("write new.txt: %v", err)
	}
	fs := newSessionFakeState(t)
	srv := New(fs)
	id := seedWorkspaceSession(t, fs, map[string]string{"gc.work_dir": repo})

	out, err := srv.humaHandleWorkspaceGitStatus(context.Background(), &WorkspaceGitStatusInput{ID: id})
	if err != nil {
		t.Fatalf("status: %v", err)
	}
	if out.Body.Branch != branch || out.Body.Detached {
		t.Errorf("branch = %q detached %v, want %q attached", out.Body.Branch, out.Body.Detached, branch)
	}
	byPath := map[string]gitStatusEntry{}
	for _, e := range out.Body.Entries {
		byPath[e.Path] = e
	}
	if e, ok := byPath["f.txt"]; !ok || e.State != "modified" || e.Staged {
		t.Errorf("f.txt = %+v, want unstaged modified", byPath["f.txt"])
	}
	if e, ok := byPath["new.txt"]; !ok || e.State != "untracked" || e.Staged {
		t.Errorf("new.txt = %+v, want untracked", byPath["new.txt"])
	}
}

// TestWorkspaceGitBranch pins branch, detached, and sha.
func TestWorkspaceGitBranch(t *testing.T) {
	repo, branch := workspaceTestRepo(t)
	sha := strings.TrimSpace(workspaceGit(t, repo, "rev-parse", "HEAD"))
	fs := newSessionFakeState(t)
	srv := New(fs)
	id := seedWorkspaceSession(t, fs, map[string]string{"gc.work_dir": repo})

	out, err := srv.humaHandleWorkspaceGitBranch(context.Background(), &WorkspaceGitBranchInput{ID: id})
	if err != nil {
		t.Fatalf("branch: %v", err)
	}
	if out.Body.Branch != branch || out.Body.Detached || out.Body.SHA != sha {
		t.Errorf("branch = %+v, want %q %q attached", out.Body, branch, sha)
	}
}

// TestWorkspaceGitDiffUncommitted pins the uncommitted shape: patch plus
// before/after blobs for tracked changes, after-only for untracked.
func TestWorkspaceGitDiffUncommitted(t *testing.T) {
	repo, _ := workspaceTestRepo(t)
	if err := os.WriteFile(filepath.Join(repo, "f.txt"), []byte("v2\n"), 0o644); err != nil {
		t.Fatalf("modify f.txt: %v", err)
	}
	if err := os.WriteFile(filepath.Join(repo, "new.txt"), []byte("hello\n"), 0o644); err != nil {
		t.Fatalf("write new.txt: %v", err)
	}
	fs := newSessionFakeState(t)
	srv := New(fs)
	id := seedWorkspaceSession(t, fs, map[string]string{"gc.work_dir": repo})

	out, err := srv.humaHandleWorkspaceGitDiff(context.Background(), &WorkspaceGitDiffInput{ID: id})
	if err != nil {
		t.Fatalf("diff: %v", err)
	}
	if out.Body.Mode != "uncommitted" || out.Body.HeadSHA == "" {
		t.Errorf("mode = %q head %q, want uncommitted with head", out.Body.Mode, out.Body.HeadSHA)
	}
	byPath := map[string]gitDiffFile{}
	for _, f := range out.Body.Files {
		byPath[f.Path] = f
	}
	mod := byPath["f.txt"]
	if mod.Status != "modified" || mod.Before == nil || mod.Before.Content != "v1\n" {
		t.Errorf("f.txt = %+v, want modified with v1 before", mod)
	}
	if mod.After == nil || mod.After.Content != "v2\n" || !strings.Contains(mod.Patch, "+v2") {
		t.Errorf("f.txt after/patch = %+v, want v2 and the patch", mod)
	}
	added := byPath["new.txt"]
	if added.Status != "untracked" || added.Before != nil || added.Patch != "" {
		t.Errorf("new.txt = %+v, want untracked after-only", added)
	}
}

// TestWorkspaceGitDiffModes pins base and commit modes plus their required
// refs.
func TestWorkspaceGitDiffModes(t *testing.T) {
	repo, _ := workspaceTestRepo(t)
	base := strings.TrimSpace(workspaceGit(t, repo, "rev-parse", "HEAD"))
	workspaceGit(t, repo, "checkout", "-b", "feature")
	if err := os.WriteFile(filepath.Join(repo, "f.txt"), []byte("feature\n"), 0o644); err != nil {
		t.Fatalf("modify f.txt: %v", err)
	}
	workspaceGit(t, repo, "commit", "-aqm", "feature change")
	head := strings.TrimSpace(workspaceGit(t, repo, "rev-parse", "HEAD"))
	fs := newSessionFakeState(t)
	srv := New(fs)
	id := seedWorkspaceSession(t, fs, map[string]string{"gc.work_dir": repo})

	bd, err := srv.humaHandleWorkspaceGitDiff(context.Background(), &WorkspaceGitDiffInput{ID: id, Mode: "base", Base: base})
	if err != nil {
		t.Fatalf("base diff: %v", err)
	}
	if bd.Body.Mode != "base" || bd.Body.BaseSHA != base || bd.Body.MergeBaseSHA != base || bd.Body.HeadSHA != head {
		t.Errorf("base shas = %+v, want base=merge=%q head=%q", bd.Body, base, head)
	}
	if len(bd.Body.Files) != 1 || bd.Body.Files[0].Path != "f.txt" || !strings.Contains(bd.Body.Files[0].Patch, "+feature") {
		t.Errorf("base files = %+v, want the feature patch", bd.Body.Files)
	}

	cd, err := srv.humaHandleWorkspaceGitDiff(context.Background(), &WorkspaceGitDiffInput{ID: id, Mode: "commit", Commit: head})
	if err != nil {
		t.Fatalf("commit diff: %v", err)
	}
	if cd.Body.Mode != "commit" || cd.Body.CommitSHA != head {
		t.Errorf("commit = %+v, want commit %q", cd.Body, head)
	}
	if len(cd.Body.Files) != 1 || cd.Body.Files[0].Before == nil || cd.Body.Files[0].Before.Content != "v1\n" {
		t.Errorf("commit files = %+v, want v1 before", cd.Body.Files)
	}

	if _, err := srv.humaHandleWorkspaceGitDiff(context.Background(), &WorkspaceGitDiffInput{ID: id, Mode: "base"}); err == nil {
		t.Errorf("base mode without base = nil, want invalid-request")
	} else if code, _ := workspaceErrCode(t, err); code != "invalid-request" {
		t.Errorf("base-without-base code = %q, want invalid-request", code)
	}
	if _, err := srv.humaHandleWorkspaceGitDiff(context.Background(), &WorkspaceGitDiffInput{ID: id, Mode: "commit"}); err == nil {
		t.Errorf("commit mode without commit = nil, want invalid-request")
	} else if code, _ := workspaceErrCode(t, err); code != "invalid-request" {
		t.Errorf("commit-without-commit code = %q, want invalid-request", code)
	}
	if _, err := srv.humaHandleWorkspaceGitDiff(context.Background(), &WorkspaceGitDiffInput{ID: id, Mode: "bogus"}); err == nil {
		t.Errorf("bogus mode = nil, want invalid-request")
	} else if code, _ := workspaceErrCode(t, err); code != "invalid-request" {
		t.Errorf("bogus-mode code = %q, want invalid-request", code)
	}
}

// TestWorkspaceGitDiffPathFilter pins repeatable path filters and their
// containment: a directory filter narrows, an escape refuses.
func TestWorkspaceGitDiffPathFilter(t *testing.T) {
	repo, _ := workspaceTestRepo(t)
	if err := os.MkdirAll(filepath.Join(repo, "sub"), 0o755); err != nil {
		t.Fatalf("mkdir sub: %v", err)
	}
	if err := os.WriteFile(filepath.Join(repo, "f.txt"), []byte("v2\n"), 0o644); err != nil {
		t.Fatalf("modify f.txt: %v", err)
	}
	if err := os.WriteFile(filepath.Join(repo, "sub", "g.txt"), []byte("g\n"), 0o644); err != nil {
		t.Fatalf("write g.txt: %v", err)
	}
	fs := newSessionFakeState(t)
	srv := New(fs)
	id := seedWorkspaceSession(t, fs, map[string]string{"gc.work_dir": repo})

	out, err := srv.humaHandleWorkspaceGitDiff(context.Background(), &WorkspaceGitDiffInput{ID: id, Path: []string{"sub"}})
	if err != nil {
		t.Fatalf("filtered diff: %v", err)
	}
	if len(out.Body.Files) != 1 || out.Body.Files[0].Path != "sub/g.txt" {
		t.Errorf("filtered files = %+v, want only sub/g.txt", out.Body.Files)
	}

	for _, p := range []string{"../escape", "/abs", "sub/../../escape"} {
		if _, err := srv.humaHandleWorkspaceGitDiff(context.Background(), &WorkspaceGitDiffInput{ID: id, Path: []string{p}}); err == nil {
			t.Errorf("path %q = nil, want forbidden", p)
		} else if code, _ := workspaceErrCode(t, err); code != "forbidden" {
			t.Errorf("path %q code = %q, want forbidden", p, code)
		}
	}
}

// TestWorkspaceGitErrors pins the git failure map: bad refs, unsafe config,
// and non-repos answer the catalog's codes.
func TestWorkspaceGitErrors(t *testing.T) {
	repo, _ := workspaceTestRepo(t)
	fs := newSessionFakeState(t)
	srv := New(fs)
	id := seedWorkspaceSession(t, fs, map[string]string{"gc.work_dir": repo})

	if _, err := srv.humaHandleWorkspaceGitDiff(context.Background(), &WorkspaceGitDiffInput{ID: id, Mode: "base", Base: "no-such-ref"}); err == nil {
		t.Errorf("unknown base = nil, want not_found")
	} else if code, _ := workspaceErrCode(t, err); code != "not_found" {
		t.Errorf("unknown-base code = %q, want not_found", code)
	}
	if _, err := srv.humaHandleWorkspaceGitDiff(context.Background(), &WorkspaceGitDiffInput{ID: id, Mode: "commit", Commit: "--output=/tmp/x"}); err == nil {
		t.Errorf("dash commit = nil, want invalid-request")
	} else if code, _ := workspaceErrCode(t, err); code != "invalid-request" {
		t.Errorf("dash-commit code = %q, want invalid-request", code)
	}

	unsafeRepo, _ := workspaceTestRepo(t)
	workspaceGit(t, unsafeRepo, "config", "--local", "core.pager", "/tmp/planted")
	unsafeID := seedWorkspaceSession(t, fs, map[string]string{"gc.work_dir": unsafeRepo, "session_name": "s-ws-unsafe"})
	if _, err := srv.humaHandleWorkspaceGitStatus(context.Background(), &WorkspaceGitStatusInput{ID: unsafeID}); err == nil {
		t.Errorf("unsafe config = nil, want unsafe_repo_config")
	} else if code, _ := workspaceErrCode(t, err); code != "unsafe_repo_config" {
		t.Errorf("unsafe-config code = %q, want unsafe_repo_config", code)
	}

	plainID := seedWorkspaceSession(t, fs, map[string]string{"gc.work_dir": t.TempDir(), "session_name": "s-ws-plain"})
	if _, err := srv.humaHandleWorkspaceGitStatus(context.Background(), &WorkspaceGitStatusInput{ID: plainID}); err == nil {
		t.Errorf("non-repo = nil, want not_found")
	} else if code, _ := workspaceErrCode(t, err); code != "not_found" {
		t.Errorf("non-repo code = %q, want not_found", code)
	}
}

// TestWorkspaceGitRoutesRegistered pins the HTTP wiring: the status route
// serves through the city mux.
func TestWorkspaceGitRoutesRegistered(t *testing.T) {
	repo, branch := workspaceTestRepo(t)
	fs := newSessionFakeState(t)
	srv := New(fs)
	id := seedWorkspaceSession(t, fs, map[string]string{"gc.work_dir": repo})
	h := newTestCityHandlerWith(t, fs, srv)

	req := httptest.NewRequest("GET", cityURL(fs, "/session/"+id+"/workspace/git/status"), nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("GET status = %d, want 200 (%s)", rec.Code, rec.Body.String())
	}
	var body struct {
		Branch string `json:"branch"`
	}
	if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
		t.Fatalf("decode status body: %v", err)
	}
	if body.Branch != branch {
		t.Errorf("branch = %q, want %q", body.Branch, branch)
	}

	req = httptest.NewRequest("GET", cityURL(fs, "/session/no-such/workspace/git/branch"), nil)
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Errorf("GET unknown branch = %d, want 404 (%s)", rec.Code, rec.Body.String())
	}

	req = httptest.NewRequest("GET", cityURL(fs, "/session/"+id+"/workspace/git/diff?bogus_filter=1"), nil)
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusUnprocessableEntity {
		t.Errorf("GET diff with unknown query = %d, want 422 (%s)", rec.Code, rec.Body.String())
	}
}
