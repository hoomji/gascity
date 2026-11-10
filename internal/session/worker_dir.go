package session

import "strings"

// WorkerDirFromInfo returns the agent process working directory recorded on a
// session, reading the canonical worker_dir mirror (Info.WorkerDir) first and
// falling back to the legacy work_dir mirror (Info.WorkDir) when the canonical
// value is absent or whitespace-only. Empty result means "no worker dir
// recorded."
//
// It is the session.Info form of contract.WorkerDirFromMetadata: because
// Info.WorkerDir mirrors beadmeta.WorkerDirMetadataKey verbatim and Info.WorkDir
// mirrors the legacy work_dir key verbatim, the canonical→legacy precedence and
// the whitespace-normalizing TrimSpace are byte-identical to the raw-metadata
// read. TestWorkerDirFromInfoMatchesContract pins that equivalence.
func WorkerDirFromInfo(info Info) string {
	if v := strings.TrimSpace(info.WorkerDir); v != "" {
		return v
	}
	return strings.TrimSpace(info.WorkDir)
}

// WorkspaceDirFromInfo returns the folder the supervisor reads a session's
// workspace from: the routed worktree (gc.work_dir) first, then the
// agent-process cwd (worker_dir), then the legacy work_dir — each trimmed,
// first non-empty wins. The worktree leads because the supervisor's git and
// file reads run at the worktree root; the older keys keep sessions recorded
// before the gc.work_dir stamp readable. Empty result means "no folder
// recorded," which the workspace endpoints refuse as unsupported_runtime.
//
// It is deliberately separate from WorkerDirFromInfo: that one answers "where
// does the agent process run" (cwd, for transcript discovery), this one "what
// worktree did the work route to" (root, for workspace reads). The two agree
// when the agent runs at its worktree root and differ when it runs in a
// subdirectory — neither precedence subsumes the other.
func WorkspaceDirFromInfo(info Info) string {
	if v := strings.TrimSpace(info.WorkDirCanonical); v != "" {
		return v
	}
	return WorkerDirFromInfo(info)
}
