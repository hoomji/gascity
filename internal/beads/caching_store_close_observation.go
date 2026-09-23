package beads

// appendObservedCloseLocked preserves an externally observed terminal transition
// before a read refresh absorbs it. Otherwise reconciliation later sees an
// already-closed row and correctly suppresses the only completion notification.
// Call only after accepting the read through its sequence/recency fences, and
// publish the returned notifications after releasing c.mu.
func (c *CachingStore) appendObservedCloseLocked(notes []cacheNotification, fresh Bead) []cacheNotification {
	if old, ok := c.beads[fresh.ID]; ok && old.Status != "closed" && fresh.Status == "closed" {
		return append(notes, cacheNotification{eventType: "bead.closed", bead: cloneBead(fresh)})
	}
	return notes
}
