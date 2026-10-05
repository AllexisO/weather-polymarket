"""
Проверка бота-мейкера по историческим стаканам Falcon API (polymarketanalytics.com, 2026-09-30; ключ PMA_TOKEN — вход
Alex, только чтение; 1 запрос = 2 кредита, 200 снимков стакана ~ раз в 1-1.5 мин). Бюджет — не больше MAX_REQ запросов.
25 погодных вариантов на 30.09, где живой бот (mm_ws) исполнялся; окно — от первого исполнения −30 мин до последнего +30 мин.
  1. точность стакана бота: лучшая заявка/предложение «да» у бота (mm_quotes, опрос 30 с) против снимка Falcon в ту же минуту;
  2. конкуренты-боты у лучшей цены: «лесенка» заявок с шагом 0.1¢ у лучшей цены, объём на лучшей цене;
  3. как долго живёт лучшая цена: доля соседних снимков, где лучшая заявка/предложение сменились.
Ответы кэшируются (data/research/falcon/), повторно кредиты не тратятся.
"""
import json
import os
import sqlite3
import statistics
from datetime import datetime, timezone
from pathlib import Path

import requests

URL = "https://narrative.agent.heisenberg.so/api/v2/semantic/retrieve/parameterized"
CACHE = Path("/data/research/falcon")
MAX_REQ, N_MARKETS = 50, 25
MM = sqlite3.connect("/data/db/mm.sqlite3", timeout=30)
used = {"req": 0}


def book(token, start_ms, end_ms):
    CACHE.mkdir(parents=True, exist_ok=True)
    out = []
    for page in range(2):
        f = CACHE / f"{token[:20]}_{start_ms}_{page}.json"
        if f.exists():
            d = json.loads(f.read_text())
        else:
            if used["req"] >= MAX_REQ:
                break
            r = requests.post(URL, headers={"Authorization": f"Bearer {os.environ['PMA_TOKEN']}"}, timeout=60,
                              json={"agent_id": 572, "params": {"token_id": token, "start_time": str(start_ms), "end_time": str(end_ms)},
                                    "pagination": {"limit": 200, "offset": 200 * page}, "formatter_config": {"format_type": "raw"}})
            used["req"] += 1
            d = r.json()
            f.write_text(json.dumps(d))
        res = (d.get("data") or {}).get("results") or []
        for x in res:
            bids = [(float(b["price"]), float(b["size"])) for b in json.loads(x["bids"])]
            asks = [(float(a["price"]), float(a["size"])) for a in json.loads(x["asks"])]
            out.append((datetime.fromisoformat(x["timestamp"].replace("Z", "+00:00")).timestamp(), bids, asks))
        if not (d.get("pagination") or {}).get("has_more"):
            break
    return sorted(out)


def main():
    rows = MM.execute("""SELECT condition_id, MIN(ts), MAX(ts), COUNT(*) FROM mm_ws_fills WHERE local_date = '2026-09-30'
                         AND wallet = 'mm_ws_all' GROUP BY condition_id ORDER BY COUNT(*) DESC LIMIT ?""", (N_MARKETS,)).fetchall()
    cids = [r[0] for r in rows]
    ms = requests.get("https://gamma-api.polymarket.com/markets", params=[("condition_ids", c) for c in cids], timeout=60).json()
    tok = {m["conditionId"]: json.loads(m["clobTokenIds"])[0] for m in ms}
    match = [0, 0]
    ladder, best_size, change_bid, change_ask, spreads = [], [], [], [], []
    for cid, t0, t1, n in rows:
        if cid not in tok:
            continue
        snaps = book(tok[cid], int((t0 - 1800) * 1000), int((t1 + 1800) * 1000))
        if len(snaps) < 3:
            continue
        prev = None
        for ts, bids, asks in snaps:
            bb = max(bids, default=None); ba = min(asks, default=None)
            if not bb or not ba:
                continue
            spreads.append(ba[0] - bb[0])
            best_size.append(bb[1] * bb[0])
            top = sorted({p for p, _ in bids if p >= bb[0] - 0.0055}, reverse=True)
            ladder.append(sum(1 for a, b in zip(top, top[1:]) if abs(a - b - 0.001) < 1e-6) >= 2)
            if prev:
                change_bid.append(abs(prev[0] - bb[0]) > 1e-9); change_ask.append(abs(prev[1] - ba[0]) > 1e-9)
            prev = (bb[0], ba[0])
            q = MM.execute("""SELECT best_bid, best_ask FROM mm_quotes WHERE condition_id = ? AND city NOT IN ('pol','own')
                              AND ts_from <= ? AND ts_to > ? ORDER BY ts_from DESC LIMIT 1""", (cid, ts, ts)).fetchone()
            if q:
                match[1] += 1
                match[0] += abs(q[0] - bb[0]) < 1e-6 and abs(q[1] - ba[0]) < 1e-6
    print(f"запросов Falcon: {used['req']} (≈ {2 * used['req']} кредитов); вариантов {len(rows)}; снимков стакана {len(spreads)}")
    if match[1]:
        print(f"1. стакан бота (опрос 30 с) совпал со снимком Falcon в ту же минуту: {100 * match[0] / match[1]:.0f}% из {match[1]}")
    if spreads:
        print(f"2. «лесенка» заявок с шагом 0.1¢ у лучшей цены (признак ботов, перебивающих друг друга): {100 * sum(ladder) / len(ladder):.0f}% снимков; "
              f"на лучшей заявке обычно ${statistics.median(best_size):.0f}; разница заявка/предложение медиана {100 * statistics.median(spreads):.1f}¢")
        print(f"3. лучшая заявка сменилась между соседними снимками (~1 мин): {100 * sum(change_bid) / max(len(change_bid), 1):.0f}%, "
              f"лучшее предложение: {100 * sum(change_ask) / max(len(change_ask), 1):.0f}%")


main()
