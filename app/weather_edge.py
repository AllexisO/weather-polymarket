"""
Сбор диагностики: расхождение между ансамблевым прогнозом погоды (GFS +
ICON, Open-Meteo, бесплатно, см. ENSEMBLE_MODELS) и ценой дневного
маркета Polymarket "Highest temperature in <город>". Прогноз по каждому
городу дополнительно сдвигается на его историческое смещение (см.
weather_bias.py) — MOS-поправка по уже накопленным резолвленным дням,
только если их набралось достаточно (MIN_BIAS_N). Ничего не торгует —
только считает и пишет edge в sqlite, чтобы честно понять, есть ли
расхождение вообще, и если да — систематическое оно или шум момента.

Запускается по крону раз в несколько часов (см. README). Каждый запуск —
один снимок по всем городам из CITIES, добавляет строки в таблицу
snapshots (не upsert — история снимков копится специально, чтобы потом
видеть, как расхождение менялось в течение дня).
"""

import json
import math
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from weather_bias import compute_city_bias, compute_emos_params

# Локально (без Docker) — файл рядом с проектом. В контейнере путь
# приходит через переменную окружения (см. docker-compose.yml), чтобы не
# зависеть от того, куда COPY положил app/ внутри образа.
DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))

# lat/lon — координаты СТАНЦИИ, по которой Polymarket реально резолвит
# маркет (см. описание маркета через Gamma API — там прямо названа станция
# NOAA/обсерватории), а НЕ центра города. Обнаружено 2026-08-25: до этого
# тут стояли координаты центра города, что давало систематический перекос
# модели против рынка на всех городах (особенно Сеул — Polymarket резолвит
# по аэропорту Incheon, это ~50км от центра Сеула). Данные, собранные до
# фикса координат, сравнивали модель не с той точкой на карте — считать их
# частью честной калибровки модели нельзя, это была ошибка в сборе, а не
# находка про качество прогноза.
# tz — таймзона для расчёта "сегодняшнего" дневного максимума (по времени
# города, не станции — они в одном поясе везде здесь), poly_slug —
# сегмент URL Polymarket (highest-temperature-in-{poly_slug}-on-{month}-{day}-{year}).
CITIES = {
    # unit — единица, в которой Polymarket задаёт бакеты ДЛЯ ЭТОГО города:
    # US-города — Fahrenheit с шагом 2°, остальные — Celsius с шагом 1°.
    # Ансамбль запрашиваем сразу в той же единице, чтобы не путать конвертацию.
    "nyc":       {"lat": 40.7769, "lon": -73.8740, "tz": "America/New_York", "poly_slug": "nyc",      "unit": "fahrenheit"},  # LaGuardia (KLGA)
    "paris":     {"lat": 48.9694, "lon": 2.4414,   "tz": "Europe/Paris",     "poly_slug": "paris",    "unit": "celsius"},     # Paris-Le Bourget (LFPB)
    "london":    {"lat": 51.5048, "lon": 0.0495,   "tz": "Europe/London",    "poly_slug": "london",   "unit": "celsius"},     # London City Airport (EGLC)
    "tokyo":     {"lat": 35.5494, "lon": 139.7798, "tz": "Asia/Tokyo",       "poly_slug": "tokyo",    "unit": "celsius"},     # Haneda (RJTT)
    "seoul":     {"lat": 37.4602, "lon": 126.4407, "tz": "Asia/Seoul",       "poly_slug": "seoul",    "unit": "celsius"},     # Incheon Intl (RKSI)
    "hong_kong": {"lat": 22.3020, "lon": 114.1740, "tz": "Asia/Hong_Kong",   "poly_slug": "hong-kong", "unit": "celsius"},    # Hong Kong Observatory
    "beijing":   {"lat": 40.0799, "lon": 116.6031, "tz": "Asia/Shanghai",    "poly_slug": "beijing",  "unit": "celsius"},     # Beijing Capital Intl (ZBAA)
}

GAMMA = "https://gamma-api.polymarket.com"
OPEN_METEO_ENSEMBLE = "https://ensemble-api.open-meteo.com/v1/ensemble"

