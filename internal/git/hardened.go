package git

// Hardened read-only git execution (Hoppy T3-borrow spine TB-AD-12).
//
// Hardened is the only way the supervisor's workspace reads run git. Every
// call carries the lockdown: an allowlist-built environment, the -c
// overrides that neuter hooks, pagers, helpers, and transports, --no-ext-diff
// --no-textconv on diffs and logs, blobs via cat-file, and a config precheck
// that refuses local, worktree, or included config setting a listed key.
// Callers pass containment-checked paths; refs are validated by resolution
// (rev-parse --verify), which proves existence where check-ref-format only
// proves shape.

import (
	"bytes"
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"unicode/utf8"
)

var (
	// ErrUnsafeConfig refuses a call whose repository sets a forbidden key in
	// local, worktree, or included config — or whose config cannot be listed
	// at all, since safety then cannot be verified.
	ErrUnsafeConfig = errors.New("git: repository config is unsafe for reads")
	// ErrNoRepo reports a folder that holds no git repository.
	ErrNoRepo = errors.New("git: not a git repository")
	// ErrNoSuchRef reports a ref that resolves to no commit.
	ErrNoSuchRef = errors.New("git: ref resolves to no commit")
	// ErrBadRef reports a malformed ref (leading dash: flag injection).
	ErrBadRef = errors.New("git: malformed ref")
)

// hardenedStreamCap bounds any single git output stream the helper reads. It
// is a backstop behind the caller's per-file maxBytes: a huge diff is cut at
// the stream level and reported truncated rather than buffered unbounded.
const hardenedStreamCap = 8 << 20

// hardenedConfigArgs are the -c overrides every hardened call carries,
// verbatim from TB-AD-12. (Push alone adds protocol/https + credential
// overrides; this read-only helper has no push path.)
var hardenedConfigArgs = []string{
	"-c", "core.hooksPath=/dev/null",
	"-c", "core.fsmonitor=false",
	"-c", "commit.gpgSign=false",
	"-c", "tag.gpgSign=false",
	"-c", "diff.external=",
	"-c", "core.pager=cat",
	"-c", "core.sshCommand=/bin/false",
	"-c", "protocol.allow=never",
}

// ReadOnlyEnv builds the child environment for hardened reads from ambient:
// PATH and HOME pass through (the supervisor's own, which global git config
// resolution needs), LC_ALL is pinned for stable output parsing, the TB-AD-12
// locks are set, and everything else — including every GIT_* locating
// variable — is dropped. Pure over its input so tests pin it exactly;
// production passes os.Environ().
func ReadOnlyEnv(ambient []string) []string {
	out := make([]string, 0, len(ambient)+8)
	keep := map[string]bool{"PATH": true, "HOME": true}
	if runtime.GOOS == "windows" {
		keep["SYSTEMROOT"] = true
		keep["USERPROFILE"] = true
		keep["HOMEDRIVE"] = true
		keep["HOMEPATH"] = true
	}
	for _, e := range ambient {
		if k, _, ok := strings.Cut(e, "="); ok && keep[k] {
			out = append(out, e)
		}
	}
	out = append(out,
		"LC_ALL=C",
		"GIT_TERMINAL_PROMPT=0",
		"GIT_PAGER=cat",
		"GIT_EDITOR=true",
		"GIT_ASKPASS=/bin/false",
		"GIT_OPTIONAL_LOCKS=0",
	)
	return out
}

// Hardened runs read-only git commands in Dir with the TB-AD-12 lockdown.
// Paths passed in must already be containment-checked by the caller; they are
// passed to git after --. Refs are validated by resolution.
type Hardened struct {
	Dir string
}

// hardedResult is one bounded git call's stdout.
type hardenedResult struct {
	out       []byte
	truncated bool
}

// runHardened runs one git call with the lockdown: the config precheck, safe
// env, -c overrides, and a stdout bound of limit bytes (limit+1 is read to
// detect truncation; limit <= 0 reads uncapped and never reports truncated,
// for small fixed-shape outputs the caller bounds by construction).
func (h Hardened) runHardened(ctx context.Context, limit int64, args ...string) (hardenedResult, error) {
	if err := h.checkRepoConfig(ctx); err != nil {
		return hardenedResult{}, err
	}
	return h.runRaw(ctx, limit, args...)
}

