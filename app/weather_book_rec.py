"""
Стаканы всех погодных вариантов каждую минуту (03.11, Alex «давай»: «каждую минуту, все 30 уровней, все стаканы — чем больше
данных, тем легче»). Без ставок: только запись. Дополняет decision_books (стакан в момент решения кошелька).

Что: все открытые варианты 47 городов (дни маркета от вчера до +3 по UTC, ~1450 стаканов «да»); раз в минуту (в :00 с) —
POST /books CLOB пачками по 50; на каждый вариант — до 30 уровней продажи (asks, от дешёвой) и покупки (bids, от дорогой),
время стакана на бирже. Стакан «нет» зеркальный (продажа «нет» = 1 − покупка «да»; /book уже содержит заявки парного токена).
Без повторов: строка пишется, только если стакан изменился с прошлой минуты (за минуту меняется ~55%) — информация та же, что у
записи всех подряд: стакан на любую минуту = последняя строка варианта не позже этой минуты. Чтобы отличить «не изменился» от
«не получили», каждая минута — строка в rounds, а варианты, чей стакан в эту минуту не получен, — в missing.
Список вариантов — Gamma, раз в 10 мин (новые дни, закрытые убираем); вариант без стакана 3 минуты подряд — маркет закрылся,
убираем сразу (иначе до обновления списка ~100 «не получено» в минуту).

Где: data/booklog/books_ГГГГ-ММ.sqlite3 (месяц по UTC; в начале каждого файла — полный стакан всех вариантов, файл читается сам
по себе). Не рабочая база (иначе гигабайты в ней и «database is locked» у крона) и не ночная копия (решение Alex 03.11: диск
копий personal_data с ошибками записи; потом — новый диск). Оценка 03.11: ~600 МБ в день, ~18 ГБ в месяц до сжатия ZFS.
Крон: каждый час в :55, живёт до :55:10 следующего часа; следующий запуск ждёт, пока предыдущий допишет минуту :55
(блокирующая блокировка до 3 мин) — разрыва на стыке часов нет.
Запуск: docker compose run --rm -e JOB_TIMEOUT=3900 collector weather_book_rec.py
"""

import fcntl
import json
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from jobmark import mark_alive
from weather_edge import CITIES, month_day_year_slug, parse_bucket

CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
BOOK_DIR = Path(os.environ.get("BOOK_DIR", "/data/booklog"))
JOB = "weather_book_rec"
DEPTH = 30
BATCH = 50
REFRESH_S = 600
GONE_AFTER = 3   # стакана нет 3 минуты подряд — маркет закрылся (день в городе кончился), убираем до обновления списка
END_MIN, END_SEC = 55, 10


