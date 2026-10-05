"""
Бот-мейкер на живом потоке Polymarket (2026-09-30, Alex: «сделай, как считаешь нужным, главное — результат»).
Та же схема и те же правила, что weather_mm_paper.py (заявки на «да» и «нет» на 0.1¢ лучше лучшей цены, 10 долей, пауза
−1…+3 мин вокруг плановой сводки METAR и после 18:00 местного в день маркета, выгодные зоны SEL), но:
  • заявка переставляется сразу при каждом изменении стакана (канал wss://ws-subscriptions-clob.polymarket.com/ws/market,
    задержка ~0.03 с) — а не раз в 30 с: бот не «стоит на старой цене», когда её уже сносят;
  • исполнение — сразу по живой сделке (событие last_trade_price) против заявки, которая стояла В ЭТОТ МОМЕНТ,
    по цене нашей заявки; не больше 10 долей на заявку (новая цена — новая заявка), перекос ≤ 30 долей на маркет.
Сделка по «да»: продажа «да» по p ≤ нашей заявке на «да» → купили «да»; покупка «да» по p ≥ 1 − заявка на «нет» → купили «нет».
Сделка по «нет» — зеркально (подписка на оба токена).
Кошельки mm_ws_all / mm_ws_sel / mm_ws_zone (с 30.09: только дешёвая сторона, см. in_zone) — сравниваются с mm_all / mm_sel (опрос раз в 30 с). Исполнения — data/db/mm.sqlite3
(mm_ws_fills), итог по закрытым маркетам — weather_mm_settle.py. Допущение то же: наша заявка первая в очереди.
Крон: каждый час, работает LISTEN_MIN минут (как weather_mm_paper.py).
"""
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import websocket

from weather_cities import OBS_CITIES
from weather_mm_paper import (EVENING_H, METAR_PAUSE, MM_DB, PMAX, PMIN, SIZE, TICK, is_sel, load_markets, metar_minutes,
                              time_zone)

WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
LISTEN_MIN = float(os.environ.get("LISTEN_MIN", "57"))
L_MAX = 30.0
WALLETS = ("mm_ws_all", "mm_ws_sel", "mm_ws_zone", "mm_ws_z30", "mm_ws_zs", "mm100")
# 02.10 (Alex: «мне нужны гарантии, что боты работают так, как будут работать на Polymarket»): mm_ws_zs — правила mm_ws_zone,
# но исполнение только когда оно ГАРАНТИРОВАНО вживую: (1) наша заявка простояла без изменений ≥ STRICT_AGE с — успела дойти
# до биржи, и отмена/перестановка не в пути; (2) сделка прошла СТРОГО хуже нашей цены — значит, весь наш уровень съеден,
# какая бы очередь перед нами ни стояла и кто бы ни перебил цену. Это нижняя оценка; правда — между mm_ws_zone и mm_ws_zs.
STRICT_AGE = 1.0
# 02.10 (Alex: «хочу положить $100 и чтобы работало ровно как в реальности»): mm100 — правила mm_ws_zone, но
#  • банк ровно $100; заявка ставится, только если на неё хватает свободных денег (Polymarket замораживает цена × доли);
#  • заявки по 5 долей (минимум Polymarket);
#  • исполнение только гарантированное (как mm_ws_zs: заявка стоит ≥ 1 с, сделка прошла хуже нашей цены);
#  • нарочно хуже жизни: отменённая заявка ещё STRICT_AGE с стоит на бирже и её могут «подобрать»;
#  • без возврата части комиссии мейкеру (в жизни будет чуть лучше).
BANK = 100.0
SIZE100 = 5.0
# 05.10 (Alex: свои стаканы вместо платного Falcon): полный стакан «да» по каждому варианту ведём из событий book / price_change,
# раз в BOOK_EVERY с пишем 5 лучших уровней заявок и предложений тех вариантов, где стакан изменился, — тот же вид, что снимки
# Falcon (data/research/falcon/book), для проверок бота на истории. Отдельная база BOOKS_DB (не в data/db — большая и
# восстанавливается только из себя; в ночную копию не входит). Ошибка записи стакана бота не останавливает.
BOOKS_DB = os.environ.get("BOOKS_DB", "/data/books/books.sqlite3")
BOOK_EVERY = 60
BOOK_TOP = 5
# 2026-09-30: правило из проверки на настоящих стаканах Falcon (weather_study_mm_book.py, зоны выбраны на 19.08-07.09,
# проверка 08.09-27.09: +6.9%, каждую неделю в плюсе; все заявки подряд — около 0%): покупать только ДЕШЁВУЮ сторону
# варианта — ниже 50¢ (с 15:00 до 18:00 дня маркета — ниже 30¢); дорогую сторону не покупать.


