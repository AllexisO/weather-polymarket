"""
Три способа обучить модель иначе (2026-09-28, решение Alex «сделаем все 3»). Схема как в weather_study_ablation.py:
8 недель авг-сент 2026, учим на всём до недели — проверяем неделю, каждый вариант — 3 обучения (зёрна 11/22/33),
прогноз — среднее трёх. Модель — признаки v3 (с мнением рынка).

1. «от рынка»: отправная точка — среднее рынка в 08:00 (fc_mean + mkt_mean_vs_fc), а не среднее 16 моделей;
   модель учит только поправку к рынку. Где цены нет — как сейчас, от fc_mean.
2. «шансы напрямую»: вместо 13 уровней (pinball) — 21 модель «максимум ≤ fc_mean + k» (k от −5 до +5°C шагом 0.5,
   логошибка). Шансы → уровни распределения (обратная интерполяция) → те же корзины.
3. «команда»: LightGBM (как сейчас) + CatBoost (MultiQuantile) + HistGradientBoosting (sklearn) — среднее уровней;
   печатаем и каждую модель отдельно.

Порог (зафиксирован 28.09 до запуска): логошибка лучше «как сейчас» (тоже среднее 3 обучений) минимум на 0.015,
деньги смеси в сентябре по настоящим сделкам — не хуже. Прошло — отдельным кошельком.
Только на копии базы. CatBoost и sklearn ставятся во временный контейнер при запуске (рабочий образ не меняется).
"""

import math
import sys
from datetime import date, timedelta

import lightgbm as lgb
import numpy as np

import weather_ml as ml
import weather_ml_q as mq
import weather_study_tune as tune

SEEDS = (11, 22, 33)
FROM = "2025-06-01"
WEEK0 = date(2026, 8, 3)
THREADS = 4
KS = np.arange(-5.0, 5.01, 0.5)
Q = np.array(mq.QUANTILES)


def lgb_q(tr, y, seed):
    p = {**tune.BASE, "seed": seed, "bagging_seed": seed, "feature_fraction_seed": seed, "num_threads": THREADS}
    return {q: lgb.train({**p, "alpha": q}, lgb.Dataset(tr[ml.FEATURES], y, categorical_feature=["city_id"]), 300)
            for q in mq.QUANTILES}


def pred_q(models, X):
    raw = np.column_stack([models[q].predict(X) for q in mq.QUANTILES])
    raw.sort(axis=1)
    return raw


def cdf_models(tr, y, seed):
    p = {**{k: v for k, v in tune.BASE.items() if k not in ("objective", "alpha")}, "objective": "binary",
         "seed": seed, "bagging_seed": seed, "feature_fraction_seed": seed, "num_threads": THREADS}
    return {k: lgb.train(p, lgb.Dataset(tr[ml.FEATURES], (y <= k).astype(int), categorical_feature=["city_id"]), 300)
            for k in KS}


def cdf_to_q(models, X):
    C = np.column_stack([models[k].predict(X) for k in KS])
    C = np.maximum.accumulate(np.clip(C, 1e-4, 1 - 1e-4), axis=1)
    xs = np.concatenate([[-12.0], KS, [12.0]])
    out = []
    for row in C:
        ys = np.concatenate([[0.0], row, [1.0]])
        ys = ys + np.arange(len(ys)) * 1e-9  # строго возрастает для обратной интерполяции
        out.append(np.interp(Q, ys, xs))
    return np.array(out)


def cat_q(tr, y, seed):
    from catboost import CatBoostRegressor
    X = tr[ml.FEATURES].copy()
    X["city_id"] = X["city_id"].astype(int)
    m = CatBoostRegressor(loss_function="MultiQuantile:alpha=" + ",".join(str(q) for q in mq.QUANTILES), iterations=500,
                          learning_rate=0.05, depth=6, random_seed=seed, verbose=False, thread_count=THREADS,
                          cat_features=["city_id"])
    m.fit(X, y)
    return m


def cat_pred(m, X):
    X = X.copy()
    X["city_id"] = X["city_id"].astype(int)
    r = np.asarray(m.predict(X))
    r.sort(axis=1)
    return r


def hgb_q(tr, y, seed):
    from sklearn.ensemble import HistGradientBoostingRegressor
    cat = [ml.FEATURES.index("city_id")]
    return {q: HistGradientBoostingRegressor(loss="quantile", quantile=q, max_iter=300, learning_rate=0.05, max_leaf_nodes=15,
                                             min_samples_leaf=40, l2_regularization=1.0, categorical_features=cat,
                                             random_state=seed).fit(tr[ml.FEATURES].values, y)
            for q in mq.QUANTILES}


def hgb_pred(models, X):
    r = np.column_stack([models[q].predict(X[ml.FEATURES].values) for q in mq.QUANTILES])
    r.sort(axis=1)
    return r


def score_qs(qs_abs, te):
    fake = {"__qs": qs_abs}
    orig = mq.predict_q
    mq.predict_q = lambda models, X, fc: models["__qs"]
    try:
        return tune.score(fake, te)
    finally:
        mq.predict_q = orig


def mkt_base(df):
    m = df["mkt_mean_vs_fc"] if "mkt_mean_vs_fc" in df else None
    return df["fc_mean"] + (m.fillna(0.0) if m is not None else 0.0)


