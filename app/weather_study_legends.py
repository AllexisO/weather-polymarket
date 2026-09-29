"""
Что на самом деле делают «легенды» погоды (2026-09-29, ссылки Alex): gopfan2 (правило из статей: «да» < 15¢,
«нет» > 45¢, ≤ $1 на ставку), WeatherHK (Азия, дешёвые «да»). Их сделки — в poly_trades (кошелёк известен не у всех
сделок). Итог каждой покупки — если держать до итога маркета (poly_market_final). Только на копии базы.
"""
import sys
from collections import defaultdict

import weather_study_0926 as base

conn = base.conn
WALLETS = {"gopfan2": "0xf2f6af4f27ec2dcf4072095ab804016e14cd5817", "gopfan2.0": "0x9770519bab54c8a89dfe0205cf1c176096fad8d5",
           "WeatherHK": "0x488c725253fc21c7a9ca812030dc2f6343f98c1c", "WeatherHK2": "0xdadbf9e1df1b8d7a184a0d6ab9c83b2337b61870"}
final = {r[0]: r[1] for r in conn.execute("SELECT condition_id, final_yes FROM poly_market_final")}
cov = conn.execute("SELECT COUNT(*), SUM(wallet IS NOT NULL) FROM poly_trades").fetchone()
print(f"сделок в базе {cov[0]}, с известным кошельком {cov[1]} ({cov[1] / cov[0] * 100:.0f}%)")
for name, addr in WALLETS.items():
    rows = conn.execute("SELECT condition_id, outcome, side, price, size, city FROM poly_trades WHERE lower(wallet) = ?", (addr,)).fetchall()
    if not rows:
        print(f"\n{name}: сделок в базе нет")
        continue
    buys = [r for r in rows if r[2] == "BUY"]
    bands = defaultdict(lambda: [0, 0.0, 0.0, 0])  # n, потрачено, итог, выиграло
    cities = defaultdict(float)
    for cid, o, sd, p, sz, city in buys:
        f = final.get(cid)
        if f is None:
            continue
        pay = f if o == "Yes" else 1 - f
        key = ("да " if o == "Yes" else "нет ") + ("<5¢" if p < .05 else "5-15¢" if p < .15 else "15-45¢" if p < .45 else "45-85¢" if p < .85 else "≥85¢")
        b = bands[key]
        b[0] += 1; b[1] += p * sz; b[2] += (pay - p) * sz; b[3] += pay > .5
        cities[city] += (pay - p) * sz
    spent = sum(v[1] for v in bands.values()); pnl = sum(v[2] for v in bands.values())
    print(f"\n{name}: сделок {len(rows)} (покупок {len(buys)}, продаж {len(rows) - len(buys)}), рассчитанных покупок "
          f"{sum(v[0] for v in bands.values())}: потрачено ${spent:.0f}, итог ${pnl:+.0f} ({pnl / max(spent, 1) * 100:+.1f}%) — "
          f"средняя покупка ${spent / max(sum(v[0] for v in bands.values()), 1):.2f}")
    for k in sorted(bands, key=lambda k: -bands[k][1]):
        n, s, pl, w = bands[k]
        print(f"   {k:10s}: {n:5d} покупок, потрачено ${s:7.0f}, итог ${pl:+7.0f} ({pl / max(s, .01) * 100:+6.1f}%), сыграло {w / n * 100:4.1f}%")
    top = sorted(cities.items(), key=lambda x: -abs(x[1]))[:6]
    print("   города: " + ", ".join(f"{c} ${v:+.0f}" for c, v in top))
