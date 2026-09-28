"""
Цены Kalshi на те же дни и станции (2026-09-28, исследование «что мы упускаем»).
У Kalshi те же ежедневные маркеты максимума температуры, ликвиднее Polymarket. Совпадают станции в 7 городах
(Kalshi — климатический отчёт NWS «CLI<станция>», Polymarket — METAR той же станции; CLI учитывает и минутные пики,
поэтому может быть на градус выше). Если цена Kalshi в 08:00 знает то, чего нет в цене Polymarket, — это новая
информация для модели.

Пишет kalshi_px (city, local_date, lo, hi, mid, last, vol) — цены в 08:00 местного (свеча 07:00-08:00) в ту базу,
что в POLY_LAB_DB (только копия). Публичный API Kalshi, без ключа.
Запуск: python weather_kalshi_hist.py 2026-07-01 2026-09-26
"""

import os
import sqlite3
import sys
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from weather_cities import OBS_CITIES

DB_PATH = Path(os.environ.get("POLY_LAB_DB", "/data/research/research.sqlite3"))
API = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = {"miami": "KXHIGHMIA", "los_angeles": "KXHIGHLAX", "san_francisco": "KXHIGHTSFO", "houston": "KXHIGHTHOU",
          "seattle": "KXHIGHTSEA", "austin": "KXHIGHAUS", "atlanta": "KXHIGHTATL"}
MON = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def get(url, **params):
    for _ in range(4):
        r = requests.get(url, params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(2)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Kalshi: слишком часто")


def ev_date(event_ticker):
    s = event_ticker.split("-")[1]  # 26SEP26
    return date(2000 + int(s[:2]), MON[s[2:5]], int(s[5:]))


def bounds(m):
    st, f, c = m.get("strike_type"), m.get("floor_strike"), m.get("cap_strike")
    if st == "between":
        return f - 0.5, c + 0.5
    if st == "greater":
        return f + 0.5, 999.0
    if st == "less":
        return -999.0, c - 0.5
    return None


def main(d0, d1):
    assert "research" in str(DB_PATH), "только на копии базы"
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("""CREATE TABLE IF NOT EXISTS kalshi_px (city TEXT, local_date TEXT, lo REAL, hi REAL, mid REAL, last REAL,
                    vol REAL, PRIMARY KEY (city, local_date, lo))""")
    for city, ser in SERIES.items():
        tz = ZoneInfo(OBS_CITIES[city]["tz"])
        markets, cursor = [], None
        while True:
            p = {"series_ticker": ser, "limit": 1000}
            if cursor:
                p["cursor"] = cursor
            j = get(f"{API}/markets", **p)
            markets += j.get("markets", [])
            cursor = j.get("cursor")
            if not cursor or not j.get("markets"):
                break
        n = 0
        for m in markets:
            try:
                d = ev_date(m["event_ticker"])
            except (KeyError, ValueError, IndexError):
                continue
            b = bounds(m)
            if not (d0 <= d <= d1) or b is None:
                continue
            t8 = int(datetime(d.year, d.month, d.day, 8, tzinfo=tz).timestamp())
            try:
                c = get(f"{API}/series/{ser}/markets/{m['ticker']}/candlesticks", start_ts=t8 - 3 * 3600, end_ts=t8,
                        period_interval=60).get("candlesticks", [])
            except (requests.RequestException, RuntimeError) as e:
                print(f"{city} {m['ticker']}: ошибка — {e}", flush=True)
                continue
            c = [x for x in c if x["end_period_ts"] <= t8]
            if not c:
                continue
            x = c[-1]
            bid = float(x.get("yes_bid", {}).get("close_dollars") or 0)
            ask = float(x.get("yes_ask", {}).get("close_dollars") or 1)
            pr = x.get("price", {})
            last = pr.get("close_dollars") or pr.get("previous_dollars")
            mid = (bid + ask) / 2 if ask - bid <= 0.2 else (float(last) if last else None)
            vol = sum(float(y.get("volume_fp") or 0) for y in c)
            conn.execute("INSERT OR REPLACE INTO kalshi_px VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (city, d.isoformat(), b[0], b[1], mid, float(last) if last else None, vol))
            n += 1
            time.sleep(0.08)
        conn.commit()
        print(f"{city} ({ser}): маркетов всего {len(markets)}, цен в 08:00 записано {n}", flush=True)
    conn.close()


if __name__ == "__main__":
    main(date.fromisoformat(sys.argv[1]), date.fromisoformat(sys.argv[2]))