// runRaw is runHardened without the precheck: the precheck itself runs here,
// and so does every call the precheck just cleared.
func (h Hardened) runRaw(ctx context.Context, limit int64, args ...string) (hardenedResult, error) {
	full := make([]string, 0, len(hardenedConfigArgs)+len(args))
	full = append(full, hardenedConfigArgs...)
	full = append(full, args...)
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	cmd := exec.CommandContext(ctx, "git", full...)
	cmd.Dir = h.Dir
	cmd.Env = ReadOnlyEnv(os.Environ())
	cmd.WaitDelay = gitWaitDelay
	stderr := &cappedBuffer{cap: 32 << 10}
	cmd.Stderr = stderr
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return hardenedResult{}, fmt.Errorf("git %s: %w", strings.Join(args, " "), err)
	}
	if err := cmd.Start(); err != nil {
		if errors.Is(err, exec.ErrNotFound) {
			return hardenedResult{}, fmt.Errorf("git executable not found: %w", err)
		}
		return hardenedResult{}, fmt.Errorf("git %s: %w", strings.Join(args, " "), err)
	}
	var out []byte
	truncated := false
	if limit > 0 {
		buf := make([]byte, limit+1)
		n, readErr := io.ReadFull(stdout, buf)
		out = buf[:n]
		if readErr == nil {
			truncated = true
			cancel()
		} else if !errors.Is(readErr, io.EOF) && !errors.Is(readErr, io.ErrUnexpectedEOF) {
			_ = cmd.Wait()
			return hardenedResult{}, fmt.Errorf("git %s: reading output: %w", strings.Join(args, " "), readErr)
		}
		_, _ = io.Copy(io.Discard, stdout)
	} else {
		var readErr error
		out, readErr = io.ReadAll(stdout)
		if readErr != nil {
			_ = cmd.Wait()
			return hardenedResult{}, fmt.Errorf("git %s: reading output: %w", strings.Join(args, " "), readErr)
		}
	}
	waitErr := cmd.Wait()
	if truncated {
		// We killed git after reading past the bound; its death rattle is
		// not a failure.
		return hardenedResult{out: out[:limit], truncated: true}, nil
	}
	if waitErr != nil {
		if ctxErr := ctx.Err(); ctxErr != nil && !errors.Is(waitErr, ctxErr) {
			return hardenedResult{}, fmt.Errorf("git %s: %w: %w", strings.Join(args, " "), waitErr, ctxErr)
		}
		combined := string(out) + "\n" + stderr.String()
		if strings.Contains(combined, "not a git repository") {
			return hardenedResult{}, fmt.Errorf("%w in %q: %s", ErrNoRepo, h.Dir, snippet(stderr.String()))
		}
		return hardenedResult{}, fmt.Errorf("git %s: %s: %w", strings.Join(args, " "), snippet(combined), waitErr)
	}
	return hardenedResult{out: out, truncated: truncated}, nil
}

// cappedBuffer is an io.Writer that keeps the first cap bytes.
type cappedBuffer struct {
	cap int
	buf bytes.Buffer
}

func (c *cappedBuffer) Write(p []byte) (int, error) {
	remaining := c.cap - c.buf.Len()
	if remaining > 0 {
		if len(p) > remaining {
			p = p[:remaining]
		}
		c.buf.Write(p)
	}
	return len(p), nil
}

func (c *cappedBuffer) String() string { return c.buf.String() }

// snippet keeps error output short and single-line.
func snippet(s string) string {
	s = strings.TrimSpace(s)
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		s = s[:i]
	}
	if len(s) > 300 {
		s = s[:300] + "…"
	}
	if s == "" {
		return "no output"
	}
	return s
}

// checkRepoConfig is TB-AD-12's precheck: it lists local, worktree, and
// included config with origins and scopes, and refuses when a listed key is
// set anywhere but system, global, or the command line (our own -c flags).
// A listing git cannot produce is itself a refusal: safety unverified.
// It runs with the safe env and -c overrides but no preceding precheck — it
// is the base case every other call's precheck bottoms out in.
func (h Hardened) checkRepoConfig(ctx context.Context) error {
	res, err := h.runRaw(ctx, 1<<20, "config", "--list", "--show-origin", "--show-scope")
	if err != nil {
		if errors.Is(err, exec.ErrNotFound) {
			return err
		}
		return fmt.Errorf("%w: cannot list repository config: %w", ErrUnsafeConfig, err)
	}
	if res.truncated {
		return fmt.Errorf("%w: config listing exceeds its bound, safety unverified", ErrUnsafeConfig)
	}
	for _, line := range strings.Split(string(res.out), "\n") {
		line = strings.TrimRight(line, "\r")
		if strings.TrimSpace(line) == "" {
			continue
		}
		fields := strings.Split(line, "\t")
		if len(fields) < 3 || !hardenedScopeNames[fields[0]] {
			return fmt.Errorf("%w: unparseable config line %q", ErrUnsafeConfig, snippet(line))
		}
		scope := fields[0]
		kv := strings.Join(fields[2:], "\t")
		if scope == "system" || scope == "global" || scope == "command" {
			continue
		}
		key, _, _ := strings.Cut(kv, "=")
		if hardenedUnsafeKey(strings.ToLower(strings.TrimSpace(key))) {
			return fmt.Errorf("%w: %s set in %s config", ErrUnsafeConfig, strings.TrimSpace(key), scope)
		}
	}
	return nil
}

