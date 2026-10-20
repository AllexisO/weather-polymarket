"""
Варианты кошелька fav (15.10, своя идея Claude: fav — единственный кошелёк в плюсе на всех трёх отрезках: июль-авг +12.1%,
с 21.08 по сделкам +19.6%, вживую с 28.09 +34.5% на 29 ставках — расширить без подгонки).
Правило fav: в 08:00 «да» на вариант с ценой в полосе, у которого смесь v3 + рынок > цены + перевес; одна ставка на город-день.
Перебор: полоса цены × минимальный перевес.
Отрезки: A — 01.07-20.08 (проверка вслепую v3, цена 08:00 + 1¢); B — 21.08-24.09 (то же, по настоящим сделкам);
C — 25.09-12.10 (живая смесь ml3c из снимков 08:00, по настоящим сделкам).
ПОРОГ (записан до прогона): вариант выбирается ТОЛЬКО по A (лучший итог при ≥ 150 ставках); принимается, если он лучше
текущего fav (50-95¢, перевес 0) на A И на B, и на C ≥ +5%. Иначе fav остаётся как есть.
Запуск: docker compose run --rm -e JOB_TIMEOUT=0 -e POLY_LAB_DB=/data/research/research.sqlite3 collector weather_study_fav_var.py
"""
import collections
from datetime import datetime
from zoneinfo import ZoneInfo

import weather_study_0926 as base
from weather_cities import OBS_CITIES

conn = base.conn
BANDS = [(0.50, 0.95), (0.40, 0.95), (0.30, 0.95), (0.65, 0.95), (0.50, 0.65), (0.40, 0.65)]
THRS = [0.0, 0.03, 0.06]


def live_days():
    """C: живые снимки 08:00 со смесью ml3c."""
    rows = collections.defaultdict(dict)
    for city, d, lo, hi, m, b in conn.execute(
            "SELECT city, local_date, bucket_lo, bucket_hi, market_p, ml3c_model_p FROM snapshots WHERE local_hour = 8 "
            "AND local_date BETWEEN '2026-09-25' AND '2026-10-12' AND market_p IS NOT NULL AND ml3c_model_p IS NOT NULL ORDER BY ts_utc"):
        rows[(city, d)][(lo, hi)] = (m, b)
    out = []
    for (city, d), bk in rows.items():
        w = base.WIN.get((city, d))
        if w is None or len(bk) < 3:
            continue
        keys = sorted(bk)
        out.append({"city": city, "date": d, "keys": keys, "price": [bk[k][0] for k in keys],
                    "blend": {k: bk[k][1] for k in keys}, "win": w})
    return out


def run(days, lo, hi, thr, real):
    n = won = 0
    st = pnl = 0.0
    for d in days:
        c = [(d["blend"][b] - p, b, p) for b, p in zip(d["keys"], d["price"]) if lo <= p < hi and d["blend"][b] - p >= thr and d["blend"][b] > p]
        if not c:
            continue
        _, b, p = max(c)
        w = b[0] == d["win"]
        if real:
            fp = base.real_fill(d["city"], d["date"], b[0], "yes", min(0.97, p + 0.01))
            if fp in (None, "nodata"):
                continue
            r = base.pnl(fp - 0.01, w)
        else:
            r = base.pnl(p, w)
        n += 1; won += w; pnl += r; st += base.STAKE
    return n, won, (100 * pnl / st if st else 0.0)


days = base.load_days()
A = [d for d in days if d["date"] < "2026-08-21"]
B = [d for d in days if "2026-08-21" <= d["date"] <= "2026-09-24"]
C = live_days()
print(f"дней: A {len(A)}, B {len(B)}, C {len(C)}")
res = {}
for lo, hi in BANDS:
    for thr in THRS:
        a, b, c = run(A, lo, hi, thr, False), run(B, lo, hi, thr, True), run(C, lo, hi, thr, True)
        res[(lo, hi, thr)] = (a, b, c)
        print(f"{lo * 100:.0f}-{hi * 100:.0f}¢ перевес {thr * 100:.0f} п.п. | A {a[0]:4d} ставок {a[2]:+6.1f}% | B {b[0]:4d} {b[2]:+6.1f}% | C {c[0]:4d} {c[2]:+6.1f}%")
cur = res[(0.50, 0.95, 0.0)]
best = max((k for k in res if res[k][0][0] >= 150), key=lambda k: res[k][0][2])
a, b, c = res[best]
ok = best != (0.50, 0.95, 0.0) and a[2] > cur[0][2] and b[2] > cur[1][2] and c[2] >= 5
print(f"лучший по A: {best[0] * 100:.0f}-{best[1] * 100:.0f}¢, перевес {best[2] * 100:.0f} п.п. → "
      f"{'ПРИНЯТЬ (лучше fav на A и B, C ≥ +5%)' if ok else 'не прошло — fav остаётся как есть'}")
