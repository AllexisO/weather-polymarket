"""
Прогон правила кошелька по записанным стаканам (05.11, Alex «делаем», пункт 4 улучшений): что купило бы правило, по какой цене и
сколько заработало бы — без нового кошелька и без месяца ожидания. Только чтение: рабочая база (снимки, ставки, итоги —
короткие запросы, mode=ro) и файлы стаканов data/booklog (weather_book_rec, каждую минуту с 03.11 ~19:30 UTC).

Как: город-день → самый ранний утренний снимок с шансом модели (snapshots / snapshots_fast, до 12:00 местного, как
weather_paper.place) → выбор варианта по правилу кошелька (константы берутся из weather_paper — правило одно) → покупка по стакану
той минуты (последняя запись стакана не позже момента покупки; «нет» — зеркало покупки «да») тем же расчётом, что
polyexec.simulate_buy: бюджет вместе с комиссией 0.05·p(1−p), не меньше 5 долей, потолок цены → итог по weather_poly_outcomes.
Денег кошелька не считаем (как будто хватает) — прогон про правило, а не про счёт.

Проверка самого прогона (--check, порог записан до результата: ≥ 95%): правило живого кошелька, покупка в момент его настоящей
покупки (placed_at) — должно повторить его настоящие ставки: тот же вариант и сторона, средняя цена в пределах 1¢.

Запуск: docker compose run --rm --entrypoint python collector replay_books.py --wallet ml5_cal [--from Д] [--to Д] [--lag 60] [--check]
         ... replay_books.py --wallet ml5_cal --set edge=0.05 --set pick=ask     — то же правило с правкой (новая идея)
"""

import argparse
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import weather_paper as wp
from weather_cities import OBS_CITIES

DB_PATH = os.environ.get("POLY_LAB_DB", "/data/db/polymarket_lab.sqlite3")
BOOK_DIR = Path(os.environ.get("BOOK_DIR", "/data/booklog"))
FEE_RATE = 0.05          # комиссия погоды с 01.11 (feeSchedule rate 0.05, exponent 1); стаканы есть только с 03.11
MIN_SIZE = 5.0
DECISION_S = 60          # стакан из decision_books берём, если записан не дальше минуты от момента покупки


def rule_of(wallet):
    """Правило живого кошелька из констант weather_paper (те же, что у place)."""
    no = wallet in wp.NO_WALLETS
    field = (wp.NO_WALLETS if no else {**wp.WALLETS})[wallet]
    return {"wallet": wallet, "field": field, "side": "no" if no else "yes",
            "edge": wp.EDGE_BY_WALLET.get(wallet, wp.MIN_EDGE),
            "min_price": wp.MIN_PRICE_BY_WALLET.get(wallet, wp.MIN_PRICE),
            "no_band": wp.NO_BAND.get(wallet), "nocap": wallet in wp.NO_BAND_NOCAP,
            "yes_band": wp.YES_BAND.get(wallet), "yes_floor": wp.YES_FLOOR.get(wallet), "yes_ceil": wp.YES_CEIL.get(wallet, 1.0),
            "conf_skip": wp.CONF_SKIP.get(wallet), "top_rank": wp.TOP_RANK.get(wallet),
            "pick": "ask" if wallet in wp.PICK_ASK else ("ratio" if wallet in wp.PICK_RATIO else "mid"),
            "stake": wp.STAKE, "fast": wallet in wp.FAST_WALLETS, "start": wp.START_TS.get(wallet, wp.ML_START_TS)}


def snapshot(c, city, d, field, fast):
    """Самый ранний утренний снимок с шансом модели (как place: обычный и быстрый, берём раньший)."""
    best = None
    for t in ("snapshots", "snapshots_fast") if fast else ("snapshots",):
        r = c.execute(f"SELECT MIN(ts_utc) FROM {t} WHERE city = ? AND local_date = ? AND local_hour < 12 AND {field} IS NOT NULL",
                      (city, d)).fetchone()
        if r and r[0] and (best is None or r[0] < best[1]):
            best = (t, r[0])
    if best is None:
        return None, []
    rows = [dict(r) for r in c.execute(f"SELECT * FROM {best[0]} WHERE city = ? AND local_date = ? AND ts_utc = ?", (city, d, best[1]))]
    return best[1], rows


