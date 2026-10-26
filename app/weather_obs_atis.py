"""
Кошелёк obs_atis (25.10, Alex «ATIS — делаем! 2 доллара — это 2 доллара»): как obs_wethr, но сводка приходит из текстового
ATIS аэропорта (atis.info — бесплатное зеркало FAA D-ATIS), 11 городов США. Правила покупки те же (weather_obs_rt.buy_dead):
сводка подняла максимум дня → «нет» на варианты, которые она убила, пока «нет» продают дешевле MIN_BID.

Почему (data/research/atis_race, RESEARCH_JOURNAL 24-25.10): 24.10 00:51 — 25.10 07:59 UTC, 301 плановая сводка — ATIS раньше
NOAA tgftp в 244 (81%), медиана +75 с; температура ATIS = NOAA во всех 301 (группа T — точно, без неё целые °C согласуются).
Деньги на 24.10 (по сделкам data-api): в окне ATIS → NOAA на уже мёртвых вариантах ~$13 на 11 городов, из них $12 уже забрали
тейкеры — ждём немного: доллары в день, а не десятки.

Только плановые сводки: с 23.10 SPECI в итог Polymarket не идёт (weather_obs_live.routine_only — по минуте сводки города).
Текст ATIS без группы T — целые °C → нижняя граница °F (weather_obs_rt.value = safe_value).
Максимум дня до запуска — плановые сводки aviationweather за 30 ч (как ml_day); не получилось для города — город в этот час
не торгуем (иначе «прежний максимум» был бы −999 и ставка шла бы на давно мёртвые варианты).

ПРОВЕРКА ДО ПЕРВОЙ СТАВКИ (CLAUDE.md): данных ATIS — один день, нужно ≥ 7. Поэтому OBS_ATIS_DRY=1 (по умолчанию): ставок нет,
всё, что купили бы, — в data/obs_atis_dry.jsonl (сводка, максимум, убитые варианты, лучшая цена «нет» в стакане). Через 7 дней —
сверка с итогом Polymarket (выигравший вариант ни разу не «убит») → снять OBS_ATIS_DRY (решение Alex, заметка).

Запуск: крон каждый час в :48, LISTEN_MIN 15 мин (плановые сводки этих городов — :51-:56, в ATIS через 1-3 мин).
  docker compose run --rm -e JOB_TIMEOUT=1200 collector weather_obs_atis.py
"""
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from weather_cities import OBS_CITIES, station_for
from weather_obs_live import MIN_BID, ROUTINE_FROM, cash, ensure_schema, routine_only
from weather_obs_rt import Books, Markets, buy_dead, check_watch, value

DB_PATH = os.environ.get("POLY_LAB_DB", "/data/db/polymarket_lab.sqlite3")
WALLET = "obs_atis"
CITIES = ("nyc", "atlanta", "miami", "los_angeles", "chicago", "dallas", "san_francisco", "houston", "denver", "seattle", "austin")
LISTEN_MIN = float(os.environ.get("LISTEN_MIN", "15"))
POLL_SEC = 4            # опрос одного аэропорта в окне сводки (замер 24-25.10 шёл раз в 5 с)
WIN = (0, 9)            # окно опроса: от минуты сводки города до +9 мин
DRY = os.environ.get("OBS_ATIS_DRY", "1") != "0"
DRY_FILE = os.path.join(os.path.dirname(os.path.dirname(DB_PATH)), "obs_atis_dry.jsonl")
ATIS = "https://atis.info/api/{}"
RE_INFO = re.compile(r"INFO [A-Z] (\d{2})(\d{2})Z")
RE_T = re.compile(r"\bT([01])(\d{3})([01])(\d{3})\b")
RE_BODY = re.compile(r"\s(M?\d{2})/(M?\d{2})?\s")


def parse(text, now):
    """Текст ATIS → (время сводки UTC, °C, точно ли). Группа T — десятые; иначе целые °C из тела сводки."""
    m = RE_INFO.search(text)
    if not m:
        return None
    t = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    if t > now + timedelta(minutes=5):
        t -= timedelta(days=1)
    g = RE_T.search(text)
    if g:
        return t, (-1 if g.group(1) == "1" else 1) * int(g.group(2)) / 10, True
    b = RE_BODY.search(" " + text + " ")
    if b:
        v = b.group(1)
        return t, float(-int(v[1:]) if v.startswith("M") else int(v)), False
    return None


def seed(now):
    """Максимум дня по плановым сводкам aviationweather за 30 ч: {(город, дата): °F}; города, где не вышло, — нет в словаре."""
    ids = {station_for(c, now.astimezone(ZoneInfo(OBS_CITIES[c]["tz"])).date()): c for c in CITIES}
    mx = {}
    for wait in (5, 20, None):
        try:
            rows = requests.get("https://aviationweather.gov/api/data/metar", params={"ids": ",".join(ids), "hours": 30, "format": "json"},
                                timeout=30).json()
            break
        except (requests.RequestException, ValueError) as e:
            if wait is None:
                print(f"{WALLET}: сводки для максимума дня недоступны — {e}; без торговли", file=sys.stderr, flush=True)
                return {}
            time.sleep(wait)
    got = set()
    for m in rows if isinstance(rows, list) else []:
        city = ids.get(m.get("icaoId"))
        if not city or m.get("temp") is None or not m.get("obsTime"):
            continue
        cfg = OBS_CITIES[city]
        day = datetime.fromtimestamp(m["obsTime"], timezone.utc).astimezone(ZoneInfo(cfg["tz"])).date()
        got.add(city)
        if not routine_only(m, city, cfg, day.isoformat()):
            continue
        k = (city, day)
        mx[k] = max(mx.get(k, -999), value(float(m["temp"]), cfg))
    # 25.10 (урок ml_day 23.10): сервис может отдать неполные сводки — город без свежей (< 2 ч) сводки не торгуем
    fresh = {ids[m["icaoId"]] for m in rows if isinstance(rows, list) and m.get("icaoId") in ids and m.get("obsTime")
             and now.timestamp() - m["obsTime"] < 2 * 3600}
    return {k: v for k, v in mx.items() if k[0] in fresh}


