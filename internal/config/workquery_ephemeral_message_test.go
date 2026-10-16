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