def choose(rule, rows):
    """→ (вариант, шанс, цена снимка, потолок цены, None) или (None, ..., причина пропуска). Повторяет weather_paper.place."""
    f, no = rule["field"], rule["side"] == "no"
    bs = [dict(b) for b in rows if b.get(f) is not None]
    if no:
        bs = [dict(b, **{f: 1 - b[f], "market_p": 1 - b["market_p"],
                         "best_ask": (1 - b["best_bid"]) if b.get("best_bid") is not None else None}) for b in bs]
        if rule["no_band"] and bs:
            lo, hi = rule["no_band"]
            bs = [b for b in bs if lo <= 1 - b["market_p"] < hi and b[f] > b["market_p"]]
            if not bs:
                return None, "нет вариантов в полосе «нет»"
    if rule["top_rank"] and bs:
        bs = sorted(bs, key=lambda b: -(b["market_p"] or 0))[:rule["top_rank"]]
    if rule["yes_band"] and bs:
        lo, hi = rule["yes_band"]
        bs = [b for b in bs if lo <= b["market_p"] < hi and b[f] > b["market_p"]]
        if not bs:
            return None, "нет вариантов в полосе «да»"
    if rule["yes_floor"] and bs:
        bs = [b for b in bs if rule["yes_floor"] <= (b["market_p"] or 0) <= rule["yes_ceil"]]
        if not bs:
            return None, "нет вариантов не дешевле нижней цены"
    if rule["conf_skip"] and bs:
        tot = sum(b["market_p"] or 0 for b in bs) or 1.0
        if max((b["market_p"] or 0) / tot for b in bs) >= rule["conf_skip"]:
            return None, "рынок уверен"
    if not bs:
        return None, "нет шансов модели"
    if rule["pick"] == "ask":
        ab = [b for b in bs if b.get("best_ask") is not None and wp.YES_MIN <= b["best_ask"] <= wp.MAX_PRICE]
        if not ab:
            return None, "нет цены продажи"
        b = max(ab, key=lambda r: r[f] - r["best_ask"] - wp.ask_fee(r["best_ask"]))
        edge = b[f] - b["best_ask"] - wp.ask_fee(b["best_ask"])
    else:
        b = max(bs, key=(lambda r: r[f] / max(r["market_p"], 1e-6)) if rule["pick"] == "ratio" else (lambda r: r[f] - r["market_p"]))
        edge = b[f] - b["market_p"]
    if edge < rule["edge"]:
        return None, "нет перевеса"
    min_price = rule["min_price"] if no or rule["yes_band"] else max(rule["min_price"], wp.YES_MIN)
    if rule["pick"] != "ask" and not (min_price <= b["market_p"] <= wp.MAX_PRICE):
        return None, "цена вне полосы"
    cap = min(wp.MAX_PRICE, wp.ask_limit(b[f] - rule["edge"]) if rule["pick"] == "ask" else b[f] - rule["edge"])
    if (rule["no_band"] and not rule["nocap"]) or rule["yes_band"]:
        cap = min(cap, b["market_p"] + wp.NO_BAND_OVER)
    return {"b": b, "q": b[f], "mp": b["market_p"], "cap": cap, "min_price": min_price}, None