# 2026-08-31: раньше брали только GFS (NOAA). Добавили ICON (DWD, тоже
# бесплатно через Open-Meteo) — два независимых по происхождению
# ансамбля должны меньше страдать от локальных перекосов одной модели
# (см. BIAS_SINCE_TS ниже — именно такой перекос нашли в Гонконге/Токио/
# Нью-Йорке). Вероятность бакета считаем ОТДЕЛЬНО по каждой модели и
# усредняем — иначе ICON (40 членов) просто перевесил бы GFS (31 член)
# в общем пуле по числу членов, а не по качеству прогноза.
ENSEMBLE_MODELS = ["gfs_seamless", "icon_seamless"]

# 2026-09-03: Google выпустила WeatherNext 3, но она доступна только
# через Google Cloud (BigQuery/Earth Engine, allowlist) — не подходит
# для лёгкого sqlite+крон скрипта. WeatherNext 2 (предыдущая версия)
# уже бесплатно отдаётся через тот же Open-Meteo. Решили (2026-09-03)
# НЕ смешивать её с GFS+ICON — блендинг+поправка ещё не провалидированы
# сами по себе, добавлять третью переменную сейчас значит никогда не
# понять, что из изменений сработало. Вместо этого считаем и пишем её
# ОТДЕЛЬНОЙ колонкой (wn2_*), без поправки на смещение — сырое, ничем
# не тронутое сравнение с рынком, которое можно смотреть параллельно с
# основным прогнозом на /calibration и решить позже, стоит ли сливать.
WEATHERNEXT2_MODEL = "google_weathernext2_ensemble"

# Та же отметка, что WEATHER_COORD_FIX_TS в dashboard.py: данные до фикса
# координат сравнивали модель не с той точкой на карте, для расчёта
# поправки на смещение их использовать нельзя.
BIAS_SINCE_TS = "2026-08-25T19:58:27+00:00"

# Разбор диапазона из текста вопроса маркета. У Polymarket две разные схемы
# бакетов в одном и том же продукте: US-города — "X or below" / "between X-Y"
# / "X or higher" с шагом 2°F; остальные — "be X" с шагом 1°C (без слова
# between), тогда бакет — X±0.5. Юнит (°F/°C) не разбираем регуляркой:
# он уже задан в CITIES[...]["unit"] и определяет, в чём запрошен ансамбль.
RE_BELOW = re.compile(r"(-?\d+)\s*°[CF] or below")
RE_RANGE = re.compile(r"between (-?\d+)-(-?\d+)\s*°[CF]")
RE_ABOVE = re.compile(r"(-?\d+)\s*°[CF] or higher")
RE_EXACT = re.compile(r"be (-?\d+)\s*°[CF] on")


def month_day_year_slug(dt_local):
    return dt_local.strftime("%B-%-d-%Y").lower()


