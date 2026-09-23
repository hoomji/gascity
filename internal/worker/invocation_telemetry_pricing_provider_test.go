package worker

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/gastownhall/gascity/internal/beads"
	"github.com/gastownhall/gascity/internal/pricing"
	"github.com/gastownhall/gascity/internal/runtime"
	sessionpkg "github.com/gastownhall/gascity/internal/session"
	"github.com/gastownhall/gascity/internal/sessionlog"
	"github.com/gastownhall/gascity/internal/usage"
)

// newAliasPricingHandle builds a started claude-family handle whose configured
// provider is a non-family alias (the shape city.toml providers and [[pricing]]
// entries use, e.g. claude-mayor) and wires an explicit pricing registry.
// It returns the handle, the transcript path to append usage to, and the sink
// path facts land in.
func newAliasPricingHandle(t *testing.T, provider string, reg *pricing.Registry) (handle *SessionHandle, transcriptPath, sinkPath string) {
	t.Helper()
	searchBase := t.TempDir()
	workDir := t.TempDir()
	sinkPath = filepath.Join(t.TempDir(), "usage.jsonl")

	store := beads.NewMemStore()
	sp := runtime.NewFake()
	manager := sessionpkg.NewManagerWithOptions(store, sp)
	h, err := NewSessionHandle(SessionHandleConfig{
		Manager:     manager,
		SearchPaths: []string{searchBase},
		UsageSink:   usage.NewLocalSink(sinkPath),
		Pricing:     reg,
		Session: SessionSpec{
			Profile:  ProfileClaudeTmuxCLI,
			Template: "probe",
			Title:    "Probe",
			Command:  "claude",
			WorkDir:  workDir,
			Provider: provider,
			Metadata: map[string]string{"agent_name": "myrig/mayor"},
		},
	})
	if err != nil {
		t.Fatalf("NewSessionHandle: %v", err)
	}
	if err := h.Start(context.Background()); err != nil {
		t.Fatalf("Start: %v", err)
	}
	info, err := manager.Get(h.sessionID)
	if err != nil {
		t.Fatalf("Get(%q): %v", h.sessionID, err)
	}
	slugDir := filepath.Join(searchBase, sessionlog.ProjectSlug(workDir))
	if err := os.MkdirAll(slugDir, 0o755); err != nil {
		t.Fatalf("MkdirAll(%q): %v", slugDir, err)
	}
	return h, filepath.Join(slugDir, info.SessionKey+".jsonl"), sinkPath
}

// TestMessagePricesConfiguredProviderAliasFromRegistry is the regression for
// "[[pricing]] in city.toml is parsed but never honored": the operator keys a
// city rate card to the configured provider alias (claude-mayor), but the emit
// path looked the card up under the normalized transcript family (claude), so
// the entry never matched and every row stayed unpriced. The lookup must try
// the session's configured provider identity first, then fall back to the
// family, and the emitted row must name the provider whose rate priced it.
func TestMessagePricesConfiguredProviderAliasFromRegistry(t *testing.T) {
	// claude-opus-5 is intentionally absent from DefaultPricings, so only the
	// alias-keyed city entry can price this fact.
	registry := pricing.BuildRegistry(nil, []pricing.ModelPricing{{
		Provider:     "claude-mayor",
		Model:        "claude-opus-5",
		LastVerified: "2026-09-13",
		Tier: pricing.Tier{
			PromptUSDPer1M:        5,
			CompletionUSDPer1M:    25,
			CacheReadUSDPer1M:     0.5,
			CacheCreationUSDPer1M: 6.25,
		},
	}})
	handle, transcriptPath, sinkPath := newAliasPricingHandle(t, "claude-mayor", registry)

	const (
		in, out, cacheRead, cacheCreate = 2, 624, 105120, 335
	)
	writeWorkerTestJSONL(t, transcriptPath, []map[string]any{
		usageEntry("u1", "claude-opus-5", in, out, cacheRead, cacheCreate),
	})

	if _, err := handle.Message(context.Background(), MessageRequest{Text: "hello"}); err != nil {
		t.Fatalf("Message: %v", err)
	}

	facts, warnings, err := usage.ReadFacts(sinkPath)
	if err != nil {
		t.Fatalf("ReadFacts: %v", err)
	}
	if len(warnings) != 0 {
		t.Fatalf("unexpected sink warnings: %v", warnings)
	}
	if len(facts) != 1 {
		t.Fatalf("want exactly 1 model fact, got %d: %+v", len(facts), facts)
	}
	f := facts[0]
	if f.Provider != "claude-mayor" {
		t.Fatalf("Provider = %q, want the configured provider alias claude-mayor", f.Provider)
	}
	if f.Unpriced {
		t.Fatalf("alias-keyed [[pricing]] entry must price the row: %+v", f)
	}
	wantCost, ok := registry.Estimate("claude-mayor", "claude-opus-5", pricing.Usage{
		PromptTokens: in, CompletionTokens: out, CacheReadTokens: cacheRead, CacheCreationTokens: cacheCreate,
	})
	if !ok {
		t.Fatal("alias-keyed registry has no claude-opus-5 entry; fix the fixture")
	}
	if f.CostUSDEstimate != wantCost {
		t.Fatalf("CostUSDEstimate = %v, want %v", f.CostUSDEstimate, wantCost)
	}
}

