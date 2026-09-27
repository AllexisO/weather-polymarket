"""
Объединение версий обучаемой модели в одну (2026-09-25, вопрос Alex:
"можем всё объединить в одно?"). Усредняем шансы вариантов от разных
версий поровну — без подбора весов (подбор на той же истории был бы
подгонкой). У разных моделей разные ошибки, среднее часто точнее
каждой по отдельности.

Источники (walk-forward прогнозы, сохранённые weather_ml_variants.py и
weather_ml.py):
  v2  — ml_preds_var_base (распределение);
  v3  — ml_preds_var_mkt  (v2 + мнение рынка);
  all — ml_preds_var_all  (v3 + соседние станции + разборы метеорологов);
  mix — микс 16 погодных моделей (ml_preds_wf: mix_mu_c/mix_sigma_c).
Те же метрики, что у weather_ml_variants.score.

Запуск: python weather_ml_ensemble.py
"""

import json
import math
import os
import random
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import weather_ml_check as chk
import weather_ml_q as mq
from weather_cities import OBS_CITIES
from weather_edge import emos_bucket_prob

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
COMBOS = {
    "v2": ["v2"], "v3": ["v3"], "all": ["all"],
    "v2+v3": ["v2", "v3"], "v3+микс": ["v3", "mix"], "v2+v3+микс": ["v2", "v3", "mix"], "v3+all": ["v3", "all"],
}


def main():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    src = {name: pd.read_sql(f"SELECT * FROM {t}", conn).set_index(["city", "date"])
           for name, t in (("v2", "ml_preds_var_base"), ("v3", "ml_preds_var_mkt"), ("all", "ml_preds_var_all"))}
    wf = pd.read_sql("SELECT * FROM ml_preds_wf", conn).set_index(["city", "date"])
    win = {(r[0], r[1]): r[2] for r in conn.execute("SELECT city, local_date, win_lo FROM weather_poly_outcomes")}
    cid = {(r[0], r[1], r[2]): r[3] for r in conn.execute("SELECT city, local_date, bucket_lo, condition_id FROM poly_market_final")}
    first_trade = conn.execute("SELECT MIN(local_date) FROM poly_trades_days").fetchone()[0]
    keys = sorted(set(src["v2"].index) & set(src["v3"].index) & set(src["all"].index) & set(wf.index))
    res = {}
    for key in keys:
        city, d = key
        w = win.get(key)
        pr = chk.prices(conn, city, d, "A")
        if w is None or len(pr) < 3:
            continue
        unit = src["v2"].loc[key, "unit"]
        k, off = (9 / 5, 32) if unit == "fahrenheit" else (1, 0)
        single = {}
        for name in ("v2", "v3", "all"):
            qs = json.loads(src[name].loc[key, "qs"])
            single[name] = {b: mq.bucket_prob(qs, unit, b[0], b[1]) for b in pr}
        m = wf.loc[key]
        if pd.isna(m["mix_mu_c"]):
            continue
        single["mix"] = {b: emos_bucket_prob(m["mix_mu_c"] * k + off, m["mix_sigma_c"] * k, b[0], b[1]) for b in pr}
        per = "сентябрь" if d >= "2026-09-01" else "июль-август"
        for combo, parts in COMBOS.items():
            P = {b: sum(single[p][b] for p in parts) / len(parts) for b in pr}
            s = res.setdefault((per, combo), {"n": 0, "ll": 0.0, "hit": 0, "bets": [], "real": []})
            tot = sum(P.values()) or 1.0
            wb = next((b for b in P if b[0] == w), None)
            s["n"] += 1
            s["ll"] += -math.log(max(P.get(wb, 0.0) / tot, 1e-4))
            s["hit"] += max(P, key=P.get)[0] == w
            b = max(pr, key=lambda x: P[x] - pr[x])
            if P[b] - pr[b] >= 0.10 and 0.03 <= pr[b] <= 0.95:
                won = b[0] == w
                s["bets"].append(chk.pnl(pr[b], won))
                ck = (city, d, b[0])
                if d >= first_trade and ck in cid:
                    ts = datetime.fromisoformat(d).replace(tzinfo=ZoneInfo(OBS_CITIES[city]["tz"])).timestamp() + 8 * 3600
                    maxp = min(0.95, P[b] - 0.10)
                    fills = sorted((t, (p if o == "Yes" else 1 - p)) for o, sd, p, t in conn.execute(
                        "SELECT outcome, side, price, ts FROM poly_trades WHERE condition_id = ? AND ts BETWEEN ? AND ?",
                        (cid[ck], ts, ts + 1800))
                        if ((o == "Yes" and sd == "BUY") or (o == "No" and sd == "SELL")) and (p if o == "Yes" else 1 - p) <= maxp)
                    if fills:
                        s["real"].append(chk.pnl(fills[0][1] - 0.01, won))
    print("вариант       период       логошибка угадан  ставок   итог$  без5лучших   95%-интервал    | реальные сделки")
    for per in ("июль-август", "сентябрь"):
        for combo in COMBOS:
            s = res.get((per, combo))
            if not s:
                continue
            b = s["bets"]
            random.seed(1)
            bs = sorted(sum(random.choice(b) for _ in b) for _ in range(1000)) if b else [0] * 1000
            print(f"{combo:12}  {per:11}  {s['ll'] / s['n']:8.3f} {100 * s['hit'] / s['n']:5.1f}%  {len(b):6} {sum(b):+7.0f}  "
                  f"{sum(b) - sum(sorted(b)[-5:]):+8.0f}   {bs[25]:+6.0f}…{bs[975]:+6.0f}   | "
                  f"{len(s['real'])} ставок {sum(s['real']):+.0f}$")
    conn.close()


if __name__ == "__main__":
    main()
