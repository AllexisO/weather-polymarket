"""
Обучаемая модель прогноза дневного максимума (2026-09-25, идея Alex:
"давать модели данные — ветер, влажность, облачность, радар — и чтобы
она училась и прогноз становился всё точнее").

Модель — градиентный бустинг LightGBM (стандарт для табличных данных;
LLM тут подключается позже — читать текстовые разборы прогноза). Одна
модель на все 48 городов (температуры приведены к °C), учится на том,
на сколько РЕАЛЬНЫЙ максимум отличается от среднего прогноза 16 моделей
(поправка к прогнозу — классический подход метеослужб, MOS).

Что модель знает на момент решения (08:00 местного, ничего из будущего):
- прогнозы 16 моделей за сутки (mm_forecasts day1), их среднее и разброс,
  прогноз микса (walk-forward, weather_multimodel);
- прогнозные условия на 11-17 ч: облачность, солнце, влажность, точка
  росы, ветер, осадки (ml_fcst_vars, ECMWF за сутки);
- утренние замеры станции, известные к 07:50: температура, точка росы,
  минимум за ночь, изменение за 3 ч, ветер, облачность, давление и его
  изменение за 3 ч;
- вчерашний реальный максимум и вчерашняя ошибка прогнозов;
- сезон (день года), координаты, город.

Проверка — walk-forward: каждую неделю модель переобучается на всех
днях ДО этой недели и прогнозирует эту неделю. Неопределённость (sigma)
— по ошибкам на кросс-валидации внутри обучающих данных, по городу.

Итог сравниваем с миксом и с рынком: точность (ошибка в °C), попадание
в диапазон, ставки по тому же правилу, что у кошельков (перевес от 10
п.п., цена 3-95¢), по свежей цене в 08:00 + спред + комиссия.
Обучение/подбор — июль-август, проверка — сентябрь.

Запуск: python weather_ml.py
"""

import math
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import lightgbm as lgb
import numpy as np
import pandas as pd

import weather_multimodel as mm
from weather_cities import OBS_CITIES
from weather_edge import emos_bucket_prob

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
DECISION_HOUR = 8
OBS_DELAY_MIN = 10
WF_START = date(2026, 7, 1)
TEST_START = "2026-09-01"
SIGMA_FLOOR_C = 0.5
PARAMS = dict(objective="regression", learning_rate=0.05, num_leaves=15, min_data_in_leaf=30,
              feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=1, lambda_l2=1.0, verbose=-1)
N_ROUNDS = 300
CLOUD = {"CLR": 0, "SKC": 0, "NCD": 0, "NSC": 0, "CAVOK": 0, "FEW": 1, "SCT": 2, "BKN": 3, "OVC": 4, "VV": 4}


def to_c(x, unit):
    return None if x is None else ((x - 32) * 5 / 9 if unit == "fahrenheit" else x)


def f_to_c(f):
    return None if f is None else (f - 32) * 5 / 9


AFD_GUIDANCE = {"warmer": 1, "same": 0, "cooler": -1}
AFD_CONF = {"low": 0, "medium": 1, "high": 2}
# 2026-09-25: признаки из текстовых разборов метеорологов NOAA (weather_afd.py,
# только 11 городов США). По умолчанию ВЫКЛЮЧЕНЫ: сначала проверка
# (python weather_ml.py --afd сравнивает с/без на городах США), живой
# кошелёк не меняем, пока не доказано, что помогает.
USE_AFD = False
# 2026-09-25: пункты 3-4 плана Alex — по умолчанию выключены до проверки
# (weather_ml_variants.py сравнивает с/без по отдельности).
USE_NB = False   # соседние METAR-станции (weather_neighbors.py)
USE_MKT = False  # цена рынка в 08:00 (price_history)
USE_NBM = False  # 2026-09-26: прогноз NBM (служба погоды США) для станций США — weather_nbm.py, идея 4
NB_BASE_DAYS = 30
# 2026-09-26: до этих дат Polymarket резолвил город не по нашей станции METAR
# (итог рынка совпадал с нашим фактом: Шэньчжэнь 03-08.2026 — 15-42%, Париж
# 02-04.2026 — 60-77%, Сеул 12.2025-05.2026 — 72-92%; после — 98-100%).
# Цена рынка там — про другую станцию, поэтому признаки рынка не даём (модель учит без них).
MKT_UNRELIABLE_BEFORE = {"shenzhen": "2026-09-01", "paris": "2026-05-01", "seoul": "2026-06-01"}


