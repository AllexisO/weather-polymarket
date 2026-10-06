"""
Дневная модель LightGBM (2026-10-06, решение Alex «сделаем все 3», пункт 2). Утренние модели решают только в 08:00 и не видят
день; LLM днём точнее рынка (на 04-05.10 в 14:00 59% против 50% на верный вариант). Здесь — та же идея без LLM: модель в
10:00 / 12:00 / 14:00 местного видит утренние признаки v3 + замеры станции с утра до этого часа + цену рынка в этот час и учит
поправку к рынку (как v5 «от рынка»: отправная точка — ожидаемый максимум по ценам рынка в этот час).
Чем отличается от простого nowcast 23.09 (weather_nowcast.py, не сработал): там расчёт без обучения и без цены рынка.

Признаки к утренним (v3): час решения, максимум с утра, температура сейчас, изменение за 2 ч, сколько замеров —
всё против среднего 16 моделей; рынок в этот час: ожидаемый максимум, неуверенность, шанс лидера; максимум с утра против рынка.
Максимум дня не бывает ниже уже измеренного: уровни распределения поднимаются до максимума с утра.

Проверка — как weather_study_train3.py: 8 недель (с 10.08.2026), учим на всём до недели — проверяем неделю, 3 обучения
(зёрна 11/22/33), прогноз — среднее. Мера — логошибка на выигравшем варианте против рынка В ТОТ ЖЕ ЧАС; смесь — 35% модели
+ 65% рынка (как ml3_cal). Цена рынка — середина/последняя сделка (price_history), не цена покупки.

ПОРОГ (записан 06.10 до первого запуска): смесь точнее рынка в тот же час — логошибка ниже минимум на 0.010 — хотя бы в 2 из
3 часов (10/12/14) → дневная модель идёт отдельным кошельком. Не прошло — в PRD §10 «проверено и не сработало».
Итог первого запуска 06.10: прошло 3 из 3 (смесь −0.016 / −0.016 / −0.011, лучше рынка во всех 8 неделях).

ПРОВЕРКА ДЕНЬГАМИ (добавлена 06.10 после первого запуска — «слишком хорошо, ищи ошибку»: цена в истории — последняя сделка,
днём она может устаревать; порог записан до запуска денежной проверки): одна ставка $2 на город в день — в первый из часов
10/12/14, где у смеси перевес ≥ 3 п.п. над ценой (лучший вариант, «да» или «нет», цена стороны 3-95¢); исполнение — первая
настоящая сделка (poly_trades) за 30 мин после решения не дороже шанса смеси − 3 п.п., плюс 1¢ и комиссия (как run_rule).
Порог: итог по настоящим сделкам ≥ 0 и в августе, и в сентябре-октябре → кошелёк; иначе — устаревшая цена, не преимущество.
Только на копии базы:
docker compose run --rm -e JOB_TIMEOUT=0 -e POLY_LAB_DB=/data/research/research.sqlite3 collector weather_study_intraday.py
"""

import math
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import lightgbm as lgb
import numpy as np
import pandas as pd

import weather_ml as ml
import weather_ml_q as mq
import weather_study_tune as tune
from weather_cities import OBS_CITIES

SEEDS = (11, 22, 33)
HOURS = (10, 12, 14)
WEEK0 = date(2026, 8, 10)
N_WEEKS = 8
THREADS = 4
W_MODEL = 0.35
OBS_DELAY_MIN = 10
DAY_FEATS = ["hour", "day_max_vs_fc", "day_now_vs_fc", "day_dt2h", "day_nobs",
             "h_mkt_mean_vs_fc", "h_mkt_std", "h_mkt_top_p", "day_max_vs_hmkt"]


