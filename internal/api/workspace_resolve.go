package api

import (
	"errors"
	"os"
	"path"
	"path/filepath"
	"strings"

	"github.com/gastownhall/gascity/internal/api/apierr"
	"github.com/gastownhall/gascity/internal/beads"
	"github.com/gastownhall/gascity/internal/git"
	"github.com/gastownhall/gascity/internal/session"
)

// resolveWorkspaceFolder maps a session id to the absolute, symlink-resolved
// folder the supervisor's workspace reads serve from, enforcing the gate
// table (TB-AD-16/17):
//
//   - unknown session → 404 (the session ladder's own error)
//   - session on another host → 409 unsupported_runtime (absent host reads
//     as local: v1 cities are single-host)
//   - no folder recorded, relative folder, missing folder, or a folder that
//     is not a directory → 409 unsupported_runtime
//   - a forbidden folder (the filesystem root, $HOME, or an ancestor of
//     $HOME) → 403 forbidden
//
// The folder comes from session.WorkspaceDirFromInfo (gc.work_dir first),
// resolved through EvalSymlinks so a folder that IS a symlink is judged by
// its target, and every read downstream uses the resolved path.
func (s *Server) resolveWorkspaceFolder(store beads.SessionStore, id string) (string, error) {
	resolved, err := s.resolveSessionIDAllowClosedWithConfig(store, id)
	if err != nil {
		return "", humaResolveError(err)
	}
	info, err := s.sessionManager(store).Get(resolved)
	if err != nil {
		return "", humaSessionManagerError(err)
	}
	if host := strings.TrimSpace(info.Host); host != "" && !strings.EqualFold(host, session.LocalSessionHost()) {
		return "", apierr.UnsupportedRuntime.Msgf("session %s runs on host %q, which this supervisor (host %q) cannot read", resolved, host, session.LocalSessionHost())
	}
	dir := session.WorkspaceDirFromInfo(info)
	if dir == "" {
		return "", apierr.UnsupportedRuntime.Msgf("session %s records no folder (gc.work_dir is absent)", resolved)
	}
	if !filepath.IsAbs(dir) {
		return "", apierr.UnsupportedRuntime.Msgf("session %s records a relative folder %q", resolved, dir)
	}
	folder, err := filepath.EvalSymlinks(dir)
	if err != nil {
		return "", apierr.UnsupportedRuntime.Msgf("session %s folder %q is missing or unreadable: %v", resolved, dir, err)
	}
	if why := workspaceFolderForbiddenReason(folder); why != "" {
		return "", apierr.WorkspaceForbidden.Msgf("session %s folder %q is forbidden: %s", resolved, folder, why)
	}
	fi, err := os.Stat(folder)
	if err != nil {
		return "", apierr.UnsupportedRuntime.Msgf("session %s folder %q is missing or unreadable: %v", resolved, folder, err)
	}
	if !fi.IsDir() {
		return "", apierr.UnsupportedRuntime.Msgf("session %s folder %q is not a directory", resolved, folder)
	}
	return folder, nil
}

// workspaceFolderForbiddenReason reports why a resolved session folder is
// forbidden: the filesystem root, the home directory, or an ancestor of the
// home directory. Empty means allowed. A worktree is always a narrow folder;
// a folder at or above $HOME is certainly a mis-recorded row, not a
// worktree. Anything else — including folders outside $HOME such as /tmp
// cities — is judged by containment downstream, not here.
//
// Home and its ancestors compare by file identity (os.SameFile), not by
// string: a case-alias spelling or a bind mount of home is still home. When
// the home directory cannot be determined only the root is refused: the rule
// is a sanity floor, not the security boundary (containment is), and
// refusing every folder over an unknown $HOME would outage supervisors that
// run without one.
func workspaceFolderForbiddenReason(folder string) string {
	clean := filepath.Clean(folder)
	if isFilesystemRoot(clean) {
		return "it is the filesystem root"
	}
	home, err := os.UserHomeDir()
	if err != nil || strings.TrimSpace(home) == "" {
		return ""
	}
	homeResolved, err := filepath.EvalSymlinks(home)
	if err != nil {
		homeResolved = filepath.Clean(home)
	}
	fi, err := os.Stat(clean)
	if err != nil {
		return "" // Missing or unreadable: the caller refuses it as unsupported_runtime.
	}
	for d := homeResolved; ; {
		if hi, err := os.Stat(d); err == nil && os.SameFile(fi, hi) {
			switch {
			case d == homeResolved:
				return "it is the home directory"
			case isFilesystemRoot(d):
				return "it is the filesystem root"
			default:
				return "it is an ancestor of the home directory"
			}
		}
		if parent := filepath.Dir(d); parent != d {
			d = parent
		} else {
			return ""
		}
	}
}

// isFilesystemRoot reports whether clean is a volume root ("/", "C:\").
func isFilesystemRoot(clean string) bool {
	return clean == string(filepath.Separator) || clean == filepath.VolumeName(clean)+string(filepath.Separator)
}

// checkWorkspacePathFilter validates one repo-relative path filter for the
// workspace diff: absolute paths, .. escapes, and NUL bytes refuse. The
// check runs under both slash and OS path semantics so a backslash escape
// refuses on Windows too; the returned filter is the cleaned slash form git
// receives.
func checkWorkspacePathFilter(p string) (string, error) {
	if p == "" {
		return "", apierr.InvalidRequest.Msg("empty path filter")
	}
	if strings.ContainsRune(p, 0) {
		return "", apierr.InvalidRequest.Msg("path filter holds a NUL byte")
	}
	slash := path.Clean(filepath.ToSlash(p))
	if path.IsAbs(slash) || filepath.IsAbs(p) {
		return "", apierr.WorkspaceForbidden.Msgf("path filter %q is absolute", p)
	}
	if slash == ".." || strings.HasPrefix(slash, "../") {
		return "", apierr.WorkspaceForbidden.Msgf("path filter %q escapes the folder", p)
	}
	if osCleaned := filepath.Clean(p); osCleaned == ".." || strings.HasPrefix(osCleaned, ".."+string(filepath.Separator)) {
		return "", apierr.WorkspaceForbidden.Msgf("path filter %q escapes the folder", p)
	}
	return slash, nil
}

// workspaceFileState maps porcelain index/worktree codes to the status wire
// word. The index side leads when it codes a change; untracked is the ?? pair.
func workspaceFileState(index, worktree string) string {
	code := index
	if code == " " || code == "?" {
		code = worktree
	}
	switch code {
	case "A":
		return "added"
	case "D":
		return "deleted"
	case "R", "C":
		return "renamed"
	case "U":
		return "unmerged"
	case "?":
		return "untracked"
	case "T":
		return "modified"
	default:
		return "modified"
	}
}

// workspaceGitError maps the hardened helper's sentinel errors onto the
// catalog: unsafe config → 409, missing repo or ref → 404, malformed ref →
// 400. Anything else passes through as a 500.
func workspaceGitError(err error) error {
	switch {
	case errors.Is(err, git.ErrUnsafeConfig):
		return apierr.UnsafeRepoConfig.Msgf("refusing to run git: %v", err)
	case errors.Is(err, git.ErrNoRepo):
		return apierr.WorkspaceNotFound.Msg("the session folder holds no git repository")
	case errors.Is(err, git.ErrNoSuchRef):
		return apierr.WorkspaceNotFound.Msgf("no such ref: %v", err)
	case errors.Is(err, git.ErrBadRef):
		return apierr.InvalidRequest.Msgf("malformed ref: %v", err)
	default:
		return err
	}
}
