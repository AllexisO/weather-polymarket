"""
Расчёт виртуального бота-мейкера (2026-09-30, к weather_mm_paper.py): какие его заявки исполнились бы и сколько он заработал.
Раз в сутки после сбора настоящих сделок (weather_trades_history.py, 04:30): для каждого закрытого маркета, где бот держал
заявки, берём сделки (poly_trades — сделки тех, кто забирал заявки) и проверяем по времени:
  сделка «продажа да» / «покупка нет» по цене y (в ценах «да») бьёт заявки на покупку «да»: наша заявка b исполняется, если
  в этот момент она стояла и y ≤ b (мы стояли первыми — на 0.1¢ лучше остальных); иначе — заявки на «нет» (y ≥ 1 − q);
  исполняется min(размер сделки, что осталось от 10 долей этой заявки); непарный остаток ≤ L_MAX долей (дальше тяжёлую
  сторону не котируем); пары «да»+«нет» склеиваются в $1; остаток — до итога маркета; возврат мейкеру 25% комиссии
  забирающего (0.25 × 0.05 × цена × (1 − цена) на долю). Мейкер комиссию не платит.
Кошельки: mm_all — все заявки бота, mm_sel — только в «выгодных зонах» (sel_yes / sel_no).
mm_pol (с 30.09) — не погодные маркеты (city = 'pol'): сделки — data-api /trades?market= (только забиравшие заявки), итог —
clob /markets (winner); пока маркет открыт — остаток без пары по середине последнего стакана, пересчёт каждый день;
возврат с комиссии не считаем (у этих тем комиссии свои, консервативно 0).
Честно о допущениях: считаем, что наша заявка первая в очереди (на 0.1¢ лучше) и что забирающий взял бы её так же;
на деле другие боты тоже улучшают цену — поэтому это верхняя оценка доли потока. Порог решения — docs/PRD.md §9.
Пишет в data/db/mm.sqlite3: mm_results (итог по маркету), mm_fills (каждое исполнение). Идемпотентно.
Запуск: docker compose run --rm collector weather_mm_settle.py
"""
import os
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import time

import requests

from jobmark import item_guard
from weather_cities import OBS_CITIES
from weather_mm_paper import MM_DB, schema

MAIN_DB = os.environ.get("POLY_LAB_DB", "/data/db/polymarket_lab.sqlite3")
L_MAX = 30.0
REBATE = 0.25 * 0.05
WALLETS = ("mm_all", "mm_sel")


