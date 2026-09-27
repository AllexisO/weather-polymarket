"""
Микс моделей (2026-09-23, отдельный трек): прогноз дневного максимума
от ~16 детерминированных моделей (ECMWF, UKMO, Météo-France, ICON, GFS,
GEM, JMA, NBM и др. — все бесплатно через Open-Meteo), смешанных с
весами ПО КАЖДОМУ ГОРОДУ отдельно.

Зачем: проверка на прошлых днях показала, что лучшая модель в каждом
городе своя (Париж — Météo-France, Торонто — NBM, Пекин — JMA...), а
текущий основной прогноз везде берёт только GFS+ICON. Коллега Alex с
ошибкой 0.8° делает по сути то же — много источников + обучение, кому
где верить.

Метод (по каждому городу, walk-forward — только дни ДО прогнозируемого):
- для каждой модели — её среднее смещение (bias) и разброс ошибки
  после снятия смещения (MSE) за последние WINDOW_DAYS дней;
- вес модели = 1/MSE (точнее исторически — больше вес);
- прогноз = взвешенное среднее прогнозов с поправкой на смещение;
- неопределённость (sigma) = фактическая ошибка такого микса на тех же
  прошлых днях; вероятность бакета — из нормального распределения
  (как у EMOS, emos_bucket_prob).

Обучение — на прогнозах, выпущенных ЗА СУТКИ (previous-runs API,
temperature_2m_previous_day1), а не на "склеенной" истории
historical-forecast-api: та собрана из самых свежих прогонов (почти
наблюдение) и дала бы заниженные ошибки. Живой прогноз утром — чуть
короче по сроку, так что sigma по суточным прогнозам немного завышена
(консервативно).

Модели-двойники (KNMI/DMI/MetNo/MeteoSwiss вне своей зоны отдают
ECMWF/ICON один в один) отбрасываются по совпадению почасового ряда,
иначе одна и та же модель получила бы двойной вес.

Факт — weather_station_daily (METAR станций, см. weather_station_obs.py).

Запуск как скрипт (крон раз в день): догружает суточные прогнозы в
историю и заполняет mm_model_p/mm_edge в прошлых утренних снимках
(бэктест, walk-forward). Живой прогноз считает weather_edge.py через
live_bucket_probs().
"""

import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
FORECAST_API = "https://api.open-meteo.com/v1/forecast"
PREVIOUS_RUNS_API = "https://previous-runs-api.open-meteo.com/v1/forecast"

MODELS = [
    "ecmwf_ifs025", "ecmwf_aifs025_single", "ukmo_seamless", "meteofrance_seamless",
    "icon_seamless", "gfs_seamless", "gem_seamless", "jma_seamless", "cma_grapes_global",
    "ncep_nbm_conus", "knmi_seamless", "metno_seamless", "dmi_seamless",
    "meteoswiss_icon_ch1", "italia_meteo_arpae_icon_2i", "bom_access_global",
]
HISTORY_START = date(2026, 6, 1)
WINDOW_DAYS = 45    # веса по последним ~1.5 месяцам — сезон меняется, старые дни менее показательны
MIN_MM_N = 12       # тот же порядок, что MIN_EMOS_N
MIN_MODELS = 3
SIGMA_FLOOR = {"celsius": 0.5, "fahrenheit": 0.9}


