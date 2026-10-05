"""
Живой прогноз обучаемой модели (weather_ml.py) — пятый виртуальный
кошелёк "Обучаемая модель", 2026-09-25.

- python weather_ml_live.py --train — переобучение на ВСЕЙ истории
  (крон, раз в сутки ночью): модель + неопределённость по городам
  сохраняются в data/ml/. Каждый день модель учится на всех днях,
  что накопились к этому моменту.
- bucket_probs(...) — вызывается из weather_edge.py в каждом снимке с
  08:00 до 11:59 местного: строит признаки ТОЙ ЖЕ функцией, что и при
  обучении (weather_ml.row_for), по данным, известным к 07:50 местного
  (свежие METAR с aviationweather — архив Iowa Mesonet для утра ещё не
  готов), и возвращает вероятности бакетов.

Проверка на истории (walk-forward, реальные сделки): см. CLAUDE.md —
лучше микса по точности (0.71 против 0.78°C), в плюсе во всех трёх
месяцах, но плюс держится на немногих выигрышах по дешёвым вариантам и
статистически не доказан. Кошелёк — живая проверка.
"""

import json
import time
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import lightgbm as lgb
import numpy as np
import pandas as pd

import weather_ml as ml
from weather_cities import OBS_CITIES

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
ML_DIR = DB_PATH.parent.parent / "ml"
HPA_PER_INHG = 33.8639
_cache = {}


def train_and_save():
    started = datetime.now(timezone.utc)
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    df = ml.build(conn)
    df = df[df["actual_c"].notna()]
    ml.FEATURES = ml.features(df)
    timing = {}  # 30.09: сколько училась каждая версия (страница модели «Как модель училась»)
    t0 = time.time()
    model = ml.train(df)
    sig, glob = ml.city_sigmas(df)
    n_feat_v1 = len(ml.FEATURES)
    ML_DIR.mkdir(parents=True, exist_ok=True)
    model.save_model(str(ML_DIR / "model.txt"))
    (ML_DIR / "meta.json").write_text(json.dumps({
        "features": ml.FEATURES, "sigmas": sig, "sigma_global": glob,
        "trained_at": datetime.now(timezone.utc).isoformat(), "n_rows": len(df)}, ensure_ascii=False))
    print(f"обучено на {len(df)} днях, признаков {len(ml.FEATURES)}, sigma по умолчанию {glob:.2f}°C")
    timing["ml"] = time.time() - t0
    t0 = time.time()
    # версия 2 — распределение (квантили), weather_ml_q.py; отдельный кошелёк ml2
    import weather_ml_q as mq
    qmodels = mq.train_q(df)
    (ML_DIR / "q").mkdir(parents=True, exist_ok=True)
    for q, m in qmodels.items():
        m.save_model(str(ML_DIR / "q" / f"q{int(round(q * 100)):02d}.txt"))
    print(f"версия 2: обучено {len(qmodels)} уровней распределения")
    timing["ml2"] = time.time() - t0
    # версия 3 = v2 + мнение рынка в 08:00 (weather_ml.USE_MKT) — отдельный кошелёк ml3
    ml.USE_MKT = True
    try:
        dfm = ml.build(conn)
        dfm = dfm[dfm["actual_c"].notna()]
        ml.FEATURES = ml.features(dfm)
        t0 = time.time()
        qm = mq.train_q(dfm)
        (ML_DIR / "q_mkt").mkdir(parents=True, exist_ok=True)
        for q, m in qm.items():
            m.save_model(str(ML_DIR / "q_mkt" / f"q{int(round(q * 100)):02d}.txt"))
        (ML_DIR / "q_mkt" / "features.json").write_text(json.dumps(ml.FEATURES))
        print(f"версия 3 (+рынок): обучено {len(qm)} уровней, признаков {len(ml.FEATURES)}")
        timing["ml3"] = time.time() - t0
        t0 = time.time()
        train_v4(dfm)
        timing["ml4"] = time.time() - t0
        t0 = time.time()
        train_v4e(dfm)
        timing["ml4e"] = time.time() - t0
        t0 = time.time()
        try:  # 2026-09-29: v5 «от рынка» — своя ошибка не должна ломать ночное обучение v1-v4
            train_v5(dfm)
            timing["ml5"] = time.time() - t0
        except Exception as e:
            print(f"версия 5 (от рынка): ошибка обучения — {e}", file=sys.stderr)
        # 2026-09-26: подробный отчёт + экзамен для страницы /training
        import weather_ml_report
        rep = weather_ml_report.report(conn, started, df, dfm, sig, qm)
        rep["versions"][0]["features"] = rep["versions"][1]["features"] = n_feat_v1
        try:  # 30.09: подробности обучения каждой версии — не должны ломать отчёт
            rep["version_detail"] = weather_ml_report.version_detail(timing, len(df), len(dfm), n_feat_v1, len(ml.FEATURES), sig, glob)
        except Exception as e:  # noqa: BLE001
            print(f"подробности версий не записаны: {type(e).__name__}: {e}", file=sys.stderr)
        conn.execute("UPDATE ml_train_log SET details = ? WHERE trained_at = (SELECT MAX(trained_at) FROM ml_train_log)",
                     (json.dumps(rep, ensure_ascii=False),))
        conn.commit()
    finally:
        ml.USE_MKT = False
    from jobmark import mark
    mark(conn, "weather_ml_train")
    conn.close()


