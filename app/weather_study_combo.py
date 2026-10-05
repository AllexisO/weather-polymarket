"""
Одна модель из нескольких версий (2026-09-30, Alex: «модель, которая берёт всё из v1-v5 и объединяет в себе — есть смысл?»).
Ансамбль: шанс варианта = среднее шансов версий (v3, v4, v5 — v1/v2 слабее и без рынка), потом смесь 35/65 с рынком, как у
кошельков. Схема как weather_study_q4.py: учим до месяца (август, сентябрь), 3 обучения (зёрна 11/22/33), среднее.
Порог (записан до прогона): смесь ансамбля лучше смеси ЛУЧШЕЙ одиночной версии на ≥ 0.015 по логошибке в обоих месяцах.
Только на копии.
"""
import math

import lightgbm as lgb
import numpy as np

import weather_ml as ml
import weather_ml_check as chk
import weather_ml_q as mq
import weather_study_tune as tune
from weather_ml_live import blend_with_market
from weather_study_year import FOLDS, SEEDS

conn = tune.conn


def qpred(tr, te, feats, base_tr, base_te, extra):
    out = []
    for s in SEEDS:
        p = {**mq.Q_PARAMS, **extra, "seed": s, "bagging_seed": s, "feature_fraction_seed": s, "num_threads": 4}
        raw = np.column_stack([lgb.train({**p, "alpha": q}, lgb.Dataset(tr[feats], tr["actual_c"] - base_tr, categorical_feature=["city_id"]),
                                         mq.Q_ROUNDS).predict(te[feats]) for q in mq.QUANTILES])
        raw.sort(axis=1)
        out.append(raw + np.asarray(base_te)[:, None])
    return out  # по зёрнам


if __name__ == "__main__":
    ml.USE_MKT = True
    df = ml.build(conn)
    df = df[(df["actual_c"].notna()) & (df["date"] >= "2025-06-01")].copy()
    feats = ml.features(df)
    COMBOS = {"v3": ["v3"], "v4": ["v4"], "v5": ["v5"], "v3+v5": ["v3", "v5"], "v4+v5": ["v4", "v5"], "v3+v4+v5": ["v3", "v4", "v5"]}
    res = {c: {} for c in COMBOS}
    for label, start, end in FOLDS:
        tr = df[df["date"] < start]
        te = df[(df["date"] >= start) & (df["date"] < end)]
        b5tr = tr["fc_mean"] + tr["mkt_mean_vs_fc"].fillna(0.0)
        b5te = te["fc_mean"] + te["mkt_mean_vs_fc"].fillna(0.0)
        P = {"v3": qpred(tr, te, feats, tr["fc_mean"], te["fc_mean"], {}),
             "v4": qpred(tr, te, feats, tr["fc_mean"], te["fc_mean"], {"num_leaves": 31}),
             "v5": qpred(tr, te, feats, b5tr, b5te, {})}
        acc = {c: [[0.0, 0.0] for _ in SEEDS] for c in COMBOS}  # [логошибка модели, смеси] по зёрнам
        n, lk = 0, 0.0
        for j, (_, r) in enumerate(te.iterrows()):
            w = tune.base.WIN.get((r["city"], r["date"]))
            pr = chk.prices(conn, r["city"], r["date"], "A") if w is not None else {}
            keys = sorted(pr)
            if len(pr) < 3 or not any(b[0] == w for b in keys):
                continue
            wi = [b[0] for b in keys].index(w)
            n += 1
            lk -= math.log(max(pr[keys[wi]] / (sum(pr.values()) or 1), 1e-4))
            for si in range(len(SEEDS)):
                probs = {}
                for v in P:
                    x = [mq.bucket_prob(list(P[v][si][j]), r["unit"], b[0], b[1]) for b in keys]
                    t = sum(x) or 1.0
                    probs[v] = [a / t for a in x]
                for c, vs in COMBOS.items():
                    m = [float(np.mean([probs[v][i] for v in vs])) for i in range(len(keys))]
                    b = blend_with_market(m, [pr[k] for k in keys])
                    acc[c][si][0] -= math.log(max(m[wi], 1e-4))
                    acc[c][si][1] -= math.log(max(b[wi], 1e-4))
        for c in COMBOS:
            res[c][label] = (np.mean([a[0] for a in acc[c]]) / n, np.mean([a[1] for a in acc[c]]) / n)
        print(f"{label}: {n} город-дней, рынок {lk / n:.4f}", flush=True)
        for c in COMBOS:
            print(f"  {c:9s} модель {res[c][label][0]:.4f}  смесь {res[c][label][1]:.4f}", flush=True)
    best = {m: min(res[c][m][1] for c in ("v3", "v4", "v5")) for m in res["v3"]}
    print("\nансамбль против лучшей одиночной версии (смесь, минус = лучше, порог −0.015 в обоих месяцах):")
    for c in ("v3+v5", "v4+v5", "v3+v4+v5"):
        d = {m: res[c][m][1] - best[m] for m in best}
        print(f"  {c:9s} " + ", ".join(f"{m} {v:+.4f}" for m, v in d.items()) + " → " + ("ПРОШЛО" if all(v <= -0.015 for v in d.values()) else "не прошло"))
