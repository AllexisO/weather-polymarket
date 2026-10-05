"""
v6 = v5 + все маленькие улучшения вместе (2026-10-01, Alex: «брать по кусочку с каждой стороны»). По отдельности каждое не
прошло порог (−0.001…−0.006 по смеси), но это разные данные — могут сложиться:
  • LAMP (США, прогон ≤ 07:00 местного; кэш lamp.json, weather_study_lamp.py): lamp_max, lamp_vs_fc;
  • спутник утром (кэш sat.json, weather_study_sat.py): sat_sw, fc_sw, sat_minus_fc;
  • погода на пик дня (кэш factors_peak.json, weather_study_q4.py): cc_peak, pr_peak.
v5 — отправная точка «ожидаемый максимум по ценам рынка», 13 уровней, 3 обучения; v6 — то же + признаки выше.
Схема: учим до месяца, проверяем месяц (август, сентябрь), 3 зерна, смесь 35/65 с рынком.
Порог (записан до прогона): смесь v6 лучше смеси v5 на ≥ 0.015 по логошибке в обоих месяцах. Только на копии.
"""
import json
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


def load_feats(df):
    lamp = json.load(open("/data/research/lamp.json"))
    sat = json.load(open("/data/research/sat.json"))
    peak = json.load(open("/data/research/factors_peak.json"))
    g = lambda src, i=None: np.array([np.nan if (v := (src.get(c) or {}).get(d)) is None or (i is not None and v[i] is None)
                                      else (v if i is None else v[i]) for c, d in zip(df["city"], df["date"])], float)
    lm = g(lamp)
    df["lamp_max"] = (lm - 32) * 5 / 9
    df["lamp_vs_fc"] = df["lamp_max"] - df["fc_mean"]
    df["sat_sw"], df["fc_sw"] = g(sat, 0), g(sat, 1)
    df["sat_minus_fc"] = df["sat_sw"] - df["fc_sw"]
    df["cc_peak"], df["pr_peak"] = g(peak, 0), g(peak, 1)
    return ["lamp_max", "lamp_vs_fc", "sat_sw", "fc_sw", "sat_minus_fc", "cc_peak", "pr_peak"]


def qpred(tr, te, feats):
    btr = tr["fc_mean"] + tr["mkt_mean_vs_fc"].fillna(0.0)
    bte = te["fc_mean"] + te["mkt_mean_vs_fc"].fillna(0.0)
    out = []
    for s in SEEDS:
        p = {**mq.Q_PARAMS, "seed": s, "bagging_seed": s, "feature_fraction_seed": s, "num_threads": 4}
        raw = np.column_stack([lgb.train({**p, "alpha": q}, lgb.Dataset(tr[feats], tr["actual_c"] - btr, categorical_feature=["city_id"]),
                                         mq.Q_ROUNDS).predict(te[feats]) for q in mq.QUANTILES])
        raw.sort(axis=1)
        out.append(raw + np.asarray(bte)[:, None])
    return out


def score(te, preds):
    lm = [0.0] * len(preds); lb = [0.0] * len(preds); n = 0; lk = 0.0
    for j, (_, r) in enumerate(te.iterrows()):
        w = tune.base.WIN.get((r["city"], r["date"]))
        pr = chk.prices(conn, r["city"], r["date"], "A") if w is not None else {}
        keys = sorted(pr)
        if len(pr) < 3 or not any(b[0] == w for b in keys):
            continue
        wi = [b[0] for b in keys].index(w)
        n += 1
        lk -= math.log(max(pr[keys[wi]] / (sum(pr.values()) or 1), 1e-4))
        for si, P in enumerate(preds):
            x = [mq.bucket_prob(list(P[j]), r["unit"], b[0], b[1]) for b in keys]
            t = sum(x) or 1.0
            x = [a / t for a in x]
            m = blend_with_market(x, [pr[k] for k in keys])
            lm[si] -= math.log(max(x[wi], 1e-4)); lb[si] -= math.log(max(m[wi], 1e-4))
    return np.mean(lm) / n, np.mean(lb) / n, lk / n, n


if __name__ == "__main__":
    ml.USE_MKT = True
    df = ml.build(conn)
    df = df[(df["actual_c"].notna()) & (df["date"] >= "2025-06-01")].copy()
    extra = load_feats(df)
    base = [f for f in ml.features(df) if f not in extra]
    print(f"строк {len(df)}; LAMP есть у {np.isfinite(df['lamp_max']).mean():.0%}, спутник у {np.isfinite(df['sat_sw']).mean():.0%}, "
          f"пик дня у {np.isfinite(df['cc_peak']).mean():.0%}", flush=True)
    res = {}
    for label, start, end in FOLDS:
        tr = df[df["date"] < start]
        te = df[(df["date"] >= start) & (df["date"] < end)]
        for name, feats in (("v5", base), ("v6 = v5 + всё новое", base + extra)):
            m, b, k, n = score(te, qpred(tr, te, feats))
            res[(name, label)] = b
            print(f"  {label}: {name:22s} модель {m:.4f}  смесь {b:.4f}  рынок {k:.4f}  ({n} город-дней)", flush=True)
    d = {lab: res[("v6 = v5 + всё новое", lab)] - res[("v5", lab)] for _, lab in [(0, f[0]) for f in FOLDS]}
    print("\nv6 против v5 (смесь, минус = лучше, порог −0.015 в обоих месяцах): "
          + ", ".join(f"{k} {v:+.4f}" for k, v in d.items()) + " → " + ("ПРОШЛО" if all(v <= -0.015 for v in d.values()) else "не прошло"))
