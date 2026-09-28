"""
Поправка цены рынка перед смесью и несколько ставок на город (2026-09-28, исследование «что мы упускаем»).
1. Цена → реальный шанс: по июлю-августу по всем вариантам в 08:00 (корзины цены, доля сбывшихся), затем смесь v3
   с поправленной ценой. Проверка — сентябрь: логошибка и деньги смеси 3 п.п. по настоящим сделкам.
2. Ставок на город-день: 1 (как сейчас), 2, 3 — лучшие по перевесу смеси.
Порог (до запуска): логошибка лучше на ≥ 0.015 или деньги сентября выше в обоих вариантах модели. Только на копии.
"""
import math
import bisect

import weather_study_0926 as base
from weather_ml_live import blend_with_market

EDGES = [0, .01, .02, .03, .05, .07, .10, .13, .16, .20, .25, .30, .40, .50, .60, .75, 1.01]


def fit(days):
    s, n = [0.0] * len(EDGES), [0] * len(EDGES)
    for d in days:
        t = sum(d["price"]) or 1.0
        for b, p in zip(d["keys"], d["price"]):
            i = bisect.bisect_right(EDGES, p / t) - 1
            s[i] += b[0] == d["win"]; n[i] += 1
    return [(s[i] + 0.5 * (EDGES[i] + EDGES[i + 1]) / 2) / (n[i] + 0.5) if n[i] else None for i in range(len(EDGES) - 1)]


def cal(p, table):
    i = min(bisect.bisect_right(EDGES, p) - 1, len(table) - 1)
    return table[i] if table[i] is not None else p


def remix(days, table):
    out = []
    for d in days:
        t = sum(d["price"]) or 1.0
        m = [cal(p / t, table) if table else p / t for p in d["price"]]
        tm = sum(m) or 1.0
        m = [x / tm for x in m]
        B = blend_with_market([d["raw"][b] for b in d["keys"]], m)
        out.append(dict(d, blend=dict(zip(d["keys"], B)), mcal=m))
    return out


def ll(days, key):
    s = 0.0
    for d in days:
        i = [b[0] for b in d["keys"]].index(d["win"])
        v = d["mcal"][i] if key == "mcal" else d["blend"][d["keys"][i]]
        s += -math.log(max(v, 1e-4))
    return s / len(days)


if __name__ == "__main__":
    days = base.load_days()
    ja = [d for d in days if "2026-07-01" <= d["date"] < "2026-09-01"]
    se = [d for d in days if d["date"] >= "2026-09-01"]
    table = fit(ja)
    print("цена → сбылось (июль-авг): " + " ".join(f"{EDGES[i] * 100:.0f}-{EDGES[i + 1] * 100:.0f}¢:{(v or 0) * 100:.1f}%"
                                                 for i, v in enumerate(table)), flush=True)
    s0, s1 = remix(se, None), remix(se, table)
    print(f"сентябрь, логошибка: рынок {ll(s0, 'mcal'):.4f} → поправленный {ll(s1, 'mcal'):.4f} | смесь {ll(s0, 'blend'):.4f} → "
          f"с поправленным рынком {ll(s1, 'blend'):.4f}", flush=True)
    for lbl, ds in (("как сейчас", s0), ("с поправленным рынком", s1)):
        r = base.run_rule(ds, "blend", 0.03)
        print(f"деньги смеси 3 п.п., сентябрь, настоящие сделки — {lbl}: {r['n_real']} ставок {r['pnl_real']:+.1f}$ "
              f"({r['pnl_real'] / max(r['staked_real'], 1) * 100:+.0f}%)", flush=True)
    for model, thr, name in (("blend", 0.03, "смесь 3 п.п."), ("raw", 0.10, "v3 10 п.п.")):
        for k in (1, 2, 3):
            a, s = base.run_rule(ja, model, thr, per_city=k), base.run_rule(se, model, thr, per_city=k)
            print(f"{name}, ставок на город до {k}: июль-авг по цене 08:00 {a['n']} ставок {a['pnl']:+.1f}$ | сентябрь, "
                  f"настоящие сделки {s['n_real']} ставок {s['pnl_real']:+.1f}$ ({s['pnl_real'] / max(s['staked_real'], 1) * 100:+.0f}%)",
                  flush=True)
