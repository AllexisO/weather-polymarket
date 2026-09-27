"""Разовое исследование калибровки v3 (2026-09-26). Подбор — июль-август, проверка — сентябрь."""
import json, math, sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np
import weather_ml_check as chk
import weather_ml_q as mq
from weather_cities import OBS_CITIES
conn = sqlite3.connect(chk.DB_PATH)
win = {(r[0], r[1]): r[2] for r in conn.execute("SELECT city, local_date, win_lo FROM weather_poly_outcomes")}
cid = {(r[0], r[1], r[2]): r[3] for r in conn.execute("SELECT city, local_date, bucket_lo, condition_id FROM poly_market_final")}
first_trade = conn.execute("SELECT MIN(local_date) FROM poly_trades_days").fetchone()[0]
days = []
for city, date, unit, qs in conn.execute("SELECT city, date, unit, qs FROM ml_preds_var_mkt"):
    w = win.get((city, date))
    if w is None: continue
    pr = chk.prices(conn, city, date, "A")
    if len(pr) < 3: continue
    q = json.loads(qs)
    P = {b: mq.bucket_prob(q, unit, b[0], b[1]) for b in pr}
    t = sum(P.values()) or 1
    days.append((city, date, {b: P[b] / t for b in pr}, pr, w))
print("дней", len(days))
fit = [d for d in days if d[1] < "2026-09-01"]; test = [d for d in days if d[1] >= "2026-09-01"]
# калибровочная таблица (все варианты)
def table(ds, f=lambda p, m: p):
    bins = {}
    for _, _, P, pr, w in ds:
        for b in P:
            p = f(P[b], pr[b]); k = min(int(p * 10), 9)
            s = bins.setdefault(k, [0, 0.0, 0]); s[0] += 1; s[1] += p; s[2] += b[0] == w
    return {k: (v[0], v[1] / v[0], v[2] / v[0]) for k, v in sorted(bins.items())}
print("калибровка v3 (июль-авг): корзина -> n, средний шанс модели, реально")
for k, v in table(fit).items(): print(f"  {k*10:2d}-{k*10+10}%: n={v[0]:5d} модель {v[1]:.3f} реально {v[2]:.3f}")
lg = lambda p: math.log(max(p, 1e-6) / max(1 - p, 1e-6))
sg = lambda x: 1 / (1 + math.exp(-x))
def apply(P, pr, kind, a):
    if kind == "raw": Q = dict(P)
    elif kind == "platt": Q = {b: sg(a[0] * lg(P[b]) + a[1]) for b in P}
    elif kind == "blend": Q = {b: a[0] * P[b] + (1 - a[0]) * pr[b] / (sum(pr.values()) or 1) for b in P}
    t = sum(Q.values()) or 1
    return {b: Q[b] / t for b in Q}
def ll(ds, kind, a):
    return sum(-math.log(max(apply(P, pr, kind, a)[next(b for b in P if b[0] == w)] if any(b[0] == w for b in P) else 1, 1e-4)) for _, _, P, pr, w in ds) / len(ds)
# подбор на июле-августе
best_platt = min(((x, y) for x in np.arange(0.5, 1.21, 0.05) for y in np.arange(-0.6, 0.41, 0.1)), key=lambda a: ll(fit, "platt", a))
best_blend = min(((w,) for w in np.arange(0.3, 1.01, 0.05)), key=lambda a: ll(fit, "blend", a))
print("platt a,b =", [round(float(v), 2) for v in best_platt], " blend w =", round(float(best_blend[0]), 2))
def bets(ds, kind, a, real=False):
    out = {"n": 0, "won": 0, "exp_model": 0.0, "exp_mkt": 0.0, "pnl": 0.0, "n_real": 0, "pnl_real": 0.0}
    for city, date, P, pr, w in ds:
        Q = apply(P, pr, kind, a)
        b = max(pr, key=lambda x: Q[x] - pr[x])
        if not (Q[b] - pr[b] >= 0.10 and 0.03 <= pr[b] <= 0.95): continue
        won = b[0] == w
        out["n"] += 1; out["won"] += won; out["exp_model"] += Q[b]; out["exp_mkt"] += pr[b]
        out["pnl"] += chk.pnl(pr[b], won) * 2 / 5
        key = (city, date, b[0])
        if date >= first_trade and key in cid:
            ts = datetime.fromisoformat(date).replace(tzinfo=ZoneInfo(OBS_CITIES[city]["tz"])).timestamp() + 8 * 3600
            maxp = min(0.95, Q[b] - 0.10)
            fills = sorted((t, (p if o == "Yes" else 1 - p)) for o, sd, p, t in conn.execute(
                "SELECT outcome, side, price, ts FROM poly_trades WHERE condition_id = ? AND ts BETWEEN ? AND ?", (cid[key], ts, ts + 1800))
                if ((o == "Yes" and sd == "BUY") or (o == "No" and sd == "SELL")) and (p if o == "Yes" else 1 - p) <= maxp)
            if fills:
                out["n_real"] += 1; out["pnl_real"] += chk.pnl(fills[0][1] - 0.01, won) * 2 / 5
    return out
def bets_t(ds, kind, a, thr):
    global_thr = thr
    out = {"n": 0, "won": 0, "exp_model": 0.0, "exp_mkt": 0.0, "pnl": 0.0, "n_real": 0, "pnl_real": 0.0, "fees": 0.0}
    for city, date, P, pr, w in ds:
        Q = apply(P, pr, kind, a)
        b = max(pr, key=lambda x: Q[x] - pr[x])
        if not (Q[b] - pr[b] >= thr and 0.03 <= pr[b] <= 0.95): continue
        won = b[0] == w
        out["n"] += 1; out["won"] += won; out["exp_model"] += Q[b]; out["exp_mkt"] += pr[b]
        out["pnl"] += chk.pnl(pr[b], won) * 2 / 5
        key = (city, date, b[0])
        if date >= first_trade and key in cid:
            ts = datetime.fromisoformat(date).replace(tzinfo=ZoneInfo(OBS_CITIES[city]["tz"])).timestamp() + 8 * 3600
            maxp = min(0.95, Q[b] - thr)
            fills = sorted((t, (p if o == "Yes" else 1 - p)) for o, sd, p, t in conn.execute(
                "SELECT outcome, side, price, ts FROM poly_trades WHERE condition_id = ? AND ts BETWEEN ? AND ?", (cid[key], ts, ts + 1800))
                if ((o == "Yes" and sd == "BUY") or (o == "No" and sd == "SELL")) and (p if o == "Yes" else 1 - p) <= maxp)
            if fills:
                out["n_real"] += 1; out["pnl_real"] += chk.pnl(fills[0][1] - 0.01, won) * 2 / 5
    return out
for thr in (0.02, 0.03, 0.04, 0.05, 0.06, 0.08):
    for per, ds in (("июль-авг", fit), ("сентябрь", test)):
        b = bets_t(ds, "blend", best_blend, thr)
        print(f"смесь, порог {thr*100:.0f} п.п. {per:9s} | ставок {b['n']:4d} угадано {b['won']:4d} смесь ждала {b['exp_model']:6.1f} рынок {b['exp_mkt']:6.1f} | $ по цене A {b['pnl']:+7.1f} | реальные сделки: {b['n_real']:3d} ставок, {b['pnl_real']:+6.1f}$")
