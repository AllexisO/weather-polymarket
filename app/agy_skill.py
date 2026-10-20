"""
Скилл для Antigravity (16.10, идея Alex «а-ля скилл, чтобы agy лучше понимала, что происходит»; решение — вариант с запуском кода
в изолированном контейнере, agy_box.sh). Раньше «больше текста в письме» не помогло 4 раза (тетрадь, память, наша история,
текст NWS — RESEARCH_JOURNAL 13-15.10): LLM читает подсказку как фон. Здесь другое — agy сама считает по данным станции
своими инструментами (Python в контейнере) и отвечает шансами по вариантам.

Папка скилла на день (только данные ДО этого дня — без подглядывания в ответ):
  SKILL.md               — правила маркета и порядок решения;
  cities.json            — станция, единица, часовой пояс, правило итога;
  data/<город>_obs.csv   — все замеры станции с 01.06.2025 (местные дата и время, температура в единице маркета с десятыми,
                           точка росы, ветер, облака), station_obs (архив IEM);
  data/<город>_days.csv  — итог дня: максимум станции в целых единицах маркета и выигравший вариант Polymarket (если был);
  data/<город>_forecasts.csv — (v2, 16.10) прошлые прогнозы максимума против факта станции: погодные модели (mm_forecasts:
                           day1 — выпущен накануне, с 06.2025; live — утренний, как в письме, с 22.09) и LightGBM v3 (walkforward —
                           ml_preds_var_mkt, обучена только на прошлом, 07-09.2026; morning — живой утренний снимок snapshots_fast,
                           как в письме, с 27.09), у LightGBM ещё p10/p90;
  tools.py               — готовые функции (похожие дни, рост после часа, час пика).
Запуск (контейнер collector): python agy_skill.py ГГГГ-ММ-ДД [папка]  → data/agy_box/skill/<дата>/
"""
import csv
import json
import math
import os
import shutil
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from weather_cities import OBS_CITIES
from weather_llm_hour import ALL_CITIES
from weather_station_obs import whole_f

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
OUT = Path(__file__).parent.parent / "data" / "agy_box" / "skill"
SKILL_SRC = Path(__file__).parent / "agy_skill"    # SKILL.md и tools.py — в git, данные — нет
FIRST = "2025-06-01"


def market_value(tmpf, unit):
    """Максимум в целых единицах маркета: °F — как итог Polymarket (whole_f), °C — из десятых °C, половина вверх."""
    if unit == "fahrenheit":
        return whole_f(tmpf)
    return math.floor(round((tmpf - 32) * 5 / 9, 1) + 0.5 + 1e-9)


def _bucket_quantiles(rows, qs=(0.1, 0.5, 0.9)):
    """Квантили из шансов по вариантам [(lo, hi, p)] (края — половинки, крайние варианты — шириной 2)."""
    rows = sorted((lo if lo > -900 else hi - 2, hi if hi < 900 else lo + 2, p) for lo, hi, p in rows)
    t = sum(p for *_, p in rows) or 1
    out, cum = [], 0.0
    it = iter(qs)
    q = next(it)
    for lo, hi, p in rows:
        while q is not None and cum + p / t >= q:
            out.append(lo + (hi - lo) * (q - cum) / (p / t))
            q = next(it, None)
        cum += p / t
    while len(out) < len(qs):
        out.append(rows[-1][1])
    return out


def _forecasts(conn, city, unit, day, days, path):
    """Прошлые прогнозы максимума против факта станции (только дни до day с известным максимумом)."""
    to_u = (lambda c: c * 9 / 5 + 32) if unit == "fahrenheit" else (lambda c: c)
    rows = [(d, m, lead, v, None, None) for d, m, lead, v in conn.execute(
        "SELECT local_date, model, lead, fcst_max FROM mm_forecasts WHERE city = ? AND local_date < ? AND fcst_max IS NOT NULL",
        (city, day.isoformat()))]
    for d, qs in conn.execute("SELECT date, qs FROM ml_preds_var_mkt WHERE city = ? AND date < ?", (city, day.isoformat())):
        q = json.loads(qs)
        rows.append((d[:10], "lightgbm_v3", "walkforward", to_u(q[6]), to_u(q[2]), to_u(q[10])))
    snap = {}
    for d, ts, lo, hi, p in conn.execute(
            """SELECT local_date, ts_utc, bucket_lo, bucket_hi, ml3_model_p FROM snapshots_fast WHERE city = ? AND local_date < ?
            AND ml3_model_p IS NOT NULL ORDER BY local_date, ts_utc""", (city, day.isoformat())):
        first = snap.setdefault(d, [ts, []])
        if ts == first[0]:
            first[1].append((lo, hi, p))
    for d, (_, bk) in snap.items():
        p10, p50, p90 = _bucket_quantiles(bk)
        rows.append((d, "lightgbm_v3", "morning", p50, p10, p90))
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["local_date", "source", "lead", "forecast", "p10", "p90", "station_max"])
        for d, m, lead, v, a, b in sorted(rows):
            if d in days:
                w.writerow([d, m, lead, round(v, 1), "" if a is None else round(a, 1), "" if b is None else round(b, 1), days[d]])


