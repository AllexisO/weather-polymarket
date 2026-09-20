"""
Арбитражный коллектор: одно и то же футбольное событие оценено ДВУМЯ
независимыми биржами предсказаний — Kalshi (США, публичный API, без
ключа и регистрации, см. KALSHI_API) и Polymarket. В отличие от
sports_edge.py (там Pinnacle — внешний эталон "правильной" цены), тут
ни одна из сторон не считается более правой — обе просто рынки на один
и тот же реальный матч. Интересен сам факт и размер расхождения между
ними: если оно значимое и держится какое-то время — это ближе к
настоящему арбитражу (купить дёшево на одной бирже, продать дорого на
другой), а не к "у нас есть информационное преимущество".

Фаза диагностики (как в самом начале у weather/sports) — просто считаем
и пишем в sqlite, ничего не резолвим и не торгуем, пока не наберётся
достаточно снимков, чтобы понять: расхождения вообще бывают, насколько
большие и как долго держатся.
"""

import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from sports_edge import fetch_polymarket_soccer, teams_match

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

# Те же 5 лиг, что в sports_edge.py — тикеры серий Kalshi для матчей 1X2
# (каждое событие — 3 маркета: победа хозяев / ничья / победа гостей).
KALSHI_SERIES = {
    "KXEPLGAME": "EPL",
    "KXLALIGAGAME": "La Liga",
    "KXSERIEAGAME": "Serie A",
    "KXBUNDESLIGAGAME": "Bundesliga",
    "KXLIGUE1GAME": "Ligue 1",
}

# Точные имена команд из текста правил надёжнее, чем декодировать
# 3-буквенные коды в тикере ("KXEPLGAME-26SEP20FULMUN" — неясно, где
# граница между кодами без справочника команд).
RE_VS = re.compile(r"the (.+?) vs (.+?) professional")


def _price_of(m):
    # Сначала цена последней сделки; если сделок ещё не было (рынок
    # далеко от матча, объём нулевой) — середина bid/ask.
    try:
        lp = float(m.get("last_price_dollars") or 0)
    except (TypeError, ValueError):
        lp = 0.0
    if lp > 0:
        return lp
    try:
        bid = float(m.get("yes_bid_dollars") or 0)
        ask = float(m.get("yes_ask_dollars") or 0)
    except (TypeError, ValueError):
        return None
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    return None


def fetch_kalshi_games(series_ticker):
    r = requests.get(
        f"{KALSHI_API}/markets",
        params={"series_ticker": series_ticker, "status": "open", "limit": 200},
        timeout=20,
    )
    r.raise_for_status()
    markets = r.json().get("markets", [])

    by_event = {}
    for m in markets:
        by_event.setdefault(m["event_ticker"], []).append(m)

    games = []
    for ms in by_event.values():
        if len(ms) != 3 or not ms[0].get("occurrence_datetime"):
            continue
        m_vs = RE_VS.search(ms[0].get("rules_secondary", ""))
        if not m_vs:
            continue
        home_team, away_team = m_vs.group(1).strip(), m_vs.group(2).strip()

        by_outcome = {}
        for m in ms:
            if m["ticker"].endswith("-TIE"):
                by_outcome["draw"] = m
            elif m.get("yes_sub_title") == home_team:
                by_outcome["home"] = m
            elif m.get("yes_sub_title") == away_team:
                by_outcome["away"] = m
        if len(by_outcome) != 3:
            continue

        prices = {k: _price_of(v) for k, v in by_outcome.items()}
        if any(v is None for v in prices.values()):
            continue

        games.append(
            {
                "home_team": home_team,
                "away_team": away_team,
                "commence_time": ms[0]["occurrence_datetime"],
                "prices": prices,
                "tickers": {k: v["ticker"] for k, v in by_outcome.items()},
            }
        )
    return games


def ensure_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kalshi_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            league TEXT,
            home_team TEXT,
            away_team TEXT,
            commence_time TEXT,
            outcome TEXT,
            market_p REAL,
            kalshi_p REAL,
            edge REAL,
            poly_slug TEXT,
            kalshi_ticker TEXT
        )
        """
    )
    conn.commit()


def run():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    ensure_schema(conn)
    now = datetime.now(timezone.utc)

    try:
        poly_games = fetch_polymarket_soccer()
    except requests.RequestException as e:
        print(f"Polymarket: ошибка запроса — {e}", file=sys.stderr)
        conn.close()
        return
    print(f"Polymarket: {len(poly_games)} футбольных матчей с полным 1X2")

    rows = []
    for series_ticker, league in KALSHI_SERIES.items():
        try:
            kalshi_games = fetch_kalshi_games(series_ticker)
        except requests.RequestException as e:
            print(f"{league}: ошибка Kalshi — {e}", file=sys.stderr)
            continue
        print(f"{league}: {len(kalshi_games)} матчей на Kalshi с ценой")

        for kg in kalshi_games:
            # Только матчи, которые ещё не начались — та же ловушка
            # "заглядывание в будущее", что и в sports_edge.py.
            commence = datetime.fromisoformat(kg["commence_time"].replace("Z", "+00:00"))
            if commence <= now:
                continue
            pg = next(
                (
                    g
                    for g in poly_games
                    if teams_match(g["home_title"], kg["home_team"]) and teams_match(g["away_title"], kg["away_team"])
                ),
                None,
            )
            if pg is None:
                continue
            for outcome in ("home", "draw", "away"):
                market_p = pg["prices"][outcome]
                kalshi_p = kg["prices"][outcome]
                edge = kalshi_p - market_p
                rows.append(
                    (
                        now.isoformat(), league, kg["home_team"], kg["away_team"], kg["commence_time"],
                        outcome, market_p, kalshi_p, edge, pg["slug"], kg["tickers"][outcome],
                    )
                )
            max_edge = max(abs(kg["prices"][o] - pg["prices"][o]) for o in ("home", "draw", "away"))
            print(f"{league}: {kg['home_team']} vs {kg['away_team']} — сматчено, макс |edge|={max_edge:.3f}")

    if rows:
        conn.executemany(
            """
            INSERT INTO kalshi_snapshots
            (ts_utc, league, home_team, away_team, commence_time, outcome, market_p, kalshi_p, edge, poly_slug, kalshi_ticker)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    print(f"Сматчено матчей: {len(rows) // 3}, записано строк: {len(rows)}")
    conn.close()


if __name__ == "__main__":
    run()
