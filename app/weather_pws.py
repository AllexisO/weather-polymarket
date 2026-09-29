"""
Народные станции вокруг аэропорта (2026-09-29, идея коллеги Alex: «датчики в радиусе 20 км»). Источник — Synoptic
Weather API (CWOP и местные сети; только США). Пробный доступ Alex на 14 дней с 29.09 отдаёт лишь последние 7 дней —
поэтому копим: запуск раз в неделю (29.09, ~06.10, ~12.10 — заметки), каждый раз забираем прошедшие 7 дней.
Храним только утро (05:00-09:00 местного) — для признака к решению в 08:00. Аэропорт (сама станция METAR) исключён.
Кэш: /data/research/pws/<город>.json {дата: {станция: [(час_местный, °C), ...]}}. Ключ — SYNOPTIC_TOKEN в .env.
Запуск: docker compose run --rm -e JOB_TIMEOUT=0 --entrypoint python collector weather_pws.py
"""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from weather_cities import OBS_CITIES

API = "https://api.synopticdata.com/v2/stations/timeseries"
OUT = Path("/data/research/pws")
RADIUS_KM = 20


def us_cities():
    return {c: v for c, v in OBS_CITIES.items() if v["icao"].startswith("K")}


def fetch(city, cfg, token):
    tz = ZoneInfo(cfg["tz"])
    r = requests.get(API, params={"token": token, "radius": f"{cfg['icao']},{RADIUS_KM * 0.621:.1f}", "recent": 10080,
                                  "vars": "air_temp", "units": "metric", "limit": 500}, timeout=120)
    r.raise_for_status()
    d = r.json()
    if d["SUMMARY"].get("RESPONSE_CODE") != 1:
        raise RuntimeError(d["SUMMARY"].get("RESPONSE_MESSAGE"))
    path = OUT / f"{city}.json"
    cache = json.loads(path.read_text()) if path.exists() else {}
    n = 0
    for s in d.get("STATION") or []:
        if s["STID"].upper() in (cfg["icao"], cfg["icao"][1:]):
            continue
        ob = s["OBSERVATIONS"]
        for t, v in zip(ob.get("date_time", []), ob.get("air_temp_set_1") or []):
            if v is None:
                continue
            lt = datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(tz)
            h = lt.hour + lt.minute / 60
            if 5 <= h < 9:
                day = cache.setdefault(lt.date().isoformat(), {}).setdefault(s["STID"], [])
                if [round(h, 3), v] not in day:
                    day.append([round(h, 3), v])
                    n += 1
    path.write_text(json.dumps(cache))
    return len(d.get("STATION") or []), n, len(cache)


def main():
    token = os.environ.get("SYNOPTIC_TOKEN")
    if not token:
        raise SystemExit("нет SYNOPTIC_TOKEN в .env")
    OUT.mkdir(parents=True, exist_ok=True)
    for city, cfg in us_cities().items():
        try:
            st, n, days = fetch(city, cfg, token)
            print(f"{city}: станций {st}, новых утренних замеров {n}, дней в кэше {days}", flush=True)
        except Exception as e:  # noqa: BLE001 — один город не роняет загрузку
            print(f"{city}: ошибка — {e}", flush=True)
        time.sleep(2)
    print(f"готово {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")


if __name__ == "__main__":
    main()
