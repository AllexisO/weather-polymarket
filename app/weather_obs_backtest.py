"""
Бэктест стратегии по живым замерам (weather_obs_live.py) на истории цен
Polymarket (price_history, шаг 5 минут) и METAR станций (station_obs).
Разовый скрипт, ничего не пишет — печатает итог.

Те же правила, что у живого кошелька, плюс честные допущения:
- сводка становится известна через DELAY_MIN минут после времени
  наблюдения (публикация METAR + наша реакция);
- цена бакета в момент решения — последняя цена из истории не старше
  STALE_MIN минут (иначе торговли не было — не торгуем);
- история даёт цену сделки/середину, а не заявку: продаём Yes по p - SPREAD,
  то есть No стоит 1 - p + SPREAD;
- без защиты от сбойных сводок (как и вживую).

Запуск: python weather_obs_backtest.py [DELAY_MIN ...]
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from weather_cities import OBS_CITIES as CITIES

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
STAKE = 5.0
MIN_BID = 0.05
SPREAD = 0.01
STALE_MIN = 30


def run_delay(conn, delay_min, verbose=False):
    total = {}
    losses = []
    for city, cfg in CITIES.items():
        tz = ZoneInfo(cfg["tz"])
        obs_by_day = {}
        for r in conn.execute("SELECT valid_utc, tmpf FROM station_obs WHERE city = ? AND tmpf IS NOT NULL", (city,)):
            t = datetime.fromisoformat(r["valid_utc"]).replace(tzinfo=timezone.utc)
            v = round(r["tmpf"]) if cfg["unit"] == "fahrenheit" else round((r["tmpf"] - 32) * 5 / 9)
            obs_by_day.setdefault(t.astimezone(tz).date().isoformat(), []).append((t, v))
        days = conn.execute(
            """
            SELECT d.local_date, o.win_lo FROM price_history_days d
            JOIN weather_poly_outcomes o ON o.city = d.city AND o.local_date = d.local_date
            WHERE d.city = ?
            """,
            (city,),
        ).fetchall()
        st = total.setdefault(city, {"n": 0, "won": 0, "pnl": 0.0, "days": 0})
        for day in days:
            prices = {}
            for r in conn.execute(
                "SELECT bucket_lo, bucket_hi, t_utc, p FROM price_history WHERE city = ? AND local_date = ? ORDER BY t_utc",
                (city, day["local_date"]),
            ):
                prices.setdefault((r["bucket_lo"], r["bucket_hi"]), []).append((r["t_utc"], r["p"]))
            obs = sorted(obs_by_day.get(day["local_date"], []))
            if not prices or not obs:
                continue
            st["days"] += 1
            running, traded = None, set()
            for t, v in obs:
                if running is not None and v <= running:
                    continue
                running = v
                decide = t + timedelta(minutes=delay_min)
                ts = decide.timestamp()
                for (lo, hi), series in prices.items():
                    if hi >= running or (lo, hi) in traded:
                        continue
                    last = [p for (pt, p) in series if pt <= ts and pt >= ts - STALE_MIN * 60]
                    if not last or last[-1] < MIN_BID:
                        continue
                    p = last[-1]
                    price = 1 - p + SPREAD
                    won = lo != day["win_lo"]
                    pnl = STAKE / price - STAKE if won else -STAKE
                    traded.add((lo, hi))
                    st["n"] += 1
                    st["won"] += won
                    st["pnl"] += pnl
                    if not won:
                        losses.append((city, day["local_date"], running, lo, hi, p))
    return total, losses


def main():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    delays = [int(a) for a in sys.argv[1:]] or [10, 30, 60]
    for delay in delays:
        total, losses = run_delay(conn, delay)
        n = sum(v["n"] for v in total.values())
        won = sum(v["won"] for v in total.values())
        pnl = sum(v["pnl"] for v in total.values())
        print(f"\n=== задержка реакции {delay} мин: ставок {n}, выиграно {won}, итог {pnl:+.2f}$")
        for city, v in total.items():
            print(f"   {city:8} дней {v['days']:3}  ставок {v['n']:3}  выиграно {v['won']:3}  итог {v['pnl']:+8.2f}$")
        for l in losses:
            print(f"   проигрыш: {l[0]} {l[1]} станция {l[2]}, бакет {l[3]}..{l[4]} стоил {l[5]:.2f}")
    conn.close()


if __name__ == "__main__":
    main()
