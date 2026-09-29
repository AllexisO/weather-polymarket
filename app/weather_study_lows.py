"""
Разведка маркетов МИНИМАЛЬНОЙ температуры (2026-09-29, Alex: «не зря ли поднимать целую систему»). Только публичные
данные Polymarket (Gamma — события и итоги, CLOB prices-history — почасовые цены), без нашей базы и Open-Meteo.

Отвечает на три вопроса:
1. когда рынок «узнаёт» минимум — логошибка цены выигравшего варианта по часам (накануне 16:00 … сегодня 10:00)
   и час, когда выигравший вариант впервые стоит ≥ 90¢; для сравнения — те же цифры по МАКСИМУМУ на тех же днях;
2. есть ли перекос дешёвых / дорогих вариантов (как у no_cheap / no_mid / fav) — цена против доли сбывшихся;
3. объёмы маркетов.
Запуск: python weather_study_lows.py [дней]  → data/research/study_lows.log (печать) и study_lows.json (сырые цены).
"""

import json
import math
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from weather_cities import OBS_CITIES

GAMMA, CLOB = "https://gamma-api.polymarket.com", "https://clob.polymarket.com"
OUT = Path("/data/research/study_lows.json")
CITIES = ["ankara", "atlanta", "austin", "buenos-aires", "cape-town", "chengdu", "chicago", "chongqing", "helsinki",
          "hong-kong", "istanbul", "jeddah", "london", "los-angeles", "madrid", "miami", "milan", "moscow", "munich", "nyc",
          "paris", "san-francisco", "sao-paulo", "seattle", "seoul", "shanghai", "shenzhen", "taipei", "tel-aviv", "tokyo",
          "warsaw", "wellington", "wuhan"]
EXTRA_TZ = {"hong-kong": "Asia/Hong_Kong", "taipei": "Asia/Taipei"}
HOURS = [-8, -4, 0, 3, 5, 7, 8, 10]  # относительно полуночи дня маркета, местное время
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
S = requests.Session()


def tz_of(c):
    if c in EXTRA_TZ:
        return ZoneInfo(EXTRA_TZ[c])
    return ZoneInfo(OBS_CITIES[c.replace("-", "_")]["tz"])


def get(url, **p):
    for i in range(4):
        try:
            r = S.get(url, params=p, timeout=30)
            if r.status_code == 429:
                time.sleep(3 + 3 * i)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            time.sleep(2)
    return None


def event(kind, c, d):
    slug = f"{kind}-temperature-in-{c}-on-{MONTHS[d.month - 1]}-{d.day}-{d.year}"
    e = get(f"{GAMMA}/events", slug=slug)
    return e[0] if e else None


def series(ev, tz, d):
    """{вариант: [(ts, цена)]} за накануне 12:00 … день 12:00; выигравший вариант; объём."""
    t0 = int(datetime(d.year, d.month, d.day, tzinfo=tz).timestamp())
    out, win = {}, None
    for m in ev.get("markets", []):
        try:
            tok = json.loads(m["clobTokenIds"])[0]
            fin = json.loads(m.get("outcomePrices") or "[]")
        except (KeyError, ValueError):
            continue
        h = get(f"{CLOB}/prices-history", market=tok, startTs=t0 - 12 * 3600, endTs=t0 + 12 * 3600, fidelity=60)
        pts = [(x["t"], x["p"]) for x in (h or {}).get("history", [])]
        out[m["question"]] = pts
        if fin and float(fin[0]) > 0.99:
            win = m["question"]
        time.sleep(0.05)
    return out, win, float(ev.get("volume") or 0), t0


def at(pts, ts):
    p = None
    for t, v in pts:
        if t <= ts:
            p = v
        else:
            break
    return p


def analyse(recs, name):
    ll = {h: [] for h in HOURS}
    decided, cal = [], {}
    for r in recs:
        s, win, t0 = r["series"], r["win"], r["t0"]
        if not win or len(s) < 3:
            continue
        row = {}
        for h in HOURS:
            ps = {q: at(pts, t0 + h * 3600) for q, pts in s.items()}
            if any(v is None for v in ps.values()):
                break
            tot = sum(ps.values()) or 1
            row[h] = -math.log(max(ps[win] / tot, 1e-4))
            if h in (-4, 8):
                for q, v in ps.items():
                    band = ("0-5¢" if v < .05 else "5-15¢" if v < .15 else "15-30¢" if v < .30 else "30-55¢" if v < .55 else "55-95¢" if v < .95 else "95¢+")
                    c = cal.setdefault((h, band), [0, 0.0, 0])
                    c[0] += 1; c[1] += v; c[2] += q == win
        else:
            for h in HOURS:
                ll[h].append(row[h])
            first = next((t for t, v in s[win] if v >= 0.9), None)
            if first:
                decided.append((first - t0) / 3600)
    n = len(ll[HOURS[0]])
    print(f"\n=== {name}: маркетов с итогом и ценами во все часы — {n} ===")
    if not n:
        return
    for h in HOURS:
        lbl = f"накануне {24 + h:02d}:00" if h < 0 else f"{h:02d}:00"
        print(f"  {lbl:15s} логошибка рынка {sum(ll[h]) / n:.3f}")
    if decided:
        decided.sort()
        q = lambda p: decided[int(p * (len(decided) - 1))]
        print(f"  выигравший вариант впервые ≥ 90¢: медиана {q(.5):+.1f} ч от полуночи дня маркета "
              f"(четверть раньше {q(.25):+.1f} ч, четверть позже {q(.75):+.1f} ч)")
    for h in (-4, 8):
        print(f"  цена → сбылось ({'накануне 20:00' if h < 0 else '08:00'}): " + " · ".join(
            f"{b}: {c[1] / c[0] * 100:.1f}%→{c[2] / c[0] * 100:.1f}% ({c[0]})" for (hh, b), c in sorted(cal.items()) if hh == h))
    vols = sorted(r["vol"] for r in recs)
    print(f"  объём маркета: медиана ${vols[len(vols) // 2]:,.0f}, четверть больше ${vols[3 * len(vols) // 4]:,.0f}")


if __name__ == "__main__":
    ndays = int(sys.argv[1]) if len(sys.argv) > 1 else 14
    end = date(2026, 9, 27)
    data = {"lowest": [], "highest": []}
    for c in CITIES:
        tz = tz_of(c)
        for i in range(ndays):
            d = end - timedelta(days=i)
            for kind in ("lowest", "highest"):
                ev = event(kind, c, d)
                if not ev or not ev.get("closed"):
                    continue
                s, win, vol, t0 = series(ev, tz, d)
                data[kind].append({"city": c, "date": d.isoformat(), "series": s, "win": win, "vol": vol, "t0": t0})
        print(f"{c}: минимум {sum(r['city'] == c for r in data['lowest'])}, максимум {sum(r['city'] == c for r in data['highest'])}", flush=True)
        OUT.write_text(json.dumps(data))
    analyse(data["lowest"], "МИНИМУМ")
    analyse(data["highest"], "МАКСИМУМ (те же города и дни, для сравнения)")
