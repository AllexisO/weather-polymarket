"""
Кошелёк mm_ws_ml (15.10, решение Alex «да»; заметка #21): бот-мейкер mm_ws_all, но исполнение засчитывается, только если
модель ждёт по нему заработок > 0 (¢ на долю к итогу маркета). Модель — LightGBM на живых исполнениях mm_ws_all (mm_ws_fills,
настоящая цена нашей заявки) с известным итогом.

Признаки — всё известно в момент исполнения: сторона, цена, местный час, накануне / день маркета, город, доли исполнения,
сколько станция уже показала относительно границ варианта (сводки METAR из metar_seen_src — tgftp и aviationweather,
замер старше 10 мин и уже увиденный нами: сводка выходит через ~3.5 мин после замера, берём с запасом).
Цель — итог стороны (1/0) − цена. Три обучения (зёрна 11/22/33), прогноз — среднее.

Проверка до запуска (15.10, этот же код признаков; итог — weather_poly_outcomes = итог Polymarket), порог тот же, что 30.09
(до прогона): mm_ml ≥ лучшей из mm_ws_all / mm_ws_sel + 0.5¢ на долю и в плюсе. Исследование data/research/mm_live_ml.py
(признак станции — архив IEM): 09-12.10 +0.82¢ против +0.20¢, 06-08.10 +2.24¢ против +0.22¢. Здесь — `--check`.

Запуск:
  python weather_mm_ml.py --train            — обучение на всех днях с итогом (крон ночью), модели в data/ml/mm_ws_ml/
  python weather_mm_ml.py --check ДО С ПО    — учёба до ДО, проверка С..ПО (вслепую), печать порога
Бот (weather_mm_ws.py) загружает модели при старте (Scorer); моделей нет — mm_ws_ml не исполняется.
"""
import bisect
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from weather_cities import OBS_CITIES

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
MM_DB = DB_PATH.parent / "mm.sqlite3"
ML_DIR = DB_PATH.parent.parent / "ml" / "mm_ws_ml"
SEEDS = (11, 22, 33)
LAG_S = 600                  # замер старше 10 мин — сводку уже видно
CITY_ID = {c: i for i, c in enumerate(sorted(OBS_CITIES))}
FEATURES = ["side", "price", "hour", "day_of", "city", "size", "lo_gap", "hi_gap"]
PARAMS = dict(objective="regression", learning_rate=0.03, num_leaves=31, min_data_in_leaf=200, bagging_fraction=0.8,
              bagging_freq=1, verbose=-1)
ROUNDS = 300


def market_value(temp_c, unit):
    """Значение сводки в единицах маркета (как признак обучения): °C — целое, °F — из десятых °C до целого."""
    return round(temp_c) if unit == "celsius" else round(temp_c * 9 / 5 + 32)


def features(side, price, ts, city, local_date, lo, hi, size, day_max):
    """Строка признаков. day_max — максимум станции за местный день по сводкам старше LAG_S (None — нет / накануне)."""
    lt = datetime.fromtimestamp(ts, ZoneInfo(OBS_CITIES[city]["tz"]))
    day_of = lt.date().isoformat() == local_date
    mx = day_max if day_of else None
    nan = float("nan")
    return [int(side == "yes"), price, lt.hour + lt.minute / 60, int(day_of), CITY_ID[city], size,
            (mx - lo) if mx is not None and lo > -900 else nan, (hi - mx) if mx is not None and hi < 900 else nan]


class StationMax:
    """Максимум станции за местный день по сводкам METAR (metar_seen_src) на момент ts: замер ≤ ts − LAG_S и увиден нами ≤ ts."""

    def __init__(self, conn, since_utc):
        self.obs = defaultdict(list)   # icao -> [(замер ts, увидели ts, значение)]
        icao_city = {cfg["icao"]: (c, cfg) for c, cfg in OBS_CITIES.items()}
        for icao, ot, fs, t in conn.execute("""SELECT icao, obs_time_utc, MIN(first_seen_utc), MAX(temp_c) FROM metar_seen_src
                                               WHERE source IN ('tgftp', 'awc') AND temp_c IS NOT NULL AND obs_time_utc >= ?
                                               GROUP BY icao, obs_time_utc""", (since_utc,)):
            if icao in icao_city:
                self.obs[icao].append((datetime.fromisoformat(ot).timestamp(), datetime.fromisoformat(fs).timestamp(),
                                       market_value(t, icao_city[icao][1]["unit"])))
        for v in self.obs.values():
            v.sort()

    def get(self, city, local_date, ts):
        cfg = OBS_CITIES[city]
        o = self.obs.get(cfg["icao"], [])
        start = datetime.fromisoformat(local_date).replace(tzinfo=ZoneInfo(cfg["tz"])).timestamp()
        vals = [v for t, fs, v in o[bisect.bisect_left(o, (start,)):bisect.bisect_left(o, (ts - LAG_S,))] if fs <= ts]
        return max(vals) if vals else None


def dataset(until=None):
    """Исполнения mm_ws_all с известным итогом: (дни, X, y, доли)."""
    main = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=60)
    mm = sqlite3.connect(f"file:{MM_DB}?mode=ro", uri=True, timeout=60)
    win = {(r[0], r[1]): r[2] for r in main.execute("SELECT city, local_date, win_lo FROM weather_poly_outcomes")}
    st = StationMax(main, "2026-09-28")
    days, X, y = [], [], []
    for side, price, ts, k, city, d, lo, hi in mm.execute(
            "SELECT side, price, ts, size, city, local_date, bucket_lo, bucket_hi FROM mm_ws_fills WHERE wallet = 'mm_ws_all'"
            + (" AND local_date <= ?" if until else ""), (until,) if until else ()):
        if (city, d) not in win or city not in CITY_ID:
            continue
        hit = float(win[(city, d)] == lo)
        X.append(features(side, price, ts, city, d, lo, hi, k, st.get(city, d, ts)))
        y.append((hit if side == "yes" else 1 - hit) - price)
        days.append(d)
    main.close()
    mm.close()
    return days, X, y