// hardenedScopeNames are the config scopes git reports. Anything else in the
// scope column (a future scope) falls through to key checking: fail-closed.
var hardenedScopeNames = map[string]bool{
	"system":   true,
	"global":   true,
	"local":    true,
	"worktree": true,
	"command":  true,
}

// hardenedUnsafeKeys are the exact forbidden keys, lowercased.
var hardenedUnsafeKeys = map[string]bool{
	"core.fsmonitor":            true,
	"core.hookspath":            true,
	"core.sshcommand":           true,
	"core.pager":                true,
	"core.editor":               true,
	"core.askpass":              true,
	"core.alternaterefscommand": true,
	"sequence.editor":           true,
	"diff.external":             true,
}

// hardenedUnsafeKey reports whether a lowercased config key is on TB-AD-12's
// forbidden list: the exact keys above, plus *.textconv, diff.*.command,
// filter.*, merge.*.driver, gpg.*, credential.*, include.*, includeIf.*,
// core.alternateRefsCommand (exact), uploadpack.*, remote.*.uploadpack, and
// remote.*.receivepack.
func hardenedUnsafeKey(lower string) bool {
	if hardenedUnsafeKeys[lower] {
		return true
	}
	parts := strings.Split(lower, ".")
	if len(parts) < 2 {
		return false
	}
	section, last := parts[0], parts[len(parts)-1]
	switch section {
	case "credential", "filter", "gpg", "include", "includeif", "uploadpack":
		return true
	case "diff":
		return last == "command"
	case "merge":
		return last == "driver"
	case "remote":
		return last == "uploadpack" || last == "receivepack"
	}
	return last == "textconv"
}

// HardenedStatusEntry is one path's porcelain state: the index and worktree
// codes as single-character strings, e.g. "M", "?", "R".
type HardenedStatusEntry struct {
	Path     string
	Index    string
	Worktree string
	Staged   bool // Index codes anything but " " and "?"
}

// Status is a parsed porcelain status: the branch line plus one entry per
// changed path. Upstream names the tracking branch ("" when none); Ahead and
// Behind count the divergence. Truncated reports the stream hit
// hardenedStreamCap and the entry list is partial.
type Status struct {
	Branch    string
	Detached  bool
	Upstream  string
	Ahead     int
	Behind    int
	Entries   []HardenedStatusEntry
	Truncated bool
}

// Status reads the worktree state: branch line plus per-path codes. Untracked
// paths are included (?? entries, with untracked directories collapsed to a
// single "dir/" entry); ignored files are not.
func (h Hardened) Status(ctx context.Context) (Status, error) {
	return h.statusUntracked(ctx, "normal")
}

// statusUntracked is Status with the untracked-files mode chosen by the
// caller. DiffUncommitted reads "all" so untracked directories expand to
// their files (git still excludes ignored files, which a manual walk would
// have to re-derive); the status endpoint serves "normal"'s collapsed dirs.
func (h Hardened) statusUntracked(ctx context.Context, untracked string) (Status, error) {
	res, err := h.runHardened(ctx, hardenedStreamCap, "status", "--porcelain=v1", "-b", "-z", "--untracked-files="+untracked)
	if err != nil {
		return Status{}, err
	}
	return parsePorcelainZ(res.out, res.truncated)
}

// parsePorcelainZ parses status --porcelain -b -z output. Fields are
// NUL-separated and never C-quoted; renames carry the new path in the field
// after the scored entry.
func parsePorcelainZ(out []byte, truncated bool) (Status, error) {
	st := Status{Truncated: truncated}
	fields := bytes.Split(out, []byte{0})
	// The stream ends with a trailing NUL; drop the empty tail.
	if n := len(fields); n > 0 && len(fields[n-1]) == 0 {
		fields = fields[:n-1]
	}
	for i := 0; i < len(fields); i++ {
		f := string(fields[i])
		if strings.HasPrefix(f, "##") {
			st.Branch, st.Detached, st.Upstream, st.Ahead, st.Behind = parseBranchLine(f)
			continue
		}
		if len(f) < 4 || f[2] != ' ' {
			return Status{}, fmt.Errorf("git: unparseable status entry %q", snippet(f))
		}
		e := HardenedStatusEntry{Path: f[3:], Index: f[0:1], Worktree: f[1:2], Staged: f[0] != ' ' && f[0] != '?'}
		if e.Index == "R" || e.Index == "C" {
			i++
			if i >= len(fields) {
				return Status{}, fmt.Errorf("git: rename entry %q lacks its new path", snippet(f))
			}
			e.Path = string(fields[i])
		}
		st.Entries = append(st.Entries, e)
	}
	return st, nil
}

