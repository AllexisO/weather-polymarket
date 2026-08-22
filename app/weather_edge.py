"""
Сбор диагностики: расхождение между ансамблевым прогнозом погоды (GFS,
Open-Meteo, бесплатно) и ценой дневного маркета Polymarket "Highest
temperature in <город>". Ничего не торгует — только считает и пишет edge
в sqlite, чтобы через 1-2 недели честно понять, есть ли расхождение вообще,
и если да — систематическое оно или шум одного момента.

Запускается по крону раз в несколько часов (см. README). Каждый запуск —
один снимок по всем городам из CITIES, добавляет строки в таблицу
snapshots (не upsert — история снимков копится специально, чтобы потом
видеть, как расхождение менялось в течение дня).
"""

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# Локально (без Docker) — файл рядом с проектом. В контейнере путь
# приходит через переменную окружения (см. docker-compose.yml), чтобы не
# зависеть от того, куда COPY положил app/ внутри образа.
DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))

# lat/lon — координаты города, tz — таймзона для расчёта "сегодняшнего"
# дневного максимума, poly_slug — сегмент URL Polymarket
# (highest-temperature-in-{poly_slug}-on-{month}-{day}-{year}).
CITIES = {
    # unit — единица, в которой Polymarket задаёт бакеты ДЛЯ ЭТОГО города:
    # US-города — Fahrenheit с шагом 2°, остальные — Celsius с шагом 1°.
    # Ансамбль запрашиваем сразу в той же единице, чтобы не путать конвертацию.
    "nyc":       {"lat": 40.7128, "lon": -74.0060, "tz": "America/New_York", "poly_slug": "nyc",      "unit": "fahrenheit"},
    "paris":     {"lat": 48.8566, "lon": 2.3522,   "tz": "Europe/Paris",     "poly_slug": "paris",    "unit": "celsius"},
    "london":    {"lat": 51.5074, "lon": -0.1278,  "tz": "Europe/London",    "poly_slug": "london",   "unit": "celsius"},
    "tokyo":     {"lat": 35.6762, "lon": 139.6503, "tz": "Asia/Tokyo",       "poly_slug": "tokyo",    "unit": "celsius"},
    "seoul":     {"lat": 37.5665, "lon": 126.9780, "tz": "Asia/Seoul",       "poly_slug": "seoul",    "unit": "celsius"},
    "hong_kong": {"lat": 22.3193, "lon": 114.1694, "tz": "Asia/Hong_Kong",   "poly_slug": "hong-kong", "unit": "celsius"},
    "beijing":   {"lat": 39.9042, "lon": 116.4074, "tz": "Asia/Shanghai",    "poly_slug": "beijing",  "unit": "celsius"},
}

GAMMA = "https://gamma-api.polymarket.com"
OPEN_METEO_ENSEMBLE = "https://ensemble-api.open-meteo.com/v1/ensemble"

# Разбор диапазона из текста вопроса маркета. У Polymarket две разные схемы
# бакетов в одном и том же продукте: US-города — "X or below" / "between X-Y"
# / "X or higher" с шагом 2°F; остальные — "be X" с шагом 1°C (без слова
# between), тогда бакет — X±0.5. Юнит (°F/°C) не разбираем регуляркой:
# он уже задан в CITIES[...]["unit"] и определяет, в чём запрошен ансамбль.
RE_BELOW = re.compile(r"(-?\d+)\s*°[CF] or below")
RE_RANGE = re.compile(r"between (-?\d+)-(-?\d+)\s*°[CF]")
RE_ABOVE = re.compile(r"(-?\d+)\s*°[CF] or higher")
RE_EXACT = re.compile(r"be (-?\d+)\s*°[CF] on")


def month_day_year_slug(dt_local):
    return dt_local.strftime("%B-%-d-%Y").lower()


