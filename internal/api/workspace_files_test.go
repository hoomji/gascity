package api

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"syscall"
	"testing"
	"time"
)

// workspaceFilesFixture builds a temp folder: top.txt, sub/nested.txt, an
// inside link, an outside link, and a loop pair. It returns the folder and a
// path outside it the outside link points at.
func workspaceFilesFixture(t *testing.T) (folder, outside string) {
	t.Helper()
	folder = t.TempDir()
	outside = filepath.Join(t.TempDir(), "secret.txt")
	if err := os.WriteFile(outside, []byte("secret\n"), 0o644); err != nil {
		t.Fatalf("write outside: %v", err)
	}
	if err := os.WriteFile(filepath.Join(folder, "top.txt"), []byte("top\n"), 0o644); err != nil {
		t.Fatalf("write top: %v", err)
	}
	if err := os.MkdirAll(filepath.Join(folder, "sub"), 0o755); err != nil {
		t.Fatalf("mkdir sub: %v", err)
	}
	if err := os.WriteFile(filepath.Join(folder, "sub", "nested.txt"), []byte("nested\n"), 0o644); err != nil {
		t.Fatalf("write nested: %v", err)
	}
	if err := os.Symlink("top.txt", filepath.Join(folder, "inside")); err != nil {
		t.Fatalf("inside link: %v", err)
	}
	if err := os.Symlink(outside, filepath.Join(folder, "outside")); err != nil {
		t.Fatalf("outside link: %v", err)
	}
	if err := os.Symlink("loop-b", filepath.Join(folder, "loop-a")); err != nil {
		t.Fatalf("loop-a: %v", err)
	}
	if err := os.Symlink("loop-a", filepath.Join(folder, "loop-b")); err != nil {
		t.Fatalf("loop-b: %v", err)
	}
	return folder, outside
}

// TestResolveWorkspacePath pins the containment table: inside paths resolve,
// escapes refuse, loops and empties are client errors, missing names 404.
func TestResolveWorkspacePath(t *testing.T) {
	folder, outside := workspaceFilesFixture(t)
	canonical, err := filepath.EvalSymlinks(folder)
	if err != nil {
		t.Fatalf("canonical folder: %v", err)
	}

	okCases := []struct {
		rel  string
		want string
	}{
		{"top.txt", filepath.Join(canonical, "top.txt")},
		{"sub/nested.txt", filepath.Join(canonical, "sub", "nested.txt")},
		{"./top.txt", filepath.Join(canonical, "top.txt")},
		{"sub/../top.txt", filepath.Join(canonical, "top.txt")},
		{".", canonical},
		{"inside", filepath.Join(canonical, "top.txt")},
	}
	for _, tc := range okCases {
		t.Run("ok/"+tc.rel, func(t *testing.T) {
			r, err := resolveWorkspacePath(canonical, tc.rel)
			got := r.Abs
			if err != nil {
				t.Fatalf("resolveWorkspacePath(%q): %v", tc.rel, err)
			}
			if got != tc.want {
				t.Errorf("resolveWorkspacePath(%q) = %q, want %q", tc.rel, got, tc.want)
			}
		})
	}

	t.Run("link-target-reported", func(t *testing.T) {
		r, err := resolveWorkspacePath(canonical, "inside")
		target := r.LinkTarget
		if err != nil {
			t.Fatalf("inside: %v", err)
		}
		if target != "top.txt" {
			t.Errorf("link target = %q, want top.txt", target)
		}
	})

	refuseCases := []struct {
		rel  string
		code string
	}{
		{"../escape", "forbidden"},
		{"sub/../../escape", "forbidden"},
		{"/abs", "forbidden"},
		{"outside", "forbidden"},
		{"sub/../../../" + filepath.Base(outside), "forbidden"},
		{"", "invalid-request"},
		{"loop-a", "invalid-request"},
		{"missing.txt", "not_found"},
	}
	for _, tc := range refuseCases {
		t.Run("refuse/"+tc.rel, func(t *testing.T) {
			_, err := resolveWorkspacePath(canonical, tc.rel)
			code, _ := workspaceErrCode(t, err)
			if code != tc.code {
				t.Errorf("resolveWorkspacePath(%q) code = %q, want %q (err %v)", tc.rel, code, tc.code, err)
			}
		})
	}

	t.Run("nul-refused", func(t *testing.T) {
		_, err := resolveWorkspacePath(canonical, "a\x00b")
		code, _ := workspaceErrCode(t, err)
		if code != "invalid-request" {
			t.Errorf("nul code = %q, want invalid-request", code)
		}
	})
}

