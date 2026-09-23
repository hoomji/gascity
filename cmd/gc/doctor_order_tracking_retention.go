package main

import (
	"fmt"
	"strings"
	"time"

	"github.com/gastownhall/gascity/internal/beads"
	"github.com/gastownhall/gascity/internal/config"
	"github.com/gastownhall/gascity/internal/doctor"
)

const (
	orderTrackingRetentionCheckThreshold = 500
	orderTrackingRetentionCheckListLimit = 501
)

// orderTrackingRetentionCheck warns when closed order-tracking beads past
// their retention TTL pile up — i.e. when retention sweeps are overdue. The
// controller watchdog and the order-tracking-sweep order prune these; the
// check surfaces cities where neither is keeping up. It is pure observability
// and never gates.
//
// It counts only beads closed before now - delete_after_close, not every
// closed tracking bead. The live closed population is set by production rate
// times TTL (a city minting ~7k tracking beads/day with a 24h TTL holds ~7k
// closed beads when perfectly healthy), so a raw closed count against a fixed
// threshold fires forever on a busy city and says nothing about retention. The
// expired count is ~0 on a healthy city whatever its rate or TTL — only the
// per-order recent-history floor and one sweep interval of lag remain — and
// grows only when pruning falls behind, which is what this check is for.
type orderTrackingRetentionCheck struct {
	cityPath string
	newStore func(string) (beads.Store, error)
	policy   orderTrackingRetentionPolicy
	now      func() time.Time
}

// newOrderTrackingRetentionCheck constructs an orderTrackingRetentionCheck
// using the default retention policy; withConfig applies a city's policy.
func newOrderTrackingRetentionCheck(cityPath string, newStore func(string) (beads.Store, error)) *orderTrackingRetentionCheck {
	return &orderTrackingRetentionCheck{
		cityPath: cityPath,
		newStore: newStore,
		policy:   orderTrackingRetentionPolicyForConfig(nil),
		now:      time.Now,
	}
}

// withConfig applies cfg's [beads.policies.order_tracking] retention TTL.
func (c *orderTrackingRetentionCheck) withConfig(cfg *config.City) *orderTrackingRetentionCheck {
	c.policy = orderTrackingRetentionPolicyForConfig(cfg)
	return c
}

// Name implements doctor.Check.
func (c *orderTrackingRetentionCheck) Name() string { return "order-tracking-retention" }

// CanFix implements doctor.Check.
func (c *orderTrackingRetentionCheck) CanFix() bool { return false }

// Fix implements doctor.Check.
func (c *orderTrackingRetentionCheck) Fix(_ *doctor.CheckContext) error { return nil }

// WarmupEligible implements doctor.Check.
func (c *orderTrackingRetentionCheck) WarmupEligible() bool { return false }

// Run implements doctor.Check.
func (c *orderTrackingRetentionCheck) Run(_ *doctor.CheckContext) *doctor.CheckResult {
	res := &doctor.CheckResult{Name: c.Name(), Severity: doctor.SeverityAdvisory}
	if c.newStore == nil || strings.TrimSpace(c.cityPath) == "" {
		res.Status = doctor.StatusOK
		res.Message = "order-tracking retention: no bead store configured"
		return res
	}
	store, err := c.newStore(c.cityPath)
	if err != nil {
		res.Status = doctor.StatusWarning
		res.Message = fmt.Sprintf("order-tracking retention unknown: opening city bead store: %v", err)
		return res
	}
	// The city work store holds the pre-cutover backlog; the orders binding
	// holds everything a split city has written since. Counting only one of them
	// reports a healthy 0 on exactly the city whose backlog is growing.
	stores := []beads.Store{store}
	if ordersStore := relocatedOrdersClassStore(c.cityPath, nil); ordersStore != nil && ordersStore != store {
		stores = append(stores, ordersStore)
	}
	now := time.Now
	if c.now != nil {
		now = c.now
	}
	ttl := c.policy.deleteAfterClose
	if ttl <= 0 {
		ttl = defaultOrderTrackingDeleteAfterClose
	}
	cutoff := now().Add(-ttl)
	count := 0
	capped := false
	for _, s := range stores {
		entries, err := beads.HandlesFor(s).Live.List(beads.ListQuery{
			Status:   "closed",
			Label:    labelOrderTracking,
			TierMode: beads.TierBoth,
			// Same reference time the sweep uses (UpdatedAt, else CreatedAt).
			UpdatedBefore: cutoff,
			Limit:         orderTrackingRetentionCheckListLimit,
		})
		if err != nil {
			res.Status = doctor.StatusWarning
			res.Message = fmt.Sprintf("order-tracking retention unknown: listing closed beads: %v", err)
			return res
		}
		count += len(entries)
		// Each store's read is capped independently, so "at least this many"
		// has to be tracked per store rather than inferred from the total.
		if len(entries) >= orderTrackingRetentionCheckListLimit {
			capped = true
		}
	}
	if count >= orderTrackingRetentionCheckThreshold {
		countStr := fmt.Sprintf("%d", count)
		if capped {
			countStr = "≥" + countStr
		}
		res.Status = doctor.StatusWarning
		res.Message = fmt.Sprintf("%s closed order-tracking beads are past their %s retention TTL: pruning is behind (run gc order sweep-tracking --confirm, or check the order-tracking-sweep order; TTL: [beads.policies.order_tracking].delete_after_close)", countStr, ttl)
		return res
	}
	res.Status = doctor.StatusOK
	res.Message = fmt.Sprintf("%d closed order-tracking beads past the %s retention TTL", count, ttl)
	return res
}
