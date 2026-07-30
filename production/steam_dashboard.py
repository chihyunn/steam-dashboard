#!/usr/bin/env python3
"""
Steam game metrics dashboard
Sales tracking + Telegram alerts + Web dashboard
"""

import json
import time
import threading
import sqlite3
import os
import signal
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen, Request
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timedelta

# ========== CONFIG ==========
STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "")
STEAM_FINANCIAL_KEY = os.environ.get("STEAM_FINANCIAL_KEY", "")
APP_ID = os.environ.get("STEAM_APP_ID", "4451370")
GAME_LABEL = os.environ.get("STEAM_GAME_LABEL", "Grand Cru: The Wine Maker")
GAME_STAGE_LABEL = os.environ.get("STEAM_GAME_STAGE_LABEL", "EA")
LAUNCH_DATE = os.environ.get("STEAM_LAUNCH_DATE", "2026-03-13")
WISHLIST_START_DATE = os.environ.get("STEAM_WISHLIST_START_DATE", "2026-02-28")
PORT = int(os.environ.get("STEAM_DASHBOARD_PORT", "8081"))
POLL_INTERVAL = int(os.environ.get("STEAM_POLL_INTERVAL", "300"))
FULL_SCAN_INTERVAL = int(os.environ.get("STEAM_FULL_SCAN_INTERVAL", "10800"))
WISHLIST_OPENING_BALANCE = int(os.environ.get("STEAM_WISHLIST_OPENING_BALANCE", "0"))
DAILY_DIGEST_MODE = os.environ.get("STEAM_DAILY_DIGEST_MODE", "sales").strip().lower()
if DAILY_DIGEST_MODE not in {"sales", "wishlist"}:
    print(f"[CONFIG] Invalid STEAM_DAILY_DIGEST_MODE={DAILY_DIGEST_MODE!r}; using sales")
    DAILY_DIGEST_MODE = "sales"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_IDS = [x.strip() for x in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",") if x.strip()]

DB_PATH = os.environ.get("STEAM_DB_PATH", "steam_dashboard.db")

def load_steamworks_snapshot():
    """Load login-only measurements from the private runtime environment."""
    raw_snapshot = os.environ.get("STEAMWORKS_SNAPSHOT_JSON", "{}")
    try:
        snapshot = json.loads(raw_snapshot)
        return snapshot if isinstance(snapshot, dict) else {}
    except json.JSONDecodeError:
        print("[CONFIG] Invalid STEAMWORKS_SNAPSHOT_JSON; hiding manual metrics")
        return {}


STEAMWORKS_SNAPSHOT = load_steamworks_snapshot()

def load_marketing_snapshot():
    """Load a dated Steamworks traffic/UTM snapshot without storing login cookies."""
    raw_snapshot = os.environ.get("STEAM_MARKETING_SNAPSHOT_JSON", "{}")
    try:
        snapshot = json.loads(raw_snapshot)
        return snapshot if isinstance(snapshot, dict) else {}
    except json.JSONDecodeError:
        print("[CONFIG] Invalid STEAM_MARKETING_SNAPSHOT_JSON; hiding marketing metrics")
        return {}


STEAM_MARKETING_SNAPSHOT = load_marketing_snapshot()

def load_game_switcher():
    """Load public game-switcher metadata for dashboards sharing one host."""
    raw_games = os.environ.get("STEAM_DASHBOARD_GAMES_JSON", "")
    if not raw_games:
        return [{"app_id": APP_ID, "name": GAME_LABEL, "port": PORT}]
    try:
        configured = json.loads(raw_games)
    except json.JSONDecodeError:
        print("[CONFIG] Invalid STEAM_DASHBOARD_GAMES_JSON; showing current game only")
        return [{"app_id": APP_ID, "name": GAME_LABEL, "port": PORT}]

    games = []
    for item in configured if isinstance(configured, list) else []:
        if not isinstance(item, dict) or not item.get("app_id"):
            continue
        try:
            port = int(item.get("port", PORT))
        except (TypeError, ValueError):
            continue
        games.append({
            "app_id": str(item["app_id"]),
            "name": str(item.get("name") or item["app_id"]),
            "port": port,
        })

    if not any(game["app_id"] == APP_ID for game in games):
        games.append({"app_id": APP_ID, "name": GAME_LABEL, "port": PORT})
    return games


DASHBOARD_GAMES = load_game_switcher()

def shutdown_requested():
    event = globals().get("shutdown_event")
    return bool(event and event.is_set())

