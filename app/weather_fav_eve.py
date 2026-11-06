"""
fav накануне — кошелёк fav_eve (05.11, Alex «да»; разбор «кто зарабатывает» в docs/PRD.md §10).

Зачем: устойчивые трейдеры Polymarket зарабатывают на фаворитах 50-90¢ и заметную часть — НАКАНУНЕ дня, когда цены ещё не устоялись;
наши кошельки решают только в 08:00 в сам день. Правило fav (смесь модели и рынка выше цены фаворита) на модели «накануне» по неделям
21.08-02.11 (data/research/_fav_eve_1105.py, порог записан до результата): 123 ставки, угадано 86 при ожидании 70, +21.1% / +20.3%
по половинам, удача 0.2%; с покупкой по каждому сигналу по цене + 4¢ — 280 ставок, +12.8% / +10.9%.

Как: крон каждые 5 мин. Город, где сейчас 20:00-20:14 местного и решения на ЗАВТРА ещё нет:
1) прогнозы всех моделей на завтра — тот же источник, что в обучении (Open-Meteo previous runs, temperature_2m_previous_day1,
   дневной максимум), условия дня ECMWF 11-17 ч (*_previous_day1, как weather_ml_data);
2) «вчерашний» максимум = максимум СЕГОДНЯ по плановым сводкам METAR к этому часу (итог дня ещё не посчитан);
3) рынок завтра — Gamma (как снимки), признаки рынка — weather_ml.mkt_features;
4) модель v3 на 20:00 накануне (data/ml/q_eve, weather_ml_live.train_eve) → смесь 35% модели + 65% рынка;
5) правило fav: «да» на вариант 50-95¢ со смесью выше цены, наибольший перевес → покупка по живому стакану не дороже
   min(смесь, цена + 1¢), $2 с комиссией (weather_paper.buy_yes); цена покупки вне 50-95¢ — ставки нет.
Итог — weather_paper.settle (все открытые ставки). Следы каждого решения — fav_eve_preds (прогнозы, рынок, уровни, шансы).
Запуск: docker compose run --rm collector weather_fav_eve.py [--dry] [--city london]   (--city — решить сейчас, без окна 20:00)
Тот же код с другими настройками — кошельки fav_eve22 (weather_fav_eve22.py: 22:00, только где fav_eve не купил) и
fav_eve4e (weather_fav_eve4e.py: модель v4e накануне вместо v3).
"""

import json
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

import decision_books
import weather_ml as ml
import weather_ml_live as live
import weather_ml_q as mq
import weather_multimodel as mm
import weather_ml_data as mld
import weather_paper as wp
from jobmark import item_guard, mark
from weather_cities import OBS_CITIES, poly_round, station_for
from weather_edge import CITIES, fetch_polymarket_buckets

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
WALLET = "fav_eve"
HOUR, WINDOW_MIN = 20, 15
BAND, OVER, W_MODEL, STAKE = (0.50, 0.95), 0.01, 0.35, 2.0
START_TS = "2026-11-05T09:00:00+00:00"   # решения с вечера 05.11 (маркеты 06.11); задним числом не ставим
JOB, PREDS_TABLE = "weather_fav_eve", "fav_eve_preds"
MODEL = "q_eve"   # "q_eve4e" — среднее обучений v4e накануне (fav_eve4e)
AFTER = None   # кошелёк, чья купленная ставка на этот город-день отменяет нашу (fav_eve22 — после fav_eve)
PREDS_SQL = """CREATE TABLE IF NOT EXISTS {t} (city TEXT, local_date TEXT, ts_utc TEXT, fc_json TEXT, fv_json TEXT,
               today_max REAL, prices_json TEXT, qs_json TEXT, model_json TEXT, blend_json TEXT, PRIMARY KEY (city, local_date))"""