// parseBranchLine reads the ## header: "## branch...upstream [ahead N][,
// behind M]", "## branch", "## No commits yet on branch", or "## HEAD (no
// branch)" when detached. A "[gone]" upstream keeps its name with zero
// counts; unparseable counts read as zero rather than failing the status.
func parseBranchLine(f string) (branch string, detached bool, upstream string, ahead, behind int) {
	s := strings.TrimPrefix(f, "## ")
	if s == f {
		s = strings.TrimPrefix(f, "##")
	}
	switch {
	case strings.HasPrefix(s, "HEAD"):
		return "", true, "", 0, 0
	case strings.HasPrefix(s, "No commits yet on "):
		return strings.TrimPrefix(s, "No commits yet on "), false, "", 0, 0
	default:
		rest := s
		if i := strings.Index(rest, "..."); i >= 0 {
			branch, rest = rest[:i], rest[i+3:]
		} else if i := strings.IndexByte(rest, ' '); i >= 0 {
			branch = rest[:i]
			return branch, false, "", 0, 0
		} else {
			return rest, false, "", 0, 0
		}
		upstream = rest
		if i := strings.IndexByte(rest, ' '); i >= 0 {
			upstream, rest = rest[:i], rest[i+1:]
		} else {
			return branch, false, upstream, 0, 0
		}
		for _, part := range strings.Split(strings.Trim(rest, "[]"), ",") {
			part = strings.TrimSpace(part)
			if n, ok := strings.CutPrefix(part, "ahead "); ok {
				ahead = atoiOrZero(n)
			} else if n, ok := strings.CutPrefix(part, "behind "); ok {
				behind = atoiOrZero(n)
			}
		}
		return branch, false, upstream, ahead, behind
	}
}

// atoiOrZero parses a porcelain count; garbage reads as zero.
func atoiOrZero(s string) int {
	n, err := strconv.Atoi(strings.TrimSpace(s))
	if err != nil || n < 0 {
		return 0
	}
	return n
}

// BranchHead is the current branch and commit.
type BranchHead struct {
	Branch   string
	Detached bool
	SHA      string // "" when the repo has no commits yet
}

// Branch resolves the current branch and HEAD sha. The sha is empty — not an
// error — on a repo with no commits yet.
func (h Hardened) Branch(ctx context.Context) (BranchHead, error) {
	var bh BranchHead
	res, err := h.runHardened(ctx, 1<<20, "branch", "--show-current")
	if err != nil {
		return BranchHead{}, err
	}
	bh.Branch = strings.TrimSpace(string(res.out))
	bh.Detached = bh.Branch == ""
	sha, err := h.runHardened(ctx, 1<<20, "rev-parse", "HEAD")
	if err != nil {
		if errors.Is(err, ErrNoRepo) {
			return BranchHead{}, err
		}
		if isUnknownRevision(err) {
			return bh, nil
		}
		return BranchHead{}, err
	}
	bh.SHA = strings.TrimSpace(string(sha.out))
	return bh, nil
}

// isUnknownRevision reports the rev-parse failure that means "no such
// revision" (a repo with no commits) rather than a broken repo. LC_ALL=C pins
// these messages.
func isUnknownRevision(err error) bool {
	msg := err.Error()
	return strings.Contains(msg, "unknown revision") ||
		strings.Contains(msg, "ambiguous argument") ||
		strings.Contains(msg, "bad revision")
}

// resolveCommit validates ref by resolving it to a commit sha: rev-parse
// --verify proves existence where check-ref-format only proves shape. Refs
// with a leading dash are rejected before git sees them (flag injection).
// HEAD on a repo with no commits resolves to "" with a nil error.
func (h Hardened) resolveCommit(ctx context.Context, ref string) (string, error) {
	if strings.HasPrefix(ref, "-") || strings.TrimSpace(ref) == "" {
		return "", fmt.Errorf("%w: %q", ErrBadRef, ref)
	}
	res, err := h.runHardened(ctx, 1<<20, "rev-parse", "--verify", ref+"^{commit}")
	if err != nil {
		if errors.Is(err, ErrNoRepo) {
			return "", err
		}
		if isUnknownRevision(err) && ref == "HEAD" {
			return "", nil
		}
		return "", fmt.Errorf("%w: %q: %w", ErrNoSuchRef, ref, err)
	}
	return strings.TrimSpace(string(res.out)), nil
}

// emptyTree is the sha of the empty tree, the diff parent for root commits.
const emptyTree = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

// isZeroSha matches a raw-diff sha field that means "no blob on this side".
func isZeroSha(sha string) bool {
	if len(sha) == 0 {
		return true
	}
	for i := 0; i < len(sha); i++ {
		if sha[i] != '0' {
			return false
		}
	}
	return true
}

// HardenedBlob is one side of one file's content: either UTF-8 text or
// base64, cut at the caller's maxBytes with Truncated set.
type HardenedBlob struct {
	Content   string
	Encoding  string // "utf8" or "base64"
	Truncated bool
}