def angdiff(a, b):
    return abs((a - b + 180) % 360 - 180)


def nb_features(own_known, nb_by_station, nb_meta, base, d, t_dec):
    """Признаки соседних станций на момент t_dec. own_known — замеры своей
    станции (как в row_for). nb_by_station: {станция: [(utc_dt, t°C), ...]}
    за этот день. base: {станция: [(дата, разница)]} — история разниц для
    "обычной" разницы (только дни ДО d). Возвращает (признаки, сегодняшние
    разницы по станциям — чтобы дописать в base)."""
    if not own_known:
        return {}, {}
    own = own_known[-1]
    own_t = own[1]
    own_prev = [o for o in own_known if o[0] <= own[0] - timedelta(hours=3)]
    own_dt = own_t - own_prev[-1][1] if own_prev else None
    anoms, today, dts = {}, {}, []
    for st, obs in nb_by_station.items():
        known = [o for o in obs if o[0] + timedelta(minutes=OBS_DELAY_MIN) <= t_dec]
        if not known:
            continue
        diff = known[-1][1] - own_t
        today[st] = diff
        hist = [x for dd, x in base.get(st, []) if dd < d][-NB_BASE_DAYS:]
        if len(hist) >= 5:
            anoms[st] = diff - float(np.mean(hist))
        prev = [o for o in known if o[0] <= known[-1][0] - timedelta(hours=3)]
        if prev and own_dt is not None:
            dts.append((known[-1][1] - prev[-1][1]) - own_dt)
    f = {}
    if anoms:
        v = list(anoms.values())
        f.update(nb_anom_mean=float(np.mean(v)), nb_anom_max=max(v), nb_anom_min=min(v), nb_n=len(v))
        wind_from, wind_kt = own[4], own[5]
        if wind_from is not None and wind_kt and wind_kt >= 4:
            cand = [(angdiff(nb_meta[st]["bearing"], wind_from), st) for st in anoms if st in nb_meta]
            cand = [c for c in cand if c[0] <= 60]
            if cand:
                f["nb_upwind_anom"] = anoms[min(cand)[1]]
    if dts:
        f["nb_dt3h_mean"] = float(np.mean(dts))
    return f, today


def mkt_features(prices, unit, mean_fc):
    """Мнение рынка в 08:00: ожидаемый максимум, неуверенность, шанс
    самого популярного варианта. prices: {(lo, hi): цена}."""
    tot = sum(prices.values())
    if tot <= 0 or len(prices) < 3:
        return {}
    to_c = (lambda v: (v - 32) * 5 / 9) if unit == "fahrenheit" else (lambda v: v)
    step = 2 if unit == "fahrenheit" else 1
    mids = {}
    for (lo, hi) in prices:
        if lo <= -900:
            mids[(lo, hi)] = to_c(hi - 0.5 - step / 2)
        elif hi >= 900:
            mids[(lo, hi)] = to_c(lo + 0.5 + step / 2)
        else:
            mids[(lo, hi)] = to_c((lo + hi) / 2)
    m = sum(p * mids[b] for b, p in prices.items()) / tot
    sd = math.sqrt(max(sum(p * (mids[b] - m) ** 2 for b, p in prices.items()) / tot, 0))
    return {"mkt_mean_vs_fc": m - mean_fc, "mkt_std": sd, "mkt_top_p": max(prices.values()) / tot}


def afd_features(r, afd, mean_fc, unit):
    if not afd:
        return
    if afd.get("high_f") is not None:
        r["afd_high_vs_fc"] = (afd["high_f"] - 32) * 5 / 9 - mean_fc
    r["afd_guidance"] = AFD_GUIDANCE.get(afd.get("vs_guidance"), np.nan)
    r["afd_conf"] = AFD_CONF.get(afd.get("confidence"), np.nan)
    for k in ("sea_breeze", "clouds_limit", "rain_today", "front_today"):
        r[f"afd_{k}"] = afd.get(k) if afd.get(k) is not None else np.nan


