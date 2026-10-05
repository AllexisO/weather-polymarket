"""
Виртуальный бот-мейкер по схеме Poligarch (2026-09-30, решение Alex: «Да!»). Ставок и денег нет — только записываем,
какие заявки бот поставил бы; исполнение считается потом по настоящим сделкам (weather_mm_settle.py).

Разбор Poligarch (docs/PRD.md §10): 80% его покупок — погода, 95% — его заявки, которые забрали; покупает «да» и «нет»
одного варианта, пары склеивает в $1 (+4.45%). Наши поправки из проверок: не стоять 1 мин до и 3 мин после плановой сводки
METAR города (там мейкер теряет ~1¢ на долю в обоих периодах) и вечером дня маркета (после 18:00 местного максимум уже виден).

Правила (записаны до запуска, v1):
  маркеты — все варианты погоды на сегодня и завтра по местному времени города, пока принимают заявки;
  заявка «да» = лучшая заявка покупателей + 0.1¢ (встаём первыми), заявка «нет» = (1 − лучшая заявка продавцов «да») + 0.1¢,
  только если после улучшения не пересекаем другую сторону; цена купленной стороны 2-98¢; размер 10 долей с каждой стороны;
  пауза: минуты сводок METAR города (−1…+3 мин) и день маркета после 18:00 местного;
  sel_* — отметка «выгодной зоны» (цена стороны × время, в плюсе в обоих периодах weather_study_maker_seg.py) для кошелька mm_sel.
Стаканы — POST clob /books пачками, раз в LOOP_S секунд. Пишет в отдельную базу data/db/mm.sqlite3 (рабочую не трогает):
mm_quotes — заявка держится с ts_from до ts_to (новая строка — только когда цена или отметка поменялись).
2026-09-30 (Alex: «оба сразу»): + политика и прочие темы — кошелёк mm_pol. Маркеты — где Poligarch торговал за 7 дней, кроме
погоды (data-api activity, список раз в сутки, кэш mm_pol_markets.json), пока принимают заявки; те же заявки (+0.1¢, 10 долей),
без пауз METAR/вечера; в mm_quotes city = 'pol', local_date = дата окончания маркета.
2026-09-30 (Alex: «не зависеть от Poligarch»): + свой выбор не погодных маркетов — кошелёк mm_own (city = 'own').
Правила отбора (записаны до запуска): маркеты, за заявки на которых Polymarket платит награду (clob /rewards/markets/current),
не погода, принимают заявки, до окончания ≥ 7 дней, торговля за сутки ≥ $5 000; из них 40 с самой большой наградой в день.
Список раз в сутки (mm_own_markets.json). Заявки — как везде (+0.1¢, 10 долей, без пауз); маркет может быть и в mm_pol,
и в mm_own — это разные кошельки, заявки пишутся отдельно.
Крон: каждый час, работает LISTEN_MIN минут. Запуск: docker compose run --rm -e JOB_TIMEOUT=3600 collector weather_mm_paper.py
"""
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from weather_cities import OBS_CITIES
from weather_edge import GAMMA, month_day_year_slug, parse_bucket

MM_DB = Path(os.environ.get("MM_DB", "/data/db/mm.sqlite3"))
MAIN_DB = os.environ.get("POLY_LAB_DB", "/data/db/polymarket_lab.sqlite3")
CLOB = "https://clob.polymarket.com"
LISTEN_MIN = float(os.environ.get("LISTEN_MIN", "57"))
LOOP_S = 30
SIZE = 10.0
TICK = 0.001
PMIN, PMAX = 0.02, 0.98
EVENING_H = 18
METAR_PAUSE = (1, 3)  # минут до и после плановой сводки
# «выгодные зоны» для mm_sel: (цена стороны от, до, время) — в плюсе в обоих периодах (weather_study_maker_seg.py)
SEL = [(0.00, 0.10, "0-6"), (0.00, 0.10, "12-15"), (0.00, 0.10, "15-18"), (0.10, 0.30, "15-18"),
       (0.30, 0.50, "0-6"), (0.70, 0.90, "9-12"), (0.90, 1.01, "накануне"), (0.90, 1.01, "6-9")]
METAR_CACHE = MM_DB.parent / "mm_metar_minutes.json"
POL_CACHE = MM_DB.parent / "mm_pol_markets.json"
POLIGARCH = "0xb40e89677d59665d5188541ad860450a6e2a7cc9"
DATA_API = "https://data-api.polymarket.com"
OWN_CACHE = MM_DB.parent / "mm_own_markets.json"
OWN_TOP, OWN_MIN_DAYS, OWN_MIN_VOL24 = 40, 7, 5000.0


