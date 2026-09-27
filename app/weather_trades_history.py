"""
История ВСЕХ сделок погодных маркетов (data-api /trades по eventId) —
для исследования "выгодно ли быть стороной, выставляющей заявки"
(маркет-мейкером), 2026-09-25.

Каждая сделка в data-api — с точки зрения того, кто ЗАБРАЛ заявку
(taker): side BUY/SELL, outcome Yes/No, цена, размер. Вторая сторона —
тот, кто заявку выставил (maker). Сравнивая цену сделки с тем, чем
закончился маркет, считаем, кто из них в среднем зарабатывает.

Пишет:
- poly_trades — сделки (одна строка на сделку, по transactionHash+asset+
  время+цена+размер — без дублей при повторной загрузке);
- poly_market_final — чем закончился каждый маркет (финальная цена Yes).

Грузит последние DAYS дней (уже загруженные пропускает).
Запуск: python weather_trades_history.py [DAYS]
С 2026-09-26 — по крону каждую ночь (решение Alex): data-api отдаёт сделки
только за ~30 дней, без ежедневного сбора история реальных цен пропадает, а
она нужна для честной проверки моделей (не по последней цене, а по сделкам).
"""

import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from weather_cities import OBS_CITIES
from weather_edge import GAMMA, month_day_year_slug, parse_bucket

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
DATA_API = "https://data-api.polymarket.com"


def get(url, params):
    for attempt in range(5):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == 4:
                raise
            time.sleep(2 * (attempt + 1))


def load_event(city, cfg, d):
    ev = get(f"{GAMMA}/events", {"slug": f"highest-temperature-in-{cfg['poly_slug']}-on-{month_day_year_slug(d)}"})
    if not ev or not ev[0].get("closed"):
        return None
    ev = ev[0]
    finals = []
    for m in ev["markets"]:
        rng = parse_bucket(m["question"])
        if rng is None or not m.get("closed"):
            continue
        outcomes = json.loads(m["outcomes"])
        finals.append((m["conditionId"], city, d.isoformat(), rng[0], rng[1],
                       float(json.loads(m["outcomePrices"])[outcomes.index("Yes")])))
    trades, offset = [], 0
    while True:
        batch = get(f"{DATA_API}/trades", {"eventId": ev["id"], "limit": 500, "offset": offset})
        # 2026-09-27: Data API иногда отвечает объектом-ошибкой вместо списка — день не отмечаем загруженным,
        # он догрузится следующей ночью (раньше такой ответ ронял весь запуск)
        if batch and not isinstance(batch, list):
            raise requests.RequestException(f"Polymarket ответил не списком сделок: {str(batch)[:150]}")
        if not batch:
            break
        trades += batch
        offset += 500
        if len(batch) < 500:
            break
    rows = [(t["transactionHash"], t["conditionId"], t["asset"], t["outcome"], t["side"], float(t["price"]),
             float(t["size"]), int(t["timestamp"]), city, d.isoformat()) for t in trades]
    wallets = [(t["transactionHash"], t["asset"], int(t["timestamp"]), float(t["price"]), float(t["size"]), t["side"],
                t.get("proxyWallet"), city, d.isoformat()) for t in trades if t.get("proxyWallet")]
    return finals, rows, wallets


def main():
    nums = [a for a in sys.argv[1:] if a.isdigit()]
    days_back = int(nums[0]) if nums else 30
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("""CREATE TABLE IF NOT EXISTS poly_trades (
        tx TEXT, condition_id TEXT, asset TEXT, outcome TEXT, side TEXT, price REAL, size REAL,
        ts INTEGER, city TEXT, local_date TEXT,
        PRIMARY KEY (tx, asset, ts, price, size, side))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS poly_market_final (
        condition_id TEXT PRIMARY KEY, city TEXT, local_date TEXT, bucket_lo REAL, bucket_hi REAL, final_yes REAL)""")
    conn.execute("CREATE TABLE IF NOT EXISTS poly_trades_days (city TEXT, local_date TEXT, n INTEGER, PRIMARY KEY (city, local_date))")
    # 2026-09-26: кошелёк трейдера (proxyWallet) — для исследования «умных денег». Отдельная таблица
    # и только добавление строк: перезапись большой poly_trades держала базу по многу секунд и
    # мешала крону (weather_obs_live падал с «database is locked»).
    conn.execute("""CREATE TABLE IF NOT EXISTS poly_trade_wallets (tx TEXT, asset TEXT, ts INTEGER, price REAL, size REAL,
                    side TEXT, wallet TEXT, city TEXT, local_date TEXT, PRIMARY KEY (tx, asset, ts, price, size, side))""")
    conn.commit()
    done = {(r[0], r[1]) for r in conn.execute("SELECT city, local_date FROM poly_trades_days")}
    if "--refresh-wallets" in sys.argv:
        # дни, где у сделок нет кошелька ни в poly_trades.wallet (первая попытка), ни в poly_trade_wallets
        have = {(r[0], r[1]) for r in conn.execute("SELECT DISTINCT city, local_date FROM poly_trade_wallets")}
        have |= {(r[0], r[1]) for r in conn.execute(
            "SELECT city, local_date FROM poly_trades GROUP BY city, local_date HAVING SUM(wallet IS NULL) = 0")} \
            if "wallet" in [r[1] for r in conn.execute("PRAGMA table_info(poly_trades)")] else set()
        done = {k for k in done if k in have}
    jobs = []
    for city, cfg in OBS_CITIES.items():
        today = datetime.now(ZoneInfo(cfg["tz"])).date()
        for i in range(2, days_back + 2):
            d = today - timedelta(days=i)
            if (city, d.isoformat()) not in done:
                jobs.append((city, cfg, d))
    print(f"дней к загрузке: {len(jobs)}", flush=True)

    def work(job):
        # 2026-09-27 (решение Alex: «даже если что-то упало — продолжаем»): любая ошибка по одному дню —
        # день пропускается (не отмечается загруженным, догрузится следующей ночью), остальные идут дальше
        try:
            return job, load_event(*job)
        except Exception as e:
            return job, e

    n_done = 0
    with ThreadPoolExecutor(6) as pool:
        for (city, cfg, d), res in pool.map(work, jobs):
            if isinstance(res, Exception):
                import jobmark
                jobmark.ITEM_ERRORS.append(f"{city} {d}: {type(res).__name__}: {res}")
                print(f"{city} {d}: ошибка — {type(res).__name__}: {res} — пропускаю, догрузится следующей ночью", flush=True)
                continue
            if res is None:
                continue
            finals, rows, wallets = res
            conn.executemany("INSERT OR IGNORE INTO poly_market_final VALUES (?, ?, ?, ?, ?, ?)", finals)
            conn.executemany("""INSERT OR IGNORE INTO poly_trades (tx, condition_id, asset, outcome, side, price, size, ts,
                                city, local_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows)
            conn.executemany("INSERT OR IGNORE INTO poly_trade_wallets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", wallets)
            conn.execute("INSERT OR REPLACE INTO poly_trades_days VALUES (?, ?, ?)", (city, d.isoformat(), len(rows)))
            conn.commit()
            n_done += 1
            if n_done % 100 == 0:
                print(f"загружено дней: {n_done}", flush=True)
    print(f"готово, дней: {n_done}")
    from jobmark import mark
    mark(conn, "weather_trades_history")
    conn.close()


if __name__ == "__main__":
    main()
