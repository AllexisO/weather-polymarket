"""
Бот-мейкер на истории по НАСТОЯЩИМ стаканам (2026-09-30, стаканы Falcon — weather_falcon_fetch.py; Alex: «проверим быстро,
чтобы к 14.10 иметь другие варианты»). Для каждой настоящей сделки (poly_trades) — последний снимок стакана до неё:
  • если между лучшей заявкой и предложением ≥ 0.3¢ — бот встаёт на 0.1¢ лучше и он первый в очереди;
  • иначе встаёт на лучшую цену в конец очереди: ему достаётся только то, что осталось после стоявших впереди (объём уровня
    из снимка, уменьшается сделками по этой цене, пока заявка та же);
  • исполнение — по цене нашей заявки, если сделка до неё дошла; 10 долей на заявку (новая цена — новая заявка), перекос ≤ 30,
    пары «да»+«нет» склеиваются в $1, остаток — к итогу, возврат мейкеру 25% комиссии; паузы как у живого бота (−1…+3 мин
    вокруг плановой сводки METAR, после 18:00 местного в день маркета), цена стороны 2-98¢; снимок старше STALE — не котируем.
Две половины: 19.08-07.09 (подбор) и 08.09-27.09 (проверка).
Порог (записан до прогона, как у живого бота): все заявки — ≥ +1% от потраченного в ОБЕИХ половинах и ≥ 2000 исполнений;
или правила, выбранные на первой половине (время × цена стороны: в плюсе и ≥ 200 исполнений), — ≥ +1% на второй.
Только на копии базы.
"""
import bisect
import glob
import gzip
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from weather_cities import OBS_CITIES

DB = "/data/research/research.sqlite3"
SPLIT = "2026-09-08"
import os
STALE = int(os.environ.get("STALE", "300"))
SIZE, L_MAX, TICK = 10.0, 30.0, 0.001
REBATE = 0.0 if os.environ.get("NO_REBATE") else 0.25 * 0.05
JOIN_ONLY = bool(os.environ.get("JOIN_ONLY"))  # худший случай: никогда не первые — встаём в конец очереди на лучшей цене
conn = sqlite3.connect(DB)
final = {r[0]: r[1] for r in conn.execute("SELECT condition_id, final_yes FROM poly_market_final")}
try:
    MINS = json.loads(open("/data/db/mm_metar_minutes.json").read())
except OSError:
    MINS = {}
PB = ((0, .1), (.1, .3), (.3, .5), (.5, .7), (.7, .9), (.9, 1.01))


def zone(loc, ld):
    if loc.date().isoformat() < ld:
        return "накануне"
    return next(f"{a}-{b}" for a, b in ((0, 6), (6, 9), (9, 12), (12, 15), (15, 18), (18, 24)) if a <= loc.hour < b)


