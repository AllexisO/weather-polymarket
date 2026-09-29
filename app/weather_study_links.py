"""
Идеи из ссылок Alex (2026-09-29): правило «как у gopfan2», «лесенка» соседних вариантов, фильтр по объёму маркета.
Июль-авг — по цене 08:00, с 21.08 — по настоящим сделкам. Порог: плюс после комиссии в обоих периодах. Только на копии.
"""
import weather_study_0926 as base

conn = base.conn


def buy(d, b, side, p, stake=base.STAKE):
    w = (b[0] == d["win"]) if side == "yes" else (b[0] != d["win"])
    fp = base.real_fill(d["city"], d["date"], b[0], side, min(0.97, p + 0.01))
    real = None if fp in (None, "nodata") else base.pnl(fp - 0.01, w, stake)
    return base.pnl(p, w, stake), real, w


def run(days, pick):
    n = won = nr = 0
    pnl = pr = st = stk = 0.0
    for d in days:
        for b, side, p, stake in pick(d):
            a, r, w = buy(d, b, side, p, stake)
            n += 1; won += w; pnl += a; stk += stake
            if r is not None:
                nr += 1; pr += r; st += stake
    return n, won, pnl, stk, nr, pr, st


def gop_yes(d):  # «да» < 15¢ на варианте, который смесь считает самым вероятным
    b = max(d["keys"], key=lambda k: d["blend"][k])
    p = d["price"][d["keys"].index(b)]
    return [(b, "yes", p, 2.0)] if 0.03 <= p < 0.15 else []


def gop_no(d):  # «нет» дороже 45¢ (цена «да» ≤ 55¢), где смесь считает вариант переоценённым; одна на день — самая переоценённая
    c = [(p - d["blend"][b], b, p) for b, p in zip(d["keys"], d["price"]) if 1 - p > 0.45 and 1 - p <= 0.95 and d["blend"][b] < p - 0.03]
    if not c:
        return []
    _, b, p = max(c)
    return [(b, "no", 1 - p, 2.0)]


def ladder(k, max_cost):
    def pick(d):  # k соседних вариантов вокруг самого вероятного по смеси, если вместе они дешевле max_cost и смесь даёт им больше
        keys = d["keys"]
        i = max(range(len(keys)), key=lambda j: d["blend"][keys[j]])
        lo = max(0, min(i - (k - 1) // 2, len(keys) - k))
        sel = keys[lo:lo + k]
        cost = sum(d["price"][keys.index(b)] for b in sel)
        if not (0.05 < cost < max_cost) or sum(d["blend"][b] for b in sel) < cost + 0.05:
            return []
        return [(b, "yes", d["price"][keys.index(b)], 2.0 / k) for b in sel]
    return pick


def show(name, pick, ja, late):
    a, s = run(ja, pick), run(late, pick)
    print(f"{name:52s} | июль-авг по цене 08:00: {a[0]:5d} ставок, {a[2]:+7.1f}$ ({a[2] / max(a[3], 1) * 100:+5.1f}%) | "
          f"с 21.08 по сделкам: {s[4]:4d} ставок {s[5]:+7.1f}$ ({s[5] / max(s[6], 1) * 100:+5.1f}%)", flush=True)


if __name__ == "__main__":
    days = base.load_days()
    ja = [d for d in days if "2026-07-01" <= d["date"] < "2026-09-01"]
    late = [d for d in days if d["date"] >= base.FIRST_TRADE]
    print("=== правило «как у gopfan2» ===")
    show("«да» < 15¢ на самом вероятном варианте смеси", gop_yes, ja, late)
    show("«нет» > 45¢, где смесь ниже цены на 3+ п.п.", gop_no, ja, late)
    print("\n=== «лесенка» вокруг самого вероятного варианта смеси ($2 на лесенку) ===")
    for k in (2, 3):
        for mc in (0.5, 0.8):
            show(f"{k} варианта, вместе дешевле {mc * 100:.0f}¢", ladder(k, mc), ja, late)
    print("\n=== объём маркета: смесь 3 п.п. (как ml3_cal), fav, no_cheap — по третям объёма, с 22.08 по сделкам ===")
    vol = {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT city, local_date, MAX(event_vol) FROM snapshots WHERE local_hour BETWEEN 7 AND 9 GROUP BY 1, 2")}
    vd = [d for d in late if vol.get((d["city"], d["date"]))]
    vs = sorted(vol[(d["city"], d["date"])] for d in vd)
    t1, t2 = vs[len(vs) // 3], vs[2 * len(vs) // 3]
    import weather_study_longshot as ls
    import weather_study_structure as sst
    for lbl, cond in (("мелкие", lambda v: v < t1), ("средние", lambda v: t1 <= v < t2), ("крупные", lambda v: v >= t2)):
        part = [d for d in vd if cond(vol[(d["city"], d["date"])])]
        r = base.run_rule(part, "blend", 0.03)
        f = sst.fav(part, 0.5, 0.95)
        nc = ls.run_one(part, 0.05, 0.15)
        print(f"{lbl:8s} (объём ${min(vol[(d['city'], d['date'])] for d in part):,.0f}–${max(vol[(d['city'], d['date'])] for d in part):,.0f}, {len(part)} дн.): "
              f"смесь {r['n_real']} ст. {r['pnl_real']:+.1f}$ ({r['pnl_real'] / max(r['staked_real'], 1) * 100:+.0f}%) | "
              f"fav {f[3]} ст. {f[4]:+.1f}$ ({f[4] / max(f[5], 1) * 100:+.0f}%) | no_cheap {nc[3]} ст. {nc[4]:+.1f}$ ({nc[4] / max(nc[5], 1) * 100:+.1f}%)",
              flush=True)


def no_band(lo, hi, gap):
    def pick(d):
        c = [(p - d["blend"][b], b, p) for b, p in zip(d["keys"], d["price"]) if lo <= p < hi and d["blend"][b] < p - gap]
        if not c:
            return []
        _, b, p = max(c)
        return [(b, "no", 1 - p, 2.0)]
    return pick


if __name__ == "__main__" and "--no" in __import__("sys").argv:
    days = base.load_days()
    ja = [d for d in days if "2026-07-01" <= d["date"] < "2026-09-01"]
    late = [d for d in days if d["date"] >= base.FIRST_TRADE]
    print("\n=== «нет» по полосам цены «да» (смесь ниже цены на gap) — есть ли эффект вне no_cheap (5-15¢) ===")
    for lo, hi in ((0.05, 0.15), (0.15, 0.30), (0.30, 0.55), (0.15, 0.55)):
        for gap in (0.0, 0.03):
            show(f"«нет», цена «да» {lo * 100:.0f}-{hi * 100:.0f}¢, смесь ниже на {gap * 100:.0f}+ п.п.", no_band(lo, hi, gap), ja, late)
