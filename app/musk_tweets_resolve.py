"""
Резолвер для musk_tweets_edge.py — та же логика, что esports_resolve.py и
macro_resolve.py: эталон не отдельный запрос к xtracker "постфактум", а
собственная резолюция маркета Polymarket (после closed=true бакет,
резолвнутый в "Yes", получает outcomePrices[0]≈1). Бесплатно, без лимита.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from musk_tweets_edge import parse_bucket

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
GAMMA = "https://gamma-api.polymarket.com"

RESOLVE_BUFFER_HOURS = 12


def fetch_event(slug):
    r = requests.get(f"{GAMMA}/events", params={"slug": slug}, timeout=20)
    r.raise_for_status()
    events = r.json()
    return events[0] if events else None


def _bucket_mid(lo, hi):
    if lo <= -900:
        return hi - 1.0
    if hi >= 900:
        return lo + 1.0
    return (lo + hi) / 2


def resolved_actual(event):
    if not event.get("closed"):
        return None
    for m in event.get("markets", []):
        rng = parse_bucket(m.get("question", ""))
        if rng is None or not m.get("closed"):
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
        CREATE TABLE IF NOT EXISTS musk_tweets_outcomes (
            target_period TEXT PRIMARY KEY,
            poly_slug TEXT,
            actual_value REAL,
            resolved_at TEXT
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
        SELECT DISTINCT s.target_period, s.poly_slug
        FROM musk_tweets_snapshots s
        LEFT JOIN musk_tweets_outcomes o ON s.target_period = o.target_period
        WHERE o.target_period IS NULL
        """
    ).fetchall()
    if not unresolved:
        print("Нечего резолвить")
        conn.close()
        return

    outcome_rows = []
    still_pending = 0
    for r in unresolved:
        try:
            event = fetch_event(r["poly_slug"])
        except requests.RequestException as e:
            print(f"{r['poly_slug']}: ошибка запроса gamma-api — {e}", file=sys.stderr)
            continue
        if event is None:
            print(f"{r['poly_slug']}: событие не найдено", file=sys.stderr)
            continue
        end_date = event.get("endDate")
        if end_date:
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            if now < end_dt - timedelta(hours=RESOLVE_BUFFER_HOURS) and not event.get("closed"):
                still_pending += 1
                continue
        actual = resolved_actual(event)
        if actual is None:
            still_pending += 1
            continue
        outcome_rows.append((r["target_period"], r["poly_slug"], actual, now.isoformat()))
        print(f"{r['target_period']}: факт={actual}")

    if outcome_rows:
        conn.executemany(
            "INSERT OR IGNORE INTO musk_tweets_outcomes (target_period, poly_slug, actual_value, resolved_at) VALUES (?, ?, ?, ?)",
            outcome_rows,
        )
        conn.commit()

    print(f"Резолвнуто: {len(outcome_rows)} из {len(unresolved)} (ещё не закрыт маркет у {still_pending})")
    conn.close()


if __name__ == "__main__":
    run()