def intraday_rows(conn, df):
    """Утренние строки df (с рынком) × часы решения → строки с дневными признаками, рынком в этот час и вариантами."""
    out = []
    for city, g in df.groupby("city"):
        cfg = OBS_CITIES[city]
        unit, tz = cfg["unit"], ZoneInfo(cfg["tz"])
        days = set(g["date"])
        actual = {d: a for d, a in conn.execute("SELECT local_date, actual_max FROM weather_station_daily WHERE city = ?", (city,))
                  if d in days}
        obs = {}
        for v, t in conn.execute("SELECT valid_utc, tmpf FROM station_obs WHERE city = ? AND tmpf IS NOT NULL", (city,)):
            dt = datetime.fromisoformat(v).replace(tzinfo=timezone.utc)
            d = dt.astimezone(tz).date().isoformat()
            if d in days:
                obs.setdefault(d, []).append((dt, ml.f_to_c(t)))
        win = {}
        for d in days:
            base = datetime.fromisoformat(d).replace(tzinfo=tz).timestamp()
            for h in HOURS:
                win[(d, h)] = (base + (h - 1) * 3600, base + h * 3600)
        prices = {}
        for d, lo, hi, t, p in conn.execute("SELECT local_date, bucket_lo, bucket_hi, t_utc, p FROM price_history WHERE city = ? "
                                            "ORDER BY t_utc", (city,)):
            if d not in days or p is None:
                continue
            for h in HOURS:
                a, b = win[(d, h)]
                if a <= t <= b:
                    prices.setdefault((d, h), {})[(lo, hi)] = p   # последняя цена в часе до решения
        for r in g.to_dict("records"):
            d = r["date"]
            if d not in actual or d < ml.MKT_UNRELIABLE_BEFORE.get(city, ""):
                continue
            for h in HOURS:
                pr = prices.get((d, h))
                if not pr or len(pr) < 3:
                    continue
                t_dec = datetime.fromisoformat(d).replace(tzinfo=tz) + timedelta(hours=h)
                known = sorted(o for o in obs.get(d, []) if o[0] + timedelta(minutes=OBS_DELAY_MIN) <= t_dec)
                if not known:
                    continue
                mf = ml.mkt_features(pr, unit, r["fc_mean"])
                if not mf:
                    continue
                mx = max(t for _, t in known)
                earlier = [o for o in known if o[0] <= known[-1][0] - timedelta(hours=2)]
                row = dict(r)
                row.update(hour=h, day_max_c=mx, day_max_vs_fc=mx - r["fc_mean"], day_now_vs_fc=known[-1][1] - r["fc_mean"],
                           day_dt2h=known[-1][1] - earlier[-1][1] if earlier else np.nan, day_nobs=len(known),
                           h_mkt_mean_vs_fc=mf["mkt_mean_vs_fc"], h_mkt_std=mf["mkt_std"], h_mkt_top_p=mf["mkt_top_p"],
                           day_max_vs_hmkt=mx - (r["fc_mean"] + mf["mkt_mean_vs_fc"]), actual_unit=actual[d], prices=pr)
                out.append(row)
    return pd.DataFrame(out)


def lgb_q(X, y, seed):
    p = {**tune.BASE, "seed": seed, "bagging_seed": seed, "feature_fraction_seed": seed, "num_threads": THREADS}
    return {q: lgb.train({**p, "alpha": q}, lgb.Dataset(X, y, categorical_feature=["city_id"]), 300) for q in mq.QUANTILES}


def pred_q(models, X):
    raw = np.column_stack([models[q].predict(X) for q in mq.QUANTILES])
    raw.sort(axis=1)
    return raw


def win_bucket(prices, a):
    return next(((lo, hi) for lo, hi in prices if lo <= a < hi), None)


