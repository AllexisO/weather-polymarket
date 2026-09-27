"""
Обучаемая модель, версия 2: учит РАСПРЕДЕЛЕНИЕ, а не одно число
(2026-09-25, пункты 1-2 плана Alex).

Версия 1 (weather_ml.py) предсказывает одно число (поправку к среднему
прогнозу), а шансы вариантов считает по нормальному распределению с
ОДИНАКОВЫМ разбросом для города на все дни. Здесь — квантильная
регрессия LightGBM: 13 моделей, каждая учит свой уровень ("с
вероятностью 10% ниже ..., 50% ниже ..., 90% ниже ..."). Отсюда сразу:
1) разброс свой на каждый день — в спокойный ясный день уровни близко,
   в день с фронтом/грозой — далеко;
2) несимметричность — нижние и верхние уровни могут отстоять от
   середины по-разному (в жару чаще недобор из-за облаков/гроз).
Шанс варианта = доля распределения между его границами (линейно между
уровнями, за крайними уровнями — хвост нормального распределения,
подогнанный по двум крайним уровням).

Признаки и walk-forward — те же, что у версии 1 (weather_ml.build,
переобучение по неделям). Сравнение с версией 1 и с рынком — на тех же
днях: логарифмическая ошибка по реальному исходу (чем меньше, тем
точнее оценены шансы), попадание, калибровка, ставки по цене из
истории и по реальным сделкам.

Запуск: python weather_ml_q.py
"""

import json
import math
import os
import random
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import lightgbm as lgb
import numpy as np
import pandas as pd

import weather_ml as ml
import weather_ml_check as chk
from weather_cities import OBS_CITIES
from weather_edge import emos_bucket_prob

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
QUANTILES = [0.02, 0.05, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 0.95, 0.98]
Q_PARAMS = dict(objective="quantile", learning_rate=0.05, num_leaves=15, min_data_in_leaf=40,
                feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=1, lambda_l2=1.0, verbose=-1)
Q_ROUNDS = 300


def _z(p):
    # обратная функция нормального распределения (приближение Acklam)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02, 1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02, 6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00, -2.549671010173382e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00]
    if p < 0.02425:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > 1 - 0.02425:
        return -_z(1 - p)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


Z = [_z(q) for q in QUANTILES]


def cdf(x, qs):
    """Доля распределения ниже x по предсказанным уровням qs (отсортированы)."""
    if x <= qs[0]:
        s = max((qs[2] - qs[0]) / (Z[2] - Z[0]), 0.05)
        return 0.5 * (1 + math.erf(((x - qs[0]) / s + Z[0]) / math.sqrt(2)))
    if x >= qs[-1]:
        s = max((qs[-1] - qs[-3]) / (Z[-1] - Z[-3]), 0.05)
        return 0.5 * (1 + math.erf(((x - qs[-1]) / s + Z[-1]) / math.sqrt(2)))
    i = int(np.searchsorted(qs, x)) - 1
    span = qs[i + 1] - qs[i]
    t = (x - qs[i]) / span if span > 1e-9 else 0.5
    return QUANTILES[i] + t * (QUANTILES[i + 1] - QUANTILES[i])


def bucket_prob(qs_c, unit, lo, hi):
    """qs_c — уровни ФАКТИЧЕСКОГО максимума в °C; lo/hi — границы бакета в единицах города."""
    to_c = (lambda v: (v - 32) * 5 / 9) if unit == "fahrenheit" else (lambda v: v)
    p_lo = 0.0 if lo <= -900 else cdf(to_c(lo), qs_c)
    p_hi = 1.0 if hi >= 900 else cdf(to_c(hi), qs_c)
    return max(p_hi - p_lo, 0.0)


def train_q(df_train):
    X, y = df_train[ml.FEATURES], df_train["actual_c"] - df_train["fc_mean"]
    return {q: lgb.train({**Q_PARAMS, "alpha": q}, lgb.Dataset(X, y, categorical_feature=["city_id"]), Q_ROUNDS)
            for q in QUANTILES}


def predict_q(models, X, fc_mean):
    raw = np.column_stack([models[q].predict(X) for q in QUANTILES])
    raw.sort(axis=1)  # уровни не должны пересекаться
    return raw + np.asarray(fc_mean)[:, None]


def walk_forward_q(df):
    out, week = [], ml.WF_START
    last = date.fromisoformat(df["date"].max())
    while week <= last:
        nxt = week + timedelta(days=7)
        tr = df[df["date"] < week.isoformat()]
        te = df[(df["date"] >= week.isoformat()) & (df["date"] < nxt.isoformat())]
        if not te.empty and len(tr) >= 500:
            qs = predict_q(train_q(tr), te[ml.FEATURES], te["fc_mean"])
            p = te[["city", "date", "unit", "actual_c", "fc_mean"]].copy()
            p["qs"] = [json.dumps([round(v, 3) for v in row]) for row in qs]
            out.append(p)
            print(f"неделя с {week}: обучено на {len(tr)} днях", flush=True)
        week = nxt
    return pd.concat(out)


