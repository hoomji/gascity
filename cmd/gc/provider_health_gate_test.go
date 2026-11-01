package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// writeHealthCache writes a minimal provider-health.json to dir.
func writeHealthCache(t *testing.T, dir, provider, status string, probedAt float64) {
	t.Helper()
	cacheDir := filepath.Join(dir, ".gc", "cache")
	if err := os.MkdirAll(cacheDir, 0o755); err != nil {
		t.Fatalf("mkdirAll: %v", err)
	}
	rec := map[string]any{"provider": provider, "status": status, "probed_at": probedAt}
	data, _ := json.Marshal(map[string]any{"providers": []any{rec}})
	if err := os.WriteFile(filepath.Join(cacheDir, "provider-health.json"), data, 0o644); err != nil {
		t.Fatalf("write cache: %v", err)
	}
}

func nowSecs() float64 { return float64(time.Now().UnixNano()) / 1e9 }

// --- providerHealthSnapshot tests ---

func TestSnapshotCheck_HealthyProvider(t *testing.T) {
	dir := t.TempDir()
	writeHealthCache(t, dir, "zai", "healthy", nowSecs())
	snap := loadProviderHealthSnapshot(dir)
	healthy, present := snap.check("zai")
	if !present {
		t.Fatal("expected registryPresent=true")
	}
	if !healthy {
		t.Fatal("expected healthy=true")
	}
}

func TestSnapshotCheck_UnhealthyProvider(t *testing.T) {
	dir := t.TempDir()
	writeHealthCache(t, dir, "zai", "unhealthy", nowSecs())
	snap := loadProviderHealthSnapshot(dir)
	healthy, present := snap.check("zai")
	if !present {
		t.Fatal("expected registryPresent=true")
	}
	if healthy {
		t.Fatal("expected healthy=false")
	}
}

func TestSnapshotCheck_RegistryAbsent(t *testing.T) {
	dir := t.TempDir() // no file
	snap := loadProviderHealthSnapshot(dir)
	healthy, present := snap.check("zai")
	if present {
		t.Fatal("expected registryPresent=false when file absent")
	}
	if !healthy {
		// fail-open: missing registry → treat as green
		t.Fatal("expected healthy=true (fail-open) when registry absent")
	}
}

func TestSnapshotCheck_StaleEntry(t *testing.T) {
	dir := t.TempDir()
	staleAt := nowSecs() - (providerHealthTTL.Seconds() + 10)
	writeHealthCache(t, dir, "zai", "unhealthy", staleAt)
	snap := loadProviderHealthSnapshot(dir)
	healthy, present := snap.check("zai")
	if present {
		t.Fatal("expected registryPresent=false for stale entry")
	}
	if !healthy {
		t.Fatal("expected healthy=true (fail-open) for stale entry")
	}
}

func TestSnapshotCheck_UnknownProvider(t *testing.T) {
	dir := t.TempDir()
	writeHealthCache(t, dir, "anthropic", "healthy", nowSecs())
	snap := loadProviderHealthSnapshot(dir)
	healthy, present := snap.check("zai") // different provider
	if present {
		t.Fatal("expected registryPresent=false for unknown provider")
	}
	if !healthy {
		t.Fatal("expected healthy=true (fail-open) for unknown provider")
	}
}

func TestSnapshotHealthyProviders(t *testing.T) {
	dir := t.TempDir()
	// Write two providers: one healthy, one not.
	cacheDir := filepath.Join(dir, ".gc", "cache")
	if err := os.MkdirAll(cacheDir, 0o755); err != nil {
		t.Fatalf("mkdirAll: %v", err)
	}
	data, _ := json.Marshal(map[string]any{"providers": []any{
		map[string]any{"provider": "zai", "status": "healthy", "probed_at": nowSecs()},
		map[string]any{"provider": "anthropic", "status": "unhealthy", "probed_at": nowSecs()},
	}})
	if err := os.WriteFile(filepath.Join(cacheDir, "provider-health.json"), data, 0o644); err != nil {
		t.Fatalf("write: %v", err)
	}
	snap := loadProviderHealthSnapshot(dir)
	healthy := snap.healthyProviders()
	if len(healthy) != 1 || healthy[0] != "zai" {
		t.Fatalf("expected [zai], got %v", healthy)
	}
}

