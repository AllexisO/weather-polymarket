"""
Схема Poligarch на погоде, шаг 2 (2026-09-30, Alex: «давай»): то же, что weather_study_mm2.py, но
  1) котировка от СВОЕЙ честной цены — смесь v3 + рынок (35/65) на 08:00 дня маркета (проверка вслепую: ml_preds_var_mkt,
     модель училась только на прошлом), а не от последней сделки; котируем с 08:00 до часа остановки;
     варианты центра: «fv» — честная цена, «mix» — середина между честной ценой и последней сделкой, «last» — как в mm2;
  2) возврат мейкеру 25% комиссии забирающего (feeSchedule погодных маркетов: 0.05 × цена × (1 − цена) × доли, rebateRate 0.25).
Награды за ликвидность (пул на средние варианты) по истории не посчитать — отдельно, по текущему пулу.
Исполнение — строгое (как mm2) и щедрое (LOOSE=1: сделка ровно по нашей цене, 30% очереди).
Порог (записан до прогона, как в mm2): лучший вариант 19.08-07.09 даёт на 08.09-27.09 ≥ +0.5% от потраченного, оба периода в плюсе.
"""
import json
import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import weather_ml_check as chk
import weather_ml_q as mq
from weather_cities import OBS_CITIES
from weather_ml_live import blend_with_market

DB = "/data/research/research.sqlite3"
SPLIT = datetime(2026, 9, 8).timestamp()
LOOSE = os.environ.get("LOOSE") == "1"
SHARE, EPS = (0.3, -1e-9) if LOOSE else (1.0, 1e-9)
Q, L = 10.0, 30
REBATE = 0.25 * 0.05
conn = sqlite3.connect(DB)
final = {r[0]: r[1:] for r in conn.execute("SELECT condition_id, final_yes, city, local_date, bucket_lo, bucket_hi FROM poly_market_final")}


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
        bl = blend_with_market([m / tm for m in model], [pr[b] for b in keys])
        for b, p in zip(keys, bl):
            fv[(city, d, b[0], b[1])] = p
    return fv


VARIANTS = [(c, s, stop) for c in ("fv", "mix", "last") for s in (0.02, 0.04, 0.06) for stop in (12, 16)]


def run_market(trades, fin, t0, t1, fvp, center, s):
    ys = ns = yc = nc = spent = merged = reb = 0.0
    last = None
    for ts, y, hits_yes_bids, size in trades:
        if last is not None and t0 <= ts < t1:
            c = fvp if center == "fv" else (last if center == "last" else (fvp + last) / 2)
            if 0.05 <= c <= 0.95:
                b, q = round(c - s / 2, 2), round(1 - c - s / 2, 2)
                if hits_yes_bids and y < b - EPS and ys - ns < L and b > 0:
                    k = min(size * SHARE, Q)
                    ys += k; yc += k * b; spent += k * b; reb += REBATE * b * (1 - b) * k
                elif not hits_yes_bids and y > 1 - q + EPS and ns - ys < L and q > 0:
                    k = min(size * SHARE, Q)
                    ns += k; nc += k * q; spent += k * q; reb += REBATE * q * (1 - q) * k
                pairs = min(ys, ns)
                if pairs > 0:
                    ay, an = yc / ys, nc / ns
                    merged += pairs * (1 - ay - an)
                    ys -= pairs; ns -= pairs; yc -= pairs * ay; nc -= pairs * an
        last = y
    return spent, merged, ys * fin - yc + ns * (1 - fin) - nc, reb


def main():
    fv = fair_values()
    print(f"честных цен (город-день-вариант): {len(fv)}; исполнение: {'щедрое' if LOOSE else 'строгое'}", flush=True)
    res = {v: [[0.0] * 5, [0.0] * 5] for v in VARIANTS}
    cur, rows = None, []

    def flush():
        if not rows or cur not in final:
            return
        fin, city, ld, lo, hi = final[cur]
        p = fv.get((city, ld, lo, hi))
        if p is None or city not in OBS_CITIES:
            return
        tr = sorted(set(rows))
        day0 = datetime.fromisoformat(ld).replace(tzinfo=ZoneInfo(OBS_CITIES[city]["tz"])).timestamp()
        per = int(day0 >= SPLIT)
        for v in VARIANTS:
            sp, mg, inv, rb = run_market(tr, fin, day0 + 8 * 3600, day0 + v[2] * 3600, p, v[0], v[1])
            r = res[v][per]
            r[0] += sp; r[1] += mg; r[2] += inv; r[3] += rb; r[4] += sp > 0

    for cid, outc, side, price, size, ts in conn.execute(
            "SELECT condition_id, outcome, side, price, size, ts FROM poly_trades ORDER BY condition_id, ts"):
        if cid != cur:
            flush()
            cur, rows = cid, []
        y = price if outc == "Yes" else 1 - price
        rows.append((ts, round(y, 4), (outc == "Yes" and side == "SELL") or (outc == "No" and side == "BUY"), size))
    flush()
    print(f"{'центр':>5s} {'спред':>5s} {'до':>3s} | 19.08-07.09: {'потрачено':>9s} {'склейки':>8s} {'остаток':>8s} {'возврат':>7s} {'итог%':>6s} | 08.09-27.09: {'потрачено':>9s} {'склейки':>8s} {'остаток':>8s} {'возврат':>7s} {'итог%':>6s}")
    for v in VARIANTS:
        out = f"{v[0]:>5s} {v[1]:5.2f} {v[2]:3d} |"
        for sp, mg, inv, rb, n in res[v]:
            out += f"             {sp:9,.0f} {mg:+8.0f} {inv:+8.0f} {rb:+7.0f} {100 * (mg + inv + rb) / max(sp, 1):+6.2f} |"
        print(out, flush=True)


main()
