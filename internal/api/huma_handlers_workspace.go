package api

import (
	"context"

	"github.com/gastownhall/gascity/internal/api/apierr"
	"github.com/gastownhall/gascity/internal/beads"
	"github.com/gastownhall/gascity/internal/git"
)

// humaHandleWorkspaceGitStatus is the Huma-typed handler for GET
// /v0/city/{cityName}/session/{id}/workspace/git/status: the session folder's
// branch, upstream divergence, and per-path worktree state, read through the
// hardened git helper.
func (s *Server) humaHandleWorkspaceGitStatus(ctx context.Context, input *WorkspaceGitStatusInput) (*IndexOutput[gitStatusResponse], error) {
	index := s.latestIndex()
	store := s.state.Stores().Sessions()
	if store == (beads.SessionStore{}) {
		return nil, apierr.ServiceUnavailable.Msg("no bead store configured")
	}
	folder, err := s.resolveWorkspaceFolder(store, input.ID)
	if err != nil {
		return nil, err
	}
	st, err := git.Hardened{Dir: folder}.Status(ctx)
	if err != nil {
		return nil, workspaceGitError(err)
	}
	body := gitStatusResponse{
		Branch:    st.Branch,
		Detached:  st.Detached,
		Upstream:  st.Upstream,
		Ahead:     st.Ahead,
		Behind:    st.Behind,
		Entries:   []gitStatusEntry{},
		Truncated: st.Truncated,
	}
	for _, e := range st.Entries {
		body.Entries = append(body.Entries, gitStatusEntry{
			Path:   e.Path,
			State:  workspaceFileState(e.Index, e.Worktree),
			Staged: e.Staged,
		})
	}
	freshness := cacheAgeSeconds(store)
	return &IndexOutput[gitStatusResponse]{
		Index:        index,
		CacheAgeS:    freshness.ageS,
		CacheRefresh: freshness.refresh,
		Body:         body,
	}, nil
}

// humaHandleWorkspaceGitBranch is the Huma-typed handler for GET
// /v0/city/{cityName}/session/{id}/workspace/git/branch: the session
// folder's current branch and HEAD sha.
func (s *Server) humaHandleWorkspaceGitBranch(ctx context.Context, input *WorkspaceGitBranchInput) (*IndexOutput[gitBranchResponse], error) {
	index := s.latestIndex()
	store := s.state.Stores().Sessions()
	if store == (beads.SessionStore{}) {
		return nil, apierr.ServiceUnavailable.Msg("no bead store configured")
	}
	folder, err := s.resolveWorkspaceFolder(store, input.ID)
	if err != nil {
		return nil, err
	}
	bh, err := git.Hardened{Dir: folder}.Branch(ctx)
	if err != nil {
		return nil, workspaceGitError(err)
	}
	freshness := cacheAgeSeconds(store)
	return &IndexOutput[gitBranchResponse]{
		Index:        index,
		CacheAgeS:    freshness.ageS,
		CacheRefresh: freshness.refresh,
		Body:         gitBranchResponse{Branch: bh.Branch, Detached: bh.Detached, SHA: bh.SHA},
	}, nil
}

// humaHandleWorkspaceGitDiff is the Huma-typed handler for GET
// /v0/city/{cityName}/session/{id}/workspace/git/diff: a multi-file diff of
// the session folder in uncommitted, base, or commit mode, with per-file
// before/after blobs and patch sections capped at max_bytes.
func (s *Server) humaHandleWorkspaceGitDiff(ctx context.Context, input *WorkspaceGitDiffInput) (*IndexOutput[gitDiffResponse], error) {
	index := s.latestIndex()
	store := s.state.Stores().Sessions()
	if store == (beads.SessionStore{}) {
		return nil, apierr.ServiceUnavailable.Msg("no bead store configured")
	}
	mode := input.Mode
	if mode == "" {
		mode = "uncommitted"
	}
	maxBytes := input.MaxBytes
	if maxBytes <= 0 {
		maxBytes = defaultWorkspaceMaxBytes
	}
	filters := make([]string, 0, len(input.Path))
	for _, p := range input.Path {
		clean, err := checkWorkspacePathFilter(p)
		if err != nil {
			return nil, err
		}
		filters = append(filters, clean)
	}
	folder, err := s.resolveWorkspaceFolder(store, input.ID)
	if err != nil {
		return nil, err
	}
	h := git.Hardened{Dir: folder}
	var diff git.HardenedDiff
	switch mode {
	case "uncommitted":
		diff, err = h.DiffUncommitted(ctx, filters, int64(maxBytes))
	case "base":
		if input.Base == "" {
			return nil, apierr.InvalidRequest.Msg("mode=base requires the base ref")
		}
		diff, err = h.DiffSinceBase(ctx, input.Base, filters, int64(maxBytes))
	case "commit":
		if input.Commit == "" {
			return nil, apierr.InvalidRequest.Msg("mode=commit requires the commit ref")
		}
		diff, err = h.DiffCommit(ctx, input.Commit, filters, int64(maxBytes))
	default:
		return nil, apierr.InvalidRequest.Msgf("unknown diff mode %q: want uncommitted, base, or commit", mode)
	}
	if err != nil {
		return nil, workspaceGitError(err)
	}
	body := gitDiffResponse{
		Mode:         diff.Mode,
		HeadSHA:      diff.HeadSHA,
		BaseSHA:      diff.BaseSHA,
		MergeBaseSHA: diff.MergeBaseSHA,
		CommitSHA:    diff.CommitSHA,
		Files:        []gitDiffFile{},
		Truncated:    diff.Truncated,
	}
	for _, f := range diff.Files {
		body.Files = append(body.Files, gitDiffFile{
			Path:      f.Path,
			Status:    f.Status,
			Before:    workspaceWireBlob(f.Before),
			After:     workspaceWireBlob(f.After),
			Patch:     f.Patch,
			Truncated: f.Truncated,
		})
	}
	freshness := cacheAgeSeconds(store)
	return &IndexOutput[gitDiffResponse]{
		Index:        index,
		CacheAgeS:    freshness.ageS,
		CacheRefresh: freshness.refresh,
		Body:         body,
	}, nil
}

// workspaceWireBlob projects a hardened blob onto the wire, preserving nil.
func workspaceWireBlob(b *git.HardenedBlob) *gitDiffBlob {
	if b == nil {
		return nil
	}
	return &gitDiffBlob{Content: b.Content, Encoding: b.Encoding, Truncated: b.Truncated}
}
