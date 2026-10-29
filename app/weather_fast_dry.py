"""
Быстрые решения в 08:00 — ВХОЛОСТУЮ (29.10, Alex «давай с проверкой вхолостую»; подготовка к настоящим деньгам).

Зачем: обычный путь (крон weather_ml_fast в :02 / :32 → weather_paper по ~40 кошелькам × 47 городам) покупает через 3-4 мин после
снимка (медиана 3.5 мин у ml5_cal). Из неисполненных сигналов сильных кошельков ~15% — цена ушла за эти минуты; по минутным стаканам
20-26.10 (data/research/_speedup_value_1029.py) через 1 мин исполнились бы 21 из 26, итог +$3.03 за неделю на 4 кошелька.
Эта служба проверяет вживую, сколько на самом деле даёт скорость, НИЧЕГО не меняя в рабочих кошельках.

Как: постоянный процесс на час (крон в :55, до :54 следующего часа), модели загружены один раз. Каждые LOOP_S секунд — города, где
местное время 08:00-08:09 и холостого снимка на сегодня ещё нет: снимок тем же расчётом, что у weather_ml_fast (snapshot_rows), и
сразу weather_paper.place только для кошельков общего банка DRY_WALLETS — те же правила, покупка по живому стакану CLOB вхолостую
(simulate_buy, заявок нет). Раз в запуск — расчёт холостых ставок по итогам Polymarket (weather_paper.settle).

БЕЗОПАСНОСТЬ: пишет ТОЛЬКО в отдельную базу data/db/fast_dry.sqlite3 (свои snapshots_fast, snapshots, paper_trades). Рабочая база
подключена ТОЛЬКО НА ЧТЕНИЕ (ATTACH … mode=ro) — прогнозы, замеры, итоги берутся оттуда, писать туда служба не может.
Сравнение с рабочими кошельками — weather_fast_dry.py --report.
Запуск: крон 55 * * * * (JOB_TIMEOUT=3600). docker compose run --rm collector weather_fast_dry.py [--report]
"""

import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from jobmark import item_guard, mark, single_instance
from weather_cities import OBS_CITIES
from weather_edge import CITIES, fetch_polymarket_buckets

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
DRY_DB = Path(os.environ.get("FAST_DRY_DB", DB_PATH.parent / "fast_dry.sqlite3"))
DRY_WALLETS = {"fav", "ml5_cal", "ml5", "ml4e_cal"}   # утренние кошельки общего банка (dashboard._real_pool, без дневной ml_day)
HOUR, WINDOW_MIN = 8, 10       # решение в 08:00-08:09 местного
LOOP_S = 20
RUN_MIN = 59                   # процесс живёт до :54 следующего часа (запуск в :55)


def connect():
    import weather_ml_fast
    import weather_paper as wp
    c = sqlite3.connect(f"file:{DRY_DB}", uri=True, timeout=60)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    wp.ensure_schema(c)                 # свои таблицы — ДО подключения рабочей базы (чтобы создавались здесь)
    weather_ml_fast.ensure_schema(c)
    c.execute(f"ATTACH DATABASE 'file:{DB_PATH}?mode=ro' AS live")
    if not c.execute("SELECT 1 FROM main.sqlite_master WHERE name = 'snapshots'").fetchone():
        c.execute("CREATE TABLE main.snapshots AS SELECT * FROM live.snapshots WHERE 0")   # пустая, та же схема (её читает place)
    c.commit()
    import real_exec
    real_exec.ensure_schema(c)
    return c


def preload():
    """Загрузить модели всех версий заранее, чтобы первое решение не ждало чтения файлов."""
    import weather_ml_live as L
    for fn in ("_load", "_load_q", "_load_q_mkt", "_load_q_mkt31", "_load_q_mkt31e"):
        try:
            getattr(L, fn)()
        except Exception as e:
            print(f"предзагрузка {fn}: {e}")
    for prefix in ("q_fm", L.V531_PREFIX):
        try:
            L._load_q_fm(prefix)
        except Exception as e:
            print(f"предзагрузка {prefix}: {e}")


