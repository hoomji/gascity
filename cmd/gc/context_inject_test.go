package main

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gastownhall/gascity/internal/config"
)

func writeTranscript(t *testing.T, lines ...string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "transcript.jsonl")
	if err := os.WriteFile(p, []byte(strings.Join(lines, "\n")+"\n"), 0o600); err != nil {
		t.Fatalf("write transcript: %v", err)
	}
	return p
}

func usageLine(model string, input, cacheRead, cacheCreate int) string {
	return fmt.Sprintf(
		`{"type":"assistant","message":{"model":%q,"usage":{"input_tokens":%d,"cache_read_input_tokens":%d,"cache_creation_input_tokens":%d}}}`,
		model, input, cacheRead, cacheCreate)
}

func hookInputFor(path string) []byte {
	return []byte(fmt.Sprintf(`{"transcript_path":%q,"hook_event_name":"UserPromptSubmit"}`, path))
}

// codexTokenCountLine renders the event_msg token_count shape a real Codex
// rollout writes after each API call. last_token_usage.input_tokens is the live
// context occupancy and model_context_window is the live window; the cumulative
// total_token_usage is deliberately much larger so a reader that mistakenly
// used it would be obvious.
func codexTokenCountLine(lastInput, lastCached, window int) string {
	cumulative := lastInput * 50
	return fmt.Sprintf(
		`{"timestamp":"2026-09-21T01:41:10.718Z","type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":%d,"cached_input_tokens":%d,"cache_write_input_tokens":0,"output_tokens":23738,"total_tokens":%d},"last_token_usage":{"input_tokens":%d,"cached_input_tokens":%d,"cache_write_input_tokens":0,"output_tokens":103,"total_tokens":%d},"model_context_window":%d}}}`,
		cumulative, lastCached, cumulative+23738, lastInput, lastCached, lastInput+103, window)
}

// codexRateLimitOnlyLine is a token_count refresh with null info, which Codex
// emits for rate-limit-only updates. It carries no occupancy.
func codexRateLimitOnlyLine() string {
	return `{"type":"event_msg","payload":{"type":"token_count","info":null,"rate_limits":{"primary":{"used_percent":36.0}}}}`
}

// codexTokenCountNoWindowLine is a token_count entry without
// model_context_window (older rollouts), so the window must fall back to the
// turn_context model table.
func codexTokenCountNoWindowLine(lastInput, lastCached int) string {
	cumulative := lastInput * 50
	return fmt.Sprintf(
		`{"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":%d,"cached_input_tokens":%d,"total_tokens":%d},"last_token_usage":{"input_tokens":%d,"cached_input_tokens":%d,"output_tokens":103,"total_tokens":%d}}}}`,
		cumulative, lastCached, cumulative+23738, lastInput, lastCached, lastInput+103)
}

// codexTurnContextLine announces the model in effect for a Codex turn.
func codexTurnContextLine(model string) string {
	return fmt.Sprintf(`{"type":"turn_context","payload":{"model":%q}}`, model)
}

// codexTokenUsageRecordLine is the newer top-level Codex record shape. It has a
// top-level "usage" key (which the Claude path must not mistake for
// message.usage) and no model_context_window.
func codexTokenUsageRecordLine(input, cached int) string {
	return fmt.Sprintf(
		`{"type":"token_usage_record","payload":{"usage":{"input_tokens":%d,"cached_input_tokens":%d,"output_tokens":103,"total_tokens":%d}}}`,
		input, cached, input+103)
}

// writeRawTranscript writes exact bytes so tests can exercise partial,
// malformed, and truncated tail lines that the line-oriented helper cannot.
func writeRawTranscript(t *testing.T, content string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "transcript.jsonl")
	if err := os.WriteFile(p, []byte(content), 0o600); err != nil {
		t.Fatalf("write transcript: %v", err)
	}
	return p
}

func TestContextInjectSilentBelowAdvisory(t *testing.T) {
	t.Setenv("GC_INJECT_CONTEXT", "")
	// 100k of 1M = 10% — well below the 60% advisory threshold.
	p := writeTranscript(t, usageLine("claude-fable-5", 1_000, 98_000, 1_000))
	if got := contextInjectLine(hookInputFor(p)); got != "" {
		t.Errorf("below advisory should be silent, got %q", got)
	}
}