// HardenedDiffFile is one path's before/after pair plus its unified patch
// section, or a worktree-only entry (untracked) with no patch.
type HardenedDiffFile struct {
	Path      string
	Status    string // added, modified, deleted, renamed, untracked
	Before    *HardenedBlob
	After     *HardenedBlob
	Patch     string
	Truncated bool // patch or either blob hit maxBytes, or the stream cut
}

// HardenedDiff is a parsed multi-file diff. Mode names the shape
// ("uncommitted", "base", "commit"); the SHA fields carry the resolved
// endpoints the mode diffed (HeadSHA always, BaseSHA and MergeBaseSHA for
// base, CommitSHA for commit). Truncated reports the stream hit
// hardenedStreamCap and the file list is partial.
type HardenedDiff struct {
	Mode         string
	HeadSHA      string
	BaseSHA      string
	MergeBaseSHA string
	CommitSHA    string
	Files        []HardenedDiffFile
	Truncated    bool
}

// DiffUncommitted diffs the worktree and index against HEAD, plus untracked
// files from status. On a repo with no commits every file is before-less and
// patches are empty (there is no base to diff against); untracked files
// always carry after-content and no patch. paths filters (already
// containment-checked); maxBytes caps each file's patch and each blob.
func (h Hardened) DiffUncommitted(ctx context.Context, paths []string, maxBytes int64) (HardenedDiff, error) {
	head, err := h.resolveCommit(ctx, "HEAD")
	if err != nil {
		return HardenedDiff{}, err
	}
	st, err := h.statusUntracked(ctx, "all")
	if err != nil {
		return HardenedDiff{}, err
	}
	d := HardenedDiff{Mode: "uncommitted", HeadSHA: head}
	if head == "" {
		d.Files = h.headlessDiff(st, paths, maxBytes)
	} else {
		rawArgs := append([]string{"diff", "HEAD", "--raw", "--no-renames", "-z", "--"}, paths...)
		raw, err := h.runHardened(ctx, hardenedStreamCap, rawArgs...)
		if err != nil {
			return HardenedDiff{}, err
		}
		patchArgs := append([]string{"diff", "HEAD", "--patch", "--no-ext-diff", "--no-textconv", "--no-renames", "--"}, paths...)
		patch, err := h.runHardened(ctx, hardenedStreamCap, patchArgs...)
		if err != nil {
			return HardenedDiff{}, err
		}
		sections := parsePatchSections(string(patch.out))
		files, err := h.assembleWorktreeDiff(ctx, raw.out, sections, maxBytes)
		if err != nil {
			return HardenedDiff{}, err
		}
		d.Files = files
		d.Truncated = raw.truncated || patch.truncated
	}
	untracked, err := h.untrackedDiffs(st, paths, maxBytes)
	if err != nil {
		return HardenedDiff{}, err
	}
	d.Files = append(d.Files, untracked...)
	d.Truncated = d.Truncated || st.Truncated
	return d, nil
}

// DiffSinceBase diffs base's merge-base with HEAD against HEAD (pure git: both
// sides via cat-file, no worktree reads). With no common ancestor it falls
// back to two-dot base..HEAD; MergeBaseSHA tells which base the diff actually
// used.
func (h Hardened) DiffSinceBase(ctx context.Context, base string, paths []string, maxBytes int64) (HardenedDiff, error) {
	baseSHA, err := h.resolveCommit(ctx, base)
	if err != nil || baseSHA == "" {
		if err == nil {
			err = fmt.Errorf("%w: %q", ErrNoSuchRef, base)
		}
		return HardenedDiff{}, err
	}
	headSHA, err := h.resolveCommit(ctx, "HEAD")
	if err != nil || headSHA == "" {
		if err == nil {
			err = fmt.Errorf("%w: HEAD has no commits", ErrNoSuchRef)
		}
		return HardenedDiff{}, err
	}
	mergeBase := baseSHA
	if mb, mbErr := h.runHardened(ctx, 1<<20, "merge-base", baseSHA, headSHA); mbErr == nil {
		mergeBase = strings.TrimSpace(string(mb.out))
	}
	rawArgs := append([]string{"diff", mergeBase, headSHA, "--raw", "--no-renames", "-z", "--"}, paths...)
	raw, err := h.runHardened(ctx, hardenedStreamCap, rawArgs...)
	if err != nil {
		return HardenedDiff{}, err
	}
	patchArgs := append([]string{"diff", mergeBase, headSHA, "--patch", "--no-ext-diff", "--no-textconv", "--no-renames", "--"}, paths...)
	patch, err := h.runHardened(ctx, hardenedStreamCap, patchArgs...)
	if err != nil {
		return HardenedDiff{}, err
	}
	sections := parsePatchSections(string(patch.out))
	files, err := h.assembleCommitDiff(ctx, raw.out, sections, maxBytes)
	if err != nil {
		return HardenedDiff{}, err
	}
	return HardenedDiff{
		Mode: "base", HeadSHA: headSHA, BaseSHA: baseSHA, MergeBaseSHA: mergeBase,
		Files: files, Truncated: raw.truncated || patch.truncated,
	}, nil
}

