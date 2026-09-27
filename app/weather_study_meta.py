"""
Идеи 1 и 2 (2026-09-26, Alex): второй слой — модель, которая прямо предсказывает
«выиграет ли этот вариант при такой цене», и признаки движения рынка.

Данные: проверка вслепую v3 (ml_preds_var_mkt, июль-сентябрь; каждую неделю v3
училась только на прошлом — поэтому на её прогнозах можно учить второй слой).
На каждый вариант (бакет) города-дня — строка:
  шанс v3 и смеси, цена рынка в 08:00, место варианта среди цен (0 — фаворит),
  расстояние от фаворита и от вершины v3 (в вариантах), крайний ли вариант,
  неуверенность рынка (разброс), уверенность лидера, ширина прогноза v3, город;
  [идея 2] движение: цена варианта в 08:00 минус вечер накануне (20:00), то же для
  фаворита, сдвиг «средней температуры рынка» за ночь, размах цены варианта за ночь.
Ответ: выиграл ли вариант. LightGBM (бинарная), учим на июле-августе, проверяем на
сентябре. Сравнение: оценка шансов (логошибка по городу-дню) против рынка, v3 и смеси;
ставки (максимум «шанс − цена», порог) — по цене 08:00 и по НАСТОЯЩИМ сделкам.
Запуск — на копии базы: POLY_LAB_DB=/data/research/research.sqlite3
"""

import json
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import lightgbm as lgb
import numpy as np

import weather_study_0926 as base
import weather_ml_q as mq
from weather_cities import OBS_CITIES

conn = base.conn
CITY_ID = {c: i for i, c in enumerate(OBS_CITIES)}


def prices_at(city, date, hour, window=3600):
    """Последняя цена каждого варианта за час до момента (hour — от полуночи дня маркета, можно <0)."""
    ts = datetime.fromisoformat(date).replace(tzinfo=ZoneInfo(OBS_CITIES[city]["tz"])).timestamp() + hour * 3600
    out, rng = {}, {}
    for lo, hi, t, p in conn.execute("""SELECT bucket_lo, bucket_hi, t_utc, p FROM price_history WHERE city = ? AND local_date = ?
                                         AND t_utc BETWEEN ? AND ? ORDER BY t_utc""", (city, date, ts - window, ts)).fetchall():
        out[(lo, hi)] = p
    return out


def night_range(city, date):
    """Размах цены каждого варианта с 20:00 накануне до 08:00."""
    tz = ZoneInfo(OBS_CITIES[city]["tz"])
    t0 = datetime.fromisoformat(date).replace(tzinfo=tz).timestamp()
    rng = {}
    for lo, hi, p in conn.execute("""SELECT bucket_lo, bucket_hi, p FROM price_history WHERE city = ? AND local_date = ?
                                     AND t_utc BETWEEN ? AND ?""", (city, date, t0 - 4 * 3600, t0 + 8 * 3600)).fetchall():
        a = rng.setdefault((lo, hi), [p, p])
        a[0] = min(a[0], p)
        a[1] = max(a[1], p)
    return {k: v[1] - v[0] for k, v in rng.items()}


def mid(b):
    lo, hi = b
    return hi - 0.5 if lo <= -900 else (lo + 0.5 if hi >= 900 else (lo + hi) / 2)


def build():
    days = base.load_days()
    qs_of = {(c, d): (u, json.loads(q)) for c, d, u, q in conn.execute("SELECT city, date, unit, qs FROM ml_preds_var_mkt").fetchall()}
    rows = []
    for dd in days:
        keys, price = dd["keys"], dd["price"]
        city, date = dd["city"], dd["date"]
        unit, qs = qs_of[(city, date)]
        k = 1.8 if unit == "fahrenheit" else 1.0
        tot = sum(price) or 1.0
        pn = [p / tot for p in price]
        order = sorted(range(len(keys)), key=lambda i: -pn[i])
        rank = {i: r for r, i in enumerate(order)}
        fav = order[0]
        v3 = [dd["raw"][b] for b in keys]
        top_v3 = max(range(len(keys)), key=lambda i: v3[i])
        mkt_mean = sum(mid(b) * p for b, p in zip(keys, pn))
        mkt_sd = math.sqrt(sum((mid(b) - mkt_mean) ** 2 * p for b, p in zip(keys, pn)))
        eve = prices_at(city, date, -4)  # 20:00 накануне
        ev_tot = sum(eve.values()) or None
        eve_n = {b: eve[b] / ev_tot for b in eve} if ev_tot else {}
        mean_eve = sum(mid(b) * p for b, p in eve_n.items()) if eve_n else None
        rng = night_range(city, date)
        fav_move = pn[fav] - eve_n.get(keys[fav], pn[fav]) if eve_n else float("nan")
        for i, b in enumerate(keys):
            rows.append({
                "city": city, "date": date, "bucket": b, "won": int(b[0] == dd["win"]), "price": price[i],
                "f": [v3[i], dd["blend"][b], pn[i], price[i], rank[i], i - fav, i - top_v3,
                      int(b[0] <= -900 or b[1] >= 900), mkt_sd / k, pn[fav], (qs[-3] - qs[2]), CITY_ID[city],
                      int(unit == "fahrenheit"), v3[i] - pn[i]],
                "m": [pn[i] - eve_n[b] if b in eve_n else float("nan"), fav_move,
                      ((mkt_mean - mean_eve) / k) if mean_eve is not None else float("nan"),
                      rng.get(b, float("nan"))],
            })
    return rows


