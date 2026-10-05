"""
Спутник «сейчас» (2026-09-30, Alex: «проверь все 5»): «облачный сюрприз» утра — сколько солнца реально дошло до земли
по спутнику (Open-Meteo satellite radiation: Meteosat / Himawari / GOES) в часы до решения (05-07 местного, значения
часа 07 готовы к ~07:45) против вчерашнего прогноза тех же часов (previous_day1). Признаки: sat_sw, fc_sw, sat_minus_fc.
Схема — как weather_study_q4.py: учим до месяца (август, сентябрь), 3 обучения, все 48 городов.
Порог (записан до прогона): логошибка смеси лучше v3 на ≥ 0.015 в обоих месяцах. Кэш /data/research/sat.json.
Open-Meteo: ~150 вызовов. Только на копии.
"""
import json
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import requests

import weather_ml as ml
import weather_study_tune as tune
import weather_study_q4 as q4
from weather_cities import OBS_CITIES
from weather_edge import CITIES
from weather_study_year import FOLDS, SEEDS

CACHE = Path("/data/research/sat.json")
START, END = date(2025, 6, 1), date(2026, 9, 28)
HOURS = (5, 6, 7)


def get(url, params):
    for _ in range(5):
        try:
            r = requests.get(url, params=params, timeout=120)
            if r.status_code == 429:
                time.sleep(60)
                continue
            r.raise_for_status()
            return r.json()["hourly"]
        except (requests.RequestException, ValueError, KeyError):
            time.sleep(10)
    return None


def fetch():
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    for city, cfg in OBS_CITIES.items():
        if city in cache:
            continue
        lat, lon = CITIES[city]["lat"], CITIES[city]["lon"]
        days = {}
        a = START
        while a <= END:
            b = min(a + timedelta(days=179), END)
            p = {"latitude": lat, "longitude": lon, "timezone": cfg["tz"], "start_date": a.isoformat(), "end_date": b.isoformat()}
            s = get("https://satellite-api.open-meteo.com/v1/archive", {**p, "hourly": "shortwave_radiation"})
            f = get("https://previous-runs-api.open-meteo.com/v1/forecast", {**p, "hourly": "shortwave_radiation_previous_day1"})
            for src, h, key in (("s", s, "shortwave_radiation"), ("f", f, "shortwave_radiation_previous_day1")):
                if not h:
                    continue
                for t, v in zip(h["time"], h[key]):
                    if int(t[11:13]) in HOURS and v is not None:
                        x = days.setdefault(t[:10], {"s": 0.0, "f": 0.0, "ns": 0, "nf": 0})
                        x[src] += v; x["n" + src] += 1
            a = b + timedelta(days=1)
            time.sleep(1)
        cache[city] = {d: [x["s"] if x["ns"] == len(HOURS) else None, x["f"] if x["nf"] == len(HOURS) else None] for d, x in days.items()}
        CACHE.write_text(json.dumps(cache))
        print(f"{city}: дней со спутником {sum(v[0] is not None for v in cache[city].values())}", flush=True)
    return cache


if __name__ == "__main__":
    sat = fetch()
    ml.USE_MKT = True
    df = ml.build(tune.conn)
    df = df[(df["actual_c"].notna()) & (df["date"] >= "2025-06-01")].copy()
    g = lambda i: np.array([np.nan if (v := (sat.get(c, {}).get(d) or [None, None])[i]) is None else v for c, d in zip(df["city"], df["date"])], float)
    df["sat_sw"], df["fc_sw"] = g(0), g(1)
    df["sat_minus_fc"] = df["sat_sw"] - df["fc_sw"]
    base_f = [f for f in ml.features(df) if f not in ("sat_sw", "fc_sw", "sat_minus_fc")]
    print(f"строк {len(df)}, спутник есть у {df['sat_sw'].notna().mean():.0%}", flush=True)
    b = q4.run("v3", df, base_f)
    r = q4.run("v3 + спутник утром", df, base_f + ["sat_sw", "fc_sw", "sat_minus_fc"])
    d = {m: r[m] - b[m] for m in b}
    print("против v3 (модель, минус = лучше):", ", ".join(f"{m} {v:+.4f}" for m, v in d.items()))
