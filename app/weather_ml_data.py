"""
Данные для обучаемой модели прогноза (weather_ml.py), 2026-09-25.

1. Замеры станций: к уже собранным температуре/ветру/облачности
   (station_obs) догружаем точку росы (влажность) и давление (altimeter)
   из того же архива Iowa Mesonet — колонки dwpf, alti.
2. Прогнозные условия на день (ml_fcst_vars): облачность, солнечная
   радиация, относительная влажность, точка росы, ветер, осадки — по
   модели ECMWF IFS, прогноз, выпущенный ЗА СУТКИ (previous-runs API,
   *_previous_day1), усреднённые за 11:00-17:00 местного (часы, когда
   набирается дневной максимум). Это "спутник/радар" в виде прогноза:
   сколько будет солнца, облаков и влаги в пик дня.

Запуск: python weather_ml_data.py            — догрузка (идемпотентно)
        python weather_ml_data.py --obs-backfill — разово: dwpf/alti за всю историю
"""

import csv
import io
import math
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from weather_cities import OBS_CITIES

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
IEM_ASOS = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
PREVIOUS_RUNS_API = "https://previous-runs-api.open-meteo.com/v1/forecast"
HISTORY_START = date(2026, 6, 1)
FCST_MODEL = "ecmwf_ifs025"
FCST_VARS = ["cloud_cover", "shortwave_radiation", "relative_humidity_2m", "dew_point_2m",
             "wind_speed_10m", "wind_direction_10m", "precipitation"]
PEAK_HOURS = range(11, 18)


def ensure_schema(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(station_obs)")]
    for col in ("dwpf", "alti"):
        if col not in cols:
            conn.execute(f"ALTER TABLE station_obs ADD COLUMN {col} REAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS ml_fcst_vars (
            city TEXT NOT NULL, local_date TEXT NOT NULL, var TEXT NOT NULL, value REAL,
            PRIMARY KEY (city, local_date, var))"""
    )
    conn.commit()


def num(v):
    return None if v in (None, "", "M") else float(v)


def obs_backfill(conn):
    """Точка росы и давление за всю историю — одним запросом на станцию."""
    end = datetime.now(timezone.utc) + timedelta(days=1)
    for i, (city, cfg) in enumerate(OBS_CITIES.items()):
        if i:
            time.sleep(6)
        params = {"station": cfg["iem"], "data": ["dwpf", "alti"], "year1": HISTORY_START.year,
                  "month1": HISTORY_START.month, "day1": HISTORY_START.day, "year2": end.year,
                  "month2": end.month, "day2": end.day, "tz": "Etc/UTC", "format": "onlycomma",
                  "latlon": "no", "missing": "M", "report_type": [3, 4]}
        for attempt in range(4):
            r = requests.get(IEM_ASOS, params=params, timeout=120)
            if r.status_code == 429 or "Too many requests" in r.text[:200]:
                time.sleep(12 * (attempt + 1))
                continue
            break
        rows = list(csv.DictReader(io.StringIO(r.text)))
        conn.executemany("UPDATE station_obs SET dwpf = ?, alti = ? WHERE station = ? AND valid_utc = ?",
                         [(num(x["dwpf"]), num(x["alti"]), cfg["iem"], x["valid"]) for x in rows if x.get("valid")])
        conn.commit()
        print(f"{city}: точка росы/давление — {len(rows)} сводок", flush=True)


def fcst_vars(conn):
    """Прогнозные условия на день за сутки вперёд, 11-17 местного."""
    tomorrow = (datetime.now(timezone.utc).date() + timedelta(days=2)).isoformat()
    for city, cfg in OBS_CITIES.items():
        last = conn.execute("SELECT MAX(local_date) FROM ml_fcst_vars WHERE city = ?", (city,)).fetchone()[0]
        start = (date.fromisoformat(last) - timedelta(days=2)).isoformat() if last else HISTORY_START.isoformat()
        hourly = ",".join(f"{v}_previous_day1" for v in FCST_VARS)
        for attempt in range(3):
            r = requests.get(PREVIOUS_RUNS_API, params={
                "latitude": cfg["lat"], "longitude": cfg["lon"], "timezone": cfg["tz"], "models": FCST_MODEL,
                "hourly": hourly, "start_date": start, "end_date": tomorrow}, timeout=60)
            if r.status_code == 429:
                time.sleep(30)
                continue
            break
        if r.status_code != 200:
            print(f"{city}: ошибка {r.status_code}", file=sys.stderr)
            continue
        h = r.json()["hourly"]
        acc = {}
        for i, t in enumerate(h["time"]):
            if int(t[11:13]) not in PEAK_HOURS:
                continue
            for v in FCST_VARS:
                x = h.get(f"{v}_previous_day1")[i] if h.get(f"{v}_previous_day1") else None
                if x is None:
                    continue
                a = acc.setdefault((t[:10], v), [])
                a.append(x)
        rows = []
        for (d, v), xs in acc.items():
            if v == "wind_direction_10m":
                # направление — средний вектор, иначе 350° и 10° дали бы 180°
                s = sum(math.sin(math.radians(x)) for x in xs)
                c = sum(math.cos(math.radians(x)) for x in xs)
                rows.append((city, d, "wind_dir_sin", s / len(xs)))
                rows.append((city, d, "wind_dir_cos", c / len(xs)))
            elif v == "precipitation":
                rows.append((city, d, v, sum(xs)))
            else:
                rows.append((city, d, v, sum(xs) / len(xs)))
        conn.executemany("INSERT OR REPLACE INTO ml_fcst_vars VALUES (?, ?, ?, ?)", rows)
        conn.commit()
        time.sleep(1.5)
        print(f"{city}: прогнозные условия — {len({r[1] for r in rows})} дней", flush=True)


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH, timeout=60)
    ensure_schema(conn)
    if "--obs-backfill" in sys.argv:
        obs_backfill(conn)
    fcst_vars(conn)
    conn.close()
