package sling

import (
	"path/filepath"
	"strings"

	"github.com/gastownhall/gascity/internal/beadmeta"
	"github.com/gastownhall/gascity/internal/beads"
	"github.com/gastownhall/gascity/internal/config"
	"github.com/gastownhall/gascity/internal/pathutil"
	workdirutil "github.com/gastownhall/gascity/internal/workdir"
)

// WorktreeEvidenceWholeOrAbsent reports whether a bead's managed-worktree
// evidence is whole (all eight ownership keys) or absent (none): the
// ga-ryeij1.1 Decision (b) boundary. Introducing a gc.work_dir path onto
// half-published evidence is what first exposes the bead to
// worktreeSpecForBead's missing-key error, so every gc.work_dir stamp — the
// sling stamp here and the claim fallback in cmd/gc — holds this gate. One
// implementation, shared by both sites, so the guard cannot drift.
func WorktreeEvidenceWholeOrAbsent(metadata map[string]string) bool {
	present := 0
	for _, key := range beadmeta.WorktreeOwnershipEvidenceKeys {
		if strings.TrimSpace(metadata[key]) != "" {
			present++
		}
	}
	return present == 0 || present == len(beadmeta.WorktreeOwnershipEvidenceKeys)
}

// slingWorkDirForBead resolves the gc.work_dir stamp for a freshly routed
// bead: the target lane's checkout, when it is known. A rig-lane bead whose
// sessions run in the shared rig checkout must name that checkout, because
// the supervisor's workspace reads resolve the work folder from the bead key
// (Hoppy T3-borrow spine TB-AD-11); a slung bead that names nothing leaves
// those reads with no folder until a claim lands one.
//
// The value is the target agent's resolved work dir, and only when it sits
// inside the bead's rig checkout: that is the shared-checkout shape the
// stamp is a fact about. Anything else (a per-slot base, a custom dir
// outside the rig, an unresolvable template) is a prediction about where a
// session will run, and a wrong prediction would poison the bead — the claim
// keeps a recorded checkout, so it could never correct it — while the claim
// stamps the actual session dir for exactly those beads. No rig dir, a
// recorded checkout under either key, or half-published worktree evidence
// likewise skips the stamp. "" means skip.
func slingWorkDirForBead(deps SlingDeps, agent config.Agent, beadID string, metadata map[string]string) string {
	if deps.Cfg == nil {
		return ""
	}
	rigDir := RigDirForBead(deps.Cfg, beadID)
	if rigDir == "" {
		return ""
	}
	if strings.TrimSpace(metadata[beadmeta.WorkDirMetadataKey]) != "" ||
		strings.TrimSpace(metadata[beadmeta.LegacyWorkDirMetadataKey]) != "" {
		return ""
	}
	if !WorktreeEvidenceWholeOrAbsent(metadata) {
		return ""
	}
	agentDir, err := workdirutil.ResolveWorkDirPathStrict(deps.CityPath, deps.CityName, agent.QualifiedName(), agent, deps.Cfg.Rigs)
	if err != nil || agentDir == "" || !filepath.IsAbs(agentDir) {
		return ""
	}
	if !pathutil.PathWithin(rigDir, agentDir) {
		return ""
	}
	return agentDir
}

// stampSlingWorkDir best-effort stamps gc.work_dir on a freshly routed bead.
// It re-reads the bead so the no-overwrite and whole-or-absent guards see
// the row as it stands after routing, then stamps the resolved lane
// checkout. Failures trace rather than warn: the stamp is purely advisory
// (the claim stamps the actual session dir for every bead this one skips),
// and a store failure loud enough to matter already fails routing itself,
// so a warning here would only train operators to ignore the sling's real
// ones and break the exact-warning contracts beside it.
func stampSlingWorkDir(deps SlingDeps, beadStore beads.MoleculeTarget, writer beads.Writer, agent config.Agent, beadID string) {
	bead, err := beadStore.Get(beadID)
	if err != nil {
		depsTracef(deps, "sling-core: work-dir stamp skipped, bead unreadable: %v", err)
		return
	}
	dir := slingWorkDirForBead(deps, agent, beadID, bead.Metadata)
	if dir == "" {
		return
	}
	if err := writer.SetMetadata(beadID, beadmeta.WorkDirMetadataKey, dir); err != nil {
		depsTracef(deps, "sling-core: work-dir stamp failed: %v", err)
	}
}
