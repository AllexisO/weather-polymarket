"""
Идея 6 (2026-09-27, порядок согласован с Alex): влажность почвы и вчерашние осадки.
После дождя/на мокрой почве солнце тратит тепло на испарение — днём холоднее, чем обещают
прогнозы. Раньше проверяли облачность/ветер — это другое, почву не проверяли.

Источник: Open-Meteo historical-forecast, модель ECMWF IFS (у ICON/GFS почвы в архиве нет).
На день D берём то, что известно к 08:00 местного:
- soil_m   — влажность верхнего слоя почвы (0-7 см) в 07:00 местного дня D;
- rain_prev — сумма осадков за вчера (D-1), по анализу.
Пишет в таблицу ml_soil. Сейчас — ТОЛЬКО для проверки на копии базы.
Запуск: POLY_LAB_DB=/data/research/research.sqlite3 python weather_soil.py --from 2024-06-01 --to 2026-09-26 [--part 1/2]
"""

import os
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

from weather_cities import OBS_CITIES

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
API = "https://historical-forecast-api.open-meteo.com/v1/forecast"
CHUNK_DAYS = 180


def arg(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS ml_soil (
        city TEXT NOT NULL, local_date TEXT NOT NULL, soil_m REAL, rain_prev REAL,
        PRIMARY KEY (city, local_date))""")
    conn.commit()


def fetch(cfg, a, b):
    for attempt in range(4):
        r = requests.get(API, params={"latitude": cfg["lat"], "longitude": cfg["lon"], "timezone": cfg["tz"],
                                      "hourly": "soil_moisture_0_to_7cm,precipitation", "models": "ecmwf_ifs025",
                                      "start_date": a.isoformat(), "end_date": b.isoformat()}, timeout=120)
        if r.status_code == 429:
            time.sleep(60)
            continue
        r.raise_for_status()
        return r.json()["hourly"]
    raise RuntimeError("лимит Open-Meteo")


def main():
    start, end = date.fromisoformat(arg("--from", "2024-06-01")), date.fromisoformat(arg("--to", "2026-09-26"))
    k, n = map(int, arg("--part", "1/1").split("/"))
    cities = [c for i, c in enumerate(OBS_CITIES) if i % n == k - 1]
    conn = sqlite3.connect(DB_PATH, timeout=60)
    ensure_schema(conn)
    for city in cities:
        cfg = OBS_CITIES[city]
        rain, soil = {}, {}
        a = start - timedelta(days=1)
        try:
            while a <= end:
                b = min(a + timedelta(days=CHUNK_DAYS - 1), end)
                h = fetch(cfg, a, b)
                for t, sm, pr in zip(h["time"], h["soil_moisture_0_to_7cm"], h["precipitation"]):
                    d = t[:10]
                    if pr is not None:
                        rain[d] = rain.get(d, 0.0) + pr
                    if t[11:13] == "07" and sm is not None:
                        soil[d] = sm
                a = b + timedelta(days=1)
                time.sleep(1)
        except Exception as e:  # noqa: BLE001 — один город не должен ронять загрузку
            print(f"{city}: ошибка — {e}", flush=True)
            continue
        rows = []
        d = start
        while d <= end:
            ds, prev = d.isoformat(), (d - timedelta(days=1)).isoformat()
            if ds in soil or prev in rain:
                rows.append((city, ds, soil.get(ds), rain.get(prev)))
            d += timedelta(days=1)
        conn.executemany("INSERT OR REPLACE INTO ml_soil VALUES (?, ?, ?, ?)", rows)
        conn.commit()
        print(f"{city}: {len(rows)} дней, почва есть в {sum(1 for r in rows if r[2] is not None)}", flush=True)
    conn.close()


if __name__ == "__main__":
    main()
