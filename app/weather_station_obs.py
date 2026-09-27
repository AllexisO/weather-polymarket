"""
Реальные показания метеостанций — "факт" для погоды вместо модельной
оценки Open-Meteo (weather_resolve.py).

2026-09-22: выяснилось, что Open-Meteo "факт" (сетка модели, максимум по
часовым значениям) совпадал с тем, как Polymarket реально закрыл маркет,
только в 13-65% дней (см. CLAUDE.md). Polymarket резолвит по NOAA —
"highest reading under the Temp column" по станции аэропорта, а это
METAR-сводки. Берём те же METAR из архива Iowa Environmental Mesonet
(IEM, бесплатно, без ключа, с историей):
https://mesonet.agron.iastate.edu/request/download.phtml

Пишем две таблицы:
- station_obs — все сводки как есть (температура, ветер, облачность):
  сырьё для будущих поправок по ветру/облачности, история не
  перезаписывается;
- weather_station_daily — дневной максимум по местной дате в единицах
  маркета. Та же схема колонок, что у weather_outcomes, чтобы
  weather_bias.py/дашборд просто переключились на новую таблицу.

У IEM жёсткий лимит частоты ("Too many requests") — пауза между
станциями и повтор с ожиданием.
"""

import csv
import io
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from weather_cities import OBS_CITIES

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
IEM_ASOS = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
IEM_DELAY_S = 6.0

# Станции — из weather_cities.OBS_CITIES (26 городов с 2026-09-23; до
# этого — свой список на 8 станций). Токио/Сеул — те же коды, что были.
# 2026-09-23: с июня (было с 20 августа) — нужна длинная история для весов
# микса моделей (weather_multimodel.py).
HISTORY_START = datetime(2026, 6, 1, tzinfo=timezone.utc)


def fetch_obs(station, start, end):
    params = {
        "station": station,
        "data": ["tmpf", "drct", "sknt", "skyc1", "dwpf", "alti"],  # dwpf/alti — с 2026-09-25, для weather_ml
        "year1": start.year, "month1": start.month, "day1": start.day,
        "year2": end.year, "month2": end.month, "day2": end.day,
        "tz": "Etc/UTC", "format": "onlycomma", "latlon": "no", "missing": "M",
        "report_type": [3, 4],  # 3 — плановые сводки, 4 — внеочередные (SPECI)
    }
    for attempt in range(4):
        r = requests.get(IEM_ASOS, params=params, timeout=60)
        if r.status_code == 429 or "Too many requests" in r.text[:200]:
            time.sleep(IEM_DELAY_S * (attempt + 2))
            continue
        r.raise_for_status()
        return list(csv.DictReader(io.StringIO(r.text)))
    raise requests.RequestException(f"{station}: IEM не отвечает (лимит запросов)")


def num(v):
    return None if v in (None, "", "M") else float(v)


def ensure_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS station_obs (
            city TEXT NOT NULL,
            station TEXT NOT NULL,
            valid_utc TEXT NOT NULL,
            tmpf REAL,
            drct REAL,
            sknt REAL,
            skyc1 TEXT,
            PRIMARY KEY (station, valid_utc)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS weather_station_daily (
            city TEXT NOT NULL,
            local_date TEXT NOT NULL,
            unit TEXT,
            actual_max REAL,
            n_obs INTEGER,
            resolved_at TEXT,
            PRIMARY KEY (city, local_date)
        )
        """
    )
    conn.commit()


def run():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    now = datetime.now(timezone.utc)
    for i, (city, cfg) in enumerate(OBS_CITIES.items()):
        station = cfg["iem"]
        first, last = conn.execute(
            "SELECT MIN(valid_utc), MAX(valid_utc) FROM station_obs WHERE station = ?", (station,)
        ).fetchone()
        ranges = []
        if first and datetime.fromisoformat(first).replace(tzinfo=timezone.utc) > HISTORY_START + timedelta(days=1):
            ranges.append((HISTORY_START, datetime.fromisoformat(first).replace(tzinfo=timezone.utc) + timedelta(days=1)))
        start = datetime.fromisoformat(last).replace(tzinfo=timezone.utc) - timedelta(days=2) if last else HISTORY_START
        ranges.append((start, now + timedelta(days=1)))
        rows = []
        try:
            for j, (a, b) in enumerate(ranges):
                if i or j:
                    time.sleep(IEM_DELAY_S)
                rows += fetch_obs(station, a, b)
        except requests.RequestException as e:
            print(f"{city}: ошибка — {e}", file=sys.stderr)
            continue
        conn.executemany(
            "INSERT OR IGNORE INTO station_obs (city, station, valid_utc, tmpf, drct, sknt, skyc1, dwpf, alti) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(city, station, r["valid"], num(r["tmpf"]), num(r["drct"]), num(r["sknt"]), r.get("skyc1"),
              num(r.get("dwpf")), num(r.get("alti")))
             for r in rows if r.get("valid")],
        )
        conn.commit()

        # Дневной максимум по МЕСТНОЙ дате, только по полностью закончившимся дням.
        tz = ZoneInfo(cfg["tz"])
        today_local = datetime.now(tz).date().isoformat()
        by_date = {}
        for r in conn.execute("SELECT valid_utc, tmpf FROM station_obs WHERE station = ? AND tmpf IS NOT NULL", (station,)):
            d = datetime.fromisoformat(r["valid_utc"]).replace(tzinfo=timezone.utc).astimezone(tz).date().isoformat()
            if d < today_local:
                by_date.setdefault(d, []).append(r["tmpf"])
        daily = []
        for d, vals in by_date.items():
            mx = max(vals)
            if cfg["unit"] == "celsius":
                # METAR вне США — целые °C, IEM отдаёт их переведёнными в °F;
                # переводим обратно и округляем до исходного целого.
                mx = round((mx - 32) * 5 / 9)
            daily.append((city, d, cfg["unit"], mx, len(vals), now.isoformat()))
        conn.executemany(
            "INSERT OR REPLACE INTO weather_station_daily (city, local_date, unit, actual_max, n_obs, resolved_at) VALUES (?, ?, ?, ?, ?, ?)",
            daily,
        )
        conn.commit()
        print(f"{city} ({station}): {len(rows)} сводок, {len(daily)} дней")
    conn.close()


if __name__ == "__main__":
    run()
