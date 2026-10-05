"""
NWS LAMP для 11 городов США (2026-09-30, Alex: «проверь все 5»): LAMP — ежечасно обновляемый прогноз по станции с учётом
последних замеров (не NBM, который отвергнут). Признаки для v3: lamp_max — максимум часовых температур LAMP на местный
день из прогона, выпущенного не позже чем за 1 ч до решения в 08:00 (архив IEM, модель LAV), в единицах рынка → °C;
lamp_vs_fc = lamp_max − среднее 16 моделей. Без заглядывания вперёд: прогон ≤ 07:00 местного.
Схема — как weather_study_q4.py: учим до месяца (август, сентябрь), 3 обучения, сравниваем с v3 на строках США
(у остальных городов признака нет). Порог (записан до прогона): логошибка смеси на городах США лучше v3 на ≥ 0.015 в обоих
месяцах. Кэш /data/research/lamp.json. Только на копии.
"""
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import requests

import weather_ml as ml
import weather_study_tune as tune
import weather_study_q4 as q4
from weather_cities import OBS_CITIES
from weather_study_year import FOLDS, SEEDS

CACHE = Path("/data/research/lamp.json")
US = {c: v for c, v in OBS_CITIES.items() if v["icao"].startswith("K")}
START, END = date(2025, 6, 1), date(2026, 9, 28)


def fetch():
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    for city, cfg in US.items():
        tz = ZoneInfo(cfg["tz"])
        c = cache.setdefault(city, {})
        d = START
        while d <= END:
            k = d.isoformat()
            if k not in c:
                run = (datetime.combine(d, datetime.min.time(), tz) + timedelta(hours=7)).astimezone(ZoneInfo("UTC")).replace(minute=0)
                try:
                    r = requests.get("https://mesonet.agron.iastate.edu/api/1/mos.json",
                                     params={"station": cfg["icao"], "model": "LAV", "runtime": run.strftime("%Y-%m-%dT%H:%MZ")}, timeout=60)
                    rows = r.json().get("data", []) if r.status_code == 200 else []
                except (requests.RequestException, ValueError):
                    rows = None
                if rows is None:
                    time.sleep(5)
                    continue
                vals = []
                for x in rows:
                    ft = datetime.fromisoformat(x["ftime_utc"][:19]).replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
                    if ft.date() == d and x.get("tmp") is not None:
                        vals.append(x["tmp"])
                c[k] = max(vals) if vals else None
                time.sleep(0.15)
            d += timedelta(days=1)
        CACHE.write_text(json.dumps(cache))
        print(f"{city}: дней с LAMP {sum(v is not None for v in c.values())}", flush=True)
    return cache


if __name__ == "__main__":
    lamp = fetch()
    ml.USE_MKT = True
    df = ml.build(tune.conn)
    df = df[(df["actual_c"].notna()) & (df["date"] >= "2025-06-01")].copy()
    lm = np.array([np.nan if (v := (lamp.get(c) or {}).get(d)) is None else (v - 32) * 5 / 9 for c, d in zip(df["city"], df["date"])], float)
    df["lamp_max"] = lm
    df["lamp_vs_fc"] = lm - df["fc_mean"].values
    base_f = [f for f in ml.features(df) if f not in ("lamp_max", "lamp_vs_fc")]
    us = df["city"].isin(US).values
    print(f"строк {len(df)}, США {us.sum()}, LAMP есть у {np.isfinite(lm[us]).mean():.0%} строк США", flush=True)
    res = {}
    for name, feats in (("v3", base_f), ("v3 + LAMP", base_f + ["lamp_max", "lamp_vs_fc"])):
        ml.FEATURES = feats
        line, res[name] = f"{name:12s}", {}
        for label, start, end in FOLDS:
            tr = df[df["date"] < start]
            te = df[(df["date"] >= start) & (df["date"] < end) & df["city"].isin(US)]
            sc = [tune.score(q4.train(tr, s), te) for s in SEEDS]
            res[name][label] = np.mean([x["blend"] for x in sc])
            line += f" | {label} США: модель {np.mean([x['ll'] for x in sc]):.4f} смесь {res[name][label]:.4f} рынок {sc[0]['mkt']:.4f}"
        print(line, flush=True)
    d = {m: res["v3 + LAMP"][m] - res["v3"][m] for m in res["v3"]}
    print("LAMP против v3 (минус = лучше, порог −0.015 в обоих месяцах):", ", ".join(f"{m} {v:+.4f}" for m, v in d.items()),
          "→", "ПРОШЛО" if all(v <= -0.015 for v in d.values()) else "не прошло")