def fit(X, y):
    import lightgbm as lgb
    import numpy as np
    X = np.array(X, dtype=float)
    return [lgb.train({**PARAMS, "seed": s}, lgb.Dataset(X, np.array(y), feature_name=FEATURES, categorical_feature=["city"]), ROUNDS)
            for s in SEEDS]


def train():
    days, X, y = dataset()
    models = fit(X, y)
    ML_DIR.mkdir(parents=True, exist_ok=True)
    for s, m in zip(SEEDS, models):
        m.save_model(str(ML_DIR / f"s{s}.txt"))
    meta = {"trained_at": datetime.now(timezone.utc).isoformat(), "rows": len(y), "days": len(set(days)),
            "first": min(days), "last": max(days), "features": FEATURES, "city_id": CITY_ID}
    (ML_DIR / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))
    print(f"обучено: исполнений {len(y)}, дней {len(set(days))} ({min(days)}..{max(days)})", flush=True)


def take(row, pred):
    """Брать исполнение: прогноз > 0, и никогда «да» на вариант, который сводки уже убили (максимум выше варианта).
    15.10: таких исполнений в обучении 507, и половина — дни, где итог разошёлся со сводкой (Денвер 14.10 — 193, Париж 07.10,
    Лакхнау 13.10); модель выучила «мёртвый по сводке, но торгуют — значит, выиграет» и на обычном мёртвом варианте ждала
    +12¢ (Лондон, «да» за 5¢ при максимуме 22 на варианте 18). Сводка права в ~99.7% дней — такое «да» запрещено правилом."""
    if pred is None or pred <= 0:
        return False
    hi_gap = row[FEATURES.index("hi_gap")]
    return not (row[0] == 1 and hi_gap == hi_gap and hi_gap < 0)


class Scorer:
    """Прогноз заработка на долю для живого бота. Нет моделей или другие города — None (кошелёк не исполняется)."""

    def __init__(self):
        self.models = []
        try:
            import lightgbm as lgb
            meta = json.loads((ML_DIR / "meta.json").read_text())
            if meta.get("city_id") != CITY_ID or meta.get("features") != FEATURES:
                print("mm_ws_ml: модели обучены на другом списке городов/признаков — жду ночного обучения", flush=True)
                return
            self.models = [lgb.Booster(model_file=str(ML_DIR / f"s{s}.txt")) for s in SEEDS]
            self.meta = meta
        except (OSError, ValueError, ImportError) as e:
            print(f"mm_ws_ml: модели не загружены ({type(e).__name__}: {e}) — кошелёк не исполняется", flush=True)

    def __call__(self, row):
        if not self.models:
            return None
        import numpy as np
        x = np.array([row], dtype=float)
        return float(sum(m.predict(x)[0] for m in self.models) / len(self.models))


def check(until, frm, to):
    """Проверка вслепую: учёба на днях ≤ until, проверка frm..to — mm_ws_all, mm_ws_sel и mm_ws_ml (исполнения, где прогноз > 0)."""
    import numpy as np
    days, X, y = dataset(to)
    days = np.array(days)
    X, y = np.array(X, dtype=float), np.array(y)
    tr, te = days <= until, (days >= frm) & (days <= to)
    p = np.mean([m.predict(X[te]) for m in fit(X[tr], y[tr])], axis=0)
    p = np.array([1.0 if take(r, q) else -1.0 for r, q in zip(X[te], p)])   # то же правило, что у живого бота
    size = X[te][:, FEATURES.index("size")]

    def cents(mask):
        return 100 * float((y[te][mask] * size[mask]).sum() / size[mask].sum()) if mask.any() else 0.0
    a, m = cents(np.ones(te.sum(), bool)), cents(p > 0)
    mm = sqlite3.connect(f"file:{MM_DB}?mode=ro", uri=True)
    main = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    win = {(r[0], r[1]): r[2] for r in main.execute("SELECT city, local_date, win_lo FROM weather_poly_outcomes")}
    num = den = 0.0
    for side, price, k, city, d, lo in mm.execute("SELECT side, price, size, city, local_date, bucket_lo FROM mm_ws_fills "
                                                  "WHERE wallet = 'mm_ws_sel' AND local_date BETWEEN ? AND ?", (frm, to)):
        if (city, d) in win:
            hit = float(win[(city, d)] == lo)
            num += k * ((hit if side == "yes" else 1 - hit) - price)
            den += k
    s = 100 * num / den if den else 0.0
    print(f"учёба ≤ {until}: {int(tr.sum())} исп.; проверка {frm}..{to}: {int(te.sum())} исп.")
    print(f"  mm_ws_all {a:+.2f}¢, mm_ws_sel {s:+.2f}¢, mm_ws_ml {m:+.2f}¢ на долю ({int((p > 0).sum())} исп.)")
    ok = m - max(a, s) >= 0.5 and m > 0
    print(f"ПОРОГ (≥ лучшей + 0.5¢ и в плюсе) → {'ПРОШЛО' if ok else 'не прошло'}")
    return ok


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--train":
        from jobmark import mark
        train()
        c = sqlite3.connect(DB_PATH, timeout=60)
        mark(c, "weather_mm_ml")
        c.close()
    elif len(sys.argv) == 5 and sys.argv[1] == "--check":
        check(*sys.argv[2:5])
    else:
        print(__doc__)
