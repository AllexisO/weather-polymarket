"""
Официальный исход погодного маркета — по резолюции самого Polymarket.

weather_resolve.py берёт "факт" из Open-Meteo (модельная оценка по сетке,
максимум по часовым значениям), а Polymarket резолвит по реальному
показанию станции NOAA ("highest reading under the Temp column for all
times on this day" — см. описание маркета). Это не одно и то же: на
2026-09-22 рынок угадывал бакет в Сеуле в 11% случаев по нашему
"факту" — для живого рынка с деньгами так не бывает, значит, мимо
скорее наш эталон, а не рынок.

Здесь — то же, что esports_resolve.py/macro_resolve.py: после closed=true
выигравший бакет получает outcomePrices Yes=1. Это не циркулярность
(цена ≠ сигнал): резолюция подтверждена внешним источником (NOAA),
мы просто читаем уже готовый факт. Для виртуального портфеля
(weather_paper.py) это единственный правильный эталон: ставка на
Polymarket выигрывает ровно тогда, когда выиграл бакет на Polymarket.

gamma-api бесплатен и без лимита. Пишем в weather_poly_outcomes, одна
строка на (city, local_date), без перезаписи.
"""

import json
import os
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from weather_edge import CITIES, GAMMA, month_day_year_slug, parse_bucket

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))


def fetch_winning_bucket(poly_slug, local_date):
    """(lo, hi) выигравшего бакета, либо None — если маркет ещё не
    резолвлен, не найден или резолвлен неоднозначно."""
    slug = f"highest-temperature-in-{poly_slug}-on-{month_day_year_slug(local_date)}"
    r = requests.get(f"{GAMMA}/events", params={"slug": slug}, timeout=20)
    r.raise_for_status()
    events = r.json()
    if not events:
        return None
    winners = []
    for m in events[0]["markets"]:
        if not m.get("closed"):
            return None
        rng = parse_bucket(m["question"])
        if rng is None:
            continue
        prices = json.loads(m["outcomePrices"])
        outcomes = json.loads(m["outcomes"])
        if float(prices[outcomes.index("Yes")]) > 0.99:
            winners.append(rng)
    return winners[0] if len(winners) == 1 else None


def ensure_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS weather_poly_outcomes (
            city TEXT NOT NULL,
            local_date TEXT NOT NULL,
            win_lo REAL,
            win_hi REAL,
            resolved_at TEXT,
            PRIMARY KEY (city, local_date)
        )
        """
    )
    conn.commit()


def run():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    now = datetime.now().astimezone()

    total = 0
    for city, cfg in CITIES.items():
        today_local = datetime.now(ZoneInfo(cfg["tz"])).date().isoformat()
        pending = [
            r["local_date"]
            for r in conn.execute(
                """
                SELECT DISTINCT s.local_date FROM snapshots s
                LEFT JOIN weather_poly_outcomes p ON s.city = p.city AND s.local_date = p.local_date
                WHERE s.city = ? AND p.city IS NULL AND s.local_date < ?
                ORDER BY s.local_date
                """,
                (city, today_local),
            )
        ]
        for d in pending:
            try:
                win = fetch_winning_bucket(cfg["poly_slug"], date.fromisoformat(d))
            except requests.RequestException as e:
                print(f"{city} {d}: ошибка запроса — {e}", file=sys.stderr)
                continue
            if win is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO weather_poly_outcomes (city, local_date, win_lo, win_hi, resolved_at) VALUES (?, ?, ?, ?, ?)",
                (city, d, win[0], win[1], now.isoformat()),
            )
            conn.commit()
            total += 1
            print(f"{city} {d}: выиграл бакет {win[0]}..{win[1]}")

    print(f"Резолвнуто по Polymarket: {total}")
    from jobmark import mark
    mark(conn, "weather_poly_resolve")
    conn.close()


if __name__ == "__main__":
    run()
