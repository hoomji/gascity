package main

import (
	"context"
	"errors"
	"testing"

	"github.com/gastownhall/gascity/internal/beads"
)

// policyCtxListStub is a beads.Store that records the query its ListContext
// received, so the policy layer's tier expansion is observable.
type policyCtxListStub struct {
	beads.Store
	gotQuery beads.ListQuery
	rows     []beads.Bead
}

func (s *policyCtxListStub) ListContext(ctx context.Context, q beads.ListQuery) ([]beads.Bead, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	s.gotQuery = q
	return s.rows, nil
}

func TestBeadPolicyStoreListContextForwardsTier(t *testing.T) {
	inner := &policyCtxListStub{Store: beads.NewMemStore(), rows: []beads.Bead{{ID: "p-1"}}}
	wrapped := wrapStoreWithBeadPolicies(inner, nil)

	got, err := beads.ListWithContext(context.Background(), wrapped, beads.ListQuery{
		AllowScan: true,
		TierMode:  beads.TierIssues,
	})
	if err != nil {
		t.Fatalf("ListWithContext through policy store: %v", err)
	}
	if len(got) != 1 || got[0].ID != "p-1" {
		t.Fatalf("got %+v, want the stub rows", got)
	}
	if inner.gotQuery.TierMode != beads.TierBoth {
		t.Fatalf("forwarded TierMode = %v, want TierBoth (policy expansion)", inner.gotQuery.TierMode)
	}
}

func TestBeadPolicyStoreListContextCanceled(t *testing.T) {
	inner := &policyCtxListStub{Store: beads.NewMemStore()}
	wrapped := wrapStoreWithBeadPolicies(inner, nil)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	_, err := beads.ListWithContext(ctx, wrapped, beads.ListQuery{AllowScan: true})
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled ListContext = %v, want context.Canceled", err)
	}
}