def build(day, out=None):
    """Папка скилла на день day (date): данные строго до day по местному времени каждого города."""
    out = Path(out) if out else OUT / day.isoformat()
    if out.exists():
        shutil.rmtree(out)
    (out / "data").mkdir(parents=True)
    for f in ("SKILL.md", "tools.py"):
        shutil.copy(SKILL_SRC / f, out / f)
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=60)
    win = {(c, d): (lo, hi) for c, d, lo, hi in conn.execute(
        "SELECT city, local_date, win_lo, win_hi FROM weather_poly_outcomes WHERE local_date < ?", (day.isoformat(),))}
    cities = {}
    for city in ALL_CITIES:
        cfg = OBS_CITIES[city]
        tz, unit = ZoneInfo(cfg["tz"]), cfg["unit"]
        u = "F" if unit == "fahrenheit" else "C"
        cities[city] = {"station": cfg["icao"], "unit": u, "timezone": cfg["tz"],
                        "settles_on": f"highest METAR temperature of the local calendar day (00:00-23:59 {cfg['tz']}), "
                                      f"whole °{u}" + (" (from tenths of °C converted to °F, half up)" if u == "F" else "")}
        days = {}
        with open(out / "data" / f"{city}_obs.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["local_date", "local_time", f"temp_{u}", f"dew_{u}", "wind_dir", "wind_kt", "sky"])
            for v, t, dw, dr, sk, sky in conn.execute(
                    "SELECT valid_utc, tmpf, dwpf, drct, sknt, skyc1 FROM station_obs WHERE city = ? AND valid_utc >= ? "
                    "AND tmpf IS NOT NULL ORDER BY valid_utc", (city, FIRST)):
                lt = datetime.fromisoformat(v).replace(tzinfo=timezone.utc).astimezone(tz)
                if lt.date() >= day:
                    break
                conv = (lambda x: x) if u == "F" else (lambda x: (x - 32) * 5 / 9)
                w.writerow([lt.date().isoformat(), lt.strftime("%H:%M"), round(conv(t), 1),
                            "" if dw is None else round(conv(dw), 1), "" if dr is None else int(dr),
                            "" if sk is None else int(sk), sky or ""])
                d = lt.date().isoformat()
                days[d] = max(days.get(d, -999), market_value(t, unit))
        _forecasts(conn, city, unit, day, days, out / "data" / f"{city}_forecasts.csv")
        with open(out / "data" / f"{city}_days.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["local_date", f"station_max_{u}", "polymarket_winner"])
            for d in sorted(days):
                lo, hi = win.get((city, d), (None, None))
                # Лондон до 12.2025 был в °F (границы в базе — в °C, дробные) — такой итог не показываем
                odd = lo is not None and any(abs(x % 1 - 0.5) > 1e-6 for x in (lo, hi) if abs(x) < 900)
                lab = "" if lo is None or odd else (f"<= {hi - 0.5:g}" if lo < -900 else f">= {lo + 0.5:g}" if hi > 900
                                             else f"{lo + 0.5:g}-{hi - 0.5:g}" if hi - lo > 1 else f"{lo + 0.5:g}")
                w.writerow([d, days[d], lab])
    (out / "cities.json").write_text(json.dumps(cities, indent=1))
    conn.close()
    for p in [out, *out.rglob("*")]:   # контейнер collector пишет от root, agy в своём контейнере — от пользователя сервера
        p.chmod(0o755 if p.is_dir() else 0o644)
    return out


if __name__ == "__main__":
    p = build(date.fromisoformat(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else None)
    print(f"скилл на {sys.argv[1]}: {p}")