// TestResolveWorkspacePathParentLink pins symlinked parent directories: a
// parent link staying inside resolves, one pointing out refuses.
func TestResolveWorkspacePathParentLink(t *testing.T) {
	folder, _ := workspaceFilesFixture(t)
	canonical, err := filepath.EvalSymlinks(folder)
	if err != nil {
		t.Fatalf("canonical folder: %v", err)
	}
	if err := os.Symlink("sub", filepath.Join(folder, "sublink")); err != nil {
		t.Fatalf("sublink: %v", err)
	}
	r, err := resolveWorkspacePath(canonical, "sublink/nested.txt")
	got := r.Abs
	if err != nil {
		t.Fatalf("inside parent link: %v", err)
	}
	if want := filepath.Join(canonical, "sub", "nested.txt"); got != want {
		t.Errorf("got %q, want %q", got, want)
	}

	outer := t.TempDir()
	if err := os.Symlink(outer, filepath.Join(folder, "outlink")); err != nil {
		t.Fatalf("outlink: %v", err)
	}
	if _, err := resolveWorkspacePath(canonical, "outlink/x"); err == nil {
		t.Errorf("outside parent link = nil, want forbidden")
	} else if code, _ := workspaceErrCode(t, err); code != "forbidden" {
		t.Errorf("outside parent link code = %q, want forbidden", code)
	}
}

// TestReadWorkspaceFile pins content, size, encoding, truncation, and the
// directory refusal.
func TestReadWorkspaceFile(t *testing.T) {
	folder, _ := workspaceFilesFixture(t)
	canonical, err := filepath.EvalSymlinks(folder)
	if err != nil {
		t.Fatalf("canonical folder: %v", err)
	}

	got, err := readWorkspaceFile(canonical, "top.txt", 1<<20)
	if err != nil {
		t.Fatalf("read top.txt: %v", err)
	}
	if got.Content != "top\n" || got.Encoding != "utf8" || got.Size != 4 || got.Truncated || got.Path != "top.txt" {
		t.Errorf("read = %+v, want the content with size 4", got)
	}

	echo, err := readWorkspaceFile(canonical, "./sub/../top.txt", 1<<20)
	if err != nil {
		t.Fatalf("read ./sub/../top.txt: %v", err)
	}
	if echo.Path != "top.txt" || echo.Content != "top\n" {
		t.Errorf("echo read = %+v, want cleaned path echo", echo)
	}

	bin := []byte{0xff, 0xfe, 0x00, 0x01}
	if err := os.WriteFile(filepath.Join(folder, "bin.dat"), bin, 0o644); err != nil {
		t.Fatalf("write bin: %v", err)
	}
	binGot, err := readWorkspaceFile(canonical, "bin.dat", 1<<20)
	if err != nil {
		t.Fatalf("read bin.dat: %v", err)
	}
	if binGot.Encoding != "base64" || binGot.Content == string(bin) {
		t.Errorf("binary = %+v, want base64", binGot)
	}

	cut, err := readWorkspaceFile(canonical, "top.txt", 2)
	if err != nil {
		t.Fatalf("capped read: %v", err)
	}
	if !cut.Truncated || cut.Content != "to" || cut.Size != 4 {
		t.Errorf("capped = %+v, want 'to' truncated with full size 4", cut)
	}

	viaLink, err := readWorkspaceFile(canonical, "inside", 1<<20)
	if err != nil {
		t.Fatalf("read inside link: %v", err)
	}
	if viaLink.Content != "top\n" || viaLink.LinkTarget != "top.txt" {
		t.Errorf("link read = %+v, want target content with link_target", viaLink)
	}

	if _, err := readWorkspaceFile(canonical, "sub", 1<<20); err == nil {
		t.Errorf("read dir = nil, want invalid-request")
	} else if code, _ := workspaceErrCode(t, err); code != "invalid-request" {
		t.Errorf("read-dir code = %q, want invalid-request", code)
	}
	if _, err := readWorkspaceFile(canonical, "missing.txt", 1<<20); err == nil {
		t.Errorf("read missing = nil, want not_found")
	} else if code, _ := workspaceErrCode(t, err); code != "not_found" {
		t.Errorf("read-missing code = %q, want not_found", code)
	}
}

