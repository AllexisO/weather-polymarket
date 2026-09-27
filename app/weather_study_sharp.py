"""
Что делают трейдеры, стабильно зарабатывающие на погоде (2026-09-26, пункт 2 от Alex).
«Сильные» — ≥50 сделок и плюс в ОБОИХ половинах периода (первые 14 дней и остальное).
Сравниваем их покупки со всеми остальными:
- когда: за сколько часов до начала дня маркета (местное время) / в какой час дня;
- сразу ли после нового замера станции (минут с последнего METAR);
- по какой цене; «да» или «нет»; какой вариант: фаворит рынка, соседний, дальний;
- куда пошла цена через 1 час после их покупки (признак информированности);
- совпадает ли покупка с мнением нашей модели v3 в 08:00 (walk-forward).
Запуск — на копии базы: POLY_LAB_DB=/data/research/research.sqlite3
"""

import json
import os
import sqlite3
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from weather_cities import OBS_CITIES

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
conn = sqlite3.connect(DB_PATH, timeout=60)

mkt = {r[0]: r[1:] for r in conn.execute(
    "SELECT condition_id, city, local_date, bucket_lo, bucket_hi, final_yes FROM poly_market_final").fetchall()}
wallet_of = {}
for tx, asset, ts, price, size, side, w in conn.execute(
        "SELECT tx, asset, ts, price, size, side, wallet FROM poly_trade_wallets").fetchall():
    wallet_of[(tx, asset, ts, price, size, side)] = w
cols = [r[1] for r in conn.execute("PRAGMA table_info(poly_trades)")]
rows = conn.execute("SELECT tx, condition_id, asset, outcome, side, price, size, ts"
                    + (", wallet" if "wallet" in cols else ", NULL") + " FROM poly_trades").fetchall()
trades, seen = [], set()
for tx, cid, asset, outc, side, price, size, ts, w in rows:
    w = w or wallet_of.get((tx, asset, ts, price, size, side))
    k = (tx, asset, ts, price, size, side)
    if not w or cid not in mkt or k in seen:
        continue
    seen.add(k)
    city, d, lo, hi, fy = mkt[cid]
    fin = fy if outc == "Yes" else 1 - fy
    trades.append({"ts": ts, "w": w, "cid": cid, "outc": outc, "side": side, "p": price, "size": size,
                   "city": city, "date": d, "lo": lo, "hi": hi,
                   "pnl": size * (fin - price) if side == "BUY" else size * (price - fin)})
trades.sort(key=lambda t: t["ts"])
split = trades[0]["ts"] + 14 * 86400
st = defaultdict(lambda: {"n": 0, "a": 0.0, "b": 0.0})
for t in trades:
    s = st[t["w"]]
    s["n"] += 1
    s["a" if t["ts"] < split else "b"] += t["pnl"]
sharp = {w for w, s in st.items() if s["n"] >= 50 and s["a"] > 0 and s["b"] > 0}
tot = sum(s["a"] + s["b"] for w, s in st.items() if w in sharp)
print(f"сделок {len(trades)}, трейдеров {len(st)}; «сильных» (≥50 сделок, плюс в обеих половинах): {len(sharp)}, "
      f"вместе {tot:+,.0f}$")

# цены по времени для каждой доли (Yes-цена маркета): для «куда пошла цена» и «фаворит»
yes_px = defaultdict(list)
for t in trades:
    yes_px[t["cid"]].append((t["ts"], t["p"] if t["outc"] == "Yes" else 1 - t["p"]))
by_event = defaultdict(set)
for cid, (city, d, lo, hi, fy) in mkt.items():
    by_event[(city, d)].add(cid)


def yes_at(cid, ts):
    s = yes_px.get(cid)
    if not s:
        return None
    i = bisect_right(s, (ts, 2.0)) - 1
    return s[i][1] if i >= 0 else None


# METAR: время последнего замера станции до сделки
metar = defaultdict(list)
for city, v in conn.execute("SELECT city, valid_utc FROM station_obs WHERE city NOT LIKE 'nb:%'").fetchall():
    metar[city].append(datetime.fromisoformat(v).replace(tzinfo=timezone.utc).timestamp())
