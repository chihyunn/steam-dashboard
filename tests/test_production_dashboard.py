import importlib.util
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "production"
    / "steam_dashboard.py"
)
SPEC = importlib.util.spec_from_file_location("production_dashboard", MODULE_PATH)
DASHBOARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DASHBOARD)


class WishlistDailyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        DASHBOARD.DB_PATH = str(Path(self.temp_dir.name) / "dashboard.db")
        DASHBOARD.shutdown_event.clear()
        DASHBOARD.init_db()
        self.original_fetch = DASHBOARD.fetch_wishlist_for_date
        self.original_start = DASHBOARD.WISHLIST_START_DATE

    def tearDown(self):
        DASHBOARD.fetch_wishlist_for_date = self.original_fetch
        DASHBOARD.WISHLIST_START_DATE = self.original_start
        self.temp_dir.cleanup()

    def test_upsert_persists_daily_net_change_in_date_order(self):
        DASHBOARD.upsert_daily_wishlist("2026-07-27", 10, 2, 1, 0)
        DASHBOARD.upsert_daily_wishlist("2026-07-28", 8, 1, 2, 1)
        DASHBOARD.upsert_daily_wishlist("2026-07-27", 12, 3, 2, 0)

        rows = DASHBOARD.get_daily_wishlist()

        self.assertEqual([row["date"] for row in rows], ["2026-07-27", "2026-07-28"])
        self.assertEqual(rows[0]["net_change"], 7)
        self.assertEqual(rows[1]["net_change"], 4)

    def test_full_scan_excludes_current_day_and_builds_totals(self):
        start = datetime.now().date() - timedelta(days=2)
        yesterday = datetime.now().date() - timedelta(days=1)
        DASHBOARD.WISHLIST_START_DATE = start.isoformat()
        requested_dates = []

        def fake_fetch(date_str):
            requested_dates.append(date_str)
            return {"adds": 10, "deletes": 2, "purchases": 1, "gifts": 0}

        DASHBOARD.fetch_wishlist_for_date = fake_fetch
        totals = DASHBOARD.fetch_wishlist_totals()

        self.assertEqual(requested_dates, [start.isoformat(), yesterday.isoformat()])
        self.assertEqual(totals["adds"], 20)
        self.assertEqual(totals["deletes"], 4)
        self.assertEqual(totals["purchases"], 2)
        self.assertEqual(totals["net"], 14 + DASHBOARD.WISHLIST_OPENING_BALANCE)
        self.assertEqual(len(DASHBOARD.get_daily_wishlist()), 2)

    def test_incomplete_read_does_not_publish_partial_total(self):
        start = datetime.now().date() - timedelta(days=1)
        DASHBOARD.WISHLIST_START_DATE = start.isoformat()
        DASHBOARD.fetch_wishlist_for_date = lambda _date_str: None

        self.assertIsNone(DASHBOARD.fetch_wishlist_totals())
        self.assertEqual(DASHBOARD.get_daily_wishlist(), [])


if __name__ == "__main__":
    unittest.main()
