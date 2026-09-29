"""
Последние 4 идеи из очереди недельного разбора (2026-09-29, Alex: «проверить, есть ли толк»). Схема как
weather_study_soil.py: учим до месяца — проверяем месяц (август, сентябрь), по 3 обучения, сравниваем средние с v3.
  1. погода на пик дня: облачность 12-16 ч и осадки 12-18 ч из прогноза за сутки (Open-Meteo previous_day1) —
     сейчас у модели дневные значения, а не на время максимума;
  2. вес дней с рынком: строки без цены рынка (до появления маркетов) весят 0.5 / 0.25;
  3. ранняя остановка: число деревьев по отложенным последним 28 дням обучения (до 1500, стоп через 50) —
     потом обучение на всём с этим числом;
  4. края распределения: уровни 0.01 и 0.99 в дополнение к 13 — точнее ли шансы дешёвых вариантов.
Порог (записан до прогона): логошибка модели лучше v3 на ≥ 0.015 в обоих месяцах, деньги смеси в сентябре не хуже.
Только на копии; ~150 вызовов Open-Meteo (кэш /data/research/factors_peak.json).
"""
import json
import time
from datetime import date, timedelta
from pathlib import Path

import lightgbm as lgb
import numpy as np
import requests

import weather_ml as ml
import weather_ml_q as mq
import weather_study_tune as tune
from weather_cities import OBS_CITIES
from weather_edge import CITIES
from weather_study_year import FOLDS, SEEDS

API = "https://previous-runs-api.open-meteo.com/v1/forecast"
CACHE = Path("/data/research/factors_peak.json")
START, END = date(2025, 6, 1), date(2026, 9, 28)
Q0, Z0 = list(mq.QUANTILES), list(mq.Z)


def fetch_peak():
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    for city, cfg in OBS_CITIES.items():
        if city in cache:
            continue
        days, a = {}, START
        try:
            while a <= END:
                b = min(a + timedelta(days=179), END)
                for _ in range(4):
                    r = requests.get(API, params={"latitude": CITIES[city]["lat"], "longitude": CITIES[city]["lon"], "timezone": cfg["tz"],
                                                  "hourly": "cloud_cover_previous_day1,precipitation_previous_day1",
                                                  "start_date": a.isoformat(), "end_date": b.isoformat()}, timeout=120)
                    if r.status_code == 429:
                        time.sleep(60)
                        continue
                    r.raise_for_status()
                    break
                h = r.json()["hourly"]
                for i, t in enumerate(h["time"]):
                    d, hr = t[:10], int(t[11:13])
                    x = days.setdefault(d, {"cc": [], "pr": 0.0})
                    cc, pr = h["cloud_cover_previous_day1"][i], h["precipitation_previous_day1"][i]
                    if 12 <= hr < 16 and cc is not None:
                        x["cc"].append(cc)
                    if 12 <= hr < 18 and pr is not None:
                        x["pr"] += pr
                a = b + timedelta(days=1)
                time.sleep(1)
        except Exception as e:  # noqa: BLE001
            print(f"{city}: ошибка — {e}", flush=True)
            continue
        cache[city] = {d: [float(np.mean(v["cc"])) if v["cc"] else None, v["pr"]] for d, v in days.items()}
        CACHE.write_text(json.dumps(cache))
    return cache


def set_q(qs):
    mq.QUANTILES[:] = qs
    mq.Z[:] = [mq._z(q) for q in qs]


def train(tr, seed, weight=None, rounds=300, early=False):
    X, y = tr[ml.FEATURES], tr["actual_c"] - tr["fc_mean"]
    p = {**mq.Q_PARAMS, "seed": seed, "bagging_seed": seed, "feature_fraction_seed": seed, "num_threads": 4}
    out = {}
    for q in mq.QUANTILES:
        n = rounds
        if early:
            cut = (date.fromisoformat(tr["date"].max()) - timedelta(days=28)).isoformat()
            a, v = tr["date"] < cut, tr["date"] >= cut
            m = lgb.train({**p, "alpha": q}, lgb.Dataset(X[a], y[a], categorical_feature=["city_id"]), 1500,
                          valid_sets=[lgb.Dataset(X[v], y[v], categorical_feature=["city_id"])],
                          callbacks=[lgb.early_stopping(50, verbose=False)])
            n = max(m.best_iteration, 50)
        out[q] = lgb.train({**p, "alpha": q}, lgb.Dataset(X, y, weight=weight, categorical_feature=["city_id"]), n)
    return out


def run(name, df, feats, **kw):
    ml.FEATURES = feats
    line = f"{name:34s}"
    res = {}
    for label, start, end in FOLDS:
        tr = df[df["date"] < start]
        te = df[(df["date"] >= start) & (df["date"] < end)]
        w = None
        if kw.get("w_nomkt") is not None:
            w = np.where(tr["mkt_mean_vs_fc"].notna(), 1.0, kw["w_nomkt"])
        sc = [tune.score(train(tr, s, w, early=kw.get("early", False)), te) for s in SEEDS]
        res[label] = np.mean([s["ll"] for s in sc])
        line += f" | {label}: модель {res[label]:.4f} смесь {np.mean([s['blend'] for s in sc]):.4f}"
        if label == "сентябрь":
            line += f" | деньги смеси сент. {np.mean([tune.base.run_rule(s['days'], 'blend', 0.03)['pnl_real'] for s in sc]):+.1f}$"
    print(line, flush=True)
    return res


if __name__ == "__main__":
    peak = fetch_peak()
    ml.USE_MKT = True
    df = ml.build(tune.conn)
    df = df[(df["actual_c"].notna()) & (df["date"] >= "2025-06-01")].copy()
    g = lambda i: [(peak.get(c, {}).get(d) or [None, None])[i] for c, d in zip(df["city"], df["date"])]
    df["cc_peak"] = np.array([np.nan if v is None else v for v in g(0)], float)
    df["pr_peak"] = np.array([np.nan if v is None else v for v in g(1)], float)
    base_f = [f for f in ml.features(df) if f not in ("cc_peak", "pr_peak")]
    print(f"данных {len(df)}; облачность на пик есть в {df['cc_peak'].notna().mean():.0%}, строк с рынком {df['mkt_mean_vs_fc'].notna().mean():.0%}", flush=True)
    b = run("v3 как сейчас", df, base_f)
    out = {"1. погода на пик дня": run("1. погода на пик дня", df, base_f + ["cc_peak", "pr_peak"]),
           "2. вес дней без рынка 0.5": run("2. вес дней без рынка 0.5", df, base_f, w_nomkt=0.5),
           "2. вес дней без рынка 0.25": run("2. вес дней без рынка 0.25", df, base_f, w_nomkt=0.25),
           "3. ранняя остановка": run("3. ранняя остановка", df, base_f, early=True)}
    set_q(sorted(Q0 + [0.01, 0.99]))
    out["4. края 0.01/0.99"] = run("4. края 0.01/0.99", df, base_f)
    set_q(Q0)
    print("\nпротив v3 (минус = лучше; порог −0.015 в обоих месяцах):")
    for k, r in out.items():
        d = {m: r[m] - b[m] for m in b}
        ok = all(v <= -0.015 for v in d.values())
        print(f"  {k:30s} август {d['август']:+.4f}, сентябрь {d['сентябрь']:+.4f} → {'ПРОШЛО' if ok else 'нет'}")
