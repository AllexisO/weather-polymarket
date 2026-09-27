"""
Идея 6 (2026-09-27): влажность почвы в 07:00 и вчерашние осадки (таблица ml_soil,
weather_soil.py) как признаки v3/v4. Проверка как в weather_study_year.py: август и
сентябрь, по 3 обучения на вариант, сравниваем средние; деньги смеси — сентябрь, по
настоящим сделкам. Учим на данных с 2025-06-01 (как сейчас в живой модели).
Запуск — на копии базы: POLY_LAB_DB=/data/research/research.sqlite3
"""

import numpy as np

import weather_ml as ml
import weather_study_tune as tune
from weather_study_year import FOLDS, SEEDS

if __name__ == "__main__":
    ml.USE_MKT = True
    df = ml.build(tune.conn)
    df = df[(df["actual_c"].notna()) & (df["date"] >= "2025-06-01")]
    soil = {(c, d): (s, r) for c, d, s, r in tune.conn.execute("SELECT city, local_date, soil_m, rain_prev FROM ml_soil").fetchall()}
    df = df.copy()
    df["soil_m"] = [soil.get((c, d), (np.nan, np.nan))[0] for c, d in zip(df["city"], df["date"])]
    df["rain_prev"] = [soil.get((c, d), (np.nan, np.nan))[1] for c, d in zip(df["city"], df["date"])]
    print(f"данных {len(df)}; почва есть в {df['soil_m'].notna().mean():.0%}, осадки — {df['rain_prev'].notna().mean():.0%}", flush=True)
    base_feats = [f for f in ml.features(df) if f not in ("soil_m", "rain_prev")]
    for vname, extra in (("v3", {}), ("v4", {"num_leaves": 31})):
        for name, feats in (("без почвы", base_feats), ("с почвой и осадками", base_feats + ["soil_m", "rain_prev"])):
            ml.FEATURES = feats
            line = f"{vname}, {name:22s}"
            for label, start, end in FOLDS:
                tr = df[df["date"] < start]
                te = df[(df["date"] >= start) & (df["date"] < end)]
                sc = [tune.score(tune.train(tr, {**extra, "seed": s, "bagging_seed": s, "feature_fraction_seed": s}, 300, None), te)
                      for s in SEEDS]
                line += (f" | {label}: модель {np.mean([s['ll'] for s in sc]):.4f} смесь {np.mean([s['blend'] for s in sc]):.4f}"
                         f" ошибка {np.mean([s['err'] for s in sc]):.3f}°")
                if label == "сентябрь":
                    line += f" | деньги смеси сент. {np.mean([tune.base.run_rule(s['days'], 'blend', 0.03)['pnl_real'] for s in sc]):+.1f}$"
            print(line, flush=True)