def ensure_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mm_forecasts (
            city TEXT NOT NULL,
            local_date TEXT NOT NULL,
            model TEXT NOT NULL,
            lead TEXT NOT NULL,          -- 'day1' — выпущен за сутки; 'live' — утренний живой
            fcst_max REAL,
            fetched_at TEXT,
            PRIMARY KEY (city, local_date, model, lead)
        )
        """
    )
    cols = [r[1] for r in conn.execute("PRAGMA table_info(snapshots)")]
    if cols and "mm_model_p" not in cols:
        conn.execute("ALTER TABLE snapshots ADD COLUMN mm_model_p REAL")
        conn.execute("ALTER TABLE snapshots ADD COLUMN mm_edge REAL")
    conn.commit()


def _fetch_daily_max(url, var, cfg, **extra):
    """{model: {local_date: дневной максимум}} без моделей-двойников."""
    params = {
        "latitude": cfg["lat"], "longitude": cfg["lon"], "timezone": cfg["tz"],
        "temperature_unit": cfg["unit"], "hourly": var, "models": ",".join(MODELS), **extra,
    }
    r = requests.get(url, params=params, timeout=60)
    if r.status_code == 429:  # минутный лимит Open-Meteo — одна повторная попытка
        time.sleep(30)
        r = requests.get(url, params=params, timeout=60)
    r.raise_for_status()
    hourly = r.json()["hourly"]
    times = hourly["time"]
    out, seen = {}, set()
    for model in MODELS:
        series = hourly.get(f"{var}_{model}")
        if not series or all(v is None for v in series):
            continue
        key = tuple(series)
        if key in seen:
            continue
        seen.add(key)
        by_date = {}
        for t, v in zip(times, series):
            if v is not None:
                by_date.setdefault(t[:10], []).append(v)
        out[model] = {d: max(vs) for d, vs in by_date.items() if len(vs) >= 20}
    return out


def store(conn, city, lead, by_model):
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "INSERT OR REPLACE INTO mm_forecasts (city, local_date, model, lead, fcst_max, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
        [(city, d, m, lead, v, now) for m, days in by_model.items() for d, v in days.items()],
    )
    conn.commit()


def fit(conn, city, unit, before_date):
    """Параметры микса по дням строго ДО before_date (walk-forward)."""
    start = (date.fromisoformat(before_date) - timedelta(days=WINDOW_DAYS)).isoformat()
    rows = conn.execute(
        """
        SELECT f.local_date, f.model, f.fcst_max, d.actual_max
        FROM mm_forecasts f JOIN weather_station_daily d ON f.city = d.city AND f.local_date = d.local_date
        WHERE f.city = ? AND f.lead = 'day1' AND f.local_date < ? AND f.local_date >= ?
        """,
        (city, before_date, start),
    ).fetchall()
    errs, by_day = {}, {}
    for r in rows:
        errs.setdefault(r["model"], []).append(r["fcst_max"] - r["actual_max"])
        by_day.setdefault(r["local_date"], ({}, r["actual_max"]))[0][r["model"]] = r["fcst_max"]
    models = {}
    for m, e in errs.items():
        if len(e) < MIN_MM_N:
            continue
        bias = sum(e) / len(e)
        mse = sum((x - bias) ** 2 for x in e) / len(e)
        models[m] = {"bias": bias, "w": 1.0 / max(mse, 0.05)}
    if len(models) < MIN_MODELS:
        return None
    params = {"models": models}
    blend_err = []
    for fc, actual in by_day.values():
        mu = predict(params, fc)
        if mu is not None:
            blend_err.append(mu - actual)
    if len(blend_err) < MIN_MM_N:
        return None
    sigma = (sum(e * e for e in blend_err) / len(blend_err)) ** 0.5
    params["sigma"] = max(sigma, SIGMA_FLOOR[unit])
    params["n"] = len(blend_err)
    return params


def predict(params, forecasts):
    num = den = 0.0
    used = 0
    for m, p in params["models"].items():
        if forecasts.get(m) is None:
            continue
        num += p["w"] * (forecasts[m] - p["bias"])
        den += p["w"]
        used += 1
    return num / den if used >= MIN_MODELS else None


def live_bucket_probs(conn, city, cfg, buckets):
    """Для weather_edge.py: живой прогноз всех моделей на сегодня ->
    список вероятностей по бакетам (или None, если истории мало)."""
    from weather_edge import emos_bucket_prob

    ensure_schema(conn)
    today = datetime.now(ZoneInfo(cfg["tz"])).date().isoformat()
    by_model = _fetch_daily_max(FORECAST_API, "temperature_2m", cfg, forecast_days=1)
    by_model = {m: {d: v for d, v in days.items() if d == today} for m, days in by_model.items()}
    store(conn, city, "live", by_model)
    params = fit(conn, city, cfg["unit"], today)
    if params is None:
        return None
    mu = predict(params, {m: days.get(today) for m, days in by_model.items()})
    if mu is None:
        return None
    return [emos_bucket_prob(mu, params["sigma"], b["lo"], b["hi"]) for b in buckets], mu, params


def backfill_snapshots(conn, city, unit):
    """mm_model_p в прошлых утренних снимках, где его ещё нет: прогноз
    дня — суточный (day1), параметры — только по дням раньше (walk-forward)."""
    from weather_edge import emos_bucket_prob

    days = conn.execute(
        """
        SELECT local_date, MIN(ts_utc) AS ts FROM snapshots
        WHERE city = ? AND local_hour < 12 AND mm_model_p IS NULL
        GROUP BY local_date
        """,
        (city,),
    ).fetchall()
    updated = 0
    for d in days:
        if conn.execute(
            "SELECT 1 FROM mm_forecasts WHERE city = ? AND local_date = ? AND lead = 'live'", (city, d["local_date"])
        ).fetchone():
            continue  # этот день уже считался вживую — не подменять бэктестом
        params = fit(conn, city, unit, d["local_date"])
        if params is None:
            continue
        fc = dict(conn.execute(
            "SELECT model, fcst_max FROM mm_forecasts WHERE city = ? AND local_date = ? AND lead = 'day1'",
            (city, d["local_date"]),
        ).fetchall())
        mu = predict(params, fc)
        if mu is None:
            continue
        for r in conn.execute(
            "SELECT id, bucket_lo, bucket_hi, market_p FROM snapshots WHERE city = ? AND local_date = ? AND ts_utc = ?",
            (city, d["local_date"], d["ts"]),
        ).fetchall():
            p = emos_bucket_prob(mu, params["sigma"], r["bucket_lo"], r["bucket_hi"])
            conn.execute("UPDATE snapshots SET mm_model_p = ?, mm_edge = ? WHERE id = ?", (p, p - r["market_p"], r["id"]))
            updated += 1
    conn.commit()
    return updated


def run():
    from weather_edge import CITIES

    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    tomorrow = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    for city, cfg in CITIES.items():
        last = conn.execute(
            "SELECT MAX(local_date) FROM mm_forecasts WHERE city = ? AND lead = 'day1'", (city,)
        ).fetchone()[0]
        start = (date.fromisoformat(last) - timedelta(days=3)).isoformat() if last else HISTORY_START.isoformat()
        try:
            by_model = _fetch_daily_max(PREVIOUS_RUNS_API, "temperature_2m_previous_day1", cfg,
                                        start_date=start, end_date=tomorrow)
        except requests.RequestException as e:
            print(f"{city}: ошибка — {e}", file=sys.stderr)
            continue
        store(conn, city, "day1", by_model)
        updated = backfill_snapshots(conn, city, cfg["unit"])
        today = datetime.now(ZoneInfo(cfg["tz"])).date().isoformat()
        params = fit(conn, city, cfg["unit"], today)
        if params:
            top = sorted(params["models"].items(), key=lambda kv: -kv[1]["w"])[:3]
            share = sum(p["w"] for p in params["models"].values())
            desc = ", ".join(f"{m} {100 * p['w'] / share:.0f}%" for m, p in top)
            print(f"{city}: {len(by_model)} моделей, sigma={params['sigma']:.2f} (n={params['n']}), "
                  f"главные веса: {desc}; бэктест-строк: {updated}")
        else:
            print(f"{city}: {len(by_model)} моделей, истории пока мало")
    conn.close()


if __name__ == "__main__":
    run()