func TestContextInjectAdvisoryBand(t *testing.T) {
	t.Setenv("GC_INJECT_CONTEXT", "")
	// 700k of 1M = 70% — advisory band.
	p := writeTranscript(t, usageLine("claude-fable-5", 10_000, 680_000, 10_000))
	got := contextInjectLine(hookInputFor(p))
	if !strings.Contains(got, "700k/1000k") || !strings.Contains(got, "~70%") {
		t.Errorf("advisory line wrong: %q", got)
	}
	if !strings.Contains(got, "clean seam") || !strings.Contains(got, "reset") {
		t.Errorf("advisory must point toward a clean seam + planned reset, got %q", got)
	}
	if strings.Contains(got, "HIGH") {
		t.Errorf("advisory band must not be marked HIGH: %q", got)
	}
}

func TestContextInjectUrgentBand(t *testing.T) {
	t.Setenv("GC_INJECT_CONTEXT", "")
	// 900k of 1M = 90% — urgent band.
	p := writeTranscript(t, usageLine("claude-opus-4-8[1m]", 50_000, 800_000, 50_000))
	got := contextInjectLine(hookInputFor(p))
	if !strings.Contains(got, "HIGH") || !strings.Contains(got, "gc session reset") {
		t.Errorf("urgent line must direct to handoff + self gc session reset: %q", got)
	}
	if !strings.Contains(got, "operator") {
		t.Errorf("urgent line must preserve the operator-stay-up override: %q", got)
	}
}

func TestContextInjectLastUsageEntryWins(t *testing.T) {
	t.Setenv("GC_INJECT_CONTEXT", "")
	// Older 90% entry followed by a newer 10% one (post-compaction shape):
	// the LAST entry is the live context size, so this must be silent.
	p := writeTranscript(t,
		usageLine("claude-fable-5", 50_000, 800_000, 50_000),
		usageLine("claude-fable-5", 5_000, 90_000, 5_000),
	)
	if got := contextInjectLine(hookInputFor(p)); got != "" {
		t.Errorf("last entry (10%%) should win and be silent, got %q", got)
	}
}

func TestContextInjectDefaultWindow200k(t *testing.T) {
	t.Setenv("GC_INJECT_CONTEXT", "")
	// 150k on an unrecognized model = 75% of the conservative 200k default.
	p := writeTranscript(t, usageLine("some-other-model", 10_000, 130_000, 10_000))
	got := contextInjectLine(hookInputFor(p))
	if !strings.Contains(got, "150k/200k") || !strings.Contains(got, "~75%") {
		t.Errorf("200k default window not applied: %q", got)
	}
}

func TestContextInjectWindowOverride(t *testing.T) {
	t.Setenv("GC_INJECT_CONTEXT", "")
	t.Setenv("GC_CONTEXT_WINDOW_TOKENS", "500000")
	p := writeTranscript(t, usageLine("some-other-model", 10_000, 380_000, 10_000))
	got := contextInjectLine(hookInputFor(p))
	if !strings.Contains(got, "400k/500k") {
		t.Errorf("window override not applied: %q", got)
	}
}

func TestContextInjectThresholdOverrides(t *testing.T) {
	t.Setenv("GC_INJECT_CONTEXT", "")
	t.Setenv("GC_CONTEXT_ADVISORY_PCT", "30")
	t.Setenv("GC_CONTEXT_URGENT_PCT", "40")
	// 50% of 1M: above the overridden urgent threshold.
	p := writeTranscript(t, usageLine("claude-fable-5", 10_000, 480_000, 10_000))
	if got := contextInjectLine(hookInputFor(p)); !strings.Contains(got, "HIGH") {
		t.Errorf("threshold overrides not applied: %q", got)
	}
}

