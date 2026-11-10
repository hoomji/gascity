package api

import (
	"encoding/base64"
	"errors"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"unicode/utf8"

	"github.com/gastownhall/gascity/internal/api/apierr"
)

// maxWorkspaceListEntries caps one directory listing; beyond it the response
// flags truncated. It is a backstop, not a tuning knob, so it takes no wire
// parameter.
const maxWorkspaceListEntries = 2000

// workspaceResolved is a folder-relative path resolved to an absolute inside
// path.
type workspaceResolved struct {
	// Abs is the absolute path with every symlink evaluated, proven inside
	// the folder.
	Abs string
	// Clean is the cleaned folder-relative slash path (the wire echo).
	Clean string
	// LinkTarget is the final component's symlink target, "" when the final
	// component is not a symlink.
	LinkTarget string
}

// maxWorkspaceLinkHops caps lexical symlink resolution, matching the
// kernel's own MAXSYMLINKS: deeper chains refuse as loops.
const maxWorkspaceLinkHops = 40

// resolveWorkspacePath maps a folder-relative rel to an absolute inside path.
// Lexical containment comes first (absolute, ..-escaping, empty, and NUL
// paths refuse), then links splice lexically and the spliced path must stay
// inside — escape detection precedes existence, so an outside link refuses
// forbidden whether or not its target exists, while an inside-missing name
// 404s. Only then does full evaluation prove existence. Loops refuse
// invalid-request; parent-directory links resolve like any other hop.
func resolveWorkspacePath(folder, rel string) (workspaceResolved, error) {
	clean, err := checkWorkspacePathFilter(rel)
	if err != nil {
		return workspaceResolved{}, err
	}
	r := workspaceResolved{Clean: clean}
	folderAbs, err := filepath.EvalSymlinks(folder)
	if err != nil {
		return workspaceResolved{}, err
	}
	joined := filepath.Join(folderAbs, filepath.FromSlash(clean))
	if fi, err := os.Lstat(joined); err == nil && fi.Mode()&os.ModeSymlink != 0 {
		target, err := os.Readlink(joined)
		if err != nil {
			return workspaceResolved{}, err
		}
		r.LinkTarget = target
	}
	lexical, err := lexicallyResolve(joined)
	if err != nil {
		if errors.Is(err, errSymlinkLoop) {
			return workspaceResolved{}, apierr.InvalidRequest.Msgf("symlink loop or chain too deep at %q", clean)
		}
		return workspaceResolved{}, err
	}
	if workspaceEscapes(folderAbs, lexical) {
		return workspaceResolved{}, apierr.WorkspaceForbidden.Msgf("path %q escapes the folder", clean)
	}
	abs, err := filepath.EvalSymlinks(joined)
	if err != nil {
		if os.IsNotExist(err) {
			return workspaceResolved{}, apierr.WorkspaceNotFound.Msgf("no such path: %q", clean)
		}
		if isLinkLoop(err) {
			return workspaceResolved{}, apierr.InvalidRequest.Msgf("symlink loop or chain too deep at %q", clean)
		}
		return workspaceResolved{}, err
	}
	if workspaceEscapes(folderAbs, abs) {
		return workspaceResolved{}, apierr.WorkspaceForbidden.Msgf("path %q escapes the folder", clean)
	}
	r.Abs = abs
	return r, nil
}

// errSymlinkLoop reports a lexical resolution that spliced past the hop cap.
var errSymlinkLoop = errors.New("symlink loop or chain too deep")

// lexicallyResolve splices every symlink on abs's path without requiring the
// target to exist: a missing component ends the walk with the path standing
// as joined. Only links the filesystem actually holds are followed, so the
// result is what evaluation would produce were every name present.
func lexicallyResolve(abs string) (string, error) {
	for range maxWorkspaceLinkHops + 1 {
		next, spliced, err := spliceFirstLink(abs)
		if err != nil {
			return "", err
		}
		if !spliced {
			return next, nil
		}
		abs = next
	}
	return "", errSymlinkLoop
}

// spliceFirstLink finds the topmost symlink component of abs and splices its
// target in, keeping the remaining components. False reports no link found
// (or a missing component, past which no link can resolve).
func spliceFirstLink(abs string) (string, bool, error) {
	vol := filepath.VolumeName(abs)
	rest := strings.TrimPrefix(abs, vol)
	parts := strings.Split(rest, string(os.PathSeparator))
	cur := vol + string(os.PathSeparator)
	for i := 1; i < len(parts); i++ {
		p := parts[i]
		if p == "" || p == "." {
			continue
		}
		if p == ".." {
			cur = filepath.Dir(cur)
			continue
		}
		cur = filepath.Join(cur, p)
		fi, err := os.Lstat(cur)
		if err != nil {
			if os.IsNotExist(err) {
				return abs, false, nil
			}
			return "", false, err
		}
		if fi.Mode()&os.ModeSymlink == 0 {
			continue
		}
		target, err := os.Readlink(cur)
		if err != nil {
			return "", false, err
		}
		var rebuilt string
		if filepath.IsAbs(target) {
			rebuilt = target
		} else {
			rebuilt = filepath.Join(filepath.Dir(cur), target)
		}
		for _, q := range parts[i+1:] {
			rebuilt = filepath.Join(rebuilt, q)
		}
		return filepath.Clean(rebuilt), true, nil
	}
	return abs, false, nil
}

// workspaceEscapes reports whether abs sits outside folderAbs.
func workspaceEscapes(folderAbs, abs string) bool {
	rel, err := filepath.Rel(folderAbs, abs)
	return err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(os.PathSeparator))
}

