# Production dashboard

This directory tracks the single-game dashboard running on the existing EC2
instance. Runtime credentials are supplied only through
`/etc/steam-dashboard.env`; they must never be committed.

Collection cadence:

- players, reviews, recent sales, and alerts: every 5 minutes
- country and wishlist full scans: every 3 hours
- wishlist totals: dated-event net plus `STEAM_WISHLIST_OPENING_BALANCE`
- daily wishlist movement: confirmed prior-day additions, deletions,
  purchase/gift conversions, and net growth persisted in `daily_wishlist`
- login-only Steamworks playtime and refund-reason metrics: a dated manual
  snapshot, clearly marked in the UI
- login-only traffic and UTM conversion metrics: a dated manual
  `STEAM_MARKETING_SNAPSHOT_JSON`; never copy Steam login cookies to the server

Marketing snapshot shape:

```json
{
  "verified_at": "2026-07-28",
  "traffic_period": "2026-07-21 – 2026-07-27",
  "traffic": {"impressions": 50424, "visits": 1183, "ctr_percent": 1.8},
  "traffic_sources": [{"name": "Direct Navigation", "visits": 281}],
  "utm_period": "2026-07-15 – 2026-07-28",
  "utm_sources": [{"name": "SteamDB", "visits": 25, "wishlists": 0}],
  "utm": {
    "total_visits": 42,
    "trusted_visits": 5,
    "tracked_visits": 3,
    "wishlists": 0,
    "purchases": 0,
    "activations": 0
  }
}
```

Traffic sources are store-page visits, not wishlist attribution. Only UTM
conversions inside Steam's 72-hour window should be presented as attributed.

The service continues to use `/home/ubuntu/steam_dashboard.db`. Back up both the
database and the previous Python source before deploying.
