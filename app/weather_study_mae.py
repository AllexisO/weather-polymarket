"""
Средняя абсолютная ошибка (MAE), идея коллеги Alex (2026-09-29). Медиана распределения (модель v3, смесь, рынок) —
вариант, где накопленный шанс переходит 50%; ошибка — на сколько вариантов мимо итога (1 вариант = 1°C или 2°F).
Плюс: отбор городов по MAE (модель точнее рынка за прошлые 45 дней, лучшая половина) — деньги смеси 3 п.п. по сделкам
с 24.08, сравнение с отбором по логошибке (как ml3_city) и «все города». Только на копии.
"""
import statistics as st
from datetime import date, timedelta

import weather_study_0926 as base
from weather_study_city import gap


def med(ps):
    t, c = sum(ps) or 1.0, 0.0
    for i, p in enumerate(ps):
        c += p / t
        if c >= 0.5:
            return i
    return len(ps) - 1


def err(d, src):
    ks = d["keys"]
    ps = d["price"] if src == "price" else [d[src][k] for k in ks]
    return abs(med(ps) - [k[0] for k in ks].index(d["win"]))


def mae(days, src):
    return st.mean(err(d, src) for d in days) if days else None


days = base.load_days()
for name, part in (("июль-авг", [d for d in days if "2026-07-01" <= d["date"] < "2026-09-01"]),
                   ("сентябрь", [d for d in days if d["date"] >= "2026-09-01"])):
    print(f"{name} ({len(part)} город-дней): MAE модель v3 {mae(part, 'raw'):.3f}, смесь {mae(part, 'blend'):.3f}, "
          f"рынок {mae(part, 'price'):.3f} вариантов; попал точно: модель {sum(err(d, 'raw') == 0 for d in part) / len(part) * 100:.1f}%, "
          f"рынок {sum(err(d, 'price') == 0 for d in part) / len(part) * 100:.1f}%", flush=True)

by_c = {}
for d in days:
    by_c.setdefault(d["city"], []).append(d)
print("\nотбор городов, лучшая половина по прошлым 45 дням, смесь 3 п.п., настоящие сделки 24.08-27.09:")
for lbl, score in (("по MAE (рынок − модель)", lambda v: mae(v, "price") - mae(v, "raw")),
                   ("по MAE смеси (рынок − смесь)", lambda v: mae(v, "price") - mae(v, "blend")),
                   ("по логошибке (как ml3_city)", gap)):
    picked, allw = [], []
    w = date(2026, 8, 24)
    while w <= date(2026, 9, 21):
        ws, we, ls = w.isoformat(), (w + timedelta(days=7)).isoformat(), (w - timedelta(days=45)).isoformat()
        sc = {}
        for c, v in by_c.items():
            v = [d for d in v if ls <= d["date"] < ws]
            if len(v) >= 15:
                sc[c] = score(v)
        order = sorted(sc, key=lambda c: -sc[c])
        keep = set(order[:len(order) // 2])
        wk = [d for d in days if ws <= d["date"] < we]
        picked += [d for d in wk if d["city"] in keep]
        allw += wk
        w += timedelta(days=7)
    r, r0 = base.run_rule(picked, "blend", 0.03), base.run_rule(allw, "blend", 0.03)
    print(f"  {lbl:30s}: {r['n_real']} ставок {r['pnl_real']:+.1f}$ ({r['pnl_real'] / max(r['staked_real'], 1) * 100:+.0f}%) | "
          f"все города: {r0['n_real']} ставок {r0['pnl_real']:+.1f}$ ({r0['pnl_real'] / max(r0['staked_real'], 1) * 100:+.0f}%)", flush=True)