func TestContextInjectUsesPerAgentContextAdvisory(t *testing.T) {
	t.Setenv("GC_INJECT_CONTEXT", "")
	t.Setenv("GC_CONTEXT_ADVISORY_PCT", "")
	t.Setenv("GC_CONTEXT_URGENT_PCT", "")
	t.Setenv("GC_CONTEXT_WINDOW_TOKENS", "")
	p := writeTranscript(t, usageLine("claude-fable-5", 10_000, 680_000, 10_000))
	global := &config.ContextAdvisory{Tiers: []config.ContextAdvisoryTier{{Threshold: contextInjectInt(60), Message: contextInjectString("global")}}}
	agent := &config.ContextAdvisory{WindowTokens: contextInjectInt(500_000), Tiers: []config.ContextAdvisoryTier{{Threshold: contextInjectInt(80), Message: contextInjectString("agent {{.Tokens}}/{{.Window}}")}}}
	if got := contextInjectLineForAdvisory(hookInputFor(p), global, agent); got != "agent 700000/500000\n" {
		t.Errorf("per-agent advisory = %q", got)
	}
}

func contextInjectInt(value int) *int { return &value }

func contextInjectString(value string) *string { return &value }

func TestContextInjectDisabled(t *testing.T) {
	t.Setenv("GC_INJECT_CONTEXT", "0")
	p := writeTranscript(t, usageLine("claude-fable-5", 50_000, 800_000, 50_000))
	if got := contextInjectLine(hookInputFor(p)); got != "" {
		t.Errorf("disabled should be silent, got %q", got)
	}
}

func TestContextInjectFailSafeSilent(t *testing.T) {
	t.Setenv("GC_INJECT_CONTEXT", "")
	for name, input := range map[string][]byte{
		"nil stdin":          nil,
		"garbage stdin":      []byte("not json"),
		"no transcript path": []byte(`{"hook_event_name":"UserPromptSubmit"}`),
		"missing file":       hookInputFor("/nonexistent/transcript.jsonl"),
	} {
		if got := contextInjectLine(input); got != "" {
			t.Errorf("%s: want silent, got %q", name, got)
		}
	}
	// Transcript with no usage entries.
	p := writeTranscript(t, `{"type":"user","message":{"content":"hi"}}`)
	if got := contextInjectLine(hookInputFor(p)); got != "" {
		t.Errorf("no-usage transcript: want silent, got %q", got)
	}
}

// Regression: the newest usage entry lacking a model string must not flip a
// 1M session to the 200k default (would fire the urgent tier far too early).
func TestContextInjectLastNonEmptyModelWins(t *testing.T) {
	t.Setenv("GC_INJECT_CONTEXT", "")
	// First entry names the 1M model; the newest usage entry omits model.
	// 700k must read as 70% of 1M (advisory), not 350% of 200k.
	p := writeTranscript(t,
		usageLine("claude-fable-5", 10_000, 680_000, 10_000),
		`{"type":"assistant","message":{"usage":{"input_tokens":10000,"cache_read_input_tokens":680000,"cache_creation_input_tokens":10000}}}`,
	)
	got := contextInjectLine(hookInputFor(p))
	if !strings.Contains(got, "700k/1000k") {
		t.Errorf("empty-model newest entry must retain the 1M window: %q", got)
	}
	if strings.Contains(got, "HIGH") {
		t.Errorf("70%% of 1M is advisory, not urgent: %q", got)
	}
}

// Per-model windows come from the shared modelwindow table, so the injector and
// the session-log/API path report the same window for the same model ID, and a
// model added to that table is picked up here for free.
//
// Bare claude-opus-4-8 is the original regression case: a 1M-context model whose
// transcript entry carries no "[1m]" suffix, which the injector must still read
// as 1M. claude-sonnet-5 is a 1M model the shared table newly recognizes. gpt-5
// covers the second half of the change — the injector used to flatten every
// non-1M model to a blanket 200k, and now reports the family's real window.
func TestContextInjectResolvesWindowFromSharedModelTable(t *testing.T) {
	t.Setenv("GC_INJECT_CONTEXT", "")
	tests := []struct {
		model string
		// input/cacheRead/cacheCreate sum to a usage inside the advisory band
		// for that model's window, so the line renders.
		input, cacheRead, cacheCreate int
		want                          string
	}{
		{"claude-opus-4-8", 10_000, 680_000, 10_000, "700k/1000k"},
		{"claude-sonnet-5", 10_000, 680_000, 10_000, "700k/1000k"},
		{"gpt-5-20260101", 10_000, 160_000, 10_000, "180k/258k"},
	}
	for _, tt := range tests {
		t.Run(tt.model, func(t *testing.T) {
			p := writeTranscript(t, usageLine(tt.model, tt.input, tt.cacheRead, tt.cacheCreate))
			got := contextInjectLine(hookInputFor(p))
			if !strings.Contains(got, tt.want) {
				t.Errorf("%s: want window %q in line, got %q", tt.model, tt.want, got)
			}
		})
	}
}

