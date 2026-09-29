"""
Вес модели в смеси по группам городов (2026-09-29, очередь разбора): разбор 22-28.09 — смесь лучше рынка в °F-городах
(+0.029), почти вровень в °C (+0.004). Может, в группах нужен разный вес вместо общего 0.35.
Подбор веса на июле-августе (логошибка), проверка — сентябрь; группы: °F / °C, и азиатские °C / остальные °C.
Порог (до прогона): логошибка сентября лучше общего 0.35 на ≥ 0.015 или деньги смеси 3 п.п. по сделкам лучше в обоих
периодах. Только на копии.
"""
import math
import statistics as st

import weather_study_0926 as base
from weather_cities import OBS_CITIES
from weather_ml_live import blend_with_market

ASIA = {c for c, v in OBS_CITIES.items() if v["tz"].startswith("Asia/")}
WS = (0.0, 0.1, 0.2, 0.3, 0.35, 0.4, 0.5, 0.6, 0.8)


def group(c, how):
    f = OBS_CITIES[c]["unit"] == "fahrenheit"
    return ("°F" if f else "°C") if how == "unit" else ("°F" if f else "Азия °C" if c in ASIA else "прочие °C")


def reb(days, wmap, how):
    out = []
    for d in days:
        w = wmap.get(group(d["city"], how), 0.35)
        P = [d["raw"][k] for k in d["keys"]]
        out.append({**d, "blend": dict(zip(d["keys"], blend_with_market(P, d["price"], w)))})
    return out


def ll(days):
    return st.mean(-math.log(max(d["blend"][[k for k in d["keys"] if k[0] == d["win"]][0]], 1e-4)) for d in days) if days else float("nan")


days = base.load_days()
ja = [d for d in days if "2026-07-01" <= d["date"] < "2026-09-01"]
se = [d for d in days if d["date"] >= "2026-09-01"]
for how in ("unit", "asia"):
    groups = sorted({group(d["city"], how) for d in days})
    wmap = {}
    for g in groups:
        part = [d for d in ja if group(d["city"], how) == g]
        wmap[g] = min(WS, key=lambda w: ll(reb(part, {g: w}, how)))
        ps = [d for d in se if group(d["city"], how) == g]
        print(f"{g:10s}: лучший вес на июле-авг {wmap[g]:.2f} | сентябрь: при 0.35 {ll(reb(ps, {g: 0.35}, how)):.4f}, "
              f"при {wmap[g]:.2f} {ll(reb(ps, {g: wmap[g]}, how)):.4f} ({len(ps)} дн.)", flush=True)
    for name, wm in (("общий 0.35", {}), ("по группам", wmap)):
        a, s = reb(ja, wm, how), reb(se, wm, how)
        ra, rs = base.run_rule(a, "blend", 0.03), base.run_rule([d for d in s if d["date"] >= base.FIRST_TRADE], "blend", 0.03)
        print(f"  {how}, {name:11s}: логошибка июль-авг {ll(a):.4f}, сентябрь {ll(s):.4f} | деньги: июль-авг по цене 08:00 "
              f"{ra['pnl'] / max(ra['staked'], 1) * 100:+.1f}% ({ra['n']}), по сделкам {rs['pnl_real'] / max(rs['staked_real'], 1) * 100:+.1f}% ({rs['n_real']})", flush=True)
