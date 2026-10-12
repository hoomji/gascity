package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gastownhall/gascity/internal/mail"
)

func mailMsgs(ids ...string) []mail.Message {
	out := make([]mail.Message, 0, len(ids))
	for _, id := range ids {
		out = append(out, mail.Message{ID: id})
	}
	return out
}

func injectSeenTestEnv(t *testing.T, sessionID string) string {
	t.Helper()
	clearGCEnv(t)
	t.Setenv("GC_SESSION_ID", sessionID)
	t.Setenv("GC_ALIAS", "")
	t.Setenv("GC_INJECT_ALWAYS", "")
	return t.TempDir()
}

func TestInjectSeenRecordThenUnchanged(t *testing.T) {
	city := injectSeenTestEnv(t, "s1")
	seen := openInjectSeen(city, injectSeenKindStep, "")

	if seen.Unchanged("abc") {
		t.Fatal("nothing recorded yet: must read as never shown")
	}
	seen.Record("abc")
	if !seen.Unchanged("abc") {
		t.Fatal("recorded digest must read as unchanged")
	}
	if seen.Unchanged("abd") {
		t.Fatal("a different digest must read as changed")
	}
	seen.Forget()
	if seen.Unchanged("abc") {
		t.Fatal("a forgotten digest must read as never shown")
	}
}

func TestInjectSeenKindsDoNotShareState(t *testing.T) {
	city := injectSeenTestEnv(t, "s1")
	openInjectSeen(city, injectSeenKindStep, "").Record("abc")
	if openInjectSeen(city, injectSeenKindMail, "").Unchanged("abc") {
		t.Fatal("step and mail hooks run concurrently and must not share a file")
	}
}

func TestInjectSeenSessionsDoNotShareState(t *testing.T) {
	city := injectSeenTestEnv(t, "s1")
	openInjectSeen(city, injectSeenKindStep, "").Record("abc")
	t.Setenv("GC_SESSION_ID", "s2")
	if openInjectSeen(city, injectSeenKindStep, "").Unchanged("abc") {
		t.Fatal("a different session id must start from never shown")
	}
}

func TestInjectSeenUnreadableStateMeansNeverShown(t *testing.T) {
	city := injectSeenTestEnv(t, "s1")
	seen := openInjectSeen(city, injectSeenKindStep, "")
	seen.Record("abc")

	for _, garbage := range []string{"", "not json", `{"sha256":`, `{"sha256":42}`, `[]`} {
		if err := os.WriteFile(seen.path, []byte(garbage), 0o644); err != nil {
			t.Fatalf("WriteFile: %v", err)
		}
		if seen.Unchanged("abc") {
			t.Fatalf("state %q must read as never shown", garbage)
		}
	}
	if err := os.Remove(seen.path); err != nil {
		t.Fatalf("Remove: %v", err)
	}
	if seen.Unchanged("abc") {
		t.Fatal("a missing file must read as never shown")
	}
}

func TestInjectSeenOffWithoutIdentityOrCity(t *testing.T) {
	city := injectSeenTestEnv(t, "")
	if openInjectSeen(city, injectSeenKindStep, "") != nil {
		t.Fatal("no session id and no alias: nothing to key on, dedup must be off")
	}
	t.Setenv("GC_SESSION_ID", "s1")
	if openInjectSeen("", injectSeenKindStep, "") != nil {
		t.Fatal("no city: nowhere to store, dedup must be off")
	}
	var off *injectSeen
	off.Record("abc")
	off.Forget()
	if off.Unchanged("abc") {
		t.Fatal("a nil seen-state is dedup off and must never report unchanged")
	}
}

func TestInjectAlwaysValues(t *testing.T) {
	city := injectSeenTestEnv(t, "s1")
	for value, wantOff := range map[string]bool{
		"1": true, "true": true, "yes": true,
		"": false, "0": false, "false": false, "off": false, "no": false,
	} {
		t.Setenv("GC_INJECT_ALWAYS", value)
		if got := openInjectSeen(city, injectSeenKindStep, "") == nil; got != wantOff {
			t.Errorf("GC_INJECT_ALWAYS=%q: dedup disabled = %v, want %v", value, got, wantOff)
		}
	}
}