// --- providerHealthGate episode-state tests ---

func TestGate_NoRespawnWhileRed(t *testing.T) {
	dir := t.TempDir()
	writeHealthCache(t, dir, "zai", "unhealthy", nowSecs())
	gate := newProviderHealthGate()

	snap := loadProviderHealthSnapshot(dir)
	healthy, present := snap.check("zai")
	if !present || healthy {
		t.Fatalf("precondition: expected red present, got healthy=%v present=%v", healthy, present)
	}

	alerts := 0
	gate.recordRedSkip("zai", time.Now(), func(_, _ string, _ time.Time, _ int) { alerts++ })
	if alerts != 1 {
		t.Fatalf("expected 1 alert on first red skip, got %d", alerts)
	}
	// Second skip in same episode: no additional alert.
	gate.recordRedSkip("zai", time.Now(), func(_, _ string, _ time.Time, _ int) { alerts++ })
	if alerts != 1 {
		t.Fatalf("expected alert to fire exactly once per episode, got %d", alerts)
	}
}

func TestGate_ExactlyOneAlertPerEpisode(t *testing.T) {
	gate := newProviderHealthGate()
	now := time.Now()
	alerts := 0
	emit := func(_, _ string, _ time.Time, _ int) { alerts++ }

	// Ten red skips in episode 1.
	for i := 0; i < 10; i++ {
		gate.recordRedSkip("zai", now, emit)
	}
	if alerts != 1 {
		t.Fatalf("episode 1: expected 1 alert, got %d", alerts)
	}

	// Provider returns green → episode closes.
	gate.recordGreenTick("zai")

	// Provider goes red again → new episode → new alert.
	for i := 0; i < 5; i++ {
		gate.recordRedSkip("zai", now, emit)
	}
	if alerts != 2 {
		t.Fatalf("episode 2: expected 2 total alerts, got %d", alerts)
	}
}

func TestGate_RespawnResumesOnGreen(t *testing.T) {
	gate := newProviderHealthGate()
	now := time.Now()
	emit := func(_, _ string, _ time.Time, _ int) {}

	gate.recordRedSkip("zai", now, emit)
	// After green, episode state clears.
	gate.recordGreenTick("zai")
	gate.mu.Lock()
	s := gate.episodes["zai"]
	gate.mu.Unlock()
	if s.Status != providerStatusGreen {
		t.Fatal("expected green status after recordGreenTick")
	}
	if s.AlertSent {
		t.Fatal("expected AlertSent to be cleared after green tick")
	}
}

func TestGate_WakeBudgetNotConsumedOnRedSkip(t *testing.T) {
	// Verify that the gate produces a "continue" path — tested here by
	// checking that the SessionCount increments (i.e., the skip was
	// recorded) without emitting a second alert.
	gate := newProviderHealthGate()
	now := time.Now()
	alerts := 0
	emit := func(_, _ string, _ time.Time, count int) { alerts++; _ = count }

	gate.recordRedSkip("zai", now, emit)
	gate.recordRedSkip("zai", now, emit)
	gate.recordRedSkip("zai", now, emit)

	gate.mu.Lock()
	s := gate.episodes["zai"]
	gate.mu.Unlock()

	if s.SessionCount != 3 {
		t.Fatalf("expected SessionCount=3, got %d", s.SessionCount)
	}
	if alerts != 1 {
		t.Fatalf("expected 1 alert (not one per skip), got %d", alerts)
	}
}