// TestMessageFallsBackToFamilyPricingForUncardedAlias proves the second half of
// the lookup ladder: when no [[pricing]] entry names the configured alias, a
// session on an alias of a priced family still resolves the shipped/ family-
// keyed rate instead of being declared unpriced. The emitted row names the
// family rate card that actually priced it.
func TestMessageFallsBackToFamilyPricingForUncardedAlias(t *testing.T) {
	// No city layer: only DefaultPricings (keyed by the family "claude") has a
	// rate for claude-opus-4-7.
	registry := pricing.BuildRegistry(nil, nil)
	handle, transcriptPath, sinkPath := newAliasPricingHandle(t, "claude-eco", registry)

	writeWorkerTestJSONL(t, transcriptPath, []map[string]any{
		usageEntry("u1", "claude-opus-4-7", 100, 50, 2000, 800),
	})

	if _, err := handle.Message(context.Background(), MessageRequest{Text: "hello"}); err != nil {
		t.Fatalf("Message: %v", err)
	}

	facts, _, err := usage.ReadFacts(sinkPath)
	if err != nil {
		t.Fatalf("ReadFacts: %v", err)
	}
	if len(facts) != 1 {
		t.Fatalf("want exactly 1 model fact, got %d: %+v", len(facts), facts)
	}
	f := facts[0]
	if f.Unpriced {
		t.Fatalf("alias of a default-priced family must still price from the family entry: %+v", f)
	}
	if f.Provider != "claude" {
		t.Fatalf("Provider = %q, want the family rate-card key claude", f.Provider)
	}
	wantCost, ok := registry.Estimate("claude", "claude-opus-4-7", pricing.Usage{
		PromptTokens: 100, CompletionTokens: 50, CacheReadTokens: 2000, CacheCreationTokens: 800,
	})
	if !ok {
		t.Fatal("default registry has no claude-opus-4-7 entry; fix the fixture")
	}
	if f.CostUSDEstimate != wantCost {
		t.Fatalf("CostUSDEstimate = %v, want %v", f.CostUSDEstimate, wantCost)
	}
}

