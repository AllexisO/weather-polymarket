"""
Держится ли преимущество по городам (2026-09-28, вопрос Alex «выбрать город, где модель лучше»).
По каждому городу: насколько смесь v3 + рынок лучше рынка (логошибка) в июле-августе и в сентябре; связь между
периодами (если город «хороший» по-настоящему — будет хорошим и дальше). Плюс деньги сентября по настоящим сделкам
у городов, лучших и худших по июлю-августу. Только на копии базы.
"""
import math
import statistics as st

import weather_study_0926 as base


def gap(days):
    g = []
    for d in days:
        i = [b[0] for b in d["keys"]].index(d["win"])
        t = sum(d["price"]) or 1.0
        g.append(-math.log(max(d["price"][i] / t, 1e-4)) + math.log(max(d["blend"][d["keys"][i]], 1e-4)))
    return st.mean(g) if g else None


days = base.load_days()
cities = sorted({d["city"] for d in days})
rows = []
for c in cities:
    a = [d for d in days if d["city"] == c and "2026-07-01" <= d["date"] < "2026-09-01"]
    s = [d for d in days if d["city"] == c and d["date"] >= "2026-09-01"]
    if len(a) >= 20 and len(s) >= 10:
        rows.append((c, gap(a), gap(s), len(a), len(s), s))
xs, ys = [r[1] for r in rows], [r[2] for r in rows]
mx, my = st.mean(xs), st.mean(ys)
corr = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
print(f"городов: {len(rows)}; связь «насколько смесь лучше рынка» июль-авг → сентябрь: {corr:+.2f} "
      f"(1 — города стабильно те же, 0 — случайно)")
rows.sort(key=lambda r: -r[1])
for name, part in (("лучшие 10 по июлю-авг", rows[:10]), ("худшие 10 по июлю-авг", rows[-10:])):
    r = base.run_rule([d for x in part for d in x[5]], "blend", 0.03)
    print(f"{name}: в июле-авг лучше рынка в среднем на {st.mean(x[1] for x in part):+.3f}, в сентябре {st.mean(x[2] for x in part):+.3f}; "
          f"деньги смеси в сентябре по сделкам {r['pnl_real']:+.1f}$ ({r['n_real']} ставок)")
print("по городам (июль-авг → сентябрь, + = смесь лучше рынка):")
print("  " + " · ".join(f"{r[0]} {r[1]:+.2f}→{r[2]:+.2f}" for r in rows))

# --- скользящий отбор: каждую неделю города по прошлым LOOK дням, ставим только в них следующую неделю ---
from datetime import date, timedelta
print("\nскользящий отбор (как делал бы живой кошелёк), деньги смеси 3 п.п. по настоящим сделкам с 21.08:")
by_c = {}
for d in days:
    by_c.setdefault(d["city"], []).append(d)
for look in (30, 45, 60):
    for rule in ("лучшая половина", "кроме худших 10"):
        picked, allw = [], []
        w = date(2026, 8, 24)
        while w <= date(2026, 9, 21):
            ws, we, ls = w.isoformat(), (w + timedelta(days=7)).isoformat(), (w - timedelta(days=look)).isoformat()
            sc = {c: gap([d for d in v if ls <= d["date"] < ws]) for c, v in by_c.items()}
            sc = {c: g for c, g in sc.items() if g is not None}
            order = sorted(sc, key=lambda c: -sc[c])
            keep = set(order[:len(order) // 2]) if rule == "лучшая половина" else set(order[:-10])
            wk = [d for d in days if ws <= d["date"] < we]
            picked += [d for d in wk if d["city"] in keep]
            allw += wk
            w += timedelta(days=7)
        r, r0 = base.run_rule(picked, "blend", 0.03), base.run_rule(allw, "blend", 0.03)
        print(f"  по прошлым {look} дн, {rule}: {r['n_real']} ставок {r['pnl_real']:+.1f}$ "
              f"({r['pnl_real'] / max(r['staked_real'], 1) * 100:+.0f}%) | все города: {r0['n_real']} ставок {r0['pnl_real']:+.1f}$ "
              f"({r0['pnl_real'] / max(r0['staked_real'], 1) * 100:+.0f}%)", flush=True)