# ========== DATABASE ==========
def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS player_history (
        timestamp TEXT, player_count INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS review_history (
        timestamp TEXT, total_positive INTEGER, total_negative INTEGER, total_reviews INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS daily_sales (
        date TEXT PRIMARY KEY, units_sold INTEGER, units_returned INTEGER,
        gross_revenue_usd REAL, net_revenue_usd REAL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS sales_snapshots (
        timestamp TEXT PRIMARY KEY, total_units INTEGER, total_returns INTEGER,
        total_net_usd REAL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS wishlist_history (
        timestamp TEXT, total_adds INTEGER, total_deletes INTEGER,
        total_purchases INTEGER, net_wishlists INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS daily_wishlist (
        date TEXT PRIMARY KEY, adds INTEGER, deletes INTEGER,
        purchases INTEGER, gifts INTEGER, net_change INTEGER
    )''')
    conn.commit()
    conn.close()

def save_player_count(count):
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute("INSERT INTO player_history VALUES (?, ?)", (datetime.now().isoformat(), count))
        conn.commit()

def save_review_data(pos, neg, total):
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute("INSERT INTO review_history VALUES (?, ?, ?, ?)", (datetime.now().isoformat(), pos, neg, total))
        conn.commit()

def upsert_daily_sales(date_str, units, returns, gross, net):
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute("""INSERT INTO daily_sales VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
            units_sold=excluded.units_sold, units_returned=excluded.units_returned,
            gross_revenue_usd=excluded.gross_revenue_usd, net_revenue_usd=excluded.net_revenue_usd
        """, (date_str, units, returns, gross, net))
        conn.commit()

def get_player_history(limit=144):
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        rows = conn.execute("SELECT timestamp, player_count FROM player_history ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
    return list(reversed(rows))

def get_review_history(limit=144):
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        rows = conn.execute("SELECT timestamp, total_positive, total_negative, total_reviews FROM review_history ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
    return list(reversed(rows))

def get_all_daily_sales():
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        rows = conn.execute("SELECT date, units_sold, units_returned, gross_revenue_usd, net_revenue_usd FROM daily_sales ORDER BY date").fetchall()
    return rows

def save_sales_snapshot(total_units, total_returns, total_net):
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute("INSERT OR REPLACE INTO sales_snapshots VALUES (?, ?, ?, ?)",
                     (datetime.now().isoformat(), total_units, total_returns, total_net))
        conn.commit()

def get_sales_snapshots():
    """12시간 간격으로 샘플링된 판매 추이"""
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        rows = conn.execute("SELECT timestamp, total_units, total_returns, total_net_usd FROM sales_snapshots ORDER BY timestamp").fetchall()
    if not rows:
        return []
    # 12시간 간격으로 필터링
    result = []
    last_ts = None
    for row in rows:
        ts = datetime.fromisoformat(row[0])
        if last_ts is None or (ts - last_ts).total_seconds() >= 12 * 3600:
            result.append(row)
            last_ts = ts
    # 항상 최신 스냅샷 포함
    if rows[-1] not in result:
        result.append(rows[-1])
    return result

def save_wishlist_snapshot(adds, deletes, purchases, net):
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute("INSERT INTO wishlist_history VALUES (?, ?, ?, ?, ?)",
                     (datetime.now().isoformat(), adds, deletes, purchases, net))
        conn.commit()

def upsert_daily_wishlist(date_str, adds, deletes, purchases, gifts):
    net_change = adds - deletes - purchases - gifts
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute("""INSERT INTO daily_wishlist VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
            adds=excluded.adds, deletes=excluded.deletes,
            purchases=excluded.purchases, gifts=excluded.gifts,
            net_change=excluded.net_change
        """, (date_str, adds, deletes, purchases, gifts, net_change))
        conn.commit()

def get_daily_wishlist(limit=90):
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        rows = conn.execute("""SELECT date, adds, deletes, purchases, gifts, net_change
            FROM daily_wishlist ORDER BY date DESC LIMIT ?""", (limit,)).fetchall()
    return [
        {
            "date": row[0],
            "adds": row[1],
            "deletes": row[2],
            "purchases": row[3],
            "gifts": row[4],
            "net_change": row[5],
        }
        for row in reversed(rows)
    ]

def get_wishlist_history():
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        rows = conn.execute("SELECT timestamp, net_wishlists FROM wishlist_history ORDER BY timestamp DESC LIMIT 144").fetchall()
    return list(reversed(rows))

def get_latest_wishlist_net():
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        row = conn.execute("SELECT net_wishlists FROM wishlist_history ORDER BY timestamp DESC LIMIT 1").fetchone()
    return row[0] if row else 0

def get_sales_totals():
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        row = conn.execute("SELECT COALESCE(SUM(units_sold),0), COALESCE(SUM(units_returned),0), COALESCE(SUM(gross_revenue_usd),0), COALESCE(SUM(net_revenue_usd),0) FROM daily_sales").fetchone()
    return row

def _summarize_sales_rows(rows):
    units = sum(max(0, int(row[1] or 0)) for row in rows)
    returns = sum(abs(int(row[2] or 0)) for row in rows)
    gross = sum(float(row[3] or 0) for row in rows)
    net = sum(float(row[4] or 0) for row in rows)
    return {
        "units": units,
        "returns": returns,
        "refund_rate": round(returns / units * 100, 1) if units else 0,
        "gross": round(gross, 2),
        "net": round(net, 2),
    }

def get_period_metrics():
    rows = get_all_daily_sales()
    today = datetime.now().date()

    def between(start, end):
        return [
            row for row in rows
            if start <= datetime.strptime(row[0], "%Y-%m-%d").date() <= end
        ]

    result = {"lifetime": _summarize_sales_rows(rows)}
    for days in (7, 30):
        current_start = today - timedelta(days=days - 1)
        previous_end = current_start - timedelta(days=1)
        previous_start = previous_end - timedelta(days=days - 1)
        result[str(days)] = {
            "current": _summarize_sales_rows(between(current_start, today)),
            "previous": _summarize_sales_rows(between(previous_start, previous_end)),
            "start": current_start.isoformat(),
            "end": today.isoformat(),
        }
    return result

def get_recent_sales_delta(hours=3):
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        latest = conn.execute(
            "SELECT timestamp, total_units, total_returns, total_net_usd "
            "FROM sales_snapshots ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        baseline = conn.execute(
            "SELECT timestamp, total_units, total_returns, total_net_usd "
            "FROM sales_snapshots WHERE timestamp <= ? ORDER BY timestamp DESC LIMIT 1",
            (cutoff,),
        ).fetchone()
        if latest and not baseline:
            baseline = conn.execute(
                "SELECT timestamp, total_units, total_returns, total_net_usd "
                "FROM sales_snapshots ORDER BY timestamp LIMIT 1"
            ).fetchone()

    if not latest or not baseline:
        return {"hours": hours, "ready": False}

    observed_hours = max(
        0,
        (datetime.fromisoformat(latest[0]) - datetime.fromisoformat(baseline[0])).total_seconds() / 3600,
    )
    return {
        "hours": hours,
        "ready": observed_hours >= hours * 0.8,
        "observed_hours": round(observed_hours, 1),
        "units": int(latest[1] or 0) - int(baseline[1] or 0),
        "returns": abs(int(latest[2] or 0)) - abs(int(baseline[2] or 0)),
        "net": round(float(latest[3] or 0) - float(baseline[3] or 0), 2),
        "from": baseline[0],
        "to": latest[0],
    }

# ========== STEAM API ==========
def fetch_json(url):
    try:
        req = Request(url, headers={"User-Agent": "SteamDashboard/1.0"})
        with urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"  [ERROR] {e}")
        return None

def get_current_players():
    data = fetch_json(f"https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid={APP_ID}&key={STEAM_API_KEY}")
    if data and "response" in data:
        return data["response"].get("player_count", 0)
    return 0

def get_app_details():
    data = fetch_json(f"https://store.steampowered.com/api/appdetails?appids={APP_ID}&l=korean")
    if data and APP_ID in data and data[APP_ID].get("success"):
        return data[APP_ID]["data"]
    return None

def get_reviews():
    data = fetch_json(f"https://store.steampowered.com/appreviews/{APP_ID}?json=1&language=all&purchase_type=all&num_per_page=0")
    if data and data.get("success") == 1:
        return data.get("query_summary", {})
    return {}

def get_recent_reviews():
    data = fetch_json(f"https://store.steampowered.com/appreviews/{APP_ID}?json=1&language=all&purchase_type=all&num_per_page=5&filter=recent")
    if data and data.get("success") == 1:
        return data.get("reviews", [])
    return []

# ========== FINANCIAL API ==========
FINANCIAL_BASE = "https://partner.steam-api.com"

def fetch_sales_for_date(date_str):
    """일별 판매 데이터 (pagination 포함). 국가별 판매 수 + 완전성 플래그 함께 반환.
    complete=False 면 API 일시 실패로 부분/빈 응답 → 호출측에서 upsert 스킵해야 함
    (안 그러면 total_units 가 일시적으로 떨어졌다 회복하며 '유령 새 판매' 알림 발생)."""
    units = 0
    returns = 0
    gross = 0.0
    net = 0.0
    countries = {}
    hwm = 0
    complete = True

    while not shutdown_requested():
        url = (f"{FINANCIAL_BASE}/IPartnerFinancialsService/GetDetailedSales/v001/"
               f"?key={STEAM_FINANCIAL_KEY}&date={date_str}&highwatermark_id={hwm}")
        data = fetch_json(url)
        if data is None:
            # 네트워크/API 실패 → 이 날짜 데이터는 신뢰 불가 (부분 읽힘)
            complete = False
            break
        if "response" not in data:
            break
        resp = data["response"]
        for item in resp.get("results", []):
            if str(item.get("primary_appid", item.get("appid", ""))) == APP_ID:
                sold = item.get("gross_units_sold", 0)
                ret = item.get("gross_units_returned", 0)
                units += sold
                returns += ret
                gross += float(item.get("gross_sales_usd", 0))
                net += float(item.get("net_sales_usd", 0))
                cc = item.get("country_code", "??")
                countries[cc] = countries.get(cc, 0) + sold
        max_id = resp.get("max_id", 0)
        if max_id == hwm or max_id == 0:
            break
        hwm = max_id

    if shutdown_requested():
        complete = False
    return units, returns, gross, net, countries, complete

def fetch_sales_by_country():
    """출시일부터 오늘까지 국가별 판매 집계"""
    launch = datetime.strptime(LAUNCH_DATE, "%Y-%m-%d").date()
    today = datetime.now().date()
    current = launch
    countries = {}

    while current <= today and not shutdown_requested():
        ds = current.strftime("%Y-%m-%d")
        hwm = 0
        while not shutdown_requested():
            url = (f"{FINANCIAL_BASE}/IPartnerFinancialsService/GetDetailedSales/v001/"
                   f"?key={STEAM_FINANCIAL_KEY}&date={ds}&highwatermark_id={hwm}")
            data = fetch_json(url)
            if not data or "response" not in data:
                break
            resp = data["response"]
            for item in resp.get("results", []):
                if str(item.get("primary_appid", item.get("appid", ""))) == APP_ID:
                    cc = item.get("country_code", "??")
                    sold = item.get("gross_units_sold", 0)
                    ret = item.get("gross_units_returned", 0)
                    net = float(item.get("net_sales_usd", 0))
                    if cc not in countries:
                        countries[cc] = {"units": 0, "returns": 0, "net": 0.0}
                    countries[cc]["units"] += sold
                    countries[cc]["returns"] += ret
                    countries[cc]["net"] += net
            max_id = resp.get("max_id", 0)
            if max_id == hwm or max_id == 0:
                break
            hwm = max_id
        current += timedelta(days=1)

    # 판매 순 정렬
    return dict(sorted(countries.items(), key=lambda x: x[1]["units"], reverse=True))

def fetch_wishlist_by_country():
    """위시리스트 국가별 집계"""
    launch = datetime.strptime(WISHLIST_START_DATE, "%Y-%m-%d").date()
    today = datetime.now().date()
    current = launch
    countries = {}

    while current <= today and not shutdown_requested():
        ds = current.strftime("%Y-%m-%d")
        url = f"{FINANCIAL_BASE}/IPartnerFinancialsService/GetAppWishlistReporting/v001/?key={STEAM_FINANCIAL_KEY}&appid={APP_ID}&date={ds}"
        data = fetch_json(url)
        if data and "response" in data:
            for c in data["response"].get("country_summary", []):
                cc = c.get("country_code", "??")
                s = c.get("summary_actions", {})
                if cc not in countries:
                    countries[cc] = {"adds": 0, "deletes": 0, "purchases": 0}
                countries[cc]["adds"] += s.get("wishlist_adds", 0)
                countries[cc]["deletes"] += s.get("wishlist_deletes", 0)
                countries[cc]["purchases"] += s.get("wishlist_purchases", 0)
        current += timedelta(days=1)

    return dict(sorted(countries.items(), key=lambda x: x[1]["adds"], reverse=True))

def refresh_all_sales():
    """출시일부터 오늘까지 전체 판매 데이터 갱신"""
    launch = datetime.strptime(LAUNCH_DATE, "%Y-%m-%d").date()
    today = datetime.now().date()
    current = launch

    while current <= today and not shutdown_requested():
        ds = current.strftime("%Y-%m-%d")
        units, returns, gross, net, _countries, complete = fetch_sales_for_date(ds)
        if not complete:
            print(f"  [{ds}] incomplete read — keeping previous value")
        else:
            upsert_daily_sales(ds, units, returns, gross, net)
            if units > 0 or returns != 0:
                print(f"  [{ds}] +{units} sold, -{abs(returns)} returned, ${net:.2f} net")
        current += timedelta(days=1)

def refresh_recent_sales():
    """어제+오늘만 갱신 (폴링용, 전체 재스캔 방지).
    반환: (국가별 판매 합산, all_complete). all_complete=False 면 일부 날짜가
    부분 읽힘이라 알림/베이스라인 갱신을 건너뛰어야 함."""
    today = datetime.now().date()
    yesterday = today - timedelta(days=1)
    recent_countries = {}
    all_complete = True
    for d in [yesterday, today]:
        if shutdown_requested():
            return recent_countries, False
        ds = d.strftime("%Y-%m-%d")
        units, returns, gross, net, countries, complete = fetch_sales_for_date(ds)
        if not complete:
            # 실패한 읽기(data is None)로 DB 를 덮어쓰지 않음 (total_units 출렁임 방지)
            all_complete = False
            print(f"  [{ds}] incomplete read — keeping previous DB value")
            continue
        # 성공 응답이지만 일시적으로 0/부분값을 주는 경우 방어(Steam eventual consistency):
        # 이미 저장된 판매수보다 줄어드는 건 사실상 항상 오독 → 덮어쓰지 않고 유지
        with sqlite3.connect(DB_PATH, timeout=10) as _c:
            _row = _c.execute("SELECT units_sold FROM daily_sales WHERE date=?", (ds,)).fetchone()
        if _row is not None and units < _row[0]:
            all_complete = False
            print(f"  [{ds}] suspicious drop {_row[0]}->{units} — keeping previous DB value")
            continue
        upsert_daily_sales(ds, units, returns, gross, net)
        if units > 0 or returns != 0:
            print(f"  [{ds}] +{units} sold, -{abs(returns)} returned, ${net:.2f} net")
        for cc, cnt in countries.items():
            recent_countries[cc] = recent_countries.get(cc, 0) + cnt
    return recent_countries, all_complete

def fetch_wishlist_for_date(date_str):
    url = f"{FINANCIAL_BASE}/IPartnerFinancialsService/GetAppWishlistReporting/v001/?key={STEAM_FINANCIAL_KEY}&appid={APP_ID}&date={date_str}"
    data = fetch_json(url)
    if data and "response" in data:
        s = data["response"].get("wishlist_summary", data["response"].get("summary", {}))
        return {"adds": s.get("wishlist_adds", 0), "deletes": s.get("wishlist_deletes", 0),
                "purchases": s.get("wishlist_purchases", 0), "gifts": s.get("wishlist_gifts", 0)}
    return None

def fetch_wishlist_totals():
    """위시리스트 누적과 일별 확정치를 함께 갱신한다."""
    launch = datetime.strptime(WISHLIST_START_DATE, "%Y-%m-%d").date()
    # Steam wishlist financial reporting excludes the current, incomplete day.
    latest_confirmed = datetime.now().date() - timedelta(days=1)
    current = launch
    complete = True
    total = {
        "adds": 0,
        "deletes": 0,
        "purchases": 0,
        "gifts": 0,
        "opening_balance": WISHLIST_OPENING_BALANCE,
    }

    while current <= latest_confirmed and not shutdown_requested():
        ds = current.strftime("%Y-%m-%d")
        day = fetch_wishlist_for_date(ds)
        if day is None:
            complete = False
            print(f"  [{ds}] wishlist read incomplete — keeping previous total")
            current += timedelta(days=1)
            continue
        upsert_daily_wishlist(
            ds,
            day["adds"],
            day["deletes"],
            day["purchases"],
            day["gifts"],
        )
        total["adds"] += day["adds"]
        total["deletes"] += day["deletes"]
        total["purchases"] += day["purchases"]
        total["gifts"] += day["gifts"]
        current += timedelta(days=1)

    if shutdown_requested() or not complete:
        return None

    total["net"] = (
        total["opening_balance"]
        + total["adds"]
        - total["deletes"]
        - total["purchases"]
        - total["gifts"]
    )
    return total

# ========== TELEGRAM ==========
def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        return False

    import urllib.parse
    sent = 0
    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            body = urllib.parse.urlencode({
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
            }).encode()
            request = Request(url, data=body, headers={"User-Agent": "SteamDashboard/1.0"})
            with urlopen(request, timeout=10) as response:
                result = json.loads(response.read().decode())
            if result.get("ok"):
                sent += 1
            else:
                print(f"  [TG ERROR] Telegram rejected message for one recipient")
        except Exception as e:
            print(f"  [TG ERROR] {type(e).__name__}: {e}")
    print(f"  [TG] Sent to {sent}/{len(TELEGRAM_CHAT_IDS)} recipients")
    return sent == len(TELEGRAM_CHAT_IDS)

def send_startup_report():
    """시작 시 예쁜 요약 텔레그램"""
    if shutdown_requested():
        return
    totals = get_sales_totals()
    units, returns, gross, net = totals
    players = get_current_players()
    reviews = get_reviews()
    total_reviews = reviews.get("total_reviews", 0)
    total_positive = reviews.get("total_positive", 0)
    rate = round(total_positive / max(total_reviews, 1) * 100)
    launch_dt = datetime.strptime(LAUNCH_DATE, "%Y-%m-%d")
    delta = datetime.now() - launch_dt
    days_since = delta.days
    hours_since = int(delta.total_seconds() // 3600)

    daily = get_all_daily_sales()[-14:]
    daily_lines = ""
    for row in daily:
        d, u, r, g, n = row
        bar = "█" * u + ("░" * max(0, 30 - u))
        daily_lines += f"\n  {d[5:]}  {bar} {u}건 ${n:.0f}"

    with _data_lock:
        wl = dict(cached_wishlist)
    wl_net = wl.get("net", 0)
    wl_str = (
        f"위시리스트: +{wl.get('adds', 0)} / -{wl.get('deletes', 0)} / "
        f"구매전환 {wl.get('purchases', 0)} (순 ~{wl_net})"
    )

    msg = (
        f"📊 <b>{GAME_LABEL} Dashboard Online</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"\n"
        f"📊 <b>{GAME_STAGE_LABEL} D+{days_since} ({hours_since}h) 현황</b>\n"
        f"  판매: <b>{units}건</b> (환불 {abs(returns)}건)\n"
        f"  매출: ${gross:.0f} → 순수익 ${net:.0f}\n"
        f"  리뷰: {total_reviews}개 ({rate}% 긍정)\n"
        f"  동접: {players}명\n"
        f"\n"
        f"⭐ <b>위시리스트</b>\n"
        f"  {wl_str}\n"
        f"\n"
        f"📈 <b>최근 14일 판매</b>"
        f"{daily_lines}\n"
        f"\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🔔 {POLL_INTERVAL // 60}분 간격 모니터링 시작\n"
        f"  판매 발생 시 즉시 알림"
    )
    send_telegram(msg)

# ========== DATA COLLECTOR ==========
_data_lock = threading.Lock()
shutdown_event = threading.Event()

last_player_count = 0
last_review_count = 0
last_total_units = 0
last_wishlist_net = 0
last_recent_countries = {}  # 어제+오늘 국가별 판매 수 (diff 용)
peak_players = 0
cached_wishlist = {}
cached_sales_by_country = {}
cached_wishlist_by_country = {}
is_first_collection = True
last_full_scan_at = None

COUNTRY_FLAGS = {
    "US": "🇺🇸 미국", "CN": "🇨🇳 중국", "KR": "🇰🇷 한국", "DE": "🇩🇪 독일",
    "JP": "🇯🇵 일본", "GB": "🇬🇧 영국", "FR": "🇫🇷 프랑스", "CA": "🇨🇦 캐나다",
    "AU": "🇦🇺 호주", "BR": "🇧🇷 브라질", "RU": "🇷🇺 러시아", "TW": "🇹🇼 대만",
    "HK": "🇭🇰 홍콩", "IT": "🇮🇹 이탈리아", "ES": "🇪🇸 스페인", "NL": "🇳🇱 네덜란드",
    "SE": "🇸🇪 스웨덴", "PL": "🇵🇱 폴란드", "TR": "🇹🇷 튀르키예", "TH": "🇹🇭 태국",
    "AR": "🇦🇷 아르헨티나", "MX": "🇲🇽 멕시코", "CL": "🇨🇱 칠레", "IN": "🇮🇳 인도",
    "ID": "🇮🇩 인니", "PH": "🇵🇭 필리핀", "SG": "🇸🇬 싱가포르", "MY": "🇲🇾 말레이시아",
    "NZ": "🇳🇿 뉴질랜드", "AT": "🇦🇹 오스트리아", "CH": "🇨🇭 스위스", "BE": "🇧🇪 벨기에",
    "DK": "🇩🇰 덴마크", "NO": "🇳🇴 노르웨이", "FI": "🇫🇮 핀란드", "PT": "🇵🇹 포르투갈",
    "CZ": "🇨🇿 체코", "RO": "🇷🇴 루마니아", "UA": "🇺🇦 우크라이나", "ZA": "🇿🇦 남아공",
    "CO": "🇨🇴 콜롬비아", "PE": "🇵🇪 페루", "VN": "🇻🇳 베트남",
}

def refresh_heavy_metrics():
    """국가·위시리스트 전체 스캔. 최대 3시간에 한 번만 실행한다."""
    global cached_wishlist, cached_sales_by_country, cached_wishlist_by_country, last_full_scan_at

    next_wishlist_net = last_wishlist_net
    print("  Refreshing country data (3h full scan)...")
    try:
        new_sales_by_country = fetch_sales_by_country()
        new_wishlist_by_country = fetch_wishlist_by_country()
        with _data_lock:
            cached_sales_by_country = new_sales_by_country
            cached_wishlist_by_country = new_wishlist_by_country
        print(f"  Countries: {len(new_sales_by_country)} sales, {len(new_wishlist_by_country)} wishlist")
    except Exception as e:
        print(f"  [COUNTRY ERROR] {e}")

    if shutdown_requested():
        return next_wishlist_net

    print("  Refreshing wishlist data (3h full scan)...")
    try:
        new_wishlist = fetch_wishlist_totals()
        if shutdown_requested():
            return next_wishlist_net
        if new_wishlist is None:
            print("  Wishlist scan incomplete — preserving previous aggregate")
        else:
            with _data_lock:
                cached_wishlist = new_wishlist
            next_wishlist_net = new_wishlist.get("net", 0)
            save_wishlist_snapshot(
                new_wishlist["adds"],
                new_wishlist["deletes"],
                new_wishlist["purchases"],
                next_wishlist_net,
            )
    except Exception as e:
        print(f"  [WISHLIST ERROR] {e}")

    last_full_scan_at = datetime.now()
    return next_wishlist_net

def collect_data():
    global last_player_count, last_review_count, last_total_units, last_wishlist_net, peak_players, is_first_collection, last_recent_countries

    now = datetime.now().strftime('%H:%M:%S')
    print(f"[{now}] Collecting...")

    # 동접 + 리뷰
    players = get_current_players()
    reviews = get_reviews()
    save_player_count(players)

    total_reviews = reviews.get("total_reviews", 0)
    total_positive = reviews.get("total_positive", 0)
    total_negative = reviews.get("total_negative", 0)
    save_review_data(total_positive, total_negative, total_reviews)

    with _data_lock:
        if players > peak_players:
            peak_players = players

    # 판매 데이터 갱신
    recent_countries = {}
    sales_complete = True
    # API 지연 반영 대비 어제+오늘만 5분마다 갱신한다.
    print("  Refreshing sales (yesterday+today)...")
    recent_countries, sales_complete = refresh_recent_sales()
    totals = get_sales_totals()
    total_units = totals[0]
    net_revenue = totals[3]
    save_sales_snapshot(totals[0], totals[1], totals[3])

    # 비싼 전체 스캔은 시작 시 1회, 이후 3시간마다 실행한다.
    full_scan_due = (
        last_full_scan_at is None
        or (datetime.now() - last_full_scan_at).total_seconds() >= FULL_SCAN_INTERVAL
    )
    if full_scan_due:
        wl_net = refresh_heavy_metrics()
    else:
        remaining = max(
            0,
            FULL_SCAN_INTERVAL - int((datetime.now() - last_full_scan_at).total_seconds()),
        )
        print(f"  Skipping country/wishlist scan (next in {remaining // 60}min)")
        wl_net = last_wishlist_net

    # === 텔레그램 알림 ===

    if is_first_collection:
        # 첫 수집: baseline만 설정, 알림 스킵
        print(f"  [FIRST] Baseline set — units:{total_units}, wl:{wl_net}, reviews:{total_reviews}, players:{players}")
        last_wishlist_net = wl_net
        last_player_count = players
        last_review_count = total_reviews
        last_total_units = total_units
        # baseline 국가 데이터 세팅 (어제+오늘)
        last_recent_countries, _ = refresh_recent_sales()
        is_first_collection = False
        print(f"  Players: {players} | Reviews: {total_reviews} | Sales: {total_units} | Peak: {peak_players}")
        return

    # 위시리스트 변동 (5개 이상)
    if last_wishlist_net > 0 and abs(wl_net - last_wishlist_net) >= 5:
        diff = wl_net - last_wishlist_net
        direction = "📈 증가" if diff > 0 else "📉 감소"
        send_telegram(
            f"⭐ <b>위시리스트 {direction}!</b>\n"
            f"━━━━━━━━━━━━\n"
            f"  변동: {'+' if diff > 0 else ''}{diff}개\n"
            f"  누적 추가: {cached_wishlist.get('adds', 0)}개\n"
            f"  구매전환: {cached_wishlist.get('purchases', 0)}건\n"
            f"  현재 순위시: ~{wl_net}개"
        )
    last_wishlist_net = wl_net

    # 동접 급증
    if last_player_count > 0 and players > last_player_count * 1.5 and players >= 5:
        send_telegram(f"🚀 <b>동접 급증!</b>\n{last_player_count} → {players}명")

    # 새 리뷰
    if last_review_count > 0 and total_reviews > last_review_count:
        n = total_reviews - last_review_count
        send_telegram(f"📝 <b>새 리뷰 {n}개!</b>\n총 {total_reviews}개 (👍{total_positive} 👎{total_negative})")

    # 새 판매! (sales_complete 일 때만 — 부분 읽힘 회복으로 인한 유령 반복 알림 방지)
    if sales_complete and last_total_units > 0 and total_units > last_total_units:
        new_sales = total_units - last_total_units
        # 국가별 diff: 어떤 나라에서 새로 팔렸는지
        country_lines = ""
        if recent_countries and last_recent_countries:
            diffs = []
            for cc, cnt in recent_countries.items():
                prev = last_recent_countries.get(cc, 0)
                diff = cnt - prev
                if diff > 0:
                    label = COUNTRY_FLAGS.get(cc, f"🏳️ {cc}")
                    diffs.append((diff, label))
            diffs.sort(key=lambda x: -x[0])
            if diffs:
                lines = [f"  {label}  +{d}건" for d, label in diffs]
                country_lines = "\n\n📍 <b>구매 국가</b>\n" + "\n".join(lines)
        send_telegram(
            f"💰 <b>새 판매 +{new_sales}건!</b>\n"
            f"━━━━━━━━━━━━\n"
            f"  총 판매: {total_units}건\n"
            f"  순수익: ${net_revenue:.0f}\n"
            f"  동접: {players}명"
            f"{country_lines}"
        )

    last_player_count = players
    last_review_count = total_reviews
    # 베이스라인은 완전한 읽기일 때만 갱신 (부분 읽힘 값으로 기준선 오염 방지)
    if sales_complete:
        last_total_units = total_units
        if recent_countries:
            last_recent_countries = recent_countries

    print(f"  Players: {players} | Reviews: {total_reviews} | Sales: {total_units} | Peak: {peak_players}")

# ========== 데일리 리포트 (매일 KST 11:00) ==========
DIGEST_HOUR = 11  # 서버 TZ = KST(Asia/Seoul) → datetime.now() 그대로 사용
DIGEST_STATE_FILE = os.environ.get("STEAM_DIGEST_STATE_FILE", "daily_digest_state.txt")

def _digest_sent_today():
    try:
        with open(DIGEST_STATE_FILE) as f:
            return f.read().strip() == datetime.now().strftime("%Y-%m-%d")
    except OSError:
        return False

def _mark_digest_sent():
    try:
        with open(DIGEST_STATE_FILE, "w") as f:
            f.write(datetime.now().strftime("%Y-%m-%d"))
    except OSError as e:
        print(f"  [DIGEST] state write failed: {e}")

def get_units_since(hours):
    """sales_snapshots 기준 지난 N시간 판매 증가분 (스냅샷 부족 시 None)."""
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        cur = conn.execute("SELECT total_units FROM sales_snapshots ORDER BY timestamp DESC LIMIT 1").fetchone()
        base = conn.execute("SELECT total_units FROM sales_snapshots WHERE timestamp <= ? ORDER BY timestamp DESC LIMIT 1", (cutoff,)).fetchone()
        if base is None:
            base = conn.execute("SELECT total_units FROM sales_snapshots ORDER BY timestamp ASC LIMIT 1").fetchone()
    if not cur or not base:
        return None
    return cur[0] - base[0]

def _country_lines(cc_map, top_n=5):
    items = sorted(((cc, n) for cc, n in cc_map.items() if n > 0), key=lambda x: -x[1])[:top_n]
    if not items:
        return ""
    return "\n".join(f"  {COUNTRY_FLAGS.get(cc, '🏳️ ' + cc)}  {n}건" for cc, n in items)

def send_sales_daily_digest():
    """매일 KST 11시: 지난 24시간 + 오늘(자정~) 판매 요약 + 국가별."""
    today = datetime.now().date()
    yesterday = today - timedelta(days=1)

    yu = yn = 0; ycc = {}
    tu = tn = 0; tcc = {}
    yr = fetch_sales_for_date(yesterday.strftime("%Y-%m-%d"))
    if yr[5]:  # complete
        yu, yn, ycc = yr[0], yr[3], yr[4]
    tr = fetch_sales_for_date(today.strftime("%Y-%m-%d"))
    if tr[5]:
        tu, tn, tcc = tr[0], tr[3], tr[4]

    last24 = get_units_since(24)
    if last24 is None or last24 < 0:
        last24 = yu + tu  # 폴백: 어제+오늘 합산

    merged = {}
    for cc, n in ycc.items():
        merged[cc] = merged.get(cc, 0) + n
    for cc, n in tcc.items():
        merged[cc] = merged.get(cc, 0) + n
    cc_block = _country_lines(merged)

    totals = get_sales_totals()
    players = get_current_players()

    msg = (
        f"📊 <b>{GAME_LABEL} 데일리 리포트</b>\n"
        f"━━━━━━━━━━━━\n"
        f"  🗓 {today.strftime('%m/%d')} 오전 11시 기준\n"
        f"\n"
        f"  🔥 지난 24시간: <b>{last24}건</b>\n"
        f"  ☀️ 오늘(자정~): <b>{tu}건</b>  ${tn:.0f}\n"
        f"  🌙 어제 하루:   {yu}건  ${yn:.0f}\n"
    )
    if cc_block:
        msg += f"\n📍 <b>최근 24시간 구매 국가</b>\n{cc_block}\n"
    msg += (
        f"\n━━━━━━━━━━━━\n"
        f"  누적 판매: <b>{totals[0]}건</b> · 순수익 ${totals[3]:.0f}\n"
        f"  현재 동접: {players}명"
    )
    return send_telegram(msg)


def _wishlist_totals_from_rows(rows, include_opening_balance=False):
    totals = {
        "adds": sum(row["adds"] for row in rows),
        "deletes": sum(row["deletes"] for row in rows),
        "purchases": sum(row["purchases"] for row in rows),
        "gifts": sum(row["gifts"] for row in rows),
        "opening_balance": WISHLIST_OPENING_BALANCE if include_opening_balance else 0,
    }
    totals["net"] = (
        totals["opening_balance"]
        + totals["adds"]
        - totals["deletes"]
        - totals["purchases"]
        - totals["gifts"]
    )
    return totals


def _signed(value):
    return f"+{value}" if value >= 0 else str(value)


def send_wishlist_daily_digest(today=None):
    """미출시 게임용: 전날 확정 위시리스트 변화와 최근 7일·누적을 발송."""
    today = today or datetime.now().date()
    confirmed_date = today - timedelta(days=1)
    confirmed_date_str = confirmed_date.isoformat()

    # Steam 위시리스트 리포트는 당일 데이터가 미확정이므로 전날만 새로 확인한다.
    refreshed = fetch_wishlist_for_date(confirmed_date_str)
    if refreshed is not None:
        upsert_daily_wishlist(
            confirmed_date_str,
            refreshed["adds"],
            refreshed["deletes"],
            refreshed["purchases"],
            refreshed["gifts"],
        )

    rows = get_daily_wishlist(limit=10000)
    confirmed = next((row for row in rows if row["date"] == confirmed_date_str), None)
    week_start = confirmed_date - timedelta(days=6)
    weekly_rows = [
        row for row in rows
        if week_start.isoformat() <= row["date"] <= confirmed_date_str
    ]
    weekly = _wishlist_totals_from_rows(weekly_rows)

    cumulative = _wishlist_totals_from_rows(rows, include_opening_balance=True)

    if confirmed is None:
        print(f"  [DIGEST] Wishlist data for {confirmed_date_str} not confirmed yet; retrying")
        return False

    msg = (
        f"⭐ <b>{GAME_LABEL} 위시리스트 리포트</b>\n"
        f"━━━━━━━━━━━━\n"
        f"  🗓 {today.strftime('%m/%d')} 오전 11시 기준\n"
        f"  Steam 확정일: <b>{confirmed_date.strftime('%m/%d')}</b>\n"
        f"\n"
        f"⭐ <b>어제 변화</b>\n"
        f"  추가: <b>+{confirmed['adds']}개</b>\n"
        f"  삭제: -{confirmed['deletes']}개 · "
        f"구매·선물전환: -{confirmed['purchases'] + confirmed['gifts']}개\n"
        f"  순증: <b>{_signed(confirmed['net_change'])}개</b>\n"
        f"\n"
        f"📈 <b>최근 7일</b>\n"
        f"  추가 +{weekly['adds']} · 삭제 -{weekly['deletes']} · "
        f"구매·선물전환 -{weekly['purchases'] + weekly['gifts']}\n"
        f"  순증 <b>{_signed(weekly['net'])}개</b>\n"
        f"\n"
        f"━━━━━━━━━━━━\n"
        f"  현재 순위시: <b>~{cumulative.get('net', 0)}개</b>\n"
        f"  누적 추가: {cumulative.get('adds', 0)}개\n"
        f"  ※ 당일 변화는 다음 날 Steam 확정 후 반영"
    )
    return send_telegram(msg)


def send_daily_digest():
    if DAILY_DIGEST_MODE == "wishlist":
        return send_wishlist_daily_digest()
    return send_sales_daily_digest()


def daily_digest_loop():
    """매 분 확인, KST 11시대에 하루 한 번만 데일리 리포트 발송 (재시작에도 중복 방지)."""
    while not shutdown_event.is_set():
        now = datetime.now()
        if now.hour == DIGEST_HOUR and not _digest_sent_today():
            try:
                if send_daily_digest():
                    _mark_digest_sent()
                    print(f"[DIGEST] Daily report sent at {now.strftime('%Y-%m-%d %H:%M')}")
            except Exception as e:
                print(f"[DIGEST ERROR] {e}")
                traceback.print_exc()
        shutdown_event.wait(60)

def collector_loop():
    while not shutdown_event.is_set():
        try:
            collect_data()
        except Exception as e:
            print(f"[COLLECTOR ERROR] {e}")
            traceback.print_exc()
        shutdown_event.wait(POLL_INTERVAL)

# ========== HTML DASHBOARD ==========
DASHBOARD_HTML = '''<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Steam Metrics Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://cdn.jsdelivr.net">
<link href="https://fonts.googleapis.com/css2?family=Crimson+Pro:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css">
<style>
:root {
  --cave-black: #f7f3ee;
  --cave-deep: #fbf8f4;
  --cave-mid: #ffffff;
  --cave-surface: #ffffff;
  --cave-elevated: #fdfbf8;
  --cave-border: #e6ddd1;
  --cave-border-light: #d6c9b8;

  --wine-burgundy: #7d3540;
  --wine-merlot: #94404b;
  --wine-rose: #a8515c;

  --gold-aged: #9a7826;
  --gold-bright: #b08c2e;
  --gold-dim: #c4ab72;

  --green-vine: #3f7a44;
  --green-bright: #4d8f52;
  --green-dim: #8fb392;

  --red-alert: #b0424a;

  --text-primary: #2b2329;
  --text-secondary: #6a5c62;
  --text-tertiary: #8b7d84;
  --text-accent: #7d3540;

  --font-display: 'Crimson Pro', 'Pretendard Variable', Pretendard, Georgia, serif;
  --font-body: 'Pretendard Variable', Pretendard, -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
  --font-mono: 'JetBrains Mono', 'Pretendard Variable', Pretendard, monospace;

  --radius-sm: 8px;
  --radius-md: 12px;
  --radius-lg: 16px;

  --ease-out: cubic-bezier(0.25, 0.46, 0.45, 0.94);
}

* { margin: 0; padding: 0; box-sizing: border-box; }

body {
  font-family: var(--font-body);
  background: var(--cave-black);
  color: var(--text-primary);
  min-height: 100vh;
  overflow-x: hidden;
  line-height: 1.6;
  word-break: keep-all;
  overflow-wrap: break-word;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

/* Grain texture overlay */
body::after {
  content: '';
  position: fixed;
  inset: 0;
  pointer-events: none;
  z-index: 0;
  opacity: 0.02;
  background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)'/%3E%3C/svg%3E");
}

/* ─── HEADER ─── */
.header {
  position: relative;
  background: linear-gradient(165deg, #ffffff 0%, #fbf6f0 45%, #f6efe6 100%);
  padding: 28px 32px;
  display: flex;
  align-items: center;
  gap: 24px;
  border-bottom: 1px solid var(--cave-border);
  overflow: hidden;
}

.header::before {
  content: '';
  position: absolute;
  top: -50%;
  right: -10%;
  width: 400px;
  height: 400px;
  background: radial-gradient(circle, rgba(125,53,64,0.07) 0%, transparent 70%);
  pointer-events: none;
}

.header-img {
  width: 180px;
  border-radius: var(--radius-md);
  box-shadow: 0 6px 20px rgba(80,60,50,0.14), 0 0 0 1px rgba(154,120,38,0.18);
  flex-shrink: 0;
}

.header-info { flex: 1; min-width: 0; }

.header-info h1 {
  font-family: var(--font-display);
  font-size: 32px;
  font-weight: 600;
  color: var(--text-primary);
  letter-spacing: -0.02em;
  margin-bottom: 4px;
}

.header-info .subtitle {
  font-size: 13px;
  color: var(--text-tertiary);
  margin-bottom: 10px;
}

.header-info .price-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: linear-gradient(135deg, rgba(154,120,38,0.10), rgba(154,120,38,0.04));
  border: 1px solid rgba(154,120,38,0.28);
  color: var(--gold-aged);
  padding: 5px 14px;
  border-radius: 6px;
  font-family: var(--font-mono);
  font-size: 13px;
  font-weight: 500;
}

.game-switcher {
  position: relative;
  z-index: 1;
  flex: 0 1 260px;
  min-width: 190px;
}

.game-switcher label {
  display: block;
  color: var(--text-tertiary);
  font-family: var(--font-mono);
  font-size: 9px;
  letter-spacing: 0.08em;
  margin-bottom: 5px;
  text-transform: uppercase;
}

.game-switcher select {
  width: 100%;
  appearance: none;
  background: linear-gradient(150deg, #ffffff, #fbf7f2);
  border: 1px solid var(--cave-border-light);
  border-radius: 8px;
  color: var(--text-primary);
  cursor: pointer;
  font-family: var(--font-body);
  font-size: 12px;
  padding: 9px 32px 9px 12px;
}

.game-switcher::after {
  content: '⌄';
  color: var(--gold-aged);
  pointer-events: none;
  position: absolute;
  right: 12px;
  bottom: 8px;
}

.live-badge {
  margin-left: auto;
  text-align: right;
  flex-shrink: 0;
}

.live-indicator {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--green-vine);
  margin-bottom: 6px;
}

.live-dot {
  width: 7px;
  height: 7px;
  background: var(--green-bright);
  border-radius: 50%;
  box-shadow: 0 0 8px rgba(63,122,68,0.45);
  animation: livePulse 2.5s ease-in-out infinite;
}

@keyframes livePulse {
  0%, 100% { opacity: 1; box-shadow: 0 0 8px rgba(63,122,68,0.45); }
  50% { opacity: 0.4; box-shadow: 0 0 4px rgba(63,122,68,0.18); }
}

.live-badge .update-time {
  font-size: 12px;
  color: var(--text-tertiary);
  font-family: var(--font-mono);
}

.live-badge .poll-info {
  font-size: 11px;
  color: var(--text-tertiary);
  margin-top: 2px;
  opacity: 0.6;
}

.lang-toggle {
  display: inline-flex; gap: 0; border-radius: 4px; overflow: hidden;
  border: 1px solid var(--cave-border); font-size: 11px; margin-top: 4px;
}
.lang-toggle button {
  background: transparent; border: none; color: var(--text-tertiary);
  padding: 3px 8px; cursor: pointer; font-family: var(--font-mono);
  font-size: 11px; transition: all 0.2s;
}
.lang-toggle button.active {
  background: var(--cave-elevated); color: var(--text-primary);
}

/* ─── MAIN LAYOUT ─── */
.dashboard {
  max-width: 1400px;
  margin: 0 auto;
  padding: 24px 24px 48px;
}

/* ─── METRIC CARDS ─── */
.metrics-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 14px;
  margin-bottom: 14px;
}

.metric-card {
  position: relative;
  background: linear-gradient(170deg, var(--cave-mid) 0%, var(--cave-surface) 100%);
  border: 1px solid var(--cave-border);
  border-radius: var(--radius-md);
  padding: 18px 20px;
  transition: border-color 0.3s var(--ease-out), transform 0.2s var(--ease-out);
  overflow: hidden;
}

.metric-card:hover {
  border-color: var(--cave-border-light);
  transform: translateY(-2px);
}

.metric-card::after {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 2px;
  background: linear-gradient(90deg, transparent, rgba(154,120,38,0.35), transparent);
}

.metric-label {
  font-size: 12.5px;
  font-weight: 600;
  letter-spacing: 0.01em;
  color: var(--text-secondary);
  margin-bottom: 8px;
  line-height: 1.35;
}

.metric-value {
  font-family: var(--font-display);
  font-size: 32px;
  font-weight: 700;
  color: var(--text-primary);
  line-height: 1.1;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
  font-feature-settings: 'tnum' 1;
}

.metric-value.gold { color: var(--gold-aged); }
.metric-value.green { color: var(--green-vine); }

.metric-sub {
  font-size: 12px;
  color: var(--text-tertiary);
  margin-top: 6px;
  font-family: var(--font-mono);
  font-weight: 400;
}

/* ─── MEASURED EVIDENCE ─── */
.evidence-panel {
  background: linear-gradient(160deg, #ffffff, #fbf7f2);
  border: 1px solid var(--cave-border-light);
  border-radius: var(--radius-lg);
  padding: 20px;
  margin-bottom: 18px;
}

.evidence-head,
.chart-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.evidence-head { margin-bottom: 14px; }
.evidence-head h2 {
  font-family: var(--font-display);
  font-size: 22px;
  font-weight: 600;
  color: var(--text-accent);
}

.evidence-meta {
  color: var(--text-tertiary);
  font-family: var(--font-mono);
  font-size: 10px;
  line-height: 1.6;
  text-align: right;
}

.evidence-summary {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
  margin-bottom: 10px;
}

.evidence-stat,
.evidence-card {
  background: #fdfbf8;
  border: 1px solid #e6ddd1;
  border-radius: var(--radius-md);
}

.evidence-stat { padding: 14px 16px; }
.evidence-stat .label {
  color: var(--text-tertiary);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.evidence-stat .value {
  margin-top: 5px;
  color: var(--text-primary);
  font-family: var(--font-display);
  font-size: 25px;
  font-weight: 700;
}
.evidence-stat .sub {
  margin-top: 4px;
  color: var(--text-tertiary);
  font-family: var(--font-mono);
  font-size: 10px;
}

.evidence-grid {
  display: grid;
  grid-template-columns: 1.15fr 1fr;
  gap: 10px;
}
.evidence-card { padding: 16px; }
.evidence-card h3 {
  color: var(--text-secondary);
  font-family: var(--font-body);
  font-size: 12px;
  font-weight: 600;
  margin-bottom: 12px;
}

.funnel-row,
.reason-row {
  display: grid;
  align-items: center;
  gap: 10px;
  min-height: 28px;
  border-bottom: 1px solid rgba(214,201,184,0.6);
}
.funnel-row:last-child,
.reason-row:last-child { border-bottom: 0; }
.funnel-row { grid-template-columns: 56px 1fr 42px; }
.reason-row { grid-template-columns: minmax(120px, 1fr) 1.2fr 32px; }
.funnel-label,
.reason-label {
  color: var(--text-secondary);
  font-size: 11px;
}
.evidence-bar {
  height: 6px;
  overflow: hidden;
  border-radius: 999px;
  background: rgba(214,201,184,0.75);
}
.evidence-bar > span {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, var(--wine-merlot), var(--gold-aged));
}
.reason-row .evidence-bar > span {
  background: linear-gradient(90deg, var(--wine-merlot), var(--wine-rose));
}
.funnel-value,
.reason-value {
  color: var(--text-primary);
  font-family: var(--font-mono);
  font-size: 11px;
  text-align: right;
}

.live-delta {
  color: var(--text-secondary);
  font-family: var(--font-mono);
  font-size: 11px;
  margin-top: 12px;
  text-align: right;
}

/* ─── CHART CARDS ─── */
.charts-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 14px;
  margin-bottom: 14px;
}

.charts-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
  margin-bottom: 14px;
}

.chart-card {
  position: relative;
  background: linear-gradient(170deg, var(--cave-mid) 0%, var(--cave-surface) 100%);
  border: 1px solid var(--cave-border);
  border-radius: var(--radius-md);
  padding: 20px 22px;
  overflow: hidden;
  transition: border-color 0.3s var(--ease-out), transform 0.2s var(--ease-out);
}

.chart-card:hover {
  border-color: var(--cave-border-light);
  transform: translateY(-2px);
}

.chart-card::after {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 2px;
  background: linear-gradient(90deg, transparent, rgba(201,168,76,0.2), transparent);
}

.chart-card h3 {
  font-family: var(--font-display);
  font-size: 18px;
  font-weight: 500;
  color: var(--text-accent);
  margin-bottom: 16px;
  letter-spacing: -0.01em;
}

.chart-head h3 { margin-bottom: 4px; }
.chart-summary {
  color: var(--text-tertiary);
  font-family: var(--font-mono);
  font-size: 11px;
  text-align: right;
  white-space: nowrap;
}

.range-toggle {
  display: inline-flex;
  overflow: hidden;
  border: 1px solid var(--cave-border);
  border-radius: 7px;
  margin-bottom: 12px;
}
.range-toggle button {
  border: 0;
  border-right: 1px solid var(--cave-border);
  background: transparent;
  color: var(--text-tertiary);
  cursor: pointer;
  font-family: var(--font-mono);
  font-size: 10px;
  padding: 6px 10px;
}
.range-toggle button:last-child { border-right: 0; }
.range-toggle button.active {
  background: var(--cave-elevated);
  color: var(--gold-aged);
}

.chart-card canvas {
  width: 100% !important;
}

/* ─── WISHLIST ATTRIBUTION ─── */
.source-metrics {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
  margin: 14px 0;
}

.source-stat {
  background: #fdfbf8;
  border: 1px solid #e6ddd1;
  border-radius: var(--radius-sm);
  padding: 11px 12px;
}

.source-stat .label {
  color: var(--text-tertiary);
  font-size: 9px;
  font-weight: 600;
  letter-spacing: 0.05em;
  text-transform: uppercase;
}

.source-stat .value {
  color: var(--text-primary);
  font-family: var(--font-display);
  font-size: 22px;
  font-weight: 700;
  margin-top: 4px;
}

.source-row {
  display: grid;
  grid-template-columns: minmax(110px, 1fr) 1.4fr 48px;
  align-items: center;
  gap: 10px;
  min-height: 29px;
  border-bottom: 1px solid rgba(214,201,184,0.6);
}

.source-row:last-child { border-bottom: 0; }
.source-subhead {
  color: var(--text-tertiary);
  font-size: 9px;
  font-weight: 600;
  letter-spacing: 0.06em;
  margin: 13px 0 5px;
  text-transform: uppercase;
}

.source-label {
  color: var(--text-secondary);
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.source-bar {
  height: 6px;
  overflow: hidden;
  border-radius: 999px;
  background: rgba(214,201,184,0.75);
}

.source-bar > span {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, var(--wine-merlot), var(--gold-aged));
}

.source-value {
  color: var(--text-primary);
  font-family: var(--font-mono);
  font-size: 10px;
  text-align: right;
}

.utm-row {
  display: grid;
  grid-template-columns: minmax(110px, 1fr) 72px 72px;
  align-items: center;
  gap: 10px;
  min-height: 29px;
  border-bottom: 1px solid rgba(214,201,184,0.6);
}

.utm-row:last-child { border-bottom: 0; }
.utm-metric {
  color: var(--text-secondary);
  font-family: var(--font-mono);
  font-size: 10px;
  text-align: right;
}

.utm-metric.wishlist { color: var(--gold-aged); }

.source-note {
  color: var(--text-tertiary);
  border-top: 1px solid rgba(214,201,184,0.75);
  font-size: 10px;
  line-height: 1.55;
  margin-top: 12px;
  padding-top: 10px;
}

.snapshot-empty {
  color: var(--text-tertiary);
  font-size: 12px;
  font-style: italic;
  padding: 28px 0;
}

/* ─── SECTION TITLES ─── */
.section-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 14px;
  margin-top: 8px;
}

.section-header h2 {
  font-family: var(--font-display);
  font-size: 22px;
  font-weight: 500;
  color: var(--text-accent);
  letter-spacing: -0.01em;
}

.section-header::after {
  content: '';
  flex: 1;
  height: 1px;
  background: linear-gradient(90deg, var(--cave-border), transparent);
}

/* ─── COUNTRY TABLES ─── */
.country-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
  margin-bottom: 14px;
}

.country-card {
  position: relative;
  background: linear-gradient(170deg, var(--cave-mid) 0%, var(--cave-surface) 100%);
  border: 1px solid var(--cave-border);
  border-radius: var(--radius-md);
  padding: 20px 22px;
  transition: border-color 0.3s var(--ease-out), transform 0.2s var(--ease-out);
}

.country-card:hover {
  border-color: var(--cave-border-light);
  transform: translateY(-2px);
}

.country-card::after {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 2px;
  background: linear-gradient(90deg, transparent, rgba(201,168,76,0.2), transparent);
}

.country-card > div { overflow-x: auto; }

.country-card h3 {
  font-family: var(--font-display);
  font-size: 18px;
  font-weight: 500;
  color: var(--text-accent);
  margin-bottom: 14px;
}

.country-table {
  width: 100%;
  border-collapse: collapse;
}

.country-table tr {
  border-bottom: 1px solid rgba(214,201,184,0.65);
  transition: background 0.2s;
}

.country-table tr:hover {
  background: rgba(125,53,64,0.05);
}

.country-table td {
  padding: 7px 0;
  font-size: 13px;
}

.country-table .cc {
  font-weight: 600;
  color: var(--text-secondary);
  width: 100px;
}

.country-table .bar-cell {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--wine-rose);
  letter-spacing: -0.05em;
}