def in_zone(price, zone):
    return price is not None and price < (0.30 if zone == "15-18" else 0.50)


# 2026-09-30 (настройка на тех же стаканах, weather_study_mm_book2.py): граница 30¢ весь день — лучшая на 19.08-07.09 (+17.9%)
# и на проверке 08.09-27.09 +18.4% (база 50¢ +6.9%); по неделям +25/+16/+13%; в худшем случае очереди +10.8%.
def in_z30(price):
    return price is not None and price < 0.30


def schema(db):
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS mm_ws_fills (wallet TEXT, condition_id TEXT, ts REAL, side TEXT, price REAL, size REAL,
                  city TEXT, local_date TEXT, bucket_lo REAL, bucket_hi REAL, zone TEXT, sel INTEGER)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_mm_ws_fills ON mm_ws_fills(condition_id)")
    db.execute("""CREATE TABLE IF NOT EXISTS mm_ws_stats (hour TEXT PRIMARY KEY, events INTEGER, trades INTEGER, fills INTEGER,
                  quotes INTEGER, reconnects INTEGER)""")
    db.commit()


def bank100(db):
    """Свободные деньги mm100: $100 + итог закрытых маркетов + по открытым (склейки − потрачено); остаток без пары в открытых
    маркетах заморожен до их итога. Обрезка по L_MAX — как в weather_mm_settle.settle_ws."""
    settled = {r[0]: r[1] for r in db.execute("SELECT condition_id, pnl FROM mm_results WHERE wallet = 'mm100'")} \
        if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'mm_results'").fetchone() else {}
    cash = BANK + sum(settled.values())
    cur, oy, on = None, 0.0, 0.0
    for cid, side, pr, k in db.execute("SELECT condition_id, side, price, size FROM mm_ws_fills WHERE wallet = 'mm100' ORDER BY condition_id, ts, rowid"):
        if cid != cur:
            cur, oy, on = cid, 0.0, 0.0
        if cid in settled:
            continue
        k = max(0.0, min(k, L_MAX - (oy - on) if side == "yes" else L_MAX - (on - oy)))
        if k <= 1e-9:
            continue
        cash -= k * pr
        oy, on = (oy + k, on) if side == "yes" else (oy, on + k)
        m = min(oy, on)
        cash += m
        oy, on = oy - m, on - m
    return cash


def save100(db, bot):
    with db:
        db.execute("CREATE TABLE IF NOT EXISTS mm100_state (ts REAL PRIMARY KEY, cash REAL, reserved REAL, quotes INTEGER)")
        db.execute("INSERT OR REPLACE INTO mm100_state VALUES (?,?,?,?)", (time.time(), bot.cash100, sum(bot.res100.values()), len(bot.res100)))