// Sidecar/compaction call on a smaller-window model must not shrink the
// main-loop session's window: max-over-models wins. (The observed 782k/200k
// bug: a Fable session with bare-opus sidecar entries, newest entry opus.)
func TestContextInjectSidecarDoesNotShrinkWindow(t *testing.T) {
	t.Setenv("GC_INJECT_CONTEXT", "")
	// Newest entry classifies 200k but carries the live (high) token count; an
	// earlier entry is the 1M main-loop model. Window must be 1M (max), so 700k
	// reads as ~70% (advisory), not ~350% of 200k.
	p := writeTranscript(t,
		usageLine("claude-fable-5", 10_000, 680_000, 10_000),   // main loop, 1M
		usageLine("claude-haiku-4-5", 10_000, 680_000, 10_000), // 200k-classified, newest, high tokens
	)
	got := contextInjectLine(hookInputFor(p))
	if !strings.Contains(got, "700k/1000k") {
		t.Errorf("a 200k-classified newest entry must not shrink the 1M session window: %q", got)
	}
}

// TestNudgeDrainInjectEmitsAdvisoryWithoutASessionTarget is the regression for
// the no-target inject branch dropping the context advisory. A managed hook
// that carries an identity but no $GC_ALIAS/$GC_SESSION_ID returns before any
// nudge target is resolved; it must still carry the context-pressure guidance
// alongside the clock line, exactly as the resolve-failure branch does.
func TestNudgeDrainInjectEmitsAdvisoryWithoutASessionTarget(t *testing.T) {
	unmanagedInjectEnv(t)
	t.Setenv("GC_AGENT", "worker") // managed identity, but no alias/session id
	t.Setenv("GC_INJECT_CONTEXT", "")
	t.Setenv("GC_CONTEXT_ADVISORY_PCT", "")
	t.Setenv("GC_CONTEXT_URGENT_PCT", "")
	t.Setenv("GC_CONTEXT_WINDOW_TOKENS", "")

	// 700k of 1M = 70% — the advisory band.
	transcript := writeTranscript(t, usageLine("claude-fable-5", 10_000, 680_000, 10_000))
	withHookStdin(t, hookInputFor(transcript))

	var stdout, stderr bytes.Buffer
	if code := cmdNudgeDrainWithFormat(nil, true, false, "", &stdout, &stderr); code != 0 {
		t.Fatalf("cmdNudgeDrainWithFormat = %d, want 0; stderr=%q", code, stderr.String())
	}
	out := stdout.String()
	if !strings.Contains(out, "700k/1000k") || !strings.Contains(out, "~70%") {
		t.Errorf("no-target inject dropped the context advisory: %q", out)
	}
}

// withHookStdin replaces os.Stdin with a pipe holding data for the duration of
// the test, which is the shape readHookStdin requires (it ignores a terminal).
func withHookStdin(t *testing.T, data []byte) {
	t.Helper()
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatalf("os.Pipe: %v", err)
	}
	if _, err := w.Write(data); err != nil {
		t.Fatalf("write hook stdin: %v", err)
	}
	if err := w.Close(); err != nil {
		t.Fatalf("close hook stdin writer: %v", err)
	}
	orig := os.Stdin
	os.Stdin = r
	t.Cleanup(func() {
		os.Stdin = orig
		_ = r.Close()
	})
}

// clearContextInjectEnv isolates a test from ambient advisory knobs.
func clearContextInjectEnv(t *testing.T) {
	t.Helper()
	t.Setenv("GC_INJECT_CONTEXT", "")
	t.Setenv("GC_CONTEXT_ADVISORY_PCT", "")
	t.Setenv("GC_CONTEXT_URGENT_PCT", "")
	t.Setenv("GC_CONTEXT_WINDOW_TOKENS", "")
}