class Books:
    """Стакан «да» варианта на момент t из файлов data/booklog (последняя запись не позже t)."""

    def __init__(self, mode="prev", conn=None):
        # prev — последняя запись не позже t; next — первая запись после t; decision — стакан из decision_books (записан кошельком в
        # момент его покупки), если он в пределах DECISION_S от t, иначе prev. В 08:00 цены меняются за секунды — минутной записи мало
        self.dbs, self.mode, self.conn = {}, mode, conn
        self.used = {"decision": 0, "booklog": 0}

    def _db(self, t):
        m = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m")
        if m not in self.dbs:
            p = BOOK_DIR / f"books_{m}.sqlite3"
            self.dbs[m] = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=30) if p.exists() else None
        return self.dbs[m]

    def covered(self, t):
        db = self._db(t)
        return bool(db and db.execute("SELECT 1 FROM rounds WHERE ts BETWEEN ? AND ?", (int(t) - 120, int(t))).fetchone())

    def asks(self, city, d, lo, side, t):
        db = self._db(t)
        if db is None:
            return None
        q = ("SELECT b.asks, b.bids FROM tokens k JOIN books b ON b.token = k.idx WHERE k.city = ? AND k.local_date = ? "
             "AND abs(k.bucket_lo - ?) < 1e-6 AND b.ts <= ? ORDER BY b.ts DESC LIMIT 1")
        r = None
        if self.mode == "decision" and self.conn is not None:
            best = None
            for ts, a, b in self.conn.execute("SELECT ts_utc, asks_json, bids_json FROM decision_books WHERE city = ? AND local_date = ? "
                                              "AND abs(bucket_lo - ?) < 1e-6", (city, d, lo)):
                dt = abs(datetime.fromisoformat(ts).timestamp() - t)
                if dt <= DECISION_S and (best is None or dt < best[0]):
                    best = (dt, a, b)
            if best:
                r = best[1:]
                self.used["decision"] += 1
        if r is None and self.mode == "next":   # стакан первой минуты после t = последняя запись не позже этой минуты
            r = db.execute(q, (city, d, lo, (int(t) // 60 + 1) * 60)).fetchone()
        if r is None:
            r = db.execute(q, (city, d, lo, int(t))).fetchone()
            self.used["booklog"] += r is not None
        if r is None:
            return None
        if side == "yes":
            return sorted((p, s) for p, s in json.loads(r[0]))
        return sorted((round(1 - p, 6), s) for p, s in json.loads(r[1]))      # «нет» продают = 1 − покупатели «да»


def buy(asks, budget, cap):
    """Как polyexec.simulate_buy (без повторного чтения через 2 с): → shares, cost, fee, avg, reason."""
    out = {"shares": 0.0, "cost": 0.0, "fee": 0.0, "avg": None, "reason": None}
    fee = lambda p: FEE_RATE * p * (1 - p)
    if asks and asks[0][0] <= cap:
        budget = max(budget, MIN_SIZE * (asks[0][0] + fee(asks[0][0])) * 1.001)
    left = budget
    for p, s in asks:
        if p > cap:
            break
        take = min(s, left / (p + fee(p)))
        if take <= 0:
            break
        out["shares"] += take
        out["cost"] += take * p
        out["fee"] += take * fee(p)
        left -= take * (p + fee(p))
    if out["shares"]:
        out["avg"] = out["cost"] / out["shares"]
    if not asks:
        out["reason"] = "продавцов нет"
    elif out["shares"] == 0:
        out["reason"] = f"продавали от {asks[0][0] * 100:.1f}¢, потолок {cap * 100:.1f}¢"
    elif out["shares"] < MIN_SIZE:
        out.update(shares=0.0, cost=0.0, fee=0.0, avg=None, reason="меньше 5 долей по выгодной цене")
    return out


def run(rule, d0, d1, lag, check=False, book_mode="prev"):
    c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=60)
    c.row_factory = sqlite3.Row
    books = Books(book_mode, c)
    win = {(r["city"], r["local_date"]): r["win_lo"] for r in c.execute(
        "SELECT city, local_date, win_lo FROM weather_poly_outcomes WHERE local_date BETWEEN ? AND ?", (d0, d1))}
    real = {}
    if rule.get("wallet"):
        real = {(r["city"], r["local_date"]): dict(r) for r in c.execute(
            "SELECT * FROM paper_trades WHERE wallet = ? AND local_date BETWEEN ? AND ?", (rule["wallet"], d0, d1))}
    days = [r[0] for r in c.execute("SELECT DISTINCT local_date FROM snapshots_fast WHERE local_date BETWEEN ? AND ? ORDER BY 1", (d0, d1))]
    bets, no_book, match, miss = [], 0, 0, []
    for d in days:
        for city in OBS_CITIES:
            ts, rows = snapshot(c, city, d, rule["field"], rule["fast"])
            if ts is None or ts < rule["start"]:
                continue
            rr = real.get((city, d))
            t_buy = datetime.fromisoformat(ts).timestamp() + lag
            if check:   # проверка: покупка в момент настоящей покупки кошелька
                if not rr or rr["status"] not in ("open", "won", "lost", "void") or not rr["placed_at"]:
                    continue
                t_buy = datetime.fromisoformat(rr["placed_at"]).timestamp()
            if not books.covered(t_buy):
                no_book += 1
                continue
            ch, why = choose(rule, rows)
            got = None
            if ch:
                b = ch["b"]
                if book_mode == "paper" and check:   # стакан, по которому купила сама бумага (book_json «after», 5 уровней) — проверка логики
                    asks = sorted(map(tuple, (json.loads(rr["book_json"] or "{}").get("after") or []))) if abs(ch["b"]["bucket_lo"] - rr["bucket_lo"]) < 1e-6 else None
                else:
                    asks = books.asks(city, d, b["bucket_lo"], rule["side"], t_buy)
                got = buy(asks or [], rule["stake"], ch["cap"]) if asks is not None else {"shares": 0, "avg": None, "reason": "нет стакана варианта"}
                ok = got["shares"] > 0
                if ok and rule["yes_band"] and not (rule["yes_band"][0] <= got["avg"] < rule["yes_band"][1]):
                    ok, got["reason"] = False, "вне полосы по живой цене"
                if ok and rule["side"] == "yes" and not rule["yes_band"] and got["avg"] < ch["min_price"] - 1e-9:
                    ok, got["reason"] = False, "дешевле нижней цены по живой цене"
                if ok and rule["side"] == "no" and got["avg"] < ch["mp"] - wp.NO_STALE_DROP - 1e-9:
                    ok, got["reason"] = False, "«нет» подешевело — снимок устарел"
                if ok:
                    w = win.get((city, d))
                    won = None if w is None else (abs(w - b["bucket_lo"]) < 1e-6) == (rule["side"] == "yes")
                    pnl = None if won is None else (got["shares"] if won else 0.0) - got["cost"] - got["fee"]
                    bets.append({"city": city, "d": d, "lo": b["bucket_lo"], "hi": b["bucket_hi"], "unit": b["unit"], "q": ch["q"],
                                 "avg": got["avg"], "shares": got["shares"], "cost": got["cost"], "fee": got["fee"], "won": won, "pnl": pnl})
            if check:
                same = (ch and got and got["shares"] > 0 and abs(ch["b"]["bucket_lo"] - rr["bucket_lo"]) < 1e-6
                        and (rr["side"] or "yes") == rule["side"] and abs(got["avg"] - rr["price"]) <= 0.01 + 1e-9)
                match += bool(same)
                if not same:
                    miss.append(f"{city} {d}: настоящая {wp.fmt_bucket(rr['bucket_lo'], rr['bucket_hi'], rr['unit'])} по {rr['price'] * 100:.1f}¢"
                                f" ({rr['shares']:.1f} долей); прогон — "
                                + (f"{wp.fmt_bucket(ch['b']['bucket_lo'], ch['b']['bucket_hi'], ch['b']['unit'])} "
                                   + (f"по {got['avg'] * 100:.1f}¢ ({got['shares']:.1f})" if got and got['shares'] else f"не купил: {got['reason'] if got else '—'}")
                                   if ch else f"пропуск: {why}"))
    print(f"стаканы: из decision_books {books.used['decision']}, из минутной записи {books.used['booklog']}")
    return bets, no_book, match, miss


def luck_p(bets):
    """Шанс получить такой итог без перевеса (ставка выигрывает с вероятностью = цене), % — как dashboard._luck_p."""
    st = [b for b in bets if b["won"] is not None]
    var = sum(b["shares"] ** 2 * b["avg"] * (1 - b["avg"]) for b in st)
    if not var:
        return None
    z = (sum(b["pnl"] for b in st) + sum(b["fee"] for b in st)) / math.sqrt(var)
    return 100 * 0.5 * math.erfc(z / math.sqrt(2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wallet", required=True)
    ap.add_argument("--set", action="append", default=[], help="правка правила: ключ=значение (edge=0.05, pick=ask, side=no …)")
    ap.add_argument("--from", dest="d0", default="2026-11-03")
    ap.add_argument("--to", dest="d1", default=datetime.now(timezone.utc).date().isoformat())
    ap.add_argument("--lag", type=float, default=0.0, help="секунд от снимка до покупки (0 — сразу, как «Вхолостую»)")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--book", default="prev", choices=("prev", "next", "decision", "paper"), help="стакан минуты до покупки или после")
    a = ap.parse_args()
    rule = rule_of(a.wallet)
    for kv in a.set:
        k, v = kv.split("=", 1)
        try:
            rule[k] = json.loads(v)          # числа, списки, true/false/null
        except ValueError:
            rule[k] = v                      # слова: pick=ask, side=no, field=ml5_model_p
    if a.set:
        rule["wallet"] = None   # правило изменено — со ставками кошелька не сравниваем
    bets, no_book, match, miss = run(rule, a.d0, a.d1, a.lag, a.check, a.book)
    if a.check:
        n = match + len(miss)
        print(f"проверка {a.wallet} {a.d0}..{a.d1}: настоящих ставок со стаканом {n}, прогон повторил {match}"
              + (f" ({100 * match / n:.0f}%, порог ≥ 95%)" if n else "") + f"; без записи стакана {no_book}")
        for m in miss:
            print("  ≠", m)
        return
    st = [b for b in bets if b["won"] is not None]
    cost = sum(b["cost"] + b["fee"] for b in st)
    pnl = sum(b["pnl"] for b in st)
    lp = luck_p(bets)
    print(f"правило {json.dumps({k: v for k, v in rule.items() if v not in (None, False)}, ensure_ascii=False)}")
    print(f"{a.d0}..{a.d1}, покупка через {a.lag:.0f} с после снимка: ставок {len(bets)} (закрыто {len(st)}, угадано "
          f"{sum(b['won'] for b in st)}, ожидание по цене {sum(b['avg'] for b in st):.1f}), поставлено ${cost:.2f}, итог {pnl:+.2f}$"
          + (f", {100 * pnl / cost:+.1f}%" if cost else "") + (f", удача {lp:.0f}%" if lp is not None else "")
          + f"; без записи стакана город-дней {no_book}")
    for b in bets:
        print(f"  {b['d']} {b['city']:13} {wp.fmt_bucket(b['lo'], b['hi'], b['unit']):9} шанс {b['q'] * 100:3.0f}% по {b['avg'] * 100:5.1f}¢ "
              f"{b['shares']:5.1f} долей → " + ("ждёт итога" if b["won"] is None else f"{'выиграла' if b['won'] else 'проиграла'} {b['pnl']:+.2f}"))


if __name__ == "__main__":
    main()
