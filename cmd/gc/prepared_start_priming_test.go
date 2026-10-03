package main

import (
	"strings"
	"testing"

	"github.com/gastownhall/gascity/internal/beads"
	"github.com/gastownhall/gascity/internal/config"
	sessionpkg "github.com/gastownhall/gascity/internal/session"
	"github.com/gastownhall/gascity/internal/session/sessiontest"
)

// TestPreparedStartPromptDelivered pins the S19 B0 trap: prepared.promptDelivered
// is the pure delivery decision AND-ed with the fresh-launch condition, so a
// resume incarnation whose recorded prompt_hash matches the rendered template
// reports false (the conversation already holds that prompt) even though the
// launch path re-sets GC_STARTUP_PROMPT_DELIVERED="1" for hook consumption. A
// resume whose template changed (or was never stamped) re-delivers the prompt
// through the restart nudge and reports true so the new hash is stamped. It
// also pins promptHash.
func TestPreparedStartPromptDelivered(t *testing.T) {
	const prompt = "do the work"

	cases := []struct {
		name          string
		prompt        string
		startedHash   string // non-empty ⇒ not firstStart
		sessionKey    string // non-empty ⇒ hasResumeKey
		wakeMode      string // "fresh" ⇒ forceFresh
		promptHash    string // stored prompt_hash; "match" ⇒ hash of prompt
		wantDelivered bool
	}{
		{name: "fresh first start delivers", prompt: prompt, wantDelivered: true},
		{name: "no resume key delivers even with started hash", prompt: prompt, startedHash: "cfg", wantDelivered: true},
		{name: "force fresh delivers despite resume key", prompt: prompt, startedHash: "cfg", sessionKey: "warm", wakeMode: "fresh", wantDelivered: true},
		{name: "resume incarnation does NOT deliver (the trap)", prompt: prompt, startedHash: "cfg", sessionKey: "warm", promptHash: "match", wantDelivered: false},
		{name: "resume with a changed template re-delivers", prompt: prompt, startedHash: "cfg", sessionKey: "warm", promptHash: "stale", wantDelivered: true},
		{name: "resume never stamped re-delivers", prompt: prompt, startedHash: "cfg", sessionKey: "warm", wantDelivered: true},
		{name: "empty prompt never delivers", prompt: "", startedHash: "", wantDelivered: false},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			store := beads.NewMemStore()
			meta := map[string]string{
				"session_name": "worker",
				"template":     "worker",
				"state":        "asleep",
			}
			if tc.startedHash != "" {
				meta["started_config_hash"] = tc.startedHash
			}
			if tc.sessionKey != "" {
				meta["session_key"] = tc.sessionKey
			}
			if tc.wakeMode != "" {
				meta["wake_mode"] = tc.wakeMode
			}
			switch tc.promptHash {
			case "match":
				meta[sessionpkg.PromptHashMetadataKey] = sessionpkg.PromptHash(tc.prompt)
			case "":
			default:
				meta[sessionpkg.PromptHashMetadataKey] = tc.promptHash
			}
			session, err := store.Create(beads.Bead{
				Title:    "worker",
				Type:     sessionBeadType,
				Labels:   []string{sessionBeadLabel},
				Metadata: meta,
			})
			if err != nil {
				t.Fatalf("Create(session): %v", err)
			}
			candidate := startCandidate{
				info: sessiontest.SeedBead(t, session),
				tp: TemplateParams{
					TemplateName: "worker",
					SessionName:  "worker",
					Command:      "claude",
					Prompt:       tc.prompt,
				},
			}
			prepared, _, err := buildPreparedStart(candidate, &config.City{}, store)
			if err != nil {
				t.Fatalf("buildPreparedStart: %v", err)
			}
			if prepared.promptDelivered != tc.wantDelivered {
				t.Errorf("promptDelivered = %v, want %v", prepared.promptDelivered, tc.wantDelivered)
			}
			if got, want := prepared.promptHash, sessionpkg.PromptHash(tc.prompt); got != want {
				t.Errorf("promptHash = %q, want %q", got, want)
			}
			// The env marker choreography is untouched: on the resume row it is
			// still re-set to "1" even though nothing is delivered — the exact
			// reason promptDelivered cannot be inferred from it.
			if tc.name == "resume incarnation does NOT deliver (the trap)" {
				if prepared.cfg.Env[startupPromptDeliveredEnv] != "1" {
					t.Errorf("resume path must still set %s=1 for hooks; got %q", startupPromptDeliveredEnv, prepared.cfg.Env[startupPromptDeliveredEnv])
				}
				if strings.Contains(prepared.cfg.Nudge, tc.prompt) || !strings.Contains(prepared.cfg.Nudge, "Session resumed") {
					t.Errorf("unchanged-hash resume must send the short note, not the prompt; got %q", prepared.cfg.Nudge)
				}
			}
			if tc.name == "resume with a changed template re-delivers" && !strings.Contains(prepared.cfg.Nudge, tc.prompt) {
				t.Errorf("changed-hash resume must carry the prompt in the nudge; got %q", prepared.cfg.Nudge)
			}
		})
	}
}