def forecasts(cfg, d):
    """{модель: максимум °C} на день d и условия дня {переменная: значение} — previous_day1, как в обучении."""
    r = requests.get(mm.PREVIOUS_RUNS_API, timeout=60, params={
        "latitude": cfg["lat"], "longitude": cfg["lon"], "timezone": cfg["tz"], "temperature_unit": cfg["unit"],
        "hourly": "temperature_2m_previous_day1", "models": ",".join(mm.MODELS), "start_date": d, "end_date": d})
    r.raise_for_status()
    h = r.json()["hourly"]
    fc, seen = {}, set()
    for m in mm.MODELS:
        s = h.get(f"temperature_2m_previous_day1_{m}")
        vs = [v for t, v in zip(h["time"], s or []) if t.startswith(d) and v is not None]
        if len(vs) >= 20 and tuple(s) not in seen:      # двойники моделей — один раз (как _fetch_daily_max)
            seen.add(tuple(s))
            fc[m] = ml.to_c(max(vs), cfg["unit"])
    r = requests.get(mld.PREVIOUS_RUNS_API, timeout=60, params={
        "latitude": cfg["lat"], "longitude": cfg["lon"], "timezone": cfg["tz"], "models": mld.FCST_MODEL,
        "hourly": ",".join(f"{v}_previous_day1" for v in mld.FCST_VARS), "start_date": d, "end_date": d})
    r.raise_for_status()
    h = r.json()["hourly"]
    acc = {}
    for i, t in enumerate(h["time"]):
        if int(t[11:13]) in mld.PEAK_HOURS:
            for v in mld.FCST_VARS:
                x = (h.get(f"{v}_previous_day1") or [None] * len(h["time"]))[i]
                if x is not None:
                    acc.setdefault(v, []).append(x)
    fv = {}
    for v, xs in acc.items():   # как weather_ml_data: направление — средний вектор, осадки — сумма, остальное — среднее
        if v == "wind_direction_10m":
            fv["wind_dir_sin"] = sum(math.sin(math.radians(x)) for x in xs) / len(xs)
            fv["wind_dir_cos"] = sum(math.cos(math.radians(x)) for x in xs) / len(xs)
        elif v == "precipitation":
            fv[v] = sum(xs)
        else:
            fv[v] = sum(xs) / len(xs)
    return fc, fv


def today_max(city, cfg, today, metars):
    """Максимум сегодня по плановым сводкам к этому часу, в единицах города (итога дня ещё нет)."""
    from weather_ml_live import _metar_obs
    from weather_obs_live import routine_only
    tz = ZoneInfo(cfg["tz"])
    t = [o[1] for o in (_metar_obs(m) for m in metars if m.get("obsTime") and m.get("temp") is not None
                         and routine_only(m, city, cfg, today)) if o[0].astimezone(tz).date().isoformat() == today]
    if not t:
        return None
    mx = max(t)
    return poly_round(mx * 9 / 5 + 32) if cfg["unit"] == "fahrenheit" else poly_round(mx)


