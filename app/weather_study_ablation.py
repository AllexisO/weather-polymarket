"""
Вторая проверка после weather_study_window.py (2026-09-28, Alex + совет ChatGPT). Та же схема: 8 недель
авг-сент 2026, учим на всём до недели — проверяем неделю, 3 обучения (зёрна 11/22/33), модель v3.

Варианты (все — против «как сейчас», в одинаковых условиях):
- вес свежим дням: полураспад 120 и 300 дней (26.09 проверяли 90/180/365, но помесячно и одним обучением);
- 500 деревьев вместо 300 (ранняя остановка 28.09 чаще просила 400-500);
- ablation — без группы признаков: без рынка, без прошлых ошибок, без утреннего METAR, без прогнозов
  отдельных моделей; и «только сырая погода» (прогнозы, условия, город, сезон). Показывает, откуда точность.
Плюс калибровка уровней: доля дней, где факт ≤ уровня «10%», должна быть около 10% и т. д.

Порог (зафиксирован 28.09 до запуска): вариант лучше «как сейчас», только если логошибка ниже минимум на
0.015 в среднем по 3 обучениям. Ablation — для понимания, не для замены.

Только на копии базы. Запуск: python weather_study_ablation.py [имена вариантов через пробел]
"""

import math
import sys
from datetime import date, timedelta

import numpy as np

import weather_ml as ml
import weather_ml_q as mq
import weather_study_tune as tune

SEEDS = (11, 22, 33)
FROM = "2025-06-01"
WEEK0 = date(2026, 8, 3)
THREADS = 4

GROUPS = {
    "рынок": lambda f: f.startswith("mkt_"),
    "прошлые ошибки": lambda f: f in ("prev_actual_vs_fc", "prev_err", "mix_vs_fc"),
    "METAR": lambda f: f.startswith("obs_"),
    "модели по отдельности": lambda f: (f.startswith("fc_") and f != "fc_mean") ,
}
# name: (half_life, rounds, признак выкинуть?)
VARIANTS = {
    "как сейчас": (None, 300, None),
    "свежее 120": (120, 300, None),
    "свежее 300": (300, 300, None),
    "500 деревьев": (None, 500, None),
    "без рынка": (None, 300, GROUPS["рынок"]),
    "без прошлых ошибок": (None, 300, GROUPS["прошлые ошибки"]),
    "без METAR": (None, 300, GROUPS["METAR"]),
    "без моделей по отдельности": (None, 300, GROUPS["модели по отдельности"]),
    "только сырая погода": (None, 300, lambda f: GROUPS["рынок"](f) or GROUPS["прошлые ошибки"](f) or GROUPS["METAR"](f)),
}

if __name__ == "__main__":
    ml.USE_MKT = True
    df = ml.build(tune.conn)
    df = df[df["actual_c"].notna() & (df["date"] >= FROM)]
    ALL = ml.features(df)
    last = date.fromisoformat(df["date"].max())
    weeks, w = [], WEEK0
    while w <= last:
        weeks.append((w.isoformat(), min(w + timedelta(days=7), last + timedelta(days=1)).isoformat()))
        w += timedelta(days=7)
    only = [a for a in sys.argv[1:] if not a.startswith("--")]
    names = [n for n in VARIANTS if not only or n in only]
    print(f"данных: {len(df)} город-дней, признаков {len(ALL)}, недель {len(weeks)}; варианты: {', '.join(names)}", flush=True)
    for name in names:
        hl, rounds, drop = VARIANTS[name]
        ml.FEATURES = [f for f in ALL if not (drop and drop(f))]
        ll = bl = mk = mae = sq = 0.0
        n = nte = 0
        cover = np.zeros(len(mq.QUANTILES))
        weekly, days_all = [], []
        for ws, we in weeks:
            tr, te = df[df["date"] < ws], df[(df["date"] >= ws) & (df["date"] < we)]
            y = te["actual_c"].values
            lls, bls, mks, qs_list = [], [], [], []
            for seed in SEEDS:
                models = tune.train(tr, {"seed": seed, "bagging_seed": seed, "feature_fraction_seed": seed,
                                         "num_threads": THREADS}, rounds, hl)
                qs_list.append(mq.predict_q(models, te[ml.FEATURES], te["fc_mean"].values))
                s = tune.score(models, te)
                lls.append(s["ll"]); bls.append(s["blend"]); mks.append(s["mkt"])
                if seed == SEEDS[0]:
                    days_all += s["days"]
                    k = len(s["days"])
            qs = np.mean(qs_list, axis=0)
            med = qs[:, mq.QUANTILES.index(0.5)]
            ll += np.mean(lls) * k; bl += np.mean(bls) * k; mk += np.mean(mks) * k; n += k
            mae += np.sum(np.abs(med - y)); sq += np.sum((med - y) ** 2); nte += len(y)
            cover += np.sum(y[:, None] <= qs, axis=0)
            weekly.append(f"{ws[8:]}.{ws[5:7]} {np.mean(lls):.3f}")
        r = tune.base.run_rule([d for d in days_all if d["date"] >= "2026-09-01"], "blend", 0.03)
        cov = cover / nte * 100
        print(f"{name:27s} признаков {len(ml.FEATURES):2d} | логошибка {ll / n:.4f} смесь {bl / n:.4f} рынок {mk / n:.4f} | "
              f"MAE {mae / nte:.3f}° RMSE {math.sqrt(sq / nte):.3f}° | деньги смеси сент. {r['pnl_real']:+.1f}$ ({r['n_real']} ставок)\n"
              f"   по неделям: {' · '.join(weekly)}\n"
              f"   калибровка (факт ≤ уровня, % дней; должно быть как уровень): "
              + " ".join(f"{int(q * 100)}→{c:.0f}" for q, c in zip(mq.QUANTILES, cov)), flush=True)