// TestSweepPricesConfiguredProviderAliasFromRegistry pins that the
// controller-tick sweep — the path that recovers a pool agent's trailing
// invocations after its last prompt op — resolves the same alias-keyed
// [[pricing]] card as the prompt-op seam. The mayor and other self-driven pool
// agents are recorded almost entirely through this path, so a fix that only
// touched the prompt-op seam would leave their rows unpriced.
func TestSweepPricesConfiguredProviderAliasFromRegistry(t *testing.T) {
	searchBase := t.TempDir()
	workDir := t.TempDir()
	sinkPath := filepath.Join(t.TempDir(), "usage.jsonl")

	registry := pricing.BuildRegistry(nil, []pricing.ModelPricing{{
		Provider:     "claude-mayor",
		Model:        "claude-fable-5-1",
		LastVerified: "2026-09-13",
		Tier: pricing.Tier{
			PromptUSDPer1M:        10,
			CompletionUSDPer1M:    50,
			CacheReadUSDPer1M:     0.25,
			CacheCreationUSDPer1M: 12.5,
		},
	}})

	store := beads.NewMemStore()
	sp := runtime.NewFake()
	factory, err := NewFactory(FactoryConfig{
		Store:       store,
		Provider:    sp,
		SearchPaths: []string{searchBase},
		UsageSink:   usage.NewLocalSink(sinkPath),
		Pricing:     registry,
	})
	if err != nil {
		t.Fatalf("NewFactory: %v", err)
	}
	h, err := factory.Session(SessionSpec{
		Profile:  ProfileClaudeTmuxCLI,
		Template: "probe",
		Title:    "Probe",
		Command:  "claude",
		WorkDir:  workDir,
		Provider: "claude-mayor",
		Metadata: map[string]string{"agent_name": "myrig/mayor"},
	})
	if err != nil {
		t.Fatalf("Session: %v", err)
	}
	if err := h.Start(context.Background()); err != nil {
		t.Fatalf("Start: %v", err)
	}
	id := h.sessionID

	info, err := h.manager.Get(id)
	if err != nil {
		t.Fatalf("Get(%q): %v", id, err)
	}
	slugDir := filepath.Join(searchBase, sessionlog.ProjectSlug(workDir))
	if err := os.MkdirAll(slugDir, 0o755); err != nil {
		t.Fatalf("MkdirAll(%q): %v", slugDir, err)
	}
	transcriptPath := filepath.Join(slugDir, info.SessionKey+".jsonl")
	writeWorkerTestJSONL(t, transcriptPath, []map[string]any{
		usageEntry("u1", "claude-fable-5-1", 2, 624, 105120, 335),
	})
	b, err := store.Get(id)
	if err != nil {
		t.Fatal(err)
	}

	emitted, settled, err := factory.SweepSessionModelUsage(context.Background(), id, b.Metadata, time.Unix(1, 0).UTC())
	if err != nil {
		t.Fatalf("SweepSessionModelUsage: %v", err)
	}
	if !settled || emitted != 1 {
		t.Fatalf("sweep emitted=%d settled=%t, want 1/true", emitted, settled)
	}

	facts, warnings, err := usage.ReadFacts(sinkPath)
	if err != nil {
		t.Fatalf("ReadFacts: %v", err)
	}
	if len(warnings) != 0 {
		t.Fatalf("unexpected sink warnings: %v", warnings)
	}
	if len(facts) != 1 {
		t.Fatalf("want exactly 1 model fact, got %d: %+v", len(facts), facts)
	}
	f := facts[0]
	if f.Provider != "claude-mayor" {
		t.Fatalf("Provider = %q, want the configured provider alias claude-mayor", f.Provider)
	}
	if f.Unpriced {
		t.Fatalf("alias-keyed [[pricing]] entry must price the swept row: %+v", f)
	}
	wantCost, ok := registry.Estimate("claude-mayor", "claude-fable-5-1", pricing.Usage{
		PromptTokens: 2, CompletionTokens: 624, CacheReadTokens: 105120, CacheCreationTokens: 335,
	})
	if !ok {
		t.Fatal("alias-keyed registry has no claude-fable-5-1 entry; fix the fixture")
	}
	if f.CostUSDEstimate != wantCost {
		t.Fatalf("CostUSDEstimate = %v, want %v", f.CostUSDEstimate, wantCost)
	}
}

// TestPricingProviderIdentities pins the lookup ladder: the configured provider
// alias is tried first (it is what an operator writes in [[pricing]]), then the
// family rungs, with blanks and duplicates dropped in order. A regression here
// would silently re-introduce the family-only key that never matched alias-
// keyed cards.
func TestPricingProviderIdentities(t *testing.T) {
	tests := []struct {
		name                                    string
		provider, providerKind, builtinAncestor string
		family                                  string
		want                                    []string
	}{
		{
			name:     "configured alias first, family fallback deduped",
			provider: "claude-mayor", providerKind: "claude", builtinAncestor: "claude", family: "claude",
			want: []string{"claude-mayor", "claude"},
		},
		{
			name:   "family only when no alias rungs are set",
			family: "claude",
			want:   []string{"claude"},
		},
		{
			name:         "kind rung kept when provider is empty",
			providerKind: "codex", builtinAncestor: "codex", family: "codex",
			want: []string{"codex"},
		},
		{
			name: "all blank yields no identities",
			want: nil,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := pricingProviderIdentities(tc.provider, tc.providerKind, tc.builtinAncestor, tc.family)
			if len(got) != len(tc.want) {
				t.Fatalf("identities = %v, want %v", got, tc.want)
			}
			for i := range got {
				if got[i] != tc.want[i] {
					t.Fatalf("identities = %v, want %v", got, tc.want)
				}
			}
		})
	}
}