def decide(conn, city, cfg, now, metars, dry=False):
    tz = ZoneInfo(cfg["tz"])
    lt = now.astimezone(tz)
    today, tom = lt.date().isoformat(), (lt.date() + timedelta(days=1)).isoformat()
    unit = cfg["unit"]
    if AFTER and conn.execute("SELECT 1 FROM paper_trades WHERE wallet = ? AND city = ? AND local_date = ? AND stake > 0",
                              (AFTER, city, tom)).fetchone():
        if not dry:
            wp.record_skip(conn, WALLET, city, tom, now.isoformat(), "skip", f"{AFTER} уже купил на этот день")
        return f"{AFTER} уже купил — пропуск"
    mk = fetch_polymarket_buckets(CITIES[city]["poly_slug"], lt + timedelta(days=1))
    if not mk or len(mk["buckets"]) < 3:
        return "маркета на завтра нет"
    fc_t, fv_t = forecasts(cfg, tom)
    if len(fc_t) < 3:
        return f"прогнозов моделей на завтра {len(fc_t)} — мало"
    fc = {tom: fc_t}
    for dd, m, v in conn.execute("SELECT local_date, model, fcst_max FROM mm_forecasts WHERE city = ? AND lead = 'day1' AND local_date = ?",
                                 (city, today)):
        fc.setdefault(dd, {})[m] = ml.to_c(v, unit)
    tm = today_max(city, cfg, today, metars)
    actual = {today: tm} if tm is not None else {}
    prices = {(b["lo"], b["hi"]): b["market_p"] for b in mk["buckets"]}
    row = ml.row_for(conn, city, list(OBS_CITIES).index(city), cfg, tom, fc, fv_t, [], actual)
    row.update(ml.mkt_features(prices, unit, row["fc_mean"]))
    sets = live._load_q_eve4e() if MODEL == "q_eve4e" else [live._load_q_eve()]
    qs = list(np.mean([mq.predict_q(m, pd.DataFrame([row]).reindex(columns=f), [row["fc_mean"]])[0] for m, f in sets], axis=0))
    tot = sum(prices.values()) or 1.0
    model = {b: mq.bucket_prob(qs, unit, b[0], b[1]) for b in prices}
    blend = {b: W_MODEL * model[b] + (1 - W_MODEL) * prices[b] / tot for b in prices}
    lab = lambda b: f"{b[0]}..{b[1]}"
    if not dry:
        conn.execute(f"INSERT OR REPLACE INTO {PREDS_TABLE} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (city, tom, now.isoformat(), json.dumps(fc_t), json.dumps(fv_t), tm,
                      json.dumps({lab(b): p for b, p in prices.items()}), json.dumps([round(x, 3) for x in qs]),
                      json.dumps({lab(b): round(p, 4) for b, p in model.items()}), json.dumps({lab(b): round(p, 4) for b, p in blend.items()})))
        conn.commit()
    cand = [b for b in prices if BAND[0] <= prices[b] < BAND[1] and blend[b] > prices[b]]
    ts = now.isoformat()
    if not cand:
        if not dry:
            wp.record_skip(conn, WALLET, city, tom, ts, "skip",
                           f"нет варианта за {BAND[0]*100:.0f}-{BAND[1]*100:.0f}¢, который смесь модели накануне и рынка считает недооценённым")
        return "нет фаворита с перевесом"
    b = max(cand, key=lambda x: blend[x] - prices[x])
    label = wp.fmt_bucket(b[0], b[1], unit)
    q, p0 = blend[b], prices[b]
    if dry:
        return f"[проверка] купили бы {label}: смесь {q*100:.1f}% против цены {p0*100:.1f}¢ (модель {model[b]*100:.1f}%)"
    if wp.trading_stopped() or wp.cash(conn, WALLET) < STAKE * 1.1:
        wp.record_skip(conn, WALLET, city, tom, ts, "skip", f"STOP или в кошельке меньше ${STAKE * 1.1:.2f}")
        return "STOP или нет денег"
    ex = wp.buy_yes(city, tom, b[0], b[1], min(wp.MAX_PRICE, q, p0 + OVER), 0, STAKE)
    filled = ex["cost"] >= wp.MIN_FILL
    if filled and ex["cost"] + ex["fee"] > wp.cash(conn, WALLET) + 1e-9:
        filled, ex["reason"] = False, f"покупка стоит ${ex['cost'] + ex['fee']:.2f}, а в кошельке ${wp.cash(conn, WALLET):.2f}"
    if filled and not BAND[0] <= ex["avg"] < BAND[1]:
        filled, ex["reason"] = False, f"в живом стакане вариант стоит {ex['avg'] * 100:.1f}¢ — вне полосы {BAND[0]*100:.0f}-{BAND[1]*100:.0f}¢"
    reason = (f"накануне {lt:%H:%M} местного: смесь {q*100:.0f}% против цены {p0*100:.0f}¢ (модель {model[b]*100:.0f}%)"
              + ("" if filled else f"; не купили: {ex['reason'] or 'меньше $1 по выгодной цене'}"))
    conn.execute("""INSERT OR IGNORE INTO paper_trades (wallet, city, local_date, snapshot_ts, unit, bucket_lo, bucket_hi, model_p, market_p,
                    price, stake, status, reason, shares, fee, book_json, side, placed_at, price_max)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'yes', ?, ?)""",
                 (WALLET, city, tom, ts, unit, b[0], b[1], q, p0, ex["avg"], ex["cost"] if filled else 0.0, "open" if filled else "nofill",
                  reason, ex["shares"] if filled else 0.0, ex["fee"] if filled else 0.0, ex["book"],
                  datetime.now(timezone.utc).isoformat(), ex.get("worst") if filled else None))
    conn.commit()
    decision_books.record(conn, WALLET, city, tom, b[0], "yes")
    return (f"купили {label} {ex['shares']:.1f} долей по {ex['avg'] * 100:.1f}¢ (смесь {q*100:.0f}%)" if filled
            else f"не купили {label}: {ex['reason']}")