.country-table .val {
  text-align: right;
  font-family: var(--font-mono);
  font-weight: 500;
  color: var(--text-primary);
  width: 60px;
}

/* ─── REVIEWS ─── */
.reviews-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 10px;
}

.review-card {
  background: linear-gradient(170deg, var(--cave-mid) 0%, var(--cave-surface) 100%);
  border: 1px solid var(--cave-border);
  border-radius: var(--radius-md);
  padding: 18px 22px;
  min-width: 0;
  transition: border-color 0.3s var(--ease-out), transform 0.2s var(--ease-out);
}

.review-card:hover {
  border-color: var(--cave-border-light);
  transform: translateY(-2px);
}

.review-header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}

.review-thumb {
  font-size: 18px;
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 6px;
  flex-shrink: 0;
}

.review-thumb.up { background: rgba(63,122,68,0.12); }
.review-thumb.down { background: rgba(176,66,74,0.12); }

.review-author {
  font-weight: 600;
  font-size: 13px;
  color: var(--text-secondary);
}

.review-playtime {
  margin-left: auto;
  font-size: 12px;
  font-family: var(--font-mono);
  color: var(--text-tertiary);
}

.review-text {
  font-size: 13.5px;
  line-height: 1.7;
  color: var(--text-secondary);
  overflow-wrap: anywhere;
  max-height: 80px;
  overflow: hidden;
  -webkit-mask-image: linear-gradient(to bottom, black 60%, transparent 100%);
  mask-image: linear-gradient(to bottom, black 60%, transparent 100%);
}