F_NAMES = ["шанс v3", "шанс смеси", "цена (норм.)", "цена", "место по цене", "от фаворита", "от вершины v3",
           "крайний", "разброс рынка °C", "шанс фаворита", "ширина v3 °C", "город", "°F", "v3 − рынок"]
M_NAMES = ["движение цены варианта", "движение фаворита", "сдвиг средней рынка °C", "размах за ночь"]


def fit_predict(train, test, with_move):
    X = lambda rs: np.array([r["f"] + (r["m"] if with_move else []) for r in rs], dtype=float)
    y = np.array([r["won"] for r in train])
    params = dict(objective="binary", learning_rate=0.03, num_leaves=15, min_data_in_leaf=50, feature_fraction=0.8,
                  bagging_fraction=0.8, bagging_freq=1, lambda_l2=1.0, verbose=-1)
    model = lgb.train(params, lgb.Dataset(X(train), y, categorical_feature=[11]), 400)
    return model, model.predict(X(test))


def evaluate(test, pred, label):
    groups = {}
    for r, p in zip(test, pred):
        groups.setdefault((r["city"], r["date"]), []).append((r, p))
    ll = {"слой": 0.0, "рынок": 0.0, "v3": 0.0, "смесь": 0.0}
    n = 0
    days = []
    for key, g in groups.items():
        tot = sum(p for _, p in g) or 1.0
        pt = sum(r["f"][2] for r, _ in g) or 1.0
        win = next(((r, p) for r, p in g if r["won"]), None)
        if win is None:
            continue
        r, p = win
        ll["слой"] += -math.log(max(p / tot, 1e-4))
        ll["рынок"] += -math.log(max(r["f"][2] / pt, 1e-4))
        ll["v3"] += -math.log(max(r["f"][0], 1e-4))
        ll["смесь"] += -math.log(max(r["f"][1], 1e-4))
        n += 1
        days.append({"city": key[0], "date": key[1], "keys": [x["bucket"] for x, _ in g], "price": [x["price"] for x, _ in g],
                     "meta": {x["bucket"]: q / tot for x, q in g}, "win": r["bucket"][0]})
    print(f"\n== {label}: оценка шансов (меньше — лучше), {n} город-дней: " +
          ", ".join(f"{k} {v / n:.3f}" for k, v in ll.items()))
    for thr in (0.03, 0.05, 0.10):
        for sides in (("yes",), ("no",)):
            r = base.run_rule(days, "meta", thr, sides)
            print(f"   порог {thr * 100:.0f} п.п., «{'да' if sides == ('yes',) else 'нет'}»: " + base.fmt(r))


if __name__ == "__main__":
    rows = build()
    train = [r for r in rows if r["date"] < "2026-09-01"]
    test = [r for r in rows if r["date"] >= "2026-09-01"]
    print(f"строк (вариантов): обучение {len(train)}, проверка {len(test)}")
    for with_move, label in ((False, "идея 1: второй слой"), (True, "идеи 1+2: второй слой + движение рынка")):
        model, pred = fit_predict(train, test, with_move)
        evaluate(test, pred, label)
        names = F_NAMES + (M_NAMES if with_move else [])
        imp = model.feature_importance("gain")
        top = sorted(zip(names, imp), key=lambda x: -x[1])[:8]
        print("   важнее всего: " + ", ".join(f"{n} {100 * g / imp.sum():.0f}%" for n, g in top))
