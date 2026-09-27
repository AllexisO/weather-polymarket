"""
Кошелёк «повтор за сильными трейдерами» (2026-09-26, пункт 1 от Alex).

Каждую минуту: свежие сделки трейдеров из sharp_wallets (weather_sharp_rank.py)
через data-api (/trades?user=...). Повторяем покупку, если:
- это маркет «Highest temperature in <наш город>», покупка (BUY) доли «да» или «нет»;
- цена 3-95¢; сделка свежая (≤ FRESH_MIN минут);
- сделана НАКАНУНЕ дня маркета или раньше (по местному времени города). На проверке
  повтор покупок в сам день маркета — в минусе (утром −5.7%), накануне — +1.7%
  (это правило выбрано, уже видя проверочный период — доказательство только вживую);
- у кошелька ещё нет ставки на этот город и день (одна в день на город).
Покупаем по живому стакану (polyexec, комиссия, задержка), не дороже их цены + SLIP,
$2. Итог считает weather_paper.py (settle — все открытые ставки, с учётом стороны).
Крон: каждую минуту (быстрее крон не умеет). Запуск: python weather_copy.py
"""

from jobmark import item_guard
import json
import os
import sqlite3
import time
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from polyexec import simulate_buy, trading_stopped
from weather_paper import start_balance
from weather_edge import CITIES, GAMMA, parse_bucket

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
DATA_API = "https://data-api.polymarket.com"
WALLET = "copy"
STAKE = 2.0
SLIP = 0.02
FRESH_MIN = 15
MIN_FILL = 1.0
SLUG_CITY = {c["poly_slug"]: k for k, c in CITIES.items()}
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]


def parse_slug(slug):
    """highest-temperature-in-<город>-on-<month>-<day>-<year> -> (город, дата) или None."""
    if not slug or not slug.startswith("highest-temperature-in-") or "-on-" not in slug:
        return None
    place, when = slug[len("highest-temperature-in-"):].rsplit("-on-", 1)
    parts = when.split("-")
    if place not in SLUG_CITY or len(parts) != 3 or parts[0] not in MONTHS:
        return None
    try:
        return SLUG_CITY[place], date(int(parts[2]), MONTHS.index(parts[0]) + 1, int(parts[1]))
    except ValueError:
        return None