for c in metar:
    metar[c].sort()

# наша модель v3 в 08:00 (walk-forward): шанс по вариантам
import weather_ml_q as mq
model = {}
for city, d, unit, qs in conn.execute("SELECT city, date, unit, qs FROM ml_preds_var_mkt").fetchall():
    model[(city, d)] = (unit, json.loads(qs))

groups = {"сильные": defaultdict(lambda: [0, 0.0, 0.0]), "остальные": defaultdict(lambda: [0, 0.0, 0.0])}
move = {"сильные": [0, 0.0], "остальные": [0, 0.0]}
agree = {"сильные": [0, 0], "остальные": [0, 0]}
for t in trades:
    if t["side"] != "BUY" or not (0.02 <= t["p"] <= 0.98):
        continue
    g = "сильные" if t["w"] in sharp else "остальные"
    tz = ZoneInfo(OBS_CITIES[t["city"]]["tz"]) if t["city"] in OBS_CITIES else timezone.utc
    day0 = datetime.fromisoformat(t["date"]).replace(tzinfo=tz).timestamp()
    h = (t["ts"] - day0) / 3600
    when = ("за сутки и раньше" if h < -24 else "накануне вечером/ночью" if h < 0 else
            "в день: 00-08" if h < 8 else "в день: 08-12" if h < 12 else "в день: 12-16" if h < 16 else "в день: после 16")
    ms = metar.get(t["city"], [])
    i = bisect_right(ms, t["ts"]) - 1
    since = (t["ts"] - ms[i]) / 60 if i >= 0 else None
    sm = ("≤5 мин после METAR" if since is not None and since <= 5 else "5-15 мин" if since is not None and since <= 15
          else "позже 15 мин")
    # фаворит / соседний / дальний — по Yes-ценам вариантов в момент сделки
    ev = [(yes_at(c, t["ts"]), mkt[c][2]) for c in by_event[(t["city"], t["date"])]]
    ev = [(p, lo) for p, lo in ev if p is not None]
    pos = "?"
    if ev:
        fav_lo = max(ev)[1]
        los = sorted(lo for _, lo in ev)
        k = abs(los.index(t["lo"]) - los.index(fav_lo)) if t["lo"] in los and fav_lo in los else None
        pos = "фаворит" if k == 0 else "соседний" if k == 1 else "дальний" if k is not None else "?"
    what = f"{'«да»' if t['outc'] == 'Yes' else '«нет»'} на {pos}"
    px = f"цена {int(t['p'] * 5) * 20:02d}-{int(t['p'] * 5) * 20 + 20}¢"
    cost = t["size"] * t["p"]
    for key in (when, sm, what, px, "всего"):
        s = groups[g][key]
        s[0] += 1
        s[1] += t["pnl"]
        s[2] += cost
    later = yes_at(t["cid"], t["ts"] + 3600)
    now = t["p"] if t["outc"] == "Yes" else 1 - t["p"]
    if later is not None:
        move[g][0] += 1
        move[g][1] += (later - now) * (1 if t["outc"] == "Yes" else -1)
    m = model.get((t["city"], t["date"]))
    if m:
        pm = mq.bucket_prob(m[1], m[0], t["lo"], t["hi"])
        yes_p = now
        agree[g][0] += 1
        agree[g][1] += (pm > yes_p) == (t["outc"] == "Yes")

for g in groups:
    print(f"\n=== {g} (покупки) ===")
    for key in sorted(groups[g], key=lambda k: -groups[g][k][2]):
        n, pnl, cost = groups[g][key]
        if n >= 30:
            print(f"  {key:26s} покупок {n:7d}  оборот {cost:11,.0f}$  итог {pnl:+10,.0f}$ ({100 * pnl / cost:+5.1f}%)")
    print(f"  цена через 1 час сдвинулась в их сторону в среднем на {100 * move[g][1] / max(move[g][0], 1):+.2f}¢")
    print(f"  покупка совпадает с мнением нашей модели (08:00): {100 * agree[g][1] / max(agree[g][0], 1):.0f}% "
          f"из {agree[g][0]}")
