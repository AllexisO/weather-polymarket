"""
Трейдеры из рейтинга rainbot.finance/leaderboard (2026-09-29, Alex: «золотая жила — Poligarch и др., почему не копировать?»).
По нашим настоящим сделкам (poly_trades, с 21.08): как они торгуют — цены, покупки/продажи, когда (накануне / в день),
итог при удержании до закрытия и что дал бы повтор (их покупки, наша цена = их цена + 1¢, с комиссией taker).
Только на копии базы. Вход: /data/research/rb_lb.json (выгрузка rainbot.finance/api/leaderboard).
"""
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from weather_cities import OBS_CITIES

DB = "/data/research/research.sqlite3"
LB = json.load(open("/data/research/rb_lb.json"))["traders"]
conn = sqlite3.connect(DB)
final = dict(conn.execute("SELECT condition_id, final_yes FROM poly_market_final").fetchall())
sharp = {r[0].lower() for r in conn.execute("SELECT * FROM sharp_wallets").fetchall()} if conn.execute(
    "SELECT 1 FROM sqlite_master WHERE name='sharp_wallets'").fetchone() else set()
cols = [r[1] for r in conn.execute("PRAGMA table_info(sharp_wallets)")]
print("sharp_wallets:", cols, len(sharp))
span = conn.execute("SELECT MIN(ts), MAX(ts) FROM poly_trades").fetchone()
print("сделки у нас:", datetime.fromtimestamp(span[0], timezone.utc).date(), "…", datetime.fromtimestamp(span[1], timezone.utc).date())
fee = lambda p: 0.05 * p * (1 - p)
print(f"{'трейдер':22s} {'сделок':>7s} {'оборот':>10s} {'покуп%':>6s} {'ср.цена':>7s} {'<10¢%':>6s} {'>90¢%':>6s} {'в день%':>7s} {'итог':>9s} {'%обор':>6s} | повтор покупок: {'n':>5s} {'%':>6s} накануне {'%':>6s}")
for t in LB:
    a = t["address"].lower()
    rows = conn.execute("SELECT ts, price, size, side, condition_id, outcome, city, local_date FROM poly_trades WHERE lower(wallet) = ?", (a,)).fetchall()
    if not rows:
        print(f"{t['name'][:22]:22s} — сделок в нашей базе нет (оборот rainbot ${t['volume']:,.0f})")
        continue
    n = len(rows); vol = sum(p * s for _, p, s, *_ in rows)
    buys = [r for r in rows if r[3] == "BUY"]
    pnl = 0.0; cp = [0, 0.0, 0.0]; cpe = [0, 0.0, 0.0]; sameday = 0
    for ts, p, s, side, cid, outc, city, ld in rows:
        tz = ZoneInfo(OBS_CITIES.get(city, {}).get("tz", "UTC"))
        if datetime.fromtimestamp(ts, tz).date().isoformat() == ld:
            sameday += 1
        if cid not in final:
            continue
        fin = final[cid] if outc == "Yes" else 1 - final[cid]
        pnl += s * (fin - p) if side == "BUY" else s * (p - fin)
        if side == "BUY" and 0.02 <= p <= 0.95:
            q = min(p + 0.01, 0.99)  # наша цена: на 1¢ хуже
            r_ = (fin - q - fee(q)) / (q + fee(q))
            cp[0] += 1; cp[1] += r_
            if datetime.fromtimestamp(ts, tz).date().isoformat() < ld:
                cpe[0] += 1; cpe[1] += r_
    lo = sum(1 for r in buys if r[1] < 0.10) / max(len(buys), 1)
    hi = sum(1 for r in buys if r[1] > 0.90) / max(len(buys), 1)
    avgp = sum(r[1] * r[2] for r in buys) / max(sum(r[2] for r in buys), 1)
    print(f"{t['name'][:22]:22s} {n:7d} {vol:10,.0f} {100*len(buys)/n:6.0f} {avgp:7.2f} {100*lo:6.0f} {100*hi:6.0f} {100*sameday/n:7.0f} {pnl:+9,.0f} {100*pnl/max(vol,1):+6.1f} | {cp[0]:5d} {100*cp[1]/max(cp[0],1):+6.1f}   {cpe[0]:5d} {100*cpe[1]/max(cpe[0],1):+6.1f}{'  [в нашем рейтинге]' if a in sharp else ''}")