func TestInjectSeenStaysInsideItsDirectory(t *testing.T) {
	city := injectSeenTestEnv(t, "../../escape")
	seen := openInjectSeen(city, injectSeenKindStep, "")
	seen.Record("abc")

	dir := filepath.Dir(seen.path)
	if !strings.HasSuffix(dir, filepath.Join("runtime", "inject-seen")) {
		t.Fatalf("seen-state escaped its directory: %s", seen.path)
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("ReadDir: %v", err)
	}
	if len(entries) != 1 || strings.Contains(entries[0].Name(), ".tmp.") {
		t.Fatalf("want exactly the one state file and no temp leftovers, got %v", entries)
	}

	// Aliases are qualified names with a slash; two that sanitize alike must
	// still be different sessions.
	t.Setenv("GC_SESSION_ID", "")
	t.Setenv("GC_ALIAS", "rig/worker")
	openInjectSeen(city, injectSeenKindStep, "").Record("abc")
	t.Setenv("GC_ALIAS", "rig_worker")
	if openInjectSeen(city, injectSeenKindStep, "").Unchanged("abc") {
		t.Fatal("rig/worker and rig_worker are different sessions")
	}
}

func TestMailUnreadDigestIsOrderIndependentAndSetSensitive(t *testing.T) {
	a := mailMsgs("m1", "m2")
	if mailUnreadDigest("w", a) != mailUnreadDigest("w", mailMsgs("m2", "m1")) {
		t.Fatal("arrival order must not change the digest")
	}
	if mailUnreadDigest("w", a) == mailUnreadDigest("w", mailMsgs("m1", "m2", "m3")) {
		t.Fatal("a new id must change the digest")
	}
	if mailUnreadDigest("w", a) == mailUnreadDigest("w", mailMsgs("m1")) {
		t.Fatal("a removed id must change the digest")
	}
	if mailUnreadDigest("w", a) == mailUnreadDigest("other", a) {
		t.Fatal("a different recipient must change the digest")
	}
}

func TestInjectSeenDigestBelongsToTheEpochThatReceivedIt(t *testing.T) {
	city := injectSeenTestEnv(t, "s1")
	openInjectSeen(city, injectSeenKindStep, "provider-a").Record("abc")

	if !openInjectSeen(city, injectSeenKindStep, "provider-a").Unchanged("abc") {
		t.Fatal("the epoch that received the block must see it as delivered")
	}
	if openInjectSeen(city, injectSeenKindStep, "provider-b").Unchanged("abc") {
		t.Fatal("a new context epoch under the same session identity holds none of the block")
	}
	if openInjectSeen(city, injectSeenKindStep, "").Unchanged("abc") {
		t.Fatal("a payload that names no epoch must not claim another epoch's delivery")
	}
}

func TestForgetInjectSeenDropsEveryKindForTheCallingSessionOnly(t *testing.T) {
	city := injectSeenTestEnv(t, "s1")
	openInjectSeen(city, injectSeenKindStep, "").Record("step")
	openInjectSeen(city, injectSeenKindMail, "").Record("mail")
	t.Setenv("GC_SESSION_ID", "s2")
	openInjectSeen(city, injectSeenKindStep, "").Record("other")

	t.Setenv("GC_SESSION_ID", "s1")
	forgetInjectSeen(city)

	if openInjectSeen(city, injectSeenKindStep, "").Unchanged("step") {
		t.Error("step memory survived a session start")
	}
	if openInjectSeen(city, injectSeenKindMail, "").Unchanged("mail") {
		t.Error("mail memory survived a session start")
	}
	t.Setenv("GC_SESSION_ID", "s2")
	if !openInjectSeen(city, injectSeenKindStep, "").Unchanged("other") {
		t.Error("forgetting one session must leave another session's memory alone")
	}
}

func TestHookContextEpoch(t *testing.T) {
	for name, tc := range map[string]struct {
		input string
		want  string
	}{
		"claude session id":    {`{"session_id":"abc","transcript_path":"/t"}`, "abc"},
		"cursor conversation":  {`{"conversation_id":"conv-1"}`, "conv-1"},
		"session id wins":      {`{"session_id":"abc","conversation_id":"conv-1"}`, "abc"},
		"neither":              {`{"prompt":"hi"}`, ""},
		"malformed":            {`not json`, ""},
		"absent":               {``, ""},
		"whitespace only":      {`{"session_id":"  "}`, ""},
		"trims surrounding ws": {`{"session_id":" abc "}`, "abc"},
	} {
		if got := hookContextEpoch([]byte(tc.input)); got != tc.want {
			t.Errorf("%s: hookContextEpoch(%q) = %q, want %q", name, tc.input, got, tc.want)
		}
	}
}
