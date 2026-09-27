"""
Идея 3 (2026-09-26): «умные деньги» на погодных маркетах Polymarket.
Есть ли трейдеры, которые стабильно зарабатывают, и можно ли повторять их покупки?

Данные: poly_trades (все сделки ~5 недель) + кошелёк того, кто забрал заявку
(poly_trade_wallets / poly_trades.wallet) + итог маркета (poly_market_final).
Итог сделки держателя до конца: купил долю по p -> получит final (1 или 0).
Протокол: рейтинг трейдеров — по первым двум неделям, проверка — на следующих;
«повторять» = купить ту же долю по следующей настоящей сделке в течение 10 минут
не дороже его цены +2¢ (комиссия 0.05·p·(1−p) за долю).

Запуск — на копии базы: POLY_LAB_DB=/data/research/research.sqlite3
"""

import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
conn = sqlite3.connect(DB_PATH, timeout=60)

final = {r[0]: r[1] for r in conn.execute("SELECT condition_id, final_yes FROM poly_market_final").fetchall()}
cols = [r[1] for r in conn.execute("PRAGMA table_info(poly_trades)")]
wallet_of = {}
if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'poly_trade_wallets'").fetchone():
    for tx, asset, ts, price, size, side, w in conn.execute(
            "SELECT tx, asset, ts, price, size, side, wallet FROM poly_trade_wallets").fetchall():
        wallet_of[(tx, asset, ts, price, size, side)] = w
trades = []
q = "SELECT tx, condition_id, asset, outcome, side, price, size, ts" + (", wallet" if "wallet" in cols else ", NULL") + " FROM poly_trades"
for tx, cid, asset, outc, side, price, size, ts, w in conn.execute(q).fetchall():
    w = w or wallet_of.get((tx, asset, ts, price, size, side))
    if not w or cid not in final:
        continue
    fin = final[cid] if outc == "Yes" else 1 - final[cid]
    pnl = size * (fin - price) if side == "BUY" else size * (price - fin)
    trades.append((ts, w, cid, outc, side, price, size, pnl))
trades.sort()
print(f"сделок с кошельком и итогом: {len(trades)}, трейдеров: {len({t[1] for t in trades})}")
t0 = trades[0][0]
split = t0 + 14 * 86400
day = lambda ts: datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
print(f"рейтинг: {day(t0)} .. {day(split)}; проверка: {day(split)} .. {day(trades[-1][0])}")

stat = {p: defaultdict(lambda: [0, 0.0, 0.0]) for p in ("fit", "test")}  # n, pnl, объём $
for ts, w, cid, outc, side, price, size, pnl in trades:
    s = stat["fit" if ts < split else "test"][w]
    s[0] += 1
    s[1] += pnl
    s[2] += size * price
fit, test = stat["fit"], stat["test"]
for min_n in (20, 50):
    cand = [w for w in fit if fit[w][0] >= min_n]
    top = sorted(cand, key=lambda w: fit[w][1], reverse=True)[:20]
    bot = sorted(cand, key=lambda w: fit[w][1])[:20]
    for name, grp in (("20 лучших", top), ("20 худших", bot)):
        f_p = sum(fit[w][1] for w in grp)
        t_p = sum(test[w][1] for w in grp if w in test)
        t_v = sum(test[w][2] for w in grp if w in test)
        active = sum(1 for w in grp if w in test)
        print(f"мин. {min_n} сделок, {name}: на рейтинге {f_p:+9.0f}$ -> на проверке {t_p:+9.0f}$ "
              f"(активны {active}/20, оборот {t_v:,.0f}$, {100 * t_p / t_v if t_v else 0:+.1f}% от оборота)")
    # все трейдеры с ≥min_n на рейтинге: совпадает ли знак на проверке
    both = [w for w in cand if w in test and test[w][0] >= 10]
    agree = sum((fit[w][1] > 0) == (test[w][1] > 0) for w in both)
    print(f"   знак результата совпал у {agree} из {len(both)} ({100 * agree / max(len(both), 1):.0f}%; случайно ~50%)")

# повторять покупки 20 лучших (рейтинг по первым двум неделям, мин. 20 сделок)
top = set(sorted([w for w in fit if fit[w][0] >= 20], key=lambda w: fit[w][1], reverse=True)[:20])
by_cid = defaultdict(list)
for t in trades:
    by_cid[(t[2], t[3])].append(t)
res = {"n": 0, "pnl": 0.0, "cost": 0.0, "won": 0}
for ts, w, cid, outc, side, price, size, pnl in trades:
    if ts < split or w not in top or side != "BUY" or not (0.03 <= price <= 0.95):
        continue
    nxt = next((t for t in by_cid[(cid, outc)] if ts < t[0] <= ts + 600 and t[4] == "BUY" and t[5] <= price + 0.02), None)
    if nxt is None:
        continue
    p = nxt[5]
    fin = final[cid] if outc == "Yes" else 1 - final[cid]
    shares = 2.0 / (p + 0.05 * p * (1 - p))
    res["n"] += 1
    res["pnl"] += shares * fin - 2.0
    res["cost"] += 2.0
    res["won"] += fin > 0.5
