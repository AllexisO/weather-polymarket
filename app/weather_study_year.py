"""
Идеи 1 и 2 (2026-09-27, порядок согласован с Alex):
1. ещё год истории (2024-06-01..2025-05-31) — учит ли модель лучше, если дать ей и прошлый год;
2. усреднение нескольких обучений (разные зёрна) — стабильнее и точнее ли среднее, чем одно обучение.
Проверка как в weather_study_tune.py: учим на всём до 1 августа — проверяем август; до 1 сентября —
сентябрь. Проверочные дни и ответы одинаковые во всех вариантах. Для каждого варианта — 3 зерна:
среднее по отдельным обучениям (идея 1) и прогноз, усреднённый по 3 обучениям (идея 2).
Деньги — смесь 35/65, порог 3 п.п., сентябрь, по настоящим сделкам.
Запуск — на копии базы: POLY_LAB_DB=/data/research/research.sqlite3
"""

import sys

import numpy as np

import weather_ml as ml
import weather_ml_q as mq
import weather_study_tune as tune

SEEDS = (11, 22, 33)
FOLDS = (("август", "2026-08-01", "2026-09-01"), ("сентябрь", "2026-09-01", "2026-10-01"))
EXTRA_FROM = "2025-06-01"  # до этой даты — «лишний» год


def score_avg(qs_list, te):
    """Как tune.score, но по готовым квантилям (усреднённым по нескольким обучениям)."""
    avg = np.mean(qs_list, axis=0)
    fake = {"__qs": avg}
    orig = mq.predict_q
    mq.predict_q = lambda models, X, fc: models["__qs"]
    try:
        return tune.score(fake, te)
    finally:
        mq.predict_q = orig


if __name__ == "__main__":
    ml.USE_MKT = True
    df = ml.build(tune.conn)
    df = df[df["actual_c"].notna()]
    ml.FEATURES = ml.features(df)
    old = df[df["date"] < EXTRA_FROM]
    print(f"данных: {len(df)} город-дней, из них прошлого года (до {EXTRA_FROM}): {len(old)}, "
          f"городов в нём {old['city'].nunique()}, с {old['date'].min() if len(old) else '-'}", flush=True)
    variants = [("v3, без прошлого года", {}, False), ("v3, с прошлым годом", {}, True),
                ("v4, без прошлого года", {"num_leaves": 31}, False), ("v4, с прошлым годом", {"num_leaves": 31}, True)]
    only = [a for a in sys.argv[1:] if not a.startswith("--")]
    for name, extra, with_old in variants:
        if only and not any(o in name for o in only):
            continue
        data = df if with_old else df[df["date"] >= EXTRA_FROM]
        line = f"{name:24s}"
        for label, start, end in FOLDS:
            tr = data[data["date"] < start]
            te = df[(df["date"] >= start) & (df["date"] < end)]
            singles, qs_list = [], []
            for seed in SEEDS:
                ex = {**extra, "seed": seed, "bagging_seed": seed, "feature_fraction_seed": seed}
                models = tune.train(tr, ex, 300, None)
                qs_list.append(mq.predict_q(models, te[ml.FEATURES], te["fc_mean"].values))
                singles.append(tune.score(models, te))
            ens = score_avg(qs_list, te)
            m = [s["ll"] for s in singles]
            b = [s["blend"] for s in singles]
            line += (f" | {label}: одно обучение {np.mean(m):.4f} (разброс {max(m) - min(m):.4f}) смесь {np.mean(b):.4f}"
                     f" · среднее 3 обучений {ens['ll']:.4f} смесь {ens['blend']:.4f} (рынок {ens['mkt']:.4f})")
            if label == "сентябрь":
                r1 = [tune.base.run_rule(s["days"], "blend", 0.03)["pnl_real"] for s in singles]
                r = tune.base.run_rule(ens["days"], "blend", 0.03)
                line += f" | деньги смеси сент.: одно {np.mean(r1):+.1f}$ · среднее {r['pnl_real']:+.1f}$ ({r['n_real']} ставок)"
        print(line, flush=True)