/* ─── STATUS BAR ─── */
.status-bar {
  position: fixed;
  bottom: 0;
  left: 0;
  right: 0;
  background: rgba(247,243,238,0.92);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  padding: 8px 24px;
  display: flex;
  align-items: center;
  gap: 20px;
  font-size: 11px;
  font-family: var(--font-mono);
  color: var(--text-tertiary);
  border-top: 1px solid var(--cave-border);
  z-index: 100;
}

.status-bar .dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  margin-right: 4px;
  vertical-align: middle;
}

.status-bar .dot.on { background: var(--green-bright); box-shadow: 0 0 4px rgba(108,192,112,0.4); }
.status-bar .dot.off { background: var(--red-alert); }

/* ─── LOADING SHIMMER ─── */
@keyframes shimmer {
  0% { background-position: -200px 0; }
  100% { background-position: 200px 0; }
}

.metric-value.loading {
  background: linear-gradient(90deg, var(--cave-surface) 0%, var(--cave-elevated) 40%, var(--cave-surface) 80%);
  background-size: 400px 100%;
  animation: shimmer 1.8s ease-in-out infinite;
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}

/* ─── ANIMATIONS ─── */
.metric-card, .chart-card, .country-card, .review-card {
  animation: fadeUp 0.5s var(--ease-out) both;
}

