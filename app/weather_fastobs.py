"""
Быстрые замеры вместо ожидания сводки METAR (2026-09-30, решение Alex: «да» — проверить бесплатные и пробные источники,
прежде чем платить за wethr.net / Synoptic). Кошелёк obs_fast.

Polymarket считает максимум дня по сводкам METAR, а не по всем замерам (урок 23.09: Даллас, 5-мин 99°F, METAR 97°F,
выиграл 96-97°F). Поэтому быстрый источник используем только чтобы узнать значение БЛИЖАЙШЕЙ сводки раньше её публикации:
берём замеры в минуты плановой сводки города (±2 мин, минуты — data/db/mm_metar_minutes.json), округляем как METAR
(°C — до целого; °F — из °C), и если такой максимум уже выше варианта — ставим против варианта, как кошелёк obs
(weather_obs_live.bet_dead_buckets: только если рынок ещё платит за него ≥ 5¢, по живому стакану, $2).
Источники (бесплатно или пробный доступ):
  synoptic — аэропорты США, замер каждые 5 мин, задержка ~6 мин (пробный токен SYNOPTIC_TOKEN в .env, до ~13.10);
  jma      — Токио, Ханэда (AMeDAS 44166), каждые 10 мин;
  dwd      — Мюнхен, аэропорт (станция 01262, 10-минутные данные open data);
  fmi      — Хельсинки-Вантаа (как obs_fmi).
  knmi     — Амстердам, Схипхол (станция 06240), 10-минутные файлы KNMI Open Data (ключ KNMI_API_KEY, с 30.09),
             появляются через ~4 мин; сводка EHAM в :25/:55, замеры в :20/:30 и :50/:00 — окно для KNMI ±5 мин;
  hko      — Гонконг, «максимум с полуночи» обсерватории — сам итог маркета (без окна и округления);
  metar    — Тайбэй (RCSS) — только сводки (у CWA нет датчика в аэропорту: ближайшие в 2-4 км, для ставки не годятся).
Париж (Météo-France) — вход на портале сломан у них; Сеул/Пусан (KMA) — нужен корейский телефон: пропущены.
Каждый замер пишется в data/db/fastobs.sqlite3 (fast_obs: когда впервые увидели) — чтобы честно мерить фору против METAR
(metar_seen_src в рабочей базе). Ставки — в paper_obs_trades (wallet = 'obs_fast').
Крон каждую минуту: docker compose run --rm collector weather_fastobs.py
"""
import csv
import io
import json
import math
import os
import re
import sqlite3
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from jobmark import item_guard
from weather_cities import ALL_OBS_CITIES as OBS_CITIES

MAIN_DB = Path(os.environ.get("POLY_LAB_DB", "/data/db/polymarket_lab.sqlite3"))
FAST_DB = MAIN_DB.parent / "fastobs.sqlite3"
METAR_MIN = MAIN_DB.parent / "mm_metar_minutes.json"
WINDOW = 2  # минут вокруг плановой сводки
JMA_POINTS = {"tokyo": "44166"}
DWD_STATIONS = {"munich": "01262"}
FMI_IDS = {"helsinki": 100968}
WALLET = "obs_fast"


def fast_db():
    db = sqlite3.connect(FAST_DB, timeout=30)
    db.execute("""CREATE TABLE IF NOT EXISTS fast_obs (source TEXT, city TEXT, obs_utc TEXT, temp_c REAL, first_seen_utc TEXT,
                  PRIMARY KEY (source, city, obs_utc))""")
    db.execute("CREATE TABLE IF NOT EXISTS fast_state (city TEXT, day TEXT, max_v REAL, PRIMARY KEY (city, day))")
    db.execute("CREATE TABLE IF NOT EXISTS fast_files (source TEXT, name TEXT, PRIMARY KEY (source, name))")
    db.commit()
    return db


