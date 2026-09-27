"""
Проверка ставок обучаемой модели по РЕАЛЬНЫМ сделкам (2026-09-25).

price_history отдаёт точку каждые 5 минут, даже если сделок не было
(повторяет старую цену) — поэтому "свежесть" по ней не проверить. Здесь
для периода, по которому у нас есть все сделки (poly_trades, последние
30 дней), смотрим: была ли в течение 30 минут после решения (08:00
местного) реальная сделка, где кто-то ПРОДАВАЛ Yes нашего бакета по
цене не дороже, чем выгодно по нашим правилам (оценка модели − 10 п.п.).
Если была — ставим по цене первой такой сделки (+1¢, комиссия). Если нет —
считаем, что купить было нельзя.

Запуск: python weather_ml_realfill.py
"""

import os
import random
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import weather_ml_check as chk
from weather_cities import OBS_CITIES
from weather_edge import emos_bucket_prob

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))


def main():
    c = sqlite3.connect(DB_PATH, timeout=60)
    first = c.execute("SELECT MIN(local_date) FROM poly_trades_days").fetchone()[0]
    preds = pd.read_sql("SELECT * FROM ml_preds_wf WHERE date >= ?", c, params=(first,))
    win = {(r[0], r[1]): r[2] for r in c.execute("SELECT city, local_date, win_lo FROM weather_poly_outcomes")}
    cid = {(r[0], r[1], r[2]): r[3] for r in c.execute("SELECT city, local_date, bucket_lo, condition_id FROM poly_market_final")}
    res = []
    for _, r in preds.iterrows():
        w = win.get((r["city"], r["date"]))
        if w is None:
            continue
        pr = chk.prices(c, r["city"], r["date"], "A")
        if len(pr) < 3:
            continue
        k = 9 / 5 if r["unit"] == "fahrenheit" else 1
        off = 32 if r["unit"] == "fahrenheit" else 0
        probs = {b: emos_bucket_prob(r["ml_mu_c"] * k + off, r["ml_sigma_c"] * k, b[0], b[1]) for b in pr}
        b = max(pr, key=lambda x: probs[x] - pr[x])
        if not (probs[b] - pr[b] >= 0.10 and 0.03 <= pr[b] <= 0.95):
            continue
        cond = cid.get((r["city"], r["date"], b[0]))
        if cond is None:
            continue
        ts = datetime.fromisoformat(r["date"]).replace(tzinfo=ZoneInfo(OBS_CITIES[r["city"]]["tz"])).timestamp() + 8 * 3600
        maxp = min(0.95, probs[b] - 0.10)
        fills = []
        for outcome, side, p, size, t in c.execute(
                "SELECT outcome, side, price, size, ts FROM poly_trades WHERE condition_id = ? AND ts BETWEEN ? AND ?",
                (cond, ts, ts + 1800)):
            yes = p if outcome == "Yes" else 1 - p
            if ((outcome == "Yes" and side == "BUY") or (outcome == "No" and side == "SELL")) and yes <= maxp:
                fills.append((t, yes, size))
        won = b[0] == w
        if fills:
            fills.sort()
            p_real = fills[0][1]
            res.append({"date": r["date"], "city": r["city"], "p_hist": pr[b], "p_real": p_real, "won": won,
                        "pnl": chk.pnl(p_real - 0.01, won), "vol": sum(f[2] * f[1] for f in fills)})
        else:
            res.append({"date": r["date"], "city": r["city"], "p_hist": pr[b], "p_real": None, "won": won, "pnl": 0.0, "vol": 0})
    df = pd.DataFrame(res)
    f = df[df.p_real.notna()]
    print(f"сигналов ({first} и позже): {len(df)}; реально можно было купить в 30 мин после решения: {len(f)} ({100 * len(f) / len(df):.0f}%)")
    print(f"итог по реальным ценам сделок: {f.pnl.sum():+.0f}$ на {len(f)} ставках ({f.pnl.mean():+.2f}/ставку), выиграно {int(f.won.sum())}")
    print(f"без 5 лучших выигрышей: {f.pnl.sum() - f.nlargest(5, 'pnl').pnl.sum():+.0f}$")
    vals = f.pnl.tolist()
    random.seed(1)
    bs = sorted(sum(random.choice(vals) for _ in vals) for _ in range(2000))
    print(f"95% интервал: {bs[50]:+.0f}…{bs[1950]:+.0f}$")
    nf = df[df.p_real.isna()]
    print(f"не смогли бы купить: {len(nf)}; из них выиграли бы {int(nf.won.sum())}")
    print(f"цена по истории vs реальная (медиана): {f.p_hist.median():.3f} vs {f.p_real.median():.3f}; "
          f"медианный доступный объём ${f.vol.median():.0f}")
    c.close()


if __name__ == "__main__":
    main()
