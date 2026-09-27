"""
Идеи 3 и 4 (2026-09-26, Alex): настройка параметров v3 и больший вес свежим дням.
Проверка по месяцам: учим на всём до 1 августа — проверяем август; учим на всём до
1 сентября — проверяем сентябрь. Вариант выбираем по августу, сентябрь — подтверждение.
Метрики: оценка шансов (логошибка) против рынка, ошибка медианы °C, и то же для смеси
35/65 (как в кошельке ml3_cal) — и деньги смеси на сентябре по настоящим сделкам.
Запуск — на копии базы: POLY_LAB_DB=/data/research/research.sqlite3
"""

import math
import sys
from datetime import date

import lightgbm as lgb
import numpy as np

import weather_ml as ml
import weather_ml_check as chk
import weather_ml_q as mq
import weather_study_0926 as base
from weather_ml_live import blend_with_market

conn = base.conn
import sqlite3
conn.row_factory = sqlite3.Row  # ml.build ждёт Row; распаковка кортежей тоже работает
BASE = dict(mq.Q_PARAMS)
VARIANTS = {
    "как сейчас": ({}, 300, None),
    "мельче (7 листьев)": ({"num_leaves": 7}, 300, None),
    "крупнее (31 лист)": ({"num_leaves": 31}, 300, None),
    "медленнее ×600": ({"learning_rate": 0.03}, 600, None),
    "осторожнее (100 в листе)": ({"min_data_in_leaf": 100}, 300, None),
    "короче ×150": ({}, 150, None),
    "свежее: полураспад 90 дн": ({}, 300, 90),
    "свежее: полураспад 180 дн": ({}, 300, 180),
    "свежее: полураспад 365 дн": ({}, 300, 365),
}


def train(df, extra, rounds, half_life):
    X, y = df[ml.FEATURES], df["actual_c"] - df["fc_mean"]
    w = None
    if half_life:
        last = date.fromisoformat(df["date"].max())
        age = np.array([(last - date.fromisoformat(d)).days for d in df["date"]])
        w = 0.5 ** (age / half_life)
    return {q: lgb.train({**BASE, **extra, "alpha": q}, lgb.Dataset(X, y, weight=w, categorical_feature=["city_id"]), rounds)
            for q in mq.QUANTILES}


def score(models, te):
    qs_all = mq.predict_q(models, te[ml.FEATURES], te["fc_mean"].values)
    i50 = mq.QUANTILES.index(0.5)
    ll_m = ll_k = ll_b = err = 0.0
    n = 0
    days = []
    for (_, r), qs in zip(te.iterrows(), qs_all):
        err += abs(qs[i50] - r["actual_c"])
        w = base.WIN.get((r["city"], r["date"]))
        pr = chk.prices(conn, r["city"], r["date"], "A") if w is not None else {}
        wb = next((b for b in pr if b[0] == w), None)
        if len(pr) < 3 or wb is None:
            continue
        keys = sorted(pr)
        P = [mq.bucket_prob(list(qs), r["unit"], b[0], b[1]) for b in keys]
        t = sum(P) or 1.0
        P = [x / t for x in P]
        B = blend_with_market(P, [pr[b] for b in keys])
        i = keys.index(wb)
        tk = sum(pr.values()) or 1.0
        ll_m += -math.log(max(P[i], 1e-4))
        ll_k += -math.log(max(pr[wb] / tk, 1e-4))
        ll_b += -math.log(max(B[i], 1e-4))
        n += 1
        days.append({"city": r["city"], "date": r["date"], "keys": keys, "price": [pr[b] for b in keys],
                     "blend": dict(zip(keys, B)), "win": w})
    return {"ll": ll_m / n, "mkt": ll_k / n, "blend": ll_b / n, "err": err / len(te), "days": days}


if __name__ == "__main__":
    ml.USE_MKT = True
    df = ml.build(conn)
    df = df[df["actual_c"].notna()]
    ml.FEATURES = ml.features(df)
    print(f"данных: {len(df)} город-дней, признаков {len(ml.FEATURES)}")
    folds = (("август", "2026-08-01", "2026-09-01"), ("сентябрь", "2026-09-01", "2026-10-01"))
    only = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--seeds" in sys.argv:
        # 2026-09-26: повторное обучение колеблется ~±0.005 — сравниваем средние по 3 зёрнам
        for name in ("как сейчас", "крупнее (31 лист)"):
            extra, rounds, hl = VARIANTS[name]
            res = {"август": [], "сентябрь": [], "сентябрь_смесь": [], "август_смесь": []}
            for seed in (11, 22, 33):
                ex = {**extra, "seed": seed, "bagging_seed": seed, "feature_fraction_seed": seed}
                for label, start, end in folds:
                    s_ = score(train(df[df["date"] < start], ex, rounds, hl), df[(df["date"] >= start) & (df["date"] < end)])
                    res[label].append(s_["ll"]); res[label + "_смесь"].append(s_["blend"])
            avg = lambda v: sum(v) / len(v)
            print(f"{name:20s} | август: модель {avg(res['август']):.4f} (±{max(res['август']) - min(res['август']):.4f}) "
                  f"смесь {avg(res['август_смесь']):.4f} | сентябрь: модель {avg(res['сентябрь']):.4f} "
                  f"(±{max(res['сентябрь']) - min(res['сентябрь']):.4f}) смесь {avg(res['сентябрь_смесь']):.4f}", flush=True)
        sys.exit()
    for name, (extra, rounds, hl) in VARIANTS.items():
        if only and not any(o in name for o in only):
            continue
        line = f"{name:28s}"
        for label, start, end in folds:
            tr = df[df["date"] < start]
            te = df[(df["date"] >= start) & (df["date"] < end)]
            s = score(train(tr, extra, rounds, hl), te)
            line += f" | {label}: модель {s['ll']:.3f} смесь {s['blend']:.3f} (рынок {s['mkt']:.3f}) ошибка {s['err']:.3f}°"
            if label == "сентябрь":
                r = base.run_rule(s["days"], "blend", 0.03)
                line += f" | смесь 3 п.п., реальные сделки {r['n_real']} ставок {r['pnl_real']:+.1f}$"
        print(line, flush=True)