// DiffCommit diffs one commit against its first parent (the empty tree for a
// root commit). Both sides come from cat-file; the worktree is never read.
func (h Hardened) DiffCommit(ctx context.Context, commit string, paths []string, maxBytes int64) (HardenedDiff, error) {
	sha, err := h.resolveCommit(ctx, commit)
	if err != nil || sha == "" {
		if err == nil {
			err = fmt.Errorf("%w: %q", ErrNoSuchRef, commit)
		}
		return HardenedDiff{}, err
	}
	parent := emptyTree
	if res, perr := h.runHardened(ctx, 1<<20, "rev-parse", sha+"^1"); perr == nil {
		parent = strings.TrimSpace(string(res.out))
	}
	rawArgs := append([]string{"diff", parent, sha, "--raw", "--no-renames", "-z", "--"}, paths...)
	raw, err := h.runHardened(ctx, hardenedStreamCap, rawArgs...)
	if err != nil {
		return HardenedDiff{}, err
	}
	patchArgs := append([]string{"diff", parent, sha, "--patch", "--no-ext-diff", "--no-textconv", "--no-renames", "--"}, paths...)
	patch, err := h.runHardened(ctx, hardenedStreamCap, patchArgs...)
	if err != nil {
		return HardenedDiff{}, err
	}
	sections := parsePatchSections(string(patch.out))
	files, err := h.assembleCommitDiff(ctx, raw.out, sections, maxBytes)
	if err != nil {
		return HardenedDiff{}, err
	}
	return HardenedDiff{
		Mode: "commit", CommitSHA: sha, Files: files,
		Truncated: raw.truncated || patch.truncated,
	}, nil
}

// rawEntry is one parsed --raw line: statuses plus blob shas per side.
type rawEntry struct {
	status  byte // A, C, D, M, R, T, U, X
	oldPath string
	newPath string
	oldSha  string
	newSha  string
}

// parseRawZ parses diff --raw -z output. With -z the score line and each path
// are separate NUL-terminated fields: :<oldmode> <newmode> <oldsha> <newsha>
// <status>[score], then the path, then (renames and copies only) the new path.
func parseRawZ(out []byte) ([]rawEntry, error) {
	var entries []rawEntry
	fields := bytes.Split(out, []byte{0})
	if n := len(fields); n > 0 && len(fields[n-1]) == 0 {
		fields = fields[:n-1]
	}
	for i := 0; i < len(fields); i++ {
		f := string(fields[i])
		if f == "" {
			continue
		}
		meta := strings.Fields(strings.TrimPrefix(f, ":"))
		if !strings.HasPrefix(f, ":") || len(meta) != 5 || meta[4] == "" {
			return nil, fmt.Errorf("git: unparseable raw diff entry %q", snippet(f))
		}
		i++
		if i >= len(fields) {
			return nil, fmt.Errorf("git: raw diff entry %q lacks its path", snippet(f))
		}
		e := rawEntry{status: meta[4][0], oldPath: string(fields[i]), newPath: string(fields[i]), oldSha: meta[2], newSha: meta[3]}
		if e.status == 'R' || e.status == 'C' {
			i++
			if i >= len(fields) {
				return nil, fmt.Errorf("git: rename entry %q lacks its new path", snippet(f))
			}
			e.newPath = string(fields[i])
		}
		entries = append(entries, e)
	}
	return entries, nil
}

// parsePatchSections splits a unified diff into per-file sections keyed by
// the b/ path, unquoting C-quoted headers. Binary files yield a section whose
// body is the "Binary files ... differ" note.
func parsePatchSections(patch string) map[string]string {
	sections := map[string]string{}
	var cur *strings.Builder
	var curPath string
	flush := func() {
		if cur != nil {
			sections[curPath] = cur.String()
		}
	}
	for _, line := range strings.SplitAfter(patch, "\n") {
		if rest, ok := strings.CutPrefix(line, "diff --git "); ok {
			flush()
			curPath = patchSectionPath(strings.TrimSpace(rest))
			cur = &strings.Builder{}
		}
		if cur != nil {
			cur.WriteString(line)
		}
	}
	flush()
	return sections
}

