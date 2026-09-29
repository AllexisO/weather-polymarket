"""
Новые факторы для модели (2026-09-29, вопрос Alex «что мы ещё можем дать модели?»). Всё — из прогноза, выпущенного
за сутки до дня (Open-Meteo previous-runs, *_previous_day1: к 08:00 он точно был — подсмотра будущего нет):
  morn_err  — утренний замер (obs_t, последний METAR до 07:50) минус прогноз температуры на 07:00: день идёт
              теплее/холоднее прогноза уже с утра;
  amp       — прогнозный прирост 07:00 → 14:00 (насколько день «разгонится»); obs_plus_amp — замер + прирост − fc_mean;
  blh_max   — высота пограничного слоя днём (перемешивание — выше максимум), cape_max — неустойчивость (грозы),
  sun_h     — часы солнца за день.
Проверка как weather_study_soil.py: август и сентябрь, 3 обучения, логошибка модели и смеси, деньги смеси.
Порог (записан до прогона): логошибка лучше v3 на ≥ 0.015 в обоих месяцах, деньги смеси в сентябре не хуже.
Кэш загрузки: /data/research/factors.json (~150 вызовов Open-Meteo). Только на копии.
"""
import json
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import requests

import weather_ml as ml
import weather_study_tune as tune
from weather_cities import OBS_CITIES
from weather_edge import CITIES
from weather_study_year import FOLDS, SEEDS

API = "https://previous-runs-api.open-meteo.com/v1/forecast"
VARS = ["temperature_2m", "boundary_layer_height", "cape", "sunshine_duration"]
CACHE = Path("/data/research/factors.json")
START, END = date(2025, 6, 1), date(2026, 9, 28)


def fetch_all():
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    for city, cfg in OBS_CITIES.items():
        if city in cache:
            continue
        lat, lon = CITIES[city]["lat"], CITIES[city]["lon"]
        days = {}
        a = START
        try:
            while a <= END:
                b = min(a + timedelta(days=179), END)
                for attempt in range(4):
                    r = requests.get(API, params={"latitude": lat, "longitude": lon, "timezone": cfg["tz"],
                                                  "hourly": ",".join(v + "_previous_day1" for v in VARS),
                                                  "start_date": a.isoformat(), "end_date": b.isoformat()}, timeout=120)
                    if r.status_code == 429:
                        time.sleep(60)
                        continue
                    r.raise_for_status()
                    break
                h = r.json()["hourly"]
                for i, t in enumerate(h["time"]):
                    d, hr = t[:10], int(t[11:13])
                    x = days.setdefault(d, {})
                    tt = h["temperature_2m_previous_day1"][i]
                    if hr == 7:
                        x["t07"] = tt
                    if hr == 14:
                        x["t14"] = tt
                    if 10 <= hr <= 16:
                        for v, k in (("boundary_layer_height", "blh_max"), ("cape", "cape_max")):
                            val = h[v + "_previous_day1"][i]
                            if val is not None:
                                x[k] = max(x.get(k, val), val)
                    s = h["sunshine_duration_previous_day1"][i]
                    if s is not None:
                        x["sun_h"] = x.get("sun_h", 0.0) + s / 3600
                a = b + timedelta(days=1)
                time.sleep(1)
        except Exception as e:  # noqa: BLE001 — один город не роняет загрузку
            print(f"{city}: ошибка — {e}", flush=True)
            continue
        cache[city] = days
        CACHE.write_text(json.dumps(cache))
        print(f"{city}: {len(days)} дней", flush=True)
    return cache


if __name__ == "__main__":
    fx = fetch_all()
    ml.USE_MKT = True
    df = ml.build(tune.conn)
    df = df[(df["actual_c"].notna()) & (df["date"] >= "2025-06-01")].copy()
    g = lambda k: [fx.get(c, {}).get(d, {}).get(k, np.nan) for c, d in zip(df["city"], df["date"])]
    t07, t14 = np.array(g("t07"), dtype=float), np.array(g("t14"), dtype=float)
    df["morn_err"] = df["obs_t"] - t07
    df["amp"] = t14 - t07
    df["obs_plus_amp"] = df["obs_t"] + df["amp"] - df["fc_mean"]
    for k in ("blh_max", "cape_max", "sun_h"):
        df[k] = np.array(g(k), dtype=float)
    new = ["morn_err", "amp", "obs_plus_amp", "blh_max", "cape_max", "sun_h"]
    print(f"данных {len(df)}; есть: " + ", ".join(f"{k} {df[k].notna().mean():.0%}" for k in new), flush=True)
    base_feats = [f for f in ml.features(df) if f not in new]
    for name, feats in (("v3 как сейчас", base_feats),
                        ("+ утро против прогноза", base_feats + ["morn_err", "amp", "obs_plus_amp"]),
                        ("+ перемешивание, грозы, солнце", base_feats + ["blh_max", "cape_max", "sun_h"]),
                        ("+ всё", base_feats + new)):
        ml.FEATURES = feats
        line = f"{name:32s}"
        for label, start, end in FOLDS:
            tr = df[df["date"] < start]
            te = df[(df["date"] >= start) & (df["date"] < end)]
            sc = [tune.score(tune.train(tr, {"seed": s, "bagging_seed": s, "feature_fraction_seed": s}, 300, None), te)
                  for s in SEEDS]
            line += (f" | {label}: модель {np.mean([s['ll'] for s in sc]):.4f} смесь {np.mean([s['blend'] for s in sc]):.4f}"
                     f" ошибка {np.mean([s['err'] for s in sc]):.3f}°")
            if label == "сентябрь":
                line += f" | деньги смеси сент. {np.mean([tune.base.run_rule(s['days'], 'blend', 0.03)['pnl_real'] for s in sc]):+.1f}$"
        print(line, flush=True)