def run(dry=False, only=None):
    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    wp.ensure_schema(conn)
    conn.execute(PREDS_SQL.format(t=PREDS_TABLE))
    conn.commit()
    if not all((live.ML_DIR / d / "features.json").exists()
               for d in ([f"q_eve4e_s{s}" for s in live.V4E_SEEDS] if MODEL == "q_eve4e" else ["q_eve"])):
        print("модель накануне ещё не обучена (weather_ml_live.py --train)")
        mark(conn, JOB)
        conn.close()
        return
    if not dry:
        wp.settle(conn, now.astimezone().isoformat())
    due = []
    for city, cfg in OBS_CITIES.items():
        if city not in CITIES or (only and city != only):
            continue
        lt = now.astimezone(ZoneInfo(cfg["tz"]))
        tom = (lt.date() + timedelta(days=1)).isoformat()
        if not only and not (lt.hour == HOUR and lt.minute < WINDOW_MIN):
            continue
        if not only and now.isoformat() < START_TS:
            continue
        if not dry and conn.execute("SELECT 1 FROM paper_trades WHERE wallet = ? AND city = ? AND local_date = ?", (WALLET, city, tom)).fetchone():
            continue
        due.append((city, cfg, lt))
    print(f"городов, где сейчас {HOUR}:00-{HOUR}:{WINDOW_MIN - 1} и решения на завтра ещё нет: {len(due)}")
    metars = {}
    if due:
        # 05.11: первый вечер (Токио 20:00) сервис отдал пустой ответ — модель решила без «вчерашнего» максимума (важный признак).
        # Теперь 3 попытки; не вышло — решения не принимаем, города остаются на следующий запуск в окне (каждые 5 мин, окно 15 мин)
        for attempt in range(3):
            try:
                import awc   # 06.11: предел AWC 400 строк — по частям
                for m in awc.metars([station_for(c, lt.date()) for c, _, lt in due], 24):
                    metars.setdefault(m["icaoId"], []).append(m)
                break
            except (requests.RequestException, ValueError) as e:
                print(f"METAR недоступны (попытка {attempt + 1}) — {e}")
                time.sleep(10)
        else:
            print("METAR так и не получили — решения отложены до следующего запуска")
            due = []
    for city, cfg, lt in due:
        with item_guard(city, conn):
            res = decide(conn, city, cfg, now, metars.get(station_for(city, lt.date()), []), dry)
            print(f"{city} {lt:%H:%M}: {res}", flush=True)
    if not dry and not only:
        mark(conn, JOB)
    conn.close()


def main():
    assert "--city" not in sys.argv or "--dry" in sys.argv, f"--city (решить сейчас, без окна {HOUR}:00) — только с --dry"
    run(dry="--dry" in sys.argv, only=sys.argv[sys.argv.index("--city") + 1] if "--city" in sys.argv else None)


if __name__ == "__main__":
    main()