// isLinkLoop reports a symlink-loop resolution failure, whether the kernel
// raised ELOOP or Go's own resolver hit its hop cap ("EvalSymlinks: too many
// links", a plain error that wraps nothing).
func isLinkLoop(err error) bool {
	if errors.Is(err, syscall.ELOOP) {
		return true
	}
	return strings.Contains(strings.ToLower(err.Error()), "too many links")
}

// workspaceFileContent is one file's bounded content.
type workspaceFileContent struct {
	Path       string
	Size       int64
	Content    string
	Encoding   string
	Truncated  bool
	LinkTarget string
}

// readWorkspaceFile reads one contained file, capped at maxBytes. Directories
// and non-regular files (fifos, sockets, devices — whose opens would block
// or misbehave) refuse invalid-request before any open. Size always reports
// the full file size, past any truncation.
func readWorkspaceFile(folder, rel string, maxBytes int64) (*workspaceFileContent, error) {
	if maxBytes < 0 {
		maxBytes = 0
	}
	r, err := resolveWorkspacePath(folder, rel)
	if err != nil {
		return nil, err
	}
	fi, err := os.Stat(r.Abs)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, apierr.WorkspaceNotFound.Msgf("no such path: %q", r.Clean)
		}
		return nil, err
	}
	if fi.IsDir() {
		return nil, apierr.InvalidRequest.Msgf("path %q is a directory", r.Clean)
	}
	if !fi.Mode().IsRegular() {
		return nil, apierr.InvalidRequest.Msgf("path %q is not a regular file", r.Clean)
	}
	f, err := os.Open(r.Abs)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, apierr.WorkspaceNotFound.Msgf("no such path: %q", r.Clean)
		}
		return nil, err
	}
	defer f.Close() //nolint:errcheck // read-only handle; close errors carry no data loss
	buf := make([]byte, maxBytes+1)
	n, err := io.ReadFull(f, buf)
	if err != nil && !errors.Is(err, io.EOF) && !errors.Is(err, io.ErrUnexpectedEOF) {
		return nil, err
	}
	out := buf[:n]
	truncated := err == nil
	if int64(len(out)) > maxBytes {
		out = out[:maxBytes]
		truncated = true
	}
	content, encoding := workspaceEncode(out)
	return &workspaceFileContent{
		Path: r.Clean, Size: fi.Size(), Content: content, Encoding: encoding,
		Truncated: truncated, LinkTarget: r.LinkTarget,
	}, nil
}

// workspaceEncode renders bytes for the wire: raw text when valid UTF-8,
// base64 otherwise. It mirrors the hardened git wire's encoding so one
// supervisor consumer decodes both the same way.
func workspaceEncode(out []byte) (content, encoding string) {
	if utf8.Valid(out) {
		return string(out), "utf8"
	}
	return base64.StdEncoding.EncodeToString(out), "base64"
}

// workspaceDirEntry is one lstat view of a directory entry. Links are never
// followed here: listing an outside link reports it, since listing discloses
// nothing past its target string.
type workspaceDirEntry struct {
	Name       string
	Type       string
	Size       int64
	LinkTarget string
}

// workspaceDirListing is one bounded directory listing.
type workspaceDirListing struct {
	Path      string
	Entries   []workspaceDirEntry
	Truncated bool
}

// listWorkspaceDir lists one contained directory, entries sorted by name
// (os.ReadDir's order), cut at maxWorkspaceListEntries with truncated set.
// An entry that cannot be stated — a file deleted or relinked mid-list — is
// skipped with truncated set rather than failing the listing: the race is in
// the tree, not the request. That arm is race-only and has no deterministic
// test.
func listWorkspaceDir(folder, rel string) (*workspaceDirListing, error) {
	r, err := resolveWorkspacePath(folder, rel)
	if err != nil {
		return nil, err
	}
	fi, err := os.Stat(r.Abs)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, apierr.WorkspaceNotFound.Msgf("no such path: %q", r.Clean)
		}
		return nil, err
	}
	if !fi.IsDir() {
		return nil, apierr.InvalidRequest.Msgf("path %q is not a directory", r.Clean)
	}
	des, err := os.ReadDir(r.Abs)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, apierr.WorkspaceNotFound.Msgf("no such path: %q", r.Clean)
		}
		return nil, err
	}
	out := &workspaceDirListing{Path: r.Clean, Entries: []workspaceDirEntry{}}
	for _, de := range des {
		if len(out.Entries) >= maxWorkspaceListEntries {
			out.Truncated = true
			break
		}
		e, ok := workspaceListOne(r.Abs, de)
		if !ok {
			out.Truncated = true
			continue
		}
		out.Entries = append(out.Entries, e)
	}
	return out, nil
}

// workspaceListOne stats one directory entry without following links. False
// reports an entry that vanished or relinked mid-list.
func workspaceListOne(dir string, de fs.DirEntry) (workspaceDirEntry, bool) {
	e := workspaceDirEntry{Name: de.Name()}
	info, err := de.Info()
	if err != nil {
		return e, false
	}
	switch {
	case info.IsDir():
		e.Type = "dir"
	case info.Mode()&os.ModeSymlink != 0:
		e.Type = "symlink"
		e.Size = info.Size()
		target, err := os.Readlink(filepath.Join(dir, de.Name()))
		if err != nil {
			return e, false
		}
		e.LinkTarget = target
	case info.Mode().IsRegular():
		e.Type = "file"
		e.Size = info.Size()
	default:
		e.Type = "other"
		e.Size = info.Size()
	}
	return e, true
}
