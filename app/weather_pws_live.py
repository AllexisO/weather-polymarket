"""
Бесплатный сбор народных метеостанций CWOP вокруг аэропортов США (2026-09-29, решение Alex: копить свою историю, пока
идёт проверка по пробному Synoptic; если 12.10 польза подтвердится — этого хватит для обучения через несколько месяцев).

CWOP — любительская сеть: станции сами шлют замеры в открытую сеть APRS (cwop.aprs.net), подключение на чтение
бесплатное и без регистрации (логин без позывного, pass -1). Истории у сети нет — только то, что услышали.
Слушаем LISTEN_MIN минут (крон каждый час 11:00-19:00 по Кишинёву = утро в США), берём погодные пакеты станций
в RADIUS_KM от аэропорта (станция Polymarket), пишем в отдельную базу data/db/pws.sqlite3 (рабочую не трогаем).
На модель и кошельки пока не влияет. Запуск: docker compose run --rm -e JOB_TIMEOUT=3600 collector weather_pws_live.py
"""
import math
import os
import re
import socket
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from weather_cities import OBS_CITIES
from weather_edge import CITIES

PWS_DB = Path(os.environ.get("PWS_DB", "/data/db/pws.sqlite3"))
SERVERS = (("cwop.aprs.net", 14580), ("rotate.aprs2.net", 14580))
RADIUS_KM = 20
LISTEN_MIN = float(os.environ.get("LISTEN_MIN", "55"))
POS = re.compile(r"(\d{2})(\d{2}\.\d{2})([NS]).(\d{3})(\d{2}\.\d{2})([EW])_")
TEMP = re.compile(r"t(-?\d{1,3})")


def us_airports():
    return {c: (CITIES[c]["lat"], CITIES[c]["lon"]) for c, v in OBS_CITIES.items() if v["icao"].startswith("K")}


def km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def parse(line, airports):
    """Погодный пакет с координатами → (станция, город, °C, lat, lon) или None."""
    if line.startswith("#") or ":" not in line or ">" not in line:
        return None
    call, body = line.split(">", 1)[0], line.split(":", 1)[1]
    m = POS.search(body)
    if not m:
        return None
    lat = (int(m[1]) + float(m[2]) / 60) * (1 if m[3] == "N" else -1)
    lon = (int(m[4]) + float(m[5]) / 60) * (1 if m[6] == "E" else -1)
    rest = body[m.end():]
    t = TEMP.search(rest)
    if not t:
        return None
    temp_c = (int(t[1]) - 32) * 5 / 9
    if not -40 <= temp_c <= 55:
        return None
    city, d = min(((c, km((lat, lon), p)) for c, p in airports.items()), key=lambda x: x[1])
    if d > RADIUS_KM:
        return None
    return call, city, round(temp_c, 2), round(lat, 4), round(lon, 4)


def connect(airports):
    flt = " ".join(f"r/{la:.3f}/{lo:.3f}/{RADIUS_KM}" for la, lo in airports.values())
    for host, port in SERVERS:
        try:
            s = socket.create_connection((host, port), timeout=60)
            s.sendall(f"user WXLAB1 pass -1 vers weatherlab-research 0.1 filter {flt}\r\n".encode())
            return s, host
        except OSError as e:
            print(f"{host}: не подключились — {e}", flush=True)
    raise SystemExit("сеть APRS недоступна")


def main():
    airports = us_airports()
    db = sqlite3.connect(PWS_DB, timeout=30)
    db.execute("""CREATE TABLE IF NOT EXISTS cwop_obs (station TEXT, city TEXT, ts_utc TEXT, temp_c REAL, lat REAL, lon REAL,
                  PRIMARY KEY (station, ts_utc))""")
    db.commit()
    s, host = connect(airports)
    f = s.makefile("r", encoding="latin-1", errors="replace")
    end, buf, n, last_flush = time.time() + LISTEN_MIN * 60, [], 0, time.time()
    while time.time() < end:
        try:
            line = f.readline()
        except (TimeoutError, OSError):
            s.close()
            s, host = connect(airports)
            f = s.makefile("r", encoding="latin-1", errors="replace")
            continue
        if not line:
            s.close()
            time.sleep(10)
            s, host = connect(airports)
            f = s.makefile("r", encoding="latin-1", errors="replace")
            continue
        p = parse(line.strip(), airports)
        if p:
            buf.append((p[0], p[1], datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), *p[2:]))
        if buf and time.time() - last_flush > 60:
            db.executemany("INSERT OR IGNORE INTO cwop_obs VALUES (?, ?, ?, ?, ?, ?)", buf)
            db.commit()
            n += len(buf)
            buf, last_flush = [], time.time()
    if buf:
        db.executemany("INSERT OR IGNORE INTO cwop_obs VALUES (?, ?, ?, ?, ?, ?)", buf)
        db.commit()
        n += len(buf)
    s.close()
    by = dict(db.execute("""SELECT city, COUNT(DISTINCT station) FROM cwop_obs WHERE ts_utc >= datetime('now', '-1 hour')
                            GROUP BY city""").fetchall())
    db.close()
    print(f"{host}: за {LISTEN_MIN:.0f} мин замеров {n}; станций за час: " + ", ".join(f"{c} {k}" for c, k in sorted(by.items())), flush=True)
    from jobmark import mark
    c = sqlite3.connect(os.environ.get("POLY_LAB_DB", "/data/db/polymarket_lab.sqlite3"), timeout=60)
    mark(c, "weather_pws_live")
    c.close()


if __name__ == "__main__":
    main()
