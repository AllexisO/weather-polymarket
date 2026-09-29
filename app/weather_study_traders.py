"""
Зарабатывают ли «легенды» из статьи Moonsat в сумме (2026-09-29): последние сделки (data-api Polymarket) по погоде,
итог каждой покупки при удержании до итога маркета (Gamma: outcomePrices закрытого маркета), по полосам цены.
Только публичные данные Polymarket. Запуск: python weather_study_traders.py
"""
import json
import time
from collections import defaultdict

import requests

TRADERS = {"HondaCivic": None, "ShyGuy1": None, "OnlyLuckNoBrain": None, "0X3573": None, "WeatherHK": None}
if len(__import__("sys").argv) > 1:  # имена из командной строки
    TRADERS = dict.fromkeys(__import__("sys").argv[1:])
S = requests.Session()


def js(url, **p):
    for _ in range(3):
        try:
            r = S.get(url, params=p, timeout=30)
            if r.ok:
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(2)
    return None


def wallet(name):
    d = js("https://gamma-api.polymarket.com/public-search", q=name, search_profiles="true", limit_per_type=5) or {}
    p = [x for x in d.get("profiles", []) if (x.get("name") or "").lower() == name.lower()]
    return p[0]["proxyWallet"] if p else None


def band(p):
    return "<2¢" if p < .02 else "2-10¢" if p < .10 else "10-40¢" if p < .40 else "40-80¢" if p < .80 else "≥80¢"


final = {}
for name in TRADERS:
    a = wallet(name)
    t = js("https://data-api.polymarket.com/trades", user=a, limit=500) if a else None
    w = [x for x in (t or []) if "temperature" in (x.get("title") or "").lower() and x["side"] == "BUY"]
    need = sorted({x["conditionId"] for x in w} - set(final))
    for i in range(0, len(need), 20):
        ms = js("https://gamma-api.polymarket.com/markets", condition_ids=need[i:i + 20], closed="true", limit=50) or []
        for m in ms:
            try:
                final[m["conditionId"]] = [float(v) for v in json.loads(m["outcomePrices"])]
            except (KeyError, ValueError):
                pass
        time.sleep(0.2)
    agg = defaultdict(lambda: [0, 0.0, 0.0, 0])
    for x in w:
        f = final.get(x["conditionId"])
        if not f or max(f) < 0.99:
            continue  # не закрыт
        pay = f[0] if x["outcome"] == "Yes" else f[1]
        k = f"{'да' if x['outcome'] == 'Yes' else 'нет'} {band(x['price'])}"
        a_ = agg[k]
        a_[0] += 1; a_[1] += x["price"] * x["size"]; a_[2] += (pay - x["price"]) * x["size"]; a_[3] += pay > .5
    sp, pl = sum(v[1] for v in agg.values()), sum(v[2] for v in agg.values())
    print(f"\n{name}: закрытых покупок {sum(v[0] for v in agg.values())} из {len(w)}, потрачено ${sp:,.0f}, итог ${pl:+,.0f} "
          f"({pl / max(sp, 1) * 100:+.1f}%)", flush=True)
    for k in sorted(agg, key=lambda k: -agg[k][1]):
        n, s, p_, won = agg[k]
        print(f"   {k:9s}: {n:4d} покупок, ${s:7,.0f}, итог ${p_:+7,.0f} ({p_ / max(s, .01) * 100:+6.1f}%), сыграло {won / n * 100:4.1f}%")