def row_for(conn, city, ci, cfg, d, fc, fv, known, actual, afd=None, extra=None):
    """Одна строка признаков на город и день — ОДНА И ТА ЖЕ функция для
    обучения (build) и живого прогноза (weather_ml_live), чтобы признаки
    считались одинаково.
    fc: {дата: {модель: °C}} (прогнозы за сутки), fv: {переменная: значение}
    на этот день, known: замеры, известные к 07:50 местного — список
    (utc_dt, t°C, точка росы °C, давление inHg, направление ветра, скорость
    узлы, облачность), actual: {дата: максимум в единицах города}."""
    unit = cfg["unit"]
    vals = list(fc[d].values())
    mean_fc = float(np.mean(vals))
    doy = date.fromisoformat(d).timetuple().tm_yday
    r = {"city": city, "city_id": ci, "date": d, "unit": unit, "lat": cfg["lat"], "lon": cfg["lon"],
         "doy_sin": math.sin(2 * math.pi * doy / 365), "doy_cos": math.cos(2 * math.pi * doy / 365),
         "fc_mean": mean_fc, "fc_std": float(np.std(vals)), "fc_min": min(vals), "fc_max": max(vals),
         "actual_c": to_c(actual[d], unit) if d in actual else np.nan}
    for m in mm.MODELS:
        r[f"fc_{m}"] = fc[d][m] - mean_fc if m in fc[d] else np.nan
    for k, v in fv.items():
        r[f"fv_{k}"] = v
    if known:
        last = known[-1]
        r["obs_t"] = last[1]
        r["obs_dew"] = last[2]
        r["obs_spread"] = last[1] - last[2] if last[2] is not None else np.nan
        r["obs_tmin"] = min(o[1] for o in known)
        r["obs_vs_fc"] = last[1] - mean_fc
        r["obs_alti"] = last[3]
        r["obs_wind"] = last[5]
        if last[4] is not None and last[5] is not None:
            r["obs_wind_sin"] = math.sin(math.radians(last[4])) * last[5]
            r["obs_wind_cos"] = math.cos(math.radians(last[4])) * last[5]
        r["obs_cloud"] = CLOUD.get((last[6] or "").strip(), np.nan)
        earlier = [o for o in known if o[0] <= last[0] - timedelta(hours=3)]
        if earlier:
            r["obs_dt3h"] = last[1] - earlier[-1][1]
            if last[3] and earlier[-1][3]:
                r["obs_dalti3h"] = last[3] - earlier[-1][3]
    prev = (date.fromisoformat(d) - timedelta(days=1)).isoformat()
    if prev in actual:
        r["prev_actual_vs_fc"] = to_c(actual[prev], unit) - mean_fc
        if prev in fc:
            r["prev_err"] = float(np.mean(list(fc[prev].values()))) - to_c(actual[prev], unit)
    params = mm.fit(conn, city, unit, d)
    mu = mm.predict(params, {m: (v * 9 / 5 + 32 if unit == "fahrenheit" else v) for m, v in fc[d].items()}) if params else None
    r["mix_mu_c"] = to_c(mu, unit) if mu is not None else np.nan
    r["mix_sigma_c"] = (params["sigma"] * (5 / 9 if unit == "fahrenheit" else 1)) if params else np.nan
    r["mix_vs_fc"] = r["mix_mu_c"] - mean_fc if mu is not None else np.nan
    if USE_AFD:
        afd_features(r, afd, mean_fc, unit)
    if extra:
        # extra — готовые признаки соседей/рынка; mkt считается от среднего прогноза
        for k, v in extra.items():
            r[k] = v(mean_fc) if callable(v) else v
    return r


