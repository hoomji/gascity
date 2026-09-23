package main

import (
	"encoding/json"
	"io"
	"os"
	"strings"
)

const (
	hookOutputFormatAntigravity = "antigravity"
	hookOutputFormatCodex       = "codex"
	hookOutputFormatGemini      = "gemini"
)

func writeProviderHookContextForEvent(stdout io.Writer, format, eventName, content string) error {
	if content == "" {
		return nil
	}
	switch strings.ToLower(strings.TrimSpace(format)) {
	case hookOutputFormatAntigravity:
		return json.NewEncoder(stdout).Encode(antigravityHookAdditionalContext(content))
	case hookOutputFormatCodex:
		return json.NewEncoder(stdout).Encode(codexHookOutput(eventName, content))
	case hookOutputFormatGemini:
		return json.NewEncoder(stdout).Encode(geminiHookAdditionalContext(content))
	}
	_, err := io.WriteString(stdout, content)
	return err
}

func antigravityHookAdditionalContext(content string) map[string]any {
	return map[string]any{
		"injectSteps": []map[string]any{
			{"ephemeralMessage": strings.TrimRight(content, "\n")},
		},
	}
}

func codexHookOutput(eventName, content string) map[string]any {
	if strings.EqualFold(strings.TrimSpace(eventName), "Stop") {
		return map[string]any{
			"decision": "block",
			"reason":   strings.TrimRight(content, "\n"),
		}
	}
	return codexHookAdditionalContext(eventName, content)
}

func codexHookAdditionalContext(eventName, content string) map[string]any {
	if strings.TrimSpace(eventName) == "" {
		eventName = strings.TrimSpace(os.Getenv("GC_HOOK_EVENT_NAME"))
	}
	if strings.TrimSpace(eventName) == "" {
		eventName = "SessionStart"
	}
	// Codex's PreCompactCommandOutputWire is deny_unknown_fields and has no
	// hookSpecificOutput variant (unlike SessionStart/UserPromptSubmit), so the
	// Claude envelope is rejected with "hook returned invalid PreCompact hook
	// JSON output". Emit only the one accepted field; an empty object or empty
	// stdout fails too, and continue:false/stopReason would block compaction.
	if strings.EqualFold(strings.TrimSpace(eventName), "PreCompact") {
		return map[string]any{
			"systemMessage": strings.TrimRight(content, "\n"),
		}
	}
	return map[string]any{
		"hookSpecificOutput": map[string]any{
			"hookEventName":     eventName,
			"additionalContext": strings.TrimRight(content, "\n"),
		},
	}
}

func geminiHookAdditionalContext(content string) map[string]any {
	return map[string]any{
		"hookSpecificOutput": map[string]any{
			"additionalContext": strings.TrimRight(content, "\n"),
		},
	}
}