// The observed codex-mayor regression: a Codex rollout's event_msg token_count
// carries last_token_usage.input_tokens (which already includes
// cached_input_tokens) and a live model_context_window, while the cumulative
// total_token_usage is a much larger session counter. Reading only the last
// input against the live window must produce the advisory, not a cache/cumulative
// inflated urgent.
func TestContextInjectCodexTokenCountAdvisory(t *testing.T) {
	clearContextInjectEnv(t)
	// 180880 of the rollout's live 258400 window = ~70%: advisory.
	p := writeTranscript(t,
		codexTurnContextLine("gpt-6-astra"),
		codexTokenCountLine(180_880, 150_000, 258_400),
	)
	got := contextInjectLine(hookInputFor(p))
	if !strings.Contains(got, "181k/258k") || !strings.Contains(got, "~70%") {
		t.Fatalf("codex advisory = %q, want 181k/258k ~70%%", got)
	}
	if strings.Contains(got, "HIGH") {
		t.Errorf("codex advisory must not be marked HIGH: %q", got)
	}
}

// TestContextInjectCodexDoesNotDoubleCountCachedInput is the explicit regression
// for adding Codex's cached_input_tokens again. input 140000 is 54% of the
// 258400 window (silent); adding cached 100000 would be 93% and fire urgent.
func TestContextInjectCodexDoesNotDoubleCountCachedInput(t *testing.T) {
	clearContextInjectEnv(t)
	p := writeTranscript(t,
		codexTurnContextLine("gpt-6-astra"),
		codexTokenCountLine(140_000, 100_000, 258_400),
	)
	if got := contextInjectLine(hookInputFor(p)); got != "" {
		t.Errorf("cached input must not be added to input_tokens again; got %q", got)
	}
}

// Codex re-emits token_count after compaction; the newest record is the live
// occupancy, so an older pre-compaction high-water record must not keep the
// session pinned to the urgent tier.
func TestContextInjectCodexLastTokenCountWinsPostCompaction(t *testing.T) {
	clearContextInjectEnv(t)
	p := writeTranscript(t,
		codexTurnContextLine("gpt-6-astra"),
		codexTokenCountLine(240_000, 100_000, 258_400), // ~93% before compaction
		codexTokenCountLine(20_000, 10_000, 258_400),   // ~8% after compaction
	)
	if got := contextInjectLine(hookInputFor(p)); got != "" {
		t.Errorf("post-compaction newest record must win and be silent; got %q", got)
	}
}

// A rate-limit-only token_count refresh (null info) carries no occupancy and
// must not erase the newest qualifying record before it.
func TestContextInjectCodexSkipsRateLimitOnlyRefresh(t *testing.T) {
	clearContextInjectEnv(t)
	p := writeTranscript(t,
		codexTurnContextLine("gpt-6-astra"),
		codexTokenCountLine(180_880, 150_000, 258_400),
		codexRateLimitOnlyLine(),
	)
	got := contextInjectLine(hookInputFor(p))
	if !strings.Contains(got, "181k/258k") {
		t.Errorf("null-info refresh must keep the last qualifying record; got %q", got)
	}
}

// The newer top-level token_usage_record shape carries a "usage" key that is not
// message.usage; it must be ignored (it has no live window) and never overwrite
// the token_count occupancy before it.
func TestContextInjectCodexIgnoresTokenUsageRecordShape(t *testing.T) {
	clearContextInjectEnv(t)
	p := writeTranscript(t,
		codexTurnContextLine("gpt-6-astra"),
		codexTokenCountLine(180_880, 150_000, 258_400),
		codexTokenUsageRecordLine(999_000, 0), // newer, would read >100% if misparsed
	)
	got := contextInjectLine(hookInputFor(p))
	if !strings.Contains(got, "181k/258k") {
		t.Errorf("token_usage_record must not be misread as message.usage; got %q", got)
	}
}

