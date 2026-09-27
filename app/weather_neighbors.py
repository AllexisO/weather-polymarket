"""
Соседние METAR-станции вокруг каждого города — для обучаемой модели
(2026-09-25, пункт 4 плана Alex). Идея: если утром станция в 100 км
с наветренной стороны заметно теплее обычного, этот воздух идёт к нам.

- выбор: станции с METAR в 25-180 км, по одной на сектор (5 секторов
  по 72°), ближайшая в секторе — таблица ml_neighbors;
- история замеров — из архива Iowa Mesonet в station_obs с меткой
  city = 'nb:<город>' (основные станции не трогаем);
- python weather_neighbors.py --select   — подобрать соседей;
- python weather_neighbors.py --backfill — история с 2025-06-01;
- python weather_neighbors.py            — догрузка последних дней (крон).
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
IEM = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
HISTORY_START = date(2025, 6, 1)
MIN_KM, MAX_KM, SECTORS = 25, 180, 5


def dist_bearing(lat1, lon1, lat2, lon2):
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    d = 6371 * math.acos(min(1, math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl)))
    b = math.degrees(math.atan2(math.sin(dl) * math.cos(p2), math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)))
    return d, (b + 360) % 360


def select(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS ml_neighbors (
        city TEXT, icao TEXT, iem TEXT, lat REAL, lon REAL, dist_km REAL, bearing REAL, PRIMARY KEY (city, icao))""")
    main = {c["icao"] for c in OBS_CITIES.values()}
    for city, cfg in OBS_CITIES.items():
        lat, lon = cfg["lat"], cfg["lon"]
        dlat, dlon = MAX_KM / 111, MAX_KM / (111 * max(math.cos(math.radians(lat)), 0.2))
        box = f"{lat - dlat},{lon - dlon},{lat + dlat},{lon + dlon}"
        st = requests.get("https://aviationweather.gov/api/data/stationinfo", params={"bbox": box, "format": "json"},
                          headers={"User-Agent": "polymarket-lab research"}, timeout=60).json()
        best = {}
        for s in st:
            icao = s.get("icaoId")
            if not icao or "METAR" not in (s.get("siteType") or []) or icao == cfg["icao"]:
                continue
            d, b = dist_bearing(lat, lon, s["lat"], s["lon"])
            if not MIN_KM <= d <= MAX_KM:
                continue
            sec = int(b // (360 / SECTORS))
            if sec not in best or d < best[sec][2]:
                best[sec] = (icao, s, d, b)
        conn.execute("DELETE FROM ml_neighbors WHERE city = ?", (city,))
        for icao, s, d, b in best.values():
            iem = icao[1:] if icao.startswith("K") and len(icao) == 4 else icao
            conn.execute("INSERT INTO ml_neighbors VALUES (?, ?, ?, ?, ?, ?, ?)", (city, icao, iem, s["lat"], s["lon"], d, b))
        conn.commit()
        print(f"{city}: соседей {len(best)} — " + ", ".join(f"{v[0]} {v[2]:.0f}км/{v[3]:.0f}°" for v in best.values()), flush=True)
        time.sleep(1)


def fetch(iem, start, end):
    params = {"station": iem, "data": ["tmpf", "dwpf", "drct", "sknt"], "year1": start.year, "month1": start.month,
              "day1": start.day, "year2": end.year, "month2": end.month, "day2": end.day, "tz": "Etc/UTC",
              "format": "onlycomma", "latlon": "no", "missing": "M", "report_type": [3, 4]}
    for attempt in range(5):
        r = requests.get(IEM, params=params, timeout=300)
        if r.status_code == 429 or "Too many requests" in r.text[:200]:
            time.sleep(15 * (attempt + 1))
            continue
        return list(csv.DictReader(io.StringIO(r.text)))
    return []


def load(conn, backfill):
    num = lambda v: None if v in (None, "", "M") else float(v)
    end = datetime.now(timezone.utc).date() + timedelta(days=1)
    for city, icao, iem in conn.execute("SELECT city, icao, iem FROM ml_neighbors").fetchall():
        last = conn.execute("SELECT MAX(valid_utc) FROM station_obs WHERE station = ?", (iem,)).fetchone()[0]
        start = HISTORY_START if (backfill or not last) else datetime.fromisoformat(last).date() - timedelta(days=2)
        rows = fetch(iem, start, end)
        conn.executemany(
            "INSERT OR IGNORE INTO station_obs (city, station, valid_utc, tmpf, drct, sknt, dwpf) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(f"nb:{city}", iem, x["valid"], num(x["tmpf"]), num(x["drct"]), num(x["sknt"]), num(x["dwpf"]))
             for x in rows if x.get("valid") and num(x.get("tmpf")) is not None])
        conn.commit()
        print(f"{city} / {icao}: {len(rows)} сводок", flush=True)
        time.sleep(6)


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH, timeout=60)
    if "--select" in sys.argv:
        select(conn)
    else:
        load(conn, "--backfill" in sys.argv)
    conn.close()
