package main

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/gastownhall/gascity/internal/beads"
	"github.com/gastownhall/gascity/internal/config"
	"github.com/gastownhall/gascity/internal/doctor"
)

// seedExpiredOrderTracking returns minClosedOrderTrackingRetained+expired
// closed tracking beads for one order, all past a 24h TTL, oldest first
// (prefix-00 is the oldest).
func seedExpiredOrderTracking(prefix string, now time.Time, expired int) []beads.Bead {
	n := minClosedOrderTrackingRetained + expired
	seed := make([]beads.Bead, 0, n)
	for i := range n {
		seed = append(seed, beads.Bead{
			ID:        fmt.Sprintf("%s-%02d", prefix, i),
			Title:     "order:" + prefix,
			Status:    "closed",
			Type:      "task",
			CreatedAt: now.Add(-72*time.Hour + time.Duration(i)*time.Minute),
			Labels:    []string{"order-run:" + prefix, labelOrderTracking},
			Ephemeral: true,
		})
	}
	return seed
}

func retentionTestPolicy() orderTrackingRetentionPolicy {
	return orderTrackingRetentionPolicy{deleteAfterClose: 24 * time.Hour, retainLast: minClosedOrderTrackingRetained}
}

func TestRetentionBudgeted_CountBudgetDeletesAtMostNAndResumes(t *testing.T) {
	now := time.Date(2026, 9, 22, 12, 0, 0, 0, time.UTC)
	store := beads.NewMemStoreFrom(100, seedExpiredOrderTracking("tick", now, 12), nil)

	first, err := sweepClosedOrderTrackingRetentionAcrossStoresBudgeted(context.Background(), []beads.Store{store}, now, retentionTestPolicy(), nil, 5)
	if err != nil {
		t.Fatalf("first pass: %v", err)
	}
	if first.deleted != 5 || first.remaining != 7 {
		t.Fatalf("first pass deleted=%d remaining=%d, want 5 and 7", first.deleted, first.remaining)
	}
	// Oldest-first: the five oldest are gone, the sixth oldest survives.
	for i := range 5 {
		if _, err := store.Get(fmt.Sprintf("tick-%02d", i)); err == nil {
			t.Fatalf("tick-%02d survived; budgeted pass must delete oldest first", i)
		}
	}
	if _, err := store.Get("tick-05"); err != nil {
		t.Fatalf("tick-05 deleted beyond budget: %v", err)
	}

	second, err := sweepClosedOrderTrackingRetentionAcrossStoresBudgeted(context.Background(), []beads.Store{store}, now, retentionTestPolicy(), nil, 5)
	if err != nil {
		t.Fatalf("second pass: %v", err)
	}
	if second.deleted != 5 || second.remaining != 2 {
		t.Fatalf("second pass deleted=%d remaining=%d, want 5 and 2", second.deleted, second.remaining)
	}
	third, err := sweepClosedOrderTrackingRetentionAcrossStoresBudgeted(context.Background(), []beads.Store{store}, now, retentionTestPolicy(), nil, 5)
	if err != nil {
		t.Fatalf("third pass: %v", err)
	}
	if third.deleted != 2 || third.remaining != 0 {
		t.Fatalf("third pass deleted=%d remaining=%d, want 2 and 0", third.deleted, third.remaining)
	}
}

func TestRetentionBudgeted_CountBudgetReportsBacklogOfLaterStores(t *testing.T) {
	now := time.Date(2026, 9, 22, 12, 0, 0, 0, time.UTC)
	a := beads.NewMemStoreFrom(100, seedExpiredOrderTracking("alpha", now, 3), nil)
	b := beads.NewMemStoreFrom(100, seedExpiredOrderTracking("beta", now, 4), nil)

	res, err := sweepClosedOrderTrackingRetentionAcrossStoresBudgeted(context.Background(), []beads.Store{a, b}, now, retentionTestPolicy(), nil, 3)
	if err != nil {
		t.Fatal(err)
	}
	if res.deleted != 3 || res.remaining != 4 || res.storesSwept != 2 {
		t.Fatalf("deleted=%d remaining=%d storesSwept=%d, want 3, 4, 2", res.deleted, res.remaining, res.storesSwept)
	}
}