def schema(db):
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS tokens (idx INTEGER PRIMARY KEY, token_id TEXT UNIQUE, condition_id TEXT, city TEXT,
        local_date TEXT, bucket_lo REAL, bucket_hi REAL);
    CREATE TABLE IF NOT EXISTS books (token INTEGER, ts INTEGER, book_ms INTEGER, asks TEXT, bids TEXT,
        PRIMARY KEY (token, ts)) WITHOUT ROWID;
    CREATE TABLE IF NOT EXISTS rounds (ts INTEGER PRIMARY KEY, n_tokens INTEGER, n_got INTEGER, n_changed INTEGER, secs REAL);
    CREATE TABLE IF NOT EXISTS missing (ts INTEGER, token INTEGER, PRIMARY KEY (ts, token)) WITHOUT ROWID;
    """)


def open_month(now):
    BOOK_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(BOOK_DIR / f"books_{now:%Y-%m}.sqlite3", timeout=60)
    schema(db)
    return db, f"{now:%Y-%m}"


def markets():
    """{token_id: (condition_id, city, local_date, lo, hi)} — все открытые варианты, дни от вчера до +3 по UTC."""
    today = datetime.now(timezone.utc).date()
    jobs = [(c, today + timedelta(days=k)) for c in CITIES for k in range(-1, 4)]

    def one(x):
        city, d = x
        slug = f"highest-temperature-in-{CITIES[city]['poly_slug']}-on-{month_day_year_slug(d)}"
        try:
            ev = requests.get(f"{GAMMA}/events", params={"slug": slug}, timeout=20).json()
        except (requests.RequestException, ValueError):
            return city, d, None
        return city, d, [m for m in (ev[0]["markets"] if ev else []) if not m.get("closed")]

    out, failed = {}, 0
    with ThreadPoolExecutor(8) as ex:
        for city, d, ms in ex.map(one, jobs):
            if ms is None:
                failed += 1
                continue
            for m in ms:
                lo, hi = parse_bucket(m["question"]) or (None, None)
                out[json.loads(m["clobTokenIds"])[0]] = (m.get("conditionId"), city, d.isoformat(), lo, hi)
    return out, failed


def levels(xs, reverse):
    lv = sorted(((float(x["price"]), float(x["size"])) for x in xs or []), reverse=reverse)
    return json.dumps([[p, round(s, 2)] for p, s in lv[:DEPTH]], separators=(",", ":"))


def fetch(toks):
    """{token_id: book}; пачка с ошибкой — её токенов нет в ответе (попадут в missing)."""
    def one(chunk):
        for a in range(2):
            try:
                r = requests.post(f"{CLOB}/books", json=[{"token_id": t} for t in chunk], timeout=20)
                r.raise_for_status()
                return [b for b in r.json() if isinstance(b, dict)]
            except (requests.RequestException, ValueError):
                time.sleep(1 + a)
        return []
    chunks = [toks[i:i + BATCH] for i in range(0, len(toks), BATCH)]
    got = {}
    with ThreadPoolExecutor(6) as ex:
        for bs in ex.map(one, chunks):
            for b in bs:
                got[b.get("asset_id")] = b
    return got


def token_idx(db, cache, tok, meta):
    if tok not in cache:
        db.execute("INSERT OR IGNORE INTO tokens (token_id, condition_id, city, local_date, bucket_lo, bucket_hi) VALUES (?, ?, ?, ?, ?, ?)",
                   (tok, *meta))
        cache[tok] = db.execute("SELECT idx FROM tokens WHERE token_id = ?", (tok,)).fetchone()[0]
    return cache[tok]


def lock():
    """Ждём, пока предыдущий запуск допишет свою последнюю минуту (до 3 мин); дольше — значит завис, выходим."""
    fh = open("/data/db/.lock_book_rec", "w")
    t0 = time.time()
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            if time.time() - t0 > 180:
                print(f"{JOB}: предыдущий запуск не закончился за 3 мин — выхожу", flush=True)
                raise SystemExit(0)
            time.sleep(1)


def run():
    fh = lock()
    now = datetime.now(timezone.utc)
    end = now.replace(minute=END_MIN, second=END_SEC, microsecond=0)
    while end <= now + timedelta(minutes=30):
        end += timedelta(hours=1)
    if os.environ.get("BOOK_REC_MIN"):   # проба: записать N минут
        end = now + timedelta(minutes=int(os.environ["BOOK_REC_MIN"]))
    db, month = open_month(now)
    idx, last = {}, {}
    mk, t_mk, gone = {}, 0.0, {}
    alive = {}
    stats = {"rounds": 0, "rows": 0, "missing": 0}
    print(f"{JOB}: запись до {end:%H:%M:%S} UTC → {BOOK_DIR}/books_{month}.sqlite3", flush=True)
    while True:
        nxt = (time.time() // 60 + 1) * 60
        if nxt > end.timestamp():
            break
        time.sleep(max(0.0, nxt - time.time()))
        t0 = time.time()
        ts = int(nxt)
        cur = datetime.fromtimestamp(ts, timezone.utc)
        if f"{cur:%Y-%m}" != month:   # новый месяц — новый файл, в нём снова полный стакан всех вариантов
            db.close()
            db, month = open_month(cur)
            idx, last = {}, {}
        if time.time() - t_mk > REFRESH_S or not mk:
            new, failed = markets()
            if new or not mk:
                mk = new
                for t in mk:   # убранный, но Gamma ещё считает открытым — ещё одна минута на проверку (сбой сети ≠ закрытие)
                    if gone.get(t, 0) >= GONE_AFTER:
                        gone[t] = GONE_AFTER - 1
            t_mk = time.time()
            print(f"{cur:%H:%M} список вариантов: {len(mk)}" + (f", Gamma не ответила по {failed} город-дням" if failed else ""), flush=True)
        toks = sorted(mk)
        got = fetch(toks)
        rows, miss = [], []
        for tok in toks:
            i = token_idx(db, idx, tok, mk[tok])
            b = got.get(tok)
            if b is None:
                miss.append((ts, i))
                gone[tok] = gone.get(tok, 0) + 1
                continue
            gone.pop(tok, None)
            a, bd = levels(b.get("asks"), False), levels(b.get("bids"), True)
            if last.get(tok) == (a, bd):
                continue
            last[tok] = (a, bd)
            bm = b.get("timestamp")
            rows.append((i, ts, int(bm) if bm else None, a, bd))
        try:
            db.executemany("INSERT OR IGNORE INTO books VALUES (?, ?, ?, ?, ?)", rows)
            db.executemany("INSERT OR IGNORE INTO missing VALUES (?, ?)", miss)
            db.execute("INSERT OR REPLACE INTO rounds VALUES (?, ?, ?, ?, ?)", (ts, len(toks), len(got), len(rows), round(time.time() - t0, 2)))
            db.commit()
        except sqlite3.OperationalError as e:
            db.rollback()
            for tok in toks:      # не записали — в следующую минуту пишем всё заново, чтобы не было «не изменился» без строки
                last.pop(tok, None)
            print(f"{cur:%H:%M} не записано — {e}", flush=True)
            continue
        for tok in [t for t in mk if gone.get(t, 0) >= GONE_AFTER]:
            mk.pop(tok)
        stats["rounds"] += 1
        stats["rows"] += len(rows)
        stats["missing"] += len(miss)
        if cur.minute % 10 == 0:
            print(f"{cur:%H:%M} стаканов {len(got)} из {len(toks)}, изменилось {len(rows)}, {time.time() - t0:.1f} с", flush=True)
        mark_alive(JOB, alive)
    db.close()
    alive["t"] = 0
    mark_alive(JOB, alive)
    print(f"{JOB}: минут {stats['rounds']}, строк {stats['rows']}, не получено {stats['missing']}", flush=True)
    fh.close()


if __name__ == "__main__":
    run()
