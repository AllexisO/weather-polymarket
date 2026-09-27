"""Разовое исследование: решение в 08:00 против 10:00 (2026-09-26). Экзамен — последние 28 дней."""
import json, math, sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import weather_ml as ml, weather_ml_check as chk, weather_ml_q as mq
from weather_cities import OBS_CITIES
from weather_ml_live import blend_with_market
conn = sqlite3.connect(ml.DB_PATH, timeout=60); conn.row_factory = sqlite3.Row
win = {(r[0], r[1]): r[2] for r in conn.execute("SELECT city, local_date, win_lo FROM weather_poly_outcomes")}
cid = {(r[0], r[1], r[2]): r[3] for r in conn.execute("SELECT city, local_date, bucket_lo, condition_id FROM poly_market_final")}
import sys
HOURS = [int(h) for h in sys.argv[1:]] or [8, 10]
for hour in HOURS:
    ml.DECISION_HOUR = hour; chk.DECISION_HOUR = hour; ml.USE_MKT = True
    df = ml.build(conn); df = df[df["actual_c"].notna()]
    ml.FEATURES = ml.features(df)
    last = date.fromisoformat(df["date"].max()); cut = (last - timedelta(days=27)).isoformat()
    tr, te = df[df["date"] < cut], df[df["date"] >= cut]
    models = mq.train_q(tr)
    qs_all = mq.predict_q(models, te[ml.FEATURES], te["fc_mean"].values)
    i50 = mq.QUANTILES.index(0.5)
    S = {k: [] for k in ("ll_m", "ll_k", "ll_b", "err_m", "err_k")}
    bets = {"raw10": [0, 0, 0.0, 0.0], "blend3": [0, 0, 0.0, 0.0]}  # n, won, $ по цене, $ по сделкам (n_real в 3-м?) 
    real = {"raw10": [0, 0.0], "blend3": [0, 0.0]}
    for (_, r), qs in zip(te.iterrows(), qs_all):
        S["err_m"].append(abs(qs[i50] - r["actual_c"]))
        if not math.isnan(r.get("mkt_mean_vs_fc", float("nan"))):
            S["err_k"].append(abs(r["fc_mean"] + r["mkt_mean_vs_fc"] - r["actual_c"]))
        w = win.get((r["city"], r["date"]))
        pr = chk.prices(conn, r["city"], r["date"], "A") if w is not None else {}
        wb = next((b for b in pr if b[0] == w), None)
        if len(pr) < 3 or wb is None: continue
        keys = list(pr); P = {b: mq.bucket_prob(list(qs), r["unit"], b[0], b[1]) for b in keys}
        tm = sum(P.values()) or 1; P = {b: P[b] / tm for b in keys}; tk = sum(pr.values()) or 1
        B = dict(zip(keys, blend_with_market([P[b] for b in keys], [pr[b] for b in keys])))
        S["ll_m"].append(-math.log(max(P[wb], 1e-4))); S["ll_k"].append(-math.log(max(pr[wb] / tk, 1e-4))); S["ll_b"].append(-math.log(max(B[wb], 1e-4)))
        for name, Q, thr in (("raw10", P, 0.10), ("blend3", B, 0.03)):
            b = max(keys, key=lambda x: Q[x] - pr[x])
            if not (Q[b] - pr[b] >= thr and 0.03 <= pr[b] <= 0.95): continue
            won = b[0] == w
            bets[name][0] += 1; bets[name][1] += won; bets[name][2] += chk.pnl(pr[b], won) * 2 / 5
            key = (r["city"], r["date"], b[0])
            if key in cid:
                ts = datetime.fromisoformat(r["date"]).replace(tzinfo=ZoneInfo(OBS_CITIES[r["city"]]["tz"])).timestamp() + hour * 3600
                maxp = min(0.95, Q[b] - thr)
                fills = sorted((t, (p if o == "Yes" else 1 - p)) for o, sd, p, t in conn.execute(
                    "SELECT outcome, side, price, ts FROM poly_trades WHERE condition_id = ? AND ts BETWEEN ? AND ?", (cid[key], ts, ts + 1800))
                    if ((o == "Yes" and sd == "BUY") or (o == "No" and sd == "SELL")) and (p if o == "Yes" else 1 - p) <= maxp)
                if fills:
                    real[name][0] += 1; real[name][1] += chk.pnl(fills[0][1] - 0.01, won) * 2 / 5
    avg = lambda v: sum(v) / len(v) if v else float("nan")
    print(f"== {'накануне ' + str(24 + hour) + ':00' if hour < 0 else str(hour) + ':00'}  экзамен {cut}..{last} ({len(te)} город-дней, с ценами {len(S['ll_m'])})")
    print(f"   логошибка: модель {avg(S['ll_m']):.3f}  рынок {avg(S['ll_k']):.3f}  смесь {avg(S['ll_b']):.3f}   ошибка °C: модель {avg(S['err_m']):.3f} рынок {avg(S['err_k']):.3f}")
    for name in bets:
        n, won, pnl, _ = bets[name]
        print(f"   ставки {name:6s}: {n:4d}, угадано {won:3d}, $ по цене {pnl:+7.1f} | по реальным сделкам {real[name][0]:3d} ставок {real[name][1]:+7.1f}$")
