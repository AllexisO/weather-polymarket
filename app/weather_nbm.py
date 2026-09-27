"""
Прогнозы NBM (National Blend of Models, служба погоды США) для станций США —
статистически подогнанный под станцию прогноз максимума (2026-09-26, идея 4
из списка Alex). Источник — архив Iowa Mesonet (бесплатно, без ключа):
mesonet.agron.iastate.edu/cgi-bin/request/mos.py, модель NBS.

В архиве выпуски 01, 07, 13, 19 UTC; поле txn в строке с ftime 00Z следующих
суток — прогноз дневного максимума (окно 12-00Z, °F).

Пишет nbm_forecasts (station, runtime, ftime, txn). Разовая загрузка истории с
2025-06-01 + догрузка новых дней (повторный запуск берёт последние DAYS дней).
Запуск: python weather_nbm.py [--history]
"""

import csv
import io
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from weather_cities import OBS_CITIES

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/mos.py"
START = date(2025, 6, 1)
DAYS = 7


def fetch(station, sts, ets):
    for attempt in range(5):
        try:
            r = requests.get(URL, params={"station": station, "model": "NBS", "sts": f"{sts:%Y-%m-%dT%H:%MZ}",
                                          "ets": f"{ets:%Y-%m-%dT%H:%MZ}", "format": "csv"}, timeout=120)
            r.raise_for_status()
            return [row for row in csv.DictReader(io.StringIO(r.text)) if row.get("txn")]
        except requests.RequestException:
            time.sleep(5 * (attempt + 1))
    return []


def main():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("""CREATE TABLE IF NOT EXISTS nbm_forecasts (station TEXT, runtime TEXT, ftime TEXT, txn REAL,
                    PRIMARY KEY (station, runtime, ftime))""")
    stations = sorted({c["icao"] for c in OBS_CITIES.values() if c["icao"].startswith("K")})
    now = datetime.now(timezone.utc)
    start = datetime(START.year, START.month, START.day, tzinfo=timezone.utc) if "--history" in sys.argv else now - timedelta(days=DAYS)
    for st in stations:
        n, t = 0, start
        while t < now:
            t2 = min(t + timedelta(days=31), now)
            rows = fetch(st, t, t2)
            conn.executemany("INSERT OR REPLACE INTO nbm_forecasts VALUES (?, ?, ?, ?)",
                             [(st, r["runtime"], r["ftime"], float(r["txn"])) for r in rows])
            conn.commit()
            n += len(rows)
            t = t2
            time.sleep(1)
        print(f"{st}: {n} прогнозов максимума/минимума", flush=True)
    from jobmark import mark
    mark(conn, "weather_nbm")
    conn.close()


if __name__ == "__main__":
    main()