def build(conn):
    rows = []
    for ci, (city, cfg) in enumerate(OBS_CITIES.items()):
        unit, tz = cfg["unit"], ZoneInfo(cfg["tz"])
        actual = dict(conn.execute("SELECT local_date, actual_max FROM weather_station_daily WHERE city = ?", (city,)))
        fc = {}
        for d, m, v in conn.execute("SELECT local_date, model, fcst_max FROM mm_forecasts WHERE city = ? AND lead = 'day1'", (city,)):
            fc.setdefault(d, {})[m] = to_c(v, unit)
        fv = {}
        for d, var, v in conn.execute("SELECT local_date, var, value FROM ml_fcst_vars WHERE city = ?", (city,)):
            fv.setdefault(d, {})[var] = v
        obs = {}
        for v, t, dw, al, dr, sk, sky in conn.execute(
                "SELECT valid_utc, tmpf, dwpf, alti, drct, sknt, skyc1 FROM station_obs WHERE city = ? AND tmpf IS NOT NULL", (city,)):
            dt = datetime.fromisoformat(v).replace(tzinfo=timezone.utc)
            obs.setdefault(dt.astimezone(tz).date().isoformat(), []).append((dt, f_to_c(t), f_to_c(dw), al, dr, sk, sky))
        afd = {}
        if USE_AFD and conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'afd_signals'").fetchone():
            for a in conn.execute("SELECT * FROM afd_signals WHERE city = ?", (city,)):
                afd[a["local_date"]] = {"high_f": a["high_f"], "vs_guidance": a["vs_guidance"], "confidence": a["confidence"],
                                        "sea_breeze": a["sea_breeze"], "clouds_limit": a["clouds_limit"],
                                        "rain_today": a["rain_today"], "front_today": a["front_today"]}
        nb_meta, nb_obs, nb_base = {}, {}, {}
        if USE_NB:
            for st, bearing in conn.execute("SELECT iem, bearing FROM ml_neighbors WHERE city = ?", (city,)):
                nb_meta[st] = {"bearing": bearing}
            for st, v, t in conn.execute(
                    "SELECT station, valid_utc, tmpf FROM station_obs WHERE city = ? AND tmpf IS NOT NULL", (f"nb:{city}",)):
                dt = datetime.fromisoformat(v).replace(tzinfo=timezone.utc)
                nb_obs.setdefault(dt.astimezone(tz).date().isoformat(), {}).setdefault(st, []).append((dt, f_to_c(t)))
        nbm = {}
        if USE_NBM and cfg["icao"].startswith("K"):
            # txn в строке ftime 00Z следующих суток = прогноз дневного максимума (°F); берём последний
            # выпуск, опубликованный (+1 ч) до решения — ничего из будущего
            for rt, ft, txn in conn.execute("SELECT runtime, ftime, txn FROM nbm_forecasts WHERE station = ?", (cfg["icao"],)):
                if ft.endswith("00:00:00"):
                    nbm.setdefault(ft[:10], []).append((datetime.fromisoformat(rt).replace(tzinfo=timezone.utc), txn))
        mkt_prices = {}
        if USE_MKT:
            for dd, lo, hi, t, p in conn.execute(
                    "SELECT local_date, bucket_lo, bucket_hi, t_utc, p FROM price_history WHERE city = ? ORDER BY t_utc", (city,)):
                ts = datetime.fromisoformat(dd).replace(tzinfo=tz).timestamp() + DECISION_HOUR * 3600
                if ts - 3600 <= t <= ts:
                    mkt_prices.setdefault(dd, {})[(lo, hi)] = p
        for d in sorted(actual):
            if d not in fc or len(fc[d]) < 3:
                continue
            t_dec = datetime.fromisoformat(d).replace(tzinfo=tz) + timedelta(hours=DECISION_HOUR)
            known = sorted(o for o in obs.get(d, []) if o[0] + timedelta(minutes=OBS_DELAY_MIN) <= t_dec)
            extra = {}
            if USE_NB:
                day_nb = {st: sorted(v) for st, v in nb_obs.get(d, {}).items()}
                f, today = nb_features(known, day_nb, nb_meta, nb_base, d, t_dec)
                extra.update(f)
                for st, diff in today.items():
                    nb_base.setdefault(st, []).append((d, diff))
            if USE_MKT and d in mkt_prices and d >= MKT_UNRELIABLE_BEFORE.get(city, ""):
                pr = mkt_prices[d]
                extra["__mkt"] = pr
            row = row_for(conn, city, ci, cfg, d, fc, fv.get(d, {}), known, actual, afd.get(d),
                          {k: v for k, v in extra.items() if not k.startswith("__")})
            if USE_NBM:
                nxt = (datetime.fromisoformat(d) + timedelta(days=1)).date().isoformat()
                ok = [(rt, v) for rt, v in nbm.get(nxt, []) if rt + timedelta(hours=1) <= t_dec.astimezone(timezone.utc)]
                row["nbm_vs_fc"] = (f_to_c(max(ok)[1]) - row["fc_mean"]) if ok else float("nan")
            if "__mkt" in extra:
                row.update(mkt_features(extra["__mkt"], unit, row["fc_mean"]))
            rows.append(row)
    return pd.DataFrame(rows)


