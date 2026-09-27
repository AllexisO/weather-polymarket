"""
Слушатель сделок Polymarket в реальном времени — повтор за сильными трейдерами за
секунды (2026-09-26, решение Alex). Проверка на истории: чем раньше повторяем, тем
лучше — через 0.5 мин +1.0%, 1 мин +0.1%, 2.5 мин −1.1%, 5 мин −2.3%.

Канал wss://ws-live-data.polymarket.com (тема activity/trades) присылает КАЖДУЮ
сделку Polymarket за 0-1 с, с кошельком трейдера. Берём только «Highest temperature»
и только трейдеров из sharp_wallets (обновляем список раз в 10 минут); решение и
покупка — общая функция weather_copy.try_copy (те же правила, что у опроса).
Покупка — в отдельном потоке, чтобы не задерживать чтение канала.

Работает постоянно в своём контейнере (docker-compose, сервис copier), сам
переподключается. Раз в минуту отмечается в job_runs (weather_copy_live) —
если молчит, weather_alerts.py покажет предупреждение.
"""

import json
import queue
import sqlite3
import threading
import time

import websocket

import weather_copy as wc
from jobmark import mark

URL = "wss://ws-live-data.polymarket.com"
SUB = {"action": "subscribe", "subscriptions": [{"topic": "activity", "type": "trades"}]}

jobs = queue.Queue()
state = {"sharps": set(), "loaded": 0.0, "seen": 0, "weather": 0, "matched": 0, "last_msg": time.time(), "ws": None}
SILENCE_S = 60  # 2026-09-26: канал однажды замолчал на час без обрыва — после 60 с тишины переподключаемся


def load_sharps():
    conn = sqlite3.connect(wc.DB_PATH, timeout=60)
    try:
        state["sharps"] = {r[0].lower() for r in conn.execute("SELECT wallet FROM sharp_wallets").fetchall()}
    except sqlite3.Error as e:
        print(f"рейтинг не прочитан: {e}", flush=True)
    finally:
        conn.close()
    state["loaded"] = time.time()


def db():
    """2026-09-27: соединение на одну операцию — открыли, сделали, закрыли. Долгоживущее
    соединение с незавершённой записью держало базу 7 часов (сайт и крон стояли)."""
    conn = sqlite3.connect(wc.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def worker():
    """Покупки — по одной; на каждую — своё короткое соединение с базой."""
    last_mark = 0.0
    while True:
        try:
            item = jobs.get(timeout=30)
        except queue.Empty:
            item = None
        if item is not None and not wc.trading_stopped():
            w, t = item
            conn = db()
            try:
                wc.try_copy(conn, w, t, time.time(), source="слушатель")
            except Exception as e:  # одна плохая сделка не должна ронять слушателя
                print(f"ошибка повтора: {e}", flush=True)
                conn.rollback()  # незавершённая запись не должна держать базу
            finally:
                conn.close()
        if time.time() - last_mark > 60:
            conn = db()
            try:
                mark(conn, "weather_copy_live")
            except sqlite3.Error as e:
                print(f"не отметился в job_runs: {e}", flush=True)
                conn.rollback()
            finally:
                conn.close()
            last_mark = time.time()
            if time.time() - state["loaded"] > 600:
                load_sharps()


def on_message(ws, msg):
    try:
        p = (json.loads(msg) or {}).get("payload") or {}
    except ValueError:
        return
    state["seen"] += 1
    state["last_msg"] = time.time()
    slug = p.get("eventSlug") or ""
    if not slug.startswith("highest-temperature-in-"):
        return
    state["weather"] += 1
    w = (p.get("proxyWallet") or "").lower()
    if w in state["sharps"] and p.get("side") == "BUY":
        state["matched"] += 1
        jobs.put((w, p))


def on_open(ws):
    ws.send(json.dumps(SUB))
    print(f"подключено; сильных трейдеров в списке: {len(state['sharps'])}", flush=True)


def on_error(ws, err):
    print(f"ошибка канала: {err}", flush=True)


def main():
    load_sharps()
    threading.Thread(target=worker, daemon=True).start()

    def stats():
        while True:
            time.sleep(3600)
            print(f"за час: сделок {state['seen']}, погода {state['weather']}, сильных покупок {state['matched']}", flush=True)
            state["seen"] = state["weather"] = state["matched"] = 0
    threading.Thread(target=stats, daemon=True).start()
    def watchdog():
        while True:
            time.sleep(10)
            ws = state["ws"]
            if ws is not None and time.time() - state["last_msg"] > SILENCE_S:
                print(f"тишина {time.time() - state['last_msg']:.0f} с — переподключаюсь", flush=True)
                state["last_msg"] = time.time()
                try:
                    ws.close()
                except Exception:
                    pass
    threading.Thread(target=watchdog, daemon=True).start()
    while True:  # переподключение при обрыве
        ws = websocket.WebSocketApp(URL, on_open=on_open, on_message=on_message, on_error=on_error)
        state["ws"] = ws
        state["last_msg"] = time.time()
        ws.run_forever(ping_interval=10, ping_timeout=5)
        print("соединение закрыто — переподключаюсь через 5 с", flush=True)
        time.sleep(5)


if __name__ == "__main__":
    main()