def fetch_ensemble_daily_max(lat, lon, tz_name, unit, model):
    """Все члены + контроль одной модели ансамбля -> список дневных
    максимумов на СЕГОДНЯ по местному времени."""
    r = requests.get(
        OPEN_METEO_ENSEMBLE,
        params={
            "latitude": lat,
            "longitude": lon,
            "models": model,
            "hourly": "temperature_2m",
            "temperature_unit": unit,
            "timezone": tz_name,
            "forecast_days": 2,
        },
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()["hourly"]
    tz = ZoneInfo(tz_name)
    today_local = datetime.now(tz).date()

    member_cols = [k for k in data.keys() if k.startswith("temperature_2m")]
    times = data["time"]

    daily_max = []
    for col in member_cols:
        vals_today = [
            data[col][i]
            for i, t in enumerate(times)
            if datetime.fromisoformat(t).date() == today_local and data[col][i] is not None
        ]
        if vals_today:
            daily_max.append(max(vals_today))
    return daily_max


def parse_bucket(question):
    m = RE_BELOW.search(question)
    if m:
        return (-999.0, float(m.group(1)) + 0.5)
    m = RE_RANGE.search(question)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return (lo - 0.5, hi + 0.5)
    m = RE_ABOVE.search(question)
    if m:
        return (float(m.group(1)) - 0.5, 999.0)
    m = RE_EXACT.search(question)
    if m:
        v = float(m.group(1))
        return (v - 0.5, v + 0.5)
    return None


def fetch_polymarket_buckets(poly_slug, dt_local):
    slug = f"highest-temperature-in-{poly_slug}-on-{month_day_year_slug(dt_local)}"
    r = requests.get(f"{GAMMA}/events", params={"slug": slug}, timeout=20)
    r.raise_for_status()
    events = r.json()
    if not events:
        return None
    markets = events[0]["markets"]
    buckets = []
    for m in markets:
        rng = parse_bucket(m["question"])
        if rng is None:
            continue
        prices = json.loads(m["outcomePrices"])
        outcomes = json.loads(m["outcomes"])
        yes_price = float(prices[outcomes.index("Yes")])
        buckets.append({"lo": rng[0], "hi": rng[1], "market_p": yes_price, "question": m["question"]})
    return {"event_vol": events[0].get("volume", 0), "buckets": buckets}


def model_prob(ensemble, lo, hi):
    if not ensemble:
        return None
    hits = sum(1 for v in ensemble if lo < v <= hi)
    return hits / len(ensemble)


def blended_model_prob(ensembles_by_model, lo, hi, bias):
    """Вероятность бакета, усреднённая по моделям (равный вес каждой
    модели, не каждому члену — см. комментарий у ENSEMBLE_MODELS), с
    поправкой на историческое смещение конкретного города."""
    probs = []
    total_members = 0
    for members in ensembles_by_model.values():
        if not members:
            continue
        shifted = [v + bias for v in members]
        p = model_prob(shifted, lo, hi)
        if p is not None:
            probs.append(p)
            total_members += len(members)
    if not probs:
        return None, 0
    return sum(probs) / len(probs), total_members


def _normal_cdf(x, mean, std):
    if std <= 0:
        return 1.0 if x >= mean else 0.0
    return 0.5 * (1 + math.erf((x - mean) / (std * math.sqrt(2))))


def emos_bucket_prob(mean, std, lo, hi):
    """EMOS/NGR: вероятность бакета из нормального распределения с
    поправленными средним и разбросом (см. weather_bias.compute_emos_params),
    а не долей членов ансамбля напрямую — так учитывается систематическая
    ошибка не только в среднем, но и в ширине разброса модели."""
    lo_cdf = 0.0 if lo <= -900 else _normal_cdf(lo, mean, std)
    hi_cdf = 1.0 if hi >= 900 else _normal_cdf(hi, mean, std)
    return max(0.0, hi_cdf - lo_cdf)


def ensure_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            city TEXT NOT NULL,
            local_date TEXT NOT NULL,
            local_hour INTEGER,
            unit TEXT,
            bucket_lo REAL,
            bucket_hi REAL,
            market_p REAL,
            model_p REAL,
            edge REAL,
            event_vol REAL,
            ensemble_n INTEGER
        )
        """
    )
    cols = [r[1] for r in conn.execute("PRAGMA table_info(snapshots)")]
    if "wn2_model_p" not in cols:
        conn.execute("ALTER TABLE snapshots ADD COLUMN wn2_model_p REAL")
        conn.execute("ALTER TABLE snapshots ADD COLUMN wn2_edge REAL")
        conn.execute("ALTER TABLE snapshots ADD COLUMN wn2_ensemble_n INTEGER")
    if "emos_model_p" not in cols:
        conn.execute("ALTER TABLE snapshots ADD COLUMN emos_model_p REAL")
        conn.execute("ALTER TABLE snapshots ADD COLUMN emos_edge REAL")
    conn.commit()


def run():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    now = datetime.now(timezone.utc)

    city_bias = compute_city_bias(conn, BIAS_SINCE_TS)
    for city, bias in city_bias.items():
        print(f"{city}: поправка на историческое смещение {bias:+.2f}")

    emos_params = compute_emos_params(conn, BIAS_SINCE_TS)
    for city, p in emos_params.items():
        print(f"{city}: EMOS a={p['a']:.2f} b={p['b']:.2f} spread_scale={p['spread_scale']:.2f} (n={p['n']})")

    for city, cfg in CITIES.items():
        tz = ZoneInfo(cfg["tz"])
        today_local = datetime.now(tz)
        bias = city_bias.get(city, 0.0)
        emos = emos_params.get(city)

        ensembles = {}
        for model in ENSEMBLE_MODELS:
            try:
                ensembles[model] = fetch_ensemble_daily_max(cfg["lat"], cfg["lon"], cfg["tz"], cfg["unit"], model)
            except requests.RequestException as e:
                print(f"{city}: ошибка запроса ({model}) — {e}", file=sys.stderr)
        if not any(ensembles.values()):
            continue

        try:
            wn2_ensemble = fetch_ensemble_daily_max(cfg["lat"], cfg["lon"], cfg["tz"], cfg["unit"], WEATHERNEXT2_MODEL)
        except requests.RequestException as e:
            print(f"{city}: ошибка запроса (WeatherNext 2) — {e}", file=sys.stderr)
            wn2_ensemble = []

        try:
            market = fetch_polymarket_buckets(cfg["poly_slug"], today_local)
        except requests.RequestException as e:
            print(f"{city}: ошибка запроса (Polymarket) — {e}", file=sys.stderr)
            continue

        if market is None or not market["buckets"]:
            print(f"{city}: маркет на сегодня не найден", file=sys.stderr)
            continue

        # Пул сырых членов GFS+ICON — вход для EMOS (см. weather_bias.py).
        # Пулим оба источника вместе: EMOS-регрессия обучена на такой же
        # пуле (compute_ensemble_moments восстанавливает его из истории).
        pooled = [v for members in ensembles.values() for v in members]
        emos_mean = emos_std = None
        if emos is not None and pooled:
            raw_mean = sum(pooled) / len(pooled)
            raw_var = sum((v - raw_mean) ** 2 for v in pooled) / len(pooled)
            emos_mean = emos["a"] + emos["b"] * raw_mean
            emos_std = emos["spread_scale"] * (raw_var ** 0.5)

        rows = []
        for b in market["buckets"]:
            mp, ensemble_n = blended_model_prob(ensembles, b["lo"], b["hi"], bias)
            if mp is None:
                continue
            edge = mp - b["market_p"]
            # WeatherNext 2 — отдельная, ничем не поправленная колонка
            # (см. WEATHERNEXT2_MODEL): своя вероятность, свой edge.
            wn2_mp = model_prob(wn2_ensemble, b["lo"], b["hi"])
            wn2_edge = (wn2_mp - b["market_p"]) if wn2_mp is not None else None
            wn2_n = len(wn2_ensemble) if wn2_ensemble else None
            # EMOS — тоже отдельная колонка (по тем же причинам, что и
            # WeatherNext 2): свежий, непроверенный метод, смешивать с
            # основным прогнозом сразу — потерять возможность честно
            # понять, помог ли он.
            emos_mp = emos_edge = None
            if emos_mean is not None:
                emos_mp = emos_bucket_prob(emos_mean, emos_std, b["lo"], b["hi"])
                emos_edge = emos_mp - b["market_p"]
            rows.append(
                (
                    now.isoformat(),
                    city,
                    today_local.date().isoformat(),
                    today_local.hour,
                    cfg["unit"],
                    b["lo"],
                    b["hi"],
                    b["market_p"],
                    mp,
                    edge,
                    market["event_vol"],
                    ensemble_n,
                    wn2_mp,
                    wn2_edge,
                    wn2_n,
                    emos_mp,
                    emos_edge,
                )
            )
        conn.executemany(
            """
            INSERT INTO snapshots
            (ts_utc, city, local_date, local_hour, unit, bucket_lo, bucket_hi, market_p, model_p, edge, event_vol, ensemble_n,
             wn2_model_p, wn2_edge, wn2_ensemble_n, emos_model_p, emos_edge)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()

        unit_sym = "°F" if cfg["unit"] == "fahrenheit" else "°C"
        best = max(rows, key=lambda r: abs(r[9])) if rows else None
        if best:
            print(f"{city} ({today_local.hour:02d}:00 местных): макс |edge|={best[9]:+.3f} "
                  f"в бакете {best[5]}-{best[6]}{unit_sym} (модель={best[8]:.2f}, рынок={best[7]:.2f})")
        wn2_rows = [r for r in rows if r[13] is not None]
        wn2_best = max(wn2_rows, key=lambda r: abs(r[13])) if wn2_rows else None
        if wn2_best:
            print(f"{city} WeatherNext2: макс |edge|={wn2_best[13]:+.3f} "
                  f"в бакете {wn2_best[5]}-{wn2_best[6]}{unit_sym} (модель={wn2_best[12]:.2f}, рынок={wn2_best[7]:.2f})")
        emos_rows = [r for r in rows if r[16] is not None]
        emos_best = max(emos_rows, key=lambda r: abs(r[16])) if emos_rows else None
        if emos_best:
            print(f"{city} EMOS: макс |edge|={emos_best[16]:+.3f} "
                  f"в бакете {emos_best[5]}-{emos_best[6]}{unit_sym} (модель={emos_best[15]:.2f}, рынок={emos_best[7]:.2f})")

    conn.close()


if __name__ == "__main__":
    run()
