"""
Сравнение вариантов обучаемой модели v2 (распределение, weather_ml_q.py)
по одному изменению за раз (2026-09-25, пункты 3-4 плана Alex):
  base — как сейчас в живом кошельке ml2;
  nb   — + соседние METAR-станции (weather_ml.USE_NB);
  mkt  — + мнение рынка в 08:00 (weather_ml.USE_MKT).
Одинаковый walk-forward (по неделям с 2026-07-01), одинаковые метрики:
логарифмическая ошибка по реальному исходу (меньше — лучше), попадание,
ставки по правилу кошельков (цена из истории +1¢, комиссия), бутстрэп
95%-интервал, итог без 5 лучших выигрышей, ставки по реальным сделкам.

Запуск: python weather_ml_variants.py [base nb mkt ...]
"""

import json
import math
import os
import random
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import weather_ml as ml
import weather_ml_check as chk
import weather_ml_q as mq
from weather_cities import OBS_CITIES

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
VARIANTS = {"base": {}, "nb": {"USE_NB": True}, "mkt": {"USE_MKT": True}, "nb+mkt": {"USE_NB": True, "USE_MKT": True},
            # всё сразу: рынок + соседние станции + разборы метеорологов (только США)
            "all": {"USE_NB": True, "USE_MKT": True, "USE_AFD": True}}


def score(conn, preds):
    win = {(r[0], r[1]): r[2] for r in conn.execute("SELECT city, local_date, win_lo FROM weather_poly_outcomes")}
    cid = {(r[0], r[1], r[2]): r[3] for r in conn.execute("SELECT city, local_date, bucket_lo, condition_id FROM poly_market_final")}
    first_trade = conn.execute("SELECT MIN(local_date) FROM poly_trades_days").fetchone()[0]
    out = {}
    for _, r in preds.iterrows():
        w = win.get((r["city"], r["date"]))
        if w is None:
            continue
        pr = chk.prices(conn, r["city"], r["date"], "A")
        if len(pr) < 3:
            continue
        per = "сентябрь" if r["date"] >= "2026-09-01" else "июль-август"
        qs = json.loads(r["qs"])
        P = {b: mq.bucket_prob(qs, r["unit"], b[0], b[1]) for b in pr}
        s = out.setdefault(per, {"n": 0, "ll": 0.0, "hit": 0, "bets": [], "real": []})
        tot = sum(P.values()) or 1.0
        wb = next((b for b in P if b[0] == w), None)
        s["n"] += 1
        s["ll"] += -math.log(max((P.get(wb, 0.0)) / tot, 1e-4))
        s["hit"] += max(P, key=P.get)[0] == w
        b = max(pr, key=lambda x: P[x] - pr[x])
        if P[b] - pr[b] >= 0.10 and 0.03 <= pr[b] <= 0.95:
            won = b[0] == w
            s["bets"].append(chk.pnl(pr[b], won))
            key = (r["city"], r["date"], b[0])
            if r["date"] >= first_trade and key in cid:
                ts = datetime.fromisoformat(r["date"]).replace(tzinfo=ZoneInfo(OBS_CITIES[r["city"]]["tz"])).timestamp() + 8 * 3600
                maxp = min(0.95, P[b] - 0.10)
                fills = sorted((t, (p if o == "Yes" else 1 - p)) for o, sd, p, t in conn.execute(
                    "SELECT outcome, side, price, ts FROM poly_trades WHERE condition_id = ? AND ts BETWEEN ? AND ?",
                    (cid[key], ts, ts + 1800))
                    if ((o == "Yes" and sd == "BUY") or (o == "No" and sd == "SELL")) and (p if o == "Yes" else 1 - p) <= maxp)
                if fills:
                    s["real"].append(chk.pnl(fills[0][1] - 0.01, won))
    return out


def main():
    names = sys.argv[1:] or ["base", "nb", "mkt"]
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    results = {}
    for name in names:
        for flag in ("USE_NB", "USE_MKT", "USE_AFD"):
            setattr(ml, flag, VARIANTS[name].get(flag, False))
        df = ml.build(conn)
        df = df[df["actual_c"].notna()]
        ml.FEATURES = ml.features(df)
        preds = mq.walk_forward_q(df)
        # сохраняем — для усреднения версий (weather_ml_ensemble.py)
        wconn = sqlite3.connect(DB_PATH, timeout=60)
        preds.to_sql(f"ml_preds_var_{name.replace('+', '_')}", wconn, if_exists="replace", index=False)
        wconn.close()
        results[name] = score(conn, preds)
        print(f"вариант {name}: признаков {len(ml.FEATURES)}", flush=True)
    print("\nвариант  период       логошибка угадан  ставок   итог$  без5лучших   95%-интервал    | реальные сделки")
    for per in ("июль-август", "сентябрь"):
        for name in names:
            s = results[name].get(per)
            if not s:
                continue
            b = s["bets"]
            random.seed(1)
            bs = sorted(sum(random.choice(b) for _ in b) for _ in range(1000)) if b else [0] * 1000
            print(f"{name:7}  {per:11}  {s['ll'] / s['n']:8.3f} {100 * s['hit'] / s['n']:5.1f}%  {len(b):6} {sum(b):+7.0f}  "
                  f"{sum(b) - sum(sorted(b)[-5:]):+8.0f}   {bs[25]:+6.0f}…{bs[975]:+6.0f}   | "
                  f"{len(s['real'])} ставок {sum(s['real']):+.0f}$")
    conn.close()


if __name__ == "__main__":
    main()
