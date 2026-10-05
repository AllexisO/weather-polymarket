"""
Частные станции Netatmo рядом с аэропортами — сбор для проверки «видно ли сводку METAR раньше» (2026-10-01, Alex
зарегистрировал приложение на dev.netatmo.com; ключи NETATMO_* в .env).

Раз в 5 минут для городов, где в радиусе RADIUS_KM от станции аэропорта есть хотя бы MIN_STATIONS станций (первый
снимок 01.10: Париж ~800, Мадрид ~220, Лондон ~140, Веллингтон, Сиэтл, Сан-Франциско, Торонто — 10-25), запрос
getpublicdata (фильтр Netatmo от явных ошибок включён) и запись каждого замера: станция, координаты, время замера,
температура, когда мы его увидели. База — data/db/netatmo.sqlite3 (WAL). Ставок нет — только данные; разбор после 1-2
суток: медиана станций против METAR той же минуты, опережение нового максимума дня.
Токен доступа живёт 3 ч; обновляется по refresh-токену, а Netatmo при обновлении выдаёт НОВЫЙ refresh-токен — храним его в
data/db/netatmo_token.json (в .env — только самый первый). Предел Netatmo — 500 запросов в час: 12 запусков × ≤ 20 городов.
Крон: каждые 5 минут. Запуск: docker compose run --rm collector weather_netatmo.py
"""
import json
import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from jobmark import item_guard
from weather_cities import OBS_CITIES

MAIN_DB = Path(os.environ.get("POLY_LAB_DB", "/data/db/polymarket_lab.sqlite3"))
NETATMO_DB = MAIN_DB.parent / "netatmo.sqlite3"
TOKEN_FILE = MAIN_DB.parent / "netatmo_token.json"
API = "https://api.netatmo.com"
RADIUS_KM = 5.0
MIN_STATIONS = 5
MAX_CITIES = 20
CITIES_FILE = MAIN_DB.parent / "netatmo_cities.json"   # города с достаточным числом станций (пересчёт раз в сутки)


def db_conn():
    db = sqlite3.connect(NETATMO_DB, timeout=60)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS pws_obs (city TEXT, station TEXT, lat REAL, lon REAL, obs_utc TEXT, temp_c REAL,
                  dist_km REAL, first_seen_utc TEXT, PRIMARY KEY (station, obs_utc))""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_pws_city_time ON pws_obs(city, obs_utc)")
    db.commit()
    return db


def access_token():
    """Действующий токен: из файла, если не истёк; иначе обновляем по последнему refresh-токену и сохраняем новый."""
    st = {}
    try:
        st = json.loads(TOKEN_FILE.read_text())
    except (OSError, ValueError):
        pass
    if st.get("access") and st.get("expires", 0) > time.time() + 120:
        return st["access"]
    refresh = st.get("refresh") or os.environ["NETATMO_REFRESH_TOKEN"]
    # 02.10: сервер токенов тоже отвечает 503 (01.10 12:40 весь запуск упал) — повторы; не ответил — пропуск запуска
    for wait in (3, 6, 10, None):
        try:
            r = requests.post(f"{API}/oauth2/token", data={"grant_type": "refresh_token", "refresh_token": refresh,
                                                           "client_id": os.environ["NETATMO_CLIENT_ID"],
                                                           "client_secret": os.environ["NETATMO_CLIENT_SECRET"]}, timeout=30)
            if r.status_code < 500:
                break
        except (requests.Timeout, requests.ConnectionError):
            r = None
        if wait is None:
            print("сервер токенов Netatmo не ответил после 3 повторов — пропуск запуска (следующий через 5 мин)")
            return None
        time.sleep(wait)
    r.raise_for_status()
    d = r.json()
    st = {"access": d["access_token"], "refresh": d.get("refresh_token", refresh), "expires": time.time() + int(d.get("expires_in", 10800))}
    TOKEN_FILE.write_text(json.dumps(st))
    os.chmod(TOKEN_FILE, 0o600)
    return st["access"]


def box(lat, lon, km):
    dlat = km / 111.0
    dlon = km / (111.0 * math.cos(math.radians(lat)))
    return {"lat_ne": lat + dlat, "lon_ne": lon + dlon, "lat_sw": lat - dlat, "lon_sw": lon - dlon}