def run_bucket(cid, book, trades, city, ld, allow=None):
    """→ список исполнений (ts, сторона, цена, доли, первый в очереди, зона) и итог по маркету."""
    fin = final.get(cid)
    if fin is None or not book:
        return None
    tz = ZoneInfo(OBS_CITIES[city]["tz"])
    times = [s[0] for s in book]
    used, ahead = {}, {}
    ys = ns = yc = nc = spent = mpnl = reb = 0.0
    fills = []
    covered = 0
    for ts, y, hits_yes, size in trades:
        i = bisect.bisect_right(times, ts) - 1
        if i < 0 or ts - book[i][0] > STALE:
            continue
        covered += 1
        loc = datetime.fromtimestamp(ts, tz)
        if loc.date().isoformat() > ld or (loc.date().isoformat() == ld and loc.hour >= 18):
            continue
        mn = datetime.fromtimestamp(ts, timezone.utc).minute
        if any((mn - x) % 60 <= 3 or (x - mn) % 60 <= 1 for x in MINS.get(city, [])):
            continue
        _, bids, asks = book[i]
        if not bids or not asks:
            continue
        bb, bbs = bids[0]
        ba, bas = asks[0]
        improve = ba - bb >= 3 * TICK - 1e-9 and not JOIN_ONLY
        if hits_yes:
            price, qa = (round(bb + TICK, 3), 0.0) if improve else (bb, bbs)
            if not (0.02 <= price <= 0.98) or y > price + 1e-9 or ys - ns >= L_MAX:
                continue
        else:
            price, qa = (round(1 - ba + TICK, 3), 0.0) if improve else (round(1 - ba, 3), bas)
            if not (0.02 <= price <= 0.98) or y < 1 - price - 1e-9 or ns - ys >= L_MAX:
                continue
        side = "yes" if hits_yes else "no"
        z = zone(loc, ld)
        pb = next(k for k, (a, b) in enumerate(PB) if a <= price < b)
        if allow is not None and (not allow(z, price, ba - bb, cid, ts, side) if callable(allow) else (z, pb) not in allow):
            continue
        key = (i, side)                      # заявка = снимок × сторона
        if key not in ahead:
            ahead[key] = qa
        k_avail = size
        if ahead[key] > 0:                   # сначала съедают стоявших впереди
            eat = min(ahead[key], k_avail)
            ahead[key] -= eat
            k_avail -= eat
        room = L_MAX - (ys - ns) if hits_yes else L_MAX - (ns - ys)
        k = min(k_avail, SIZE - used.get(key, 0.0), room)
        if k <= 1e-9:
            continue
        used[key] = used.get(key, 0.0) + k
        if hits_yes:
            ys += k; yc += k * price
        else:
            ns += k; nc += k * price
        spent += k * price
        reb += REBATE * price * (1 - price) * k
        pay = fin if hits_yes else 1 - fin
        fills.append((ts, side, price, k, improve, z, pb, k * (pay - price)))
        pr = min(ys, ns)
        if pr > 0:
            ay, an = yc / ys, nc / ns
            mpnl += pr * (1 - ay - an)
            ys -= pr; ns -= pr; yc -= pr * ay; nc -= pr * an
    inv = ys * fin - yc + ns * (1 - fin) - nc
    return {"fills": fills, "spent": spent, "pnl": mpnl + inv + reb, "merge": mpnl, "inv": inv, "reb": reb, "covered": covered, "n_tr": len(trades)}


def load():
    data = []
    for f in glob.glob(os.environ.get("BOOK_DIR", "/data/research/falcon/book") + "/*.json.gz"):  # 05.10: BOOK_DIR — другой период
        cid = f.split("/")[-1].split(".")[0]
        d = json.load(gzip.open(f, "rt"))
        if d["city"] not in OBS_CITIES or not d["snaps"]:
            continue
        seen, trades = set(), []
        for tx, outc, side, p, size, ts in conn.execute(
                "SELECT tx, outcome, side, price, size, ts FROM poly_trades WHERE condition_id = ? ORDER BY ts", (cid,)):
            k = (tx, outc, side, p, size, ts)
            if k in seen or not (0 < p < 1):
                continue
            seen.add(k)
            y = p if outc == "Yes" else 1 - p
            trades.append((ts, y, (outc == "Yes" and side == "SELL") or (outc == "No" and side == "BUY"), size))
        data.append((cid, d["snaps"], trades, d["city"], d["local_date"]))
    return data