// Malformed, partial, and truncated JSONL lines are tolerated silently: the
// newest parseable record before them still drives the advisory.
func TestContextInjectCodexMalformedTailTolerated(t *testing.T) {
	clearContextInjectEnv(t)
	valid := codexTurnContextLine("gpt-6-astra") + "\n" + codexTokenCountLine(180_880, 150_000, 258_400)
	tests := map[string]string{
		// A 2MiB tail read can begin mid-line; the partial first line must not
		// abort the scan.
		"partial first line":    `{"type":"event_msg","payload":{"type":"token_count","info":` + "\n" + valid,
		"malformed middle line": valid + "\n" + `{"broken":"token_count"` + "\n",
		"truncated trailing token_count": valid + "\n" +
			`{"type":"event_msg","payload":{"type":"token_count","info":{"last_token_usage":{"input_tokens":`,
	}
	for name, content := range tests {
		t.Run(name, func(t *testing.T) {
			p := writeRawTranscript(t, content)
			got := contextInjectLine(hookInputFor(p))
			if !strings.Contains(got, "181k/258k") {
				t.Errorf("%s: newest parseable record dropped; got %q", name, got)
			}
		})
	}

	// No parseable usage at all still fails safe to silent.
	p := writeRawTranscript(t, codexTurnContextLine("gpt-6-astra")+"\n"+`{"type":"event_msg","payload":{"type":"token_count"`+"\n")
	if got := contextInjectLine(hookInputFor(p)); got != "" {
		t.Errorf("usage-free codex tail must be silent; got %q", got)
	}
}

// The explicit window env pin still outranks the Codex live window.
func TestContextInjectCodexWindowEnvOverride(t *testing.T) {
	clearContextInjectEnv(t)
	t.Setenv("GC_CONTEXT_WINDOW_TOKENS", "100000")
	p := writeTranscript(t,
		codexTurnContextLine("gpt-6-astra"),
		codexTokenCountLine(70_000, 10_000, 258_400),
	)
	got := contextInjectLine(hookInputFor(p))
	if !strings.Contains(got, "70k/100k") {
		t.Errorf("env window override not applied over live window: %q", got)
	}
}

// The provider-reported live window is authoritative and must beat a configured
// window (e.g. a 1M window copied from the Claude mayor), which would silence a
// 258k Codex session at ~70% real usage.
func TestContextInjectCodexLiveWindowBeatsConfiguredWindow(t *testing.T) {
	clearContextInjectEnv(t)
	p := writeTranscript(t,
		codexTurnContextLine("gpt-6-astra"),
		codexTokenCountLine(180_880, 150_000, 258_400),
	)
	agent := &config.ContextAdvisory{WindowTokens: contextInjectInt(1_000_000)}
	got := contextInjectLineForAdvisory(hookInputFor(p), nil, agent)
	if !strings.Contains(got, "181k/258k") {
		t.Errorf("live Codex window must beat a configured 1M window; got %q", got)
	}
}

// Owner thresholds (15% advisory / 20% urgent) applied to a Codex rollout. The
// at-urgent boundary intentionally stays on the lower tier, matching the legacy
// GC_CONTEXT_ADVISORY_PCT/GC_CONTEXT_URGENT_PCT semantics preserved for Claude
// (SelectTier treats the highest tier as strictly-greater; see
// TestDefaultContextAdvisoryPreservesThresholdBoundaries).
func TestContextInjectCodexThresholdBoundaries(t *testing.T) {
	clearContextInjectEnv(t)
	global := &config.ContextAdvisory{Tiers: []config.ContextAdvisoryTier{
		{Threshold: contextInjectInt(15), Message: contextInjectString("ADVISORY {{.UsedK}}/{{.WindowK}}")},
		{Threshold: contextInjectInt(20), Message: contextInjectString("URGENT {{.UsedK}}/{{.WindowK}}")},
	}}
	tests := []struct {
		name  string
		input int
		want  string
	}{
		{"below advisory", 14_000, ""},
		{"at advisory", 15_000, "ADVISORY"},
		{"just below urgent", 19_999, "ADVISORY"},
		{"at urgent stays lower tier", 20_000, "ADVISORY"},
		{"above urgent", 20_001, "URGENT"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			p := writeTranscript(t,
				codexTurnContextLine("gpt-6-astra"),
				codexTokenCountLine(tt.input, tt.input/2, 100_000),
			)
			got := contextInjectLineForAdvisory(hookInputFor(p), global, nil)
			if tt.want == "" {
				if got != "" {
					t.Errorf("%d/100000: want silent, got %q", tt.input, got)
				}
				return
			}
			if !strings.Contains(got, tt.want) {
				t.Errorf("%d/100000: want %q tier, got %q", tt.input, tt.want, got)
			}
		})
	}
}

