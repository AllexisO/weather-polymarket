"""
П1 «поправка к рынку» (идея Argon 28.10, вариант S+; проверка — data/research/_argon3_1028.py, _argon4_1028.py, журнал 28.10).

Модель учит не температуру, а шанс каждого варианта выиграть при цене рынка в 08:00: LightGBM binary на строку «город-день × вариант»,
затем нормировка по дню. Признаки (9, порядок важен — FEATURES):
  logit цены варианта (доля от суммы), доли соседей слева/справа, (медиана v5_31 − центр рынка) °C, ширина рынка mkt_std, цена фаворита
  mkt_top_p, разброс 16 прогнозов fc_std, положение варианта (середина − центр рынка) / max(mkt_std, 0.3), крайний вариант (1/0).
Вариант «o» (кошелёк ml5_p1o, 28.10): + 10-й признак — середина варианта − последний замер obs_t, известный к 08:00 (°C).
Проверка data/research/_argon5_1028.py: лучше П1 S+ во всех 06-10 (−0.0061…−0.0009), В2 на нём — стаканы 05-26.10 +$153.57 против +$111.77 (при весе 0.85; при живом весе 0.75 — +$120.48 против +$119.61).
Обучение — один раз на всех днях 05-10.2026 (v7_days.pkl, прогнозы v5_31 вне обучения — v5x_argon_preds.json L31, признаки дня —
v5x_dfm.pkl), 3 зерна (11/22/33), параметры как в проверке. Модели — data/ml/p1_s{зерно}.txt и p1o_s{зерно}.txt.
Вживую v5_31 — та же модель, что у ml5_31.

В2 (кошельки ml5_p1, ml5_p1o): шанс = (1 − W)·смесь ml5_31_cal + W·П1, W = 0.75 постоянно (решение Alex 28.10, до первой ставки).
Было: w = min(1, дней от 01.05 / 180) — формула Argon, с ноября давала 1.0 (чистый П1). Постоянный W: обе формулы Argon (потолок 0.75 и
N/(N + 2) при 6 месяцах обучения) дают для ноября 0.75; П1 не переобучается, так что вес не должен расти; на 05-26.10 W 1.0 хуже всех
(_argon5_1028.py: 0.75 +$119.61, 1.0 +$90.97).
Обучение: docker compose run --rm --entrypoint python collector weather_p1.py --train   (оба варианта)
"""

import json
import math
import pickle
import sys
from pathlib import Path

import numpy as np

ML_DIR = Path("/data/ml")
SEEDS = (11, 22, 33)
PRM = dict(num_leaves=15, learning_rate=0.03, min_data_in_leaf=40, feature_fraction=0.8, bagging_fraction=0.8,
           bagging_freq=1, lambda_l2=1.0, verbose=-1, objective="binary")
ROUNDS = 400
W = 0.75
FEATURES = ["logit_p", "p_left", "p_right", "v5_vs_mkt", "mkt_std", "mkt_top_p", "fc_std", "pos", "edge"]
VARIANTS = {"": FEATURES, "o": FEATURES + ["mid_vs_obs"]}   # имя файла p1{вариант}_s{зерно}.txt → признаки
_models = {}


def _lgt(x):
    x = min(max(x, 1e-3), 1 - 1e-3)
    return math.log(x / (1 - x))


def mids(unit, bs):
    to_c = (lambda v: (v - 32) * 5 / 9) if unit == "fahrenheit" else (lambda v: v)
    s = 2 if unit == "fahrenheit" else 1
    return {b: to_c(b[1] - 0.5 - s / 2) if b[0] <= -900 else to_c(b[0] + 0.5 + s / 2) if b[1] >= 900 else to_c((b[0] + b[1]) / 2)
            for b in bs}


def rows(prices, unit, v531_median_c, mkt_std, mkt_top_p, fc_std, obs_t=None, variant=""):
    """prices — {(lo, hi): цена}; строки признаков в порядке sorted(prices) (так же, как в проверке)."""
    bs = sorted(prices)
    tot = sum(prices.values()) or 1.0
    p = {b: prices[b] / tot for b in bs}
    md = mids(unit, bs)
    cm = sum(p[b] * md[b] for b in bs)
    sd = mkt_std or 1.0
    has_obs = obs_t is not None and not math.isnan(obs_t)
    out = []
    for i, b in enumerate(bs):
        f = [_lgt(p[b]), p[bs[i - 1]] if i > 0 else 0.0, p[bs[i + 1]] if i + 1 < len(bs) else 0.0,
             v531_median_c - cm, mkt_std or 0.0, mkt_top_p or 0.0, fc_std,
             (md[b] - cm) / max(sd, 0.3), 1.0 if (b[0] <= -900 or b[1] >= 900) else 0.0]
        if variant == "o":
            f.append(md[b] - obs_t if has_obs else float("nan"))
        out.append(f)
    return bs, out


def predict(prices, unit, v531_median_c, mkt_std, mkt_top_p, fc_std, obs_t=None, variant=""):
    """{(lo, hi): шанс П1} (сумма 1) или None, если моделей нет."""
    if variant not in _models:
        import lightgbm as lgb
        files = [ML_DIR / f"p1{variant}_s{s}.txt" for s in SEEDS]
        if not all(f.exists() for f in files):
            return None
        _models[variant] = [lgb.Booster(model_file=str(f)) for f in files]
    bs, X = rows(prices, unit, v531_median_c, mkt_std, mkt_top_p, fc_std, obs_t, variant)
    v = np.mean([m.predict(np.array(X, float)) for m in _models[variant]], axis=0)
    v = v / v.sum()
    return dict(zip(bs, (float(x) for x in v)))


def blend_v2(cal_probs, p1_probs):
    mix = [(1 - W) * c + W * q for c, q in zip(cal_probs, p1_probs)]
    s = sum(mix) or 1.0
    return [x / s for x in mix]


def train(before=None, out_dir=ML_DIR, variants=("", "o")):
    """before — только дни раньше этой даты (для сверки с проверкой); out_dir — куда писать модели."""
    import lightgbm as lgb
    from weather_cities import OBS_CITIES
    P = json.load(open("/data/research/v5x_argon_preds.json"))["L31"]
    days = pickle.load(open("/data/research/v7_days.pkl", "rb"))
    dfm = pickle.load(open("/data/research/v5x_dfm.pkl", "rb"))
    FR = {f"{r['city']}|{r['date']}": r for r in dfm.to_dict("records")}
    for variant in variants:
        X, y, n = [], [], 0
        for d in days:
            r = FR.get(d["key"])
            if r is None or d["key"] not in P or (before and d["key"].split("|")[1] >= before):
                continue
            city = d["key"].split("|")[0]
            bs, f = rows(d["pr"], OBS_CITIES[city]["unit"], float(np.median(P[d["key"]])), r.get("mkt_std"), r.get("mkt_top_p"),
                         r["fc_std"], r.get("obs_t"), variant)
            X += f
            y += [1 if b == d["w"] else 0 for b in bs]
            n += 1
        X, y = np.array(X, float), np.array(y)
        out_dir.mkdir(parents=True, exist_ok=True)
        for s in SEEDS:
            lgb.train({**PRM, "seed": s}, lgb.Dataset(X, y, feature_name=VARIANTS[variant]), ROUNDS).save_model(
                str(out_dir / f"p1{variant}_s{s}.txt"))
        print(f"П1{variant}: обучено на {n} город-днях ({len(y)} строк), признаков {len(VARIANTS[variant])}")


if __name__ == "__main__":
    if "--train" in sys.argv:
        sys.path.insert(0, "/app")
        train()
