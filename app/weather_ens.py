"""
Ансамблевые прогнозы максимума — каждое утро перед решением (2026-09-26, идея 5
из списка Alex). Честно проверить ансамбли на истории нельзя: архив Open-Meteo
отдаёт их только с 25.06.2026, и за прошлые даты это склейка самых свежих прогонов
(почти факт), а не прогноз, сделанный заранее. Поэтому собираем сами — ровно то,
что было известно до решения, — и через 1-2 месяца проверяем как признак v3.

Для каждого города один раз в день, когда местное время 05:00-08:00: прогноз
максимума на сегодня от ECMWF IFS (51 вариант), GEFS (31), ICON EPS (40); с 28.09 ещё
ECMWF AIFS (51, нейросеть), UKMO (18), GEM (21) — всего 212 вариантов.
Пишет ens_forecasts: по модели — число вариантов, среднее, разброс, 10/50/90%.
Нагрузка на Open-Meteo: ~21 «вызов» на город (212 вариантов), ~1000 в сутки.
Крон: каждые 2 часа. Запуск: python weather_ens.py

С 28.09 пишем и сами варианты (members_json, °C) — из них кошелёк `ens` (weather_edge → ens_model_p)
считает шанс варианта температуры: доля вариантов в его границах, каждый ансамбль с равным весом,
сдвиг на поправку города (как у основной модели), затем смесь 35/65 с рынком (как ml3_cal).
"""

from jobmark import item_guard
import json
import os
import sqlite3
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from weather_cities import OBS_CITIES
from weather_edge import CITIES

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
ENS_API = "https://ensemble-api.open-meteo.com/v1/ensemble"
MODELS = {"ecmwf_ifs025": "ecmwf_ifs025_ensemble", "gfs025": "ncep_gefs025", "icon_seamless": "icon_seamless_eps",
          # 2026-09-28: + нейросеть ECMWF (AIFS), британский и канадский ансамбли — больше независимых
          # мнений о разбросе. Австралийский (bom_access_global_ensemble) Open-Meteo отдаёт пустым — не берём.
          "ecmwf_aifs025": "ecmwf_aifs025_ensemble", "ukmo_global": "ukmo_global_ensemble_20km",
          "gem_global": "gem_global_ensemble"}
WINDOW = range(5, 8)  # местные часы, когда берём прогноз (до решения в 08:00)
ENS_MIN_MODELS = 4    # кошелёк ens: меньше ансамблей в пуле — оценки нет, ставки нет


def q(vals, p):
    s = sorted(vals)
    return s[min(len(s) - 1, max(0, round(p * (len(s) - 1))))]


def load_members(conn, city, local_date, unit, models=None):
    """{ансамбль: [варианты максимума в единицах города]} за день — только записи с вариантами."""
    if "members_json" not in {r[1] for r in conn.execute("PRAGMA table_info(ens_forecasts)")}:
        return {}
    out = {}
    for model, js in conn.execute("SELECT model, members_json FROM ens_forecasts WHERE city = ? AND local_date = ? "
                                  "AND members_json IS NOT NULL", (city, local_date)):
        if models is None or model in models:
            vals = json.loads(js)
            out[model] = [v * 9 / 5 + 32 for v in vals] if unit == "fahrenheit" else vals
    return out


def main():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("""CREATE TABLE IF NOT EXISTS ens_forecasts (city TEXT, local_date TEXT, model TEXT, fetched_utc TEXT,
                    n INTEGER, mean_c REAL, std_c REAL, p10_c REAL, p50_c REAL, p90_c REAL,
                    PRIMARY KEY (city, local_date, model))""")
    if "members_json" not in {r[1] for r in conn.execute("PRAGMA table_info(ens_forecasts)")}:
        conn.execute("ALTER TABLE ens_forecasts ADD COLUMN members_json TEXT")  # 2026-09-28: варианты, °C
    conn.commit()
    # собран = есть сами варианты (до 28.09 писали только сводку — такие дни в окне догружаем)
    done = {(r[0], r[1]) for r in conn.execute("SELECT DISTINCT city, local_date FROM ens_forecasts WHERE members_json IS NOT NULL")}
    n_ok = 0
    for city, cfg in OBS_CITIES.items():
        with item_guard(city, conn):
            tz = ZoneInfo(cfg["tz"])
            now = datetime.now(tz)
            d = now.date().isoformat()
            if now.hour not in WINDOW or (city, d) in done or city not in CITIES:
                continue
            c = CITIES[city]
            try:
                r = requests.get(ENS_API, params={"latitude": c["lat"], "longitude": c["lon"], "daily": "temperature_2m_max",
                                                  "models": ",".join(MODELS.values()), "timezone": cfg["tz"], "forecast_days": 1}, timeout=60)
                r.raise_for_status()
                daily = r.json()["daily"]
            except (requests.RequestException, KeyError, ValueError) as e:
                print(f"{city}: ошибка — {e}")
                continue
            fetched = datetime.now(timezone.utc).isoformat()
            rows = []
            for key, suffix in MODELS.items():
                vals = [v[0] for k, v in daily.items() if k.startswith("temperature_2m_max") and k.endswith("_" + suffix) and v and v[0] is not None]
                if len(vals) < 5:
                    continue
                rows.append((city, d, key, fetched, len(vals), statistics.fmean(vals), statistics.pstdev(vals),
                             q(vals, 0.1), q(vals, 0.5), q(vals, 0.9), json.dumps(sorted(round(v, 2) for v in vals))))
            conn.executemany("""INSERT OR REPLACE INTO ens_forecasts (city, local_date, model, fetched_utc, n, mean_c, std_c,
                                p10_c, p50_c, p90_c, members_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows)
            conn.commit()
            n_ok += bool(rows)
            time.sleep(2)
    print(f"ансамбли: записано городов {n_ok}")
    from jobmark import mark
    mark(conn, "weather_ens")
    conn.close()


if __name__ == "__main__":
    main()