def run(test_city=None):
    """test_city — проверка: один раз решить по этому городу сейчас (не ждать 08:00); база — FAST_DRY_DB."""
    import real_exec
    import weather_ml_fast
    import weather_paper as wp
    if test_city:
        assert "FAST_DRY_DB" in os.environ, "проверку — только в отдельную базу (FAST_DRY_DB)"
    else:
        single_instance("fast_dry")
    t_end = time.time() + (0 if test_city else RUN_MIN * 60)
    c = connect()
    t0 = time.time()
    preload()
    print(f"модели загружены за {time.time() - t0:.0f} с", flush=True)
    wp.settle(c, datetime.now().astimezone().isoformat())
    done = 0
    first = True
    while first or time.time() < t_end:
        first = False
        due = []
        for city, cfg in CITIES.items():
            if city not in OBS_CITIES:
                continue
            lt = datetime.now(ZoneInfo(cfg["tz"]))
            if test_city:
                if city == test_city:
                    due.append((city, cfg, lt))
                continue
            if lt.hour == HOUR and lt.minute < WINDOW_MIN and not c.execute(
                    "SELECT 1 FROM main.snapshots_fast WHERE city = ? AND local_date = ?", (city, lt.date().isoformat())).fetchone():
                due.append((city, cfg, lt))
        if due:
            metars = {}
            try:
                for m in requests.get("https://aviationweather.gov/api/data/metar", timeout=30,
                                      params={"ids": ",".join(OBS_CITIES[x[0]]["icao"] for x in due), "hours": 36, "format": "json"}).json():
                    metars.setdefault(m["icaoId"], []).append(m)
            except (requests.RequestException, ValueError) as e:
                print(f"METAR недоступны — {e}")
            for city, cfg, lt in due:
                with item_guard(city):
                    market = fetch_polymarket_buckets(cfg["poly_slug"], lt)
                    if not market or not market["buckets"]:
                        print(f"{city}: маркета на сегодня нет")
                        continue
                    rows, mu = weather_ml_fast.snapshot_rows(c, city, cfg, lt, market, metars.get(OBS_CITIES[city]["icao"], []))
                    if rows is None:
                        print(f"{city}: {mu}")
                        continue
                    c.executemany(weather_ml_fast.INSERT_SQL.replace("INTO snapshots_fast", "INTO main.snapshots_fast"), rows)
                    c.commit()
                    done += 1
                    print(f"{city}: холостой снимок {lt:%H:%M:%S} местного", flush=True)
            wp.place(c, datetime.now().astimezone().isoformat(), DRY_WALLETS)
        # 29.10: настоящий счёт вхолостую (real_exec) — заявки по новым ставкам банка сразу после решения, расчёт, гашение
        with item_guard("real_exec"):
            n_new = real_exec.tick(c)
            if n_new:
                print(f"настоящий счёт вхолостую: новых заявок {n_new}", flush=True)
        if not test_city:
            time.sleep(LOOP_S)
    c.close()
    if not test_city:
        live = sqlite3.connect(DB_PATH, timeout=60)
        mark(live, "weather_fast_dry")
        live.close()
    print(f"fast_dry: холостых снимков {done}", flush=True)


def report():
    """Только чтение — то же, что блок «Настоящий счёт вхолостую» на /real (real_exec.summary)."""
    import real_exec
    c = real_exec.open_ro(DRY_DB, DB_PATH)
    if c is None:
        print("данных ещё нет — служба начнёт с ближайших 08:00 местного")
        return
    real_exec.report(c)
    print("\nкошелёк   общих сигналов | исполнено: быстро / обычно | задержка: быстро / обычно | итог закрытых: быстро / обычно")
    for r in real_exec.compare(c):
        lag = lambda x: f"{x:.1f} мин" if x is not None else "—"
        print(f"{r['wallet']:9} {r['n']:5}          | {r['dry_filled']:3} / {r['live_filled']:<3}                 | {lag(r['dry_lag'])} / {lag(r['live_lag'])}"
              f"      | {r['dry_pnl']:+.2f}$ / {r['live_pnl']:+.2f}$")
    c.close()


if __name__ == "__main__":
    if "--report" in sys.argv:
        report()
    else:
        run(sys.argv[sys.argv.index("--test") + 1] if "--test" in sys.argv else None)