def dist_km(a, b, c, d):
    return math.hypot((a - c) * 111.0, (b - d) * 111.0 * math.cos(math.radians(a)))


def fetch(tok, city):
    cfg = OBS_CITIES[city]
    # 01.10: getpublicdata отвечает 500/503 примерно на 1 запрос из 10 (любой город, далеко от предела 500/ч) — сбой их
    # сервиса. До 3 повторов; не ответил — тихий пропуск этого 5-мин замера (для сбора данных не важен), не ошибка запуска.
    for wait in (3, 6, 10, None):
        try:
            r = requests.get(f"{API}/api/getpublicdata", headers={"Authorization": f"Bearer {tok}"},
                             params={**box(cfg["lat"], cfg["lon"], RADIUS_KM), "required_data": "temperature", "filter": "true"}, timeout=30)
            code = r.status_code
        except (requests.Timeout, requests.ConnectionError) as e:   # 03.10: обрыв соединения — так же, как 5xx
            r, code = None, type(e).__name__
        if r is not None and r.status_code < 500:
            break
        if wait is None:
            print(f"Netatmo не ответил по {city} ({code}) после 3 повторов — пропуск замера")
            return None   # сбой, а не «нет станций» — см. cities()
        time.sleep(wait)
    r.raise_for_status()
    out = []
    for s in r.json().get("body", []):
        lon, lat = (s.get("place") or {}).get("location", [None, None])
        if lat is None:
            continue
        dk = dist_km(cfg["lat"], cfg["lon"], lat, lon)
        if dk > RADIUS_KM:
            continue
        for m in (s.get("measures") or {}).values():
            if "res" not in m or "temperature" not in (m.get("type") or []):
                continue
            k = m["type"].index("temperature")
            for ts, vals in m["res"].items():
                if vals[k] is not None:
                    out.append((city, s["_id"], lat, lon, datetime.fromtimestamp(int(ts), timezone.utc).isoformat(), float(vals[k]), round(dk, 2)))
    return out


def cities(tok):
    """Список городов с ≥ MIN_STATIONS станций — пересчитываем раз в сутки (иначе 48 запросов каждые 5 мин)."""
    try:
        d = json.loads(CITIES_FILE.read_text())
        if time.time() - d["at"] < 86400:
            return d["cities"]
    except (OSError, ValueError, KeyError):
        pass
    try:
        prev = json.loads(CITIES_FILE.read_text()).get("cities", [])
    except (OSError, ValueError):
        prev = []
    cnt, failed = {}, []
    for c in OBS_CITIES:
        try:
            rows = fetch(tok, c)
        except requests.RequestException:
            rows = None
        if rows is None:   # 02.10: сбой Netatmo при пересчёте не должен выкидывать город на сутки
            failed.append(c)
        else:
            cnt[c] = len({x[1] for x in rows})
        time.sleep(0.5)
    keep = [c for c, n in sorted(cnt.items(), key=lambda x: -x[1]) if n >= MIN_STATIONS][:MAX_CITIES]
    keep += [c for c in prev if c in failed and c not in keep]   # не ответил — оставляем, как было
    # были сбои — пересчитать снова через час, а не через сутки
    CITIES_FILE.write_text(json.dumps({"at": time.time() - (86400 - 3600 if failed else 0), "cities": keep, "counts": cnt, "failed": failed}))
    print("пересчёт городов: " + ", ".join(f"{c} {cnt[c]}" for c in keep))
    return keep


def main():
    from jobmark import single_instance
    single_instance("netatmo")
    db = db_conn()
    main_conn = sqlite3.connect(MAIN_DB, timeout=60)
    tok = access_token()
    if tok is None:
        return
    now = datetime.now(timezone.utc).isoformat()
    n = 0
    for c in cities(tok):
        with item_guard(f"netatmo {c}", main_conn):
            rows = fetch(tok, c) or []
            with db:
                db.executemany("INSERT OR IGNORE INTO pws_obs VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [r + (now,) for r in rows])
            n += len(rows)
        time.sleep(0.3)
    print(f"замеров Netatmo: {n}")
    from jobmark import mark
    mark(main_conn, "weather_netatmo")


if __name__ == "__main__":
    main()
