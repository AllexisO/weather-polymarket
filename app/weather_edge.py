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

from jobmark import item_guard
import json
import math
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from weather_cities import OBS_CITIES
from weather_bias import compute_city_bias, compute_emos_params
from weather_multimodel import ensure_schema as ensure_mm_schema, live_bucket_probs

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
# 2026-09-22: Азия (Токио, Сеул, Гонконг, Пекин) отключена по решению
# Alex — по официальным исходам Polymarket модель там проигрывала рынку
# сильнее всего (11-32% против 22-57%). Взамен — Мадрид и Торонто: лучше
# меньше городов, но глубже данные по каждому. Старые азиатские снимки
# в sqlite остаются. Координаты — точные координаты самой метеостанции
# (aviationweather.gov stationinfo), а не аэропорта в целом.
# 2026-09-23: Пекин возвращён (решение Alex) — Азию отключали по цифрам,
# посчитанным ещё по неправильному "факту"; на реальных показаниях
# станции EMOS в Пекине угадывает 56% против 39% у рынка (16 дней),
# бэктест +$122 на 9 ставках — мало, проверяем на живых ставках.
# 2026-09-23: все 26 городов из weather_cities.OBS_CITIES (решение Alex —
# больше городов = быстрее статистика по виртуальным кошелькам).
# Координаты — точные координаты станций.
CITIES = {
    k: {"lat": c["lat"], "lon": c["lon"], "tz": c["tz"], "poly_slug": c["poly_slug"], "unit": c["unit"]}
    for k, c in OBS_CITIES.items()
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
# 2026-09-23: при 48 городах упёрлись в бесплатный лимит Open-Meteo
# (ансамбль считается как много запросов: каждые ~10 членов = 1 вызов;
# у WeatherNext 2 64 члена — самый тяжёлый, и при этом самый слабый,
# ~10% попаданий). WN2 оставлен только на исходных 6 городах — это
# отдельное исследование, на кошельки не влияет.
# 2026-09-27 (решение Alex): WN2 проверена и отброшена, страница /calibration убрана —
# больше не скачиваем (~500 вызовов Open-Meteo в сутки). Колонки wn2_* остаются пустыми.
WN2_CITIES = set()
# Пауза между городами — размазать ~48 городов по паре минут, чтобы не
# упираться в минутный лимит Open-Meteo.
CITY_PAUSE_S = 1.5
OPEN_METEO_RETRY_S = 30

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
    params = {
        "latitude": lat,
        "longitude": lon,
        "models": model,
        "hourly": "temperature_2m",
        "temperature_unit": unit,
        "timezone": tz_name,
        "forecast_days": 2,
    }
    r = requests.get(OPEN_METEO_ENSEMBLE, params=params, timeout=20)
    if r.status_code == 429:
        # минутный лимит Open-Meteo — одна повторная попытка после паузы
        time.sleep(OPEN_METEO_RETRY_S)
        r = requests.get(OPEN_METEO_ENSEMBLE, params=params, timeout=20)
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
        # bestAsk — цена, по которой реально можно купить Yes прямо сейчас
        # (outcomePrices — середина/последняя сделка). Нужна виртуальному
        # портфелю (weather_paper.py), чтобы не покупать дешевле, чем дал бы рынок.
        best_ask = m.get("bestAsk")
        buckets.append({"lo": rng[0], "hi": rng[1], "market_p": yes_price, "question": m["question"],
                        "best_ask": float(best_ask) if best_ask not in (None, "") else None})
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
    if "best_ask" not in cols:
        conn.execute("ALTER TABLE snapshots ADD COLUMN best_ask REAL")
    if "ml_model_p" not in cols:
        # 2026-09-25: обучаемая модель (weather_ml_live.py), с 08:00 до 12:00 местного
        conn.execute("ALTER TABLE snapshots ADD COLUMN ml_model_p REAL")
        conn.execute("ALTER TABLE snapshots ADD COLUMN ml_edge REAL")
    if "ml2_model_p" not in cols:
        # 2026-09-25: обучаемая модель v2 — распределение (weather_ml_q.py)
        conn.execute("ALTER TABLE snapshots ADD COLUMN ml2_model_p REAL")
        conn.execute("ALTER TABLE snapshots ADD COLUMN ml2_edge REAL")
    if "ml3_model_p" not in cols:
        # 2026-09-25: обучаемая модель v3 = v2 + мнение рынка в 08:00
        conn.execute("ALTER TABLE snapshots ADD COLUMN ml3_model_p REAL")
        conn.execute("ALTER TABLE snapshots ADD COLUMN ml3_edge REAL")
    if "ml4_model_p" not in cols:
        # 2026-09-26: v4 (v3 с 31 листом) и её смесь с рынком — кошельки ml4 / ml4_cal
        for c in ("ml4_model_p", "ml4_edge", "ml4c_model_p", "ml4c_edge"):
            conn.execute(f"ALTER TABLE snapshots ADD COLUMN {c} REAL")
    if "ml4e_model_p" not in cols:
        # 2026-09-27: v4e (v4, среднее 3 обучений) и её смесь с рынком — кошельки ml4e / ml4e_cal
        for c in ("ml4e_model_p", "ml4e_edge", "ml4ec_model_p", "ml4ec_edge"):
            conn.execute(f"ALTER TABLE snapshots ADD COLUMN {c} REAL")
    if "ml5_model_p" not in cols:
        # 2026-09-29: v5 «от рынка» (учит поправку к рынку) и её смесь с рынком — кошелёк ml5_cal
        for c in ("ml5_model_p", "ml5_edge", "ml5c_model_p", "ml5c_edge"):
            conn.execute(f"ALTER TABLE snapshots ADD COLUMN {c} REAL")
    if "ml3c_model_p" not in cols:
        # 2026-09-26: смесь главной модели с рынком (weather_ml_live.ML3_BLEND_W) — кошелёк ml3_cal
        conn.execute("ALTER TABLE snapshots ADD COLUMN ml3c_model_p REAL")
        conn.execute("ALTER TABLE snapshots ADD COLUMN ml3c_edge REAL")
    if "ens_model_p" not in cols:
        # 2026-09-28: 6 ансамблей (weather_ens.py) + смесь с рынком — кошелёк ens
        conn.execute("ALTER TABLE snapshots ADD COLUMN ens_model_p REAL")
        conn.execute("ALTER TABLE snapshots ADD COLUMN ens_edge REAL")
    conn.commit()


def run():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_mm_schema(conn)
    now = datetime.now(timezone.utc)

    city_bias = compute_city_bias(conn, BIAS_SINCE_TS)
    for city, bias in city_bias.items():
        print(f"{city}: поправка на историческое смещение {bias:+.2f}")

    emos_params = compute_emos_params(conn, BIAS_SINCE_TS)

    # Свежие METAR всех станций одним запросом — утренние замеры для
    # обучаемой модели (архив Iowa Mesonet для утра ещё не готов).
    metars_by_icao = {}
    try:
        icaos = ",".join(c["icao"] for c in OBS_CITIES.values())
        for m in requests.get("https://aviationweather.gov/api/data/metar",
                              params={"ids": icaos, "hours": 36, "format": "json"}, timeout=30).json():
            metars_by_icao.setdefault(m["icaoId"], []).append(m)
    except (requests.RequestException, ValueError) as e:
        print(f"METAR для обучаемой модели недоступны — {e}", file=sys.stderr)
    for city, p in emos_params.items():
        print(f"{city}: EMOS a={p['a']:.2f} b={p['b']:.2f} spread_scale={p['spread_scale']:.2f} (n={p['n']})")

    for i_city, (city, cfg) in enumerate(CITIES.items()):
        with item_guard(city, conn):
            if i_city:
                time.sleep(CITY_PAUSE_S)
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

            wn2_ensemble = []
            if city in WN2_CITIES:
                try:
                    wn2_ensemble = fetch_ensemble_daily_max(cfg["lat"], cfg["lon"], cfg["tz"], cfg["unit"], WEATHERNEXT2_MODEL)
                except requests.RequestException as e:
                    print(f"{city}: ошибка запроса (WeatherNext 2) — {e}", file=sys.stderr)

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
            # Микс ~16 моделей с весами по городу — отдельный трек (mm_*),
            # см. weather_multimodel.py. Ошибка сети здесь не должна ломать
            # основной снимок.
            mm_probs = None
            try:
                mm = live_bucket_probs(conn, city, cfg, market["buckets"])
                if mm is not None:
                    mm_probs, mm_mu, _ = mm
                    print(f"{city}: микс моделей — прогноз максимума {mm_mu:.1f}")
            except requests.RequestException as e:
                print(f"{city}: ошибка запроса (микс моделей) — {e}", file=sys.stderr)

            ml_probs = ml2_probs = ml3_probs = ml3c_probs = ml4_probs = ml4c_probs = ml4e_probs = ml4ec_probs = None
            ml5_probs = ml5c_probs = None
            try:
                from weather_ml_live import bucket_probs as ml_bucket_probs
                ml_res = ml_bucket_probs(conn, city, cfg, market["buckets"],
                                         metars_by_icao.get(OBS_CITIES[city]["icao"], []))
                if ml_res is not None:
                    ml_probs, ml_mu, ml2_probs, ml3_probs, ml4_probs, ml4e_probs, ml5_probs = ml_res
                    ml3c_probs = ml4c_probs = None
                    from weather_ml_live import blend_with_market
                    if ml3_probs:
                        ml3c_probs = blend_with_market(ml3_probs, [b["market_p"] for b in market["buckets"]])
                    if ml4_probs:
                        ml4c_probs = blend_with_market(ml4_probs, [b["market_p"] for b in market["buckets"]])
                    if ml4e_probs:
                        ml4ec_probs = blend_with_market(ml4e_probs, [b["market_p"] for b in market["buckets"]])
                    if ml5_probs:
                        ml5c_probs = blend_with_market(ml5_probs, [b["market_p"] for b in market["buckets"]])
                    print(f"{city}: обучаемая модель — прогноз максимума {ml_mu:.1f}")
            except Exception as e:  # отдельный трек: его ошибка не должна ломать снимок
                print(f"{city}: ошибка обучаемой модели — {e}", file=sys.stderr)

            # 2026-09-28: кошелёк ens — утренние 6 ансамблей (weather_ens.py, 212 вариантов), каждый с равным
            # весом и сдвигом на поправку города (как основная модель), затем смесь 35/65 с рынком (как ml3_cal).
            # Поправки ещё нет (новый город) — без сдвига: смесь с рынком и так гасит ошибку.
            ens_probs = None
            try:
                from weather_ens import ENS_MIN_MODELS, load_members
                ens_members = load_members(conn, city, today_local.date().isoformat(), cfg["unit"])
                if len(ens_members) >= ENS_MIN_MODELS:
                    raw = [blended_model_prob(ens_members, b["lo"], b["hi"], bias)[0] or 0.0 for b in market["buckets"]]
                    from weather_ml_live import blend_with_market
                    ens_probs = blend_with_market(raw, [b["market_p"] for b in market["buckets"]])
            except Exception as e:  # отдельный трек: его ошибка не должна ломать снимок
                print(f"{city}: ошибка ансамблей (кошелёк ens) — {e}", file=sys.stderr)

            pooled = [v for members in ensembles.values() for v in members]
            emos_mean = emos_std = None
            if emos is not None and pooled:
                raw_mean = sum(pooled) / len(pooled)
                raw_var = sum((v - raw_mean) ** 2 for v in pooled) / len(pooled)
                emos_mean = emos["a"] + emos["b"] * raw_mean
                emos_std = emos["spread_scale"] * (raw_var ** 0.5)

            rows = []
            for i_b, b in enumerate(market["buckets"]):
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
                        b["best_ask"],
                        mm_probs[i_b] if mm_probs else None,
                        (mm_probs[i_b] - b["market_p"]) if mm_probs else None,
                        ml_probs[i_b] if ml_probs else None,
                        (ml_probs[i_b] - b["market_p"]) if ml_probs else None,
                        ml2_probs[i_b] if ml2_probs else None,
                        (ml2_probs[i_b] - b["market_p"]) if ml2_probs else None,
                        ml3_probs[i_b] if ml3_probs else None,
                        (ml3_probs[i_b] - b["market_p"]) if ml3_probs else None,
                        ml3c_probs[i_b] if ml3c_probs else None,
                        (ml3c_probs[i_b] - b["market_p"]) if ml3c_probs else None,
                        ml4_probs[i_b] if ml4_probs else None,
                        (ml4_probs[i_b] - b["market_p"]) if ml4_probs else None,
                        ml4c_probs[i_b] if ml4c_probs else None,
                        (ml4c_probs[i_b] - b["market_p"]) if ml4c_probs else None,
                        ml4e_probs[i_b] if ml4e_probs else None,
                        (ml4e_probs[i_b] - b["market_p"]) if ml4e_probs else None,
                        ml4ec_probs[i_b] if ml4ec_probs else None,
                        (ml4ec_probs[i_b] - b["market_p"]) if ml4ec_probs else None,
                        ens_probs[i_b] if ens_probs else None,
                        (ens_probs[i_b] - b["market_p"]) if ens_probs else None,
                        ml5_probs[i_b] if ml5_probs else None,
                        (ml5_probs[i_b] - b["market_p"]) if ml5_probs else None,
                        ml5c_probs[i_b] if ml5c_probs else None,
                        (ml5c_probs[i_b] - b["market_p"]) if ml5c_probs else None,
                    )
                )
            conn.executemany(
                """
                INSERT INTO snapshots
                (ts_utc, city, local_date, local_hour, unit, bucket_lo, bucket_hi, market_p, model_p, edge, event_vol, ensemble_n,
                 wn2_model_p, wn2_edge, wn2_ensemble_n, emos_model_p, emos_edge, best_ask, mm_model_p, mm_edge, ml_model_p, ml_edge, ml2_model_p, ml2_edge, ml3_model_p, ml3_edge, ml3c_model_p, ml3c_edge, ml4_model_p, ml4_edge, ml4c_model_p, ml4c_edge,
                 ml4e_model_p, ml4e_edge, ml4ec_model_p, ml4ec_edge, ens_model_p, ens_edge,
                 ml5_model_p, ml5_edge, ml5c_model_p, ml5c_edge)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    from jobmark import mark
    mark(conn, "weather_edge")
    conn.close()


if __name__ == "__main__":
    run()
