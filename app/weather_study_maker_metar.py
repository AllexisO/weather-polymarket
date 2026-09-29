"""
Мейкер и выход сводок METAR (2026-09-30, разбор Poligarch: его исполнения в минуты после сводок в минусе, в остальные — в плюсе;
он от них не уходит). Для каждого города — обычные минуты сводок (самые частые минуты valid_utc в station_obs за сентябрь).
Для каждой сделки — сколько минут прошло с последней плановой сводки; итог стоявшего с заявкой (как weather_study_maker_seg.py)
по этим минутам, отдельно 19.08-07.09 и 08.09-27.09. Порог (до прогона): «окно риска», выбранное на первом периоде, на проверке
даёт итог вне окна ≥ +0.5¢ на долю лучше, чем всё вместе. Только на копии.
"""
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime

from weather_cities import OBS_CITIES

DB = "/data/research/research.sqlite3"
SPLIT = datetime(2026, 9, 8).timestamp()
conn = sqlite3.connect(DB)
final = {r[0]: r[1:] for r in conn.execute("SELECT condition_id, final_yes, city, local_date FROM poly_market_final")}
mins = {}
for city in OBS_CITIES:
    c = Counter(int(r[0][14:16]) for r in conn.execute(
        "SELECT valid_utc FROM station_obs WHERE city = ? AND valid_utc >= '2026-09-01'", (city,)))
    tot = sum(c.values()) or 1
    mins[city] = sorted(m for m, n in c.items() if n / tot >= 0.15) or [0]
print("минуты сводок, примеры:", {k: mins[k] for k in list(mins)[:8]})
agg = defaultdict(lambda: [[0.0, 0.0, 0], [0.0, 0.0, 0]])  # доли, итог $, n
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
    mp, pay = (p, fin_o) if side == "SELL" else (1 - p, 1 - fin_o)
    m = datetime.utcfromtimestamp(ts).minute
    since = min((m - x) % 60 for x in mins[f[1]])
    a = agg[since][int(ts >= SPLIT)]
    a[0] += size; a[1] += size * (pay - mp + 0.25 * 0.05 * p * (1 - p)); a[2] += 1
print("минут после плановой сводки | 19.08-07.09: ¢ на долю (сделок) | 08.09-27.09")
for b in range(0, 60, 3):
    r = [[sum(agg[s][per][i] for s in range(b, b + 3)) for i in range(3)] for per in (0, 1)]
    print(f"  {b:2d}-{b+2:2d}   {100*r[0][1]/max(r[0][0],1):+6.2f}¢ ({r[0][2]:7.0f}) | {100*r[1][1]/max(r[1][0],1):+6.2f}¢ ({r[1][2]:7.0f})")
def outside(per, lo, hi):
    ks = [s for s in agg if not (lo <= s < hi)]
    sh = sum(agg[s][per][0] for s in ks); pn = sum(agg[s][per][1] for s in ks)
    return 100 * pn / max(sh, 1)
allv = [100 * sum(agg[s][per][1] for s in agg) / max(sum(agg[s][per][0] for s in agg), 1) for per in (0, 1)]
best = max(((lo, hi) for lo in range(0, 30) for hi in range(lo + 3, min(lo + 31, 61))), key=lambda w: outside(0, *w) - 0.02 * (w[1] - w[0]))
print(f"\nвсё вместе: {allv[0]:+.2f}¢ | {allv[1]:+.2f}¢")
print(f"окно риска по первому периоду: {best[0]}-{best[1]} мин после сводки; вне окна: {outside(0, *best):+.2f}¢ | ПРОВЕРКА {outside(1, *best):+.2f}¢")
print("ПОРОГ:", "ПРОШЛО" if outside(1, *best) - allv[1] >= 0.5 else "не прошло")
