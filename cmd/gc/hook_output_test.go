package main

import (
	"bytes"
	"encoding/json"
	"testing"
)

func TestWriteProviderHookContextGemini(t *testing.T) {
	var out bytes.Buffer
	err := writeProviderHookContextForEvent(&out, "gemini", "", "<system-reminder>\nhello\n</system-reminder>\n")
	if err != nil {
		t.Fatalf("writeProviderHookContextForEvent: %v", err)
	}

	var payload struct {
		HookSpecificOutput struct {
			AdditionalContext string `json:"additionalContext"`
		} `json:"hookSpecificOutput"`
	}
	if err := json.Unmarshal(out.Bytes(), &payload); err != nil {
		t.Fatalf("unmarshal output: %v\n%s", err, out.String())
	}
	if got, want := payload.HookSpecificOutput.AdditionalContext, "<system-reminder>\nhello\n</system-reminder>"; got != want {
		t.Fatalf("additionalContext = %q, want %q", got, want)
	}
}

func TestWriteProviderHookContextAntigravity(t *testing.T) {
	var out bytes.Buffer
	err := writeProviderHookContextForEvent(&out, "antigravity", "", "<system-reminder>\nhello\n</system-reminder>\n")
	if err != nil {
		t.Fatalf("writeProviderHookContextForEvent: %v", err)
	}

	var payload struct {
		InjectSteps []struct {
			EphemeralMessage string `json:"ephemeralMessage"`
		} `json:"injectSteps"`
	}
	if err := json.Unmarshal(out.Bytes(), &payload); err != nil {
		t.Fatalf("unmarshal output: %v\n%s", err, out.String())
	}
	if got, want := len(payload.InjectSteps), 1; got != want {
		t.Fatalf("len(injectSteps) = %d, want %d", got, want)
	}
	if got, want := payload.InjectSteps[0].EphemeralMessage, "<system-reminder>\nhello\n</system-reminder>"; got != want {
		t.Fatalf("ephemeralMessage = %q, want %q", got, want)
	}
}

func TestWriteProviderHookContextCodex(t *testing.T) {
	var out bytes.Buffer
	err := writeProviderHookContextForEvent(&out, "codex", "Stop", "<system-reminder>\nhello\n</system-reminder>\n")
	if err != nil {
		t.Fatalf("writeProviderHookContextForEvent: %v", err)
	}

	var payload struct {
		Decision string `json:"decision"`
		Reason   string `json:"reason"`
	}
	if err := json.Unmarshal(out.Bytes(), &payload); err != nil {
		t.Fatalf("unmarshal output: %v\n%s", err, out.String())
	}
	if got, want := payload.Decision, "block"; got != want {
		t.Fatalf("decision = %q, want %q", got, want)
	}
	if got, want := payload.Reason, "<system-reminder>\nhello\n</system-reminder>"; got != want {
		t.Fatalf("reason = %q, want %q", got, want)
	}
}

func TestWriteProviderHookContextCodexAdditionalContext(t *testing.T) {
	var out bytes.Buffer
	err := writeProviderHookContextForEvent(&out, "codex", "UserPromptSubmit", "<system-reminder>\nhello\n</system-reminder>\n")
	if err != nil {
		t.Fatalf("writeProviderHookContextForEvent: %v", err)
	}

	var payload struct {
		HookSpecificOutput struct {
			HookEventName     string `json:"hookEventName"`
			AdditionalContext string `json:"additionalContext"`
		} `json:"hookSpecificOutput"`
	}
	if err := json.Unmarshal(out.Bytes(), &payload); err != nil {
		t.Fatalf("unmarshal output: %v\n%s", err, out.String())
	}
	if got, want := payload.HookSpecificOutput.HookEventName, "UserPromptSubmit"; got != want {
		t.Fatalf("hookEventName = %q, want %q", got, want)
	}
	if got, want := payload.HookSpecificOutput.AdditionalContext, "<system-reminder>\nhello\n</system-reminder>"; got != want {
		t.Fatalf("additionalContext = %q, want %q", got, want)
	}
}

// TestWriteProviderHookContextCodexPreCompactEmitsSystemMessageOnly locks the
// codex PreCompact shape: exactly {"systemMessage":"..."} and nothing else.
// Codex's PreCompactCommandOutputWire has no hookSpecificOutput variant and is
// deny_unknown_fields, so the Claude envelope is rejected; an empty object or
// empty stdout also fails, and continue/stopReason would block compaction.
func TestWriteProviderHookContextCodexPreCompactEmitsSystemMessageOnly(t *testing.T) {
	const content = "<system-reminder>\nhello\n</system-reminder>\n"
	const wantMessage = "<system-reminder>\nhello\n</system-reminder>"

	var out bytes.Buffer
	if err := writeProviderHookContextForEvent(&out, "codex", "PreCompact", content); err != nil {
		t.Fatalf("writeProviderHookContextForEvent: %v", err)
	}

	var fields map[string]json.RawMessage
	if err := json.Unmarshal(out.Bytes(), &fields); err != nil {
		t.Fatalf("unmarshal output: %v\n%s", err, out.String())
	}
	if len(fields) != 1 {
		t.Fatalf("PreCompact codex output fields = %v, want exactly systemMessage", fields)
	}
	raw, ok := fields["systemMessage"]
	if !ok {
		t.Fatalf("PreCompact codex output = %s, want systemMessage", out.String())
	}
	var got string
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatalf("unmarshal systemMessage: %v", err)
	}
	if got != wantMessage {
		t.Fatalf("systemMessage = %q, want %q", got, wantMessage)
	}

	// The json.Encoder emits one line; assert the exact bytes so no envelope,
	// continue, stopReason, or suppressOutput field can creep back in.
	var want bytes.Buffer
	if err := json.NewEncoder(&want).Encode(map[string]any{"systemMessage": wantMessage}); err != nil {
		t.Fatalf("encode want: %v", err)
	}
	if out.String() != want.String() {
		t.Fatalf("raw output = %q, want %q", out.String(), want.String())
	}
}

