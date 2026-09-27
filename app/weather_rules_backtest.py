"""
Проверка правил ставок микса моделей на истории цен (2026-09-24,
исследование, не в кроне).

Вопрос: можно ли улучшить правило "ставим на бакет с максимальным
перевесом модели над рынком, если перевес >= 10 п.п."? Варианты
зафиксированы ДО просмотра результатов:
- порог перевеса: 5 / 10 (текущий) / 15 / 20 п.п.;
- минимальная оценка модели: 0 / 20% / 30% (отсечь "лотерейные билеты");
- время решения: 02:00 (как сейчас — первый снимок после полуночи) или
  08:00 по местному;
- смешивание с рынком в логит-шкале (0.5·logit(модель) + 0.5·logit(рынок)).
  Линейное смешивание не проверяем: w·модель + (1−w)·рынок − рынок =
  w·(модель − рынок), то есть это просто другой порог.

Честные допущения (урок +$7260, см. CLAUDE.md):
- цена — последняя точка истории НЕ СТАРШЕ FRESH_MIN минут до решения,
  иначе день пропускаем; покупка по цене + SPREAD;
- комиссия Polymarket: доли × 0.05 × p × (1−p), ставка $5 вместе с ней;
- прогноз микса — выпущенный за сутки (day1), параметры микса —
  walk-forward (только дни до решения);
- подбор правила — на TRAIN (июнь-август), проверка — на TEST
  (сентябрь), который при подборе не использовался.

Запуск: python weather_rules_backtest.py
"""

import math
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import weather_multimodel as mm
from weather_edge import CITIES, emos_bucket_prob

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
STAKE, SPREAD, FEE_RATE = 5.0, 0.01, 0.05
FRESH_MIN = 60
MIN_PRICE, MAX_PRICE = 0.03, 0.95
TRAIN = ("2026-06-01", "2026-08-31")
TEST = ("2026-09-01", "2026-09-30")

THRESHOLDS = [0.05, 0.10, 0.15, 0.20]
MIN_MODEL = [0.0, 0.20, 0.30]
HOURS = [2, 8]
BLENDS = ["нет", "логит 50/50"]


def logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def pnl(price, won):
    buy = price + SPREAD
    per_share = buy + FEE_RATE * buy * (1 - buy)
    shares = STAKE / per_share
    return shares - STAKE if won else -STAKE


def collect(conn):
    """[(date, hour, {bucket: (p_model, price)}, win_lo)] по всем городам."""
    out = []
    for city, cfg in CITIES.items():
        tz = ZoneInfo(cfg["tz"])
        days = conn.execute(
            """SELECT d.local_date, o.win_lo FROM price_history_days d
               JOIN weather_poly_outcomes o ON o.city = d.city AND o.local_date = d.local_date
               WHERE d.city = ?""", (city,)).fetchall()
        for d, win_lo in days:
            params = mm.fit(conn, city, cfg["unit"], d)
            if not params:
                continue
            fc = dict(conn.execute(
                "SELECT model, fcst_max FROM mm_forecasts WHERE city = ? AND local_date = ? AND lead = 'day1'",
                (city, d)).fetchall())
            mu = mm.predict(params, fc)
            if mu is None:
                continue
            series = {}
            for lo, hi, t, p in conn.execute(
                    "SELECT bucket_lo, bucket_hi, t_utc, p FROM price_history WHERE city = ? AND local_date = ? ORDER BY t_utc",
                    (city, d)):
                series.setdefault((lo, hi), []).append((t, p))
            for hour in HOURS:
                ts = datetime.fromisoformat(d).replace(tzinfo=tz).timestamp() + hour * 3600
                snap = {}
                for k, s in series.items():
                    last = [p for t, p in s if ts - FRESH_MIN * 60 <= t <= ts]
                    if last:
                        snap[k] = (emos_bucket_prob(mu, params["sigma"], k[0], k[1]), last[-1])
                if len(snap) >= 3:
                    out.append((d, hour, snap, win_lo))
    return out


def evaluate(data, period, thr, min_model, hour, blend):
    n = w = 0
    total = 0.0
    for d, h, snap, win_lo in data:
        if h != hour or not (period[0] <= d <= period[1]):
            continue
        def score(k):
            p, m = snap[k]
            q = p if blend == "нет" else 1 / (1 + math.exp(-(0.5 * logit(p) + 0.5 * logit(m))))
            return q - m
        k = max(snap, key=score)
        p, m = snap[k]
        if score(k) < thr or p < min_model or not (MIN_PRICE <= m <= MAX_PRICE):
            continue
        won = k[0] == win_lo
        n += 1
        w += won
        total += pnl(m, won)
    return n, w, total


def main():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    data = collect(conn)
    print(f"точек решения: {len(data)}")
    rows = []
    for thr in THRESHOLDS:
        for mn in MIN_MODEL:
            for hour in HOURS:
                for blend in BLENDS:
                    tr = evaluate(data, TRAIN, thr, mn, hour, blend)
                    te = evaluate(data, TEST, thr, mn, hour, blend)
                    rows.append(((thr, mn, hour, blend), tr, te))
    print("\nпорог  мин.модель  час  смешивание    | ОБУЧЕНИЕ: ставок выигр  итог   $/ставку | ПРОВЕРКА: ставок выигр  итог   $/ставку")
    for (thr, mn, hour, blend), tr, te in sorted(rows, key=lambda r: -r[1][2]):
        f = lambda x: f"{x[0]:5} {x[1]:5} {x[2]:+8.1f} {x[2] / x[0] if x[0] else 0:+6.2f}"
        mark = "  <- как сейчас" if (thr, mn, hour, blend) == (0.10, 0.0, 2, "нет") else ""
        print(f"{thr*100:4.0f}  {mn*100:6.0f}%    {hour:3}  {blend:12} | {f(tr)} | {f(te)}{mark}")
    conn.close()


if __name__ == "__main__":
    main()
