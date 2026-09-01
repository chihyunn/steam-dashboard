import importlib.util
import json
import os
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

RUNTIME_ENV_PATH = (
    Path(__file__).resolve().parents[1]
    / "production"
    / "update_runtime_env.py"
)
RUNTIME_SPEC = importlib.util.spec_from_file_location("update_runtime_env", RUNTIME_ENV_PATH)
RUNTIME_ENV = importlib.util.module_from_spec(RUNTIME_SPEC)
RUNTIME_SPEC.loader.exec_module(RUNTIME_ENV)


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


class DailyDigestTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        DASHBOARD.DB_PATH = str(Path(self.temp_dir.name) / "dashboard.db")
        DASHBOARD.init_db()
        self.original_fetch = DASHBOARD.fetch_wishlist_for_date
        self.original_send = DASHBOARD.send_telegram
        self.original_label = DASHBOARD.GAME_LABEL
        self.original_mode = DASHBOARD.DAILY_DIGEST_MODE
        self.original_cached_wishlist = dict(DASHBOARD.cached_wishlist)

    def tearDown(self):
        DASHBOARD.fetch_wishlist_for_date = self.original_fetch
        DASHBOARD.send_telegram = self.original_send
        DASHBOARD.GAME_LABEL = self.original_label
        DASHBOARD.DAILY_DIGEST_MODE = self.original_mode
        DASHBOARD.cached_wishlist = self.original_cached_wishlist
        self.temp_dir.cleanup()

    def test_wishlist_digest_reports_confirmed_daily_weekly_and_cumulative_changes(self):
        DASHBOARD.GAME_LABEL = "AIR EMPIRE: 1950 Airline Tycoon"
        DASHBOARD.upsert_daily_wishlist("2026-07-28", 3, 0, 0, 0)
        DASHBOARD.fetch_wishlist_for_date = lambda date_str: {
            "adds": 2,
            "deletes": 0,
            "purchases": 0,
            "gifts": 0,
        } if date_str == "2026-07-29" else None
        DASHBOARD.cached_wishlist = {
            "adds": 5,
            "deletes": 0,
            "purchases": 0,
            "gifts": 0,
            "net": 5,
        }
        messages = []
        DASHBOARD.send_telegram = lambda message: messages.append(message) or True

        DASHBOARD.send_wishlist_daily_digest(datetime(2026, 7, 30).date())

        self.assertEqual(len(messages), 1)
        message = messages[0]
        self.assertIn("AIR EMPIRE: 1950 Airline Tycoon 위시리스트 리포트", message)
        self.assertIn("Steam 확정일: <b>07/29</b>", message)
        self.assertIn("추가: <b>+2개</b>", message)
        self.assertIn("순증: <b>+2개</b>", message)
        self.assertIn("순증 <b>+5개</b>", message)
        self.assertIn("현재 순위시: <b>~5개</b>", message)
        self.assertNotIn("누적 판매", message)

    def test_digest_dispatches_by_configured_mode(self):
        original_wishlist_digest = DASHBOARD.send_wishlist_daily_digest
        original_sales_digest = DASHBOARD.send_sales_daily_digest
        calls = []
        try:
            DASHBOARD.send_wishlist_daily_digest = lambda: calls.append("wishlist")
            DASHBOARD.send_sales_daily_digest = lambda: calls.append("sales")

            DASHBOARD.DAILY_DIGEST_MODE = "wishlist"
            DASHBOARD.send_daily_digest()
            DASHBOARD.DAILY_DIGEST_MODE = "sales"
            DASHBOARD.send_daily_digest()
        finally:
            DASHBOARD.send_wishlist_daily_digest = original_wishlist_digest
            DASHBOARD.send_sales_daily_digest = original_sales_digest

        self.assertEqual(calls, ["wishlist", "sales"])

    def test_wishlist_digest_retries_when_previous_day_is_not_confirmed(self):
        DASHBOARD.fetch_wishlist_for_date = lambda _date_str: None
        messages = []
        DASHBOARD.send_telegram = lambda message: messages.append(message) or True

        sent = DASHBOARD.send_wishlist_daily_digest(datetime(2026, 7, 30).date())

        self.assertFalse(sent)
        self.assertEqual(messages, [])


class GameSwitcherTests(unittest.TestCase):
    def setUp(self):
        self.original_games = os.environ.get("STEAM_DASHBOARD_GAMES_JSON")

    def tearDown(self):
        if self.original_games is None:
            os.environ.pop("STEAM_DASHBOARD_GAMES_JSON", None)
        else:
            os.environ["STEAM_DASHBOARD_GAMES_JSON"] = self.original_games

    def test_loads_two_public_game_targets(self):
        os.environ["STEAM_DASHBOARD_GAMES_JSON"] = json.dumps([
            {"app_id": "4451370", "name": "Grand Cru", "port": 8081},
            {"app_id": "4958590", "name": "Air Empire", "port": 8083},
        ])

        games = DASHBOARD.load_game_switcher()

        self.assertEqual([game["app_id"] for game in games], ["4451370", "4958590"])
        self.assertEqual(games[1]["port"], 8083)

    def test_invalid_config_falls_back_to_current_game(self):
        os.environ["STEAM_DASHBOARD_GAMES_JSON"] = "{invalid"

        games = DASHBOARD.load_game_switcher()

        self.assertEqual(games, [{
            "app_id": DASHBOARD.APP_ID,
            "name": DASHBOARD.GAME_LABEL,
            "port": DASHBOARD.PORT,
        }])