func TestGate_FailOpenRegistryUnavailable(t *testing.T) {
	dir := t.TempDir() // no file
	snap := loadProviderHealthSnapshot(dir)
	healthy, present := snap.check("zai")
	if present {
		t.Fatal("expected registryPresent=false")
	}
	// Caller should proceed as green (fail-open); no gate interaction needed.
	if !healthy {
		t.Fatal("expected healthy=true (fail-open) when registry absent")
	}
}

func TestGate_NoChangeBehaviorForGreenProvider(t *testing.T) {
	dir := t.TempDir()
	writeHealthCache(t, dir, "zai", "healthy", nowSecs())
	gate := newProviderHealthGate()
	snap := loadProviderHealthSnapshot(dir)

	healthy, present := snap.check("zai")
	if !present || !healthy {
		t.Fatalf("precondition: expected healthy present, got healthy=%v present=%v", healthy, present)
	}
	// A green provider records a green tick; no alert.
	gate.recordGreenTick("zai")
	gate.mu.Lock()
	_, hasEpisode := gate.episodes["zai"]
	gate.mu.Unlock()
	// recordGreenTick on unknown provider is a no-op (no entry created).
	if hasEpisode {
		t.Fatal("recordGreenTick on a never-red provider should not create episode state")
	}
}

func TestGate_Concurrent(t *testing.T) {
	gate := newProviderHealthGate()
	now := time.Now()
	var mu sync.Mutex
	alerts := 0
	emit := func(_, _ string, _ time.Time, _ int) {
		mu.Lock()
		alerts++
		mu.Unlock()
	}

	var wg sync.WaitGroup
	for i := 0; i < 50; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			gate.recordRedSkip("zai", now, emit)
		}()
	}
	wg.Wait()

	if alerts != 1 {
		t.Fatalf("concurrent: expected exactly 1 alert, got %d", alerts)
	}
}

func TestEmitProviderHealthGateAlert_Format(t *testing.T) {
	var captured string
	w := &capWriter{fn: func(b []byte) { captured += string(b) }}
	redSince := time.Date(2026, 6, 2, 12, 0, 0, 0, time.UTC)

	emitProviderHealthGateAlert(nil, w, "zai", "ep-123", redSince, 3)

	for _, want := range []string{"zai", "ep-123", "2026-06-02T12:00:00Z", "sessions_parked=3"} {
		if !strings.Contains(captured, want) {
			t.Errorf("alert message missing %q\ngot: %s", want, captured)
		}
	}
}

type capWriter struct{ fn func([]byte) }

func (c *capWriter) Write(b []byte) (int, error) { c.fn(b); return len(b), nil }

// writeHealthRecords writes provider-health.json with the given raw records.
func writeHealthRecords(t *testing.T, dir string, recs ...map[string]any) {
	t.Helper()
	cacheDir := filepath.Join(dir, ".gc", "cache")
	if err := os.MkdirAll(cacheDir, 0o755); err != nil {
		t.Fatalf("mkdirAll: %v", err)
	}
	data, _ := json.Marshal(map[string]any{"providers": recs})
	if err := os.WriteFile(filepath.Join(cacheDir, "provider-health.json"), data, 0o644); err != nil {
		t.Fatalf("write cache: %v", err)
	}
}