// cancelAfterDeletesStore cancels a context once n deletes have committed,
// modeling an exec order whose deadline lands mid-pass.
type cancelAfterDeletesStore struct {
	*beads.MemStore
	n       int
	deletes int
	cancel  context.CancelFunc
}

func (s *cancelAfterDeletesStore) Delete(id string) error {
	if err := s.MemStore.Delete(id); err != nil {
		return err
	}
	s.deletes++
	if s.deletes == s.n {
		s.cancel()
	}
	return nil
}

func TestRetentionBudgeted_CancelledMidPassKeepsCompletedDeletes(t *testing.T) {
	now := time.Date(2026, 9, 22, 12, 0, 0, 0, time.UTC)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	store := &cancelAfterDeletesStore{MemStore: beads.NewMemStoreFrom(100, seedExpiredOrderTracking("tick", now, 10), nil), n: 3, cancel: cancel}

	res, err := sweepClosedOrderTrackingRetentionAcrossStoresBudgeted(ctx, []beads.Store{store}, now, retentionTestPolicy(), nil, 0)
	if err != nil {
		t.Fatalf("canceled pass must end cleanly, got %v", err)
	}
	if res.deleted != 3 || res.remaining != 7 {
		t.Fatalf("deleted=%d remaining=%d, want 3 and 7", res.deleted, res.remaining)
	}
	for i := range 3 {
		if _, err := store.Get(fmt.Sprintf("tick-%02d", i)); err == nil {
			t.Fatalf("tick-%02d present after its delete completed", i)
		}
	}
	all, err := store.List(beads.ListQuery{Label: labelOrderTracking, IncludeClosed: true, TierMode: beads.TierBoth})
	if err != nil {
		t.Fatal(err)
	}
	if len(all) != minClosedOrderTrackingRetained+7 {
		t.Fatalf("store holds %d tracking beads, want %d", len(all), minClosedOrderTrackingRetained+7)
	}
}

// notFoundDeleteStore reports one bead as missing on delete — an orphaned row
// the list still returns (gastownhall/gascity#3926).
type notFoundDeleteStore struct {
	*beads.MemStore
	missingID string
}

func (s *notFoundDeleteStore) Delete(id string) error {
	if id == s.missingID {
		return fmt.Errorf("delete bead: %s: %w", id, beads.ErrNotFound)
	}
	return s.MemStore.Delete(id)
}

func TestRetentionBudgeted_OrphanedRowDoesNotFailThePass(t *testing.T) {
	now := time.Date(2026, 9, 22, 12, 0, 0, 0, time.UTC)
	store := &notFoundDeleteStore{MemStore: beads.NewMemStoreFrom(100, seedExpiredOrderTracking("tick", now, 4), nil), missingID: "tick-00"}

	res, err := sweepClosedOrderTrackingRetentionAcrossStoresBudgeted(context.Background(), []beads.Store{store}, now, retentionTestPolicy(), nil, 0)
	if err != nil {
		t.Fatalf("orphaned row must not fail the pass: %v", err)
	}
	if res.deleted != 3 || res.alreadyGone != 1 || res.storesSwept != 1 {
		t.Fatalf("deleted=%d alreadyGone=%d storesSwept=%d, want 3, 1, 1", res.deleted, res.alreadyGone, res.storesSwept)
	}
}

