"""
Учим бота-мейкера (2026-09-30, Alex: «можем учить бота? LightGBM?» → «давай»): модель предсказывает, сколько заработает
стоящий с заявкой, если её заберут сейчас (¢ на долю к итогу маркета, с возвратом 25% комиссии). Бот держит заявку только
там, где прогноз > 0. Каждая сделка poly_trades — исполнение чьей-то заявки с известным итогом = готовый пример.

Признаки (всё известно в момент сделки): цена стороны мейкера, «да»/«нет», часов до конца дня маркета, местный час, накануне
или день маркета, минут после плановой сводки METAR, размер сделки, движение цены за 10 и 60 мин и число сделок за 10 мин
(по сделкам этого варианта до этой), город, сколько станция уже показала относительно границ варианта (station_obs до момента
сделки), отличие цены от честной цены (смесь v3 + рынок на 08:00, проверка вслепую ml_preds_var_mkt; есть не везде).
Проверка по неделям: учим на всех неделях до k, проверяем неделю k (k = 3-6-я неделя с 19.08). Все политики — с паузами
бота (−1…+3 мин вокруг METAR, после 18:00 в день маркета) и размером как у бота (≤ 10 долей на исполнение).
Сравнение: mm_all (все), mm_sel (выгодные зоны бота), mm_ml (прогноз > 0).
Порог (записан до прогона): mm_ml на проверочных неделях ≥ +0.5¢ на долю лучше лучшей из mm_all / mm_sel и сама в плюсе.
Только на копии базы.
Прогон 1 (30.09): «+$173k, +4.2¢» — слишком хорошо, найдены две ошибки: (1) честная цена на 08:00 дня маркета попадала
в признаки сделок накануне и ночью (заглядывание вперёд); (2) сделка, прошедшая сквозь несколько уровней стакана,
записана по худшей цене — мейкер первой в очереди заявки получил бы лучшую. Исправлено (прогон 2): честная цена — только
с 08:00 дня маркета; цена мейкера = не лучше, чем у предыдущей сделки (осторожно); движение цены — только по прошлым сделкам.
"""
import bisect
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import lightgbm as lgb
import numpy as np
import pandas as pd

import weather_ml_check as chk
import weather_ml_q as mq
from weather_cities import OBS_CITIES
from weather_ml_live import blend_with_market
from weather_mm_paper import SEL

DB = "/data/research/research.sqlite3"
conn = sqlite3.connect(DB)
T0 = datetime(2026, 8, 19, tzinfo=timezone.utc).timestamp()
WEEK = 7 * 86400
CITY_ID = {c: i for i, c in enumerate(OBS_CITIES)}


def metar_minutes():
    from collections import Counter
    out = {}
    for city in OBS_CITIES:
        c = Counter(int(r[0][14:16]) for r in conn.execute(
            "SELECT valid_utc FROM station_obs WHERE city = ? AND valid_utc >= '2026-08-15'", (city,)))
        tot = sum(c.values()) or 1
        out[city] = sorted(m for m, n in c.items() if n / tot >= 0.15) or [0]
    return out


def fair_values():
    fv = {}
    for city, d, unit, qs in conn.execute("SELECT city, date, unit, qs FROM ml_preds_var_mkt WHERE date >= '2026-08-18'"):
        if city not in OBS_CITIES:
            continue
        pr = chk.prices(conn, city, d, "A")
        if len(pr) < 3:
            continue
        q = json.loads(qs)
        keys = list(pr)
        model = [mq.bucket_prob(q, unit, b[0], b[1]) for b in keys]
        tm = sum(model) or 1.0
        for b, p in zip(keys, blend_with_market([m / tm for m in model], [pr[b] for b in keys])):
            fv[(city, d, b[0], b[1])] = p
    return fv


def obs_series():
    """(город, день) → (секунды UTC, максимум с начала местного дня в единицах маркета)."""
    out = {}
    rows = conn.execute("SELECT city, valid_utc, tmpf FROM station_obs WHERE valid_utc >= '2026-08-17' AND tmpf IS NOT NULL ORDER BY city, valid_utc")
    cur = None
    for city, v, tf in rows:
        if city not in OBS_CITIES:
            continue
        cfg = OBS_CITIES[city]
        t = datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=timezone.utc) if "+" not in v else datetime.fromisoformat(v)
        d = t.astimezone(ZoneInfo(cfg["tz"])).date().isoformat()
        val = tf if cfg["unit"] == "fahrenheit" else (tf - 32) * 5 / 9
        k = (city, d)
        if k != cur:
            cur, mx = k, -1e9
            out[k] = ([], [])
        mx = max(mx, val)
        out[k][0].append(t.timestamp()); out[k][1].append(mx)
    return out


