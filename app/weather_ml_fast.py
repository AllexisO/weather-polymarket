"""
Быстрый снимок для кошельков обучаемых моделей (2026-09-26, решение Alex).

Зачем: модели решают по данным на 08:00 местного, но обычный снимок
(weather_edge.py) — раз в 2 часа, и решение фактически принималось в 08:00-10:00
(в среднем +34 мин, 47% — позже 09:00). На проверке решение в 10:00 вместо 08:00
по реальным сделкам: v3 +$69 против +$104, смесь +$53 против +$107 — рынок успевает.

Что делает (крон каждые 30 минут): для городов, где сейчас 08:00-08:59 местного и
быстрого снимка на сегодня ещё нет, — цены рынка (Polymarket) + шансы v1/v2/v3 и
смеси той же функцией, что и обычный снимок (weather_ml_live.bucket_probs), и сразу
ставки только кошельков обучаемых моделей (weather_paper.FAST_WALLETS).
Open-Meteo не нужен (прогнозы уже в базе).

БЕЗОПАСНОСТЬ: пишет в ОТДЕЛЬНУЮ таблицу snapshots_fast — её читают только кошельки
обучаемых моделей (и «насколько модель права»). Обычные снимки, формулы, страницы,
поправки — не затронуты. Если быстрый снимок не сработал, кошельки решат по
обычному снимку, как раньше. Отключить — убрать строку из крона.
Запуск: python weather_ml_fast.py [--no-bets]
"""

from jobmark import item_guard
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from weather_cities import OBS_CITIES
from weather_edge import CITIES, fetch_polymarket_buckets
from weather_ml_live import blend_with_market, bucket_probs

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
HOUR = 8  # решение в 08:00 местного — как в обучении


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS snapshots_fast (
        ts_utc TEXT NOT NULL, city TEXT NOT NULL, local_date TEXT NOT NULL, local_hour INTEGER, unit TEXT,
        bucket_lo REAL, bucket_hi REAL, market_p REAL, best_ask REAL, event_vol REAL,
        ml_model_p REAL, ml2_model_p REAL, ml3_model_p REAL, ml3c_model_p REAL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_fast_city_date ON snapshots_fast (city, local_date)")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(snapshots_fast)")]
    for c in ("ml4_model_p", "ml4c_model_p", "ml4e_model_p", "ml4ec_model_p",  # 2026-09-26: v4; 09-27: v4e
              "ml5_model_p", "ml5c_model_p"):  # 2026-09-29: v5 «от рынка»
        if c not in cols:
            conn.execute(f"ALTER TABLE snapshots_fast ADD COLUMN {c} REAL")
    conn.commit()


def main():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    due = []
    for city, cfg in CITIES.items():
        with item_guard(city, conn):
            now_local = datetime.now(ZoneInfo(cfg["tz"]))
            d = now_local.date().isoformat()
            if now_local.hour != HOUR or city not in OBS_CITIES:
                continue
            if conn.execute("SELECT 1 FROM snapshots_fast WHERE city = ? AND local_date = ?", (city, d)).fetchone():
                continue
            due.append((city, cfg, now_local))
    print(f"городов, где сейчас 08:xx и быстрого снимка ещё нет: {len(due)}")
    if due:
        metars = {}
        try:
            icaos = ",".join(OBS_CITIES[c]["icao"] for c, _, _ in due)
            for m in requests.get("https://aviationweather.gov/api/data/metar",
                                  params={"ids": icaos, "hours": 36, "format": "json"}, timeout=30).json():
                metars.setdefault(m["icaoId"], []).append(m)
        except (requests.RequestException, ValueError) as e:
            print(f"METAR недоступны — {e}")
        for city, cfg, now_local in due:
            try:
                market = fetch_polymarket_buckets(cfg["poly_slug"], now_local)
            except requests.RequestException as e:
                print(f"{city}: ошибка Polymarket — {e}")
                continue
            if not market or not market["buckets"]:
                print(f"{city}: маркет на сегодня не найден")
                continue
            res = bucket_probs(conn, city, cfg, market["buckets"], metars.get(OBS_CITIES[city]["icao"], []))
            if res is None:
                print(f"{city}: у модели нет оценки (нет прогнозов/замеров)")
                continue
            v1, mu, v2, v3, v4, v4e, v5 = res
            v3c = blend_with_market(v3, [b["market_p"] for b in market["buckets"]]) if v3 else None
            v4c = blend_with_market(v4, [b["market_p"] for b in market["buckets"]]) if v4 else None
            v4ec = blend_with_market(v4e, [b["market_p"] for b in market["buckets"]]) if v4e else None
            v5c = blend_with_market(v5, [b["market_p"] for b in market["buckets"]]) if v5 else None
            ts = datetime.now(timezone.utc).isoformat()
            rows = [(ts, city, now_local.date().isoformat(), now_local.hour, cfg["unit"], b["lo"], b["hi"], b["market_p"],
                     b.get("best_ask"), market["event_vol"], v1[i] if v1 else None, v2[i] if v2 else None,
                     v3[i] if v3 else None, v3c[i] if v3c else None, v4[i] if v4 else None,
                     v4c[i] if v4c else None, v4e[i] if v4e else None, v4ec[i] if v4ec else None,
                     v5[i] if v5 else None, v5c[i] if v5c else None) for i, b in enumerate(market["buckets"])]
            conn.executemany("""INSERT INTO snapshots_fast (ts_utc, city, local_date, local_hour, unit, bucket_lo, bucket_hi,
                                market_p, best_ask, event_vol, ml_model_p, ml2_model_p, ml3_model_p, ml3c_model_p,
                                ml4_model_p, ml4c_model_p, ml4e_model_p, ml4ec_model_p, ml5_model_p, ml5c_model_p)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows)
            conn.commit()
            print(f"{city}: быстрый снимок {now_local:%H:%M} местного, v1 максимум {mu:.1f}", flush=True)
            time.sleep(1)
    if "--no-bets" not in sys.argv:
        import weather_paper
        weather_paper.run(only=weather_paper.FAST_WALLETS)
    from jobmark import mark
    mark(conn, "weather_ml_fast")
    conn.close()


if __name__ == "__main__":
    main()