# 2026-09-26: смесь главной модели с рынком (кошелёк ml3_cal). На проверке вслепую
# v3 честна по всем вариантам, но в ставках самоуверенна: ставка выбирает вариант,
# где модель сильнее всего спорит с рынком, — там чаще её ошибки. Смесь 35% v3 +
# 65% рынка (вес подобран на июле-августе по логошибке) лучше и модели, и рынка:
# логошибка июль-авг 1.234 (v3 1.302, рынок 1.248), сентябрь 1.136 (1.182 / 1.147),
# а в ставках угадывает ровно столько, сколько обещает.
ML3_BLEND_W = 0.35


def blend_with_market(model_probs, market_prices, w=ML3_BLEND_W):
    tot = sum(market_prices) or 1.0
    mix = [w * p + (1 - w) * m / tot for p, m in zip(model_probs, market_prices)]
    s = sum(mix) or 1.0
    return [x / s for x in mix]


# 2026-09-26: v4 = v3 с 31 листом (идея 3, решение Alex). Проверка по месяцам (учим до
# месяца, проверяем месяц), среднее по 3 обучениям: август 1.2732 против 1.2788 у v3,
# сентябрь 1.1718 против 1.1829; смесь с рынком 1.2021/1.1271 против 1.2053/1.1305.
# Обучается рядом с v3 (папка q_mkt31), свои кошельки ml4 / ml4_cal.
V4_EXTRA = {"num_leaves": 31}


def train_v4(dfm):
    """dfm — данные v3 (с признаками рынка); ml.FEATURES уже выставлены под v3."""
    import lightgbm as lgb_
    import weather_ml_q as mq
    X, y = dfm[ml.FEATURES], dfm["actual_c"] - dfm["fc_mean"]
    out = ML_DIR / "q_mkt31"
    out.mkdir(parents=True, exist_ok=True)
    for q in mq.QUANTILES:
        m = lgb_.train({**mq.Q_PARAMS, **V4_EXTRA, "alpha": q}, lgb_.Dataset(X, y, categorical_feature=["city_id"]), mq.Q_ROUNDS)
        m.save_model(str(out / f"q{int(round(q * 100)):02d}.txt"))
    (out / "features.json").write_text(json.dumps(ml.FEATURES))
    print(f"версия 4 (v3, 31 лист): обучено {len(mq.QUANTILES)} уровней")


# 2026-09-27: v4e = v4, усреднённая по 3 обучениям с разными зёрнами (пункт 2, решение Alex —
# отдельные кошельки ml4e / ml4e_cal). Проверка (weather_study_year.py, учим до месяца,
# проверяем месяц): среднее 3 обучений точнее одного во всех 8 сравнениях; v4 август
# 1.2639 против 1.2719, сентябрь 1.1636 против 1.1672; деньги смеси в сентябре по настоящим
# сделкам +$104 против +$80. Квантили каждой модели сортируются, потом усредняются.
V4E_SEEDS = (11, 22, 33)


def train_v4e(dfm):
    import lightgbm as lgb_
    import weather_ml_q as mq
    X, y = dfm[ml.FEATURES], dfm["actual_c"] - dfm["fc_mean"]
    for seed in V4E_SEEDS:
        out = ML_DIR / f"q_mkt31_s{seed}"
        out.mkdir(parents=True, exist_ok=True)
        ex = {**V4_EXTRA, "seed": seed, "bagging_seed": seed, "feature_fraction_seed": seed}
        for q in mq.QUANTILES:
            m = lgb_.train({**mq.Q_PARAMS, **ex, "alpha": q}, lgb_.Dataset(X, y, categorical_feature=["city_id"]), mq.Q_ROUNDS)
            m.save_model(str(out / f"q{int(round(q * 100)):02d}.txt"))
        (out / "features.json").write_text(json.dumps(ml.FEATURES))
    print(f"версия 4e (v4, среднее {len(V4E_SEEDS)} обучений): обучено {len(V4E_SEEDS)} × {len(mq.QUANTILES)} уровней")


