"""
Длина истории для обучения (2026-09-28, вопрос Alex по совету ChatGPT: «какая длина истории даёт лучшую
ошибку на новых днях»). Уже проверено 26.09 (study_tune.log) и не повторяем: вес свежим дням (полураспад
90/180/365) и число деревьев (150/300/600) — не лучше «как сейчас».

Новое здесь:
- жёсткое окно: учим только на последних 90 / 180 / 365 днях против всей истории с 2025-06-01;
- проверка по неделям (учим на всём до недели — проверяем неделю, сдвигаемся), август-сентябрь 2026:
  видно, растёт ли отставание «всей истории» со временем (дрейф);
- ошибка по каждому из 13 уровней распределения (pinball), ошибка медианы в градусах (MAE / RMSE);
- best_iteration: ранняя остановка на последних 14 днях перед неделей — сколько деревьев модели нужно.

Порог (зафиксирован 28.09 до запуска): вариант лучше «всей истории», только если логошибка ниже минимум на
0.015 в среднем по 3 обучениям (зёрна 11/22/33). Прошёл — отдельным кошельком, главную модель не меняем.

Только на копии базы (-e POLY_LAB_DB=/data/research/research.sqlite3). В копии есть лишний год (2024-06..) —
отбрасываем, как в рабочей модели. Сам останавливается в DEADLINE по Кишинёву (ночью — тяжёлые задачи крона),
готовые недели уже напечатаны.
"""

import math
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import lightgbm as lgb
import numpy as np

import weather_ml as ml
import weather_ml_q as mq
import weather_study_tune as tune

SEEDS = (11, 22, 33)
FROM = "2025-06-01"
WINDOWS = (("вся история", None), ("последние 365 дн", 365), ("последние 180 дн", 180), ("последние 90 дн", 90))
WEEK0 = date(2026, 8, 3)  # понедельник; недели до последнего дня с фактом
THREADS = 4
DEADLINE = datetime.now(ZoneInfo("Europe/Chisinau")).replace(hour=4, minute=45, second=0, microsecond=0)
if DEADLINE < datetime.now(ZoneInfo("Europe/Chisinau")):
    DEADLINE += timedelta(days=1)


def pinball(qs, y):
    """Средняя pinball-ошибка по каждому уровню: [len(QUANTILES)]."""
    d = y[:, None] - qs
    Q = np.array(mq.QUANTILES)[None, :]
    return np.mean(np.maximum(Q * d, (Q - 1) * d), axis=0)


def best_iter(tr, seed):
    """Ранняя остановка: учим без последних 14 дней, проверяем на них; уровни 10/50/90%."""
    cut = (date.fromisoformat(tr["date"].max()) - timedelta(days=14)).isoformat()
    a, v = tr[tr["date"] <= cut], tr[tr["date"] > cut]
    out = {}
    for q in (0.1, 0.5, 0.9):
        p = {**tune.BASE, "alpha": q, "seed": seed, "num_threads": THREADS}
        dt = lgb.Dataset(a[ml.FEATURES], a["actual_c"] - a["fc_mean"], categorical_feature=["city_id"])
        dv = lgb.Dataset(v[ml.FEATURES], v["actual_c"] - v["fc_mean"], reference=dt)
        b = lgb.train(p, dt, 1000, valid_sets=[dv], callbacks=[lgb.early_stopping(50, verbose=False)])
        out[q] = b.best_iteration
    return out