@keyframes fadeUp {
  from { opacity: 0; transform: translateY(12px); }
  to { opacity: 1; transform: translateY(0); }
}

.metrics-grid .metric-card:nth-child(1) { animation-delay: 0.05s; }
.metrics-grid .metric-card:nth-child(2) { animation-delay: 0.1s; }
.metrics-grid .metric-card:nth-child(3) { animation-delay: 0.15s; }
.metrics-grid .metric-card:nth-child(4) { animation-delay: 0.2s; }
.metrics-grid + .metrics-grid .metric-card:nth-child(1) { animation-delay: 0.25s; }
.metrics-grid + .metrics-grid .metric-card:nth-child(2) { animation-delay: 0.3s; }
.metrics-grid + .metrics-grid .metric-card:nth-child(3) { animation-delay: 0.35s; }
.metrics-grid + .metrics-grid .metric-card:nth-child(4) { animation-delay: 0.4s; }

/* ─── MOBILE RESPONSIVE ─── */
@media (max-width: 1024px) {
  .metrics-grid { grid-template-columns: repeat(2, 1fr); }
  .evidence-summary { grid-template-columns: repeat(2, 1fr); }
  .evidence-grid { grid-template-columns: 1fr; }
  .charts-row { grid-template-columns: 1fr; }
  .country-grid { grid-template-columns: 1fr; }
}

@media (max-width: 768px) {
  .header { padding: 20px 20px; gap: 16px; }
  .header-img { width: 140px; }
  .header-info h1 { font-size: 26px; }
  .game-switcher { flex-basis: 210px; min-width: 170px; }
  .dashboard { padding: 20px 16px 72px; }
  .chart-card canvas { min-height: 160px; }
  .country-table .cc { width: 70px; font-size: 12px; }
}

@media (max-width: 640px) {
  .header {
    flex-direction: column;
    align-items: flex-start;
    padding: 16px;
    gap: 14px;
  }

  .header-img {
    width: 100%;
    max-width: none;
    height: auto;
    max-height: 160px;
    object-fit: cover;
    border-radius: var(--radius-sm);
  }

  .header-info h1 { font-size: 22px; }

  .game-switcher {
    flex: none;
    min-width: 0;
    width: 100%;
  }

  .live-badge {
    margin-left: 0;
    display: flex;
    align-items: center;
    gap: 12px;
    width: 100%;
  }

  .live-badge .poll-info { display: none; }

  .dashboard { padding: 14px 10px 72px; }

  .metrics-grid {
    grid-template-columns: 1fr 1fr;
    gap: 10px;
  }

  .metric-card { padding: 14px 16px; }
  .metric-value { font-size: 26px; }
  .metric-label { font-size: 11.5px; }
  .metric-sub { font-size: 11px; }

  .chart-card { padding: 16px 14px; }
  .chart-card canvas { min-height: 150px; }
  .source-row { grid-template-columns: minmax(90px, 1fr) 1fr 42px; }
  .evidence-panel { padding: 15px; }
  .evidence-head { flex-direction: column; gap: 4px; }
  .evidence-meta { text-align: left; }
  .evidence-summary { gap: 8px; }
  .evidence-stat { padding: 12px; }
  .evidence-stat .value { font-size: 22px; }

  .section-header { padding: 0 4px; }
  .section-header h2 { font-size: 17px; }

  .review-card { padding: 14px 16px; }

  .status-bar {
    padding: 6px 12px;
    gap: 10px;
    font-size: 10px;
  }

  .status-bar span:nth-child(1) { display: none; }
}

@media (max-width: 380px) {
  .metrics-grid { grid-template-columns: 1fr; }
  .evidence-summary { grid-template-columns: 1fr; }
  .source-metrics { grid-template-columns: 1fr; }
  .header-img { max-height: 120px; }
}
</style>
</head>
<body>

<div class="header">
  <img id="headerImg" class="header-img" src="" alt="" />
  <div class="header-info">
    <h1 id="gameName">Loading...</h1>
    <div class="subtitle" id="gameDev"></div>
    <div class="price-badge" id="gamePrice"></div>
  </div>
  <div class="game-switcher">
    <label for="gameSelector" data-i18n="gameSelector">게임 선택</label>
    <select id="gameSelector" onchange="switchGame(this.value)" aria-label="Game selector"></select>
  </div>
  <div class="live-badge">
    <div class="live-indicator"><span class="live-dot"></span>LIVE</div>
    <div class="update-time" id="lastUpdate">--</div>
    <div class="poll-info" data-i18n="pollInfo">5분 감시 · 핵심지표 3시간</div>
    <div class="lang-toggle" id="langToggle">
      <button id="langKo" onclick="setLang('ko')">KR</button>
      <button id="langEn" onclick="setLang('en')">EN</button>
    </div>
  </div>
</div>

<div class="dashboard">

  <!-- Row 1: Sales & Revenue -->
  <div class="metrics-grid">
    <div class="metric-card">
      <div class="metric-label" data-i18n="totalSales">총 판매</div>
      <div class="metric-value gold loading" id="totalSales">--</div>
      <div class="metric-sub" id="salesSub"></div>
    </div>
    <div class="metric-card">
      <div class="metric-label" data-i18n="netRevenue">순수익</div>
      <div class="metric-value green loading" id="netRevenue">--</div>
      <div class="metric-sub" id="revenueSub"></div>
    </div>
    <div class="metric-card">
      <div class="metric-label" data-i18n="playersOnline">현재 동접</div>
      <div class="metric-value loading" id="currentPlayers">--</div>
      <div class="metric-sub" id="playerChange"></div>
    </div>
    <div class="metric-card">
      <div class="metric-label" data-i18n="peakPlayers">피크 동접</div>
      <div class="metric-value loading" id="peakPlayers">--</div>
      <div class="metric-sub" data-i18n="sessionHigh">세션 최고치</div>
    </div>
  </div>

  <!-- Row 2: Reviews & Wishlist -->
  <div class="metrics-grid">
    <div class="metric-card">
      <div class="metric-label" data-i18n="reviews">리뷰</div>
      <div class="metric-value loading" id="totalReviews">--</div>
      <div class="metric-sub" id="reviewRatio"></div>
    </div>
    <div class="metric-card">
      <div class="metric-label" data-i18n="positiveRate">긍정률</div>
      <div class="metric-value green loading" id="positiveRate">--</div>
      <div class="metric-sub" id="reviewScore"></div>
    </div>
    <div class="metric-card">
      <div class="metric-label" data-i18n="wishlists">위시리스트</div>
      <div class="metric-value loading" id="wishlistNet">--</div>
      <div class="metric-sub" id="wishlistSub"></div>
    </div>
    <div class="metric-card">
      <div class="metric-label" data-i18n="refundRate">환불률</div>
      <div class="metric-value loading" id="refundRate">--</div>
      <div class="metric-sub" data-i18n="refundSales">환불 / 판매</div>
    </div>
  </div>

  <!-- Login-only Steamworks measurements + automatically calculated periods -->
  <div class="evidence-panel">
    <div class="evidence-head">
      <h2 data-i18n="measuredFacts">핵심 실측</h2>
      <div class="evidence-meta">
        <div id="snapshotVerified">Steamworks 확인: --</div>
        <div id="fullScanUpdated">자동 전체집계: --</div>
      </div>
    </div>
    <div class="evidence-summary">
      <div class="evidence-stat">
        <div class="label" data-i18n="averagePlaytime">평균 플레이타임</div>
        <div class="value" id="averagePlaytime">--</div>
        <div class="sub" id="playtimeSample">--</div>
      </div>
      <div class="evidence-stat">
        <div class="label" data-i18n="medianPlaytime">중앙 플레이타임</div>
        <div class="value" id="medianPlaytime">--</div>
        <div class="sub" data-i18n="steamworksMeasured">Steamworks 실측</div>
      </div>
      <div class="evidence-stat">
        <div class="label" data-i18n="last7Days">최근 7일</div>
        <div class="value" id="sales7Value">--</div>
        <div class="sub" id="sales7Sub">--</div>
      </div>
      <div class="evidence-stat">
        <div class="label" data-i18n="last30Days">최근 30일</div>
        <div class="value" id="sales30Value">--</div>
        <div class="sub" id="sales30Sub">--</div>
      </div>
    </div>
    <div class="evidence-grid">
      <div class="evidence-card">
        <h3 data-i18n="playtimeFunnel">플레이타임 도달률</h3>
        <div id="playtimeFunnel"></div>
      </div>
      <div class="evidence-card">
        <h3 id="refundReasonsTitle" data-i18n="refundReasons">환불 사유</h3>
        <div id="refundReasons"></div>
      </div>
    </div>
    <div class="live-delta" id="liveDelta">최근 3시간 변화 수집 중</div>
  </div>

  <!-- Daily wishlist movement + dated Steamworks marketing snapshot -->
  <div class="section-header"><h2 data-i18n="wishlistGrowth">위시리스트 성장·유입</h2></div>

  <div class="charts-row">
    <div class="chart-card">
      <div class="chart-head">
        <h3 data-i18n="dailyWishlist">일별 위시리스트 변화</h3>
        <div class="chart-summary" id="wishlistTrendSummary">--</div>
      </div>
      <canvas id="wishlistTrendChart" height="235"></canvas>
    </div>
    <div class="chart-card">
      <div class="chart-head">
        <h3 data-i18n="trafficAttribution">유입·전환 귀속</h3>
        <div class="chart-summary" id="marketingVerified">--</div>
      </div>
      <div id="marketingSnapshot">
        <div class="source-metrics">
          <div class="source-stat">
            <div class="label" data-i18n="storeVisits">스토어 방문</div>
            <div class="value" id="trafficVisits">--</div>
          </div>
          <div class="source-stat">
            <div class="label" data-i18n="utmVisits">UTM 방문</div>
            <div class="value" id="utmVisits">--</div>
          </div>
          <div class="source-stat">
            <div class="label" data-i18n="utmWishlists">UTM 위시리스트</div>
            <div class="value" id="utmWishlists">--</div>
          </div>
        </div>
        <div class="source-subhead" data-i18n="trafficSources">스토어 방문 출처</div>
        <div id="trafficSources"></div>
        <div class="source-subhead" data-i18n="utmSources">UTM 매체별 전환</div>
        <div id="utmSources"></div>
        <div class="source-note" id="trafficSourceNote"></div>
      </div>
    </div>
  </div>

  <!-- Charts -->
  <div class="section-header"><h2 data-i18n="salesPerf">판매 현황</h2></div>

  <div class="range-toggle" aria-label="Timeline range">
    <button type="button" data-range="7" onclick="setTimelineRange('7')">7D</button>
    <button type="button" class="active" data-range="30" onclick="setTimelineRange('30')">30D</button>
    <button type="button" data-range="all" onclick="setTimelineRange('all')">ALL</button>
  </div>

  <div class="charts-row">
    <div class="chart-card">
      <div class="chart-head">
        <h3 data-i18n="cumSales">누적 판매</h3>
        <div class="chart-summary" id="cumSalesSummary">--</div>
      </div>
      <canvas id="cumulativeSalesChart" height="200"></canvas>
    </div>
    <div class="chart-card">
      <div class="chart-head">
        <h3 data-i18n="cumRevenue">누적 순수익</h3>
        <div class="chart-summary" id="cumRevenueSummary">--</div>
      </div>
      <canvas id="cumulativeRevenueChart" height="200"></canvas>
    </div>
  </div>

  <div class="charts-row">
    <div class="chart-card">
      <h3 data-i18n-html="dailySales">일별 판매 &amp; 수익</h3>
      <canvas id="salesChart" height="220"></canvas>
    </div>
    <div class="chart-card">
      <h3 data-i18n="playerActivity">동접자 추이</h3>
      <canvas id="playerChart" height="220"></canvas>
    </div>
  </div>

  <!-- Country Breakdown -->
  <div class="section-header"><h2 data-i18n="geoBreakdown">국가별 현황</h2></div>

  <div class="country-grid">
    <div class="country-card">
      <h3 data-i18n="salesByCountry">국가별 판매</h3>
      <div id="salesByCountry"></div>
    </div>
    <div class="country-card">
      <h3 data-i18n="wlByCountry">국가별 위시리스트</h3>
      <div id="wishlistByCountry"></div>
    </div>
  </div>

  <!-- Reviews -->
  <div class="section-header"><h2 data-i18n="recentReviews">최근 리뷰</h2></div>

  <div class="reviews-grid" id="recentReviews"></div>