def synoptic():
    tok = os.environ.get("SYNOPTIC_TOKEN")
    if not tok:
        return []
    us = {cfg["icao"]: c for c, cfg in OBS_CITIES.items() if (cfg.get("icao") or "").startswith("K")}
    r = requests.get("https://api.synopticdata.com/v2/stations/timeseries",
                     params={"stid": ",".join(us), "recent": 180, "vars": "air_temp", "obtimezone": "utc", "token": tok}, timeout=30).json()
    out = []
    for s in r.get("STATION", []) or []:
        o = s.get("OBSERVATIONS", {})
        for t, v in zip(o.get("date_time", []), o.get("air_temp_set_1", [])):
            if v is not None and s["STID"] in us:
                out.append(("synoptic", us[s["STID"]], datetime.fromisoformat(t.replace("Z", "+00:00")), float(v)))
    return out


def jma():
    out = []
    lt = datetime.fromisoformat(requests.get("https://www.jma.go.jp/bosai/amedas/data/latest_time.txt", timeout=20).text.strip())
    for city, pt in JMA_POINTS.items():
        for blk in {lt.replace(hour=lt.hour // 3 * 3, minute=0), (lt - timedelta(hours=3)).replace(hour=(lt - timedelta(hours=3)).hour // 3 * 3, minute=0)}:
            d = requests.get(f"https://www.jma.go.jp/bosai/amedas/data/point/{pt}/{blk:%Y%m%d}_{blk.hour:02d}.json", timeout=20)
            if d.status_code != 200:
                continue
            for k, v in d.json().items():
                tv = (v.get("temp") or [None])[0]
                if tv is not None:
                    t = datetime.strptime(k, "%Y%m%d%H%M%S").replace(tzinfo=ZoneInfo("Asia/Tokyo")).astimezone(timezone.utc)
                    out.append(("jma", city, t, float(tv)))
    return out


def dwd():
    out = []
    for city, st in DWD_STATIONS.items():
        url = f"https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/10_minutes/air_temperature/now/10minutenwerte_TU_{st}_now.zip"
        z = zipfile.ZipFile(io.BytesIO(requests.get(url, timeout=30).content))
        name = next(n for n in z.namelist() if n.endswith(".txt"))
        rows = csv.reader(io.StringIO(z.read(name).decode("latin-1")), delimiter=";")
        head = [h.strip() for h in next(rows)]
        i_t, i_v = head.index("MESS_DATUM"), head.index("TT_10")
        for r in rows:
            try:
                v = float(r[i_v])
            except (ValueError, IndexError):
                continue
            if v > -99:
                out.append(("dwd", city, datetime.strptime(r[i_t].strip(), "%Y%m%d%H%M").replace(tzinfo=timezone.utc), v))
    return out


def fmi():
    out = []
    start = (datetime.now(timezone.utc) - timedelta(hours=26)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for city, fid in FMI_IDS.items():
        r = requests.get("https://opendata.fmi.fi/wfs", params={
            "service": "WFS", "version": "2.0.0", "request": "getFeature", "storedquery_id": "fmi::observations::weather::simple",
            "fmisid": fid, "parameters": "t2m", "starttime": start}, timeout=20)
        for t, v in zip(re.findall(r"<BsWfs:Time>([^<]+)", r.text), re.findall(r"<BsWfs:ParameterValue>([^<]+)", r.text)):
            if v != "NaN":
                out.append(("fmi", city, datetime.fromisoformat(t.replace("Z", "+00:00")), float(v)))
    return out


def hko():
    """«Максимум с полуночи» на станции обсерватории (это и есть итог маркета Гонконга), раз в 10 мин."""
    out = []
    txt = requests.get("https://data.weather.gov.hk/weatherAPI/hko_data/regional-weather/latest_since_midnight_maxmin.csv", timeout=20)
    txt.encoding = "utf-8"
    for r in csv.reader(io.StringIO(txt.text.lstrip("\ufeff"))):
        if len(r) >= 3 and r[1].strip() == OBS_CITIES["hong_kong"]["hko"]:
            try:
                t = datetime.strptime(r[0].strip(), "%Y%m%d%H%M").replace(tzinfo=ZoneInfo("Asia/Hong_Kong")).astimezone(timezone.utc)
                out.append(("hko", "hong_kong", t, float(r[2])))
            except ValueError:
                pass
    return out


def metar_extra():
    """Сводки METAR городов вне 48 (Тайбэй — RCSS), пока нет быстрого источника (CWA — по ключу)."""
    ids = {cfg["icao"]: c for c, cfg in OBS_CITIES.items() if cfg.get("icao") and c in ("taipei",)}
    r = requests.get("https://aviationweather.gov/api/data/metar", params={"ids": ",".join(ids), "hours": 26, "format": "json"}, timeout=30).json()
    return [("metar", ids[m["icaoId"]], datetime.fromtimestamp(m["obsTime"], timezone.utc), float(m["temp"]))
            for m in r if isinstance(m, dict) and m.get("temp") is not None and m.get("obsTime") and m.get("icaoId") in ids]


KNMI_URL = "https://api.dataplatform.knmi.nl/open-data/v1/datasets/10-minute-in-situ-meteorological-observations/versions/1.0/files"
KNMI_STATIONS = {"amsterdam": "06240"}
WINDOW_BY_SOURCE = {"knmi": 5}
# 2026-10-01: запас к границе округления для источников с другим датчиком/усреднением, чем METAR. Замер у границы .5
# (x.4-x.6) после округления расходился со сводкой METAR в ~1/3 случаев (KNMI 4 из 12, FMI 11 из 30, DWD 4 из 14;
# JMA 0 из 10 — даёт то же, что сводка). Пример: KNMI 19.5 в 22:30Z → «уже 20», а METAR 22:25/22:55 — 19.
# Градус считаем достигнутым, только если замер выше границы на ROUND_MARGIN (20 — от 19.7).
ROUND_MARGIN = {"knmi": 0.2, "fmi": 0.2, "dwd": 0.2}


def knmi(db):
    """Последние 10-минутные файлы KNMI (скачиваем только новые — список уже прочитанных в fast_files)."""
    key = os.environ.get("KNMI_API_KEY")
    if not key:
        return []
    import h5py
    h = {"Authorization": key}
    files = requests.get(KNMI_URL, params={"orderBy": "created", "sorting": "desc", "maxKeys": 6}, headers=h, timeout=30).json().get("files", [])
    done = {r[0] for r in db.execute("SELECT name FROM fast_files WHERE source = 'knmi'")}
    out = []
    for f in files:
        if f["filename"] in done:
            continue
        url = requests.get(f"{KNMI_URL}/{f['filename']}/url", headers=h, timeout=30).json()["temporaryDownloadUrl"]
        with h5py.File(io.BytesIO(requests.get(url, timeout=60).content), "r") as nc:
            st = [s.decode() if isinstance(s, bytes) else str(s) for s in nc["station"][:]]
            t = datetime(1950, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=float(nc["time"][0]))
            for city, sid in KNMI_STATIONS.items():
                if sid in st:
                    v = float(nc["ta"][st.index(sid)][0])
                    if not math.isnan(v):
                        out.append(("knmi", city, t, v))
        with db:
            db.execute("INSERT OR IGNORE INTO fast_files VALUES ('knmi', ?)", (f["filename"],))
    return out


def metar_value(temp_c, unit):
    return round(temp_c * 9 / 5 + 32) if unit == "fahrenheit" else math.floor(temp_c + 0.5)


def patient(name, fn, waits=(5, 10)):
    """01.10: сервер источника иногда не отвечает (opendata.dwd.de — ~1 запуск из 6 не дождались за 30 с; aviationweather иногда отдаёт пустой ответ вместо JSON). Повторяем;
    не ответил и после повторов — пропускаем без ошибки: следующий запуск через 2 мин возьмёт те же данные (файлы и
    ответы источников — за последние часы). Долгий простой источника видит проверка дня (свежесть fast_obs)."""
    for w in (*waits, None):
        try:
            return fn()
        except (requests.Timeout, requests.ConnectionError, requests.exceptions.JSONDecodeError) as e:   # 02.10: aviationweather отдал пустой ответ
            if w is None:
                print(f"источник {name} не ответил ({type(e).__name__}) — возьмём в следующий запуск", flush=True)
                return []
            time.sleep(w)


def main():
    from jobmark import single_instance
    single_instance("fastobs")
    now = datetime.now(timezone.utc)
    db = fast_db()
    try:
        mins = json.loads(METAR_MIN.read_text())
    except (OSError, ValueError):
        mins = {}
    readings = []
    main_conn = sqlite3.connect(MAIN_DB, timeout=60)
    for name, fn in (("synoptic", synoptic), ("jma", jma), ("dwd", dwd), ("fmi", fmi), ("hko", hko), ("metar", metar_extra),
                     ("knmi", lambda: knmi(db))):
        with item_guard(f"источник {name}", main_conn):
            readings += patient(name, fn)
    with db:
        db.executemany("INSERT OR IGNORE INTO fast_obs VALUES (?, ?, ?, ?, ?)",
                       [(s, c, t.isoformat(), v, now.isoformat()) for s, c, t, v in readings])
    # максимум дня по замерам в минуты плановой сводки — только когда он вырос, проверяем варианты
    best = {}
    for s, c, t, v in readings:
        cfg = OBS_CITIES.get(c)
        if not cfg:
            continue
        loc_day = t.astimezone(ZoneInfo(cfg["tz"])).date()
        if loc_day != now.astimezone(ZoneInfo(cfg["tz"])).date():
            continue
        if s == "hko":
            mv = v  # официальный максимум с полуночи — сам итог маркета, без округления: мёртв вариант с верхней границей ниже
        elif not any(min((t.minute - m) % 60, (m - t.minute) % 60) <= WINDOW_BY_SOURCE.get(s, WINDOW) for m in mins.get(c, [0, 30])):
            continue
        else:
            mv = metar_value(v - ROUND_MARGIN.get(s, 0.0), cfg["unit"])
        if c not in best or mv > best[c][0]:
            best[c] = (mv, t, s)
    from weather_obs_live import bet_dead_buckets, ensure_schema
    main_conn.row_factory = sqlite3.Row
    ensure_schema(main_conn)
    events_cache, n_new = {}, 0
    for c, (mv, t, s) in best.items():
        cfg = OBS_CITIES[c]
        day = t.astimezone(ZoneInfo(cfg["tz"])).date()
        prev = db.execute("SELECT max_v FROM fast_state WHERE city = ? AND day = ?", (c, day.isoformat())).fetchone()
        if prev and prev[0] >= mv:
            continue
        with db:
            db.execute("INSERT OR REPLACE INTO fast_state VALUES (?, ?, ?)", (c, day.isoformat(), mv))
        n_new += 1
        with item_guard(f"obs_fast {c}", main_conn):
            bet_dead_buckets(main_conn, WALLET, c, cfg, day, mv, t, now, events_cache,
                             f"быстрый замер ({s}) в минуту сводки уже {mv} ({t:%H:%M}Z)")
    print(f"замеров {len(readings)} ({', '.join(f'{n} {sum(r[0] == n for r in readings)}' for n in ('synoptic', 'jma', 'dwd', 'fmi', 'hko', 'metar', 'knmi'))}); "
          f"новый максимум дня в {n_new} городах", flush=True)
    db.close()
    from jobmark import mark
    mark(main_conn, "weather_fastobs")
    main_conn.close()


if __name__ == "__main__":
    main()
