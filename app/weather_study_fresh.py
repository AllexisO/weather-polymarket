"""
Помогает ли свежий прогноз к 08:00 (weather_fresh_fc.py → fresh_hres) — 2026-09-28, исследование «что мы упускаем».

Δ = ECMWF HRES свежий (прогон, доступный к 08:00 дня маркета) − он же вчерашний (доступный к 08:00 накануне).
Модель v3 (проверка вслепую, ml_preds_var_mkt) знает только вчерашние прогнозы. Сдвигаем все её уровни на β·Δ:
β подбираем на первой половине (27.08-10.09), проверяем на второй (11-24.09). Логошибка — модели и смеси 35/65
по цене 08:00, как в weather_study_tune.score. Порог (зафиксирован до запуска): смесь со сдвигом лучше на ≥ 0.015
на второй половине. Прошло — грузим историю прогонов для обучения (признак в модели), кошелёк — отдельно.
Только на копии базы.
"""

import json
import math

import numpy as np

import weather_ml_q as mq
import weather_study_0926 as base
from weather_ml_live import blend_with_market

conn = base.conn
FIT = ("2026-08-27", "2026-09-11")
TEST = ("2026-09-11", "2026-09-25")


def load():
    fr = {}
    for city, d, kind, v in conn.execute("SELECT city, local_date, kind, max_c FROM fresh_hres"):
        fr.setdefault((city, d), {})[kind] = v
    rows = []
    for city, d, unit, act, fc, qs in conn.execute(
            "SELECT city, date, unit, actual_c, fc_mean, qs FROM ml_preds_var_mkt WHERE date >= ?", (FIT[0],)).fetchall():
        f = fr.get((city, d), {})
        if "fresh" in f and "stale" in f and act is not None:
            rows.append({"city": city, "date": d, "unit": unit, "act": act, "fc": fc, "qs": json.loads(qs),
                         "fresh": f["fresh"], "stale": f["stale"], "delta": f["fresh"] - f["stale"]})
    return rows


def score(rows, beta):
    ll_m = ll_b = ll_k = 0.0
    n = 0
    days = []
    for r in rows:
        w = base.WIN.get((r["city"], r["date"]))
        pr = base.chk.prices(conn, r["city"], r["date"], "A") if w is not None else {}
        wb = next((b for b in pr if b[0] == w), None)
        if len(pr) < 3 or wb is None:
            continue
        q = [x + beta * r["delta"] for x in r["qs"]]
        keys = sorted(pr)
        P = [mq.bucket_prob(q, r["unit"], b[0], b[1]) for b in keys]
        t = sum(P) or 1.0
        P = [x / t for x in P]
        B = blend_with_market(P, [pr[b] for b in keys])
        i = keys.index(wb)
        tk = sum(pr.values()) or 1.0
        ll_m += -math.log(max(P[i], 1e-4)); ll_b += -math.log(max(B[i], 1e-4)); ll_k += -math.log(max(pr[wb] / tk, 1e-4))
        n += 1
        days.append({"city": r["city"], "date": r["date"], "keys": keys, "price": [pr[b] for b in keys],
                     "raw": dict(zip(keys, P)), "blend": dict(zip(keys, B)), "win": w})
    return ll_m / n, ll_b / n, ll_k / n, n, days


if __name__ == "__main__":
    rows = load()
    fit = [r for r in rows if FIT[0] <= r["date"] < FIT[1]]
    test = [r for r in rows if TEST[0] <= r["date"] < TEST[1]]
    i50 = mq.QUANTILES.index(0.5)
    for name, rs in (("подбор", fit), ("проверка", test)):
        a = np.array([r["act"] for r in rs])
        print(f"{name}: {len(rs)} город-дней | ошибка ECMWF свежий {np.mean(np.abs(np.array([r['fresh'] for r in rs]) - a)):.3f}° "
              f"вчерашний {np.mean(np.abs(np.array([r['stale'] for r in rs]) - a)):.3f}° | модель v3 (медиана) "
              f"{np.mean(np.abs(np.array([r['qs'][i50] for r in rs]) - a)):.3f}° | |Δ| в среднем {np.mean(np.abs([r['delta'] for r in rs])):.2f}°", flush=True)
    res = np.array([r["act"] - r["qs"][i50] for r in fit])
    dl = np.array([r["delta"] for r in fit])
    beta = float(np.sum(res * dl) / np.sum(dl * dl))
    corr = float(np.corrcoef(res, dl)[0, 1])
    rt = np.array([r["act"] - r["qs"][i50] for r in test]); dt = np.array([r["delta"] for r in test])
    print(f"β (подбор) = {beta:.3f}; связь ошибки модели с Δ: подбор {corr:.3f}, проверка {np.corrcoef(rt, dt)[0, 1]:.3f}", flush=True)
    for name, rs in (("подбор", fit), ("проверка", test)):
        m0, b0, k0, n, d0 = score(rs, 0.0)
        m1, b1, k1, _, d1 = score(rs, beta)
        line = (f"{name} ({n} с ценами): модель {m0:.4f} → со сдвигом {m1:.4f} ({m1 - m0:+.4f}) | смесь {b0:.4f} → {b1:.4f} "
                f"({b1 - b0:+.4f}) | рынок {k0:.4f}")
        for lbl, dd in (("без сдвига", d0), ("со сдвигом", d1)):
            r = base.run_rule(dd, "blend", 0.03)
            if r["n_real"]:
                line += f" | деньги смеси {lbl}: {r['pnl_real']:+.1f}$ ({r['n_real']} ставок)"
        print(line, flush=True)
