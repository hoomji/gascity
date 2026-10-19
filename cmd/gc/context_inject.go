package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"

	"github.com/gastownhall/gascity/internal/config"
	"github.com/gastownhall/gascity/internal/modelwindow"
)

// Context-usage injection — the context-pressure sibling of clock_inject.go.
//
// Gas City has canonical handoff machinery (`gc handoff`, the PreCompact
// auto-handoff, deployment handoff skills) but agents have no signal for WHEN
// to trigger it: a session cannot see its own context usage (the provider
// footer is rendered for humans only), so unmonitored agents run into context
// compaction by default — losing the deliberate wrap-up (durable notes, bead
// updates, clean seams) the handoff machinery exists to provide.
//
// This reads the provider hook input (UserPromptSubmit JSON on stdin carries
// transcript_path), computes the session's current context footprint from the
// last usage entry in the transcript, and injects ONE line of guidance —
// folded into the same single provider payload as the clock (see
// cmd_nudge.go), so JSON hook formats stay one valid document.
//
// The reader is provider-aware: Claude transcripts expose per-message
// message.usage and name the model, while Codex rollouts emit event_msg
// token_count records whose last_token_usage.input_tokens is the live
// occupancy (it ALREADY includes cached input) and whose model_context_window
// is the live window. Both shapes are recognized in the tail, so the same
// hook path serves either provider.
//
// THRESHOLD-GATED BY DESIGN — not an always-on countdown. Model-provider
// guidance (Anthropic, Claude Fable 5 migration notes) documents "context
// anxiety": a continuously visible remaining-context count induces premature
// wrap-up and unprompted session-splitting. Below the advisory threshold this
// injects NOTHING. Above it, the message is actionable ("steer toward a clean
// handoff point", "run your handoff process now") and explicitly tells the
// agent NOT to panic-stop at the advisory tier.
//
//	< advisory (default 60%)  : silent
//	advisory..urgent (60–80%) : plan toward a clean handoff point
//	> urgent (default 80%)    : trigger the canonical handoff now
//
// Knobs: GC_INJECT_CONTEXT=0|false|off disables; GC_CONTEXT_ADVISORY_PCT and
// GC_CONTEXT_URGENT_PCT override the thresholds; GC_CONTEXT_WINDOW_TOKENS
// overrides the context-window size when model-string detection is wrong.
// Fail-safe: any parse/read problem returns "" — never blocks a prompt.

// hookStdinInput is the subset of the provider hook JSON we need.
type hookStdinInput struct {
	TranscriptPath string `json:"transcript_path"`
}

// transcriptUsage is the usage block shape inside Claude transcript entries.
type transcriptUsage struct {
	InputTokens              int `json:"input_tokens"`
	CacheReadInputTokens     int `json:"cache_read_input_tokens"`
	CacheCreationInputTokens int `json:"cache_creation_input_tokens"`
}

// codexTokenCountEntry is the subset of a Codex rollout event_msg token_count
// entry needed for context occupancy. The live window rides on the same entry.
type codexTokenCountEntry struct {
	Type    string `json:"type"`
	Payload struct {
		Type string `json:"type"`
		Info *struct {
			LastTokenUsage struct {
				// Codex input_tokens already includes cached_input_tokens, so
				// it is the occupancy directly; adding the cached subset again
				// would double-count it.
				InputTokens int `json:"input_tokens"`
			} `json:"last_token_usage"`
			ModelContextWindow *int `json:"model_context_window"`
		} `json:"info"`
	} `json:"payload"`
}

// codexTurnContextEntry carries the model in effect for a Codex turn. token_count
// itself names no model, so this is the window-table fallback when a rollout
// predates (or omits) model_context_window.
type codexTurnContextEntry struct {
	Type    string `json:"type"`
	Payload struct {
		Model string `json:"model"`
	} `json:"payload"`
}

// transcriptContextUsage is the newest qualifying usage record in a transcript
// plus the model/window evidence collected around it. Models drives the shared
// model-window table fallback; LiveWindow is the provider-reported window
// (Codex model_context_window) when present, and 0 otherwise.
type transcriptContextUsage struct {
	Tokens     int
	Models     []string
	LiveWindow int
}

// providerUsageRecord is one usage observation parsed from a transcript line.
type providerUsageRecord struct {
	tokens int
	model  string
	window int
}

// contextInjectLine returns the context-usage guidance line for the session
// whose hook input JSON is in hookInput, or "" when disabled, below the
// advisory threshold, or on any error (fail-safe silent).
func contextInjectLine(hookInput []byte) string {
	return contextInjectLineForAdvisory(hookInput, nil, nil)
}

