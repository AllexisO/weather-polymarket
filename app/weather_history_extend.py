"""
Расширение истории для обучаемой модели назад до 2025-06-01 (2026-09-25,
решение Alex: "чем больше данных, тем лучше учится модель").

До этого история начиналась с 2026-06-01 (только лето). Для обучения
прогноза погоды цены Polymarket не нужны — только прогнозы и реальная
температура, а они есть раньше: прогнозы моделей за сутки (Open-Meteo
previous-runs, 13 из 16 моделей с июня 2025), замеры станций (Iowa
Mesonet — архив METAR за годы). Грузим:
1. mm_forecasts (lead='day1') — 16 моделей, кусками по 60 дней;
2. ml_fcst_vars — прогнозные условия 11-17 ч (ECMWF), кусками;
3. station_obs — METAR с точкой росы и давлением.
Дневные максимумы (weather_station_daily) потом пересчитает обычный
weather_station_obs.py по всем замерам.

Разовый скрипт, идемпотентный (INSERT OR IGNORE/REPLACE).
Запуск: python weather_history_extend.py
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

import weather_ml_data as mld
import weather_multimodel as mm
from weather_cities import OBS_CITIES

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
FROM = date(2025, 6, 1)
TO = date(2026, 6, 3)   # стык с уже загруженной историей (с 2026-06-01)
# 2026-09-26 (идея 5 от Alex — ещё год назад): --from/--to и --part N/M (часть городов —
# чтобы не упереться в суточный лимит Open-Meteo и не оставить рабочий крон без прогнозов).
# Грузить сначала в КОПИЮ базы (POLY_LAB_DB=/data/research/research.sqlite3).
for _i, _a in enumerate(sys.argv):
    if _a == "--from":
        FROM = date.fromisoformat(sys.argv[_i + 1])
    if _a == "--to":
        TO = date.fromisoformat(sys.argv[_i + 1])
CHUNK_DAYS = 60


def chunks():
    d = FROM
    while d < TO:
        e = min(d + timedelta(days=CHUNK_DAYS - 1), TO)
        yield d, e
        d = e + timedelta(days=1)


def get_json(url, params):
    for attempt in range(6):
        r = requests.get(url, params=params, timeout=120)
        if r.status_code == 429:
            time.sleep(60 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise requests.RequestException("Open-Meteo: лимит запросов")


def load_mm(conn, city, cfg):
    for a, b in chunks():
        by_model = {}
        h = get_json(mm.PREVIOUS_RUNS_API, {
            "latitude": cfg["lat"], "longitude": cfg["lon"], "timezone": cfg["tz"], "temperature_unit": cfg["unit"],
            "hourly": "temperature_2m_previous_day1", "models": ",".join(mm.MODELS),
            "start_date": a.isoformat(), "end_date": b.isoformat()})["hourly"]
        seen = set()
        for model in mm.MODELS:
            series = h.get(f"temperature_2m_previous_day1_{model}")
            if not series or all(v is None for v in series) or tuple(series) in seen:
                continue
            seen.add(tuple(series))
            days = {}
            for t, v in zip(h["time"], series):
                if v is not None:
                    days.setdefault(t[:10], []).append(v)
            by_model[model] = {d: max(vs) for d, vs in days.items() if len(vs) >= 20}
        conn.executemany(
            "INSERT OR IGNORE INTO mm_forecasts (city, local_date, model, lead, fcst_max, fetched_at) VALUES (?, ?, ?, 'day1', ?, ?)",
            [(city, d, m, v, datetime.now(timezone.utc).isoformat()) for m, dd in by_model.items() for d, v in dd.items()])
        conn.commit()
        time.sleep(2)


def load_fv(conn, city, cfg):
    for a, b in chunks():
        h = get_json(mld.PREVIOUS_RUNS_API, {
            "latitude": cfg["lat"], "longitude": cfg["lon"], "timezone": cfg["tz"], "models": mld.FCST_MODEL,
            "hourly": ",".join(f"{v}_previous_day1" for v in mld.FCST_VARS),
            "start_date": a.isoformat(), "end_date": b.isoformat()})["hourly"]
        acc = {}
        for i, t in enumerate(h["time"]):
            if int(t[11:13]) not in mld.PEAK_HOURS:
                continue
            for v in mld.FCST_VARS:
                col = h.get(f"{v}_previous_day1")
                if col and col[i] is not None:
                    acc.setdefault((t[:10], v), []).append(col[i])
        rows = []
        for (d, v), xs in acc.items():
            if v == "wind_direction_10m":
                rows.append((city, d, "wind_dir_sin", sum(math.sin(math.radians(x)) for x in xs) / len(xs)))
                rows.append((city, d, "wind_dir_cos", sum(math.cos(math.radians(x)) for x in xs) / len(xs)))
            elif v == "precipitation":
                rows.append((city, d, v, sum(xs)))
            else:
                rows.append((city, d, v, sum(xs) / len(xs)))
        conn.executemany("INSERT OR IGNORE INTO ml_fcst_vars VALUES (?, ?, ?, ?)", rows)
        conn.commit()
        time.sleep(2)


def load_obs(conn, city, cfg):
    params = {"station": cfg["iem"], "data": ["tmpf", "drct", "sknt", "skyc1", "dwpf", "alti"],
              "year1": FROM.year, "month1": FROM.month, "day1": FROM.day, "year2": TO.year, "month2": TO.month,
              "day2": TO.day, "tz": "Etc/UTC", "format": "onlycomma", "latlon": "no", "missing": "M", "report_type": [3, 4]}
    for attempt in range(5):
        r = requests.get("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py", params=params, timeout=300)
        if r.status_code == 429 or "Too many requests" in r.text[:200]:
            time.sleep(15 * (attempt + 1))
            continue
        break
    num = lambda v: None if v in (None, "", "M") else float(v)
    rows = list(csv.DictReader(io.StringIO(r.text)))
    conn.executemany(
        "INSERT OR IGNORE INTO station_obs (city, station, valid_utc, tmpf, drct, sknt, skyc1, dwpf, alti) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(city, cfg["iem"], x["valid"], num(x["tmpf"]), num(x["drct"]), num(x["sknt"]), x.get("skyc1"),
          num(x.get("dwpf")), num(x.get("alti"))) for x in rows if x.get("valid") and num(x["tmpf"]) is not None])
    conn.commit()
    time.sleep(6)
    return len(rows)


def main():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    mm.ensure_schema(conn)
    mld.ensure_schema(conn)
    cities = list(OBS_CITIES.items())
    if "--part" in sys.argv:
        k, m = map(int, sys.argv[sys.argv.index("--part") + 1].split("/"))
        cities = cities[k - 1::m]
    print(f"период {FROM}..{TO}, городов {len(cities)}", flush=True)
    for city, cfg in cities:
        try:
            n = load_obs(conn, city, cfg)
            load_mm(conn, city, cfg)
            load_fv(conn, city, cfg)
            print(f"{city}: замеров {n}, прогнозы и условия загружены", flush=True)
        except (requests.RequestException, ValueError, KeyError) as e:
            print(f"{city}: ошибка — {e}", file=sys.stderr, flush=True)
    conn.close()


if __name__ == "__main__":
    main()