def schema(db):
    db.execute("PRAGMA journal_mode=WAL")  # 01.10: чтение (проверки, сайт) не блокирует запись бота
    db.execute("""CREATE TABLE IF NOT EXISTS mm_quotes (id INTEGER PRIMARY KEY, condition_id TEXT, city TEXT, local_date TEXT,
                  bucket_lo REAL, bucket_hi REAL, ts_from INTEGER, ts_to INTEGER, yes_bid REAL, no_bid REAL, size REAL,
                  sel_yes INTEGER, sel_no INTEGER, best_bid REAL, best_ask REAL)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_mm_quotes_cond ON mm_quotes(condition_id, ts_from)")
    db.commit()


def metar_minutes():
    """Плановые минуты сводок по городу (самые частые минуты valid_utc за 30 дней). Кэш на сутки."""
    try:
        if METAR_CACHE.exists() and time.time() - METAR_CACHE.stat().st_mtime < 86400:
            return json.loads(METAR_CACHE.read_text())
    except (OSError, ValueError):
        pass
    from collections import Counter
    conn = sqlite3.connect(MAIN_DB, timeout=30)
    since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    out = {}
    for city in OBS_CITIES:
        c = Counter(int(r[0][14:16]) for r in conn.execute(
            "SELECT valid_utc FROM station_obs WHERE city = ? AND valid_utc >= ?", (city, since)))
        tot = sum(c.values()) or 1
        out[city] = sorted(m for m, n in c.items() if n / tot >= 0.15) or [0]
    conn.close()
    METAR_CACHE.write_text(json.dumps(out))
    return out


def time_zone(now_local, local_date):
    """«накануне» или часовой отрезок дня маркета (как в weather_study_maker_seg.py)."""
    if now_local.date().isoformat() < local_date:
        return "накануне"
    h = now_local.hour
    for a, b in ((0, 6), (6, 9), (9, 12), (12, 15), (15, 18), (18, 24)):
        if a <= h < b:
            return f"{a}-{b}"


def is_sel(price, zone):
    return any(a <= price < b and z == zone for a, b, z in SEL)


def load_markets():
    """Варианты погоды на сегодня и завтра по местному времени каждого города."""
    out = []
    for city, cfg in OBS_CITIES.items():
        now = datetime.now(ZoneInfo(cfg["tz"]))
        for d in (now, now + timedelta(days=1)):
            slug = f"highest-temperature-in-{cfg['poly_slug']}-on-{month_day_year_slug(d)}"
            try:
                ev = requests.get(f"{GAMMA}/events", params={"slug": slug}, timeout=20).json()
            except (requests.RequestException, ValueError):
                continue
            if not ev or not isinstance(ev, list):
                continue
            for m in ev[0].get("markets", []):
                rng = parse_bucket(m.get("question") or "")
                if rng is None or m.get("closed") or not m.get("acceptingOrders", True):
                    continue
                out.append({"cid": m["conditionId"], "token": json.loads(m["clobTokenIds"])[0],
                            "token_no": json.loads(m["clobTokenIds"])[1], "city": city,
                            "local_date": d.date().isoformat(), "lo": rng[0], "hi": rng[1]})
            time.sleep(0.1)
    return out


def pol_markets():
    """Не погодные маркеты, где Poligarch торговал за 7 дней и которые ещё принимают заявки. Кэш на сутки."""
    try:
        if POL_CACHE.exists() and time.time() - POL_CACHE.stat().st_mtime < 86400:
            return json.loads(POL_CACHE.read_text())
    except (OSError, ValueError):
        pass
    start, end, cids = int(time.time()) - 7 * 86400, int(time.time()), {}
    for _ in range(80):
        try:
            got = requests.get(f"{DATA_API}/activity", params={"user": POLIGARCH, "limit": 500, "start": start, "end": end,
                                                                "sortBy": "TIMESTAMP", "sortDirection": "DESC"}, timeout=60).json()
        except (requests.RequestException, ValueError):
            break
        if not isinstance(got, list) or not got:
            break
        for a in got:
            if a.get("type") == "TRADE" and "temperature" not in (a.get("title") or "").lower():
                cids[a["conditionId"]] = a.get("title")
        mn = min(a["timestamp"] for a in got)
        end = mn - 1 if mn < end else end - 1
        if len(got) < 500:
            break
        time.sleep(0.4)
    out = []
    for cid in cids:
        try:
            m = requests.get(f"{CLOB}/markets/{cid}", timeout=30).json()
        except (requests.RequestException, ValueError):
            continue
        if m.get("closed") or not m.get("accepting_orders") or not m.get("tokens"):
            continue
        out.append({"cid": cid, "token": m["tokens"][0]["token_id"], "city": "pol", "q": m.get("question"),
                    "local_date": (m.get("end_date_iso") or "2099-01-01")[:10], "lo": None, "hi": None})
        time.sleep(0.1)
    POL_CACHE.write_text(json.dumps(out))
    return out


def own_markets():
    """Наш выбор не погодных маркетов (правила — в описании модуля). Кэш на сутки."""
    try:
        if OWN_CACHE.exists() and time.time() - OWN_CACHE.stat().st_mtime < 86400:
            return json.loads(OWN_CACHE.read_text())
    except (OSError, ValueError):
        pass
    rw, cur = {}, None
    for _ in range(200):
        try:
            r = requests.get(f"{CLOB}/rewards/markets/current", params={"next_cursor": cur} if cur else {}, timeout=60).json()
        except (requests.RequestException, ValueError):
            break
        for x in r.get("data", []):
            rw[x["condition_id"]] = float(x.get("total_daily_rate") or 0)
        cur = r.get("next_cursor")
        if not cur or cur == "LTE=" or not r.get("data"):
            break
        time.sleep(0.1)
    top = sorted(rw, key=rw.get, reverse=True)[:800]
    now = datetime.now(timezone.utc)
    cand = []
    for i in range(0, len(top), 40):
        try:
            ms = requests.get(f"{GAMMA}/markets", params=[("condition_ids", c) for c in top[i:i + 40]], timeout=60).json()
        except (requests.RequestException, ValueError):
            continue
        for m in ms if isinstance(ms, list) else []:
            q = m.get("question") or ""
            try:
                end = datetime.fromisoformat((m.get("endDate") or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if ("temperature" in q.lower() or m.get("closed") or not m.get("acceptingOrders")
                    or end < now + timedelta(days=OWN_MIN_DAYS) or float(m.get("volume24hr") or 0) < OWN_MIN_VOL24):
                continue
            cand.append({"cid": m["conditionId"], "token": json.loads(m["clobTokenIds"])[0], "city": "own", "q": q,
                         "local_date": end.date().isoformat(), "lo": None, "hi": None, "reward": rw.get(m["conditionId"], 0.0),
                         "vol24": float(m.get("volume24hr") or 0)})
        time.sleep(0.2)
    out = sorted(cand, key=lambda x: (-x["reward"], -x["vol24"]))[:OWN_TOP]
    OWN_CACHE.write_text(json.dumps(out))
    return out


def books(tokens):
    res = {}
    for i in range(0, len(tokens), 100):
        chunk = tokens[i:i + 100]
        try:
            r = requests.post(f"{CLOB}/books", json=[{"token_id": t} for t in chunk], timeout=30)
            data = r.json() if r.status_code == 200 else []
        except (requests.RequestException, ValueError):
            continue
        for b in data if isinstance(data, list) else []:
            bids = [float(x["price"]) for x in b.get("bids", []) if float(x["size"]) > 0]
            asks = [float(x["price"]) for x in b.get("asks", []) if float(x["size"]) > 0]
            res[b.get("asset_id")] = (max(bids) if bids else None, min(asks) if asks else None)
    return res


def quote(m, bk, mins, now_utc):
    """Заявки бота на этот вариант или None (пауза / нет стакана)."""
    bb, ba = bk
    if bb is None or ba is None or ba - bb < 2 * TICK - 1e-9:
        return None
    pol = m["city"] in ("pol", "own")
    loc = now_utc.astimezone(ZoneInfo("UTC" if pol else OBS_CITIES[m["city"]]["tz"]))
    if loc.date().isoformat() > m["local_date"]:
        return None
    if not pol and loc.date().isoformat() == m["local_date"] and loc.hour >= EVENING_H:
        return None
    mn = now_utc.minute
    if not pol and any((mn - x) % 60 <= METAR_PAUSE[1] or (x - mn) % 60 <= METAR_PAUSE[0] for x in mins.get(m["city"], [])):
        return None
    yb = round(bb + TICK, 3)
    nb = round(1 - ba + TICK, 3)
    if yb >= ba - 1e-9 or (1 - nb) <= yb + 1e-9:  # после улучшения стороны пересеклись бы — стоим на лучшей цене
        yb, nb = round(bb, 3), round(1 - ba, 3)
    zone = None if pol else time_zone(loc, m["local_date"])
    yes = yb if PMIN <= yb <= PMAX else None
    no = nb if PMIN <= nb <= PMAX else None
    if yes is None and no is None:
        return None
    return {"yes": yes, "no": no, "sel_yes": int(yes is not None and is_sel(yes, zone)),
            "sel_no": int(no is not None and is_sel(no, zone)), "bb": bb, "ba": ba}


def main():
    from jobmark import single_instance
    single_instance("mm_paper")
    MM_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(MM_DB, timeout=120)
    schema(db)
    mins = metar_minutes()
    end = time.time() + LISTEN_MIN * 60
    markets, loaded = [], 0.0
    open_q = {}  # condition_id -> (row id, ключ заявки)
    n_loops = 0
    from jobmark import mark_alive
    alive = {}
    while time.time() < end:
        t0 = time.time()
        mark_alive("weather_mm_paper", alive)
        if t0 - loaded > 1800 or not markets:
            markets, loaded = load_markets(), t0
            for name, fn in (("политика (список Poligarch)", pol_markets), ("свой выбор маркетов", own_markets)):
                try:
                    markets += fn()
                except Exception as e:  # noqa: BLE001 — не погодные маркеты не должны ронять погоду
                    print(f"{name}: список не загружен ({type(e).__name__}: {e})", flush=True)
            n_pol = sum(m["city"] == "pol" for m in markets); n_own = sum(m["city"] == "own" for m in markets)
            print(f"{datetime.now(timezone.utc):%H:%M} в работе: погода {len(markets) - n_pol - n_own} вариантов, "
                  f"список Poligarch {n_pol}, свой выбор {n_own}", flush=True)
        bk = books([m["token"] for m in markets])
        now = datetime.now(timezone.utc)
        ts = int(now.timestamp())
        seen = set()
        try:
            with db:
                for m in markets:
                    q = quote(m, bk.get(m["token"], (None, None)), mins, now)
                    cid = (m["cid"], m["city"])
                    key = None if q is None else (q["yes"], q["no"], q["sel_yes"], q["sel_no"])
                    prev = open_q.get(cid)
                    if prev and prev[1] == key:
                        seen.add(cid)
                        continue
                    if prev:
                        db.execute("UPDATE mm_quotes SET ts_to = ? WHERE id = ?", (ts, prev[0]))
                        del open_q[cid]
                    if q is None:
                        continue
                    cur = db.execute("""INSERT INTO mm_quotes (condition_id, city, local_date, bucket_lo, bucket_hi, ts_from, ts_to,
                                        yes_bid, no_bid, size, sel_yes, sel_no, best_bid, best_ask) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                     (m["cid"], m["city"], m["local_date"], m["lo"], m["hi"], ts, ts + LOOP_S, q["yes"], q["no"], SIZE,
                                      q["sel_yes"], q["sel_no"], q["bb"], q["ba"]))
                    open_q[cid] = (cur.lastrowid, key)
                    seen.add(cid)
                # заявки, которые держатся, — продлеваем до следующего круга; пропавшие варианты — закрываем
                for cid in list(open_q):
                    if cid in seen:
                        db.execute("UPDATE mm_quotes SET ts_to = ? WHERE id = ?", (ts + LOOP_S, open_q[cid][0]))
                    else:
                        db.execute("UPDATE mm_quotes SET ts_to = ? WHERE id = ?", (ts, open_q[cid][0]))
                        del open_q[cid]
        except sqlite3.OperationalError as e:  # 01.10: база занята — пропускаем круг, заявки допишутся на следующем
            print(f"база ботов занята ({e}) — пропускаю круг", flush=True)
            open_q.clear()  # запись откатилась — на следующем круге заявки ставятся заново
        n_loops += 1
        time.sleep(max(1.0, LOOP_S - (time.time() - t0)))
    ts = int(time.time())
    with db:
        for rid, _ in open_q.values():
            db.execute("UPDATE mm_quotes SET ts_to = MIN(ts_to, ?) WHERE id = ?", (ts, rid))
    n_q = db.execute("SELECT COUNT(*) FROM mm_quotes WHERE ts_from >= ?", (int(end - LISTEN_MIN * 60),)).fetchone()[0]
    db.close()
    print(f"кругов {n_loops}, новых заявок (изменений цены) {n_q}, вариантов в работе {len(markets)}", flush=True)
    from jobmark import mark
    c = sqlite3.connect(MAIN_DB, timeout=60)
    mark(c, "weather_mm_paper")
    c.close()


if __name__ == "__main__":
    main()