// TestWriteProviderHookContextCodexPreCompactFromEnv covers the event name
// arriving via GC_HOOK_EVENT_NAME with no explicit argument.
func TestWriteProviderHookContextCodexPreCompactFromEnv(t *testing.T) {
	t.Setenv("GC_HOOK_EVENT_NAME", "PreCompact")

	var out bytes.Buffer
	if err := writeProviderHookContextForEvent(&out, "codex", "", "handoff note\n"); err != nil {
		t.Fatalf("writeProviderHookContextForEvent: %v", err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(out.Bytes(), &fields); err != nil {
		t.Fatalf("unmarshal output: %v\n%s", err, out.String())
	}
	if len(fields) != 1 {
		t.Fatalf("PreCompact codex output fields = %v, want exactly systemMessage", fields)
	}
	var got string
	if err := json.Unmarshal(fields["systemMessage"], &got); err != nil {
		t.Fatalf("unmarshal systemMessage: %v", err)
	}
	if got != "handoff note" {
		t.Fatalf("systemMessage = %q, want %q", got, "handoff note")
	}
}

// TestWriteProviderHookContextClaudePreCompactUnchanged guards the non-codex
// (Claude) PreCompact path: it keeps emitting the raw context text. Claude
// hooks invoke gc without --hook-format, and an explicit "claude" format takes
// the same plain-text path.
func TestWriteProviderHookContextClaudePreCompactUnchanged(t *testing.T) {
	const content = "<system-reminder>\nhello\n</system-reminder>\n"
	for _, format := range []string{"", "claude"} {
		t.Run("format="+format, func(t *testing.T) {
			var out bytes.Buffer
			if err := writeProviderHookContextForEvent(&out, format, "PreCompact", content); err != nil {
				t.Fatalf("writeProviderHookContextForEvent: %v", err)
			}
			if got := out.String(); got != content {
				t.Fatalf("output = %q, want %q", got, content)
			}
		})
	}
}

// TestWriteProviderHookContextCodexSessionStartUnchanged guards the other
// events against the PreCompact fix: SessionStart codex keeps the
// hookSpecificOutput envelope byte for byte.
func TestWriteProviderHookContextCodexSessionStartUnchanged(t *testing.T) {
	const content = "<system-reminder>\nhello\n</system-reminder>\n"
	const wantContext = "<system-reminder>\nhello\n</system-reminder>"

	var out bytes.Buffer
	if err := writeProviderHookContextForEvent(&out, "codex", "SessionStart", content); err != nil {
		t.Fatalf("writeProviderHookContextForEvent: %v", err)
	}

	var payload struct {
		HookSpecificOutput struct {
			HookEventName     string `json:"hookEventName"`
			AdditionalContext string `json:"additionalContext"`
		} `json:"hookSpecificOutput"`
	}
	if err := json.Unmarshal(out.Bytes(), &payload); err != nil {
		t.Fatalf("unmarshal output: %v\n%s", err, out.String())
	}
	if got, want := payload.HookSpecificOutput.HookEventName, "SessionStart"; got != want {
		t.Fatalf("hookEventName = %q, want %q", got, want)
	}
	if got, want := payload.HookSpecificOutput.AdditionalContext, wantContext; got != want {
		t.Fatalf("additionalContext = %q, want %q", got, want)
	}

	var want bytes.Buffer
	if err := json.NewEncoder(&want).Encode(map[string]any{
		"hookSpecificOutput": map[string]any{
			"hookEventName":     "SessionStart",
			"additionalContext": wantContext,
		},
	}); err != nil {
		t.Fatalf("encode want: %v", err)
	}
	if out.String() != want.String() {
		t.Fatalf("raw output = %q, want %q", out.String(), want.String())
	}
}

func TestWriteProviderHookContextCodexDefaultsSessionStartFromEnv(t *testing.T) {
	t.Setenv("GC_HOOK_EVENT_NAME", "SessionStart")

	var out bytes.Buffer
	err := writeProviderHookContextForEvent(&out, "codex", "", "<system-reminder>\nhello\n</system-reminder>\n")
	if err != nil {
		t.Fatalf("writeProviderHookContextForEvent: %v", err)
	}

	var payload struct {
		HookSpecificOutput struct {
			HookEventName     string `json:"hookEventName"`
			AdditionalContext string `json:"additionalContext"`
		} `json:"hookSpecificOutput"`
	}
	if err := json.Unmarshal(out.Bytes(), &payload); err != nil {
		t.Fatalf("unmarshal output: %v\n%s", err, out.String())
	}
	if got, want := payload.HookSpecificOutput.HookEventName, "SessionStart"; got != want {
		t.Fatalf("hookEventName = %q, want %q", got, want)
	}
	if got, want := payload.HookSpecificOutput.AdditionalContext, "<system-reminder>\nhello\n</system-reminder>"; got != want {
		t.Fatalf("additionalContext = %q, want %q", got, want)
	}
}

func TestWriteProviderHookContextPlain(t *testing.T) {
	var out bytes.Buffer
	err := writeProviderHookContextForEvent(&out, "", "", "<system-reminder>\nhello\n</system-reminder>\n")
	if err != nil {
		t.Fatalf("writeProviderHookContextForEvent: %v", err)
	}
	if got, want := out.String(), "<system-reminder>\nhello\n</system-reminder>\n"; got != want {
		t.Fatalf("output = %q, want %q", got, want)
	}
}
