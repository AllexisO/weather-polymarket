"""
Резолвер исходов для weather_edge.py: сверяет накопленные снимки с тем,
что реально произошло, чтобы можно было честно посчитать точность модели
и рынка (а не только смотреть на edge в моменте, который сам по себе
ничего не доказывает).

Логика: для каждого (city, local_date), где локальный день у этого города
уже полностью закончился, тянем реально наблюдённый дневной максимум
через Open-Meteo forecast API с параметром past_days (не archive-api —
там задержка публикации ~5 дней, для быстрой итерации это слишком долго;
past_days отдаёт недавнюю историю почти сразу). Пишем в weather_outcomes,
одна строка на (city, local_date) — не перезаписывается повторно
(INSERT OR IGNORE), чтобы после первого резолва не дёргать API зря.

Запускается по крону отдельно от weather_edge.py (см. README) — не на
каждый снимок, а раз в несколько часов, ничего не торгует.
"""

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from weather_edge import CITIES

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"

PAST_DAYS = 10  # с запасом покрывает всю историю снимков на этой стадии проекта


def fetch_actual_daily_max(lat, lon, tz_name, unit):
    r = requests.get(
        OPEN_METEO_FORECAST,
        params={
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m",
            "temperature_unit": unit,
            "timezone": tz_name,
            "past_days": PAST_DAYS,
            "forecast_days": 1,
        },
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()["hourly"]
    by_date = {}
    for t, v in zip(data["time"], data["temperature_2m"]):
        if v is None:
            continue
        d = t.split("T")[0]
        by_date.setdefault(d, []).append(v)
    return {d: max(vals) for d, vals in by_date.items()}


def ensure_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS weather_outcomes (
            city TEXT NOT NULL,
            local_date TEXT NOT NULL,
            unit TEXT,
            actual_max REAL,
            resolved_at TEXT,
            PRIMARY KEY (city, local_date)
        )
        """
    )
    conn.commit()


def run():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    now = datetime.now().astimezone()

    total_resolved = 0
    for city, cfg in CITIES.items():
        pending_dates = {
            r["local_date"]
            for r in conn.execute(
                """
                SELECT DISTINCT s.local_date FROM snapshots s
                LEFT JOIN weather_outcomes o ON s.city = o.city AND s.local_date = o.local_date
                WHERE s.city = ? AND o.city IS NULL
                """,
                (city,),
            )
        }
        if not pending_dates:
            continue

        tz = ZoneInfo(cfg["tz"])
        today_local = datetime.now(tz).date().isoformat()
        # Резолвим только полностью закончившиеся дни — сегодняшний день
        # ещё не завершён, реальный максимум пока не наступил.
        pending_dates = {d for d in pending_dates if d < today_local}
        if not pending_dates:
            continue

        try:
            actual_by_date = fetch_actual_daily_max(cfg["lat"], cfg["lon"], cfg["tz"], cfg["unit"])
        except requests.RequestException as e:
            print(f"{city}: ошибка запроса — {e}", file=sys.stderr)
            continue

        rows = []
        for d in pending_dates:
            if d in actual_by_date:
                rows.append((city, d, cfg["unit"], actual_by_date[d], now.isoformat()))

        if rows:
            conn.executemany(
                """
                INSERT OR IGNORE INTO weather_outcomes (city, local_date, unit, actual_max, resolved_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
            total_resolved += len(rows)
            for city_, d, unit, actual, _ in rows:
                unit_sym = "°F" if unit == "fahrenheit" else "°C"
                print(f"{city_} {d}: факт={actual:.1f}{unit_sym}")

    print(f"Резолвнуто дней: {total_resolved}")
    conn.close()


if __name__ == "__main__":
    run()