def compare(conn, q_preds):
    v1 = pd.read_sql("SELECT * FROM ml_preds_wf", conn).set_index(["city", "date"])
    win = {(r[0], r[1]): r[2] for r in conn.execute("SELECT city, local_date, win_lo FROM weather_poly_outcomes")}
    cid = {(r[0], r[1], r[2]): r[3] for r in conn.execute("SELECT city, local_date, bucket_lo, condition_id FROM poly_market_final")}
    first_trade = conn.execute("SELECT MIN(local_date) FROM poly_trades_days").fetchone()[0]
    res = {}
    calib = {"v1": {}, "v2": {}}
    for _, r in q_preds.iterrows():
        key = (r["city"], r["date"])
        w = win.get(key)
        if w is None or key not in v1.index:
            continue
        pr = chk.prices(conn, r["city"], r["date"], "A")
        if len(pr) < 3:
            continue
        per = "сентябрь" if r["date"] >= "2026-09-01" else "июль-август"
        k, off = (9 / 5, 32) if r["unit"] == "fahrenheit" else (1, 0)
        a = v1.loc[key]
        qs = json.loads(r["qs"])
        probs = {
            "v1": {b: emos_bucket_prob(a["ml_mu_c"] * k + off, a["ml_sigma_c"] * k, b[0], b[1]) for b in pr},
            "v2": {b: bucket_prob(qs, r["unit"], b[0], b[1]) for b in pr},
            "рынок": {b: p for b, p in pr.items()},
        }
        for name, P in probs.items():
            s = res.setdefault((per, name), {"n": 0, "ll": 0.0, "hit": 0, "bets": [], "real": []})
            tot = sum(P.values()) or 1.0
            s["n"] += 1
            s["ll"] += -math.log(max(P.get(next((b for b in P if b[0] == w), None), 0.0) / tot, 1e-4))
            s["hit"] += max(P, key=P.get)[0] == w
            if name == "рынок":
                continue
            for b, p in P.items():
                c = calib[name].setdefault(min(int(p * 10), 9), [0, 0])
                c[0] += 1
                c[1] += b[0] == w
            b = max(pr, key=lambda x: P[x] - pr[x])
            if P[b] - pr[b] >= 0.10 and 0.03 <= pr[b] <= 0.95:
                won = b[0] == w
                s["bets"].append(chk.pnl(pr[b], won))
                if r["date"] >= first_trade and (r["city"], r["date"], b[0]) in cid:
                    ts = datetime.fromisoformat(r["date"]).replace(tzinfo=ZoneInfo(OBS_CITIES[r["city"]]["tz"])).timestamp() + 8 * 3600
                    maxp = min(0.95, P[b] - 0.10)
                    fills = sorted((t, (p if o == "Yes" else 1 - p)) for o, sd, p, t in conn.execute(
                        "SELECT outcome, side, price, ts FROM poly_trades WHERE condition_id = ? AND ts BETWEEN ? AND ?",
                        (cid[(r["city"], r["date"], b[0])], ts, ts + 1800))
                        if ((o == "Yes" and sd == "BUY") or (o == "No" and sd == "SELL")) and (p if o == "Yes" else 1 - p) <= maxp)
                    if fills:
                        s["real"].append(chk.pnl(fills[0][1] - 0.01, won))
    print("\n                      логошибка  угадан   ставок  итог$   без5лучших  95%-интервал      | реальные сделки")
    for per in ("июль-август", "сентябрь"):
        for name in ("рынок", "v1", "v2"):
            s = res.get((per, name))
            if not s:
                continue
            line = f"{per:11} {name:6}  {s['ll'] / s['n']:8.3f}  {100 * s['hit'] / s['n']:5.1f}%"
            if s["bets"]:
                b = s["bets"]
                random.seed(1)
                bs = sorted(sum(random.choice(b) for _ in b) for _ in range(1000))
                top5 = sum(sorted(b)[-5:])
                line += (f"  {len(b):6} {sum(b):+7.0f}  {sum(b) - top5:+8.0f}   {bs[25]:+6.0f}…{bs[975]:+6.0f}"
                         f"   | {len(s['real'])} ставок {sum(s['real']):+.0f}$" if s["real"] else "")
            print(line)
    print("\nкалибровка (модель говорит → реально выигрывает):")
    for name in ("v1", "v2"):
        print(f"  {name}: " + "  ".join(f"{k * 10}-{k * 10 + 10}%→{100 * v[1] / v[0]:.0f}%({v[0]})"
                                        for k, v in sorted(calib[name].items()) if v[0] >= 30))


def main():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    df = ml.build(conn)
    df = df[df["actual_c"].notna()]
    ml.FEATURES = ml.features(df)
    q_preds = walk_forward_q(df)
    wconn = sqlite3.connect(DB_PATH, timeout=60)
    q_preds.to_sql("ml_preds_q", wconn, if_exists="replace", index=False)
    wconn.close()
    compare(conn, q_preds)
    conn.close()


if __name__ == "__main__":
    main()
