"""
Резолвер исходов для esports_edge.py. В отличие от sports_resolve.py (счёт
через the-odds-api /scores), эталон факта здесь — САМ Polymarket, а именно
его финальную резолюцию конкретного маркета (outcomePrices после closed=true).

Это НЕ то же самое, что "смотреть на текущую цену как на сигнал" (та самая
циркулярность, из-за которой закрыты крипто-маркеты, см. CLAUDE.md) — резолюция
маркета на Polymarket основана на реальном результате матча, который проверяет
и подтверждает UMA-оракул по внешнему источнику (HLTV.org и т.п., указан в
описании маркета), а не на цене этого же маркета до матча. Сравнивать
"угадал ли Pinnacle победителя до матча" с "кто по факту выиграл (по
резолюции Polymarket)" — ровно то же самое по духу, что sports_resolve.py
делает через отдельный API счёта, просто источник факта другой.

Экономически это НАМНОГО дешевле, чем резолвить через OddsPapi (там
бюджет — 250 запросов/месяц, и не факт, что у settlements понятная схема
outcomeId/playerId без дополнительных платных вызовов на сверку): gamma-api
Polymarket бесплатен и без лимита, мы и так его дёргаем в esports_edge.py.

Если резолюция маркета — 50/50 (типично: отменённый/перенесённый за дедлайн
матч, см. текст правил маркета), это НЕ реальный исход матча — пропускаем,
не путать "матч закончился вничью по факту" (в BOn ничьей не бывает) с
"маркет расформирован без результата".
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
GAMMA = "https://gamma-api.polymarket.com"

# BO3/BO5 может идти несколько часов — резолвим не раньше, чем через
# столько часов после заявленного начала, чтобы не спрашивать маркет,
# который просто ещё не успели закрыть.
RESOLVE_BUFFER_HOURS = 5


def fetch_event(slug):
    r = requests.get(f"{GAMMA}/events", params={"slug": slug}, timeout=20)
    r.raise_for_status()
    events = r.json()
    return events[0] if events else None


def resolved_outcome(event):
    """None, если маркет ещё не резолвлен. 'home'/'away', если резолвлен на
    реальный исход. 'void', если резолвлен 50/50 (отменённый/просроченный
    перенос матча — не реальный результат)."""
    title = event["title"]
    series_market = next((m for m in event.get("markets", []) if m.get("question") == title), None)
    if series_market is None or not series_market.get("closed"):
        return None
    try:
        prices = [float(p) for p in json.loads(series_market["outcomePrices"])]
    except (KeyError, ValueError, TypeError):
        return None
    if len(prices) != 2:
        return None
    if abs(prices[0] - prices[1]) < 0.1:  # оба ~0.5 -> 50/50, не реальный исход
        return "void"
    return "home" if prices[0] > prices[1] else "away"


def ensure_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS esports_outcomes (
            game TEXT,
            home_team TEXT,
            away_team TEXT,
            commence_time TEXT,
            poly_slug TEXT,
            actual_outcome TEXT,
            resolved_at TEXT,
            PRIMARY KEY (home_team, away_team, commence_time)
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
    cutoff = now - timedelta(hours=RESOLVE_BUFFER_HOURS)

    unresolved = conn.execute(
        """
        SELECT DISTINCT s.game, s.home_team, s.away_team, s.commence_time, s.poly_slug
        FROM esports_snapshots s
        LEFT JOIN esports_outcomes o
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

    outcome_rows = []
    still_pending = 0
    for p in pending:
        try:
            event = fetch_event(p["poly_slug"])
        except requests.RequestException as e:
            print(f"{p['poly_slug']}: ошибка запроса gamma-api — {e}", file=sys.stderr)
            continue
        if event is None:
            print(f"{p['poly_slug']}: событие не найдено на Polymarket", file=sys.stderr)
            continue
        outcome = resolved_outcome(event)
        if outcome is None:
            still_pending += 1
            continue
        if outcome == "void":
            print(f"{p['game']}: {p['home_team']} vs {p['away_team']} — маркет резолвлен 50/50 "
                  f"(отменён/перенесён), не реальный исход, пропущено")
            continue
        outcome_rows.append((p["game"], p["home_team"], p["away_team"], p["commence_time"], p["poly_slug"], outcome, now.isoformat()))
        print(f"{p['game']}: {p['home_team']} vs {p['away_team']} — победил {'хозяин' if outcome == 'home' else 'гость'}")

    if outcome_rows:
        conn.executemany(
            """
            INSERT OR IGNORE INTO esports_outcomes
            (game, home_team, away_team, commence_time, poly_slug, actual_outcome, resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            outcome_rows,
        )
        conn.commit()

    print(f"Резолвнуто матчей: {len(outcome_rows)} из {len(pending)} ожидавших "
          f"(ещё не закрыт маркет у {still_pending})")
    conn.close()


if __name__ == "__main__":
    run()