class Bot:
    def __init__(self, markets, mins):
        self.m = {m["cid"]: m for m in markets}
        self.by_token = {}
        for m in markets:
            self.by_token[m["token"]] = (m["cid"], "yes")
            self.by_token[m["token_no"]] = (m["cid"], "no")
        self.book = {}        # cid -> (лучшая заявка «да», лучшее предложение «да»)
        self.quote = {}       # cid -> {"yes": цена, "no": цена, "id": номер заявки, "sel_yes", "sel_no", "ts": с какого момента}
        self.hist = {}        # cid -> последние заявки (сверка сделки с той, что стояла в момент сделки по часам биржи)
        self.used = {}        # (cid, id заявки, сторона) -> исполнено долей
        self.inv = {w: {} for w in WALLETS}   # кошелёк -> cid -> [долей «да», долей «нет»]
        self.mins = mins
        self.qid = 0
        self.stats = {"events": 0, "trades": 0, "fills": 0, "quotes": 0}
        self.fills = []
        self.cash100 = BANK   # свободные деньги mm100 (без замороженных под заявки); пересчитывается при старте из базы
        self.res100 = {}      # cid -> замороженно под текущую заявку mm100
        self.l2 = {}          # cid -> ({цена: доли} заявок «да», {цена: доли} предложений «да») — для записи стаканов
        self.l2_dirty = set()

    def requote(self, cid, now, ev_ts=None):
        m = self.m[cid]
        bb, ba = self.book.get(cid, (None, None))
        q = None
        if bb is not None and ba is not None and ba - bb >= 2 * TICK - 1e-9:
            cfg = OBS_CITIES[m["city"]]
            loc = now.astimezone(ZoneInfo(cfg["tz"]))
            ld = m["local_date"]
            mn = now.minute
            pause = (loc.date().isoformat() > ld or (loc.date().isoformat() == ld and loc.hour >= EVENING_H)
                     or any((mn - x) % 60 <= METAR_PAUSE[1] or (x - mn) % 60 <= METAR_PAUSE[0] for x in self.mins.get(m["city"], [])))
            if not pause:
                yb, nb = round(bb + TICK, 3), round(1 - ba + TICK, 3)
                if yb >= ba - 1e-9 or (1 - nb) <= yb + 1e-9:
                    yb, nb = round(bb, 3), round(1 - ba, 3)
                zone = time_zone(loc, ld)
                yes = yb if PMIN <= yb <= PMAX else None
                no = nb if PMIN <= nb <= PMAX else None
                if yes is not None or no is not None:
                    q = {"yes": yes, "no": no, "sel_yes": int(yes is not None and is_sel(yes, zone)),
                         "sel_no": int(no is not None and is_sel(no, zone)), "zone": zone}
        old = self.quote.get(cid)
        ts = ev_ts if ev_ts is not None else now.timestamp()
        if q is None:
            self.res100.pop(cid, None)
            if old is not None:
                self.quote.pop(cid, None)
                self.hist.setdefault(cid, []).append({"ts": ts, "off": True})
            return
        if old and old["yes"] == q["yes"] and old["no"] == q["no"]:
            return
        self.qid += 1
        q["id"], q["ts"] = self.qid, ts
        # mm100: заморозить деньги под новую заявку (только дешёвая сторона, по 5 долей); не хватает — заявки mm100 нет
        self.res100.pop(cid, None)
        need = sum(q[sd] * SIZE100 for sd in ("yes", "no") if q[sd] is not None and in_zone(q[sd], q["zone"]))
        q["m100"] = need > 0 and self.cash100 - sum(self.res100.values()) >= need
        if q["m100"]:
            self.res100[cid] = need
        self.quote[cid] = q
        h = self.hist.setdefault(cid, [])
        h.append(q)
        del h[:-6]
        self.stats["quotes"] += 1

    def on_trade_100(self, cid, hits_yes, y, ts):
        """mm100: какие наши заявки в момент сделки ТОЧНО стоят на бирже — текущая, если ей ≥ STRICT_AGE с, и предыдущая,
        если её заменили меньше STRICT_AGE с назад (отмена ещё в пути — её могут «подобрать»). Исполнение — только если
        сделка прошла хуже нашей цены (весь наш уровень съеден)."""
        live = [x for x in self.hist.get(cid, []) if x["ts"] < ts]
        if not live:
            return
        cur, cands = live[-1], []
        if not cur.get("off") and ts - cur["ts"] >= STRICT_AGE:
            cands.append(cur)
        if len(live) >= 2 and ts - cur["ts"] < STRICT_AGE and not live[-2].get("off"):
            cands.append(live[-2])
        side = "yes" if hits_yes else "no"
        for q in cands:
            price = q.get(side)
            if price is None or not q.get("m100") or not in_zone(price, q["zone"]):
                continue
            if not ((y < price - 1e-9) if hits_yes else (y > 1 - price + 1e-9)):
                continue
            ys, ns = self.inv["mm100"].setdefault(cid, [0.0, 0.0])
            room = L_MAX - (ys - ns) if hits_yes else L_MAX - (ns - ys)
            k = min(SIZE100 - self.used.get(("mm100", q["id"], side), 0.0), room, self.cash100 / price)
            if k <= 1e-9:
                continue
            m = self.m[cid]
            self.used[("mm100", q["id"], side)] = self.used.get(("mm100", q["id"], side), 0.0) + k
            self.cash100 -= k * price
            if cid in self.res100:
                self.res100[cid] = max(0.0, self.res100[cid] - k * price)
            self.inv["mm100"][cid][0 if hits_yes else 1] += k
            pair = min(self.inv["mm100"][cid])
            if pair > 0:
                self.cash100 += pair   # склейка «да»+«нет» → $1 за пару сразу
                self.inv["mm100"][cid] = [self.inv["mm100"][cid][0] - pair, self.inv["mm100"][cid][1] - pair]
            self.fills.append(("mm100", cid, ts, side, price, k, m["city"], m["local_date"], m["lo"], m["hi"], q["zone"], q["sel_" + side]))
            self.stats["fills"] += 1
            return

    def on_trade(self, cid, side_token, taker_side, p, size, ts):
        """Живая сделка против заявки, стоявшей в момент сделки (по часам биржи; строго раньше сделки)."""
        self.on_trade_100(cid, (side_token == "yes" and taker_side == "SELL") or (side_token == "no" and taker_side == "BUY"),
                          p if side_token == "yes" else 1 - p, ts)
        q = next((x for x in reversed(self.hist.get(cid, [])) if x["ts"] < ts), None)
        if q is None or q.get("off"):
            return
        y = p if side_token == "yes" else 1 - p                  # цена в «да»
        last = self.hist.get(cid, [])[-1] if self.hist.get(cid) else None
        hits_yes = (side_token == "yes" and taker_side == "SELL") or (side_token == "no" and taker_side == "BUY")
        side = "yes" if hits_yes else "no"
        price = q[side]
        if price is None:
            return
        if (hits_yes and y > price + 1e-9) or (not hits_yes and y < 1 - price - 1e-9):
            return
        m = self.m[cid]
        for w in WALLETS:
            if w == "mm100":
                continue   # своя логика — on_trade_100
            if w == "mm_ws_sel" and not q["sel_" + side]:
                continue
            if w == "mm_ws_zone" and not in_zone(price, q["zone"]):
                continue
            if w == "mm_ws_z30" and not in_z30(price):
                continue
            if w == "mm_ws_zs":
                through = (y < price - 1e-9) if hits_yes else (y > 1 - price + 1e-9)
                if not in_zone(price, q["zone"]) or last is not q or ts - q["ts"] < STRICT_AGE or not through:
                    continue
            ys, ns = self.inv[w].setdefault(cid, [0.0, 0.0])
            room = L_MAX - (ys - ns) if hits_yes else L_MAX - (ns - ys)
            left = SIZE - self.used.get((w, q["id"], side), 0.0)
            k = min(left, room) if w == "mm_ws_zs" else min(size, left, room)   # строгий: уровень съеден целиком
            if k <= 1e-9:
                continue
            self.used[(w, q["id"], side)] = self.used.get((w, q["id"], side), 0.0) + k
            self.inv[w][cid][0 if hits_yes else 1] += k
            pair = min(self.inv[w][cid])
            self.inv[w][cid] = [self.inv[w][cid][0] - pair, self.inv[w][cid][1] - pair]
            self.fills.append((w, cid, ts, side, price, k, m["city"], m["local_date"], m["lo"], m["hi"], q["zone"], q["sel_" + side]))
            self.stats["fills"] += 1

    def handle(self, raw, now):
        try:
            data = json.loads(raw)
        except ValueError:
            return
        for d in data if isinstance(data, list) else [data]:
            et = d.get("event_type")
            self.stats["events"] += 1
            if et == "book":
                tok = self.by_token.get(d.get("asset_id"))
                if tok and tok[1] == "yes":
                    bids = [float(x["price"]) for x in d.get("bids", []) if float(x["size"]) > 0]
                    asks = [float(x["price"]) for x in d.get("asks", []) if float(x["size"]) > 0]
                    self.book[tok[0]] = (max(bids) if bids else None, min(asks) if asks else None)
                    self.l2[tok[0]] = ({float(x["price"]): float(x["size"]) for x in d.get("bids", []) if float(x["size"]) > 0},
                                       {float(x["price"]): float(x["size"]) for x in d.get("asks", []) if float(x["size"]) > 0})
                    self.l2_dirty.add(tok[0])
                    self.requote(tok[0], now, int(d.get("timestamp") or 0) / 1000 or None)
            elif et == "price_change":
                for c in d.get("price_changes", []):
                    tok = self.by_token.get(c.get("asset_id"))
                    if not tok or tok[1] != "yes":
                        continue
                    lv = self.l2.get(tok[0])
                    if lv is not None and c.get("price") is not None and c.get("side") in ("BUY", "SELL"):
                        side_l = lv[0] if c["side"] == "BUY" else lv[1]
                        px, sz = float(c["price"]), float(c.get("size") or 0)
                        if sz > 0:
                            side_l[px] = sz
                        else:
                            side_l.pop(px, None)
                        self.l2_dirty.add(tok[0])
                    bb, ba = c.get("best_bid"), c.get("best_ask")
                    self.book[tok[0]] = (float(bb) if bb not in (None, "", "0") else None, float(ba) if ba not in (None, "", "0") else None)
                    self.requote(tok[0], now, int(d.get("timestamp") or 0) / 1000 or None)
            elif et == "last_trade_price":
                tok = self.by_token.get(d.get("asset_id"))
                if tok:
                    self.stats["trades"] += 1
                    self.on_trade(tok[0], tok[1], d.get("side"), float(d["price"]), float(d["size"]), int(d["timestamp"]) / 1000)


