"""
Идея 5 (2026-09-27): свежая точность каждой погодной модели в городе.
Уже есть mix_vs_fc — веса моделей по точности за 45 дней. Проверяем короткие окна:
- mixN_vs_fc — смесь моделей (поправка сдвига + вес 1/ошибка²) по последним N дням (7, 14), минус среднее;
- biasN — средний сдвиг среднего прогноза за последние N дней (есть только вчерашний prev_err);
- best14_vs_fc — прогноз модели, самой точной за 14 дней, минус среднее.
Всё — по дням строго до дня прогноза (вчерашний факт к 08:00 уже известен).
Проверка как в weather_study_year.py (август, сентябрь, 3 обучения). Данные с 2025-06-01.
Запуск — на копии базы: POLY_LAB_DB=/data/research/research.sqlite3
"""

from datetime import date, timedelta

import numpy as np

import weather_ml as ml
import weather_study_tune as tune
from weather_cities import OBS_CITIES
from weather_study_year import FOLDS, SEEDS

NEW = ["mix7_vs_fc", "mix14_vs_fc", "bias7", "bias14", "best14_vs_fc"]


def add_recent(df):
    fc, act = {}, {}
    for city, cfg in OBS_CITIES.items():
        unit = cfg["unit"]
        for d, m, v in tune.conn.execute(
                "SELECT local_date, model, fcst_max FROM mm_forecasts WHERE city = ? AND lead = 'day1'", (city,)).fetchall():
            fc.setdefault((city, d), {})[m] = ml.to_c(v, unit)
        for d, v in tune.conn.execute("SELECT local_date, actual_max FROM weather_station_daily WHERE city = ?", (city,)).fetchall():
            act[(city, d)] = ml.to_c(v, unit)
    out = {k: [] for k in NEW}
    for city, d, mean_fc in zip(df["city"], df["date"], df["fc_mean"]):
        today = fc.get((city, d), {})
        past = []
        for i in range(1, 15):
            p = (date.fromisoformat(d) - timedelta(days=i)).isoformat()
            if (city, p) in fc and (city, p) in act:
                past.append((i, fc[(city, p)], act[(city, p)]))
        res = dict.fromkeys(NEW, np.nan)
        for n in (7, 14):
            win = [x for x in past if x[0] <= n]
            if len(win) >= n // 2:
                res[f"bias{n}"] = float(np.mean([np.mean(list(f.values())) - a for _, f, a in win]))
                num = den = 0.0
                for m, v in today.items():
                    e = [f[m] - a for _, f, a in win if m in f]
                    if len(e) >= n // 2:
                        b = float(np.mean(e))
                        w = 1.0 / max(float(np.mean([(x - b) ** 2 for x in e])), 0.05)
                        num += w * (v - b)
                        den += w
                if den:
                    res[f"mix{n}_vs_fc"] = num / den - mean_fc
                if n == 14:
                    mae = {m: np.mean([abs(f[m] - a) for _, f, a in win if m in f]) for m in today
                           if sum(1 for _, f, _a in win if m in f) >= 7}
                    if mae:
                        res["best14_vs_fc"] = today[min(mae, key=mae.get)] - mean_fc
        for k in NEW:
            out[k].append(res[k])
    for k in NEW:
        df[k] = out[k]
    return df


if __name__ == "__main__":
    ml.USE_MKT = True
    df = ml.build(tune.conn)
    df = df[(df["actual_c"].notna()) & (df["date"] >= "2025-06-01")].copy()
    df = add_recent(df)
    print(f"данных {len(df)}; заполнено: " + ", ".join(f"{k} {df[k].notna().mean():.0%}" for k in NEW), flush=True)
    base_feats = [f for f in ml.features(df) if f not in NEW]
    for vname, extra in (("v3", {}), ("v4", {"num_leaves": 31})):
        for name, feats in (("как сейчас", base_feats), ("со свежей точностью", base_feats + NEW)):
            ml.FEATURES = feats
            line = f"{vname}, {name:20s}"
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