# 2026-09-29 (решение Alex: «сократить отставание от рынка, потом опережать»): v5 «от рынка» — отправная точка не
# среднее 16 погодных моделей, а ожидаемый максимум по ценам рынка в 08:00 (fc_mean + mkt_mean_vs_fc); модель учит
# только поправку к рынку — где и насколько рынок ошибается. Признаки — как у v3, среднее 3 обучений.
# Проверка (weather_study_train3.py, 8 недель, 3 обучения): логошибка 1.1843 против 1.2150 у v3 (−0.031, лучше
# все 8 недель, почти вровень с рынком 1.178); деньги смеси в сентябре по сделкам хуже (+$39 против +$124).
# Кошелёк ml5_cal; главное — следить за отставанием от рынка (weather_week_review.py).
V5_SEEDS = (11, 22, 33)


def v5_base(fc_mean, mkt_mean_vs_fc):
    """Отправная точка v5, °C: ожидаемый максимум рынка; нет цены — среднее погодных моделей."""
    return fc_mean + (0.0 if mkt_mean_vs_fc is None or mkt_mean_vs_fc != mkt_mean_vs_fc else mkt_mean_vs_fc)


def train_v5(dfm):
    import lightgbm as lgb_
    import weather_ml_q as mq
    base = dfm["fc_mean"] + (dfm["mkt_mean_vs_fc"].fillna(0.0) if "mkt_mean_vs_fc" in dfm else 0.0)
    X, y = dfm[ml.FEATURES], dfm["actual_c"] - base
    for seed in V5_SEEDS:
        out = ML_DIR / f"q_fm_s{seed}"
        out.mkdir(parents=True, exist_ok=True)
        ex = {"seed": seed, "bagging_seed": seed, "feature_fraction_seed": seed}
        for q in mq.QUANTILES:
            m = lgb_.train({**mq.Q_PARAMS, **ex, "alpha": q}, lgb_.Dataset(X, y, categorical_feature=["city_id"]), mq.Q_ROUNDS)
            m.save_model(str(out / f"q{int(round(q * 100)):02d}.txt"))
        (out / "features.json").write_text(json.dumps(ml.FEATURES))
    print(f"версия 5 (от рынка, среднее {len(V5_SEEDS)} обучений): обучено {len(V5_SEEDS)} × {len(mq.QUANTILES)} уровней")


def _load_q_fm():
    if "q_fm" not in _cache:
        import weather_ml_q as mq
        _cache["q_fm"] = [({q: lgb.Booster(model_file=str(ML_DIR / f"q_fm_s{s}" / f"q{int(round(q * 100)):02d}.txt"))
                            for q in mq.QUANTILES}, json.loads((ML_DIR / f"q_fm_s{s}" / "features.json").read_text()))
                          for s in V5_SEEDS]
    return _cache["q_fm"]


def _load_q_mkt31e():
    if "q_mkt31e" not in _cache:
        import weather_ml_q as mq
        _cache["q_mkt31e"] = [({q: lgb.Booster(model_file=str(ML_DIR / f"q_mkt31_s{s}" / f"q{int(round(q * 100)):02d}.txt"))
                                for q in mq.QUANTILES}, json.loads((ML_DIR / f"q_mkt31_s{s}" / "features.json").read_text()))
                              for s in V4E_SEEDS]
    return _cache["q_mkt31e"]


def _load_q_mkt31():
    if "q_mkt31" not in _cache:
        import weather_ml_q as mq
        _cache["q_mkt31"] = ({q: lgb.Booster(model_file=str(ML_DIR / "q_mkt31" / f"q{int(round(q * 100)):02d}.txt")) for q in mq.QUANTILES},
                             json.loads((ML_DIR / "q_mkt31" / "features.json").read_text()))
    return _cache["q_mkt31"]


def _load_q():
    if "q" not in _cache:
        import weather_ml_q as mq
        _cache["q"] = {q: lgb.Booster(model_file=str(ML_DIR / "q" / f"q{int(round(q * 100)):02d}.txt")) for q in mq.QUANTILES}
    return _cache["q"]


