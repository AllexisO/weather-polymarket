"""
Дневная модель — кошелёк ml_day (2026-10-06, решение Alex «сделаем все 3», пункт 2).

Утренние модели (v1-v5) решают только в 08:00 местного и проигрывают рынку; днём появляется то, чего у рынка в 08:00 не было:
замеры с утра. Здесь LightGBM в 10:00 / 12:00 / 14:00 местного видит утренние признаки v3 + замеры станции с утра до этого
часа + цену рынка в этот час и учит поправку к рынку (как v5: отправная точка — ожидаемый максимум по ценам рынка в этот час).
Максимум дня не бывает ниже уже измеренного: уровни распределения поднимаются до максимума с утра. Смесь 35% модели +
65% рынка (как ml3_cal).

Проверка до запуска (weather_study_intraday.py, копия базы, 8 недель с 10.08, 3 обучения, порог записан до запуска):
логошибка смеси против рынка в тот же час — 10:00 −0.016, 12:00 −0.016, 14:00 −0.011, лучше рынка во всех 8 неделях
(порог: −0.010 в 2 из 3 часов). Деньги по настоящим сделкам (порог ≥ 0 в обоих периодах, записан до проверки):
август 171 ставка +$138 (+40%), сентябрь — 04.10 485 ставок +$29 (+3%).

Ставка (как в проверке): одна на город в день — в первый из часов 10/12/14, где у смеси перевес ≥ 3 п.п. над ценой продавца
(«да» — по цене продавца «да», «нет» — по 1 − цена покупателя «да»), цена стороны 3-95¢; покупка по живому стакану
(polyexec.simulate_buy) не дороже шанса смеси − 3 п.п., $2. Не исполнилось — в этот день больше не пробуем (как в проверке).
Каждый час решения пишется в ml_day_preds (шансы модели и смеси, цены рынка, максимум с утра).

Запуск:
  python weather_ml_day.py --train   — обучение на всей истории (крон ночью, после weather_ml_train), модели в data/ml/day_s*/
  python weather_ml_day.py           — решения в 10/12/14 местного (крон каждый час в :05)
  python weather_ml_day.py --dry     — проверка: посчитать и напечатать, ничего не записывать и не ставить
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import lightgbm as lgb
import numpy as np
import pandas as pd
import requests

import weather_ml as ml
import weather_ml_q as mq
from jobmark import item_guard, mark
from weather_cities import OBS_CITIES

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
ML_DIR = DB_PATH.parent.parent / "ml"
WALLET = "ml_day"
HOURS = (10, 12, 14)
SEEDS = (11, 22, 33)
W_MODEL = 0.35          # доля модели в смеси с рынком (как ml3_cal)
EDGE = 0.03
MIN_PRICE, MAX_PRICE = 0.03, 0.95
STAKE = 2.0
OBS_DELAY_MIN = 10      # замер доступен через ~10 мин после времени замера (как у утренних моделей)
DAY_FEATS = ["hour", "day_max_vs_fc", "day_now_vs_fc", "day_dt2h", "day_nobs",
             "h_mkt_mean_vs_fc", "h_mkt_std", "h_mkt_top_p", "day_max_vs_hmkt"]
PREDS_SQL = """CREATE TABLE IF NOT EXISTS ml_day_preds (city TEXT, local_date TEXT, local_hour INTEGER, ts_utc TEXT,
    unit TEXT, day_max_c REAL, model_json TEXT, blend_json TEXT, market_json TEXT, PRIMARY KEY (city, local_date, local_hour))"""


def day_feats(known, fc_mean, hour, prices, unit):
    """Дневные признаки — ОДНА функция для обучения и живого решения. known — [(utc_dt, t°C)] замеры, доступные к часу
    решения (по времени); prices — {(lo, hi): цена «да»} рынка в этот час."""
    if not known:
        return None
    mf = ml.mkt_features(prices, unit, fc_mean)
    if not mf:
        return None
    mx = max(t for _, t in known)
    earlier = [o for o in known if o[0] <= known[-1][0] - timedelta(hours=2)]
    return {"hour": hour, "day_max_c": mx, "day_max_vs_fc": mx - fc_mean, "day_now_vs_fc": known[-1][1] - fc_mean,
            "day_dt2h": known[-1][1] - earlier[-1][1] if earlier else np.nan, "day_nobs": len(known),
            "h_mkt_mean_vs_fc": mf["mkt_mean_vs_fc"], "h_mkt_std": mf["mkt_std"], "h_mkt_top_p": mf["mkt_top_p"],
            "day_max_vs_hmkt": mx - (fc_mean + mf["mkt_mean_vs_fc"])}


def build_train(conn):
    """Утренние строки v3 (с рынком в 08:00) × часы решения: замеры станции (station_obs) и цена рынка (price_history) к часу."""
    ml.USE_MKT = True
    try:
        morning = ml.build(conn)
    finally:
        ml.USE_MKT = False
    morning = morning[morning["actual_c"].notna()]
    feats_morning = ml.features(morning)
    out = []
    for city, g in morning.groupby("city"):
        cfg = OBS_CITIES[city]
        unit, tz = cfg["unit"], ZoneInfo(cfg["tz"])
        days = set(g["date"])
        obs = {}
        for v, t in conn.execute("SELECT valid_utc, tmpf FROM station_obs WHERE city = ? AND tmpf IS NOT NULL", (city,)):
            dt = datetime.fromisoformat(v).replace(tzinfo=timezone.utc)
            d = dt.astimezone(tz).date().isoformat()
            if d in days:
                obs.setdefault(d, []).append((dt, ml.f_to_c(t)))
        win = {(d, h): (datetime.fromisoformat(d).replace(tzinfo=tz).timestamp() + (h - 1) * 3600,
                        datetime.fromisoformat(d).replace(tzinfo=tz).timestamp() + h * 3600) for d in days for h in HOURS}
        prices = {}
        for d, lo, hi, t, p in conn.execute("SELECT local_date, bucket_lo, bucket_hi, t_utc, p FROM price_history WHERE city = ? "
                                            "ORDER BY t_utc", (city,)):
            if d in days and p is not None:
                for h in HOURS:
                    a, b = win[(d, h)]
                    if a <= t <= b:
                        prices.setdefault((d, h), {})[(lo, hi)] = p
        for r in g.to_dict("records"):
            d = r["date"]
            if d < ml.MKT_UNRELIABLE_BEFORE.get(city, ""):
                continue
            for h in HOURS:
                pr = prices.get((d, h))
                if not pr or len(pr) < 3:
                    continue
                t_dec = datetime.fromisoformat(d).replace(tzinfo=tz) + timedelta(hours=h)
                known = sorted(o for o in obs.get(d, []) if o[0] + timedelta(minutes=OBS_DELAY_MIN) <= t_dec)
                f = day_feats(known, r["fc_mean"], h, pr, unit)
                if f:
                    out.append({**r, **f})
    return pd.DataFrame(out), feats_morning + DAY_FEATS


def train():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    df, feats = build_train(conn)
    base = df["fc_mean"] + df["h_mkt_mean_vs_fc"]
    X, y = df[feats], df["actual_c"] - base
    for seed in SEEDS:
        out = ML_DIR / f"day_s{seed}"
        out.mkdir(parents=True, exist_ok=True)
        ex = {"seed": seed, "bagging_seed": seed, "feature_fraction_seed": seed}
        for q in mq.QUANTILES:
            m = lgb.train({**mq.Q_PARAMS, **ex, "alpha": q}, lgb.Dataset(X, y, categorical_feature=["city_id"]), mq.Q_ROUNDS)
            m.save_model(str(out / f"q{int(round(q * 100)):02d}.txt"))
        (out / "features.json").write_text(json.dumps(feats))
    (ML_DIR / "day_meta.json").write_text(json.dumps({"trained_at": datetime.now(timezone.utc).isoformat(), "rows": len(df),
                                                      "first": df["date"].min(), "last": df["date"].max(), "features": len(feats)}))
    print(f"дневная модель: {len(df)} строк (город-день × час), {len(feats)} признаков, {len(SEEDS)} × {len(mq.QUANTILES)} уровней")
    mark(conn, "weather_ml_day_train")
    conn.close()


_models = None


def models():
    global _models
    if _models is None:
        _models = [({q: lgb.Booster(model_file=str(ML_DIR / f"day_s{s}" / f"q{int(round(q * 100)):02d}.txt")) for q in mq.QUANTILES},
                    json.loads((ML_DIR / f"day_s{s}" / "features.json").read_text())) for s in SEEDS]
    return _models


def morning_row(conn, city, d, tz, metars):
    """Утренняя строка признаков — как weather_ml_live.bucket_probs (прогнозы за сутки, замеры к 07:50, вчерашний максимум),
    плюс рынок в 08:00 из первого быстрого снимка дня (snapshots_fast)."""
    from weather_ml_live import _metar_obs
    cfg = OBS_CITIES[city]
    unit = cfg["unit"]
    prev = (datetime.fromisoformat(d) - timedelta(days=1)).date().isoformat()
    fc = {}
    for dd, mdl, v in conn.execute("SELECT local_date, model, fcst_max FROM mm_forecasts WHERE city = ? AND lead = 'day1' "
                                   "AND local_date IN (?, ?)", (city, d, prev)):
        fc.setdefault(dd, {})[mdl] = ml.to_c(v, unit)
    if d not in fc or len(fc[d]) < 3:
        return None
    fv = dict(conn.execute("SELECT var, value FROM ml_fcst_vars WHERE city = ? AND local_date = ?", (city, d)).fetchall())
    t8 = datetime.fromisoformat(d).replace(tzinfo=tz) + timedelta(hours=ml.DECISION_HOUR)
    obs = sorted(_metar_obs(m) for m in metars if m.get("obsTime") and m.get("temp") is not None)
    known8 = [o for o in obs if o[0].astimezone(tz).date().isoformat() == d and o[0] + timedelta(minutes=ml.OBS_DELAY_MIN) <= t8]
    actual = dict(conn.execute("SELECT local_date, actual_max FROM weather_station_daily WHERE city = ? AND local_date = ?",
                               (city, prev)).fetchall())
    if prev not in actual:
        pv = [o[1] for o in obs if o[0].astimezone(tz).date().isoformat() == prev]
        if pv:
            actual[prev] = round(max(pv) * 9 / 5 + 32) if unit == "fahrenheit" else round(max(pv))
    row = ml.row_for(conn, city, list(OBS_CITIES).index(city), cfg, d, fc, fv, known8, actual)
    p8 = {(lo, hi): p for lo, hi, p in conn.execute(
        """SELECT bucket_lo, bucket_hi, market_p FROM snapshots_fast WHERE city = ? AND local_date = ?
           AND ts_utc = (SELECT MIN(ts_utc) FROM snapshots_fast WHERE city = ? AND local_date = ?)""", (city, d, city, d))
        if p is not None}
    row.update(ml.mkt_features(p8, unit, row["fc_mean"]))
    return row


def decide(row, f, mk, unit):
    """Шансы модели и смеси по вариантам маркета mk (weather_llm_hour.markets)."""
    X_base = row["fc_mean"] + f["h_mkt_mean_vs_fc"]
    full = {**row, **f}
    qs = np.mean([np.sort(np.column_stack([m[q].predict(pd.DataFrame([full]).reindex(columns=feats)) for q in mq.QUANTILES]), axis=1)[0]
                  for m, feats in models()], axis=0) + X_base
    qs = np.maximum(qs, f["day_max_c"])
    model = {b["label"]: mq.bucket_prob(list(qs), unit, b["lo"], b["hi"]) for b in mk}
    tot = sum(b["price"] for b in mk) or 1.0
    blend = {b["label"]: W_MODEL * model[b["label"]] + (1 - W_MODEL) * b["price"] / tot for b in mk}
    return model, blend


def bet(conn, city, day, now, mk, blend):
    """Лучший перевес смеси ≥ EDGE над ценой продавца; покупка по живому стакану. Не исполнилось — запись nofill (в этот день
    больше не пробуем, как в проверке)."""
    from polyexec import simulate_buy, trading_stopped
    from weather_paper import cash
    best = None
    for b in mk:
        q = blend[b["label"]]
        for side, price, qq in (("yes", b["ask"], q), ("no", 1 - b["bid"], 1 - q)):
            if MIN_PRICE <= price <= MAX_PRICE and qq - price >= EDGE and (best is None or qq - price > best[0]):
                best = (qq - price, side, price, qq, b)
    if best is None:
        return None
    if trading_stopped() or cash(conn, WALLET) < STAKE * 1.1:
        return "ставок нет: STOP или нет денег"
    edge, side, price, q, b = best
    tok = json.loads(b["m"]["clobTokenIds"])[0 if side == "yes" else 1]
    f = simulate_buy(b["m"], tok, STAKE, q - EDGE)
    filled = f["shares"] > 0
    lt = now.astimezone(ZoneInfo(OBS_CITIES[city]["tz"]))
    conn.execute("""INSERT OR IGNORE INTO paper_trades (wallet, city, local_date, snapshot_ts, unit, bucket_lo, bucket_hi,
        model_p, market_p, price, stake, status, reason, shares, fee, book_json, condition_id, token_id, placed_at, side)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                 (WALLET, city, day, now.isoformat(), OBS_CITIES[city]["unit"], b["lo"], b["hi"], q, price,
                  f["avg"] if filled else None, f["cost"] if filled else 0.0, "open" if filled else "nofill",
                  f"смесь {q * 100:.0f}% против цены {price * 100:.0f}¢ ({side}), {lt:%H:%M} местного"
                  + ("" if filled else f"; не исполнилось: {f['reason']}"),
                  f["shares"], f["fee"], f["book"], b["m"].get("conditionId"), tok, now.isoformat(), side))
    conn.commit()
    return (f"купили {side} {b['label']} {f['shares']:.1f} долей по {f['avg'] * 100:.1f}¢ (смесь {q * 100:.0f}%)" if filled
            else f"не купили {side} {b['label']}: {f['reason']}")


