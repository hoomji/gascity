package api

// Supervisor workspace-read Huma input/output types (Hoppy T3-borrow spine
// TB-AD-11/12/16/17): read-only git and file views over a session's folder.
// These types drive the OpenAPI spec for all
// /v0/city/{cityName}/session/{id}/workspace/* endpoints.

// WorkspaceGitStatusInput is the Huma input for GET
// /v0/city/{cityName}/session/{id}/workspace/git/status.
type WorkspaceGitStatusInput struct {
	CityScope
	ID string `path:"id" doc:"Session ID, alias, or runtime session_name."`
}

// gitStatusEntry is one path's worktree state.
type gitStatusEntry struct {
	Path   string `json:"path" doc:"Repo-relative path."`
	State  string `json:"state" doc:"Change state: added, modified, deleted, renamed, unmerged, or untracked."`
	Staged bool   `json:"staged" doc:"True when the index side codes a change (staged); an unstaged change has staged false and a non-untracked state."`
}

// gitStatusResponse is the body for GET .../workspace/git/status.
type gitStatusResponse struct {
	Branch    string           `json:"branch" doc:"Current branch name; empty when detached."`
	Detached  bool             `json:"detached" doc:"True when HEAD is detached."`
	Upstream  string           `json:"upstream,omitempty" doc:"Tracking branch (e.g. origin/main); empty when none."`
	Ahead     int              `json:"ahead,omitempty" doc:"Commits ahead of the upstream."`
	Behind    int              `json:"behind,omitempty" doc:"Commits behind the upstream."`
	Entries   []gitStatusEntry `json:"entries" doc:"One entry per changed path; renames report the new path."`
	Truncated bool             `json:"truncated,omitempty" doc:"True when the listing hit the stream bound and entries is partial."`
}

// WorkspaceGitBranchInput is the Huma input for GET
// /v0/city/{cityName}/session/{id}/workspace/git/branch.
type WorkspaceGitBranchInput struct {
	CityScope
	ID string `path:"id" doc:"Session ID, alias, or runtime session_name."`
}

// gitBranchResponse is the body for GET .../workspace/git/branch.
type gitBranchResponse struct {
	Branch   string `json:"branch" doc:"Current branch name; empty when detached."`
	Detached bool   `json:"detached" doc:"True when HEAD is detached."`
	SHA      string `json:"sha,omitempty" doc:"HEAD commit sha; empty when the repo has no commits yet."`
}

// WorkspaceGitDiffInput is the Huma input for GET
// /v0/city/{cityName}/session/{id}/workspace/git/diff.
type WorkspaceGitDiffInput struct {
	CityScope
	ID       string   `path:"id" doc:"Session ID, alias, or runtime session_name."`
	Mode     string   `query:"mode" required:"false" enum:"uncommitted,base,commit" default:"uncommitted" doc:"What to diff: uncommitted (worktree and index against HEAD, plus untracked), base (base's merge-base with HEAD against HEAD), or commit (one commit against its first parent). Omitted is uncommitted."`
	Base     string   `query:"base" required:"false" doc:"Base ref for mode=base; required in that mode, ignored otherwise."`
	Commit   string   `query:"commit" required:"false" doc:"Commit for mode=commit; required in that mode, ignored otherwise."`
	Path     []string `query:"path,explode" required:"false" doc:"Repo-relative path filter; repeatable, every match listed. A filter is a file or a directory (which matches everything under it). Absolute paths and .. escapes are refused."`
	MaxBytes int      `query:"max_bytes" required:"false" minimum:"0" maximum:"16777216" default:"524288" doc:"Per-file byte cap for each patch section and each before/after blob; content past the cap is cut and flagged truncated. Omitted or 0 = server default (524288)."`
}

// gitDiffBlob is one side of one file's content.
type gitDiffBlob struct {
	Content   string `json:"content" doc:"File content: raw text when encoding is utf8, base64 when binary."`
	Encoding  string `json:"encoding" doc:"Content encoding: utf8 or base64."`
	Truncated bool   `json:"truncated,omitempty" doc:"True when the content hit max_bytes and was cut."`
}

// gitDiffFile is one path's before/after pair plus its unified patch section.
type gitDiffFile struct {
	Path      string       `json:"path" doc:"Repo-relative path."`
	Status    string       `json:"status" doc:"Change status: added, modified, deleted, renamed, or untracked."`
	Before    *gitDiffBlob `json:"before,omitempty" doc:"Base-side content; absent when the file is new on this side."`
	After     *gitDiffBlob `json:"after,omitempty" doc:"Head-side content; absent when the file is deleted on this side."`
	Patch     string       `json:"patch,omitempty" doc:"Unified patch section for this file; empty for untracked files and headless repos."`
	Truncated bool         `json:"truncated,omitempty" doc:"True when the patch or either blob hit max_bytes, or the stream bound cut the listing."`
}

// gitDiffResponse is the body for GET .../workspace/git/diff.
type gitDiffResponse struct {
	Mode         string        `json:"mode" doc:"Diff mode served: uncommitted, base, or commit."`
	HeadSHA      string        `json:"head_sha,omitempty" doc:"Resolved HEAD (uncommitted and base modes)."`
	BaseSHA      string        `json:"base_sha,omitempty" doc:"Resolved base ref (base mode)."`
	MergeBaseSHA string        `json:"merge_base_sha,omitempty" doc:"Merge-base actually diffed (base mode); equals base_sha when the histories share no ancestor."`
	CommitSHA    string        `json:"commit_sha,omitempty" doc:"Resolved commit (commit mode)."`
	Files        []gitDiffFile `json:"files" doc:"One entry per changed path."`
	Truncated    bool          `json:"truncated,omitempty" doc:"True when the stream bound cut the file list and it is partial."`
}

// defaultWorkspaceMaxBytes is the per-file byte cap when the client omits
// max_bytes or sends 0.
const defaultWorkspaceMaxBytes = 524288