</div>

<div class="status-bar">
  <span>Steam App ID: ''' + APP_ID + '''</span>
  <span>Watch: 5min · Full: 3h</span>
  <span>Telegram: <span class="dot" id="tgDot"></span> <span id="tgStatus"></span></span>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<script>
let playerChart, salesChart, cumulativeSalesChart, cumulativeRevenueChart, wishlistTrendChart;
let timelineData = [];
let timelineRange = '30';
let gameOptions = [];
let curLang = localStorage.getItem('dashLang') || 'ko';

const i18n = {
  ko: {
    gameSelector: '게임 선택',
    totalSales: '총 판매', netRevenue: '순수익', playersOnline: '현재 동접',
    peakPlayers: '피크 동접', sessionHigh: '세션 최고치', reviews: '리뷰',
    positiveRate: '긍정률', wishlists: '위시리스트', refundRate: '환불률',
    refundSales: '환불 / 판매', salesPerf: '판매 현황',
    measuredFacts: '핵심 실측', averagePlaytime: '평균 플레이타임',
    medianPlaytime: '중앙 플레이타임', steamworksMeasured: 'Steamworks 실측',
    last7Days: '최근 7일', last30Days: '최근 30일',
    playtimeFunnel: '플레이타임 도달률', refundReasons: '환불 사유',
    wishlistGrowth: '위시리스트 성장·유입', dailyWishlist: '일별 위시리스트 변화',
    trafficAttribution: '유입·전환 귀속', storeVisits: '스토어 방문',
    utmVisits: 'UTM 방문', utmWishlists: 'UTM 위시리스트',
    trafficSources: '스토어 방문 출처', utmSources: 'UTM 매체별 전환',
    cumSales: '누적 판매', cumRevenue: '누적 순수익', dailySales: '일별 판매 &amp; 수익',
    playerActivity: '동접자 추이', geoBreakdown: '국가별 현황',
    salesByCountry: '국가별 판매', wlByCountry: '국가별 위시리스트',
    recentReviews: '최근 리뷰', pollInfo: '5분 감시 · 핵심지표 3시간',
    collecting: '데이터 수집 중...', noChange: '— 변동 없음',
    refunds: '환불', grossLabel: '총매출', beforeFees: '수수료 전',
    conversion: '구매전환', hours: '시간',
    chartCumSales: '누적 판매 (건)', chartCumRev: '누적 순수익 ($)',
    chartSales: '판매 (건)', chartRefunds: '환불', chartNetRev: '순수익 ($)',
    chartWishlistAdds: '추가', chartWishlistDeletes: '삭제',
    chartWishlistPurchases: '구매·선물전환', chartWishlistNet: '순증가',
    chartUnits: '건수', chartRevenue: '수익 ($)', chartPlayers: '동접',
    chartSalesAxis: '판매 (건)', chartRevenueAxis: '수익 ($)'
  },
  en: {
    gameSelector: 'Select Game',
    totalSales: 'Total Sales', netRevenue: 'Net Revenue', playersOnline: 'Players Online',
    peakPlayers: 'Peak Players', sessionHigh: 'Session high', reviews: 'Reviews',
    positiveRate: 'Positive Rate', wishlists: 'Wishlists', refundRate: 'Refund Rate',
    refundSales: 'returns / sales', salesPerf: 'Sales Performance',
    measuredFacts: 'Measured Facts', averagePlaytime: 'Average Playtime',
    medianPlaytime: 'Median Playtime', steamworksMeasured: 'Steamworks measurement',
    last7Days: 'Last 7 Days', last30Days: 'Last 30 Days',
    playtimeFunnel: 'Playtime Reach', refundReasons: 'Refund Reasons',
    wishlistGrowth: 'Wishlist Growth & Acquisition', dailyWishlist: 'Daily Wishlist Movement',
    trafficAttribution: 'Traffic & Conversion Attribution', storeVisits: 'Store Visits',
    utmVisits: 'UTM Visits', utmWishlists: 'UTM Wishlists',
    trafficSources: 'Store Traffic Sources', utmSources: 'UTM Conversions by Source',
    cumSales: 'Cumulative Sales', cumRevenue: 'Cumulative Net Revenue', dailySales: 'Daily Sales &amp; Revenue',
    playerActivity: 'Player Activity', geoBreakdown: 'Geographic Breakdown',
    salesByCountry: 'Sales by Country', wlByCountry: 'Wishlists by Country',
    recentReviews: 'Recent Reviews', pollInfo: '5min watch · 3h full refresh',
    collecting: 'Collecting data...', noChange: '— no change',
    refunds: 'refunds', grossLabel: 'gross', beforeFees: 'before fees',
    conversion: 'conv.', hours: 'h',
    chartCumSales: 'Cumulative Sales', chartCumRev: 'Net Revenue ($)',
    chartSales: 'Sales', chartRefunds: 'Refunds', chartNetRev: 'Net Revenue ($)',
    chartWishlistAdds: 'Adds', chartWishlistDeletes: 'Deletes',
    chartWishlistPurchases: 'Purchases/Gifts', chartWishlistNet: 'Net Growth',
    chartUnits: 'Units', chartRevenue: 'Revenue ($)', chartPlayers: 'Players',
    chartSalesAxis: 'Sales', chartRevenueAxis: 'Revenue ($)'
  }
};

function T(key) { return (i18n[curLang] || i18n.ko)[key] || key; }

function renderGameSelector(config) {
  const selector = document.getElementById('gameSelector');
  const settings = config || {};
  gameOptions = Array.isArray(settings.games) ? settings.games : [];
  selector.replaceChildren();
  gameOptions.forEach(function(game) {
    const option = document.createElement('option');
    option.value = String(game.app_id);
    option.textContent = game.name;
    option.selected = String(game.app_id) === String(settings.active_app_id);
    selector.appendChild(option);
  });
}

function switchGame(appId) {
  const target = gameOptions.find(function(game) {
    return String(game.app_id) === String(appId);
  });
  if (!target || String(target.port) === String(window.location.port || 80)) return;
  const url = new URL(window.location.href);
  url.port = String(target.port);
  url.pathname = '/';
  url.search = '';
  url.hash = '';
  window.location.assign(url.toString());
}

function applyStaticLabels() {
  document.querySelectorAll('[data-i18n]').forEach(function(el) {
    el.textContent = T(el.getAttribute('data-i18n'));
  });
  document.querySelectorAll('[data-i18n-html]').forEach(function(el) {
    el.innerHTML = T(el.getAttribute('data-i18n-html'));
  });
}

function updateToggleButtons() {
  document.getElementById('langKo').className = curLang === 'ko' ? 'active' : '';
  document.getElementById('langEn').className = curLang === 'en' ? 'active' : '';
}

function setLang(lang) {
  curLang = lang;
  localStorage.setItem('dashLang', lang);
  applyStaticLabels();
  updateToggleButtons();
  rebuildCharts();
  fetchData();
}

const chartColors = {
  gold: '#9a7826',
  goldFill: 'rgba(154,120,38,0.13)',
  green: '#3f7a44',
  greenFill: 'rgba(63,122,68,0.11)',
  red: '#b0424a',
  purple: '#7a4f7a',
  purpleFill: 'rgba(122,79,122,0.12)',
  grid: 'rgba(43,35,41,0.09)',
  tick: '#8b7d84',
  legend: '#6a5c62'
};

function rebuildCharts() {
  if (cumulativeSalesChart) cumulativeSalesChart.destroy();
  if (cumulativeRevenueChart) cumulativeRevenueChart.destroy();
  if (salesChart) salesChart.destroy();
  if (playerChart) playerChart.destroy();
  if (wishlistTrendChart) wishlistTrendChart.destroy();
  initCharts();
  renderTimelineCharts();
}

function initCharts() {
  const isMobile = window.innerWidth <= 768;
  const pr = isMobile ? 2 : 4;
  const phr = isMobile ? 3 : 6;
  const base = {
    responsive: true,
    animation: { duration: 500, easing: 'easeOutQuart' },
    interaction: { mode: 'index', intersect: false },
    scales: {
      x: {
        ticks: { color: chartColors.tick, maxTicksLimit: 12, font: { family: "'JetBrains Mono', 'Pretendard Variable', Pretendard, monospace", size: 10 } },
        grid: { color: chartColors.grid, lineWidth: 0.5 },
        border: { display: false }
      },
      y: {
        ticks: { color: chartColors.tick, font: { family: "'JetBrains Mono', 'Pretendard Variable', Pretendard, monospace", size: 10 } },
        grid: { color: chartColors.grid, lineWidth: 0.5 },
        border: { display: false },
        beginAtZero: true
      }
    },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: 'rgba(255,255,255,0.97)',
        borderColor: 'rgba(154,120,38,0.3)',
        titleColor: '#2b2329',
        bodyColor: '#3d343a',
        footerColor: '#6a5c62',
        borderWidth: 1,
        titleFont: { family: "'Pretendard Variable', Pretendard, sans-serif", weight: '600' },
        bodyFont: { family: "'JetBrains Mono', 'Pretendard Variable', Pretendard, monospace", size: 12 },
        padding: 12,
        cornerRadius: 8,
        displayColors: true,
        boxPadding: 4
      }
    }
  };

  const cumulativeOptions = function(valueFormatter) {
    return {
      ...base,
      plugins: {
        ...base.plugins,
        tooltip: {
          ...base.plugins.tooltip,
          callbacks: {
            label: function(context) {
              return context.dataset.label + ': ' + valueFormatter(context.raw);
            },
            afterLabel: function(context) {
              const values = context.chart.data.datasets[0].data;
              const delta = Number(context.raw || 0) - Number(values[0] || 0);
              return 'Δ ' + (delta >= 0 ? '+' : '') + valueFormatter(delta);
            }
          }
        }
      },
      scales: {
        x: { ...base.scales.x, ticks: { ...base.scales.x.ticks, maxTicksLimit: 10 } },
        y: { ...base.scales.y, beginAtZero: false }
      }
    };
  };

  cumulativeSalesChart = new Chart(document.getElementById('cumulativeSalesChart'), {
    type: 'line',
    data: {
      labels: [],
      datasets: [{
        label: T('chartCumSales'),
        data: [],
        borderColor: chartColors.gold,
        backgroundColor: chartColors.goldFill,
        fill: true,
        tension: 0.25,
        pointRadius: Math.max(1, pr - 2),
        pointHoverRadius: phr,
        pointBackgroundColor: chartColors.gold,
        pointBorderColor: 'transparent',
        borderWidth: 2.5
      }]
    },
    options: cumulativeOptions(function(value) { return Number(value || 0).toLocaleString(); })
  });

  cumulativeRevenueChart = new Chart(document.getElementById('cumulativeRevenueChart'), {
    type: 'line',
    data: {
      labels: [],
      datasets: [{
        label: T('chartCumRev'),
        data: [],
        borderColor: chartColors.green,
        backgroundColor: chartColors.greenFill,
        fill: true,
        tension: 0.25,
        pointRadius: Math.max(1, pr - 2),
        pointHoverRadius: phr,
        pointBackgroundColor: chartColors.green,
        pointBorderColor: 'transparent',
        borderWidth: 2.5
      }]
    },
    options: cumulativeOptions(function(value) {
      const amount = Number(value || 0);
      return (amount < 0 ? '-$' : '$') + Math.abs(amount).toLocaleString(undefined, {maximumFractionDigits: 0});
    })
  });

  salesChart = new Chart(document.getElementById('salesChart'), {
    type: 'bar',
    data: {
      labels: [],
      datasets: [
        { label: T('chartSales'), data: [], backgroundColor: chartColors.gold, borderRadius: 4, yAxisID: 'y', order: 2, barPercentage: 0.7 },
        { label: T('chartRefunds'), data: [], backgroundColor: chartColors.red, borderRadius: 4, yAxisID: 'y', order: 3, barPercentage: 0.7 },
        { label: T('chartNetRev'), data: [], type: 'line', borderColor: chartColors.green, backgroundColor: 'transparent',
          borderWidth: 2, pointRadius: Math.max(1, pr - 1), pointHoverRadius: Math.max(2, phr - 1), pointBackgroundColor: chartColors.green, pointBorderColor: 'transparent', tension: 0.35, yAxisID: 'y1', order: 1 }
      ]
    },
    options: {
      ...base,
      plugins: {
        ...base.plugins,
        legend: { display: true, labels: { color: chartColors.legend, usePointStyle: true, pointStyle: 'circle', padding: 16, font: { family: "'Pretendard Variable', Pretendard, sans-serif", size: 12 } } }
      },
      scales: {
        x: base.scales.x,
        y: { ...base.scales.y, position: 'left', title: { display: !isMobile, text: T('chartUnits'), color: chartColors.tick, font: { family: "'Pretendard Variable', Pretendard, sans-serif", size: 11 } } },
        y1: { ...base.scales.y, position: 'right', grid: { drawOnChartArea: false }, title: { display: !isMobile, text: T('chartRevenueAxis'), color: chartColors.tick, font: { family: "'Pretendard Variable', Pretendard, sans-serif", size: 11 } } }
      }
    }
  });

  wishlistTrendChart = new Chart(document.getElementById('wishlistTrendChart'), {
    type: 'bar',
    data: {
      labels: [],
      datasets: [
        { label: T('chartWishlistAdds'), data: [], backgroundColor: chartColors.green, borderRadius: 3, order: 2, barPercentage: 0.72 },
        { label: T('chartWishlistDeletes'), data: [], backgroundColor: chartColors.red, borderRadius: 3, order: 3, barPercentage: 0.72 },
        { label: T('chartWishlistPurchases'), data: [], backgroundColor: chartColors.gold, borderRadius: 3, order: 4, barPercentage: 0.72 },
        { label: T('chartWishlistNet'), data: [], type: 'line', borderColor: chartColors.purple, backgroundColor: 'transparent',
          borderWidth: 2.5, pointRadius: Math.max(1, pr - 1), pointHoverRadius: phr, pointBackgroundColor: chartColors.purple,
          pointBorderColor: 'transparent', tension: 0.3, order: 1 }
      ]
    },
    options: {
      ...base,
      plugins: {
        ...base.plugins,
        legend: { display: true, labels: { color: chartColors.legend, usePointStyle: true, pointStyle: 'circle', padding: 13, font: { family: "'Pretendard Variable', Pretendard, sans-serif", size: 11 } } },
        tooltip: {
          ...base.plugins.tooltip,
          callbacks: {
            label: function(context) {
              const value = Number(context.raw || 0);
              return context.dataset.label + ': ' + (value > 0 ? '+' : '') + value.toLocaleString();
            }
          }
        }
      },
      scales: {
        x: { ...base.scales.x, ticks: { ...base.scales.x.ticks, maxTicksLimit: 10 } },
        y: {
          ...base.scales.y,
          title: { display: !isMobile, text: T('chartUnits'), color: chartColors.tick, font: { family: "'Pretendard Variable', Pretendard, sans-serif", size: 11 } }
        }
      }
    }
  });

  playerChart = new Chart(document.getElementById('playerChart'), {
    type: 'line',
    data: {
      labels: [],
      datasets: [{
        label: T('chartPlayers'),
        data: [],
        borderColor: chartColors.purple,
        backgroundColor: chartColors.purpleFill,
        fill: true, tension: 0.35, pointRadius: isMobile ? 1 : 1.5, pointHoverRadius: isMobile ? 2 : 4,
        pointBackgroundColor: chartColors.purple,
        pointBorderColor: 'transparent',
        borderWidth: 2
      }]
    },
    options: base
  });
}

