"""
Система RainBot v2/v3 (rainbot.finance, «Как RainBot находит эдж», прислал Alex 30.09) — их формулы без их AI на наших данных:
модели ECMWF 35% · GFS 25% · UKMO 20% · NWS 20% (у нас NWS ≈ NBM, только США; нет — вес делится между остальными),
модель дальше 1.5 стандартных отклонений от среднего — вес вдвое меньше; σ по времени до итога (решение в 08:00 дня маркета,
до итога ~16 ч → 1.1°C) × число моделей (4 → 1.0, 3 → 1.1, 2 → 1.25) × (1 + разброс/5°C); для °F всё × 1.8;
шанс варианта — нормальная CDF по границам; ставка «да»/«нет», если перевес ≥ порога и цена стороны 36-80¢ (их «entry band»);
все сигналы дня, по $2. Прогнозы — mm_forecasts lead='day1' (выпущены за сутки, как у них ~26 ч). Их «исторический prior»
(10 лет NOAA, вес ~10%) и финальный AI не воспроизводим. Рядом — то же правило на нашей смеси v3+рынок.
Порог: плюс после комиссии в обоих периодах (июль-авг по цене 08:00, с 21.08 по настоящим сделкам). Только на копии.
"""
import math

import numpy as np

import weather_study_0926 as base
from weather_cities import OBS_CITIES
from weather_study_links import show

conn = base.conn
W = {"ecmwf_ifs025": 0.35, "gfs_seamless": 0.25, "ukmo_seamless": 0.20, "ncep_nbm_conus": 0.20}
FC = {}
for city, d, m, v in conn.execute(f"SELECT city, local_date, model, fcst_max FROM mm_forecasts WHERE lead = 'day1' AND model IN ({','.join('?' * len(W))}) AND fcst_max IS NOT NULL", list(W)):
    FC.setdefault((city, d), {})[m] = v


def cdf(x, mu, s):
    return 0.5 * (1 + math.erf((x - mu) / (s * math.sqrt(2))))


def rb_probs(d):
    f = FC.get((d["city"], d["date"]))
    if not f or len(f) < 2:
        return None
    unit = OBS_CITIES[d["city"]]["unit"]
    k = 1.8 if unit == "fahrenheit" else 1.0
    vals = np.array(list(f.values())); ws = np.array([W[m] for m in f])
    mean, sd = vals.mean(), vals.std()
    ws = np.where(np.abs(vals - mean) > 1.5 * sd, ws / 2, ws) if sd > 0 else ws
    mu = float((vals * ws).sum() / ws.sum())
    spread = float(sd) / k                                         # в °C
    sigma = 1.1 * {4: 1.0, 3: 1.1, 2: 1.25}.get(len(f), 1.5) * (1 + spread / 5) * k
    P = []
    for lo, hi in d["keys"]:
        a = 0.0 if lo <= -900 else cdf(lo, mu, sigma)
        b = 1.0 if hi >= 900 else cdf(hi, mu, sigma)
        P.append(max(b - a, 1e-4))
    t = sum(P)
    return {b: p / t for b, p in zip(d["keys"], P)}


def rule(src, thr, band=(0.36, 0.80)):
    def pick(d):
        pr = d.get(src)
        if pr is None:
            return []
        out = []
        for b, p in zip(d["keys"], d["price"]):
            q = pr[b]
            if q - p >= thr and band[0] <= p <= band[1]:
                out.append((b, "yes", p, 2.0))
            elif p - q >= thr and band[0] <= 1 - p <= band[1]:
                out.append((b, "no", 1 - p, 2.0))
        return out
    return pick


def ll(days, src):
    xs = [(-math.log(max(d[src][next(b for b in d["keys"] if b[0] == d["win"])], 1e-3)),
           -math.log(max(d["price"][[b[0] for b in d["keys"]].index(d["win"])] / sum(d["price"]), 1e-3))) for d in days if d.get(src)]
    return np.mean([x[0] for x in xs]), np.mean([x[1] for x in xs]), len(xs)


if __name__ == "__main__":
    days = base.load_days()
    for d in days:
        d["rb"] = rb_probs(d)
    ja = [d for d in days if "2026-07-01" <= d["date"] < "2026-09-01"]
    late = [d for d in days if d["date"] >= base.FIRST_TRADE]
    for name, part in (("июль-авг", ja), ("с 21.08", late)):
        a, m, n = ll(part, "rb"); b, _, _ = ll(part, "blend")
        print(f"точность ({name}, {n} город-дней): логошибка RainBot {a:.3f}, наша смесь {b:.3f}, рынок {m:.3f} (меньше — лучше)", flush=True)
    print("\n=== правило RainBot: цена стороны 36-80¢, все сигналы, $2 ===")
    for thr in (0.03, 0.05, 0.08, 0.12):
        show(f"их прогноз, перевес ≥ {thr * 100:.0f} п.п.", rule("rb", thr), ja, late)
        show(f"  то же на нашей смеси v3+рынок", rule("blend", thr), ja, late)
