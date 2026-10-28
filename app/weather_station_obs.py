"""
Реальные показания метеостанций — "факт" для погоды вместо модельной
оценки Open-Meteo (weather_resolve.py).

2026-09-22: выяснилось, что Open-Meteo "факт" (сетка модели, максимум по
часовым значениям) совпадал с тем, как Polymarket реально закрыл маркет,
только в 13-65% дней (см. CLAUDE.md). Polymarket резолвит по NOAA —
"highest reading under the Temp column" по станции аэропорта, а это
METAR-сводки. Берём те же METAR из архива Iowa Environmental Mesonet
(IEM, бесплатно, без ключа, с историей):
https://mesonet.agron.iastate.edu/request/download.phtml

Пишем две таблицы:
- station_obs — все сводки как есть (температура, ветер, облачность):
  сырьё для будущих поправок по ветру/облачности, история не
  перезаписывается;
- weather_station_daily — дневной максимум по местной дате в единицах
  маркета. Та же схема колонок, что у weather_outcomes, чтобы
  weather_bias.py/дашборд просто переключились на новую таблицу.

У IEM жёсткий лимит частоты ("Too many requests") — пауза между
станциями и повтор с ожиданием.
"""

from jobmark import item_guard
import csv
import io
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from weather_cities import OBS_CITIES, history_iem


def whole_f(tmpf):
    """Максимум °F-города как в итоге Polymarket — целые °F (15.10, проверка проекта). IEM отдаёт tmpf с десятыми из группы T
    METAR (десятые °C): KBKF 13.10 18.6 °C = 65.48 → было 65.5 (вариант «66-67»), итог — 65 («64-65»); Денвер — 25 дробных
    дней из 44. Обратно в десятые °C, затем в °F до целого (половина — вверх): все 18 дней Денвера с 25.09 совпали с итогом."""
    return math.floor(round((tmpf - 32) * 5 / 9, 1) * 9 / 5 + 32 + 0.5 + 1e-9)

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
IEM_ASOS = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
IEM_DELAY_S = 6.0

# Станции — из weather_cities.OBS_CITIES (26 городов с 2026-09-23; до
# этого — свой список на 8 станций). Токио/Сеул — те же коды, что были.
# 2026-09-23: с июня (было с 20 августа) — нужна длинная история для весов
# микса моделей (weather_multimodel.py).
HISTORY_START = datetime(2026, 6, 1, tzinfo=timezone.utc)


def fetch_obs(station, start, end, report_type=(3, 4)):
    params = {
        "station": station,
        "data": ["tmpf", "drct", "sknt", "skyc1", "dwpf", "alti"],  # dwpf/alti — с 2026-09-25, для weather_ml
        "year1": start.year, "month1": start.month, "day1": start.day,
        "year2": end.year, "month2": end.month, "day2": end.day,
        "tz": "Etc/UTC", "format": "onlycomma", "latlon": "no", "missing": "M",
        "report_type": list(report_type),  # 3 — плановые сводки, 4 — внеочередные (SPECI)
    }
    for attempt in range(4):
        r = requests.get(IEM_ASOS, params=params, timeout=60)
        if r.status_code == 429 or "Too many requests" in r.text[:200]:
            time.sleep(IEM_DELAY_S * (attempt + 2))
            continue
        r.raise_for_status()
        return list(csv.DictReader(io.StringIO(r.text)))
    raise requests.RequestException(f"{station}: IEM не отвечает (лимит запросов)")


# 28.10 (полная проверка, Alex «SPECI — решай сам»): с маркетов на 23.10 итог Polymarket — только плановые сводки METAR, SPECI не
# считается (weather_obs_live.ROUTINE_FROM). Факт weather_station_daily считал все сводки — с 23.10 расходился с итогом (Денвер 24.10,
# Сан-Франциско 25.10), модели учились на неверном ответе. Тип сводки — IEM (report_type=3) ИЛИ плановая минута города (mark_types):
# только минута ошибается у Мехико (плановые в :40-:47), только IEM — у получасовых станций. report_type: 3 — плановая, 4 — SPECI,
# NULL — тип не известен (до 22.10 или IEM не ответил — такие сводки идут в факт, как раньше). Проверка data/research/_fact_routine_1028.py.
from weather_obs_live import ROUTINE_FROM


_ROUTINE_MIN = {}


def routine_minutes(city):
    """Минуты плановых сводок города (mm_metar_minutes.json — как weather_obs_live.routine_only)."""
    if not _ROUTINE_MIN:
        import json
        try:
            _ROUTINE_MIN.update(json.loads((DB_PATH.parent / "mm_metar_minutes.json").read_text()))
        except (OSError, ValueError):
            pass
    return set(_ROUTINE_MIN.get(city, []))


def mark_types(conn, station, start, end, city):
    """Пометить тип сводок станции за [start, end]: SPECI (4) — только если IEM не считает её плановой (report_type=3) И её минута
    не из плановых минут города. 28.10: IEM относит к типу 3 лишь ежечасные сводки — получасовые METAR (Лондон :20/:50, Токио :30)
    у него тип 4, а Polymarket их считает; у Мехико плановые в :40-:47 — по минуте их не узнать, а IEM даёт им тип 3."""
    a = max(start, datetime.fromisoformat(ROUTINE_FROM).replace(tzinfo=timezone.utc) - timedelta(days=1))
    if a >= end:
        return 0
    routine = {r["valid"] for r in fetch_obs(station, a, end, (3,)) if r.get("valid")}
    lo, hi = a.strftime("%Y-%m-%d %H:%M"), end.strftime("%Y-%m-%d %H:%M")
    mins = routine_minutes(city)
    rows = conn.execute("SELECT valid_utc FROM station_obs WHERE station = ? AND valid_utc BETWEEN ? AND ?", (station, lo, hi)).fetchall()
    rt = {v: 3 if (v in routine or int(v[14:16]) in mins) else 4 for (v,) in rows}
    conn.executemany("UPDATE station_obs SET report_type = ? WHERE station = ? AND valid_utc = ?", [(t, station, v) for v, t in rt.items()])
    conn.commit()
    return sum(t == 4 for t in rt.values())