print(f"повторять покупки 20 лучших (на проверке): ставок {res['n']}, угадано {res['won']}, итог {res['pnl']:+.1f}$ "
      f"на ${res['cost']:.0f} ({100 * res['pnl'] / res['cost'] if res['cost'] else 0:+.1f}%)")


# ---- проверка на ошибку (2026-09-26): не артефакт ли? ----
import random
from zoneinfo import ZoneInfo
city_of = {r[0]: r[1] for r in conn.execute("SELECT condition_id, city FROM poly_market_final").fetchall()}
date_of = {r[0]: r[1] for r in conn.execute("SELECT condition_id, local_date FROM poly_market_final").fetchall()}


def follow(group, label, detail=False):
    res = defaultdict(lambda: [0, 0.0, 0.0])  # ключ -> n, pnl, cost
    per_w = defaultdict(float)
    for ts, w, cid, outc, side, price, size, pnl in trades:
        if ts < split or w not in group or side != "BUY" or not (0.03 <= price <= 0.95):
            continue
        nxt = next((t for t in by_cid[(cid, outc)] if ts < t[0] <= ts + 600 and t[4] == "BUY" and t[5] <= price + 0.02), None)
        if nxt is None:
            continue
        p = nxt[5]
        fin = final[cid] if outc == "Yes" else 1 - final[cid]
        r = 2.0 / (p + 0.05 * p * (1 - p)) * fin - 2.0
        tz = ZoneInfo(OBS_TZ.get(city_of.get(cid), "UTC"))
        lt = datetime.fromtimestamp(ts, tz)
        day_off = (lt.date() - datetime.fromisoformat(date_of[cid]).date()).days if cid in date_of else 0
        keys = ["всего", f"цена {int(p * 10) * 10:02d}-{int(p * 10) * 10 + 10}¢",
                "накануне и раньше" if day_off < 0 else (f"в день маркета до 12:00" if lt.hour < 12 else "в день маркета после 12:00")]
        for k in keys:
            res[k][0] += 1; res[k][1] += r; res[k][2] += 2.0
        per_w[w] += r
    tot = res["всего"]
    print(f"{label}: ставок {tot[0]}, итог {tot[1]:+.0f}$ ({100 * tot[1] / tot[2] if tot[2] else 0:+.1f}%)")
    if detail:
        for k in sorted(res):
            if k != "всего":
                print(f"    {k:26s} ставок {res[k][0]:6d} итог {res[k][1]:+8.0f}$ ({100 * res[k][1] / res[k][2]:+.1f}%)")
        top_w = sorted(per_w.items(), key=lambda x: -x[1])[:3]
        print("    вклад трёх лучших кошельков:", [f"{v:+.0f}$" for _, v in top_w], f"из {tot[1]:+.0f}$")


from weather_cities import OBS_CITIES
OBS_TZ = {c: v["tz"] for c, v in OBS_CITIES.items()}
follow(top, "повторять 20 лучших", detail=True)
active = [w for w in fit if fit[w][0] >= 20]
random.seed(1)
for i in range(3):
    follow(set(random.sample(active, 20)), f"повторять 20 случайных №{i + 1}")
follow(set(bot), "повторять 20 худших")
follow(set(active), "повторять всех активных")


# ---- 2026-09-26: широкий набор «сильных» только по первой половине (без подглядывания) ----
for min_n, min_pct in ((50, 0.0), (50, 0.03), (100, 0.05)):
    grp = {w for w in fit if fit[w][0] >= min_n and fit[w][1] > 0 and fit[w][1] / max(fit[w][2], 1) >= min_pct}
    print(f"\nсильные по первой половине: ≥{min_n} сделок, ≥{min_pct * 100:.0f}% от оборота — {len(grp)} трейдеров")
    follow(grp, "  повторять их покупки", detail=True)


# ---- 2026-09-26: как часто проверять (задержка повтора) ----
def follow_lag(group, lag_s, window_s=300, only_d1=True):
    n = 0; tot = 0.0
    for ts, w, cid, outc, side, price, size, pnl in trades:
        if ts < split or w not in group or side != "BUY" or not (0.03 <= price <= 0.95):
            continue
        if only_d1 and cid in date_of:
            tz = ZoneInfo(OBS_TZ.get(city_of.get(cid), "UTC"))
            if datetime.fromtimestamp(ts, tz).date() >= datetime.fromisoformat(date_of[cid]).date():
                continue
        nxt = next((t for t in by_cid[(cid, outc)] if ts + lag_s <= t[0] <= ts + lag_s + window_s and t[4] == "BUY"
                    and t[5] <= price + 0.02), None)
        if nxt is None:
            continue
        p = nxt[5]
        fin = final[cid] if outc == "Yes" else 1 - final[cid]
        n += 1; tot += 2.0 / (p + 0.05 * p * (1 - p)) * fin - 2.0
    return n, tot


grp = {w for w in fit if fit[w][0] >= 100 and fit[w][1] > 0 and fit[w][1] / max(fit[w][2], 1) >= 0.05}
print(f"\nзадержка повтора (сильные по первой половине: {len(grp)} трейдеров), только покупки накануне:")
for lag in (30, 60, 150, 300, 600):
    n, tot = follow_lag(grp, lag)
    print(f"  через {lag / 60:4.1f} мин: ставок {n:6d}, итог {tot:+8.1f}$ ({100 * tot / (2 * n) if n else 0:+.1f}%)")
