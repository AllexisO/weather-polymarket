"""
История цен погодных маркетов Polymarket (CLOB /prices-history) — для
бэктестов на месяцах данных, а не только на наших собственных снимках
раз в 2 часа (2026-09-23).

Зачем:
- стратегия по живым замерам станции (weather_obs_live.py) решает за
  минуты — снимков раз в 2 часа для проверки мало;
- у Мадрида и Торонто вообще нет наших прошлых снимков, а маркеты по
  ним есть с июня;
- та же история пригодится для проверки "рынок отстаёт от выхода новых
  прогонов моделей".

Пишет:
- price_history — цена Yes каждого бакета с шагом FIDELITY_MIN минут,
  окно — с полудня накануне до конца местного дня;
- weather_poly_outcomes — официальный выигравший бакет (та же таблица,
  что у weather_poly_resolve.py).

Внимание: p в /prices-history — цена последней сделки/середина, не цена,
по которой реально можно было купить. В бэктестах добавлять спред.

Разовая загрузка + догрузка новых дней (повторный запуск пропускает уже
загруженные дни). Бесплатно, без ключа.
"""

import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from weather_cities import OBS_CITIES
from weather_edge import GAMMA, month_day_year_slug, parse_bucket

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
CLOB = "https://clob.polymarket.com"
# 2026-09-26: было 2026-06-01. Маркеты есть раньше: до ~02.2026 — адрес без года
# («...-on-january-15»); Нью-Йорк и Лондон — с весны 2025, ещё 6 городов — с 12.2025,
# остальные — с 02-04.2026. Начало = начало истории обучения (weather_history_extend.py).
HISTORY_START = date(2025, 6, 1)
FIDELITY_MIN = 5
PARALLEL = 6  # параллельных запросов истории на день (по бакетам) — иначе загрузка ~4 часа


def ensure_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS price_history (
            city TEXT NOT NULL,
            local_date TEXT NOT NULL,
            bucket_lo REAL NOT NULL,
            bucket_hi REAL NOT NULL,
            t_utc INTEGER NOT NULL,
            p REAL,
            PRIMARY KEY (city, local_date, bucket_lo, t_utc)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS price_history_days (
            city TEXT NOT NULL,
            local_date TEXT NOT NULL,
            n_buckets INTEGER,
            PRIMARY KEY (city, local_date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS weather_poly_outcomes (
            city TEXT NOT NULL, local_date TEXT NOT NULL, win_lo REAL, win_hi REAL, resolved_at TEXT,
            PRIMARY KEY (city, local_date)
        )
        """
    )
    conn.commit()


def get(url, params):
    for attempt in range(5):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == 4:
                raise
            time.sleep(2 * (attempt + 1))
    return None


def _find_event(cfg, d):
    """Событие дня: сначала адрес с годом, затем старый без года (проверяем, что это именно наш год)."""
    events = get(f"{GAMMA}/events", {"slug": f"highest-temperature-in-{cfg['poly_slug']}-on-{month_day_year_slug(d)}"})
    if events:
        return events
    old = f"highest-temperature-in-{cfg['poly_slug']}-on-{d.strftime('%B').lower()}-{d.day}"
    events = get(f"{GAMMA}/events", {"slug": old})
    if events and (events[0].get("endDate") or "")[:10] == d.isoformat():
        return events
    return None


def _to_city_unit(rng, question, cfg):
    """Старые маркеты бывали в других единицах (Лондон в 2025 — °F): переводим границы
    в единицы города, чтобы признаки рынка и итоги считались одинаково."""
    q_unit = "fahrenheit" if "°F" in question else ("celsius" if "°C" in question else cfg["unit"])
    if q_unit == cfg["unit"]:
        return rng
    conv = (lambda v: (v - 32) * 5 / 9) if q_unit == "fahrenheit" else (lambda v: v * 9 / 5 + 32)
    return tuple(v if abs(v) >= 900 else round(conv(v), 3) for v in rng)


def load_day(conn, city, cfg, d):
    events = _find_event(cfg, d)
    if not events:
        # прошлый день без маркета — запоминаем, чтобы ночная догрузка не спрашивала его снова
        if d < date.today() - timedelta(days=3):
            conn.execute("INSERT OR REPLACE INTO price_history_days (city, local_date, n_buckets) VALUES (?, ?, 0)",
                         (city, d.isoformat()))
            conn.commit()
        return 0
    tz = ZoneInfo(cfg["tz"])
    day_start = datetime(d.year, d.month, d.day, tzinfo=tz)
    start_ts = int((day_start - timedelta(hours=12)).timestamp())
    end_ts = int((day_start + timedelta(days=1)).timestamp())
    winners, jobs = [], []
    for m in events[0]["markets"]:
        rng = parse_bucket(m["question"])
        if rng is None:
            continue
        rng = _to_city_unit(rng, m["question"], cfg)
        if not m.get("clobTokenIds"):
            continue  # маркет создан, но торги по нему не открывались — цен нет (2026-09-26, Чжэнчжоу)
        if m.get("closed"):
            outcomes = json.loads(m["outcomes"])
            if float(json.loads(m["outcomePrices"])[outcomes.index("Yes")]) > 0.99:
                winners.append(rng)
        jobs.append((rng, json.loads(m["clobTokenIds"])[0]))  # первый токен — Yes

    def fetch(job):
        rng, token = job
        hist = get(f"{CLOB}/prices-history",
                   {"market": token, "startTs": start_ts, "endTs": end_ts, "fidelity": FIDELITY_MIN})
        return rng, (hist or {}).get("history", [])

    with ThreadPoolExecutor(PARALLEL) as pool:
        results = list(pool.map(fetch, jobs))
    for rng, pts in results:
        conn.executemany(
            "INSERT OR IGNORE INTO price_history (city, local_date, bucket_lo, bucket_hi, t_utc, p) VALUES (?, ?, ?, ?, ?, ?)",
            [(city, d.isoformat(), rng[0], rng[1], int(p["t"]), float(p["p"])) for p in pts],
        )
    n = len(results)
    if len(winners) == 1:
        conn.execute(
            "INSERT OR IGNORE INTO weather_poly_outcomes (city, local_date, win_lo, win_hi, resolved_at) VALUES (?, ?, ?, ?, ?)",
            (city, d.isoformat(), winners[0][0], winners[0][1], datetime.now(timezone.utc).isoformat()),
        )
    conn.execute("INSERT OR REPLACE INTO price_history_days (city, local_date, n_buckets) VALUES (?, ?, ?)",
                 (city, d.isoformat(), n))
    conn.commit()
    return n


def run():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    for city, cfg in OBS_CITIES.items():
        done = {r[0] for r in conn.execute("SELECT local_date FROM price_history_days WHERE city = ?", (city,))}
        today = datetime.now(ZoneInfo(cfg["tz"])).date()
        d, loaded = HISTORY_START, 0
        while d < today:
            if d.isoformat() not in done:
                try:
                    load_day(conn, city, cfg, d)
                    loaded += 1
                except requests.RequestException as e:
                    print(f"{city} {d}: ошибка — {e}", file=sys.stderr)
            d += timedelta(days=1)
        print(f"{city}: загружено дней {loaded}", flush=True)
    conn.close()


if __name__ == "__main__":
    run()
