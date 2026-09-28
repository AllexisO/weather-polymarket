"""
Свежий прогноз к моменту решения (2026-09-28, исследование «что мы упускаем»).

Модель учится на прогнозах day1 (выпущены за 24 ч до часа прогноза), а в 08:00 местного, когда мы решаем,
уже есть прогоны на 6-18 ч свежее — и рынок их видит. Живой утренний прогноз (mm_forecasts 'live', 22-26.09)
ошибается в среднем на 0.80°C против 0.92°C у day1.

Честная история: архив отдельных прогонов Open-Meteo (single-runs-api, ECMWF IFS HRES 9 км с 03.2024).
Для города и дня d прогон R(d) — последний из 00/06/12/18Z, у которого запуск + AVAIL_H ≤ 08:00 местного d
(запас на выпуск данных). fresh(d) — максимум дня d по R(d); stale(d) — максимум дня d по R(d−1) (то, что было
известно накануне в 08:00). Один запрос на город-день.

Пишет таблицу fresh_hres в ту базу, что в POLY_LAB_DB (только копия!). Останавливается при ошибке лимита.
Запуск: python weather_fresh_fc.py 2026-08-27 2026-09-26
"""

import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from weather_cities import OBS_CITIES

DB_PATH = Path(os.environ.get("POLY_LAB_DB", "/data/research/research.sqlite3"))
API = "https://single-runs-api.open-meteo.com/v1/forecast"
AVAIL_H = 7
DECIDE_H = 8


def run_for(city_tz, d):
    dec = datetime(d.year, d.month, d.day, DECIDE_H, tzinfo=ZoneInfo(city_tz)).astimezone(timezone.utc)
    t = dec - timedelta(hours=AVAIL_H)
    return t.replace(hour=t.hour // 6 * 6, minute=0, second=0, microsecond=0)


def main(d0, d1):
    assert "research" in str(DB_PATH), "только на копии базы"
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("""CREATE TABLE IF NOT EXISTS fresh_hres (city TEXT, local_date TEXT, run_utc TEXT, kind TEXT, max_c REAL,
                    PRIMARY KEY (city, local_date, kind))""")
    have = {(r[0], r[1]) for r in conn.execute("SELECT city, local_date FROM fresh_hres WHERE kind = 'fresh'")}
    calls = 0
    for city, cfg in OBS_CITIES.items():
        d = d0 - timedelta(days=1)
        while d <= d1:
            if (city, d.isoformat()) in have and d >= d0:
                d += timedelta(days=1)  # уже загружено (повторный запуск догружает только пропуски)
                continue
            run = run_for(cfg["tz"], d)
            try:
                r = requests.get(API, params={"latitude": cfg["lat"], "longitude": cfg["lon"], "run": run.strftime("%Y-%m-%dT%H:%M"),
                                              "hourly": "temperature_2m", "models": "ecmwf_ifs", "timezone": cfg["tz"],
                                              "forecast_days": 3}, timeout=30)
                calls += 1
                if r.status_code == 429 or "limit" in r.text.lower()[:300]:
                    print(f"СТОП: Open-Meteo — лимит ({r.status_code}): {r.text[:200]}; запросов сделано {calls}", flush=True)
                    conn.commit()
                    return
                r.raise_for_status()
                h = r.json()["hourly"]
            except (requests.RequestException, KeyError, ValueError) as e:
                print(f"{city} {d} {run:%m-%d %HZ}: ошибка — {e}", flush=True)
                d += timedelta(days=1)
                continue
            by_day = {}
            for t, v in zip(h["time"], h["temperature_2m"]):
                if v is not None:
                    by_day.setdefault(t[:10], []).append(v)
            rows = []
            for day, kind in ((d, "fresh"), (d + timedelta(days=1), "stale")):
                vals = by_day.get(day.isoformat())
                if vals and len(vals) >= 20 and d0 <= day <= d1:
                    rows.append((city, day.isoformat(), run.isoformat(), kind, max(vals)))
            conn.executemany("INSERT OR REPLACE INTO fresh_hres VALUES (?, ?, ?, ?, ?)", rows)
            d += timedelta(days=1)
            time.sleep(0.25)
        conn.commit()
        print(f"{city}: готово, всего запросов {calls}", flush=True)
    conn.close()


if __name__ == "__main__":
    main(date.fromisoformat(sys.argv[1]), date.fromisoformat(sys.argv[2]))
