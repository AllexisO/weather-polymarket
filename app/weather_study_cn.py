"""
Разбор недели 27.09-03.10: в Чэнду, Шанхае, Сеуле, Чунцине, Ухане смесь хуже рынка 5 недель подряд (отбор — сентябрь).
Проверка на независимом периоде июль-август (docs/RESEARCH_JOURNAL.md, порог записан до прогона):
(а) смесь хуже рынка в этих городах вместе на ≥ 0.02 логошибки; (б) смесь 3 п.п. без них по цене 08:00 доходнее.
Только на копии.
"""
import math
import statistics as st

import weather_study_0926 as base

CN = {"chengdu", "shanghai", "seoul", "chongqing", "wuhan"}


def ll(days, key):
    out = []
    for d in days:
        k = [k for k in d["keys"] if k[0] == d["win"]][0]
        p = d[key][k] if key != "price" else d["price"][d["keys"].index(k)] / (sum(d["price"]) or 1)
        out.append(-math.log(max(p, 1e-4)))
    return st.mean(out) if out else float("nan")


days = base.load_days()
for name, lo, hi in (("июль-август (проверка)", "2026-07-01", "2026-09-01"), ("сентябрь-03.10 (где отбирали)", "2026-09-01", "2026-10-04")):
    per = [d for d in days if lo <= d["date"] < hi]
    cn = [d for d in per if d["city"] in CN]
    rest = [d for d in per if d["city"] not in CN]
    print(f"== {name}: 5 городов {len(cn)} дн. — смесь {ll(cn, 'blend'):.4f}, рынок {ll(cn, 'price'):.4f}, "
          f"смесь лучше рынка на {ll(cn, 'price') - ll(cn, 'blend'):+.4f} | остальные {len(rest)} дн.: на {ll(rest, 'price') - ll(rest, 'blend'):+.4f}", flush=True)
    for label, sub in (("все города", per), ("без 5 городов", rest)):
        r = base.run_rule(sub, "blend", 0.03)
        print(f"   смесь 3 п.п., {label}: {r['n']} ставок {r['pnl']:+.1f}$ ({r['pnl'] / max(r['staked'], 1) * 100:+.1f}%) по цене 08:00", flush=True)