def build():
    mins = metar_minutes()
    fv = fair_values()
    obs = obs_series()
    final = {r[0]: r[1:] for r in conn.execute("SELECT condition_id, final_yes, city, local_date, bucket_lo, bucket_hi FROM poly_market_final")}
    print(f"честных цен {len(fv)}, дней замеров {len(obs)}", flush=True)
    recs = []
    cur, hist = None, None
    for cid, outc, side, p, size, ts, tx in conn.execute(
            "SELECT condition_id, outcome, side, price, size, ts, tx FROM poly_trades ORDER BY condition_id, ts"):
        if cid != cur:
            cur, hist, seen = cid, ([], []), set()
        k = (tx, outc, side, p, size, ts)
        if k in seen or not (0 < p < 1):
            continue
        seen.add(k)
        f = final.get(cid)
        y = p if outc == "Yes" else 1 - p
        hist[0].append(ts); hist[1].append(y)
        if not f or f[1] not in OBS_CITIES:
            continue
        fin, city, ld, lo, hi = f
        cfg = OBS_CITIES[city]
        hits_yes = (outc == "Yes" and side == "SELL") or (outc == "No" and side == "BUY")
        prev_y = hist[1][-2] if len(hist[1]) >= 2 else y
        # осторожно: мейкер первым в очереди получил бы цену не лучше предыдущей сделки (проход сквозь стакан записан по худшей)
        mp = max(y, prev_y) if hits_yes else max(1 - y, 1 - prev_y)
        pay = fin if hits_yes else 1 - fin
        loc = datetime.fromtimestamp(ts, ZoneInfo(cfg["tz"]))
        same = loc.date().isoformat() == ld
        if same and loc.hour >= 18:
            continue
        mn = datetime.fromtimestamp(ts, timezone.utc).minute
        since = min((mn - x) % 60 for x in mins[city]); until = min((x - mn) % 60 for x in mins[city])
        if since <= 3 or until <= 1:
            continue                               # пауза бота вокруг сводки
        if not (0.02 <= mp <= 0.98):
            continue
        day_end = datetime.fromisoformat(ld).replace(tzinfo=ZoneInfo(cfg["tz"])) + timedelta(days=1)
        i = len(hist[0]) - 1                       # эта сделка — последняя в истории; смотрим на прошлые
        def y_ago(sec):
            j = bisect.bisect_right(hist[0], ts - sec, 0, i) - 1
            return hist[1][j] if j >= 0 else None
        y10, y60 = y_ago(600), y_ago(3600)
        n10 = i - bisect.bisect_left(hist[0], ts - 600, 0, i)
        prev = hist[1][i - 1] if i > 0 else None
        y_now = prev if prev is not None else y        # цена до этой сделки — всё, что знали в момент заявки
        ob = obs.get((city, ld))
        omax = None
        if ob and same:
            j = bisect.bisect_right(ob[0], ts) - 1
            omax = ob[1][j] if j >= 0 else None
        f_yes = fv.get((city, ld, lo, hi)) if same and loc.hour >= 8 else None  # честная цена известна только с 08:00 дня маркета
        f_side = None if f_yes is None else (f_yes if hits_yes else 1 - f_yes)
        zone = "накануне" if not same else next(f"{a}-{b}" for a, b in ((0, 6), (6, 9), (9, 12), (12, 15), (15, 18), (18, 24)) if a <= loc.hour < b)
        sh = min(size, 10.0)
        recs.append({
            "ts": ts, "week": int((ts - T0) // WEEK), "sh": sh,
            "pnl": pay - mp + 0.25 * 0.05 * p * (1 - p),
            "sel": int(any(a <= mp < b and z == zone for a, b, z in SEL)),
            "mp": mp, "is_yes": int(hits_yes), "hours_left": (day_end.timestamp() - ts) / 3600, "hour": loc.hour, "same": int(same),
            "since_metar": since, "until_metar": until, "usd": size * p, "d10": (y_now - y10) if y10 is not None else np.nan,
            "d60": (y_now - y60) if y60 is not None else np.nan, "n10": n10,
            "city_id": CITY_ID[city], "fv_gap": (f_side - mp) if f_side is not None else np.nan,
            "obs_above_hi": (omax - hi) if omax is not None and hi < 900 else np.nan,
            "obs_below_lo": (lo - omax) if omax is not None and lo > -900 else np.nan,
            "is_tail": int(lo <= -900 or hi >= 900),
        })
    return pd.DataFrame(recs)


FEATS = ["mp", "is_yes", "hours_left", "hour", "same", "since_metar", "until_metar", "usd", "d10", "d60", "n10",
         "city_id", "fv_gap", "obs_above_hi", "obs_below_lo", "is_tail"]
PARAMS = dict(objective="regression", learning_rate=0.05, num_leaves=31, min_data_in_leaf=500, feature_fraction=0.8,
              bagging_fraction=0.8, bagging_freq=1, lambda_l2=1.0, verbose=-1, num_threads=4)


def money(df, mask):
    d = df[mask]
    sh = d["sh"].sum()
    usd = (d["sh"] * d["mp"]).sum()
    pn = (d["sh"] * d["pnl"]).sum()
    return pn, 100 * pn / sh if sh else 0.0, 100 * pn / usd if usd else 0.0, len(d)


def main():
    df = build()
    print(f"примеров {len(df)}, недель {df['week'].nunique()}, честная цена есть у {df['fv_gap'].notna().mean():.0%}, "
          f"замер дня у {df['obs_above_hi'].notna().mean() + 0:.0%}", flush=True)
    rows = []
    imp = defaultdict(float)
    for k in sorted(df["week"].unique()):
        if k < 2:
            continue
        tr, te = df[df["week"] < k], df[df["week"] == k].copy()
        preds = []
        for seed in (11, 22, 33):
            m = lgb.train({**PARAMS, "seed": seed}, lgb.Dataset(tr[FEATS], tr["pnl"], weight=tr["sh"], categorical_feature=["city_id"]), 300)
            preds.append(m.predict(te[FEATS]))
            for n, g in zip(FEATS, m.feature_importance("gain")):
                imp[n] += g
        te["pred"] = np.mean(preds, axis=0)
        wk = datetime.fromtimestamp(T0 + k * WEEK, timezone.utc).strftime("%d.%m")
        for name, mask in (("mm_all", te["sh"] > 0), ("mm_sel", te["sel"] == 1), ("mm_ml", te["pred"] > 0), ("mm_ml > 0.5¢", te["pred"] > 0.005)):
            pn, c, r, n = money(te, mask)
            rows.append((wk, name, pn, c, r, n))
    out = pd.DataFrame(rows, columns=["неделя", "политика", "итог", "c", "pct", "n"])
    print("\nпо неделям (итог $ при заявках по 10 долей, ¢ на долю, % от потраченного, исполнений):")
    for wk, g in out.groupby("неделя", sort=False):
        print("  " + wk + ": " + " | ".join(f"{r.политика} {r.итог:+,.0f}$ {r.c:+.2f}¢ {r.pct:+.1f}% n={r.n}" for r in g.itertuples()))
    tot = out.groupby("политика", sort=False).agg(итог=("итог", "sum"), n=("n", "sum"))
    print("\nвсе проверочные недели:")
    best_rule = None
    res = {}
    for name in tot.index:
        g = out[out["политика"] == name]
        sh_w = (g["итог"] / (g["c"] / 100)).replace([np.inf, -np.inf], 0).fillna(0).sum()
        c = 100 * g["итог"].sum() / sh_w if sh_w else 0
        res[name] = c
        pos = (g["итог"] > 0).sum()
        print(f"  {name:14s} итог {g['итог'].sum():+,.0f}$  {c:+.2f}¢ на долю  исполнений {g['n'].sum():,}  недель в плюсе {pos} из {len(g)}")
    best_rule = max(res["mm_all"], res["mm_sel"])
    ok = res["mm_ml"] - best_rule >= 0.5 and res["mm_ml"] > 0
    print(f"\nПОРОГ (mm_ml ≥ лучшего правила + 0.5¢ и в плюсе): {'ПРОШЛО' if ok else 'не прошло'}")
    tot_imp = sum(imp.values()) or 1
    print("на что смотрит модель:", ", ".join(f"{n} {100 * g / tot_imp:.0f}%" for n, g in sorted(imp.items(), key=lambda x: -x[1])[:10]))


main()
