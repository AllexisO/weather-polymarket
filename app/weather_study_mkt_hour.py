"""
Как быстро рынок узнаёт итог в течение ночи и утра (2026-09-28, «решать раньше 08:00»). Логошибка цены рынка на
выигравшем варианте по часам местного времени (накануне 12:00 … сегодня 12:00), одни и те же город-дни (все часы с
ценами), август-сентябрь 2026. Где логошибка резко падает — в этот час рынок получает новую информацию.
Только на копии базы.
"""
import math
import weather_ml_check as chk
import weather_study_0926 as base

HOURS = [-12, -8, -4, -2, 0, 2, 4, 5, 6, 7, 8, 9, 10, 12]
conn = base.conn
from weather_cities import OBS_CITIES
days = [(c, d, w) for (c, d), w in base.WIN.items() if "2026-08-01" <= d <= "2026-09-25" and c in OBS_CITIES]
cov = {}
for h in HOURS:
    chk.DECISION_HOUR = h
    cov[h] = sum(1 for c, d, w in days[:400] if len(chk.prices(conn, c, d, "A")) >= 3)
print("есть цены (из первых 400 город-дней): " + " ".join(f"{h}:{n}" for h, n in cov.items()))
HOURS = [h for h in HOURS if cov[h] >= 300]
res = {h: [] for h in HOURS}
keep = 0
for c, d, w in days:
    row = {}
    for h in HOURS:
        chk.DECISION_HOUR = h
        pr = chk.prices(conn, c, d, "A")
        wb = next((b for b in pr if b[0] == w), None)
        if len(pr) < 3 or wb is None:
            break
        row[h] = -math.log(max(pr[wb] / (sum(pr.values()) or 1), 1e-4))
    else:
        keep += 1
        for h in HOURS:
            res[h].append(row[h])
print(f"город-дней с ценами во все часы: {keep}")
prev = None
for h in HOURS:
    v = sum(res[h]) / len(res[h])
    lbl = f"накануне {24 + h:02d}:00" if h < 0 else f"{h:02d}:00"
    print(f"{lbl:15s} логошибка рынка {v:.4f}" + (f"  ({v - prev:+.4f} за шаг)" if prev is not None else ""))
    prev = v