// patchSectionPath extracts the b/ path from a "diff --git a/X b/Y" header,
// unquoting C-quoted pairs. All hardened diffs run with --no-renames, so the
// a/ and b/ sides always match (or one side is /dev/null); a path holding " "
// stays unambiguous because the split that equalizes both sides wins.
func patchSectionPath(header string) string {
	header = strings.TrimSuffix(header, "\n")
	if rest, ok := strings.CutPrefix(header, `"a/`); ok {
		// Quoted pair: C-quoting escapes inner quotes, so the last "b/
		// opens the b/ side.
		if i := strings.LastIndex(rest, `"b/`); i >= 0 {
			if un, err := strconv.Unquote(rest[i:]); err == nil {
				return strings.TrimPrefix(un, "b/")
			}
			return strings.TrimSuffix(strings.TrimPrefix(rest[i+1:], "b/"), `"`)
		}
		return header
	}
	rest, ok := strings.CutPrefix(header, "a/")
	if !ok {
		return header
	}
	// Try each " b/" occurrence; the separator is the one that equalizes
	// both sides (or lands a side on /dev/null).
	search, offset := rest, 0
	for {
		sep := strings.Index(search, " b/")
		if sep < 0 {
			break
		}
		a, b := rest[:offset+sep], rest[offset+sep+3:]
		switch {
		case a == "/dev/null":
			return b
		case b == "/dev/null":
			return a
		case a == b:
			return b
		}
		offset += sep + 1
		search = rest[offset:]
	}
	if _, b, ok := strings.Cut(rest, " b/"); ok {
		return b
	}
	return header
}

// rawStatusName maps raw status codes to the wire vocabulary.
func rawStatusName(code byte) string {
	switch code {
	case 'A':
		return "added"
	case 'M', 'T':
		return "modified"
	case 'D':
		return "deleted"
	case 'R', 'C':
		return "renamed"
	default:
		return "modified"
	}
}

// assembleWorktreeDiff joins raw entries with patch sections for the
// worktree-against-HEAD diff: before comes from the old blob, after from the
// worktree file (what the supervisor actually sees).
func (h Hardened) assembleWorktreeDiff(ctx context.Context, raw []byte, sections map[string]string, maxBytes int64) ([]HardenedDiffFile, error) {
	entries, err := parseRawZ(raw)
	if err != nil {
		return nil, err
	}
	files := make([]HardenedDiffFile, 0, len(entries))
	for _, e := range entries {
		df := HardenedDiffFile{Path: e.newPath, Status: rawStatusName(e.status)}
		if !isZeroSha(e.oldSha) {
			blob, err := h.catBlob(ctx, e.oldSha, maxBytes)
			if err != nil {
				return nil, err
			}
			df.Before = blob
		}
		if e.status != 'D' {
			blob, truncated, err := h.readWorktreeFile(e.newPath, maxBytes)
			if err != nil {
				return nil, err
			}
			df.After = blob
			df.Truncated = truncated
		}
		df.Patch, df.Truncated = capPatch(sections[e.newPath], maxBytes, df.Truncated)
		if df.Before != nil && df.Before.Truncated {
			df.Truncated = true
		}
		if df.After != nil && df.After.Truncated {
			df.Truncated = true
		}
		files = append(files, df)
	}
	return files, nil
}

// assembleCommitDiff joins raw entries with patch sections for commit-range
// diffs: both sides come from blobs, never the worktree.
func (h Hardened) assembleCommitDiff(ctx context.Context, raw []byte, sections map[string]string, maxBytes int64) ([]HardenedDiffFile, error) {
	entries, err := parseRawZ(raw)
	if err != nil {
		return nil, err
	}
	files := make([]HardenedDiffFile, 0, len(entries))
	for _, e := range entries {
		df := HardenedDiffFile{Path: e.newPath, Status: rawStatusName(e.status)}
		if !isZeroSha(e.oldSha) {
			blob, err := h.catBlob(ctx, e.oldSha, maxBytes)
			if err != nil {
				return nil, err
			}
			df.Before = blob
		}
		if !isZeroSha(e.newSha) {
			blob, err := h.catBlob(ctx, e.newSha, maxBytes)
			if err != nil {
				return nil, err
			}
			df.After = blob
		}
		df.Patch, df.Truncated = capPatch(sections[e.newPath], maxBytes, false)
		if df.Before != nil && df.Before.Truncated {
			df.Truncated = true
		}
		if df.After != nil && df.After.Truncated {
			df.Truncated = true
		}
		files = append(files, df)
	}
	return files, nil
}

// headlessDiff derives the uncommitted diff from status alone: no HEAD exists,
// so every entry is before-less and patches are empty.
func (h Hardened) headlessDiff(st Status, paths []string, maxBytes int64) []HardenedDiffFile {
	var files []HardenedDiffFile
	for _, e := range st.Entries {
		if e.Index == "?" && e.Worktree == "?" {
			continue // untrackedDiffs covers these
		}
		if !pathMatched(e.Path, paths) {
			continue
		}
		df := HardenedDiffFile{Path: e.Path, Status: "added"}
		if blob, truncated, err := h.readWorktreeFile(e.Path, maxBytes); err == nil {
			df.After = blob
			df.Truncated = truncated
		}
		files = append(files, df)
	}
	return files
}