func TestOrderTrackingRetentionDeadline(t *testing.T) {
	now := time.Date(2026, 9, 22, 12, 0, 0, 0, time.UTC)
	if _, ok := orderTrackingRetentionDeadline(now, 0, ""); ok {
		t.Fatal("no budget and no order deadline must be unbounded")
	}
	if _, ok := orderTrackingRetentionDeadline(now, 0, "not-a-time"); ok {
		t.Fatal("unparseable order deadline must be unbounded")
	}
	if got, _ := orderTrackingRetentionDeadline(now, 2*time.Minute, now.Add(time.Hour).Format(time.RFC3339Nano)); !got.Equal(now.Add(2 * time.Minute)) {
		t.Fatalf("explicit budget: got %v", got)
	}
	// 300s order timeout: 20% headroom = 60s, stop at +240s.
	if got, _ := orderTrackingRetentionDeadline(now, 0, now.Add(300*time.Second).Format(time.RFC3339Nano)); !got.Equal(now.Add(240 * time.Second)) {
		t.Fatalf("300s order: got %v, want +240s", got.Sub(now))
	}
	// 30s left: 20% is 6s, so the 15s floor applies.
	if got, _ := orderTrackingRetentionDeadline(now, 0, now.Add(30*time.Second).Format(time.RFC3339Nano)); !got.Equal(now.Add(15 * time.Second)) {
		t.Fatalf("30s order: got %v, want +15s", got.Sub(now))
	}
}

func TestWithOrderExecDeadlineEnv(t *testing.T) {
	if got := withOrderExecDeadlineEnv(context.Background(), []string{"A=1"}); len(got) != 1 {
		t.Fatalf("no deadline must not add env, got %v", got)
	}
	deadline := time.Date(2026, 9, 22, 12, 5, 0, 0, time.UTC)
	ctx, cancel := context.WithDeadline(context.Background(), deadline)
	defer cancel()
	got := withOrderExecDeadlineEnv(ctx, []string{"A=1"})
	want := orderExecDeadlineEnv + "=" + deadline.Format(time.RFC3339Nano)
	if len(got) != 2 || got[1] != want {
		t.Fatalf("env = %v, want trailing %q", got, want)
	}
}

func TestOrderTrackingRetentionCheck_CountsOnlyExpiredBeads(t *testing.T) {
	now := time.Date(2026, 9, 22, 12, 0, 0, 0, time.UTC)
	// A busy healthy city: 2000 closed tracking beads, all inside a 24h TTL.
	fresh := make([]beads.Bead, 2000)
	for i := range fresh {
		fresh[i] = beads.Bead{
			ID:        fmt.Sprintf("fresh-%04d", i),
			Status:    "closed",
			Labels:    []string{labelOrderTracking},
			CreatedAt: now.Add(-time.Duration(i) * 30 * time.Second),
			UpdatedAt: now.Add(-time.Duration(i) * 30 * time.Second),
		}
	}
	cfg := &config.City{Beads: config.BeadsConfig{Policies: map[string]config.BeadPolicyConfig{
		orderTrackingBeadPolicyName: {DeleteAfterClose: "24h"},
	}}}
	store := beads.NewMemStoreFrom(3000, fresh, nil)
	check := newOrderTrackingRetentionCheck("/city", func(string) (beads.Store, error) { return store, nil }).withConfig(cfg)
	check.now = func() time.Time { return now }
	if res := check.Run(&doctor.CheckContext{}); res.Status != doctor.StatusOK {
		t.Fatalf("healthy busy city: Status = %v, want OK: %s", res.Status, res.Message)
	}

	// The same city with 600 beads past the TTL: pruning is behind.
	expired := make([]beads.Bead, 600)
	for i := range expired {
		expired[i] = beads.Bead{
			ID:        fmt.Sprintf("old-%04d", i),
			Status:    "closed",
			Labels:    []string{labelOrderTracking},
			CreatedAt: now.Add(-48 * time.Hour),
			UpdatedAt: now.Add(-48 * time.Hour),
		}
	}
	store = beads.NewMemStoreFrom(3000, append(fresh, expired...), nil)
	res := check.Run(&doctor.CheckContext{})
	if res.Status != doctor.StatusWarning {
		t.Fatalf("overdue city: Status = %v, want Warning: %s", res.Status, res.Message)
	}
	if !strings.Contains(res.Message, "24h0m0s") {
		t.Fatalf("message %q should name the configured TTL", res.Message)
	}
}