class DashboardVisibilityTests(unittest.TestCase):
    def test_income_totals_remain_visible_without_portfolio_targets(self):
        self.assertIn('id="totalSales"', DASHBOARD.DASHBOARD_HTML)
        self.assertIn('id="netRevenue"', DASHBOARD.DASHBOARD_HTML)
        self.assertIn("grossLabel: '총매출'", DASHBOARD.DASHBOARD_HTML)
        self.assertNotIn('id="portfolioPanel"', DASHBOARD.DASHBOARD_HTML)
        self.assertNotIn("renderPortfolio", DASHBOARD.DASHBOARD_HTML)
        self.assertNotIn("환상모험", DASHBOARD.DASHBOARD_HTML)
        self.assertNotIn("월 400만원", DASHBOARD.DASHBOARD_HTML)


class CachedDashboardStateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        DASHBOARD.DB_PATH = str(Path(self.temp_dir.name) / "dashboard.db")
        DASHBOARD.init_db()
        self.original_wishlist = dict(DASHBOARD.cached_wishlist)
        self.original_sales_countries = dict(DASHBOARD.cached_sales_by_country)
        self.original_wishlist_countries = dict(DASHBOARD.cached_wishlist_by_country)
        self.original_peak = DASHBOARD.peak_players

    def tearDown(self):
        DASHBOARD.cached_wishlist = self.original_wishlist
        DASHBOARD.cached_sales_by_country = self.original_sales_countries
        DASHBOARD.cached_wishlist_by_country = self.original_wishlist_countries
        DASHBOARD.peak_players = self.original_peak
        self.temp_dir.cleanup()

    def test_nonempty_wishlist_always_returns_peak_and_country_state(self):
        DASHBOARD.cached_wishlist = {"net": 2200}
        DASHBOARD.cached_sales_by_country = {"US": {"units": 10}}
        DASHBOARD.cached_wishlist_by_country = {"DE": {"adds": 20}}
        DASHBOARD.peak_players = 7

        wishlist, sales_countries, wishlist_countries, peak = (
            DASHBOARD.get_cached_dashboard_state()
        )

        self.assertEqual(wishlist["net"], 2200)
        self.assertEqual(sales_countries["US"]["units"], 10)
        self.assertEqual(wishlist_countries["DE"]["adds"], 20)
        self.assertEqual(peak, 7)

    def test_empty_memory_cache_uses_last_saved_wishlist_total(self):
        DASHBOARD.save_wishlist_snapshot(0, 0, 0, 2195)
        DASHBOARD.cached_wishlist = {}

        wishlist, _sales_countries, _wishlist_countries, _peak = (
            DASHBOARD.get_cached_dashboard_state()
        )

        self.assertEqual(wishlist, {"net": 2195, "stale": True})


class RuntimeEnvironmentUpdateTests(unittest.TestCase):
    def test_json_merge_preserves_unmeasured_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / "dashboard.env"
            patch_path = Path(temp_dir) / "playtime.json"
            env_path.write_text(
                "KEEP=value\n"
                "STEAMWORKS_SNAPSHOT_JSON={"
                "\"measured_users\":671,"
                "\"refund_reasons\":[{\"label_en\":\"It is not fun\",\"count\":4}]}"
                "\n",
                encoding="utf-8",
            )
            patch_path.write_text(
                json.dumps({"measured_users": 676, "average_playtime_minutes": 427}),
                encoding="utf-8",
            )

            updated = RUNTIME_ENV.update_environment_file(
                env_path,
                {"PORTFOLIO_USD_KRW": "1380"},
                {"STEAMWORKS_SNAPSHOT_JSON": patch_path},
            )

            assignments = {}
            for line in env_path.read_text(encoding="utf-8").splitlines():
                key, raw_value = line.split("=", 1)
                assignments[key] = RUNTIME_ENV.decode_value(raw_value)
            snapshot = json.loads(assignments["STEAMWORKS_SNAPSHOT_JSON"])
            self.assertEqual(snapshot["measured_users"], 676)
            self.assertEqual(snapshot["average_playtime_minutes"], 427)
            self.assertEqual(snapshot["refund_reasons"][0]["count"], 4)
            self.assertEqual(assignments["KEEP"], "value")
            self.assertEqual(assignments["PORTFOLIO_USD_KRW"], "1380")
            self.assertEqual(updated, ["PORTFOLIO_USD_KRW", "STEAMWORKS_SNAPSHOT_JSON"])


if __name__ == "__main__":
    unittest.main()