def _load_q_mkt():
    if "q_mkt" not in _cache:
        import weather_ml_q as mq
        _cache["q_mkt"] = ({q: lgb.Booster(model_file=str(ML_DIR / "q_mkt" / f"q{int(round(q * 100)):02d}.txt")) for q in mq.QUANTILES},
                           json.loads((ML_DIR / "q_mkt" / "features.json").read_text()))
    return _cache["q_mkt"]


def _load():
    if "model" not in _cache:
        meta = json.loads((ML_DIR / "meta.json").read_text())
        _cache["model"] = lgb.Booster(model_file=str(ML_DIR / "model.txt"))
        _cache["meta"] = meta
    return _cache["model"], _cache["meta"]


def _metar_obs(m):
    """METAR из aviationweather JSON -> кортеж в формате station_obs для row_for."""
    wdir = m.get("wdir")
    cloud = (m.get("clouds") or [{}])[0].get("cover") if m.get("clouds") else "CLR"
    return (datetime.fromtimestamp(m["obsTime"], timezone.utc), m.get("temp"), m.get("dewp"),
            (m["altim"] / HPA_PER_INHG) if m.get("altim") else None,
            wdir if isinstance(wdir, (int, float)) else None, m.get("wspd"), cloud)


def bucket_probs(conn, city, cfg, buckets, metars):
    """Вероятности бакетов на сегодня или None (рано / нет данных / нет модели)."""
    if city not in OBS_CITIES or not (ML_DIR / "model.txt").exists():
        return None
    tz = ZoneInfo(cfg["tz"])
    now_local = datetime.now(tz)
    if not (ml.DECISION_HOUR <= now_local.hour < 12):
        return None
    model, meta = _load()
    ocfg = {**OBS_CITIES[city], "lat": cfg["lat"], "lon": cfg["lon"]}
    unit = ocfg["unit"]
    d = now_local.date().isoformat()
    prev = (now_local.date() - timedelta(days=1)).isoformat()
    fc = {}
    for dd, mdl, v in conn.execute(
            "SELECT local_date, model, fcst_max FROM mm_forecasts WHERE city = ? AND lead = 'day1' AND local_date IN (?, ?)",
            (city, d, prev)):
        fc.setdefault(dd, {})[mdl] = ml.to_c(v, unit)
    if d not in fc or len(fc[d]) < 3:
        return None
    fv = {var: v for var, v in conn.execute("SELECT var, value FROM ml_fcst_vars WHERE city = ? AND local_date = ?", (city, d))}
    t_dec = datetime.fromisoformat(d).replace(tzinfo=tz) + timedelta(hours=ml.DECISION_HOUR)
    obs = sorted(_metar_obs(m) for m in metars if m.get("obsTime") and m.get("temp") is not None)
    known = [o for o in obs if o[0].astimezone(tz).date().isoformat() == d
             and o[0] + timedelta(minutes=ml.OBS_DELAY_MIN) <= t_dec]
    actual = dict(conn.execute("SELECT local_date, actual_max FROM weather_station_daily WHERE city = ? AND local_date = ?", (city, prev)))
    if prev not in actual:
        # вчерашний максимум ещё не в архиве — считаем по свежим METAR за вчера
        prev_vals = [o[1] for o in obs if o[0].astimezone(tz).date().isoformat() == prev]
        if prev_vals:
            mx = max(prev_vals)
            actual[prev] = round(mx * 9 / 5 + 32) if unit == "fahrenheit" else round(mx)
    ci = list(OBS_CITIES).index(city)
    row = ml.row_for(conn, city, ci, ocfg, d, fc, fv, known, actual)
    X = pd.DataFrame([row]).reindex(columns=meta["features"])
    mu_c = row["fc_mean"] + float(model.predict(X)[0])
    sigma_c = meta["sigmas"].get(city, meta["sigma_global"])
    k, off = (9 / 5, 32) if unit == "fahrenheit" else (1, 0)
    from weather_edge import emos_bucket_prob
    v1 = [emos_bucket_prob(mu_c * k + off, sigma_c * k, b["lo"], b["hi"]) for b in buckets]
    v2 = None
    if (ML_DIR / "q").exists():
        import weather_ml_q as mq
        qs = mq.predict_q(_load_q(), X, [row["fc_mean"]])[0]
        v2 = [mq.bucket_prob(list(qs), unit, b["lo"], b["hi"]) for b in buckets]
    v3 = None
    if (ML_DIR / "q_mkt" / "features.json").exists():
        import weather_ml_q as mq
        models, feats = _load_q_mkt()
        prices = {(b["lo"], b["hi"]): b["market_p"] for b in buckets}
        row3 = {**row, **ml.mkt_features(prices, unit, row["fc_mean"])}
        qs3 = mq.predict_q(models, pd.DataFrame([row3]).reindex(columns=feats), [row["fc_mean"]])[0]
        v3 = [mq.bucket_prob(list(qs3), unit, b["lo"], b["hi"]) for b in buckets]
    v4 = None
    if (ML_DIR / "q_mkt31" / "features.json").exists():
        import weather_ml_q as mq
        models, feats = _load_q_mkt31()
        prices = {(b["lo"], b["hi"]): b["market_p"] for b in buckets}
        row4 = {**row, **ml.mkt_features(prices, unit, row["fc_mean"])}
        qs4 = mq.predict_q(models, pd.DataFrame([row4]).reindex(columns=feats), [row["fc_mean"]])[0]
        v4 = [mq.bucket_prob(list(qs4), unit, b["lo"], b["hi"]) for b in buckets]
    v4e = None
    if all((ML_DIR / f"q_mkt31_s{s}" / "features.json").exists() for s in V4E_SEEDS):
        import weather_ml_q as mq
        prices = {(b["lo"], b["hi"]): b["market_p"] for b in buckets}
        row4 = {**row, **ml.mkt_features(prices, unit, row["fc_mean"])}
        qs = np.mean([mq.predict_q(m, pd.DataFrame([row4]).reindex(columns=f), [row["fc_mean"]])[0]
                      for m, f in _load_q_mkt31e()], axis=0)
        v4e = [mq.bucket_prob(list(qs), unit, b["lo"], b["hi"]) for b in buckets]
    v5 = None
    if all((ML_DIR / f"q_fm_s{s}" / "features.json").exists() for s in V5_SEEDS):
        try:  # отдельный трек: ошибка v5 не ломает v1-v4e
            import weather_ml_q as mq
            prices = {(b["lo"], b["hi"]): b["market_p"] for b in buckets}
            mf = ml.mkt_features(prices, unit, row["fc_mean"])
            row5 = {**row, **mf}
            b5 = v5_base(row["fc_mean"], mf.get("mkt_mean_vs_fc"))
            qs = np.mean([mq.predict_q(m, pd.DataFrame([row5]).reindex(columns=f), [b5])[0]
                          for m, f in _load_q_fm()], axis=0)
            v5 = [mq.bucket_prob(list(qs), unit, b["lo"], b["hi"]) for b in buckets]
        except Exception as e:
            print(f"{city}: ошибка v5 — {e}", file=sys.stderr)
    return v1, mu_c * k + off, v2, v3, v4, v4e, v5


