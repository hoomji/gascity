package config

import (
	"encoding/json"
	"os/exec"
	"strings"
	"testing"
)

// An open message bead assigned to the session must not win the assigned
// tiers: the claim path drops messages, so serving one ends the tick as
// no_work while routed pool work sits unqueried below it.

const ephemeralMessageRow = `{"id":"msg-1","status":"open","issue_type":"message","assignee":"sess-1","title":"hello","created_at":"2026-10-16T00:00:00Z","dependency_count":0}`

func fakeBdMessageThenStep(status string) string {
	step := `{"id":"step-1","status":"` + status + `","issue_type":"task","assignee":"sess-1","title":"step","created_at":"2026-10-16T01:00:00Z","dependency_count":0}`
	return `#!/bin/sh
case "$1" in
  query) printf '%s' '[` + ephemeralMessageRow + `,` + step + `]' ;;
  *) printf '[]' ;;
esac
`
}

func serveFirstID(t *testing.T, out string) string {
	t.Helper()
	var rows []map[string]any
	if err := json.Unmarshal([]byte(strings.TrimSpace(out)), &rows); err != nil {
		t.Fatalf("probe output is not a JSON array: %v (output %q)", err, out)
	}
	if len(rows) == 0 {
		return ""
	}
	id, _ := rows[0]["id"].(string)
	return id
}

func TestEphemeralReadyProbeSkipsMessageBeads(t *testing.T) {
	if _, err := exec.LookPath("jq"); err != nil {
		t.Skip("jq not available; the work-query shell requires it")
	}
	script := ephemeralAssignedReadyProbeScript("GC_SESSION_ID", QueryTopology{}) + `printf "[]"`
	out := runShellWithFakeBd(t, script, map[string]string{"GC_SESSION_ID": "sess-1"}, fakeBdMessageThenStep("open"))
	if got := serveFirstID(t, out); got != "step-1" {
		t.Fatalf("ready probe served %q, want the older message skipped and step-1 served", got)
	}
}

func TestEphemeralReadyProbeYieldsNothingForOnlyMessage(t *testing.T) {
	if _, err := exec.LookPath("jq"); err != nil {
		t.Skip("jq not available; the work-query shell requires it")
	}
	bd := `#!/bin/sh
case "$1" in
  query) printf '%s' '[` + ephemeralMessageRow + `]' ;;
  *) printf '[]' ;;
esac
`
	script := ephemeralAssignedReadyProbeScript("GC_SESSION_ID", QueryTopology{}) + `printf "[]"`
	out := runShellWithFakeBd(t, script, map[string]string{"GC_SESSION_ID": "sess-1"}, bd)
	if got := serveFirstID(t, out); got != "" {
		t.Fatalf("ready probe served message %q; the routed tier would never be reached", got)
	}
}

func TestEphemeralInProgressProbeSkipsMessageBeads(t *testing.T) {
	if _, err := exec.LookPath("jq"); err != nil {
		t.Skip("jq not available; the work-query shell requires it")
	}
	msg := strings.Replace(ephemeralMessageRow, `"status":"open"`, `"status":"in_progress"`, 1)
	bd := `#!/bin/sh
case "$1" in
  query) printf '%s' '[` + msg + `]' ;;
  *) printf '[]' ;;
esac
`
	script := ephemeralAssignedInProgressProbeScript("GC_SESSION_ID", QueryTopology{}) + `printf "[]"`
	out := runShellWithFakeBd(t, script, map[string]string{"GC_SESSION_ID": "sess-1"}, bd)
	if got := serveFirstID(t, out); got != "" {
		t.Fatalf("in_progress probe served message %q", got)
	}
}

// The jq clause must agree with Go's hookClaimCandidateIsMessage
// (TrimSpace + EqualFold on issue_type), or a row the claim path drops is
// served by the shell tier and starves the routed pool below it.
func TestEphemeralReadyProbeMessageMatchFoldsCaseAndSpace(t *testing.T) {
	if _, err := exec.LookPath("jq"); err != nil {
		t.Skip("jq not available; the work-query shell requires it")
	}
	for _, typ := range []string{"Message", "MESSAGE", " message ", "\\tmessage\\n"} {
		msg := strings.Replace(ephemeralMessageRow, `"issue_type":"message"`, `"issue_type":"`+typ+`"`, 1)
		step := `{"id":"step-1","status":"open","issue_type":"task","assignee":"sess-1","title":"step","created_at":"2026-10-16T01:00:00Z","dependency_count":0}`
		bd := "#!/bin/sh\ncase \"$1\" in\n  query) printf '%s' '[" + msg + "," + step + "]' ;;\n  *) printf '[]' ;;\nesac\n"
		script := ephemeralAssignedReadyProbeScript("GC_SESSION_ID", QueryTopology{}) + `printf "[]"`
		out := runShellWithFakeBd(t, script, map[string]string{"GC_SESSION_ID": "sess-1"}, bd)
		if got := serveFirstID(t, out); got != "step-1" {
			t.Errorf("issue_type %q: probe served %q, want message skipped and step-1", typ, got)
		}
	}
}

// Go decodes only issue_type (beads.Bead.Type), so a row carrying just a
// legacy "type":"message" is NOT a message to the claim path. The shell must
// not skip what Go would serve.
func TestEphemeralReadyProbeIgnoresLegacyTypeKey(t *testing.T) {
	if _, err := exec.LookPath("jq"); err != nil {
		t.Skip("jq not available; the work-query shell requires it")
	}
	row := `{"id":"legacy-1","status":"open","type":"message","assignee":"sess-1","title":"t","created_at":"2026-10-16T00:00:00Z","dependency_count":0}`
	bd := "#!/bin/sh\ncase \"$1\" in\n  query) printf '%s' '[" + row + "]' ;;\n  *) printf '[]' ;;\nesac\n"
	script := ephemeralAssignedReadyProbeScript("GC_SESSION_ID", QueryTopology{}) + `printf "[]"`
	out := runShellWithFakeBd(t, script, map[string]string{"GC_SESSION_ID": "sess-1"}, bd)
	if got := serveFirstID(t, out); got != "legacy-1" {
		t.Fatalf("probe served %q; a legacy .type key must not be honored (Go ignores it)", got)
	}
}

// bd ready --include-ephemeral and bd list --status in_progress serve
// ephemeral message beads (mail is created Ephemeral:true) and carry no jq
// filter, so the message exclusion has to ride the bd flag.
func TestAssignedTiersExcludeMessageType(t *testing.T) {
	for _, topo := range []QueryTopology{{}, {FederatedReady: true}} {
		for name, got := range map[string]string{
			"assigned-ready":       assignedReadyTierCommand("id", topo),
			"assigned-in-progress": assignedInProgressTierCommand("id", topo),
		} {
			if !strings.Contains(got, "--exclude-type=message") {
				t.Errorf("%s (federated=%v) lacks --exclude-type=message: %q", name, topo.FederatedReady, got)
			}
		}
	}
}