if __name__ == "__main__":
    ml.USE_MKT = True
    df = ml.build(tune.conn)
    df = df[df["actual_c"].notna() & (df["date"] >= FROM)]
    ml.FEATURES = ml.features(df)
    last = date.fromisoformat(df["date"].max())
    weeks, w = [], WEEK0
    while w <= last:
        weeks.append((w.isoformat(), min(w + timedelta(days=7), last + timedelta(days=1)).isoformat()))
        w += timedelta(days=7)
    only = [a for a in sys.argv[1:] if not a.startswith("--")]
    names = ["как сейчас", "от рынка", "шансы напрямую", "CatBoost", "HistGB (sklearn)", "команда из трёх"]
    names = [n for n in names if not only or n in only or (n == "команда из трёх" and "команда" in only)]
    print(f"данных {len(df)}, признаков {len(ml.FEATURES)}, недель {len(weeks)}; варианты: {', '.join(names)}", flush=True)
    acc = {n: {"ll": 0.0, "bl": 0.0, "mk": 0.0, "n": 0, "mae": 0.0, "nte": 0, "days": [], "weekly": []} for n in names}
    for ws, we in weeks:
        tr, te = df[df["date"] < ws], df[(df["date"] >= ws) & (df["date"] < we)]
        y_fc = (tr["actual_c"] - tr["fc_mean"]).values
        qs = {}
        need_team = "команда из трёх" in names
        if "как сейчас" in names or need_team:
            qs["как сейчас"] = np.mean([pred_q(lgb_q(tr, y_fc, s), te[ml.FEATURES]) for s in SEEDS], axis=0) + te["fc_mean"].values[:, None]
        if "от рынка" in names:
            b_tr, b_te = mkt_base(tr), mkt_base(te)
            yb = (tr["actual_c"] - b_tr).values
            qs["от рынка"] = np.mean([pred_q(lgb_q(tr, yb, s), te[ml.FEATURES]) for s in SEEDS], axis=0) + b_te.values[:, None]
        if "шансы напрямую" in names:
            qs["шансы напрямую"] = np.mean([cdf_to_q(cdf_models(tr, y_fc, s), te[ml.FEATURES]) for s in SEEDS], axis=0) \
                + te["fc_mean"].values[:, None]
        if "CatBoost" in names or need_team:
            qs["CatBoost"] = np.mean([cat_pred(cat_q(tr, y_fc, s), te[ml.FEATURES]) for s in SEEDS], axis=0) + te["fc_mean"].values[:, None]
        if "HistGB (sklearn)" in names or need_team:
            qs["HistGB (sklearn)"] = np.mean([hgb_pred(hgb_q(tr, y_fc, s), te) for s in SEEDS], axis=0) + te["fc_mean"].values[:, None]
        if need_team:
            qs["команда из трёх"] = np.mean([qs["как сейчас"], qs["CatBoost"], qs["HistGB (sklearn)"]], axis=0)
        y = te["actual_c"].values
        line = f"неделя {ws[8:]}.{ws[5:7]}:"
        for n in names:
            s = score_qs(qs[n], te)
            k = len(s["days"])
            a = acc[n]
            a["ll"] += s["ll"] * k; a["bl"] += s["blend"] * k; a["mk"] += s["mkt"] * k; a["n"] += k
            a["mae"] += float(np.sum(np.abs(qs[n][:, mq.QUANTILES.index(0.5)] - y))); a["nte"] += len(y)
            a["days"] += s["days"]; a["weekly"].append(s["ll"])
            line += f" {n} {s['ll']:.3f} ·"
        print(line.rstrip(" ·"), flush=True)
    base = acc[names[0]]["ll"] / acc[names[0]]["n"] if names[0] == "как сейчас" else None
    print(f"\nИТОГ ({acc[names[0]]['n']} город-дней с ценами):", flush=True)
    for n in names:
        a = acc[n]
        ll = a["ll"] / a["n"]
        sep = [d for d in a["days"] if d["date"] >= "2026-09-01"]
        r = tune.base.run_rule(sep, "blend", 0.03)
        if "--rules" in sys.argv:
            aug = [d for d in a["days"] if d["date"] < "2026-09-01"]
            for model, thr in (("raw", 0.03), ("raw", 0.05), ("raw", 0.10), ("blend", 0.03)):
                ra, rs = tune.base.run_rule(aug, model, thr), tune.base.run_rule(sep, model, thr)
                print(f"   {n}: {'модель' if model == 'raw' else 'смесь'} {thr * 100:.0f} п.п. — август по цене 08:00 {ra['n']} ставок "
                      f"{ra['pnl']:+.1f}$ | сентябрь по сделкам {rs['n_real']} ставок {rs['pnl_real']:+.1f}$"
                      + (f" ({rs['pnl_real'] / rs['staked_real'] * 100:+.0f}%)" if rs["staked_real"] else ""), flush=True)
        cmp = "" if base is None or n == "как сейчас" else \
            f" ({ll - base:+.4f}, {'ПРОШЁЛ порог' if base - ll >= 0.015 else 'не прошёл'})"
        print(f"{n:18s} логошибка {ll:.4f}{cmp} | смесь {a['bl'] / a['n']:.4f} рынок {a['mk'] / a['n']:.4f} | "
              f"MAE {a['mae'] / a['nte']:.3f}° | деньги смеси сент. {r['pnl_real']:+.1f}$ ({r['n_real']} ставок)", flush=True)
