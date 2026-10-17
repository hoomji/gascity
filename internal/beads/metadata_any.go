package beads

// MetadataAnyLister is the optional capability of answering "every bead whose
// metadata[key] is one of values" in ONE listing. A caller that holds N values
// and asks ListByMetadata N times pays N backend round trips, and on a
// backend that cannot index a JSON metadata value each one is a full scan of
// the ledger. Callers discover the capability by type assertion and keep the
// per-value ListByMetadata path as the fallback.
//
// The answer is exact: a bead is returned only when its metadata[key] equals
// one of values, whatever the backend pushed down. Limit and opts mean what
// they mean to ListByMetadata; limit 0 is unlimited.
type MetadataAnyLister interface {
	ListByMetadataAny(key string, values []string, limit int, opts ...QueryOpt) ([]Bead, error)
}

// ListByMetadataAnyQuery is the ListQuery a List-composed store answers
// ListByMetadataAny with. It is the sibling of ListByMetadataQuery.
func ListByMetadataAnyQuery(key string, values []string, limit int, opts ...QueryOpt) ListQuery {
	return ListQuery{
		MetadataAny:   MetadataAny{Key: key, Values: values},
		Limit:         limit,
		IncludeClosed: HasOpt(opts, IncludeClosed),
		AllowScan:     true,
		Sort:          SortCreatedDesc,
		TierMode:      TierModeFromOpts(opts),
	}
}
