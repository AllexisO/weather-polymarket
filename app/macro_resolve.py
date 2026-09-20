"""
Резолвер для macro_edge.py. Как и esports_resolve.py — эталон не отдельный
API, а собственная резолюция маркета Polymarket (после closed=true бакет,
резолвнутый в "Yes", получает outcomePrices[0]≈1). Это НЕ циркулярность:
маркет резолвится реальным отчётом BLS/BEA (см. описание маркета), мы
просто читаем уже готовый факт с Polymarket вместо повторного похода в
FRED за тем же самым числом. Бесплатно и без лимита запросов (gamma-api).

Открытые бакеты ("X% и ниже"/"X% и выше") хранятся как (-999, X)/(X, 999) —
как и в погоде, при определении фактического значения берём границу
закрытого конца, а не -999/999 напрямую (см. _bucket_mid в weather_bias.py,
та же идея).
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from macro_edge import METRICS, parse_bucket

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
GAMMA = "https://gamma-api.polymarket.com"

RESOLVE_BUFFER_HOURS = 6


def fetch_event(slug):
    r = requests.get(f"{GAMMA}/events", params={"slug": slug}, timeout=20)
    r.raise_for_status()
    events = r.json()
    return events[0] if events else None


def _bucket_mid(lo, hi):
    if lo <= -900:
        return hi - 0.05
    if hi >= 900:
        return lo + 0.05
    return (lo + hi) / 2


def resolved_actual(event, step):
    if not event.get("closed"):
        return None
    for m in event.get("markets", []):
        q = m.get("question", "")
        rng = parse_bucket(q, step)
        if rng is None:
            continue
        if not m.get("closed"):
            continue
        try:
            outcomes = json.loads(m["outcomes"])
            prices = json.loads(m["outcomePrices"])
            yes_p = float(prices[outcomes.index("Yes")])
        except (KeyError, ValueError, TypeError, IndexError):
            continue
        if yes_p >= 0.9:
            return _bucket_mid(rng[0], rng[1])
    return None


def ensure_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS macro_outcomes (
            metric TEXT,
            target_period TEXT,
            poly_slug TEXT,
            actual_value REAL,
            resolved_at TEXT,
            PRIMARY KEY (metric, target_period)
        )
        """
    )
    conn.commit()


def run():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    now = datetime.now(timezone.utc)

    unresolved = conn.execute(
        """
        SELECT DISTINCT s.metric, s.target_period, s.poly_slug
        FROM macro_snapshots s
        LEFT JOIN macro_outcomes o ON s.metric = o.metric AND s.target_period = o.target_period
        WHERE o.metric IS NULL
        """
    ).fetchall()
    if not unresolved:
        print("Нечего резолвить — нет несматченных маркетов")
        conn.close()
        return

    outcome_rows = []
    still_pending = 0
    for r in unresolved:
        step = METRICS.get(r["metric"], {}).get("step", 0.1)
        try:
            event = fetch_event(r["poly_slug"])
        except requests.RequestException as e:
            print(f"{r['poly_slug']}: ошибка запроса gamma-api — {e}", file=sys.stderr)
            continue
        if event is None:
            print(f"{r['poly_slug']}: событие не найдено на Polymarket", file=sys.stderr)
            continue
        end_date = event.get("endDate")
        if end_date:
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            if now < end_dt - timedelta(hours=RESOLVE_BUFFER_HOURS) and not event.get("closed"):
                still_pending += 1
                continue
        actual = resolved_actual(event, step)
        if actual is None:
            still_pending += 1
            continue
        outcome_rows.append((r["metric"], r["target_period"], r["poly_slug"], actual, now.isoformat()))
        print(f"{r['metric']} ({r['target_period']}): факт={actual}")

    if outcome_rows:
        conn.executemany(
            """
            INSERT OR IGNORE INTO macro_outcomes
            (metric, target_period, poly_slug, actual_value, resolved_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            outcome_rows,
        )
        conn.commit()

    print(f"Резолвнуто: {len(outcome_rows)} из {len(unresolved)} ожидавших (ещё не закрыт маркет у {still_pending})")
    conn.close()


if __name__ == "__main__":
    run()