def ensure(db):
    schema(db)
    db.execute("""CREATE TABLE IF NOT EXISTS mm_results (wallet TEXT, condition_id TEXT, city TEXT, local_date TEXT, bucket_lo REAL,
                  bucket_hi REAL, final_yes REAL, n_fills INTEGER, sh_yes REAL, sh_no REAL, spent REAL, merged REAL, merge_pnl REAL,
                  inv_pnl REAL, rebate REAL, pnl REAL, settled_at TEXT, PRIMARY KEY (wallet, condition_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS mm_fills (wallet TEXT, condition_id TEXT, ts INTEGER, side TEXT, price REAL, size REAL,
                  city TEXT, local_date TEXT, zone TEXT, pnl_final REAL)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_mm_fills ON mm_fills(wallet, condition_id)")
    db.commit()


def zone_of(ts, city, local_date):
    loc = datetime.fromtimestamp(ts, ZoneInfo(OBS_CITIES[city]["tz"]))
    if loc.date().isoformat() < local_date:
        return "накануне"
    for a, b in ((0, 6), (6, 9), (9, 12), (12, 15), (15, 18), (18, 24)):
        if a <= loc.hour < b:
            return f"{a}-{b}"


def settle_market(quotes, trades, fin, wallet):
    """quotes: строки mm_quotes по маркету (по ts_from); trades: (ts, y, бьёт заявки «да», размер). → итог, исполнения."""
    ys = ns = yc = nc = spent = merge_pnl = merged = reb = 0.0
    used = {}  # (id заявки, сторона) -> исполнено долей
    fills = []
    qi = 0
    for ts, y, hits_yes, size in trades:
        while qi < len(quotes) and quotes[qi]["ts_to"] <= ts:
            qi += 1
        q = next((x for x in quotes[qi:qi + 3] if x["ts_from"] <= ts < x["ts_to"]), None)
        if q is None:
            continue
        side = "yes" if hits_yes else "no"
        price = q["yes_bid"] if hits_yes else q["no_bid"]
        if price is None or (wallet == "mm_sel" and not q["sel_" + side]):
            continue
        if hits_yes and not (y <= price + 1e-9 and ys - ns < L_MAX):
            continue
        if not hits_yes and not (y >= 1 - price - 1e-9 and ns - ys < L_MAX):
            continue
        left = q["size"] - used.get((q["id"], side), 0.0)
        room = L_MAX - (ys - ns) if hits_yes else L_MAX - (ns - ys)  # сколько ещё можно взять, не превысив перекос
        k = min(size, left, room)
        if k <= 1e-9:
            continue
        used[(q["id"], side)] = used.get((q["id"], side), 0.0) + k
        if hits_yes:
            ys += k; yc += k * price
        else:
            ns += k; nc += k * price
        spent += k * price
        reb += REBATE * price * (1 - price) * k
        pay = fin if hits_yes else 1 - fin
        fills.append((ts, side, price, k, k * (pay - price)))
        pairs = min(ys, ns)
        if pairs > 0:
            ay, an = yc / ys, nc / ns
            merge_pnl += pairs * (1 - ay - an)
            merged += pairs
            ys -= pairs; ns -= pairs; yc -= pairs * ay; nc -= pairs * an
    inv = ys * fin - yc + ns * (1 - fin) - nc
    sh_y = sum(f[3] for f in fills if f[1] == "yes"); sh_n = sum(f[3] for f in fills if f[1] == "no")
    return {"n": len(fills), "sh_yes": sh_y, "sh_no": sh_n, "spent": spent, "merged": merged, "merge_pnl": merge_pnl,
            "inv_pnl": inv, "rebate": reb, "pnl": merge_pnl + inv + reb}, fills


def pol_trades(cid, since):
    """Сделки забиравших по не погодному маркету с момента первой заявки бота: (ts, y, бьёт заявки «да», размер)."""
    out, offset = [], 0
    while offset < 20000:
        try:
            got = requests.get("https://data-api.polymarket.com/trades", params={"market": cid, "limit": 500, "offset": offset}, timeout=60).json()
        except (requests.RequestException, ValueError):
            break
        if not isinstance(got, list) or not got:
            break
        for t in got:
            ts = int(t["timestamp"])
            if ts < since:
                continue
            p, idx, side = float(t["price"]), int(t.get("outcomeIndex", 0)), t["side"]
            out.append((ts, p if idx == 0 else 1 - p, (idx == 0 and side == "SELL") or (idx == 1 and side == "BUY"), float(t["size"])))
        if min(int(t["timestamp"]) for t in got) < since or len(got) < 500:
            break
        offset += 500
        time.sleep(0.3)
    return sorted(set(out))


def settle_pol(db, main_db):
    now = datetime.now(timezone.utc).isoformat()
    closed = {r[0] for r in db.execute("SELECT condition_id FROM mm_results WHERE wallet = 'mm_pol' AND final_yes IS NOT NULL")}
    n = 0
    for cid, since, ld in db.execute("SELECT condition_id, MIN(ts_from), MAX(local_date) FROM mm_quotes WHERE city = 'pol' GROUP BY condition_id").fetchall():
        if cid in closed:
            continue
        with item_guard(cid, main_db):
            m = requests.get(f"https://clob.polymarket.com/markets/{cid}", timeout=30).json()
            toks = m.get("tokens") or []
            fin_known = bool(m.get("closed")) and any(t.get("winner") for t in toks)
            if fin_known:
                fin = 1.0 if toks[0].get("winner") else 0.0
            else:
                last = db.execute("SELECT best_bid, best_ask FROM mm_quotes WHERE condition_id = ? ORDER BY ts_from DESC LIMIT 1", (cid,)).fetchone()
                fin = (last[0] + last[1]) / 2 if last and last[0] is not None and last[1] is not None else float(toks[0].get("price") or 0.5)
            quotes = [dict(r) for r in db.execute("SELECT * FROM mm_quotes WHERE condition_id = ? ORDER BY ts_from", (cid,))]
            res, fills = settle_market(quotes, pol_trades(cid, since), fin, "mm_pol")
            res["rebate"] = 0.0
            res["pnl"] = res["merge_pnl"] + res["inv_pnl"]
            with db:
                db.execute("INSERT OR REPLACE INTO mm_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           ("mm_pol", cid, "pol", ld, None, None, fin if fin_known else None, res["n"], res["sh_yes"], res["sh_no"],
                            res["spent"], res["merged"], res["merge_pnl"], res["inv_pnl"], 0.0, res["pnl"], now))
                db.execute("DELETE FROM mm_fills WHERE wallet = 'mm_pol' AND condition_id = ?", (cid,))
                db.executemany("INSERT INTO mm_fills VALUES (?,?,?,?,?,?,?,?,?,?)",
                               [("mm_pol", cid, ts, sd, pr, k, "pol", ld, None, pn) for ts, sd, pr, k, pn in fills])
            n += 1
            time.sleep(0.2)
    return n


def main():
    db = sqlite3.connect(MM_DB, timeout=30)
    ensure(db)
    db.row_factory = sqlite3.Row
    main_db = sqlite3.connect(MAIN_DB, timeout=60)
    done = {r[0] for r in db.execute("SELECT DISTINCT condition_id FROM mm_results")}
    cids = [r[0] for r in db.execute("SELECT DISTINCT condition_id FROM mm_quotes WHERE city != 'pol'")]
    todo = [c for c in cids if c not in done]
    loaded = {(r[0], r[1]) for r in main_db.execute("SELECT city, local_date FROM poly_trades_days")}
    n_ok = 0
    now = datetime.now(timezone.utc).isoformat()
    for cid in todo:
        f = main_db.execute("SELECT final_yes, city, local_date, bucket_lo, bucket_hi FROM poly_market_final WHERE condition_id = ?", (cid,)).fetchone()
        if not f or (f[1], f[2]) not in loaded or f[1] not in OBS_CITIES:
            continue  # маркет ещё не закрыт или сделки ещё не собраны — посчитаем в следующий раз
        with item_guard(cid, main_db):
            fin, city, ld, lo, hi = f
            quotes = [dict(r) for r in db.execute("SELECT * FROM mm_quotes WHERE condition_id = ? ORDER BY ts_from", (cid,))]
            seen, trades = set(), []
            for tx, outc, side, p, size, ts in main_db.execute(
                    "SELECT tx, outcome, side, price, size, ts FROM poly_trades WHERE condition_id = ? ORDER BY ts", (cid,)):
                k = (tx, outc, side, p, size, ts)
                if k in seen:
                    continue
                seen.add(k)
                y = p if outc == "Yes" else 1 - p
                trades.append((ts, y, (outc == "Yes" and side == "SELL") or (outc == "No" and side == "BUY"), size))
            with db:
                for w in WALLETS:
                    res, fills = settle_market(quotes, trades, fin, w)
                    db.execute("INSERT OR REPLACE INTO mm_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (w, cid, city, ld, lo, hi, fin, res["n"], res["sh_yes"], res["sh_no"], res["spent"], res["merged"],
                                res["merge_pnl"], res["inv_pnl"], res["rebate"], res["pnl"], now))
                    db.execute("DELETE FROM mm_fills WHERE wallet = ? AND condition_id = ?", (w, cid))
                    db.executemany("INSERT INTO mm_fills VALUES (?,?,?,?,?,?,?,?,?,?)",
                                   [(w, cid, ts, sd, pr, k, city, ld, zone_of(ts, city, ld), pn) for ts, sd, pr, k, pn in fills])
            n_ok += 1
    n_pol = settle_pol(db, main_db)
    print(f"политика: пересчитано маркетов {n_pol}", flush=True)
    for w in WALLETS + ("mm_pol",):
        r = db.execute("SELECT COUNT(*), SUM(n_fills), SUM(spent), SUM(pnl), SUM(merge_pnl), SUM(inv_pnl), SUM(rebate) FROM mm_results WHERE wallet = ?", (w,)).fetchone()
        sp = r[2] or 0
        print(f"{w}: маркетов {r[0]}, исполнений {r[1] or 0}, потрачено ${sp:,.0f}, итог ${r[3] or 0:+,.2f} "
              f"({100 * (r[3] or 0) / sp if sp else 0:+.2f}%): склейки {r[4] or 0:+.2f}, остаток {r[5] or 0:+.2f}, возврат {r[6] or 0:+.2f}", flush=True)
    print(f"посчитано новых маркетов: {n_ok}; ждут итога или сделок: {len(todo) - n_ok}")
    db.close()
    from jobmark import mark
    mark(main_db, "weather_mm_settle")
    main_db.close()


if __name__ == "__main__":
    main()
