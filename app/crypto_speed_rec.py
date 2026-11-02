"""Запись для проверки скорости на часовых BTC (02.11, Alex «давай»; основание — data/research/crypto/speed.py: после скачка
Binance старая цена в сделках Polymarket живёт ~3 с по времени блока, т.е. меньше секунды вживую — по истории не различить).
Без ставок: только запись, отдельная база CRYPTO_DB (рабочую базу погоды не трогает, кроме отметки запуска в job_log).
Крон: каждый час в :59 — слушает следующий час целиком (маркет «Bitcoin Up or Down - <час> ET»), до конца часа + 5 с.
Пишется (время получения у нас — recv_ms, мс по часам сервера, NTP):
  • bn_trades — поток aggTrade BTCUSDT Binance (T — время сделки на Binance, мс);
  • pm_book — полные снимки стакана (событие book), pm_change — каждое изменение уровня (price_change, с лучшими ценами),
    pm_last — сделки (last_trade_price); время сервера Polymarket — srv_ms;
  • rtt — раз в 10 с время ответа GET clob.polymarket.com/time и api.binance.com/api/v3/time (наша задержка до бирж).
ПОРОГ (записан до записи): после скачка Binance ≥ 5 б.п. за 3 с старое предложение с перевесом ≥ 2¢ после комиссии
(7%·p(1−p)) ещё стоит в стакане к нашему приходу (получили сделку Binance + половина RTT до Polymarket + 0.15 с на заявку)
хотя бы в 20% скачков → отдельный виртуальный кошелёк на живом потоке. Иначе — скорость закрыта с цифрами. Разбор — заметка.
"""
import json
import os
import queue
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
import websocket

CRYPTO_DB = os.environ.get("CRYPTO_DB", "/data/research/crypto/live.sqlite3")
PM_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BN_WS = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
GAMMA = "https://gamma-api.polymarket.com"
ET = ZoneInfo("America/New_York")
TAIL_S = 5
Q = queue.Queue()
now_ms = lambda: int(time.time() * 1000)


