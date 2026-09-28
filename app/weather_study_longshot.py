"""
«Против лотерейных билетов» как отдельная стратегия (2026-09-28, исследование «что мы упускаем»).
На всём рынке варианты за 5-15¢ сбываются реже цены (июль-авг 10-15¢: 12.2% → 10.3%; сентябрь 5-10¢: 7.0% → 5.2%);
работа по Polymarket (arXiv 2609.12878): покупки дешевле 10¢ теряют ~19¢ с доллара.

Правило: в 08:00 на каждый вариант с ценой в полосе покупаем «нет» за $2 (первая настоящая сделка за 30 мин не дороже
1 − цена + 1¢; комиссия и спред — как везде). Варианты: без фильтра; только если смесь v3 + рынок тоже считает вариант
переоценённым (шанс смеси < цены). Порог: июль-авг по цене 08:00 и конец авг-сент по настоящим сделкам — оба в плюсе
после комиссии. Только на копии базы.
"""

import weather_study_0926 as base

BANDS = ((0.05, 0.10), (0.10, 0.15), (0.05, 0.15), (0.15, 0.25))


def run(days, lo, hi, need_model):
    n = won = 0
    pnl = 0.0
    nr = 0
    pr = st = 0.0
    for d in days:
        for b, p in zip(d["keys"], d["price"]):
            if not (lo <= p < hi):
                continue
            if need_model and d["blend"][b] >= p:
                continue
            w = b[0] != d["win"]
            n += 1
            won += w
            pnl += base.pnl(1 - p, w)
            fp = base.real_fill(d["city"], d["date"], b[0], "no", min(0.97, 1 - p + 0.01))
            if fp not in (None, "nodata"):
                nr += 1
                pr += base.pnl(fp - 0.01, w)
                st += base.STAKE
    return n, won, pnl, nr, pr, st


if __name__ == "__main__":
    days = base.load_days()
    ja = [d for d in days if "2026-07-01" <= d["date"] < "2026-09-01"]
    late = [d for d in days if d["date"] >= base.FIRST_TRADE]
    for need, name in ((False, "все варианты полосы"), (True, "только если смесь тоже считает переоценённым")):
        print(f"\n=== «нет» на дешёвые варианты · {name} ===")
        for lo, hi in BANDS:
            a = run(ja, lo, hi, need)
            s = run(late, lo, hi, need)
            real = f"{s[3]:4d} ставок {s[4]:+7.1f}$ ({s[4] / s[5] * 100:+.1f}%)" if s[3] else "нет сделок"
            print(f"{lo * 100:.0f}-{hi * 100:.0f}¢ | июль-авг по цене 08:00: {a[0]:5d} ставок, «нет» сыграло {a[1] / max(a[0], 1) * 100:.1f}%, "
                  f"{a[2]:+7.1f}$ ({a[2] / max(a[0] * base.STAKE, 1) * 100:+.1f}%) | с {base.FIRST_TRADE}, настоящие сделки: {real}", flush=True)


def run_one(days, lo, hi):
    """Одна ставка на город-день: вариант полосы с наибольшей переоценкой по смеси (цена − шанс смеси)."""
    n = won = nr = 0
    pnl = pr = st = 0.0
    for d in days:
        c = [(p - d["blend"][b], b, p) for b, p in zip(d["keys"], d["price"]) if lo <= p < hi and d["blend"][b] < p]
        if not c:
            continue
        _, b, p = max(c)
        w = b[0] != d["win"]
        n += 1; won += w; pnl += base.pnl(1 - p, w)
        fp = base.real_fill(d["city"], d["date"], b[0], "no", min(0.97, 1 - p + 0.01))
        if fp not in (None, "nodata"):
            nr += 1; pr += base.pnl(fp - 0.01, w); st += base.STAKE
    return n, won, pnl, nr, pr, st


def blend_w(days, wfun):
    """Логошибка смеси с весом модели wfun(d) вместо 0.35."""
    import math
    from weather_ml_live import blend_with_market
    s = 0.0
    for d in days:
        B = blend_with_market([d["raw"][b] for b in d["keys"]], d["price"], w=wfun(d))
        i = [b[0] for b in d["keys"]].index(d["win"])
        s += -math.log(max(B[i], 1e-4))
    return s / len(days)


def entropy(d):
    import math
    t = sum(d["price"]) or 1.0
    return -sum(p / t * math.log(p / t) for p in d["price"] if p > 0)


if __name__ == "__main__" and "--more" in __import__("sys").argv:
    days = base.load_days()
    ja = [d for d in days if "2026-07-01" <= d["date"] < "2026-09-01"]
    se = [d for d in days if d["date"] >= "2026-09-01"]
    late = [d for d in days if d["date"] >= base.FIRST_TRADE]
    print("\n=== «нет», одна ставка на город-день (наибольшая переоценка по смеси) ===")
    for lo, hi in ((0.05, 0.15), (0.03, 0.15), (0.05, 0.20)):
        a, s = run_one(ja, lo, hi), run_one(late, lo, hi)
        print(f"{lo * 100:.0f}-{hi * 100:.0f}¢ | июль-авг по цене 08:00: {a[0]} ставок, «нет» {a[1] / max(a[0], 1) * 100:.1f}%, {a[2]:+.1f}$ "
              f"({a[2] / max(a[0] * base.STAKE, 1) * 100:+.1f}%) | настоящие сделки: {s[3]} ставок {s[4]:+.1f}$ "
              f"({s[4] / max(s[5], 1) * 100:+.1f}%)", flush=True)
    print("\n=== вес модели в смеси по неопределённости рынка (энтропия цен) ===")
    ents = sorted(entropy(d) for d in ja)
    t1, t2 = ents[len(ents) // 3], ents[2 * len(ents) // 3]
    grid = (0.2, 0.35, 0.5)
    best = {}
    for k, cond in enumerate((lambda e: e < t1, lambda e: t1 <= e < t2, lambda e: e >= t2)):
        part = [d for d in ja if cond(entropy(d))]
        sc = {w: blend_w(part, lambda d, w=w: w) for w in grid}
        best[k] = min(sc, key=sc.get)
        print(f"треть {k + 1} (рынок {'уверен' if k == 0 else 'средне' if k == 1 else 'не уверен'}): июль-авг "
              + " ".join(f"w={w}: {v:.4f}" for w, v in sc.items()), flush=True)
    wf = lambda d: best[0] if entropy(d) < t1 else (best[1] if entropy(d) < t2 else best[2])
    print(f"выбрано по июлю-авг: {best} | сентябрь: как сейчас (0.35) {blend_w(se, lambda d: 0.35):.4f}, "
          f"по неопределённости {blend_w(se, wf):.4f}", flush=True)
