"""
Знает ли цена Kalshi в 08:00 то, чего нет в цене Polymarket (2026-09-28, weather_kalshi_hist.py → kalshi_px).
7 городов с той же станцией. Шанс варианта Polymarket по Kalshi — доля распределения Kalshi, попавшая в его границы
(внутри корзины Kalshi — равномерно). Смесь: (1 − a)·Polymarket + a·Kalshi, a подбираем на июле-августе, проверяем
на сентябре (логошибка на выигравшем варианте Polymarket). Деньги: покупаем на Polymarket вариант, где Kalshi выше цены
на ≥ порог (подбор на июле-августе по цене 08:00, проверка — сентябрь по настоящим сделкам).
Порог решения (до запуска): смесь с Kalshi лучше цены Polymarket на ≥ 0.015 в сентябре, или правило в плюсе в обоих периодах.
Только на копии базы.
"""

import math

import weather_study_0926 as base

conn = base.conn


def kalshi_dist(city, d):
    rows = conn.execute("SELECT lo, hi, mid FROM kalshi_px WHERE city = ? AND local_date = ? AND mid IS NOT NULL",
                        (city, d)).fetchall()
    if len(rows) < 3:
        return None
    t = sum(r[2] for r in rows)
    if not 0.6 <= t <= 1.6:
        return None  # цены не складываются в распределение — пропуск
    return [(r[0], r[1], r[2] / t) for r in rows]


def prob(K, lo, hi):
    s = 0.0
    for a, b, p in K:
        a2, b2 = max(a, -200), min(b, 200)
        A, B = max(a2, max(lo, -200)), min(b2, min(hi, 200))
        if B > A and b2 > a2:
            s += p * (B - A) / (b2 - a2)
    return s


def load():
    out = []
    for city, d in conn.execute("SELECT DISTINCT city, local_date FROM kalshi_px ORDER BY local_date").fetchall():
        w = base.WIN.get((city, d))
        K = kalshi_dist(city, d)
        pr = base.chk.prices(conn, city, d, "A") if w is not None and K else {}
        if len(pr) < 3 or not any(b[0] == w for b in pr):
            continue
        keys = sorted(pr)
        tp = sum(pr.values()) or 1.0
        pk = [prob(K, b[0], b[1]) for b in keys]
        tk = sum(pk) or 1.0
        out.append({"city": city, "date": d, "keys": keys, "price": [pr[b] for b in keys],
                    "P": [pr[b] / tp for b in keys], "K": [x / tk for x in pk], "win": w})
    return out


def ll(days, a):
    s = 0.0
    for d in days:
        i = [b[0] for b in d["keys"]].index(d["win"])
        s += -math.log(max((1 - a) * d["P"][i] + a * d["K"][i], 1e-4))
    return s / len(days)


def rule(days, thr):
    n = won = nr = 0
    pnl = pr = st = 0.0
    for d in days:
        c = [(k - p, b, p) for b, p, k in zip(d["keys"], d["price"], d["K"]) if k - p >= thr and 0.03 <= p <= 0.95]
        if not c:
            continue
        e, b, p = max(c)
        w = b[0] == d["win"]
        n += 1; won += w; pnl += base.pnl(p, w)
        fp = base.real_fill(d["city"], d["date"], b[0], "yes", min(0.95, p + e / 2))
        if fp not in (None, "nodata"):
            nr += 1; pr += base.pnl(fp - 0.01, w); st += base.STAKE
    return n, won, pnl, nr, pr, st


if __name__ == "__main__":
    days = load()
    ja = [d for d in days if d["date"] < "2026-09-01"]
    se = [d for d in days if d["date"] >= "2026-09-01"]
    print(f"город-дней с ценами обеих площадок: июль-авг {len(ja)}, сентябрь {len(se)}; города: "
          f"{sorted({d['city'] for d in days})}", flush=True)
    grid = (0.0, 0.1, 0.2, 0.3, 0.5, 0.7, 1.0)
    sa = {a: ll(ja, a) for a in grid}
    best = min(sa, key=sa.get)
    print("июль-авг, логошибка по весу Kalshi: " + " ".join(f"{a}: {v:.4f}" for a, v in sa.items()), flush=True)
    print(f"выбран вес {best} | сентябрь: только Polymarket {ll(se, 0):.4f}, только Kalshi {ll(se, 1):.4f}, "
          f"смесь {best}: {ll(se, best):.4f} ({ll(se, best) - ll(se, 0):+.4f})", flush=True)
    for thr in (0.05, 0.10, 0.15):
        a, s = rule(ja, thr), rule(se, thr)
        print(f"покупка на Polymarket, где Kalshi выше цены на ≥{thr * 100:.0f} п.п. | июль-авг по цене 08:00: {a[0]} ставок, "
              f"угадано {a[1]}, {a[2]:+.1f}$ | сентябрь, настоящие сделки: {s[3]} ставок {s[4]:+.1f}$"
              + (f" ({s[4] / s[5] * 100:+.0f}%)" if s[5] else ""), flush=True)