function formatDuration(minutes) {
  const total = Number(minutes || 0);
  const hours = Math.floor(total / 60);
  const mins = total % 60;
  if (curLang === 'ko') {
    if (hours && mins) return hours + '시간 ' + mins + '분';
    return hours ? hours + '시간' : mins + '분';
  }
  if (hours && mins) return hours + 'h ' + mins + 'm';
  return hours ? hours + 'h' : mins + 'm';
}

function formatMoney(value) {
  const amount = Number(value || 0);
  return (amount < 0 ? '-$' : '$') + Math.abs(amount).toLocaleString(undefined, {maximumFractionDigits: 0});
}

function escapeHtml(value) {
  const div = document.createElement('div');
  div.textContent = String(value == null ? '' : value);
  return div.innerHTML;
}

function renderWishlistTrend(rows) {
  const allRows = Array.isArray(rows) ? rows : [];
  const visible = allRows.slice(-30);
  wishlistTrendChart.data.labels = visible.map(function(row) { return row.date.substring(5); });
  wishlistTrendChart.data.datasets[0].data = visible.map(function(row) { return Number(row.adds || 0); });
  wishlistTrendChart.data.datasets[1].data = visible.map(function(row) { return -Number(row.deletes || 0); });
  wishlistTrendChart.data.datasets[2].data = visible.map(function(row) {
    return -(Number(row.purchases || 0) + Number(row.gifts || 0));
  });
  wishlistTrendChart.data.datasets[3].data = visible.map(function(row) { return Number(row.net_change || 0); });
  wishlistTrendChart.update('none');

  if (!allRows.length) {
    document.getElementById('wishlistTrendSummary').textContent = T('collecting');
    return;
  }

  const latest = allRows[allRows.length - 1];
  const last7 = allRows.slice(-7);
  const average = last7.reduce(function(total, row) {
    return total + Number(row.net_change || 0);
  }, 0) / Math.max(1, last7.length);
  const latestNet = Number(latest.net_change || 0);
  const latestText = (latestNet >= 0 ? '+' : '') + latestNet.toLocaleString();
  const averageText = (average >= 0 ? '+' : '') + average.toFixed(1);
  document.getElementById('wishlistTrendSummary').textContent =
    (curLang === 'ko' ? '확정 ' : 'Confirmed ') + latest.date.substring(5) + ' ' + latestText +
    (curLang === 'ko' ? ' · 7일 평균 ' : ' · 7d avg ') + averageText +
    (curLang === 'ko' ? '/일' : '/day');
}

function renderMarketingSnapshot(snapshot) {
  const data = snapshot || {};
  const traffic = data.traffic || {};
  const utm = data.utm || {};
  const sources = Array.isArray(data.traffic_sources) ? data.traffic_sources : [];
  const utmSources = Array.isArray(data.utm_sources) ? data.utm_sources : [];
  const verified = data.verified_at ? new Date(data.verified_at) : null;
  const dateOnly = /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(data.verified_at || '');
  const verifiedText = dateOnly
    ? data.verified_at
    : verified && !Number.isNaN(verified.getTime())
    ? verified.toLocaleString()
    : (data.verified_at || '--');

  document.getElementById('marketingVerified').textContent =
    (curLang === 'ko' ? 'Steamworks 확인 ' : 'Steamworks ') + verifiedText;
  document.getElementById('trafficVisits').textContent =
    data.verified_at ? Number(traffic.visits || 0).toLocaleString() : '--';
  document.getElementById('utmVisits').textContent =
    data.verified_at ? Number(utm.total_visits || 0).toLocaleString() : '--';
  document.getElementById('utmWishlists').textContent =
    data.verified_at ? Number(utm.wishlists || 0).toLocaleString() : '--';

  if (!data.verified_at) {
    document.getElementById('trafficSources').innerHTML =
      '<div class="snapshot-empty">' + T('collecting') + '</div>';
    document.getElementById('utmSources').innerHTML = '';
    document.getElementById('trafficSourceNote').textContent =
      curLang === 'ko'
        ? 'Steamworks 로그인 없이 자동 수집할 수 없는 항목입니다.'
        : 'Steamworks login-only data cannot be collected automatically.';
    return;
  }

  const maxVisits = Math.max(1, ...sources.map(function(item) { return Number(item.visits || 0); }));
  document.getElementById('trafficSources').innerHTML = sources.slice(0, 6).map(function(item) {
    const visits = Number(item.visits || 0);
    const width = Math.round(visits / maxVisits * 100);
    return '<div class="source-row">' +
      '<span class="source-label" title="' + escapeHtml(item.name) + '">' + escapeHtml(item.name) + '</span>' +
      '<span class="source-bar"><span style="width:' + width + '%"></span></span>' +
      '<span class="source-value">' + visits.toLocaleString() + '</span>' +
      '</div>';
  }).join('');

  document.getElementById('utmSources').innerHTML = utmSources.length
    ? utmSources.slice(0, 6).map(function(item) {
      const visits = Number(item.visits || 0);
      const wishlists = Number(item.wishlists || 0);
      return '<div class="utm-row">' +
        '<span class="source-label" title="' + escapeHtml(item.name) + '">' + escapeHtml(item.name) + '</span>' +
        '<span class="utm-metric">' + visits.toLocaleString() + (curLang === 'ko' ? ' 방문' : ' visits') + '</span>' +
        '<span class="utm-metric wishlist">' + wishlists.toLocaleString() + (curLang === 'ko' ? ' 위시' : ' wishlists') + '</span>' +
        '</div>';
    }).join('')
    : '<div class="snapshot-empty">' + (curLang === 'ko' ? 'UTM 매체 데이터 없음' : 'No UTM source data') + '</div>';

  const trafficPeriod = data.traffic_period || '--';
  const utmPeriod = data.utm_period || '--';
  document.getElementById('trafficSourceNote').textContent =
    curLang === 'ko'
      ? '일반 유입(' + trafficPeriod + ')은 방문 참고치이며 위시리스트 귀속값이 아닙니다. 실제 매체 귀속은 UTM 전환(' + utmPeriod + ', 72시간 창)만 확정값입니다.'
      : 'Traffic (' + trafficPeriod + ') is visit context, not wishlist attribution. Only UTM conversions (' + utmPeriod + ', 72-hour window) are attributed.';
}

function timelineLabel(timestamp) {
  const d = new Date(timestamp);
  if (timelineRange === 'all') {
    return (d.getMonth() + 1) + '/' + d.getDate();
  }
  return (d.getMonth() + 1) + '/' + d.getDate() + ' ' + d.getHours().toString().padStart(2, '0') + 'h';
}

function setTimelineRange(range) {
  timelineRange = range;
  document.querySelectorAll('[data-range]').forEach(function(button) {
    button.classList.toggle('active', button.getAttribute('data-range') === range);
  });
  renderTimelineCharts();
}

function renderTimelineCharts() {
  if (!cumulativeSalesChart || !cumulativeRevenueChart) return;
  let visible = timelineData.slice();
  if (timelineRange !== 'all' && visible.length) {
    const lastTime = new Date(visible[visible.length - 1][0]).getTime();
    const cutoff = lastTime - Number(timelineRange) * 24 * 60 * 60 * 1000;
    visible = visible.filter(function(row) { return new Date(row[0]).getTime() >= cutoff; });
  }

  const labels = visible.map(function(row) { return timelineLabel(row[0]); });
  const sales = visible.map(function(row) { return Number(row[1] || 0); });
  const revenue = visible.map(function(row) { return Number(row[3] || 0); });

  cumulativeSalesChart.data.labels = labels;
  cumulativeSalesChart.data.datasets[0].data = sales;
  cumulativeSalesChart.update('none');
  cumulativeRevenueChart.data.labels = labels;
  cumulativeRevenueChart.data.datasets[0].data = revenue;
  cumulativeRevenueChart.update('none');

  const currentSales = sales.length ? sales[sales.length - 1] : 0;
  const salesDelta = sales.length ? currentSales - sales[0] : 0;
  const currentRevenue = revenue.length ? revenue[revenue.length - 1] : 0;
  const revenueDelta = revenue.length ? currentRevenue - revenue[0] : 0;
  document.getElementById('cumSalesSummary').textContent =
    (curLang === 'ko' ? '현재 ' : 'Now ') + currentSales.toLocaleString() +
    ' · Δ ' + (salesDelta >= 0 ? '+' : '') + salesDelta.toLocaleString();
  document.getElementById('cumRevenueSummary').textContent =
    (curLang === 'ko' ? '현재 ' : 'Now ') + formatMoney(currentRevenue) +
    ' · Δ ' + (revenueDelta >= 0 ? '+' : '') + formatMoney(revenueDelta);
}

function renderMeasuredFacts(data) {
  const snapshot = data.steamworks_snapshot || {};
  const periods = data.period_metrics || {};
  const measuredAt = snapshot.verified_at ? new Date(snapshot.verified_at).toLocaleString() : '--';
  const fullScanAt = data.collection_status?.full_scan_at
    ? new Date(data.collection_status.full_scan_at).toLocaleString()
    : '--';
  document.getElementById('snapshotVerified').textContent =
    (curLang === 'ko' ? 'Steamworks 확인: ' : 'Steamworks verified: ') + measuredAt;
  document.getElementById('fullScanUpdated').textContent =
    (curLang === 'ko' ? '자동 전체집계: ' : 'Full refresh: ') + fullScanAt;

  const hasSteamworksSnapshot = Boolean(snapshot.verified_at);
  document.getElementById('averagePlaytime').textContent =
    hasSteamworksSnapshot ? formatDuration(snapshot.average_playtime_minutes) : '--';
  document.getElementById('medianPlaytime').textContent =
    hasSteamworksSnapshot ? formatDuration(snapshot.median_playtime_minutes) : '--';
  document.getElementById('playtimeSample').textContent = hasSteamworksSnapshot
    ? (curLang === 'ko' ? '측정 이용자 ' : 'Measured users ') +
      Number(snapshot.measured_users || 0).toLocaleString() +
      (curLang === 'ko' ? '명' : '')
    : (curLang === 'ko' ? 'Steamworks 데이터 대기' : 'Awaiting Steamworks data');

  [7, 30].forEach(function(days) {
    const period = periods[String(days)] || {};
    const current = period.current || {};
    const previous = period.previous || {};
    document.getElementById('sales' + days + 'Value').textContent =
      Number(current.units || 0).toLocaleString() +
      (curLang === 'ko' ? ' 판매' : ' sold');
    document.getElementById('sales' + days + 'Sub').textContent =
      (curLang === 'ko' ? '환불 ' : 'returns ') +
      Number(current.returns || 0).toLocaleString() +
      ' (' + Number(current.refund_rate || 0).toFixed(1) + '%)' +
      ' · ' + (curLang === 'ko' ? '이전 ' : 'prior ') +
      Number(previous.units || 0).toLocaleString();
  });

  const thresholdRows = snapshot.playtime_thresholds || [];
  document.getElementById('playtimeFunnel').innerHTML = thresholdRows.map(function(item) {
    return '<div class="funnel-row">' +
      '<span class="funnel-label">≥ ' + formatDuration(item.minutes) + '</span>' +
      '<span class="evidence-bar"><span style="width:' + Number(item.percent || 0) + '%"></span></span>' +
      '<span class="funnel-value">' + Number(item.percent || 0) + '%</span>' +
      '</div>';
  }).join('');

  const reasons = snapshot.refund_reasons || [];
  const refundReasonTotal = reasons.reduce(function(total, item) {
    return total + Number(item.count || 0);
  }, 0);
  document.getElementById('refundReasonsTitle').textContent =
    (curLang === 'ko' ? '환불 사유 · 총 ' : 'Refund Reasons · ') +
    refundReasonTotal.toLocaleString() +
    (curLang === 'ko' ? '건' : ' total');
  const maxReason = Math.max(1, ...reasons.map(function(item) { return Number(item.count || 0); }));
  document.getElementById('refundReasons').innerHTML = reasons.map(function(item) {
    const label = curLang === 'ko' ? item.label_ko : item.label_en;
    const width = Math.round(Number(item.count || 0) / maxReason * 100);
    return '<div class="reason-row">' +
      '<span class="reason-label">' + label + '</span>' +
      '<span class="evidence-bar"><span style="width:' + width + '%"></span></span>' +
      '<span class="reason-value">' + Number(item.count || 0) + '</span>' +
      '</div>';
  }).join('');

  const delta = data.recent_delta || {};
  if (delta.ready) {
    document.getElementById('liveDelta').textContent =
      (curLang === 'ko' ? '최근 3시간 변화 · 판매 ' : 'Last 3h · sales ') +
      (delta.units >= 0 ? '+' : '') + Number(delta.units || 0) +
      (curLang === 'ko' ? ' · 환불 ' : ' · returns ') +
      (delta.returns >= 0 ? '+' : '') + Number(delta.returns || 0) +
      (curLang === 'ko' ? ' · 순수익 ' : ' · net ') +
      (delta.net >= 0 ? '+' : '') + formatMoney(delta.net);
  } else {
    document.getElementById('liveDelta').textContent =
      curLang === 'ko' ? '최근 3시간 변화 수집 중' : 'Collecting the last 3 hours';
  }
}

