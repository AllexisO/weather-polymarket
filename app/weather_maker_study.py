"""
Выгодно ли быть стороной, ВЫСТАВЛЯЮЩЕЙ заявки (maker), на погодных
маркетах — по полной истории сделок (poly_trades, см.
weather_trades_history.py). 2026-09-25, исследование.

Для каждой сделки (с точки зрения taker'а из data-api):
- taker BUY исхода X по p: taker получает payoff(X) − p за долю;
- taker SELL исхода X по p: taker получает p − payoff(X);
- maker — ровно противоположное, плюс возврат части комиссии
  (rebateRate 0.25 от комиссии taker'а: 0.05 × p × (1 − p) за долю).
payoff(Yes) = финальная цена Yes (1/0), payoff(No) = 1 − она.
Деньги maker'а под риском ("вложено"): p, если он купил X по p;
1 − p, если продал X по p (то есть купил противоположный исход).

Разбивки: цена, местный час сделки, размер сделки, объём торгов города.
Итог "на вложенный доллар" — сколько maker зарабатывает с каждого
доллара, которым он рискует.

ВАЖНО (почему это оценка сверху, а не обещание): в реальности наши
заявки исполнялись бы не на всех сделках поровну. Профессиональные
мейкеры успевают снять заявки, когда приходит новая информация
(свежая сводка, прогноз), и тогда исполняются как раз наши "устаревшие"
заявки — худшие сделки. Поэтому если maker в среднем зарабатывает мало,
нам, скорее всего, будет в минус; если заметно — стоит проверять дальше.

Запуск: python weather_maker_study.py
"""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from weather_cities import OBS_CITIES

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
FEE_RATE, REBATE = 0.05, 0.25


def rows(conn):
    finals = {r[0]: r[1] for r in conn.execute("SELECT condition_id, final_yes FROM poly_market_final")}
    for tx, cid, outcome, side, p, size, ts, city, d in conn.execute(
            "SELECT tx, condition_id, outcome, side, price, size, ts, city, local_date FROM poly_trades"):
        y = finals.get(cid)
        if y is None or not (0 < p < 1):
            continue
        payoff = y if outcome == "Yes" else 1 - y
        taker = (payoff - p) if side == "BUY" else (p - payoff)
        fee = FEE_RATE * p * (1 - p)
        maker = -taker + REBATE * fee
        invested = p if side == "SELL" else 1 - p   # maker купил X по p / купил противоположное по 1-p
        maker_price = p if side == "SELL" else 1 - p  # цена позиции maker'а
        hour = datetime.fromtimestamp(ts, timezone.utc).astimezone(ZoneInfo(OBS_CITIES[city]["tz"])).hour
        yield {"maker": maker * size, "taker_after_fee": (taker - fee) * size, "inv": invested * size,
               "maker_price": maker_price, "hour": hour, "size_usd": p * size, "city": city, "date": d}


def table(title, groups):
    print(f"\n== {title} ==")
    print(f"{'группа':22} {'сделок':>7} {'оборот $':>10} {'maker итог $':>13} {'на вложенный $':>15} {'taker после комиссии $':>23}")
    for name, g in groups:
        if not g["n"]:
            continue
        print(f"{name:22} {g['n']:7} {g['inv']:10.0f} {g['maker']:+13.0f} {100 * g['maker'] / g['inv']:+14.1f}% {g['taker']:+23.0f}")


def main():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    vol = {r[0]: r[1] for r in conn.execute("SELECT city, SUM(n) FROM poly_trades_days GROUP BY city")}
    top = set(sorted(vol, key=vol.get, reverse=True)[:16])
    buckets = {k: {} for k in ("all", "price", "hour", "size", "tier", "half")}
    def add(kind, key, r):
        g = buckets[kind].setdefault(key, {"n": 0, "inv": 0.0, "maker": 0.0, "taker": 0.0})
        g["n"] += 1
        g["inv"] += r["inv"]
        g["maker"] += r["maker"]
        g["taker"] += r["taker_after_fee"]
    for r in rows(conn):
        add("all", "все сделки", r)
        mp = r["maker_price"]
        add("price", next(f"{a}-{b}¢" for a, b in ((0, 5), (5, 15), (15, 35), (35, 65), (65, 85), (85, 95), (95, 101)) if a <= mp * 100 < b), r)
        add("hour", next(f"{a:02d}-{b:02d} ч" for a, b in ((0, 6), (6, 10), (10, 13), (13, 16), (16, 19), (19, 24)) if a <= r["hour"] < b), r)
        add("size", next(f"сделка {n}" for lim, n in ((5, "до $5"), (25, "$5-25"), (100, "$25-100"), (1e9, "больше $100")) if r["size_usd"] < lim), r)
        add("tier", "16 самых ликвидных" if r["city"] in top else "остальные 32", r)
        add("half", "первая половина" if r["date"] < "2026-09-10" else "вторая половина", r)
    order = lambda d: sorted(d.items())
    table("Все сделки", order(buckets["all"]))
    table("По цене позиции maker'а", sorted(buckets["price"].items(), key=lambda kv: int(kv[0].split("-")[0])))
    table("По местному часу сделки", order(buckets["hour"]))
    table("По размеру сделки", order(buckets["size"]))
    table("По ликвидности города", order(buckets["tier"]))
    table("По времени (устойчивость)", order(buckets["half"]))
    conn.close()


if __name__ == "__main__":
    main()
