"""
Стакан всех вариантов города в момент решения (03.11, Alex «давай»: «чем больше данных — тем легче дебажить»).

Зачем: на истории нет того стакана, по которому кошелёк купил. Снимок цены (snapshots_fast) — только лучшие цены за 1-2 мин до
покупки; прогон правила ml5_cal по нему повторил 106 из 152 настоящих ставок, по сделкам poly_trades — 57 (PRD, 03.11). С записью
любое другое правило можно прогнать на тех же днях по настоящему стакану того момента.

Что: когда кошелёк ПЕРВЫЙ раз за день пытается купить в городе (и купил, и «не купили»), после покупки (чтобы не задерживать
ставку) — один запрос Gamma (варианты города) и один POST /books CLOB (стаканы всех вариантов, ~0.1 с). Пишем стакан «да» каждого
варианта: продажа (asks, от дешёвой) и покупка (bids, от дорогой), до DEPTH уровней — стакан «нет» зеркальный (продажа «нет» = 1 −
покупка «да»; /book уже содержит заявки парного токена). Если этот же город-день записан другим кошельком меньше REUSE_S секунд
назад — не запрашиваем, а ссылаемся на ту запись.

Таблицы (в базе вызывающего: рабочая; у «Вхолостую» — data/db/fast_dry.sqlite3, рабочая у него только на чтение):
  decision_books     — город, день, время записи, вариант (condition_id, границы), время стакана на бирже, asks/bids JSON;
  decision_book_use  — кошелёк, город, день → какая запись (ts_utc) и что он делал (вариант, сторона).
Ошибка записи не мешает ставке — печатаем и идём дальше.
"""

import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone

import requests

CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
DB_PATH = os.environ.get("POLY_LAB_DB", "/data/db/polymarket_lab.sqlite3")
DEPTH = 30       # уровней с каждой стороны — на $2-5 ставки с запасом
REUSE_S = 60     # тот же город-день другим кошельком только что — ссылка на ту запись


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS main.decision_books (city TEXT, local_date TEXT, ts_utc TEXT, condition_id TEXT,
                    bucket_lo REAL, bucket_hi REAL, book_ts TEXT, asks_json TEXT, bids_json TEXT,
                    PRIMARY KEY (city, local_date, ts_utc, condition_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS main.decision_book_use (wallet TEXT, city TEXT, local_date TEXT, ts_utc TEXT,
                    bucket_lo REAL, side TEXT, PRIMARY KEY (wallet, city, local_date))""")


def _levels(xs, reverse):
    lv = sorted(((float(x["price"]), float(x["size"])) for x in xs or []), reverse=reverse)
    return json.dumps([[p, round(s, 2)] for p, s in lv[:DEPTH]])


def _markets(city, local_date):
    from weather_edge import CITIES, month_day_year_slug
    slug = f"highest-temperature-in-{CITIES[city]['poly_slug']}-on-{month_day_year_slug(date.fromisoformat(local_date))}"
    ev = requests.get(f"{GAMMA}/events", params={"slug": slug}, timeout=20).json()
    return [m for m in (ev[0]["markets"] if ev else []) if not m.get("closed")]


def record(conn, wallet, city, local_date, bucket_lo=None, side=None):
    """Стакан всех вариантов city/local_date для первой за день попытки wallet. conn=None — своё короткое соединение с DB_PATH."""
    own = conn is None
    try:
        if own:
            conn = sqlite3.connect(DB_PATH, timeout=30)
        ensure_schema(conn)   # IF NOT EXISTS — без записи, если таблицы уже есть
        if conn.execute("SELECT 1 FROM main.decision_book_use WHERE wallet = ? AND city = ? AND local_date = ?",
                        (wallet, city, local_date)).fetchone():
            return
        now = datetime.now(timezone.utc)
        r = conn.execute("SELECT MAX(ts_utc) FROM main.decision_books WHERE city = ? AND local_date = ?", (city, local_date)).fetchone()
        ts = r[0] if r and r[0] and r[0] >= (now - timedelta(seconds=REUSE_S)).isoformat() else None
        if ts is None:
            from weather_edge import parse_bucket
            ms = _markets(city, local_date)
            toks = [(m, json.loads(m["clobTokenIds"])[0]) for m in ms]
            books = requests.post(f"{CLOB}/books", json=[{"token_id": t} for _, t in toks], timeout=20).json() if toks else []
            by_tok = {b.get("asset_id"): b for b in books if isinstance(b, dict)}
            ts = now.isoformat()
            rows = []
            for m, t in toks:
                b = by_tok.get(t)
                if b is None:
                    continue
                lo, hi = parse_bucket(m["question"]) or (None, None)
                bt = b.get("timestamp")
                bt = datetime.fromtimestamp(int(bt) / 1000, timezone.utc).isoformat() if bt else None
                rows.append((city, local_date, ts, m.get("conditionId"), lo, hi, bt,
                             _levels(b.get("asks"), False), _levels(b.get("bids"), True)))
            if not rows:
                print(f"decision_books: {city} {local_date} — стаканов нет (вариантов {len(toks)})")
                return
            conn.executemany("INSERT OR IGNORE INTO main.decision_books VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        conn.execute("INSERT OR IGNORE INTO main.decision_book_use VALUES (?, ?, ?, ?, ?, ?)",
                     (wallet, city, local_date, ts, bucket_lo, side))
        conn.commit()
    except Exception as e:   # запись — для разбора; ставку не трогаем
        print(f"decision_books: {wallet} {city} {local_date} не записан — {type(e).__name__}: {e}")
        if own and conn is not None:
            conn.rollback()   # чужое соединение не откатываем — там может быть незаписанное вызывающего
    finally:
        if own and conn is not None:
            conn.close()