// Kills: ttl_s ignored (a long-lived probe goes stale at 60 s, a short one
// outlives its window), until ignored (a spent quota with a known reset
// fails open once the probe ages), until honoured past its own time or on a
// healthy record, and the old format changing behaviour. Each row is one
// record for "zai" at the given age.
func TestSnapshotCheck_HealthRecordFreshness(t *testing.T) {
	ttl := providerHealthTTL.Seconds()
	for _, tc := range []struct {
		name        string
		status      string
		ageSecs     float64
		extra       map[string]any
		wantPresent bool
		wantHealthy bool
	}{
		{name: "old format, fresh red", status: "unhealthy", ageSecs: ttl - 5, wantPresent: true, wantHealthy: false},
		{name: "old format, fresh green", status: "healthy", ageSecs: ttl - 5, wantPresent: true, wantHealthy: true},
		{name: "old format, stale red fails open", status: "unhealthy", ageSecs: ttl + 5, wantPresent: false, wantHealthy: true},
		{name: "ttl_s extends freshness", status: "unhealthy", ageSecs: 300, extra: map[string]any{"ttl_s": 600}, wantPresent: true, wantHealthy: false},
		{name: "ttl_s extends a green", status: "healthy", ageSecs: 300, extra: map[string]any{"ttl_s": 600}, wantPresent: true, wantHealthy: true},
		{name: "ttl_s shortens freshness", status: "unhealthy", ageSecs: 30, extra: map[string]any{"ttl_s": 10}, wantPresent: false, wantHealthy: true},
		{name: "ttl_s past its window", status: "unhealthy", ageSecs: 700, extra: map[string]any{"ttl_s": 600}, wantPresent: false, wantHealthy: true},
		{name: "until holds red past the TTL", status: "unhealthy", ageSecs: 3000, extra: map[string]any{"until": nowSecs() + 3600}, wantPresent: true, wantHealthy: false},
		{name: "until releases after its time", status: "unhealthy", ageSecs: 3000, extra: map[string]any{"until": nowSecs() - 10}, wantPresent: false, wantHealthy: true},
		{name: "past until falls back to the TTL rule", status: "unhealthy", ageSecs: 5, extra: map[string]any{"until": nowSecs() - 10}, wantPresent: true, wantHealthy: false},
		{name: "until on a healthy record is ignored", status: "healthy", ageSecs: 3000, extra: map[string]any{"until": nowSecs() + 3600}, wantPresent: false, wantHealthy: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			rec := map[string]any{"provider": "zai", "status": tc.status, "probed_at": nowSecs() - tc.ageSecs}
			for k, v := range tc.extra {
				rec[k] = v
			}
			writeHealthRecords(t, dir, rec)
			healthy, present := loadProviderHealthSnapshot(dir).check("zai")
			if present != tc.wantPresent || healthy != tc.wantHealthy {
				t.Fatalf("check(zai) = (healthy %v, present %v), want (%v, %v)", healthy, present, tc.wantHealthy, tc.wantPresent)
			}
		})
	}
}

// Kills: the record's reason dropped from the snapshot, or kept for a
// record that is no longer live.
func TestSnapshotReason(t *testing.T) {
	dir := t.TempDir()
	writeHealthRecords(t, dir,
		map[string]any{"provider": "claude", "status": "unhealthy", "probed_at": nowSecs() - 3000, "until": nowSecs() + 600, "reason": "weekly quota spent"},
		map[string]any{"provider": "codex", "status": "unhealthy", "probed_at": nowSecs() - 3000, "reason": "stale"},
	)
	snap := loadProviderHealthSnapshot(dir)
	if got := snap.reason("claude"); got != "weekly quota spent" {
		t.Fatalf("reason(claude) = %q, want the file's reason", got)
	}
	if got := snap.reason("codex"); got != "" {
		t.Fatalf("reason(codex) = %q, want empty for a stale record", got)
	}
}

// Kills: the gate alert dropping the health file's reason.
func TestEmitProviderHealthGateAlert_CarriesReason(t *testing.T) {
	var w strings.Builder
	rec := &capturingRecorder{}
	emitProviderHealthGateAlertWithReason(rec, &w, "claude", "ep-1", "weekly quota spent", time.Now(), 2)
	if !strings.Contains(w.String(), "Reason: weekly quota spent") {
		t.Fatalf("alert %q does not carry the reason", w.String())
	}
	if len(rec.events) != 1 || !strings.Contains(string(rec.events[0].Payload), `"reason":"weekly quota spent"`) {
		t.Fatalf("events %+v, want one alert whose payload carries the reason", rec.events)
	}
}
