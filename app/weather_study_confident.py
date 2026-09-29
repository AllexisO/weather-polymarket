"""
Из разбора недели 22-28.09: когда рынок уверен (фаворит ≥ 60¢), смесь хуже рынка (−0.068 за неделю).
Проверка: меньший вес модели в смеси в такие дни (0.35 → 0.2 / 0.1 / 0). Порог: логошибка лучше в июле-авг И в
сентябре, деньги смеси 3 п.п. по сделкам не хуже. Только на копии.
"""
import math
import statistics as st

import weather_study_0926 as base
from weather_ml_live import blend_with_market


def reblend(days, w_conf, thr=0.60):
    out = []
    for d in days:
        t = sum(d["price"]) or 1.0
        if max(d["price"]) / t >= thr:
            P = [d["raw"][k] for k in d["keys"]]
            d = {**d, "blend": dict(zip(d["keys"], blend_with_market(P, d["price"], w_conf)))}
        out.append(d)
    return out


def ll(days):
    return st.mean(-math.log(max(d["blend"][[k for k in d["keys"] if k[0] == d["win"]][0]], 1e-4)) for d in days)


days = base.load_days()
ja = [d for d in days if "2026-07-01" <= d["date"] < "2026-09-01"]
se = [d for d in days if d["date"] >= "2026-09-01"]
print(f"дней с уверенным рынком (≥60¢): июль-авг {sum(max(d['price']) / (sum(d['price']) or 1) >= .6 for d in ja)}, сентябрь {sum(max(d['price']) / (sum(d['price']) or 1) >= .6 for d in se)}")
for w in (0.35, 0.2, 0.1, 0.0):
    a, s = reblend(ja, w), reblend(se, w)
    r = base.run_rule([d for d in s if d["date"] >= base.FIRST_TRADE], "blend", 0.03)
    print(f"вес модели при уверенном рынке {w:.2f}: логошибка июль-авг {ll(a):.4f}, сентябрь {ll(s):.4f} | "
          f"смесь 3 п.п. по сделкам: {r['n_real']} ставок {r['pnl_real']:+.1f}$ ({r['pnl_real'] / max(r['staked_real'], 1) * 100:+.1f}%)", flush=True)

print("\nиюль-авг по цене 08:00 (второй период для правила ставок):")
for w in (0.35, 0.1, 0.0):
    r = base.run_rule(reblend(ja, w), "blend", 0.03)
    print(f"  вес {w:.2f}: {r['n']} ставок {r['pnl']:+.1f}$ ({r['pnl'] / max(r['staked'], 1) * 100:+.1f}%)", flush=True)
