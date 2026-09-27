"""
Перепроверка результата обучаемой модели (weather_ml.py), 2026-09-25.
Урок +$7260 (см. CLAUDE.md): слишком хороший результат сначала
разбираем, потом показываем.

Проверяем на сохранённых walk-forward прогнозах (ml_preds_wf):
1. Цена покупки — три варианта:
   A) последняя цена за час ДО решения (как в weather_ml.py);
   B) только свежая: не старше 10 минут до решения;
   C) первая цена ПОСЛЕ решения (в пределах 30 минут) — как было бы
      в реальности: решили, потом купили.
   Везде +1¢ спред и комиссия Polymarket.
2. Откуда прибыль: по ценовым корзинам, по месяцам, по ликвидности
   города; итог без 5 самых крупных выигрышей.
3. Разброс: бутстрэп 95%-интервал итога (перевыборка ставок).
4. Калибровка: когда модель говорит 30%, выигрывает ли в ~30%.

Запуск: python weather_ml_check.py
"""

import os
import random
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from weather_cities import OBS_CITIES
from weather_edge import emos_bucket_prob

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
DECISION_HOUR = 8


def pnl(price, won):
    buy = min(price + 0.01, 0.999)
    shares = 5 / (buy + 0.05 * buy * (1 - buy))
    return shares - 5 if won else -5


def prices(conn, city, d, variant):
    ts = datetime.fromisoformat(d).replace(tzinfo=ZoneInfo(OBS_CITIES[city]["tz"])).timestamp() + DECISION_HOUR * 3600
    if variant == "A":
        lo, hi, pick = ts - 3600, ts, -1
    elif variant == "B":
        lo, hi, pick = ts - 600, ts, -1
    else:
        lo, hi, pick = ts + 1, ts + 1800, 0
    out = {}
    for b_lo, b_hi, t, p in conn.execute(
            "SELECT bucket_lo, bucket_hi, t_utc, p FROM price_history WHERE city = ? AND local_date = ? AND t_utc BETWEEN ? AND ? ORDER BY t_utc",
            (city, d, lo, hi)):
        k = (b_lo, b_hi)
        if pick == -1 or k not in out:
            out[k] = p
    return out


def main():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    preds = pd.read_sql("SELECT * FROM ml_preds_wf", conn)
    win = {(r[0], r[1]): r[2] for r in conn.execute("SELECT city, local_date, win_lo FROM weather_poly_outcomes")}
    vol = {r[0]: r[1] for r in conn.execute("SELECT city, SUM(n) FROM poly_trades_days GROUP BY city")}
    top = set(sorted(vol, key=vol.get, reverse=True)[:16])
    calib = {}
    for variant, title in (("A", "цена: последняя за час ДО решения"), ("B", "цена: только свежая (≤10 мин до решения)"),
                           ("C", "цена: первая ПОСЛЕ решения (≤30 мин)")):
        bets = []
        for _, r in preds.iterrows():
            w = win.get((r["city"], r["date"]))
            if w is None:
                continue
            pr = prices(conn, r["city"], r["date"], variant)
            if len(pr) < 3:
                continue
            k = 9 / 5 if r["unit"] == "fahrenheit" else 1
            off = 32 if r["unit"] == "fahrenheit" else 0
            probs = {b: emos_bucket_prob(r["ml_mu_c"] * k + off, r["ml_sigma_c"] * k, b[0], b[1]) for b in pr}
            if variant == "A":
                for b, p in probs.items():
                    key = min(int(p * 10), 9)
                    c = calib.setdefault(key, [0, 0])
                    c[0] += 1
                    c[1] += b[0] == w
            b = max(pr, key=lambda x: probs[x] - pr[x])
            if probs[b] - pr[b] >= 0.10 and 0.03 <= pr[b] <= 0.95:
                won = b[0] == w
                bets.append({"date": r["date"], "city": r["city"], "price": pr[b], "won": won, "pnl": pnl(pr[b], won)})
        df = pd.DataFrame(bets)
        print(f"\n######## {title} ########")
        for per, g in (("июль-август", df[df["date"] < "2026-09-01"]), ("сентябрь (проверка)", df[df["date"] >= "2026-09-01"])):
            if g.empty:
                continue
            top5 = g.nlargest(5, "pnl")["pnl"].sum()
            boots = []
            vals = g["pnl"].tolist()
            random.seed(1)
            for _ in range(2000):
                boots.append(sum(random.choice(vals) for _ in vals))
            boots.sort()
            print(f"{per}: ставок {len(g)}, выиграно {int(g['won'].sum())}, итог {g['pnl'].sum():+.0f}$ "
                  f"({g['pnl'].mean():+.2f}/ставку); без 5 лучших выигрышей: {g['pnl'].sum() - top5:+.0f}$; "
                  f"95% интервал: {boots[50]:+.0f}…{boots[1950]:+.0f}$")
        if variant == "A":
            print("\nпо цене покупки (все месяцы):")
            for lo_, hi_ in ((0.03, 0.08), (0.08, 0.15), (0.15, 0.30), (0.30, 0.50), (0.50, 0.96)):
                g = df[(df["price"] >= lo_) & (df["price"] < hi_)]
                if len(g):
                    print(f"  {lo_*100:2.0f}-{hi_*100:2.0f}¢: ставок {len(g):4}, выиграно {100*g['won'].mean():4.1f}%, итог {g['pnl'].sum():+7.0f}$")
            print("по месяцам:")
            for m, g in df.groupby(df["date"].str[:7]):
                print(f"  {m}: ставок {len(g):4}, итог {g['pnl'].sum():+7.0f}$")
            print("по ликвидности города:")
            for name, g in (("16 самых ликвидных", df[df["city"].isin(top)]), ("остальные 32", df[~df["city"].isin(top)])):
                print(f"  {name}: ставок {len(g):4}, итог {g['pnl'].sum():+7.0f}$")
    print("\nкалибровка обучаемой модели (все бакеты, цена A): модель говорит → реально выигрывает")
    for key in sorted(calib):
        n, w = calib[key]
        print(f"  {key*10:2d}-{key*10+10:3d}%: {n:6} случаев, реально {100*w/n:5.1f}%")
    conn.close()


if __name__ == "__main__":
    main()
