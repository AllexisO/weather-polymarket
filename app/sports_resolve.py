"""
Резолвер исходов для sports_edge.py: тянет реальный счёт завершившихся
матчей через the-odds-api /scores и сверяет с тем, что было записано в
sports_snapshots, чтобы можно было посчитать, чья сторона (Pinnacle или
цена Polymarket) реально угадывала исход чаще — а не просто смотреть на
edge в моменте.

Экономит бюджет API (500 запросов/месяц, общий с sports_edge.py):
дёргает /scores только для тех лиг, где вообще есть несматченные строки
(матч уже завершился + буфер на то, что счёт успел зафиксироваться), и
не резолвит уже резолвленное (INSERT OR IGNORE, PRIMARY KEY по матчу).

Домашние/гостевые имена команд и commence_time для матчинга берём как
есть — они и в sports_snapshots, и в ответе /scores приходят из одного
и того же the-odds-api, так что сравниваем строки напрямую, без fuzzy
matching команд (он нужен только там, где сводим с Polymarket).
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from sports_edge import ALL_SPORTS as SPORTS

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
ODDS_API = "https://api.the-odds-api.com/v4/sports"

# Буфер после commence_time: не резолвим матч, пока не пройдёт это время —
# счёт может быть ещё не зафиксирован API как completed.
RESOLVE_BUFFER_HOURS = 3
DAYS_FROM = 3  # /scores отдаёт завершённые матчи за последние N дней


def fetch_scores(sport_key):
    r = requests.get(
        f"{ODDS_API}/{sport_key}/scores/",
        params={"apiKey": ODDS_API_KEY, "daysFrom": DAYS_FROM},
        timeout=20,
    )
    r.raise_for_status()
    return r.json(), r.headers.get("x-requests-remaining")


def actual_outcome(ev):
    if not ev.get("completed") or not ev.get("scores"):
        return None
    score_map = {s["name"]: s["score"] for s in ev["scores"] if s.get("score") is not None}
    if ev["home_team"] not in score_map or ev["away_team"] not in score_map:
        return None
    try:
        hs, aw = int(score_map[ev["home_team"]]), int(score_map[ev["away_team"]])
    except (TypeError, ValueError):
        return None
    if hs > aw:
        return "home", hs, aw
    if hs < aw:
        return "away", hs, aw
    return "draw", hs, aw


def ensure_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sports_outcomes (
            league TEXT,
            home_team TEXT,
            away_team TEXT,
            commence_time TEXT,
            home_score INTEGER,
            away_score INTEGER,
            actual_outcome TEXT,
            resolved_at TEXT,
            PRIMARY KEY (home_team, away_team, commence_time)
        )
        """
    )
    conn.commit()


def run():
    if not ODDS_API_KEY:
        print("ODDS_API_KEY не задан (.env) — нечем резолвить", file=sys.stderr)
        return

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=RESOLVE_BUFFER_HOURS)

    unresolved = conn.execute(
        """
        SELECT DISTINCT s.league, s.home_team, s.away_team, s.commence_time
        FROM sports_snapshots s
        LEFT JOIN sports_outcomes o
          ON s.home_team = o.home_team AND s.away_team = o.away_team AND s.commence_time = o.commence_time
        WHERE o.home_team IS NULL
        """
    ).fetchall()

    pending = [
        r for r in unresolved
        if datetime.fromisoformat(r["commence_time"].replace("Z", "+00:00")) <= cutoff
    ]
    if not pending:
        print("Нечего резолвить — нет завершившихся несматченных матчей")
        conn.close()
        return

    leagues_needed = {r["league"] for r in pending}
    sport_keys_needed = {k: v for k, v in SPORTS.items() if v in leagues_needed}

    outcome_rows = []
    last_remaining = None
    for sport_key, league in sport_keys_needed.items():
        try:
            events, remaining = fetch_scores(sport_key)
            last_remaining = remaining
        except requests.RequestException as e:
            print(f"{league}: ошибка odds-api scores — {e}", file=sys.stderr)
            continue

        events_by_key = {(ev["home_team"], ev["away_team"], ev["commence_time"]): ev for ev in events}
        for p in pending:
            if p["league"] != league:
                continue
            ev = events_by_key.get((p["home_team"], p["away_team"], p["commence_time"]))
            if ev is None:
                continue
            result = actual_outcome(ev)
            if result is None:
                continue
            outcome, hs, aw = result
            outcome_rows.append((league, p["home_team"], p["away_team"], p["commence_time"], hs, aw, outcome, now.isoformat()))
            print(f"{league}: {p['home_team']} {hs}-{aw} {p['away_team']} — исход={outcome}")

    if outcome_rows:
        conn.executemany(
            """
            INSERT OR IGNORE INTO sports_outcomes
            (league, home_team, away_team, commence_time, home_score, away_score, actual_outcome, resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            outcome_rows,
        )
        conn.commit()

    print(f"Резолвнуто матчей: {len(outcome_rows)} из {len(pending)} ожидавших, остаток запросов the-odds-api: {last_remaining}")
    conn.close()


if __name__ == "__main__":
    run()