def try_copy(conn, w, t, now, source="опрос"):
    """Повторить одну сделку сильного трейдера w, если подходит под правила. True — купили,
    False — пытались, но не купили, None — не подходит. Общая для опроса и слушателя (weather_copy_live.py)."""
    if t.get("side") != "BUY" or not (0.03 <= float(t.get("price", 0)) <= 0.95):
        return None
    if now - int(t.get("timestamp", 0)) > FRESH_MIN * 60:
        return None
    cd = parse_slug(t.get("eventSlug") or "")
    if cd is None:
        return None
    city, d = cd
    tz = ZoneInfo(CITIES[city]["tz"])
    if datetime.fromtimestamp(int(t["timestamp"]), tz).date() >= d:
        return None  # покупка в сам день маркета — не повторяем
    bucket = parse_bucket(t.get("title") or "")
    if bucket is None:
        return None
    if conn.execute("""SELECT 1 FROM paper_trades WHERE wallet = ? AND city = ? AND local_date = ?
                       AND status != 'nofill'""", (WALLET, city, d.isoformat())).fetchone():
        return None  # уже есть ставка на этот город и день (неудачная попытка не мешает следующей)
    spent, back = conn.execute("""SELECT COALESCE(SUM(stake + COALESCE(fee, 0)), 0), COALESCE(SUM(COALESCE(payout, 0)), 0)
                                  FROM paper_trades WHERE wallet = ?""", (WALLET,)).fetchone()
    if start_balance(WALLET) - spent + back < STAKE:
        return None  # в кошельке меньше $2
    side = "yes" if t.get("outcome") == "Yes" else "no"
    their = float(t["price"])
    try:
        ev = requests.get(f"{GAMMA}/events", params={"slug": t["eventSlug"]}, timeout=20).json()
        m = next((m for m in (ev[0]["markets"] if ev else []) if parse_bucket(m["question"]) == bucket
                  and not m.get("closed") and m.get("acceptingOrders", True)), None)
        if m is None:
            return None
        ex = simulate_buy(m, json.loads(m["clobTokenIds"])[0 if side == "yes" else 1], STAKE, min(0.95, their + SLIP))
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"{city} {d}: ошибка стакана — {e}", flush=True)
        return None
    filled = ex["cost"] >= MIN_FILL
    from weather_paper import fmt_bucket
    what = ("на " if side == "yes" else "против ") + fmt_bucket(bucket[0], bucket[1], CITIES[city]["unit"])
    lag = time.time() - int(t["timestamp"])
    reason = (f"повтор за {w[:10]}…: купил {what} по {their * 100:.0f}¢ "
              f"({datetime.fromtimestamp(int(t['timestamp']), tz):%d.%m %H:%M} местного), мы — через {lag:.0f} с ({source})")
    if not filled:
        reason += "; " + (ex["reason"] or "купить можно было меньше чем на $1")
    conn.execute("DELETE FROM paper_trades WHERE wallet = ? AND city = ? AND local_date = ? AND status = 'nofill'",
                 (WALLET, city, d.isoformat()))
    conn.execute(
        """INSERT OR IGNORE INTO paper_trades
           (wallet, city, local_date, snapshot_ts, unit, bucket_lo, bucket_hi, model_p, market_p, price, stake,
            status, reason, shares, fee, book_json, side)
           VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (WALLET, city, d.isoformat(), datetime.now(timezone.utc).isoformat(), CITIES[city]["unit"],
         bucket[0], bucket[1], their, ex["avg"], ex["cost"] if filled else 0.0,
         "open" if filled else "nofill", reason, ex["shares"] if filled else 0.0,
         ex["fee"] if filled else 0.0, ex["book"], side))
    conn.commit()
    print(f"{city} {d}: {'купили' if filled else 'не купили'} — {reason}", flush=True)
    return filled


def main():
    if trading_stopped():
        print("Файл STOP — не повторяем")
        return
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'sharp_wallets'").fetchone():
        print("нет рейтинга сильных трейдеров (weather_sharp_rank.py)")
        return
    sharps = [r[0] for r in conn.execute("SELECT wallet FROM sharp_wallets ORDER BY pnl DESC").fetchall()]
    # 2026-09-26: основной — слушатель weather_copy_live.py (секунды); этот опрос — запасной, раз в 5 минут
    now = time.time()
    placed = 0

    def fetch(w):
        # 2026-09-26: опрос параллельно — проверка раз в минуту (задержка повтора съедает заработок:
        # на проверке через 0.5 мин +1.0%, 1 мин +0.1%, 2.5 мин −1.1%, 5 мин −2.3%)
        try:
            data = requests.get(f"{DATA_API}/trades", params={"user": w, "limit": 50}, timeout=20).json()
        except (requests.RequestException, ValueError) as e:
            print(f"{w[:10]}: ошибка — {e}")
            return w, []
        # 2026-09-27: Data API иногда отвечает объектом-ошибкой ({"error": ...}) вместо списка сделок —
        # раньше скрипт падал на первом таком трейдере и не проверял остальных; теперь пропускаем только его
        if not isinstance(data, list):
            print(f"{w[:10]}: Polymarket ответил не списком сделок — {str(data)[:150]}")
            return w, []
        return w, [t for t in data if isinstance(t, dict)]

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as pool:
        feeds = list(pool.map(fetch, sharps))
    for w, trades in feeds:
        with item_guard(w[:10], conn):
            for t in sorted(trades, key=lambda x: x.get("timestamp", 0)):
                placed += bool(try_copy(conn, w, t, now))
    print(f"повтор: проверено трейдеров {len(sharps)}, новых ставок {placed}")
    from jobmark import mark
    mark(conn, "weather_copy")
    conn.close()


if __name__ == "__main__":
    main()
