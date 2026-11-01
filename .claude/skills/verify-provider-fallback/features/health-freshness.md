# Health record freshness

Each record in `provider-health.json` has a lifetime. Old-format records (`provider`, `status`, `probed_at`) are fresh for 60 s. `ttl_s` sets a record's own lifetime. On an unhealthy record, `until` keeps the provider red until that time regardless of age. A missing, stale or unreadable record means fail-open: the provider counts as healthy.

## Sub-features

- `ttl-extends`: a red record with `ttl_s = 600` still blocks after 60 s.
- `ttl-expires`: once `ttl_s` has passed and there's no `until`, the provider is used again.
- `until-holds`: a red record with a short `ttl_s` and a later `until` keeps blocking past the TTL.
- `until-releases`: once `until` has passed and the TTL has expired, the provider is used again.
- `fail-open-unreadable`: a corrupt health file counts as healthy.

## How to get to it (user POV)

The `quota-watch` pack writes these records every 10 minutes, with `ttl_s = 900` and `until` = the quota reset time. An operator can inspect them with `cat .gc/cache/provider-health.json`.

## Driving it with gcv

`gcv hold` sets `ttl_s` = SECS (at least 60) and `until` = now + SECS. For the cases below, write the file directly with `python3`, so `ttl_s` and `until` can differ:

- **ttl-extends.** Write `{"provider":"fake-primary","status":"unhealthy","probed_at":<now-120>,"ttl_s":600}`, then `gcv recycle; gcv sling t1; gcv wait-launch`. Expect `provider=fake-fallback`.
- **ttl-expires.** Write the same record with `"ttl_s":60` and `probed_at` = now−120, then recycle, sling and wait. Expect `provider=fake-primary`.
- **until-holds.** Write `"ttl_s":60`, `probed_at` = now−120, `until` = now+600. Expect `provider=fake-fallback`.
- **until-releases.** Write `until` = now−10 and `probed_at` = now−120 with `ttl_s` 60. Expect `provider=fake-primary`.
- **fail-open-unreadable.** Write `not json` to the file. Expect `provider=fake-primary`.

Take `gcv proof <sub-feature>` after each case.

## Gotchas

- The reconciler reads the file at the start of each pass. A change made between a sling and the next pass is seen within one pass, so wait for the launch rather than using a fixed sleep.
- Not yet proven end to end. The unit test `TestSnapshotCheck_HealthRecordFreshness` covers the rules, but a live run is still owed.