FEATURES = None


def features(df):
    drop = {"city", "date", "unit", "actual_c", "mix_mu_c", "mix_sigma_c"}
    return [c for c in df.columns if c not in drop]


def train(df_train):
    X, y = df_train[FEATURES], df_train["actual_c"] - df_train["fc_mean"]
    return lgb.train(PARAMS, lgb.Dataset(X, y, categorical_feature=["city_id"]), N_ROUNDS)


def city_sigmas(df_train):
    """Честная неопределённость: ошибки на 4-кратной кросс-валидации по
    блокам дат внутри обучающих данных, по каждому городу."""
    dates = sorted(df_train["date"].unique())
    folds = np.array_split(dates, 4)
    res = []
    for f in folds:
        tr = df_train[~df_train["date"].isin(f)]
        te = df_train[df_train["date"].isin(f)]
        if len(tr) < 200 or te.empty:
            continue
        pred = train(tr).predict(te[FEATURES]) + te["fc_mean"]
        res.append(pd.DataFrame({"city": te["city"], "err": te["actual_c"] - pred}))
    res = pd.concat(res)
    glob = max(float(np.sqrt((res["err"] ** 2).mean())), SIGMA_FLOOR_C)
    out = {}
    for city, g in res.groupby("city"):
        out[city] = max(float(np.sqrt((g["err"] ** 2).mean())), SIGMA_FLOOR_C) if len(g) >= 15 else glob
    return out, glob


def walk_forward(df):
    preds = []
    week = WF_START
    last = date.fromisoformat(df["date"].max())
    while week <= last:
        nxt = week + timedelta(days=7)
        tr = df[df["date"] < week.isoformat()]
        te = df[(df["date"] >= week.isoformat()) & (df["date"] < nxt.isoformat())]
        if not te.empty and len(tr) >= 500:
            model = train(tr)
            sig, glob = city_sigmas(tr)
            p = te[["city", "date", "unit", "actual_c", "fc_mean", "mix_mu_c", "mix_sigma_c"]].copy()
            p["ml_mu_c"] = model.predict(te[FEATURES]) + te["fc_mean"]
            p["ml_sigma_c"] = p["city"].map(lambda c: sig.get(c, glob))
            preds.append(p)
            print(f"неделя с {week}: обучено на {len(tr)} днях, прогноз на {len(te)}", flush=True)
        week = nxt
    return pd.concat(preds)


def price_at(conn, city, d, cfg):
    ts = datetime.fromisoformat(d).replace(tzinfo=ZoneInfo(cfg["tz"])).timestamp() + DECISION_HOUR * 3600
    out = {}
    for lo, hi, t, p in conn.execute(
            "SELECT bucket_lo, bucket_hi, t_utc, p FROM price_history WHERE city = ? AND local_date = ? AND t_utc BETWEEN ? AND ? ORDER BY t_utc",
            (city, d, ts - 3600, ts)):
        out[(lo, hi)] = p
    return out


def pnl(price, won):
    buy = price + 0.01
    shares = 5 / (buy + 0.05 * buy * (1 - buy))
    return shares - 5 if won else -5