// TestReadWorkspaceFileRefusesFifo pins that non-regular files refuse instead
// of blocking the handler on a fifo open.
func TestReadWorkspaceFileRefusesFifo(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("fifos need unix")
	}
	folder := t.TempDir()
	canonical, err := filepath.EvalSymlinks(folder)
	if err != nil {
		t.Fatalf("canonical folder: %v", err)
	}
	if err := syscall.Mkfifo(filepath.Join(folder, "pipe"), 0o644); err != nil {
		t.Fatalf("mkfifo: %v", err)
	}
	// A regression that opens before checking regularity blocks forever on
	// the fifo; fail fast instead of hanging the suite.
	done := make(chan error, 1)
	go func() {
		_, err := readWorkspaceFile(canonical, "pipe", 1<<20)
		done <- err
	}()
	select {
	case err := <-done:
		code, _ := workspaceErrCode(t, err)
		if code != "invalid-request" {
			t.Errorf("fifo code = %q, want invalid-request (err %v)", code, err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("readWorkspaceFile blocked on a fifo instead of refusing it")
	}
}

// TestListWorkspaceDir pins sorted entries with types, sizes, and link
// targets, plus the file and missing refusals.
func TestListWorkspaceDir(t *testing.T) {
	folder, _ := workspaceFilesFixture(t)
	canonical, err := filepath.EvalSymlinks(folder)
	if err != nil {
		t.Fatalf("canonical folder: %v", err)
	}

	l, err := listWorkspaceDir(canonical, ".")
	entries, truncated := l.Entries, l.Truncated
	if err != nil {
		t.Fatalf("list root: %v", err)
	}
	if truncated {
		t.Errorf("truncated = true for a small folder")
	}
	byName := map[string]workspaceDirEntry{}
	var names []string
	for _, e := range entries {
		byName[e.Name] = e
		names = append(names, e.Name)
	}
	for i := 1; i < len(names); i++ {
		if names[i-1] >= names[i] {
			t.Errorf("entries not sorted: %q", names)
			break
		}
	}
	if e := byName["top.txt"]; e.Type != "file" || e.Size != 4 {
		t.Errorf("top.txt = %+v, want file size 4", e)
	}
	if e := byName["sub"]; e.Type != "dir" || e.Size != 0 {
		t.Errorf("sub = %+v, want dir size 0", e)
	}
	if e := byName["inside"]; e.Type != "symlink" || e.LinkTarget != "top.txt" {
		t.Errorf("inside = %+v, want symlink to top.txt", e)
	}
	if e := byName["outside"]; e.Type != "symlink" || e.LinkTarget == "" {
		t.Errorf("outside = %+v, want symlink with its target listed (listing is not following)", e)
	}

	subl, err := listWorkspaceDir(canonical, "sub")
	sub := subl.Entries
	if err != nil {
		t.Fatalf("list sub: %v", err)
	}
	if len(sub) != 1 || sub[0].Name != "nested.txt" {
		t.Errorf("sub = %+v, want only nested.txt", sub)
	}

	if _, err := listWorkspaceDir(canonical, "top.txt"); err == nil {
		t.Errorf("list file = nil, want invalid-request")
	} else if code, _ := workspaceErrCode(t, err); code != "invalid-request" {
		t.Errorf("list-file code = %q, want invalid-request", code)
	}
	if _, err := listWorkspaceDir(canonical, "missing"); err == nil {
		t.Errorf("list missing = nil, want not_found")
	} else if code, _ := workspaceErrCode(t, err); code != "not_found" {
		t.Errorf("list-missing code = %q, want not_found", code)
	}
	if _, err := listWorkspaceDir(canonical, "../escape"); err == nil {
		t.Errorf("list escape = nil, want forbidden")
	} else if code, _ := workspaceErrCode(t, err); code != "forbidden" {
		t.Errorf("list-escape code = %q, want forbidden", code)
	}
}

// TestListWorkspaceDirCaps pins the entry cap: a huge directory lists the
// first maxWorkspaceListEntries names and flags truncated.
func TestListWorkspaceDirCaps(t *testing.T) {
	folder := t.TempDir()
	canonical, err := filepath.EvalSymlinks(folder)
	if err != nil {
		t.Fatalf("canonical folder: %v", err)
	}
	for i := 0; i < maxWorkspaceListEntries+5; i++ {
		name := filepath.Join(folder, fmt.Sprintf("f-%05d", i))
		if err := os.WriteFile(name, []byte("x"), 0o644); err != nil {
			t.Fatalf("write %d: %v", i, err)
		}
	}
	l, err := listWorkspaceDir(canonical, ".")
	entries, truncated := l.Entries, l.Truncated
	if err != nil {
		t.Fatalf("list huge: %v", err)
	}
	if len(entries) != maxWorkspaceListEntries || !truncated {
		t.Errorf("listed %d truncated %v, want %d truncated true", len(entries), truncated, maxWorkspaceListEntries)
	}
}

// TestWorkspaceFilesReadHandler pins the read shape over a seeded session and
// proves the files endpoints share the folder gate (remote host refuses).
func TestWorkspaceFilesReadHandler(t *testing.T) {
	folder, _ := workspaceFilesFixture(t)
	canonical, err := filepath.EvalSymlinks(folder)
	if err != nil {
		t.Fatalf("canonical folder: %v", err)
	}
	fs := newSessionFakeState(t)
	srv := New(fs)
	id := seedWorkspaceSession(t, fs, map[string]string{"gc.work_dir": canonical})

	out, err := srv.humaHandleWorkspaceFilesRead(context.Background(), &WorkspaceFilesReadInput{ID: id, Path: "sub/nested.txt"})
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	if out.Body.Content != "nested\n" || out.Body.Size != 7 || out.Body.Encoding != "utf8" || out.Body.Path != "sub/nested.txt" {
		t.Errorf("body = %+v, want the nested content", out.Body)
	}

	remoteID := seedWorkspaceSession(t, fs, map[string]string{"host": "remote-host.invalid", "gc.work_dir": canonical, "session_name": "s-ws-files-remote"})
	_, err = srv.humaHandleWorkspaceFilesRead(context.Background(), &WorkspaceFilesReadInput{ID: remoteID, Path: "top.txt"})
	code, _ := workspaceErrCode(t, err)
	if code != "unsupported_runtime" {
		t.Errorf("remote code = %q, want unsupported_runtime", code)
	}

	_, err = srv.humaHandleWorkspaceFilesRead(context.Background(), &WorkspaceFilesReadInput{ID: id})
	code, _ = workspaceErrCode(t, err)
	if code != "invalid-request" {
		t.Errorf("empty path code = %q, want invalid-request", code)
	}
}

// TestWorkspaceFilesListHandler pins the list shape and the root default.
func TestWorkspaceFilesListHandler(t *testing.T) {
	folder, _ := workspaceFilesFixture(t)
	canonical, err := filepath.EvalSymlinks(folder)
	if err != nil {
		t.Fatalf("canonical folder: %v", err)
	}
	fs := newSessionFakeState(t)
	srv := New(fs)
	id := seedWorkspaceSession(t, fs, map[string]string{"gc.work_dir": canonical})

	out, err := srv.humaHandleWorkspaceFilesList(context.Background(), &WorkspaceFilesListInput{ID: id})
	if err != nil {
		t.Fatalf("list root: %v", err)
	}
	if out.Body.Path != "." {
		t.Errorf("path = %q, want .", out.Body.Path)
	}
	names := map[string]bool{}
	for _, e := range out.Body.Entries {
		names[e.Name] = true
	}
	for _, want := range []string{"top.txt", "sub", "inside", "outside"} {
		if !names[want] {
			t.Errorf("entries miss %q: %+v", want, out.Body.Entries)
		}
	}
}

// TestWorkspaceFilesRoutesRegistered pins the HTTP wiring for both files
// routes.
func TestWorkspaceFilesRoutesRegistered(t *testing.T) {
	folder, _ := workspaceFilesFixture(t)
	canonical, err := filepath.EvalSymlinks(folder)
	if err != nil {
		t.Fatalf("canonical folder: %v", err)
	}
	fs := newSessionFakeState(t)
	srv := New(fs)
	id := seedWorkspaceSession(t, fs, map[string]string{"gc.work_dir": canonical})
	h := newTestCityHandlerWith(t, fs, srv)

	req := httptest.NewRequest("GET", cityURL(fs, "/session/"+id+"/workspace/files/read?path=top.txt"), nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("GET read = %d, want 200 (%s)", rec.Code, rec.Body.String())
	}
	var readBody struct {
		Content string `json:"content"`
	}
	if err := json.NewDecoder(rec.Body).Decode(&readBody); err != nil {
		t.Fatalf("decode read body: %v", err)
	}
	if readBody.Content != "top\n" {
		t.Errorf("content = %q, want top", readBody.Content)
	}

	req = httptest.NewRequest("GET", cityURL(fs, "/session/"+id+"/workspace/files/list?path=sub"), nil)
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("GET list = %d, want 200 (%s)", rec.Code, rec.Body.String())
	}

	req = httptest.NewRequest("GET", cityURL(fs, "/session/"+id+"/workspace/files/read"), nil)
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusUnprocessableEntity {
		t.Errorf("GET read without path = %d, want 422", rec.Code)
	}
}
