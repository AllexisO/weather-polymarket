"""
Помогают ли признаки из текстовых разборов метеорологов (weather_afd.py,
Qwen3 8B) обучаемой модели? 2026-09-25, исследование.

Две модели, одинаковые во всём, кроме признаков afd_* (USE_AFD), обе
walk-forward на всех 48 городах; сравниваем ТОЛЬКО на 11 городах США,
где разборы есть: ошибка прогноза, попадание в диапазон, ставки по
тому же правилу (цена A из weather_ml_check, +1¢, комиссия).
Плюс — насколько точен сам максимум, названный метеорологом.

Запуск: python weather_ml_afd_eval.py
"""

import os
import sqlite3
from pathlib import Path

import numpy as np

import weather_ml as ml
import weather_ml_check as chk
from weather_afd import WFO
from weather_edge import emos_bucket_prob

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))


def run(conn, use_afd):
    ml.USE_AFD = use_afd
    df = ml.build(conn)
    df = df[df["actual_c"].notna()]
    ml.FEATURES = ml.features(df)
    return ml.walk_forward(df)


def score(conn, preds, win):
    out = {}
    for per, g in (("июль-август", preds[preds["date"] < "2026-09-01"]), ("сентябрь", preds[preds["date"] >= "2026-09-01"])):
        g = g[g["city"].isin(WFO)]
        err = np.abs(g["ml_mu_c"] - g["actual_c"]).mean()
        hits = n = 0
        bets = [0, 0.0]
        for _, r in g.iterrows():
            w = win.get((r["city"], r["date"]))
            pr = chk.prices(conn, r["city"], r["date"], "A")
            if w is None or len(pr) < 3:
                continue
            k, off = (9 / 5, 32) if r["unit"] == "fahrenheit" else (1, 0)
            probs = {b: emos_bucket_prob(r["ml_mu_c"] * k + off, r["ml_sigma_c"] * k, b[0], b[1]) for b in pr}
            n += 1
            hits += max(probs, key=probs.get)[0] == w
            b = max(pr, key=lambda x: probs[x] - pr[x])
            if probs[b] - pr[b] >= 0.10 and 0.03 <= pr[b] <= 0.95:
                bets[0] += 1
                bets[1] += chk.pnl(pr[b], b[0] == w)
        out[per] = (err, 100 * hits / n if n else 0, bets)
    return out


def main():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    win = {(r[0], r[1]): r[2] for r in conn.execute("SELECT city, local_date, win_lo FROM weather_poly_outcomes")}
    base = score(conn, run(conn, False), win)
    with_afd = score(conn, run(conn, True), win)
    print("\nТолько 11 городов США:")
    for per in ("июль-август", "сентябрь"):
        for name, s in (("без разборов", base[per]), ("с разборами", with_afd[per])):
            e, h, (nb, p) = s
            print(f"  {per:12} {name:13}: ошибка {e:.2f}°C, угадан диапазон {h:.0f}%, ставок {nb}, итог {p:+.0f}$")
    # точность самого метеоролога
    rows = conn.execute(
        """SELECT a.city, a.high_f, d.actual_max, o.unit FROM afd_signals a
           JOIN weather_station_daily d ON d.city = a.city AND d.local_date = a.local_date
           JOIN (SELECT 'x' AS unit) o WHERE a.high_f IS NOT NULL""").fetchall()
    if rows:
        e = np.mean([abs(r[1] - r[2]) for r in rows])
        print(f"\nмаксимум, названный метеорологом в утреннем разборе: {len(rows)} дней, средняя ошибка {e:.2f}°F ({e * 5 / 9:.2f}°C)")
    conn.close()


if __name__ == "__main__":
    main()