def num(v):
    return None if v in (None, "", "M") else float(v)


def ensure_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS station_obs (
            city TEXT NOT NULL,
            station TEXT NOT NULL,
            valid_utc TEXT NOT NULL,
            tmpf REAL,
            drct REAL,
            sknt REAL,
            skyc1 TEXT,
            PRIMARY KEY (station, valid_utc)
        )
        """
    )
    cols = [r[1] for r in conn.execute("PRAGMA table_info(station_obs)")]
    if "report_type" not in cols:
        conn.execute("ALTER TABLE station_obs ADD COLUMN report_type INTEGER")   # 28.10: 3 — плановая, 4 — SPECI, NULL — не известно
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS weather_station_daily (
            city TEXT NOT NULL,
            local_date TEXT NOT NULL,
            unit TEXT,
            actual_max REAL,
            n_obs INTEGER,
            resolved_at TEXT,
            PRIMARY KEY (city, local_date)
        )
        """
    )
    conn.commit()


def run(types_from=None):
    """types_from — разовая догрузка типа сводок (плановая / SPECI) с этой даты по всем станциям, без новых сводок."""
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    now = datetime.now(timezone.utc)
    for i, (city, cfg) in enumerate(OBS_CITIES.items()):
        with item_guard(city, conn):
            station = history_iem(city)   # 22.10: Денвер — сразу KDEN (weather_cities.STATION_FROM)
            first, last = conn.execute(
                "SELECT MIN(valid_utc), MAX(valid_utc) FROM station_obs WHERE station = ?", (station,)
            ).fetchone()
            ranges = []
            if first and datetime.fromisoformat(first).replace(tzinfo=timezone.utc) > HISTORY_START + timedelta(days=1):
                ranges.append((HISTORY_START, datetime.fromisoformat(first).replace(tzinfo=timezone.utc) + timedelta(days=1)))
            start = datetime.fromisoformat(last).replace(tzinfo=timezone.utc) - timedelta(days=2) if last else HISTORY_START
            ranges.append((start, now + timedelta(days=1)))
            if types_from:
                ranges = [(datetime.fromisoformat(types_from).replace(tzinfo=timezone.utc), now + timedelta(days=1))]
            rows = []
            try:
                for j, (a, b) in enumerate(ranges):
                    if i or j:
                        time.sleep(IEM_DELAY_S)
                    if not types_from:
                        rows += fetch_obs(station, a, b)
            except requests.RequestException as e:
                print(f"{city}: ошибка — {e}", file=sys.stderr)
                continue
            conn.executemany(
                "INSERT OR IGNORE INTO station_obs (city, station, valid_utc, tmpf, drct, sknt, skyc1, dwpf, alti) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(city, station, r["valid"], num(r["tmpf"]), num(r["drct"]), num(r["sknt"]), r.get("skyc1"),
                  num(r.get("dwpf")), num(r.get("alti")))
                 for r in rows if r.get("valid")],
            )
            conn.commit()
            try:
                speci = 0
                for a, b in ranges:
                    time.sleep(IEM_DELAY_S)
                    speci += mark_types(conn, station, a, b, city)
            except requests.RequestException as e:
                print(f"{city}: тип сводок не получен — {e} (такие сводки пока идут в факт)", file=sys.stderr)
                speci = None

            # Дневной максимум по МЕСТНОЙ дате, только по полностью закончившимся дням.
            tz = ZoneInfo(cfg["tz"])
            today_local = datetime.now(tz).date().isoformat()
            by_date = {}
            for r in conn.execute("SELECT valid_utc, tmpf, report_type FROM station_obs WHERE station = ? AND tmpf IS NOT NULL", (station,)):
                d = datetime.fromisoformat(r["valid_utc"]).replace(tzinfo=timezone.utc).astimezone(tz).date().isoformat()
                if d >= ROUTINE_FROM and r["report_type"] == 4:
                    continue   # SPECI — с 23.10 не в итоге Polymarket
                if d < today_local:
                    by_date.setdefault(d, []).append(r["tmpf"])
            daily = []
            for d, vals in by_date.items():
                mx = max(vals)
                if cfg["unit"] == "celsius":
                    # METAR вне США — целые °C, IEM отдаёт их переведёнными в °F;
                    # переводим обратно и округляем до исходного целого.
                    mx = round((mx - 32) * 5 / 9)
                else:
                    mx = whole_f(mx)
                daily.append((city, d, cfg["unit"], mx, len(vals), now.isoformat()))
            conn.executemany(
                "INSERT OR REPLACE INTO weather_station_daily (city, local_date, unit, actual_max, n_obs, resolved_at) VALUES (?, ?, ?, ?, ?, ?)",
                daily,
            )
            conn.commit()
            print(f"{city} ({station}): {len(rows)} сводок, {len(daily)} дней, SPECI помечено {speci}")
    conn.close()


if __name__ == "__main__":
    run(sys.argv[sys.argv.index("--types-from") + 1] if "--types-from" in sys.argv else None)