if __name__ == "__main__":
    if "--train-v5" in sys.argv:
        # только v5 (не трогая рабочие v1-v4e) — разовый запуск при вводе v5
        _conn = sqlite3.connect(DB_PATH, timeout=60)
        _conn.row_factory = sqlite3.Row
        ml.USE_MKT = True
        _dfm = ml.build(_conn)
        _dfm = _dfm[_dfm["actual_c"].notna()]
        ml.FEATURES = ml.features(_dfm)
        _conn.close()
        train_v5(_dfm)
        ml.USE_MKT = False
        sys.exit()
    if "--train-v4e" in sys.argv:
        # только v4e (не трогая рабочие v1-v4) — разовый запуск при вводе v4e
        _conn = sqlite3.connect(DB_PATH, timeout=60)
        _conn.row_factory = sqlite3.Row
        ml.USE_MKT = True
        _dfm = ml.build(_conn)
        _dfm = _dfm[_dfm["actual_c"].notna()]
        ml.FEATURES = ml.features(_dfm)
        train_v4e(_dfm)
        ml.USE_MKT = False
        _conn.close()
        sys.exit()
    if "--train-v4" in sys.argv:
        # только v4 (не трогая рабочие v1-v3) — разовый запуск при вводе v4
        _conn = sqlite3.connect(DB_PATH, timeout=60)
        _conn.row_factory = sqlite3.Row
        ml.USE_MKT = True
        _dfm = ml.build(_conn)
        _dfm = _dfm[_dfm["actual_c"].notna()]
        ml.FEATURES = ml.features(_dfm)
        train_v4(_dfm)
        ml.USE_MKT = False
        _conn.close()
        sys.exit()
    if "--train" in sys.argv:
        train_and_save()