def evaluate(conn, preds):
    win = {(r[0], r[1]): r[2] for r in conn.execute("SELECT city, local_date, win_lo FROM weather_poly_outcomes")}
    stats = {}
    for _, r in preds.iterrows():
        per = "сентябрь (проверка)" if r["date"] >= TEST_START else "июль-август"
        s = stats.setdefault(per, {k: [] for k in ("ml_err", "mix_err", "fc_err")} | {"hit": {"ml": 0, "mix": 0, "mkt": 0, "n": 0},
                                                                                        "bets": {"ml": [0, 0, 0.0], "mix": [0, 0, 0.0]}})
        s["ml_err"].append(abs(r["ml_mu_c"] - r["actual_c"]))
        s["fc_err"].append(abs(r["fc_mean"] - r["actual_c"]))
        if not np.isnan(r["mix_mu_c"]):
            s["mix_err"].append(abs(r["mix_mu_c"] - r["actual_c"]))
        cfg = OBS_CITIES[r["city"]]
        prices = price_at(conn, r["city"], r["date"], cfg)
        w = win.get((r["city"], r["date"]))
        if len(prices) < 3 or w is None or np.isnan(r["mix_mu_c"]):
            continue
        k = 9 / 5 if r["unit"] == "fahrenheit" else 1
        off = 32 if r["unit"] == "fahrenheit" else 0
        probs = {}
        for name, mu, sg in (("ml", r["ml_mu_c"], r["ml_sigma_c"]), ("mix", r["mix_mu_c"], r["mix_sigma_c"])):
            probs[name] = {b: emos_bucket_prob(mu * k + off, sg * k, b[0], b[1]) for b in prices}
        s["hit"]["n"] += 1
        s["hit"]["mkt"] += max(prices, key=prices.get)[0] == w
        for name in ("ml", "mix"):
            pr = probs[name]
            s["hit"][name] += max(pr, key=pr.get)[0] == w
            b = max(prices, key=lambda x: pr[x] - prices[x])
            if pr[b] - prices[b] >= 0.10 and 0.03 <= prices[b] <= 0.95:
                won = b[0] == w
                s["bets"][name][0] += 1
                s["bets"][name][1] += won
                s["bets"][name][2] += pnl(prices[b], won)
    for per in ("июль-август", "сентябрь (проверка)"):
        s = stats.get(per)
        if not s:
            continue
        h = s["hit"]
        print(f"\n=== {per} ===")
        print(f"средняя ошибка, °C: обучаемая модель {np.mean(s['ml_err']):.2f} | микс {np.mean(s['mix_err']):.2f} | "
              f"простое среднее 16 моделей {np.mean(s['fc_err']):.2f}")
        print(f"угадан диапазон (в 08:00, {h['n']} дней): обучаемая {100*h['ml']/h['n']:.0f}% | микс {100*h['mix']/h['n']:.0f}% | "
              f"рынок {100*h['mkt']/h['n']:.0f}%")
        for name, label in (("ml", "обучаемая"), ("mix", "микс")):
            n, wn, p = s["bets"][name]
            print(f"ставки {label}: {n} ставок, выиграно {wn}, итог {p:+.1f}$ ({p / n if n else 0:+.2f} на ставку)")


def main():
    global FEATURES
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    df = build(conn)
    FEATURES = features(df)
    print(f"строк для обучения: {len(df)}, признаков: {len(FEATURES)}")
    preds = walk_forward(df)
    # сохраняем прогнозы — для быстрых перепроверок (weather_ml_check.py)
    wconn = sqlite3.connect(DB_PATH, timeout=60)
    preds.to_sql("ml_preds_wf", wconn, if_exists="replace", index=False)
    wconn.close()
    evaluate(conn, preds)
    model = train(df[df["date"] < TEST_START])
    imp = sorted(zip(FEATURES, model.feature_importance("gain")), key=lambda x: -x[1])[:12]
    print("\nсамые полезные признаки:", ", ".join(f"{f}" for f, _ in imp))
    conn.close()


if __name__ == "__main__":
    main()
