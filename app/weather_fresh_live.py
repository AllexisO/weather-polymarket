"""
Живая запись свежего прогона ECMWF к 08:00 (28.10, Alex «да»; разбор v7 — docs/RESEARCH_JOURNAL.md 28.10).

v7 (слой 3) сдвигала v5_31 на 0.128·Δ, Δ = свежий ECMWF − вчерашний. В 06-09 рынок недодвигал цену на Δ (наклон +0.11…+0.16),
в 10 — почти нет (+0.04): постоянный k «съеден». Следующая проверка — k по последним 30 дням, на ноябрьских днях.
Здесь только запись, ставок нет.

Тот же прогон, что в исследовании (weather_fresh_fc.run_for): R(d) — последний из 00/06/12/18Z с запуском + 7 ч ≤ 08:00 местного d.
fresh(d) — максимум дня d по R(d), stale(d+1) — максимум дня d+1 по нему же (то, что будет «вчерашним» завтра).
Один запрос на город-день (+ один раз stale на сегодня — прогон R(d−1)). Каждый час с 07:00 местного, пока не получим;
fetched_utc — когда получили: позже 08:00 местного — прогон к решению не успел бы (для живого кошелька — смотреть).
Запуск: крон каждый час в :47. docker compose run --rm collector weather_fresh_live.py
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from jobmark import item_guard, mark
from weather_cities import OBS_CITIES
from weather_fresh_fc import API, run_for

DB_PATH = os.environ.get("POLY_LAB_DB", "/data/db/polymarket_lab.sqlite3")
FROM_H, TO_H = 7, 14         # окно попыток, местное время


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS fresh_hres_live (city TEXT, local_date TEXT, kind TEXT, run_utc TEXT, max_c REAL,
                    fetched_utc TEXT, PRIMARY KEY (city, local_date, kind))""")


class LimitHit(BaseException):   # не Exception: item_guard не должен её глотать — лимит останавливает весь запуск
    pass


def fetch(cfg, run):
    """Максимумы по дням местного времени из прогона run; {} — прогона ещё нет."""
    r = requests.get(API, params={"latitude": cfg["lat"], "longitude": cfg["lon"], "run": run.strftime("%Y-%m-%dT%H:%M"),
                                  "hourly": "temperature_2m", "models": "ecmwf_ifs", "timezone": cfg["tz"],
                                  "forecast_days": 3}, timeout=30)
    if r.status_code == 429 or "limit" in r.text.lower()[:300]:
        raise LimitHit(f"{r.status_code}: {r.text[:200]}")
    if r.status_code == 400 and "not available" in r.text:
        return {}
    r.raise_for_status()
    by_day = {}
    h = r.json()["hourly"]
    for t, v in zip(h["time"], h["temperature_2m"]):
        if v is not None:
            by_day.setdefault(t[:10], []).append(v)
    return {d: max(v) for d, v in by_day.items() if len(v) >= 20}


def main():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    ensure_schema(conn)
    have = {(c, d, k) for c, d, k in conn.execute("SELECT city, local_date, kind FROM fresh_hres_live")}
    now = datetime.now(timezone.utc)
    calls = got = waiting = 0
    try:
        for city, cfg in OBS_CITIES.items():
            local = now.astimezone(ZoneInfo(cfg["tz"]))
            d = local.date()
            if not (FROM_H <= local.hour < TO_H):
                continue
            with item_guard(city, conn):
                todo = []
                if (city, d.isoformat(), "fresh") not in have:
                    todo.append((d, ((d, "fresh"), (d + timedelta(days=1), "stale"))))
                if (city, d.isoformat(), "stale") not in have:
                    todo.append((d - timedelta(days=1), ((d, "stale"),)))   # первый день записи: вчерашний прогон на сегодня
                for rd, want in todo:
                    run = run_for(cfg["tz"], rd)
                    mx = fetch(cfg, run)
                    calls += 1
                    rows = [(city, day.isoformat(), kind, run.isoformat(), mx[day.isoformat()], now.isoformat(timespec="seconds"))
                            for day, kind in want if day.isoformat() in mx]
                    if not rows:
                        waiting += 1
                        continue
                    conn.executemany("INSERT OR IGNORE INTO fresh_hres_live VALUES (?, ?, ?, ?, ?, ?)", rows)
                    conn.commit()
                    got += len(rows)
    except LimitHit as e:
        print(f"СТОП: Open-Meteo — лимит ({e}); запросов {calls}", flush=True)
    print(f"fresh_live: запросов {calls}, записано {got}, прогон ещё не вышел {waiting}", flush=True)
    mark(conn, "weather_fresh_live")
    conn.close()


if __name__ == "__main__":
    main()
