"""
Где на погоде выгодно стоять с заявкой (2026-09-30, разбор Poligarch: 95% его покупок на погоде — его заявки, которые забрали;
покупает на 3.6¢ ниже последней сделки, к итогу +1¢ на долю; хорошо — 30-70¢ и 9-12 местного, плохо — 10-30¢ и вечер).
Каждая сделка в poly_trades — исполнение чьей-то заявки на другой стороне. Итог того, кто стоял с заявкой, на долю:
  забирающий купил «да» по p → мейкер купил «нет» по 1 − p; забирающий продал «да» по p → мейкер купил «да» по p (для «нет» — зеркально);
  итог = выплата − цена + возврат 25% комиссии забирающего (0.25 × 0.05 × p × (1 − p)). Мейкер без комиссии.
Разрезы: цена купленной мейкером стороны, накануне / день маркета, местный час, размер сделки. Выбор разрезов — на 19.08-07.09
(итог ≥ +1¢ на долю, ≥ 2000 сделок), проверка — 08.09-27.09. Порог (до прогона): выбранные вместе ≥ +1¢ на долю и ≥ +1% от
потраченного на проверке. Только на копии.
"""
import sqlite3
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from weather_cities import OBS_CITIES

DB = "/data/research/research.sqlite3"
SPLIT = datetime(2026, 9, 8).timestamp()
conn = sqlite3.connect(DB)
final = {r[0]: r[1:] for r in conn.execute("SELECT condition_id, final_yes, city, local_date FROM poly_market_final")}
PB = ((0, .1), (.1, .3), (.3, .5), (.5, .7), (.7, .9), (.9, 1.01))
HB = (("накануне", None), ("0-6", (0, 6)), ("6-9", (6, 9)), ("9-12", (9, 12)), ("12-15", (12, 15)), ("15-18", (15, 18)), ("18-24", (18, 24)))
SB = ((0, 10), (10, 50), (50, 1e9))
agg = defaultdict(lambda: [[0, 0.0, 0.0, 0.0], [0, 0.0, 0.0, 0.0]])  # n, доли, итог $, потрачено $
seen = set()
for cid, outc, side, p, size, ts, tx in conn.execute("SELECT condition_id, outcome, side, price, size, ts, tx FROM poly_trades"):
    k = (tx, cid, outc, side, p, size, ts)
    if k in seen:
        continue
    seen.add(k)
    f = final.get(cid)
    if not f or f[1] not in OBS_CITIES or not (0 < p < 1):
        continue
    fin_o = f[0] if outc == "Yes" else 1 - f[0]
    if side == "SELL":           # мейкер купил эту же сторону по p
        mp, pay = p, fin_o
    else:                        # мейкер купил противоположную сторону по 1 − p
        mp, pay = 1 - p, 1 - fin_o
    pnl = size * (pay - mp + 0.25 * 0.05 * p * (1 - p))
    loc = datetime.fromtimestamp(ts, ZoneInfo(OBS_CITIES[f[1]]["tz"]))
    hb = "накануне" if loc.date().isoformat() < f[2] else next((n for n, r in HB[1:] if r[0] <= loc.hour < r[1]), "18-24")
    pb = next(i for i, (a, b) in enumerate(PB) if a <= mp < b)
    usd = size * mp
    sb = next(i for i, (a, b) in enumerate(SB) if a <= usd < b)
    a = agg[(pb, hb, sb)][int(ts >= SPLIT)]
    a[0] += 1; a[1] += size; a[2] += pnl; a[3] += usd


def tot(keys, per):
    n = sum(agg[k][per][0] for k in keys); sh = sum(agg[k][per][1] for k in keys)
    pn = sum(agg[k][per][2] for k in keys); us = sum(agg[k][per][3] for k in keys)
    return n, 100 * pn / max(sh, 1), 100 * pn / max(us, 1), pn


allk = list(agg)
for per, name in ((0, "19.08-07.09"), (1, "08.09-27.09")):
    n, c, r, pn = tot(allk, per)
    print(f"все исполнения {name}: {n} сделок, мейкер {c:+.2f}¢ на долю, {r:+.2f}% от потраченного, итог ${pn:+,.0f}")
print("\nразрезы (цена стороны мейкера × время):  19.08-07.09 | 08.09-27.09")
for pb in range(len(PB)):
    for hb, _ in HB:
        ks = [k for k in allk if k[0] == pb and k[1] == hb]
        a, b = tot(ks, 0), tot(ks, 1)
        print(f"  {int(PB[pb][0]*100):3d}-{int(min(PB[pb][1],1)*100):3d}¢ {hb:9s} n={a[0]:6d} {a[1]:+5.2f}¢ {a[2]:+6.2f}% | n={b[0]:6d} {b[1]:+5.2f}¢ {b[2]:+6.2f}%")
sel = [k for k in allk if agg[k][0][0] >= 2000 and 100 * agg[k][0][2] / max(agg[k][0][1], 1) >= 1.0]
print(f"\nвыбрано на 19.08-07.09 (≥ +1¢, ≥ 2000 сделок): {len(sel)} разрезов (цена × время × размер)")
for per, name in ((0, "подбор 19.08-07.09"), (1, "ПРОВЕРКА 08.09-27.09")):
    n, c, r, pn = tot(sel, per)
    print(f"  {name}: {n} сделок, {c:+.2f}¢ на долю, {r:+.2f}% от потраченного, итог мейкеров ${pn:+,.0f}")
n, c, r, pn = tot(sel, 1)
print("ПОРОГ:", "ПРОШЛО" if c >= 1 and r >= 1 else "не прошло")
