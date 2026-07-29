# Production dashboards

This directory tracks the game dashboards running on the existing EC2 instance.
The same Python source is used by isolated systemd services so a slow financial
scan or database problem in one game cannot affect another:

- Grand Cru: port `8081`, `/etc/steam-dashboard.env`,
  `/home/ubuntu/steam_dashboard.db`
- Air Empire: port `8083`, `/etc/air-empire-dashboard.env`,
  `/home/ubuntu/air_empire_dashboard.db`

Runtime credentials are supplied only through the environment files; they must
never be committed. Both environments use the same public
`STEAM_DASHBOARD_GAMES_JSON` so the header selector can switch ports on the
current host.

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

Per-game runtime settings:

```text
STEAM_APP_ID
STEAM_GAME_LABEL
STEAM_GAME_STAGE_LABEL
STEAM_LAUNCH_DATE
STEAM_WISHLIST_START_DATE
STEAM_WISHLIST_OPENING_BALANCE
STEAM_DASHBOARD_PORT
STEAM_DB_PATH
STEAM_DIGEST_STATE_FILE
STEAM_DASHBOARD_GAMES_JSON
```

Back up each database, environment file, and the previous Python source before
deploying.