def fetch_ensemble_daily_max(lat, lon, tz_name, unit):
    """31 GFS-член + контроль -> список дневных максимумов на СЕГОДНЯ по местному времени."""
    r = requests.get(
        OPEN_METEO_ENSEMBLE,
        params={
            "latitude": lat,
            "longitude": lon,
            "models": "gfs_seamless",
            "hourly": "temperature_2m",
            "temperature_unit": unit,
            "timezone": tz_name,
            "forecast_days": 2,
        },
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()["hourly"]
    tz = ZoneInfo(tz_name)
    today_local = datetime.now(tz).date()

    member_cols = [k for k in data.keys() if k.startswith("temperature_2m")]
    times = data["time"]

    daily_max = []
    for col in member_cols:
        vals_today = [
            data[col][i]
            for i, t in enumerate(times)
            if datetime.fromisoformat(t).date() == today_local and data[col][i] is not None
        ]
        if vals_today:
            daily_max.append(max(vals_today))
    return daily_max


def parse_bucket(question):
    m = RE_BELOW.search(question)
    if m:
        return (-999.0, float(m.group(1)) + 0.5)
    m = RE_RANGE.search(question)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return (lo - 0.5, hi + 0.5)
    m = RE_ABOVE.search(question)
    if m:
        return (float(m.group(1)) - 0.5, 999.0)
    m = RE_EXACT.search(question)
    if m:
        v = float(m.group(1))
        return (v - 0.5, v + 0.5)
    return None


def fetch_polymarket_buckets(poly_slug, dt_local):
    slug = f"highest-temperature-in-{poly_slug}-on-{month_day_year_slug(dt_local)}"
    r = requests.get(f"{GAMMA}/events", params={"slug": slug}, timeout=20)
    r.raise_for_status()
    events = r.json()
    if not events:
        return None
    markets = events[0]["markets"]
    buckets = []
    for m in markets:
        rng = parse_bucket(m["question"])
        if rng is None:
            continue
        prices = json.loads(m["outcomePrices"])
        outcomes = json.loads(m["outcomes"])
        yes_price = float(prices[outcomes.index("Yes")])
        buckets.append({"lo": rng[0], "hi": rng[1], "market_p": yes_price, "question": m["question"]})
    return {"event_vol": events[0].get("volume", 0), "buckets": buckets}


def model_prob(ensemble, lo, hi):
    if not ensemble:
        return None
    hits = sum(1 for v in ensemble if lo < v <= hi)
    return hits / len(ensemble)


def ensure_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            city TEXT NOT NULL,
            local_date TEXT NOT NULL,
            local_hour INTEGER,
            unit TEXT,
            bucket_lo REAL,
            bucket_hi REAL,
            market_p REAL,
            model_p REAL,
            edge REAL,
            event_vol REAL,
            ensemble_n INTEGER
        )
        """
    )
    conn.commit()


def run():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    ensure_schema(conn)
    now = datetime.now(timezone.utc)

    for city, cfg in CITIES.items():
        tz = ZoneInfo(cfg["tz"])
        today_local = datetime.now(tz)
        try:
            ensemble = fetch_ensemble_daily_max(cfg["lat"], cfg["lon"], cfg["tz"], cfg["unit"])
            market = fetch_polymarket_buckets(cfg["poly_slug"], today_local)
        except requests.RequestException as e:
            print(f"{city}: ошибка запроса — {e}", file=sys.stderr)
            continue

        if market is None or not market["buckets"]:
            print(f"{city}: маркет на сегодня не найден", file=sys.stderr)
            continue

        rows = []
        for b in market["buckets"]:
            mp = model_prob(ensemble, b["lo"], b["hi"])
            if mp is None:
                continue
            edge = mp - b["market_p"]
            rows.append(
                (
                    now.isoformat(),
                    city,
                    today_local.date().isoformat(),
                    today_local.hour,
                    cfg["unit"],
                    b["lo"],
                    b["hi"],
                    b["market_p"],
                    mp,
                    edge,
                    market["event_vol"],
                    len(ensemble),
                )
            )
        conn.executemany(
            """
            INSERT INTO snapshots
            (ts_utc, city, local_date, local_hour, unit, bucket_lo, bucket_hi, market_p, model_p, edge, event_vol, ensemble_n)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()

        unit_sym = "°F" if cfg["unit"] == "fahrenheit" else "°C"
        best = max(rows, key=lambda r: abs(r[9])) if rows else None
        if best:
            print(f"{city} ({today_local.hour:02d}:00 местных): макс |edge|={best[9]:+.3f} "
                  f"в бакете {best[5]}-{best[6]}{unit_sym} (модель={best[8]:.2f}, рынок={best[7]:.2f})")

    conn.close()


if __name__ == "__main__":
    run()