def dry_record(city, day, v, prev, t, src_exact, markets, books):
    """OBS_ATIS_DRY: что купили бы — убитые варианты и лучшая цена «нет» в стакане сейчас."""
    rows = []
    for rng, m in markets:
        if prev <= rng[1] < v:
            asks = books.get(json.loads(m["clobTokenIds"])[1]) or []
            rows.append({"bucket": rng, "best_no_ask": asks[0][0] if asks else None,
                         "no_depth_usd": round(sum(p * s for p, s in asks if p <= 1 - MIN_BID), 2)})
    if rows:
        with open(DRY_FILE, "a") as f:
            f.write(json.dumps({"seen": datetime.now(timezone.utc).isoformat(), "city": city, "day": day.isoformat(), "obs": t.isoformat(),
                                "max_f": v, "prev_f": prev, "exact": src_exact, "dead": rows}) + "\n")


def main():
    from jobmark import mark_alive, single_instance
    single_instance("obs_atis")
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    now = datetime.now(timezone.utc)
    day_max = seed(now)
    mk, alive, watch = Markets(), {"t": 0}, {}
    try:
        mins = json.loads(open(os.path.join(os.path.dirname(DB_PATH), "mm_metar_minutes.json")).read())
    except (OSError, ValueError):
        mins = {}
    no_tokens = []
    for c in CITIES:
        for _rng, m in mk.get(c, now.astimezone(ZoneInfo(OBS_CITIES[c]["tz"])).date()):
            no_tokens.append(json.loads(m["clobTokenIds"])[1])
    books = Books(no_tokens)
    books.start()
    books.ready.wait(20)
    sess = requests.Session()
    end = time.time() + LISTEN_MIN * 60
    known, last, n_new, n_err = set(), {}, 0, 0
    print(f"{WALLET}: старт{' (ВХОЛОСТУЮ — ставок нет)' if DRY else ''}, городов {len(CITIES)}, максимумов дня {len(day_max)}", flush=True)
    while time.time() < end:
        now = datetime.now(timezone.utc)
        for city in CITIES:
            cfg = OBS_CITIES[city]
            tz = ZoneInfo(cfg["tz"])
            day = now.astimezone(tz).date()
            if not any(WIN[0] * 60 <= (now.minute * 60 + now.second - mm * 60) % 3600 <= WIN[1] * 60 for mm in mins.get(city, [53])):
                continue
            if time.time() - last.get(city, 0) < POLL_SEC:
                continue
            last[city] = time.time()
            icao = station_for(city, day)
            try:
                text = sess.get(ATIS.format(icao), timeout=6).text
            except requests.RequestException:
                n_err += 1
                continue
            p = parse(text, now)
            if not p:
                continue
            t, temp, exact = p
            if (city, t) in known:
                continue
            known.add((city, t))
            try:
                conn.execute("INSERT OR IGNORE INTO metar_seen_src (icao, obs_time_utc, source, first_seen_utc, temp_c) VALUES (?, ?, ?, ?, ?)",
                             (icao, t.isoformat(), "atis_rt", now.isoformat(), temp))
                conn.commit()
            except sqlite3.OperationalError:
                conn.rollback()
            mark_alive("weather_obs_atis", alive)
            d_obs = t.astimezone(tz).date()
            if d_obs != day or (city, day) not in day_max:
                continue                                          # чужой день или максимум дня неизвестен — не торгуем
            if not routine_only({"obsTime": t.timestamp(), "src": "tgftp"}, city, cfg, day.isoformat()):
                continue                                          # SPECI (не по минуте сводки) — с 23.10 не в итог
            n_new += 1
            v = value(temp, cfg)                                  # целые °C в °F-городе — нижняя граница (safe_value)
            prev = day_max[(city, day)]
            if v <= prev:
                continue
            day_max[(city, day)] = v
            try:
                if DRY:
                    dry_record(city, day, v, prev, t, exact, mk.get(city, day), books)
                else:
                    buy_dead(conn, city, cfg, day, v, prev, t, mk.get(city, day), "atis_rt", wallet=WALLET, books=books, watch=watch)
            except Exception as e:  # noqa: BLE001 — один город не роняет весь запуск
                conn.rollback()
                print(f"{WALLET}: {city} ошибка — {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        if not DRY:
            try:
                check_watch(conn, books, watch, WALLET)
            except Exception as e:  # noqa: BLE001
                conn.rollback()
                print(f"{WALLET}: слежка — ошибка {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        time.sleep(0.5)
    print(f"{WALLET}: новых плановых сводок {n_new}, ошибок запроса {n_err}, баланс ${cash(conn, WALLET):.2f}", flush=True)
    conn.close()


if __name__ == "__main__":
    if not ROUTINE_FROM:
        sys.exit("ROUTINE_FROM не задан")
    main()