// contextInjectLineForAdvisory applies city and agent context-advisory
// configuration to a hook payload. Environment variables remain the final
// compatibility override for enablement, thresholds, and window size.
func contextInjectLineForAdvisory(hookInput []byte, global, agent *config.ContextAdvisory) string {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("GC_INJECT_CONTEXT"))) {
	case "0", "false", "off":
		return ""
	}
	var in hookStdinInput
	if err := json.Unmarshal(hookInput, &in); err != nil || strings.TrimSpace(in.TranscriptPath) == "" {
		return ""
	}
	usage, ok := lastTranscriptUsage(in.TranscriptPath)
	if !ok {
		return ""
	}
	builtin := config.DefaultContextAdvisory()
	policy := config.ResolveContextAdvisory(&builtin, global, agent)
	window := contextWindowTokensWithOverride(usage.Models, usage.LiveWindow, policy.WindowTokens)
	return contextUsageMessageForPolicy(usage.Tokens, window, policy)
}

// lastTranscriptUsage reads the tail of a provider transcript (JSONL) and folds
// it into the context footprint of the most recent usage record plus the
// model/window evidence around it.
//
// Claude entries expose message.usage; the footprint is prompt-side input +
// cache reads + cache writes. Codex event_msg token_count entries expose
// last_token_usage.input_tokens, which ALREADY includes cached input (adding
// cached again double-counts), and model_context_window, the live window. The
// scan is shape-driven, not path-driven, so either provider's rollout is read
// without a provider flag. Lines of the other shape (e.g. a newer Codex
// token_usage_record, which carries a top-level "usage" but no message.usage)
// are ignored.
//
// The LAST qualifying record wins: after compaction the newest record reads low
// again, so an older high-water record must not keep the session pinned to the
// urgent tier. Every non-empty model string seen is retained — the window is
// the MAX over those (see contextWindowTokensWithOverride), so a smaller-window
// sidecar/compaction call logged in the same transcript can't shrink the
// main-loop session's window.
func lastTranscriptUsage(path string) (transcriptContextUsage, bool) {
	const tailBytes = 2 << 20 // last 2MiB is ample for the newest entries
	var usage transcriptContextUsage
	f, err := os.Open(path) //nolint:gosec // path comes from the provider hook input
	if err != nil {
		return usage, false
	}
	defer f.Close() //nolint:errcheck // read-only
	if st, err := f.Stat(); err == nil && st.Size() > tailBytes {
		if _, err := f.Seek(st.Size()-tailBytes, io.SeekStart); err != nil {
			return usage, false
		}
	}
	data, err := io.ReadAll(io.LimitReader(f, tailBytes))
	if err != nil {
		return usage, false
	}
	found := false
	for _, line := range strings.Split(string(data), "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}
		switch {
		case strings.Contains(line, `"usage"`):
			// Claude shape. A Codex token_usage_record also contains a
			// top-level "usage" key but no message.usage, so it parses to false
			// here and is skipped.
			record, ok := claudeUsageRecord(line)
			if !ok {
				continue
			}
			usage.Tokens = record.tokens
			usage.LiveWindow = 0
			if record.model != "" {
				usage.Models = append(usage.Models, record.model)
			}
			found = true
		case strings.Contains(line, `"token_count"`):
			record, ok := codexTokenCountRecord(line)
			if !ok {
				continue
			}
			usage.Tokens = record.tokens
			if record.window > 0 {
				usage.LiveWindow = record.window
			}
			found = true
		case strings.Contains(line, `"turn_context"`):
			if model, ok := codexTurnContextModel(line); ok && model != "" {
				usage.Models = append(usage.Models, model)
			}
		}
	}
	return usage, found
}

// claudeUsageRecord parses one Claude transcript line into a provider usage
// record. All three token buckets are summed: Claude reports cache reads and
// cache writes separately from input_tokens, so the sum is the prompt-side
// context footprint. The model string is empty when the entry omits it.
func claudeUsageRecord(line string) (providerUsageRecord, bool) {
	var entry struct {
		Message struct {
			Model string           `json:"model"`
			Usage *transcriptUsage `json:"usage"`
		} `json:"message"`
	}
	if err := json.Unmarshal([]byte(line), &entry); err != nil || entry.Message.Usage == nil {
		return providerUsageRecord{}, false
	}
	u := entry.Message.Usage
	tokens := u.InputTokens + u.CacheReadInputTokens + u.CacheCreationInputTokens
	if tokens <= 0 {
		return providerUsageRecord{}, false
	}
	return providerUsageRecord{tokens: tokens, model: entry.Message.Model}, true
}