if __name__ == "__main__":
    tz = ZoneInfo("Europe/Chisinau")
    ml.USE_MKT = True
    df = ml.build(tune.conn)
    df = df[df["actual_c"].notna() & (df["date"] >= FROM)]
    ml.FEATURES = ml.features(df)
    last = date.fromisoformat(df["date"].max())
    weeks = []
    w = WEEK0
    while w <= last:
        weeks.append((w.isoformat(), min(w + timedelta(days=7), last + timedelta(days=1)).isoformat()))
        w += timedelta(days=7)
    if "--one" in sys.argv:
        weeks = weeks[:1]
    print(f"данных: {len(df)} город-дней с {FROM} по {last}, признаков {len(ml.FEATURES)}; недель {len(weeks)}; "
          f"стоп в {DEADLINE:%H:%M}", flush=True)
    tot = {name: {"ll": [], "blend": [], "mkt": [], "n": [], "nte": [], "mae": [], "sq": [], "pin": [], "days": []}
           for name, _ in WINDOWS}
    for ws, we in weeks:
        if datetime.now(tz) >= DEADLINE:
            print(f"СТОП по времени ({DEADLINE:%H:%M}) — дальше недели не считали", flush=True)
            break
        te = df[(df["date"] >= ws) & (df["date"] < we)]
        y = te["actual_c"].values
        bi = best_iter(df[df["date"] < ws], SEEDS[0])
        line = f"неделя {ws[8:]}.{ws[5:7]} ({len(te)} дн; деревьев нужно: 10% {bi[0.1]}, 50% {bi[0.5]}, 90% {bi[0.9]})"
        for name, win in WINDOWS:
            start = (date.fromisoformat(ws) - timedelta(days=win)).isoformat() if win else FROM
            tr = df[(df["date"] >= start) & (df["date"] < ws)]
            lls, bls, mks, qs_list = [], [], [], []
            for seed in SEEDS:
                models = tune.train(tr, {"seed": seed, "bagging_seed": seed, "feature_fraction_seed": seed,
                                         "num_threads": THREADS}, 300, None)
                qs_list.append(mq.predict_q(models, te[ml.FEATURES], te["fc_mean"].values))
                s = tune.score(models, te)
                lls.append(s["ll"]); bls.append(s["blend"]); mks.append(s["mkt"])
                if seed == SEEDS[0]:
                    days = s["days"]
            qs = np.mean(qs_list, axis=0)
            med = qs[:, mq.QUANTILES.index(0.5)]
            t = tot[name]
            n = len(days)
            t["ll"].append(np.mean(lls) * n); t["blend"].append(np.mean(bls) * n); t["mkt"].append(np.mean(mks) * n)
            t["n"].append(n); t["nte"].append(len(y)); t["mae"].append(np.sum(np.abs(med - y))); t["sq"].append(np.sum((med - y) ** 2))
            t["pin"].append(pinball(qs, y) * len(y)); t["days"] += days
            line += f"\n   {name:17s} строк {len(tr):5d} | логошибка {np.mean(lls):.4f} (±{(max(lls) - min(lls)) / 2:.4f}) " \
                    f"смесь {np.mean(bls):.4f} рынок {np.mean(mks):.4f} | MAE {np.mean(np.abs(med - y)):.3f}°"
        print(line, flush=True)

    done = [name for name, _ in WINDOWS if tot[name]["n"]]
    if not done:
        sys.exit(0)
    N = sum(tot[done[0]]["n"])
    print(f"\nИТОГ за {len(tot[done[0]]['n'])} недель ({N} город-дней с ценами):", flush=True)
    base_ll = sum(tot["вся история"]["ll"]) / N
    for name in done:
        t = tot[name]
        ll, n_te = sum(t["ll"]) / N, sum(t["nte"])
        pin = np.sum(t["pin"], axis=0) / n_te
        r = tune.base.run_rule([d for d in t["days"] if d["date"] >= "2026-09-01"], "blend", 0.03)
        verdict = "—" if name == "вся история" else ("ПРОШЁЛ порог" if base_ll - ll >= 0.015 else "не прошёл")
        print(f"{name:17s} логошибка {ll:.4f} ({ll - base_ll:+.4f} к всей истории, {verdict}) смесь {sum(t['blend']) / N:.4f} "
              f"рынок {sum(t['mkt']) / N:.4f} | MAE {sum(t['mae']) / n_te:.3f}° RMSE {math.sqrt(sum(t['sq']) / n_te):.3f}° | "
              f"деньги смеси сент. {r['pnl_real']:+.1f}$ ({r['n_real']} ставок)\n   уровни: "
              + " ".join(f"{int(q * 100)}%:{p:.3f}" for q, p in zip(mq.QUANTILES, pin)), flush=True)