if __name__ == "__main__":
    ml.USE_MKT = True
    morning = ml.build(tune.conn)
    morning = morning[morning["actual_c"].notna()]
    ml.FEATURES = ml.features(morning)
    df = intraday_rows(tune.conn, morning)
    feats = ml.FEATURES + DAY_FEATS
    df["base"] = df["fc_mean"] + df["h_mkt_mean_vs_fc"]
    print(f"строк {len(df)} (город-день × час), признаков {len(feats)}; по часам: "
          + ", ".join(f"{h}:00 — {int((df['hour'] == h).sum())}" for h in HOURS), flush=True)
    acc = {h: {"m": [], "k": [], "b": []} for h in HOURS}
    recs = []
    for i in range(N_WEEKS):
        ws, we = (WEEK0 + timedelta(days=7 * i)).isoformat(), (WEEK0 + timedelta(days=7 * (i + 1))).isoformat()
        tr, te = df[df["date"] < ws], df[(df["date"] >= ws) & (df["date"] < we)]
        if te.empty:
            continue
        y = (tr["actual_c"] - tr["base"]).values
        qs = np.mean([pred_q(lgb_q(tr[feats], y, s), te[feats]) for s in SEEDS], axis=0) + te["base"].values[:, None]
        qs = np.maximum(qs, te["day_max_c"].values[:, None])   # максимум дня не ниже уже измеренного
        wk = {h: [] for h in HOURS}
        for q, r in zip(qs, te.to_dict("records")):
            wb = win_bucket(r["prices"], r["actual_unit"])
            if wb is None:
                continue
            tot = sum(r["prices"].values())
            pm = max(mq.bucket_prob(list(q), r["unit"], *wb), 1e-4)
            pk = max(r["prices"][wb] / tot, 1e-4)
            pb = W_MODEL * pm + (1 - W_MODEL) * pk
            for k, v in (("m", pm), ("k", pk), ("b", pb)):
                acc[r["hour"]][k].append(-math.log(v))
            blend = {b: W_MODEL * mq.bucket_prob(list(q), r["unit"], *b) + (1 - W_MODEL) * p / tot for b, p in r["prices"].items()}
            recs.append({"city": r["city"], "date": r["date"], "hour": r["hour"], "win": wb, "blend": blend, "price": r["prices"]})
            wk[r["hour"]].append(-math.log(pb) + math.log(pk))
        print(f"неделя {ws[8:]}.{ws[5:7]}: смесь минус рынок " + ", ".join(
            f"{h}:00 {np.mean(v):+.3f} (n={len(v)})" for h, v in wk.items() if v), flush=True)
    print("\nИТОГ (логошибка, меньше — точнее):", flush=True)
    passed = 0
    for h in HOURS:
        a = acc[h]
        m, k, b = np.mean(a["m"]), np.mean(a["k"]), np.mean(a["b"])
        ok = k - b >= 0.010
        passed += ok
        print(f"{h}:00 — город-дней {len(a['k'])}: модель {m:.4f} | рынок {k:.4f} | смесь {b:.4f} "
              f"({b - k:+.4f}, {'лучше рынка на ≥0.010' if ok else 'не прошёл'})", flush=True)
    print(f"\nПОРОГ (2 из 3 часов): {'ПРОШЁЛ' if passed >= 2 else 'НЕ ПРОШЁЛ'} — {passed} из 3", flush=True)

    # проверка деньгами по настоящим сделкам
    import weather_study_0926 as base
    cids = {base.CID[(x["city"], x["date"], b[0])] for x in recs for b in x["price"] if (x["city"], x["date"], b[0]) in base.CID}
    trades = {}
    for cid, o, sd, p, t in tune.conn.execute("SELECT condition_id, outcome, side, price, ts FROM poly_trades ORDER BY ts"):
        if cid in cids:
            trades.setdefault(cid, []).append((t, o, sd, p))
    by_day = {}
    for x in sorted(recs, key=lambda x: x["hour"]):
        by_day.setdefault((x["city"], x["date"]), []).append(x)
    res = {}
    for (city, d), xs in by_day.items():
        for x in xs:
            cand = []
            for b, p in x["price"].items():
                q = x["blend"][b]
                if q - p >= 0.03 and 0.03 <= p <= 0.95:
                    cand.append((q - p, "yes", b, q))
                if p - q >= 0.03 and 0.03 <= 1 - p <= 0.95:
                    cand.append((p - q, "no", b, 1 - q))
            if not cand:
                continue
            edge, side, b, q = max(cand)
            cid = base.CID.get((city, d, b[0]))
            if d < base.FIRST_TRADE or cid is None:
                break
            ts = datetime.fromisoformat(d).replace(tzinfo=ZoneInfo(OBS_CITIES[city]["tz"])).timestamp() + x["hour"] * 3600
            fp = None
            for t, o, sd, p in trades.get(cid, []):
                if t < ts:
                    continue
                if t > ts + 1800:
                    break
                px = (p if o == "Yes" and sd == "BUY" else (1 - p if o == "No" and sd == "SELL" else None)) if side == "yes" else \
                     (p if o == "No" and sd == "BUY" else (1 - p if o == "Yes" and sd == "SELL" else None))
                if px is not None and px <= min(0.95, q - 0.03):
                    fp = px
                    break
            if fp is not None:
                won = (b == x["win"]) if side == "yes" else (b != x["win"])
                per = "август" if d < "2026-09-01" else "сентябрь-октябрь"
                a = res.setdefault(per, {"n": 0, "won": 0, "pnl": 0.0, "st": 0.0, "h": {}})
                a["n"] += 1; a["won"] += won; a["pnl"] += base.pnl(fp - 0.01, won, base.STAKE); a["st"] += base.STAKE
                a["h"][x["hour"]] = a["h"].get(x["hour"], 0) + 1
            break   # одна попытка на город в день — в первый час с перевесом
    print("\nДЕНЬГИ по настоящим сделкам (смесь, перевес ≥ 3 п.п., $2, одна ставка на город в день):", flush=True)
    ok = 0
    for per in ("август", "сентябрь-октябрь"):
        a = res.get(per)
        if not a:
            print(f"{per}: ставок нет", flush=True)
            continue
        ok += a["pnl"] >= 0
        print(f"{per}: ставок {a['n']}, угадано {a['won']}, итог {a['pnl']:+.2f}$ ({100 * a['pnl'] / a['st']:+.1f}% от вложенного); "
              f"по часам " + ", ".join(f"{h}:00 — {n}" for h, n in sorted(a["h"].items())), flush=True)
    print(f"ПОРОГ ДЕНЕГ (≥ 0 в обоих периодах): {'ПРОШЁЛ' if ok == 2 else 'НЕ ПРОШЁЛ'}", flush=True)