// codexTokenCountRecord parses one Codex event_msg token_count line into a
// provider usage record. Only last_token_usage is used: total_token_usage is
// the session-cumulative counter, not the live context size, and input_tokens
// already includes cached_input_tokens. Rate-limit-only refreshes carry a null
// info and report false, so the previous qualifying record stays the newest.
func codexTokenCountRecord(line string) (providerUsageRecord, bool) {
	var entry codexTokenCountEntry
	if err := json.Unmarshal([]byte(line), &entry); err != nil {
		return providerUsageRecord{}, false
	}
	if entry.Type != "event_msg" || entry.Payload.Type != "token_count" || entry.Payload.Info == nil {
		return providerUsageRecord{}, false
	}
	tokens := entry.Payload.Info.LastTokenUsage.InputTokens
	if tokens <= 0 {
		return providerUsageRecord{}, false
	}
	record := providerUsageRecord{tokens: tokens}
	if w := entry.Payload.Info.ModelContextWindow; w != nil && *w > 0 {
		record.window = *w
	}
	return record, true
}

// codexTurnContextModel returns the model a Codex turn_context line announces.
func codexTurnContextModel(line string) (string, bool) {
	var entry codexTurnContextEntry
	if err := json.Unmarshal([]byte(line), &entry); err != nil || entry.Type != "turn_context" {
		return "", false
	}
	return entry.Payload.Model, true
}

// contextWindowTokensWithOverride resolves the session's context window. The
// explicit GC_CONTEXT_WINDOW_TOKENS env pin wins first; then the
// provider-reported liveWindow (Codex model_context_window), which is
// authoritative for that session; then the advisory policy's configuredWindow;
// then the MAX window of any model the session ran (they share one context), so
// a smaller-window sidecar or compaction call (e.g. a 200k-window Haiku entry
// inside a 1M Fable session) can't flip the session to the 200k default and
// fire the urgent tier at ~20% of real usage. Per-model windows come from the
// shared modelwindow package so this agrees with the API/session-log path; an
// unrecognized model (window 0) floors to the conservative default.
//
// The live window must beat both a model-table guess and a configured window:
// the observed gpt-6-astra rollout reports 258400 where the table would floor
// an unknown ID to 200000 (firing urgent at ~70% of real usage), and a
// configured window copied from a 1M Claude agent must not mask a 258k Codex
// session. An operator who truly needs to pin the Codex window sets
// GC_CONTEXT_WINDOW_TOKENS.
func contextWindowTokensWithOverride(models []string, liveWindow, configuredWindow int) int {
	if v := strings.TrimSpace(os.Getenv("GC_CONTEXT_WINDOW_TOKENS")); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return n
		}
	}
	if liveWindow > 0 {
		return liveWindow
	}
	if configuredWindow > 0 {
		return configuredWindow
	}
	best := 0
	for _, m := range models {
		if w := modelwindow.Window(m); w > best {
			best = w
		}
	}
	if best == 0 {
		return modelwindow.Default
	}
	return best
}

func contextUsageMessageForPolicy(tokens, window int, policy config.ContextAdvisoryPolicy) string {
	if window <= 0 || !policy.Enabled {
		return ""
	}
	policy = contextUsagePolicyWithEnvThresholdOverrides(policy)
	pct := 100 * float64(tokens) / float64(window)
	tier, ok := policy.SelectTier(pct)
	if !ok {
		return ""
	}
	return config.RenderTier(tier, config.ContextAdvisoryView{
		Tokens: tokens, Window: window, UsedK: contextUsageK(tokens), WindowK: contextUsageK(window), Pct: pct, Threshold: tier.Threshold,
	}) + "\n"
}

func contextUsageK(tokens int) string { return fmt.Sprintf("%dk", (tokens+500)/1000) }

func contextUsagePolicyWithEnvThresholdOverrides(policy config.ContextAdvisoryPolicy) config.ContextAdvisoryPolicy {
	if len(policy.Tiers) > 0 {
		policy.Tiers[0].Threshold = thresholdPct("GC_CONTEXT_ADVISORY_PCT", policy.Tiers[0].Threshold)
	}
	if len(policy.Tiers) > 1 {
		policy.Tiers[1].Threshold = thresholdPct("GC_CONTEXT_URGENT_PCT", policy.Tiers[1].Threshold)
	}
	return policy
}

func thresholdPct(env string, def int) int {
	if v := strings.TrimSpace(os.Getenv(env)); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 && n <= 100 {
			return n
		}
	}
	return def
}

// readHookStdin returns the provider hook input JSON from stdin when stdin is
// a pipe (the hook invocation shape). Interactive/manual invocations (stdin is
// a terminal) return nil so the command never blocks waiting for input.
func readHookStdin() []byte {
	st, err := os.Stdin.Stat()
	if err != nil || st.Mode()&os.ModeCharDevice != 0 {
		return nil
	}
	data, err := io.ReadAll(io.LimitReader(os.Stdin, 1<<20))
	if err != nil {
		return nil
	}
	return data
}