async function fetchData() {
  try {
    const resp = await fetch('/api/data');
    const data = await resp.json();
    renderGameSelector(data.game_selector || {});

    // Game info
    if (data.app_details) {
      const d = data.app_details;
      document.getElementById('gameName').textContent = d.name || '';
      document.title = (d.name || 'Steam') + ' - Metrics Dashboard';
      document.getElementById('gameDev').textContent = (d.developers||[]).join(', ') + ' · ' + (d.publishers||[]).join(', ');
      document.getElementById('headerImg').src = d.header_image || '';
      if (d.price_overview) document.getElementById('gamePrice').textContent = d.price_overview.final_formatted || '';
    }

    // Remove loading shimmer
    document.querySelectorAll('.metric-value.loading').forEach(function(el) { el.classList.remove('loading'); });

    // Sales totals
    const s = data.sales_totals || {};
    document.getElementById('totalSales').textContent = (s.units || 0).toLocaleString();
    const totalReturns = Math.abs(Number(s.returns || 0));
    document.getElementById('salesSub').textContent = T('refunds') + ' ' + totalReturns + (curLang === 'ko' ? '건' : '') + ' · ' + T('grossLabel') + ' $' + (s.gross || 0).toFixed(0);
    document.getElementById('netRevenue').textContent = '$' + (s.net || 0).toLocaleString(undefined, {minimumFractionDigits: 0, maximumFractionDigits: 0});
    document.getElementById('revenueSub').textContent = T('beforeFees') + ' $' + (s.gross || 0).toFixed(0);
    const refRate = s.units > 0 ? ((totalReturns / s.units) * 100).toFixed(1) : '0';
    document.getElementById('refundRate').textContent = refRate + '%';

    // Independent cumulative charts (default 30 days)
    timelineData = data.sales_timeline || [];
    renderTimelineCharts();
    renderMeasuredFacts(data);

    // Daily sales chart
    const daily = data.daily_sales || [];
    salesChart.data.labels = daily.map(function(r) { return r[0].substring(5); });
    salesChart.data.datasets[0].data = daily.map(function(r) { return r[1]; });
    salesChart.data.datasets[1].data = daily.map(function(r) { return Math.abs(Number(r[2] || 0)); });
    salesChart.data.datasets[2].data = daily.map(function(r) { return r[4]; });
    salesChart.update('none');

    // Players
    const players = data.current_players || 0;
    document.getElementById('currentPlayers').textContent = players.toLocaleString();
    document.getElementById('peakPlayers').textContent = (data.peak_players || 0).toLocaleString();

    const hist = data.player_history || [];
    if (hist.length > 1) {
      const prev = hist[hist.length - 2][1];
      const diff = players - prev;
      const el = document.getElementById('playerChange');
      el.textContent = diff > 0 ? '▲ +' + diff : diff < 0 ? '▼ ' + diff : T('noChange');
      el.style.color = diff > 0 ? 'var(--green-vine)' : diff < 0 ? 'var(--red-alert)' : 'var(--text-tertiary)';
    }

    // Player chart
    playerChart.data.labels = hist.map(function(r) {
      const d = new Date(r[0]);
      return d.getHours().toString().padStart(2,'0') + ':' + d.getMinutes().toString().padStart(2,'0');
    });
    playerChart.data.datasets[0].data = hist.map(function(r) { return r[1]; });
    playerChart.update('none');

    // Reviews
    const rev = data.reviews || {};
    const total = rev.total_reviews || 0, pos = rev.total_positive || 0, neg = rev.total_negative || 0;
    document.getElementById('totalReviews').textContent = total;
    document.getElementById('reviewRatio').textContent = '👍 ' + pos + ' / 👎 ' + neg;
    document.getElementById('positiveRate').textContent = total > 0 ? Math.round(pos/total*100) + '%' : '--';
    document.getElementById('reviewScore').textContent = rev.review_score_desc || '';

    // Wishlist
    const wl = data.wishlist || {};
    const wlNet = wl.net || 0;
    document.getElementById('wishlistNet').textContent = '~' + wlNet.toLocaleString();
    const openingBalance = Number(wl.opening_balance || 0);
    document.getElementById('wishlistSub').textContent =
      (openingBalance ? (curLang === 'ko' ? '기준 +' : 'base +') + openingBalance + ' · ' : '') +
      '+' + (wl.adds||0) + ' / -' + (wl.deletes||0) + ' / ' + T('conversion') + ' ' + (wl.purchases||0);
    renderWishlistTrend(data.wishlist_daily || []);
    renderMarketingSnapshot(data.marketing_snapshot || {});

    // Country data
    const sc = data.sales_by_country || {};
    const wlc = data.wishlist_by_country || {};

    const esc = escapeHtml;

    const renderCountryTable = function(obj, valFn) {
      const entries = Object.entries(obj).slice(0, 15);
      if (!entries.length) return '<div style="color:var(--text-tertiary);font-style:italic;padding:12px 0;">' + T('collecting') + '</div>';
      const maxVal = Math.max(1, valFn(entries[0][1]));
      return '<table class="country-table">' +
        entries.map(function(entry) {
          var cc = esc(entry[0]);
          var d = entry[1];
          const val = valFn(d);
          const pct = Math.round(val / maxVal * 100);
          return '<tr>' +
            '<td class="cc">' + cc + '</td>' +
            '<td class="bar-cell"><div style="background:linear-gradient(90deg, var(--wine-rose), var(--wine-merlot));width:' + pct + '%;height:7px;border-radius:3px;min-width:6px;box-shadow:0 0 6px rgba(168,74,86,0.2);"></div></td>' +
            '<td class="val">' + val + '</td></tr>';
        }).join('') + '</table>';
    };

    document.getElementById('salesByCountry').innerHTML = renderCountryTable(sc, function(d) { return d.units || 0; });
    document.getElementById('wishlistByCountry').innerHTML = renderCountryTable(wlc, function(d) { return d.adds || 0; });

    // Recent reviews
    const recent = data.recent_reviews || [];
    document.getElementById('recentReviews').innerHTML = recent.map(function(r) {
      const isUp = r.voted_up;
      const thumb = isUp ? '👍' : '👎';
      const thumbClass = isUp ? 'up' : 'down';
      const playtime = Math.round((r.author?.playtime_forever||0)/60*10)/10;
      const text = esc((r.review||'').substring(0, 300)).split(String.fromCharCode(10)).join(' ');
      return '<div class="review-card">' +
        '<div class="review-header">' +
        '<span class="review-thumb ' + thumbClass + '">' + thumb + '</span>' +
        '<span class="review-author">' + esc(r.author?.personaname||'Anonymous') + '</span>' +
        '<span class="review-playtime">' + playtime + T('hours') + '</span>' +
        '</div>' +
        '<div class="review-text">' + text + '</div>' +
        '</div>';
    }).join('');

    // Status
    document.getElementById('tgDot').className = 'dot ' + (data.telegram_active ? 'on' : 'off');
    document.getElementById('tgStatus').textContent = data.telegram_active ? 'ON' : 'OFF';
    document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();
  } catch(e) { console.error('Fetch error:', e); throw e; }
}

// Initialize with stored language
applyStaticLabels();
updateToggleButtons();
initCharts();

let fetchFailCount = 0;
async function fetchWithBackoff() {
  try {
    await fetchData();
    fetchFailCount = 0;
  } catch(e) {
    fetchFailCount++;
    console.warn('Fetch failed, attempt', fetchFailCount);
  }
  const delay = Math.min(30000 * Math.pow(1.5, fetchFailCount), 300000);
  setTimeout(fetchWithBackoff, delay);
}
fetchWithBackoff();
</script>
</body>
</html>'''

# ========== HTTP SERVER ==========
class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        try:
            parsed = urlparse(self.path)

            if parsed.path in ('/', '/dashboard'):
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(DASHBOARD_HTML.encode('utf-8'))

            elif parsed.path == '/api/data':
                players = get_current_players()
                reviews = get_reviews()
                recent = get_recent_reviews()
                app_details = get_app_details()
                p_history = get_player_history()
                daily = get_all_daily_sales()
                timeline = get_sales_snapshots()
                totals = get_sales_totals()
                period_metrics = get_period_metrics()
                recent_delta = get_recent_sales_delta(3)
                daily_wishlist = get_daily_wishlist()

                with _data_lock:
                    wl = dict(cached_wishlist)
                    local_sales_by_country = dict(cached_sales_by_country)
                    local_wishlist_by_country = dict(cached_wishlist_by_country)
                    local_peak_players = peak_players

                wl_history = get_wishlist_history()

                payload = {
                    "game_selector": {
                        "active_app_id": APP_ID,
                        "games": DASHBOARD_GAMES,
                    },
                    "current_players": players,
                    "peak_players": local_peak_players,
                    "reviews": reviews,
                    "recent_reviews": recent,
                    "app_details": app_details,
                    "player_history": p_history,
                    "daily_sales": daily,
                    "sales_timeline": timeline,
                    "sales_totals": {
                        "units": totals[0], "returns": abs(totals[1]),
                        "gross": totals[2], "net": totals[3]
                    },
                    "period_metrics": period_metrics,
                    "recent_delta": recent_delta,
                    "steamworks_snapshot": STEAMWORKS_SNAPSHOT,
                    "marketing_snapshot": STEAM_MARKETING_SNAPSHOT,
                    "collection_status": {
                        "light_interval_seconds": POLL_INTERVAL,
                        "full_interval_seconds": FULL_SCAN_INTERVAL,
                        "full_scan_at": last_full_scan_at.isoformat() if last_full_scan_at else None,
                    },
                    "wishlist": wl,
                    "wishlist_history": wl_history,
                    "wishlist_daily": daily_wishlist,
                    "sales_by_country": local_sales_by_country,
                    "wishlist_by_country": local_wishlist_by_country,
                    "telegram_active": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS),
                    "timestamp": datetime.now().isoformat()
                }

                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps(payload, ensure_ascii=False).encode('utf-8'))

            else:
                self.send_response(404)
                self.end_headers()
        except Exception as e:
            print(f"[HTTP ERROR] {e}")
            try:
                self.send_response(500)
                self.end_headers()
            except:
                pass

# ========== MAIN ==========
if __name__ == '__main__':
    def handle_signal(signum, frame):
        print(f"\n[SIGNAL] Received signal {signum}, shutting down...")
        shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    init_db()

    print("=" * 50)
    print(f"  📊 {GAME_LABEL} - Steam Dashboard")
    print("=" * 50)
    print(f"  App ID:     {APP_ID}")
    print(f"  Dashboard:  http://localhost:{PORT}")
    print(f"  Polling:    {POLL_INTERVAL}s ({POLL_INTERVAL//60}min)")
    print(f"  Full scan:  {FULL_SCAN_INTERVAL}s ({FULL_SCAN_INTERVAL//3600}h)")
    print(f"  Financial:  partner.steam-api.com")
    print(f"  Telegram:   {'ON' if TELEGRAM_BOT_TOKEN else 'OFF'}")
    print("=" * 50)

    # 웹서버 먼저 시작 (초기화 중에도 접속 가능)
    print(f"\n[READY] Dashboard at http://localhost:{PORT}")
    class ReusableHTTPServer(ThreadingHTTPServer):
        allow_reuse_address = True

    server = ReusableHTTPServer(('0.0.0.0', PORT), DashboardHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    # 초기 데이터 수집 (기존 데이터 있으면 스킵)
    existing_totals = get_sales_totals()
    if existing_totals[0] > 0:
        print(f"\n[INIT] Existing data found: {existing_totals[0]} units, ${existing_totals[3]:.2f} net")
        print("[INIT] Refreshing latest data only...")
        # 오늘 + 어제만 갱신 (이전 데이터는 이미 있음)
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        today_str = datetime.now().strftime("%Y-%m-%d")
        for ds in [yesterday, today_str]:
            u, r, g, n, _cc, complete = fetch_sales_for_date(ds)
            if not complete:
                print(f"  [{ds}] incomplete read — skipping upsert")
                continue
            upsert_daily_sales(ds, u, r, g, n)
            if u > 0:
                print(f"  [{ds}] +{u} sold, -{abs(r)} returned, ${n:.2f} net")
    else:
        print("\n[INIT] No existing data. Fetching all sales since launch...")
        refresh_all_sales()

    totals = get_sales_totals()
    last_total_units = totals[0]
    save_sales_snapshot(totals[0], totals[1], totals[3])

    # 국가·위시리스트는 시작 시 1회, 이후 3시간마다 갱신한다.
    print("[INIT] Running initial 3h full scan...")
    last_wishlist_net = refresh_heavy_metrics()

    print(f"[INIT] Sales: {totals[0]} units | Revenue: ${totals[3]:.2f} | Wishlists: ~{last_wishlist_net}")

    # 시작 리포트
    print("[INIT] Sending startup report to Telegram...")
    send_startup_report()

    # 백그라운드 수집 시작
    collector = threading.Thread(target=collector_loop, daemon=True)
    collector.start()

    # 데일리 리포트 스케줄러 (매일 KST 11:00 — 지난 24시간 + 오늘 판매)
    digest = threading.Thread(target=daily_digest_loop, daemon=True)
    digest.start()

    # 메인 스레드 유지
    try:
        shutdown_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        print("\nShutting down...")
        shutdown_event.set()
        server.shutdown()
        server.server_close()
        print("Dashboard stopped.")