// untrackedDiffs turns status ?? entries into worktree-only diff files.
func (h Hardened) untrackedDiffs(st Status, paths []string, maxBytes int64) ([]HardenedDiffFile, error) {
	var files []HardenedDiffFile
	for _, e := range st.Entries {
		if e.Index != "?" || e.Worktree != "?" {
			continue
		}
		if !pathMatched(e.Path, paths) {
			continue
		}
		df := HardenedDiffFile{Path: e.Path, Status: "untracked"}
		blob, truncated, err := h.readWorktreeFile(e.Path, maxBytes)
		if err != nil {
			if os.IsNotExist(err) {
				continue
			}
			return nil, err
		}
		df.After = blob
		df.Truncated = truncated
		files = append(files, df)
	}
	return files, nil
}

// pathMatched reports whether rel matches one of the caller's path filters
// (exact or under a filtered directory). Empty filters match everything.
func pathMatched(rel string, paths []string) bool {
	if len(paths) == 0 {
		return true
	}
	for _, p := range paths {
		if rel == p || strings.HasPrefix(rel, p+"/") {
			return true
		}
	}
	return false
}

// catBlob reads one blob through cat-file (never smudge filters: the -c
// overrides plus --no-textconv-equivalent direct blob read keep it raw),
// capped at maxBytes and encoded UTF-8-or-base64.
func (h Hardened) catBlob(ctx context.Context, sha string, maxBytes int64) (*HardenedBlob, error) {
	res, err := h.runHardened(ctx, maxBytes+1, "cat-file", "blob", sha)
	if err != nil {
		return nil, err
	}
	out := res.out
	truncated := res.truncated
	if int64(len(out)) > maxBytes {
		out = out[:maxBytes]
		truncated = true
	}
	return encodeBlob(out, truncated), nil
}

// readWorktreeFile reads one contained worktree file, capped at maxBytes.
// The caller containment-checks rel, but a symlink inside the folder (or a
// symlinked parent directory) could still point outside it — so symlinks are
// never followed. A symlinked file reports its target string as content
// (exactly what git stores for it); anything else resolves through
// EvalSymlinks and is re-checked for containment before the read.
func (h Hardened) readWorktreeFile(rel string, maxBytes int64) (*HardenedBlob, bool, error) {
	joined := h.Dir + string(os.PathSeparator) + rel
	if fi, err := os.Lstat(joined); err == nil && fi.Mode()&os.ModeSymlink != 0 {
		target, err := os.Readlink(joined)
		if err != nil {
			return nil, false, fmt.Errorf("reading worktree link %q: %w", rel, err)
		}
		return encodeBlob([]byte(target), false), false, nil
	}
	resolved, err := filepath.EvalSymlinks(joined)
	if err != nil {
		return nil, false, err
	}
	root, err := filepath.EvalSymlinks(h.Dir)
	if err != nil {
		return nil, false, fmt.Errorf("resolving folder %q: %w", h.Dir, err)
	}
	if contained, err := pathContained(root, resolved); err != nil || !contained {
		return nil, false, fmt.Errorf("worktree file %q escapes its folder", rel)
	}
	f, err := os.Open(resolved)
	if err != nil {
		return nil, false, err
	}
	defer f.Close() //nolint:errcheck // read-only handle; close errors carry no data loss
	buf := make([]byte, maxBytes+1)
	n, err := io.ReadFull(f, buf)
	if err != nil && !errors.Is(err, io.EOF) && !errors.Is(err, io.ErrUnexpectedEOF) {
		return nil, false, fmt.Errorf("reading worktree file %q: %w", rel, err)
	}
	truncated := err == nil
	out := buf[:n]
	if int64(len(out)) > maxBytes {
		out = out[:maxBytes]
		truncated = true
	}
	return encodeBlob(out, truncated), truncated, nil
}

// encodeBlob wraps bytes in the wire encoding: raw UTF-8 when valid, base64
// otherwise (binary files, non-UTF-8 text).
func encodeBlob(out []byte, truncated bool) *HardenedBlob {
	if utf8.Valid(out) {
		return &HardenedBlob{Content: string(out), Encoding: "utf8", Truncated: truncated}
	}
	return &HardenedBlob{Content: base64.StdEncoding.EncodeToString(out), Encoding: "base64", Truncated: truncated}
}

// capPatch cuts a patch section at maxBytes.
func capPatch(section string, maxBytes int64, truncated bool) (string, bool) {
	if int64(len(section)) > maxBytes {
		return section[:maxBytes], true
	}
	return section, truncated
}

// pathContained reports whether target (absolute, symlink-resolved) sits
// inside root (absolute, symlink-resolved).
func pathContained(root, target string) (bool, error) {
	rel, err := filepath.Rel(root, target)
	if err != nil {
		return false, err
	}
	if rel == ".." || strings.HasPrefix(rel, ".."+string(os.PathSeparator)) {
		return false, nil
	}
	return true, nil
}
