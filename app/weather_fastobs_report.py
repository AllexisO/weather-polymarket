"""
Фора быстрых замеров перед сводкой METAR (2026-09-30, к weather_fastobs.py): для каждой сводки METAR (metar_seen_src —
когда мы её впервые увидели, опрос раз в 2 мин) ищем быстрый замер той же станции в ту же минуту (±2) и сравниваем,
когда он появился у нас, и совпало ли значение после округления, как в METAR. Плюс — итог кошелька obs_fast.
Запуск: docker compose run --rm collector weather_fastobs_report.py [дней, по умолчанию 3]
"""
import math
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os

from weather_cities import ALL_OBS_CITIES as OBS_CITIES  # 01.10: и Гонконг/Тайбэй (только кошельки по замерам)

MAIN_DB = Path(os.environ.get("POLY_LAB_DB", "/data/db/polymarket_lab.sqlite3"))
FAST_DB = MAIN_DB.parent / "fastobs.sqlite3"
days = int(sys.argv[1]) if len(sys.argv) > 1 else 3
since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
main = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True, timeout=30)
fast = sqlite3.connect(f"file:{FAST_DB}?mode=ro", uri=True, timeout=30)
icao_city = {v["icao"]: c for c, v in OBS_CITIES.items()}
metar = {}
for icao, obs, seen, t in main.execute("SELECT icao, obs_time_utc, MIN(first_seen_utc), temp_c FROM metar_seen_src WHERE obs_time_utc >= ? GROUP BY icao, obs_time_utc", (since,)):
    if icao in icao_city:
        metar.setdefault(icao_city[icao], []).append((datetime.fromisoformat(obs), datetime.fromisoformat(seen), t))
fr = {}
for src, city, obs, t, seen in fast.execute("SELECT source, city, obs_utc, temp_c, first_seen_utc FROM fast_obs WHERE obs_utc >= ?", (since,)):
    fr.setdefault((src, city), []).append((datetime.fromisoformat(obs), datetime.fromisoformat(seen), t))
res = {}
for (src, city), rs in fr.items():
    unit = OBS_CITIES[city]["unit"]
    f = (lambda c: round(c * 9 / 5 + 32)) if unit == "fahrenheit" else (lambda c: math.floor(c + 0.5))
    for mo, ms, mt in metar.get(city, []):
        near = [x for x in rs if abs((x[0] - mo).total_seconds()) <= 120]
        if not near or mt is None:
            continue
        ob, sn, t = min(near, key=lambda x: abs((x[0] - mo).total_seconds()))
        r = res.setdefault(src, {"lead": [], "same": 0, "hi": 0, "lo": 0, "n": 0})
        r["n"] += 1
        d = f(t) - f(mt)
        r["same"] += d == 0; r["hi"] += d > 0; r["lo"] += d < 0
        if (sn - ob).total_seconds() <= 3600:  # фора — только по замерам, пойманным вживую
            r["lead"].append((ms - sn).total_seconds() / 60)
print(f"фора быстрых замеров перед сводкой METAR за {days} дн. (плюс — быстрый замер у нас раньше сводки):")
for src, r in sorted(res.items()):
    L = sorted(r["lead"])
    lead = (f"фора медиана {statistics.median(L):+5.1f} мин (25% {L[len(L)//4]:+5.1f}, 75% {L[3*len(L)//4]:+5.1f}, пойманных вживую {len(L)})"
            if L else "пойманных вживую ещё нет")
    print(f"  {src:9s} сводок {r['n']:5d}: {lead}; значение как в METAR {100 * r['same'] / r['n']:.0f}%, "
          f"выше {100 * r['hi'] / r['n']:.0f}% (опасно для ставки), ниже {100 * r['lo'] / r['n']:.0f}%")
rows = main.execute("""SELECT status, COUNT(*), ROUND(SUM(COALESCE(payout,0)-stake-COALESCE(fee,0)),2) FROM paper_obs_trades
                       WHERE wallet = 'obs_fast' GROUP BY status""").fetchall()
print("кошелёк obs_fast:", ", ".join(f"{s} {n} ({p:+.2f}$)" for s, n, p in rows) or "ставок пока нет")