def run(dry=False):
    from weather_llm_hour import markets
    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row   # ml.row_for → weather_multimodel.fit читает поля по имени (как в weather_ml_fast)
    conn.execute(PREDS_SQL)
    conn.commit()
    if not all((ML_DIR / f"day_s{s}" / "features.json").exists() for s in SEEDS):
        print("дневная модель ещё не обучена — запуск с --train")
        mark(conn, "weather_ml_day")
        conn.close()
        return
    due = []
    for city, cfg in OBS_CITIES.items():
        lt = now.astimezone(ZoneInfo(cfg["tz"]))
        if lt.hour in HOURS and not conn.execute("SELECT 1 FROM ml_day_preds WHERE city = ? AND local_date = ? AND local_hour = ?",
                                                 (city, lt.date().isoformat(), lt.hour)).fetchone():
            due.append((city, cfg, lt))
    print(f"городов, где сейчас {'/'.join(str(h) for h in HOURS)}:xx и решения ещё нет: {len(due)}")
    metars = {}
    if due:
        try:
            for m in requests.get("https://aviationweather.gov/api/data/metar", timeout=30,
                                  params={"ids": ",".join(cfg["icao"] for _, cfg, _ in due), "hours": 36, "format": "json"}).json():
                metars.setdefault(m["icaoId"], []).append(m)
        except (requests.RequestException, ValueError) as e:
            print(f"METAR недоступны — {e}; без замеров решать нельзя, час пропущен")
            due = []
    from weather_ml_live import _metar_obs
    for city, cfg, lt in due:
        with item_guard(city, conn):
            d, tz, unit = lt.date().isoformat(), ZoneInfo(cfg["tz"]), cfg["unit"]
            ms = metars.get(cfg["icao"], [])
            row = morning_row(conn, city, d, tz, ms)
            if row is None:
                print(f"{city}: нет прогнозов моделей на сегодня")
                continue
            mk = markets(city, lt.date())
            if len(mk) < 3:
                print(f"{city}: маркета на сегодня нет")
                continue
            obs = sorted((o[0], o[1]) for o in (_metar_obs(m) for m in ms if m.get("obsTime") and m.get("temp") is not None))
            known = [o for o in obs if o[0].astimezone(tz).date().isoformat() == d and o[0] + timedelta(minutes=OBS_DELAY_MIN) <= now]
            f = day_feats(known, row["fc_mean"], lt.hour, {(b["lo"], b["hi"]): b["price"] for b in mk}, unit)
            if f is None:
                print(f"{city}: нет замеров с утра — без решения")
                continue
            model, blend = decide(row, f, mk, unit)
            if dry:
                top = max(blend, key=blend.get)
                print(f"{city} {lt:%H:%M} [проверка]: максимум с утра {f['day_max_c']:.1f}°C, рынок ждёт "
                      f"{row['fc_mean'] + f['h_mkt_mean_vs_fc']:.1f}°C; смесь — {top} {blend[top] * 100:.0f}% "
                      f"(модель {model[top] * 100:.0f}%, рынок {next(b['price'] for b in mk if b['label'] == top) * 100:.0f}¢)", flush=True)
                continue
            conn.execute("INSERT OR REPLACE INTO ml_day_preds VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (city, d, lt.hour, now.isoformat(), unit, f["day_max_c"], json.dumps(model), json.dumps(blend),
                          json.dumps({b["label"]: b["price"] for b in mk})))
            conn.commit()
            res = None
            if not conn.execute("SELECT 1 FROM paper_trades WHERE wallet = ? AND city = ? AND local_date = ?", (WALLET, city, d)).fetchone():
                res = bet(conn, city, d, now, mk, blend)
            top = max(blend, key=blend.get)
            print(f"{city} {lt:%H:%M}: максимум с утра {f['day_max_c']:.1f}°C, смесь — {top} {blend[top] * 100:.0f}%"
                  + (f"; {res}" if res else ""), flush=True)
    if not dry:
        mark(conn, "weather_ml_day")
    conn.close()


if __name__ == "__main__":
    train() if "--train" in sys.argv else run(dry="--dry" in sys.argv)