// When a rollout omits model_context_window, the turn_context model resolves
// through the shared model-window table (gpt-5 => 258k), so an unknown-to-the-
// injector model ID still gets its real window instead of the 200k floor.
func TestContextInjectCodexFallsBackToTurnContextWindow(t *testing.T) {
	clearContextInjectEnv(t)
	// 180600 of 258000 = ~70%: advisory.
	p := writeTranscript(t,
		codexTurnContextLine("gpt-5-20260101"),
		codexTokenCountNoWindowLine(180_600, 100_000),
	)
	got := contextInjectLine(hookInputFor(p))
	if !strings.Contains(got, "181k/258k") || !strings.Contains(got, "~70%") {
		t.Errorf("turn_context model window fallback not applied: %q", got)
	}
}

// The owner's updated Astra policy (advisory at 50% USED, orderly handoff at
// 60% USED) arrives through the same GC_CONTEXT_ADVISORY_PCT /
// GC_CONTEXT_URGENT_PCT knobs the Claude mayor uses, so this pins the exact
// boundary semantics the city.toml env pair relies on:
//
//   - pct < 50        : silent
//   - 50 <= pct <= 60 : advisory (the advisory threshold is inclusive)
//   - pct > 60        : urgent   (the higher tier is strictly greater)
//
// The strict-greater high tier is the pre-existing SelectTier contract (see
// TestDefaultContextAdvisoryPreservesThresholdBoundaries); keeping it here
// ensures the new Astra numbers do not silently change Claude's defaults.
func TestContextInjectCodexAstraPolicyEnvBoundaries(t *testing.T) {
	clearContextInjectEnv(t)
	t.Setenv("GC_CONTEXT_ADVISORY_PCT", "50")
	t.Setenv("GC_CONTEXT_URGENT_PCT", "60")
	tests := []struct {
		name  string
		input int
		want  string // "", "ADVISORY", "URGENT"
	}{
		{"below advisory", 49_999, ""},
		{"at advisory", 50_000, "ADVISORY"},
		{"between thresholds", 59_999, "ADVISORY"},
		{"at urgent stays lower tier", 60_000, "ADVISORY"},
		{"above urgent", 60_001, "URGENT"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			p := writeTranscript(t,
				codexTurnContextLine("gpt-6-astra"),
				codexTokenCountLine(tt.input, tt.input/2, 100_000),
			)
			got := contextInjectLine(hookInputFor(p))
			switch tt.want {
			case "":
				if got != "" {
					t.Errorf("%d/100000: want silent, got %q", tt.input, got)
				}
			case "ADVISORY":
				if !strings.Contains(got, "Approaching the recycle zone") {
					t.Errorf("%d/100000: want advisory tier, got %q", tt.input, got)
				}
				if strings.Contains(got, "HIGH") {
					t.Errorf("%d/100000: advisory tier must not be marked HIGH: %q", tt.input, got)
				}
			case "URGENT":
				if !strings.Contains(got, "HIGH") {
					t.Errorf("%d/100000: want urgent tier, got %q", tt.input, got)
				}
			}
		})
	}
}

// hookEventNameFromInput must trust the payload's own hook_event_name (the
// Codex output schema rejects an event mismatch) and fall back to
// GC_HOOK_EVENT_NAME only when the payload omits it.
func TestHookEventNameFromInput(t *testing.T) {
	t.Setenv("GC_HOOK_EVENT_NAME", "FallbackEvent")
	if got := hookEventNameFromInput([]byte(`{"hook_event_name":"PostToolUse"}`)); got != "PostToolUse" {
		t.Errorf("payload event = %q, want PostToolUse", got)
	}
	if got := hookEventNameFromInput([]byte(`{"transcript_path":"/tmp/x"}`)); got != "FallbackEvent" {
		t.Errorf("env fallback = %q, want FallbackEvent", got)
	}
	if got := hookEventNameFromInput([]byte(`not json`)); got != "FallbackEvent" {
		t.Errorf("malformed payload fallback = %q, want FallbackEvent", got)
	}
	t.Setenv("GC_HOOK_EVENT_NAME", "")
	if got := hookEventNameFromInput(nil); got != "" {
		t.Errorf("no evidence event = %q, want empty", got)
	}
}