def schema(db):
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS markets (cond TEXT PRIMARY KEY, slug TEXT, start_utc TEXT, tok_up TEXT, tok_down TEXT);
    CREATE TABLE IF NOT EXISTS bn_trades (a INTEGER PRIMARY KEY, t_ms INTEGER, recv_ms INTEGER, price REAL, qty REAL, maker INTEGER);
    CREATE TABLE IF NOT EXISTS pm_book (recv_ms INTEGER, srv_ms INTEGER, asset TEXT, bids TEXT, asks TEXT);
    CREATE TABLE IF NOT EXISTS pm_change (recv_ms INTEGER, srv_ms INTEGER, asset TEXT, price REAL, size REAL, side TEXT,
        best_bid REAL, best_ask REAL);
    CREATE TABLE IF NOT EXISTS pm_last (recv_ms INTEGER, srv_ms INTEGER, asset TEXT, price REAL, size REAL, side TEXT);
    CREATE TABLE IF NOT EXISTS rtt (recv_ms INTEGER, target TEXT, ms REAL);
    CREATE INDEX IF NOT EXISTS i_bn_recv ON bn_trades(recv_ms);
    CREATE INDEX IF NOT EXISTS i_pmc ON pm_change(asset, recv_ms);
    CREATE INDEX IF NOT EXISTS i_pmb ON pm_book(asset, recv_ms);
    """)


def market(start):
    """Маркет часа, начинающегося в start (UTC): slug по времени ET, как на Polymarket."""
    et = start.astimezone(ET)
    h = et.hour
    slug = f"bitcoin-up-or-down-{et.strftime('%B').lower()}-{et.day}-{et.year}-{(h % 12) or 12}{'am' if h < 12 else 'pm'}-et"
    for a in range(5):
        try:
            ev = requests.get(f"{GAMMA}/events", params={"slug": slug}, timeout=20).json()
            m = ev[0]["markets"][0]
            outs, toks = json.loads(m["outcomes"]), json.loads(m["clobTokenIds"])
            up = outs.index("Up")
            return m["conditionId"], slug, toks[up], toks[1 - up]
        except (requests.RequestException, ValueError, IndexError, KeyError):
            time.sleep(5 * (a + 1))
    raise RuntimeError(f"маркет не найден: {slug}")


def f(x):
    return float(x) if x not in (None, "") else None


def on_pm(raw, recv):
    if raw in ("PONG", ""):
        return
    msgs = json.loads(raw)
    for m in msgs if isinstance(msgs, list) else [msgs]:
        ev, srv = m.get("event_type"), int(m.get("timestamp") or 0)
        if ev == "book":
            Q.put(("pm_book", (recv, srv, m["asset_id"], json.dumps(m.get("bids") or m.get("buys")),
                               json.dumps(m.get("asks") or m.get("sells")))))
        elif ev == "price_change":
            for ch in m.get("price_changes") or m.get("changes") or []:
                Q.put(("pm_change", (recv, srv, ch.get("asset_id") or m.get("asset_id"), f(ch["price"]), f(ch["size"]), ch["side"],
                                     f(ch.get("best_bid")), f(ch.get("best_ask")))))
        elif ev == "last_trade_price":
            Q.put(("pm_last", (recv, srv, m["asset_id"], f(m["price"]), f(m["size"]), m.get("side"))))


def pm_loop(toks, end, stats):
    while time.time() < end:
        try:
            ws = websocket.create_connection(PM_WS, timeout=10)
            ws.send(json.dumps({"assets_ids": toks, "type": "market"}))
            last_ping = time.time()
            while time.time() < end:
                if time.time() - last_ping > 10:
                    ws.send("PING"); last_ping = time.time()
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                on_pm(raw, now_ms())
            ws.close()
        except (websocket.WebSocketException, OSError, ValueError) as e:
            stats["pm_reconnects"] += 1
            print(f"Polymarket: канал оборвался ({type(e).__name__}: {e}) — переподключаюсь", flush=True)
            time.sleep(2)


def bn_loop(end, stats):
    while time.time() < end:
        try:
            ws = websocket.create_connection(BN_WS, timeout=10)
            while time.time() < end:
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                recv = now_ms()
                m = json.loads(raw)
                if m.get("e") == "aggTrade":
                    Q.put(("bn_trades", (m["a"], m["T"], recv, float(m["p"]), float(m["q"]), int(m["m"]))))
            ws.close()
        except (websocket.WebSocketException, OSError, ValueError) as e:
            stats["bn_reconnects"] += 1
            print(f"Binance: канал оборвался ({type(e).__name__}: {e}) — переподключаюсь", flush=True)
            time.sleep(2)


def rtt_loop(end):
    s = requests.Session()
    while time.time() < end:
        for name, url in (("polymarket", "https://clob.polymarket.com/time"), ("binance", "https://api.binance.com/api/v3/time")):
            t = time.perf_counter()
            try:
                s.get(url, timeout=5)
                Q.put(("rtt", (now_ms(), name, 1000 * (time.perf_counter() - t))))
            except requests.RequestException:
                pass
        time.sleep(10)


SQL = {"bn_trades": "INSERT OR IGNORE INTO bn_trades VALUES (?,?,?,?,?,?)", "pm_book": "INSERT INTO pm_book VALUES (?,?,?,?,?)",
       "pm_change": "INSERT INTO pm_change VALUES (?,?,?,?,?,?,?,?)", "pm_last": "INSERT INTO pm_last VALUES (?,?,?,?,?,?)",
       "rtt": "INSERT INTO rtt VALUES (?,?,?)"}


def flush(db, counts):
    batch = {}
    while True:
        try:
            k, row = Q.get_nowait()
        except queue.Empty:
            break
        batch.setdefault(k, []).append(row)
    if not batch:
        return
    try:
        with db:
            for k, rows in batch.items():
                db.executemany(SQL[k], rows)
                counts[k] = counts.get(k, 0) + len(rows)
    except sqlite3.OperationalError as e:   # база занята (пересечение с прошлым запуском) — вернуть в очередь
        print(f"запись отложена ({e})", flush=True)
        for k, rows in batch.items():
            for r in rows:
                Q.put((k, r))


def main():
    now = datetime.now(timezone.utc)
    start = (now + timedelta(minutes=5)).replace(minute=0, second=0, microsecond=0)   # :59 → следующий час
    cond, slug, up, down = market(start)
    end = (start + timedelta(hours=1)).timestamp() + TAIL_S
    if os.environ.get("REC_TEST_S"):          # проверка: текущий час, N секунд
        start = now.replace(minute=0, second=0, microsecond=0)
        cond, slug, up, down = market(start)
        end = time.time() + int(os.environ["REC_TEST_S"])
    db = sqlite3.connect(CRYPTO_DB, timeout=30)
    schema(db)
    with db:
        db.execute("INSERT OR REPLACE INTO markets VALUES (?,?,?,?,?)", (cond, slug, start.isoformat(), up, down))
    print(f"{slug}: запись до {datetime.fromtimestamp(end, timezone.utc):%H:%M:%S} UTC", flush=True)
    stats = {"pm_reconnects": 0, "bn_reconnects": 0}
    th = [threading.Thread(target=pm_loop, args=([up, down], end, stats), daemon=True),
          threading.Thread(target=bn_loop, args=(end, stats), daemon=True),
          threading.Thread(target=rtt_loop, args=(end,), daemon=True)]
    for t in th:
        t.start()
    from jobmark import mark_alive
    alive, counts = {}, {}
    while time.time() < end or any(t.is_alive() for t in th[:2]):
        time.sleep(1)
        flush(db, counts)
        mark_alive("crypto_speed_rec", alive)
        if time.time() > end + 30:
            break
    flush(db, counts)
    db.close()
    print("записано: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) +
          f"; переподключений Polymarket {stats['pm_reconnects']}, Binance {stats['bn_reconnects']}", flush=True)
    if counts.get("bn_trades", 0) == 0 or counts.get("pm_change", 0) + counts.get("pm_book", 0) == 0:
        raise SystemExit("поток пустой — записи нет")


if __name__ == "__main__":
    main()