def books_db():
    os.makedirs(os.path.dirname(BOOKS_DB), exist_ok=True)
    b = sqlite3.connect(BOOKS_DB, timeout=30)
    b.execute("PRAGMA journal_mode=WAL")
    b.execute("""CREATE TABLE IF NOT EXISTS book_snaps (condition_id TEXT, ts INTEGER, token TEXT, bids TEXT, asks TEXT,
                 PRIMARY KEY (condition_id, ts)) WITHOUT ROWID""")
    return b


def save_books(bdb, bot):
    """Снимок 5 лучших уровней по вариантам, где стакан изменился с прошлого снимка: [[цена, доли], ...] как у Falcon."""
    ts, rows = int(time.time()), []
    for cid in bot.l2_dirty:
        bids, asks = bot.l2.get(cid, ({}, {}))
        rows.append((cid, ts, bot.m[cid]["token"], json.dumps(sorted(bids.items(), reverse=True)[:BOOK_TOP]),
                     json.dumps(sorted(asks.items())[:BOOK_TOP])))
    with bdb:
        bdb.executemany("INSERT OR REPLACE INTO book_snaps VALUES (?,?,?,?,?)", rows)
    bot.l2_dirty.clear()
    return len(rows)


def main():
    from jobmark import single_instance
    single_instance("mm_ws")
    MM_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(MM_DB, timeout=120)
    schema(db)
    markets = load_markets()
    bot = Bot(markets, metar_minutes())
    # 02.10: перекос — на маркет за всё время, а не на запуск. Раньше inv начинался с нуля каждый час (перезапуск по крону),
    # и лимит L_MAX работал «на час»: Лондон 01.10 — 382 «да» против 75 «нет» (+$348 удачей, не мейкерством).
    cids = list(bot.m)
    for i in range(0, len(cids), 500):
        part = cids[i:i + 500]
        for w, cid, y, n in db.execute(f"""SELECT wallet, condition_id, SUM(CASE WHEN side = 'yes' THEN size ELSE 0 END),
                                           SUM(CASE WHEN side = 'no' THEN size ELSE 0 END) FROM mm_ws_fills
                                           WHERE condition_id IN ({",".join("?" * len(part))}) GROUP BY 1, 2""", part):
            if w in bot.inv:
                pair = min(y, n)
                bot.inv[w][cid] = [y - pair, n - pair]
    print(f"перекос из прошлых запусков: {sum(len(v) for v in bot.inv.values())} пар кошелёк-маркет", flush=True)
    bot.cash100 = bank100(db)
    print(f"mm100: свободно ${bot.cash100:.2f} из ${BANK:.0f}", flush=True)
    toks = [t for m in markets for t in (m["token"], m["token_no"])]
    end = time.time() + LISTEN_MIN * 60
    reconnects, last_flush, last_sweep, last_book = 0, time.time(), time.time(), time.time()
    try:
        bdb = books_db()
    except sqlite3.Error as e:
        bdb = None
        print(f"база стаканов недоступна ({e}) — стаканы в этот запуск не пишу", flush=True)
    n_book = 0
    from jobmark import mark_alive
    alive = {}
    print(f"вариантов {len(markets)}, токенов {len(toks)}", flush=True)
    while time.time() < end:
        try:
            ws = websocket.create_connection(WS, timeout=30)
            ws.send(json.dumps({"assets_ids": toks, "type": "market"}))
            last_ping = time.time()
            while time.time() < end:
                if time.time() - last_ping > 10:
                    ws.send("PING")
                    last_ping = time.time()
                mark_alive("weather_mm_ws", alive)
                raw = ws.recv()
                if raw and raw != "PONG":
                    bot.handle(raw, datetime.now(timezone.utc))
                if time.time() - last_flush > 30 and bot.fills:
                    try:
                        with db:
                            db.executemany("INSERT INTO mm_ws_fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", bot.fills)
                        bot.fills = []
                        save100(db, bot)
                    except sqlite3.OperationalError as e:  # 01.10: база занята — исполнения остаются в памяти, запишутся позже
                        print(f"база ботов занята ({e}) — запишу позже", flush=True)
                    last_flush = time.time()
                elif time.time() - last_flush > 30:
                    try:
                        save100(db, bot)
                    except sqlite3.OperationalError:
                        pass
                    last_flush = time.time()
                if bdb is not None and time.time() - last_book > BOOK_EVERY:
                    try:
                        n_book += save_books(bdb, bot)
                    except sqlite3.Error as e:  # база стаканов занята — изменения останутся отмеченными, запишутся в следующий раз
                        print(f"стаканы не записались ({e})", flush=True)
                    last_book = time.time()
                # паузы вокруг сводок наступают по времени, а не по событию — раз в 20 с пересчитываем все заявки
                if time.time() - last_sweep > 20:
                    now = datetime.now(timezone.utc)
                    for cid in list(bot.book):
                        bot.requote(cid, now)
                    last_sweep = time.time()
            ws.close()
        except (websocket.WebSocketException, OSError) as e:
            reconnects += 1
            print(f"канал оборвался ({type(e).__name__}: {e}) — переподключаюсь", flush=True)
            time.sleep(3)
    if bot.fills:
        with db:
            db.executemany("INSERT INTO mm_ws_fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", bot.fills)
    hour = datetime.now(timezone.utc).strftime("%Y-%m-%d %H")
    with db:
        db.execute("INSERT OR REPLACE INTO mm_ws_stats VALUES (?,?,?,?,?,?)",
                   (hour, bot.stats["events"], bot.stats["trades"], bot.stats["fills"], bot.stats["quotes"], reconnects))
    db.close()
    print(f"событий {bot.stats['events']}, сделок {bot.stats['trades']}, наших исполнений {bot.stats['fills']}, "
          f"новых заявок {bot.stats['quotes']}, переподключений {reconnects}, снимков стаканов {n_book}", flush=True)
    if bdb is not None:
        bdb.close()
    from jobmark import mark
    c = sqlite3.connect(os.environ.get("POLY_LAB_DB", "/data/db/polymarket_lab.sqlite3"), timeout=60)
    mark(c, "weather_mm_ws")
    c.close()


if __name__ == "__main__":
    main()
