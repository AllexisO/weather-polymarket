"""
Схема Poligarch на погоде (2026-09-29, Alex: «сделать своего бота по его стратегии»): свои заявки на покупку
ОБЕИХ сторон маркета («да» и «нет»), сумма цен = 1 − спред; купленные пары «да»+«нет» склеиваются в $1 (MERGE),
непарный остаток держится до итога. Перекос остатка ограничен (тяжёлую сторону не котируем).

Моделирование по настоящим сделкам (poly_trades, side — сторона того, кто забрал заявку), всё в ценах «да»:
  y = цена «да» (для сделок по «нет» — 1 − цена). (Yes,SELL) и (No,BUY) бьют заявки на покупку «да»;
  (Yes,BUY) и (No,SELL) — заявки на покупку «нет». Наша заявка «да» по b исполняется, только если сделка прошла
  СТРОГО ниже b (значит, все заявки по b и выше, включая нашу, уже съедены); «нет» по q — если y > 1 − q.
  Котировка — от предыдущей сделки (без заглядывания вперёд), до Q долей на сделку. Комиссии у своих заявок нет,
  выплаты мейкерам не учитываем (запас в нашу пользу). Очередь не учитываем полностью — строгое «ниже» частично это покрывает.
Порог (записан до прогона): лучший вариант, выбранный на 19.08-07.09, на 08.09-27.09 даёт ≥ +0.5% от потраченного,
и оба периода в плюсе. Только на копии базы.
"""
import sqlite3
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from weather_cities import OBS_CITIES

DB = "/data/research/research.sqlite3"
SPLIT = datetime(2026, 9, 8).timestamp()
conn = sqlite3.connect(DB)
final = dict(conn.execute("SELECT condition_id, final_yes FROM poly_market_final").fetchall())

VARIANTS = [(s, stop, L) for s in (0.02, 0.04, 0.06, 0.10) for stop in (0, 10, 24) for L in (30,)]
Q = 10.0
# 29.09 (шаг 3, оценка под награды за ликвидность): QSZ=100 MIDONLY=1 — по 100 долей, только средние варианты (10-90¢), перекос до 300
import os as _os
if _os.environ.get("QSZ"):
    Q = float(_os.environ["QSZ"])
    VARIANTS = [(0.02, 24, 300), (0.04, 24, 300)]
LO, HI = (0.10, 0.90) if _os.environ.get("MIDONLY") else (0.05, 0.95)
import os
LOOSE = os.environ.get("LOOSE") == "1"  # щедрый вариант: исполнение и при сделке ровно по нашей цене, доля в очереди 30%
SHARE = 0.3 if LOOSE else 1.0
EPS = 1e-9 if not LOOSE else -1e-9


def run_market(trades, fin, tz, ld, s, stop_h, L):
    """→ (потрачено, итог склеек, итог остатка) по периоду (0/1) первой сделки маркета."""
    day0 = datetime.fromisoformat(ld).replace(tzinfo=ZoneInfo(tz)).timestamp()
    stop_ts = day0 + stop_h * 3600
    ys = ns = 0.0      # непарные доли «да» / «нет»
    yc = nc = 0.0      # их стоимость
    spent = merged = 0.0
    last = None
    for ts, y, hits_yes_bids, size in trades:
        if last is not None and ts < stop_ts and LO <= last <= HI:
            b = round(last - s / 2, 2)
            q = round(1 - last - s / 2, 2)
            if hits_yes_bids and y < b - EPS and ys - ns < L and b > 0:
                k = min(size * SHARE, Q)
                ys += k; yc += k * b; spent += k * b
            elif not hits_yes_bids and y > 1 - q + EPS and ns - ys < L and q > 0:
                k = min(size * SHARE, Q)
                ns += k; nc += k * q; spent += k * q
            pairs = min(ys, ns)
            if pairs > 0:  # склейка по средней цене
                ay, an = yc / ys, nc / ns
                merged += pairs * (1 - ay - an)
                ys -= pairs; ns -= pairs; yc -= pairs * ay; nc -= pairs * an
        last = y
    inv = ys * fin - yc + ns * (1 - fin) - nc
    return spent, merged, inv


def main():
    res = {v: [[0.0, 0.0, 0.0, 0], [0.0, 0.0, 0.0, 0]] for v in VARIANTS}
    cur, rows, meta = None, [], None

    def flush():
        if not rows or cur not in final or meta[0] not in OBS_CITIES:
            return
        tr = sorted(set(rows))
        per = int(tr[0][0] >= SPLIT)
        for v in VARIANTS:
            sp, mg, inv = run_market(tr, final[cur], OBS_CITIES[meta[0]]["tz"], meta[1], *v)
            r = res[v][per]
            r[0] += sp; r[1] += mg; r[2] += inv; r[3] += sp > 0

    for cid, outc, side, price, size, ts, city, ld, tx in conn.execute(
            "SELECT condition_id, outcome, side, price, size, ts, city, local_date, tx FROM poly_trades ORDER BY condition_id, ts"):
        if cid != cur:
            flush()
            cur, rows, meta = cid, [], (city, ld)
        y = price if outc == "Yes" else 1 - price
        hits_yes_bids = (outc == "Yes" and side == "SELL") or (outc == "No" and side == "BUY")
        rows.append((ts, round(y, 4), hits_yes_bids, size))
    flush()
    print(f"{'спред':>6s} {'стоп':>12s} | {'19.08-07.09: потрачено':>22s} {'склейки':>8s} {'остаток':>8s} {'итог %':>7s} | {'08.09-27.09: потрачено':>22s} {'склейки':>8s} {'остаток':>8s} {'итог %':>7s}")
    for v in VARIANTS:
        out = f"{v[0]:6.2f} {('до 00:00 дня' if v[1] == 0 else f'до {v[1]}:00 дня'):>12s} |"
        for sp, mg, inv, n in res[v]:
            out += f" {sp:>14,.0f} ({n:4d} мк) {mg:+8.0f} {inv:+8.0f} {100 * (mg + inv) / max(sp, 1):+7.2f} |"
        print(out, flush=True)


main()