def summarize(name, results):
    for half in ("19.08-07.09", "08.09-27.09"):
        rs = [r for (ld, r) in results if r and ((ld < SPLIT) == (half.startswith("19")))]
        sp = sum(r["spent"] for r in rs); pn = sum(r["pnl"] for r in rs); nf = sum(len(r["fills"]) for r in rs)
        sh = sum(f[3] for r in rs for f in r["fills"])
        first = sum(f[3] for r in rs for f in r["fills"] if f[4])
        print(f"  {name:28s} {half}: вариантов {len(rs):4d}, исполнений {nf:6d}, потрачено ${sp:8,.0f}, итог ${pn:+8,.0f} "
              f"({100 * pn / max(sp, 1):+6.2f}%, {100 * pn / max(sh, 1):+5.2f}¢/долю); первыми в очереди {100 * first / max(sh, 1):.0f}% долей; "
              f"склейки ${sum(r['merge'] for r in rs):+,.0f}, остаток ${sum(r['inv'] for r in rs):+,.0f}, возврат ${sum(r['reb'] for r in rs):+,.0f}",
              flush=True)


def main():
    data = load()
    cov = sum(1 for d in data for _ in d[2])
    print(f"вариантов со стаканом {len(data)}, их сделок {cov}", flush=True)
    base = [(ld, run_bucket(cid, b, t, c, ld)) for cid, b, t, c, ld in data]
    covered = sum(r["covered"] for _, r in base if r); ntr = sum(r["n_tr"] for _, r in base if r)
    print(f"сделок со свежим стаканом (≤ {STALE} с): {100 * covered / max(ntr, 1):.0f}%", flush=True)
    summarize("все заявки (как живой бот)", base)
    # подбор зон на первой половине
    seg = defaultdict(lambda: [0.0, 0.0, 0])
    for ld, r in base:
        if r and ld < SPLIT:
            for f in r["fills"]:
                s = seg[(f[5], f[6])]; s[0] += f[7]; s[1] += f[2] * f[3]; s[2] += 1
    allow = {k for k, v in seg.items() if v[2] >= 200 and v[0] > 0}
    print("\nзоны (время × цена стороны) на 19.08-07.09 — итог по исполнениям к итогу маркета, без склеек:")
    for k, v in sorted(seg.items(), key=lambda x: -x[1][0])[:12]:
        print(f"  {k[0]:9s} {int(PB[k[1]][0] * 100):3d}-{int(min(PB[k[1]][1], 1) * 100):3d}¢  исполнений {v[2]:5d}  ${v[0]:+8.1f} ({100 * v[0] / max(v[1], 1):+.1f}%)")
    print(f"выбрано зон: {len(allow)}: " + ", ".join(f"{z} {int(PB[b][0] * 100)}-{int(min(PB[b][1], 1) * 100)}¢" for z, b in sorted(allow)))
    if not (JOIN_ONLY or os.environ.get("STALE") or os.environ.get("NO_REBATE")):
        json.dump([[z, PB[b][0], PB[b][1]] for z, b in sorted(allow)], open("/data/research/mm_zones.json", "w"), ensure_ascii=False)
    sel = [(ld, run_bucket(cid, b, t, c, ld, allow)) for cid, b, t, c, ld in data]
    summarize("выбранные зоны", sel)
    # устойчивость на проверочной половине: по неделям и без лучших маркетов
    from datetime import date as _d
    wk = defaultdict(lambda: [0.0, 0.0])
    per = []
    for ld, r in sel:
        if r and ld >= SPLIT:
            w = _d.fromisoformat(ld); w = (w.toordinal() - _d.fromisoformat(SPLIT).toordinal()) // 7
            wk[w][0] += r["pnl"]; wk[w][1] += r["spent"]; per.append(r["pnl"])
    print("проверочная половина по неделям: " + ", ".join(f"нед.{w + 1} ${v[0]:+,.0f} ({100 * v[0] / max(v[1], 1):+.1f}%)" for w, v in sorted(wk.items())))
    per.sort(reverse=True)
    tot = sum(per)
    print(f"без 10 лучших маркетов: ${tot - sum(per[:10]):+,.0f} (было ${tot:+,.0f}); без 50 лучших: ${tot - sum(per[:50]):+,.0f}; "
          f"маркетов в плюсе {sum(p > 0 for p in per)} из {len(per)}")


if __name__ == "__main__":
    main()
