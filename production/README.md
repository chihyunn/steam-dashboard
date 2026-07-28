# Production dashboard

This directory tracks the single-game dashboard running on the existing EC2
instance. Runtime credentials are supplied only through
`/etc/steam-dashboard.env`; they must never be committed.

Collection cadence:

- players, reviews, recent sales, and alerts: every 5 minutes
- country and wishlist full scans: every 3 hours
- wishlist totals: dated-event net plus `STEAM_WISHLIST_OPENING_BALANCE`
- login-only Steamworks playtime and refund-reason metrics: a dated manual
  snapshot, clearly marked in the UI

The service continues to use `/home/ubuntu/steam_dashboard.db`. Back up both the
database and the previous Python source before deploying.
