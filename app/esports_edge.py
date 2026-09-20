"""
Третья спортивная гипотеза — киберспорт (та же идея, что в
sports_edge.py: линия Pinnacle против цены Polymarket на исход
конкретного матча). В BOn (BO3/BO5) ничьей не бывает — исходов два, не
три, формула no-vig проще, чем у футбола.

Источник линии Pinnacle — OddsPapi.io (oddspapi.io), API-ключ в .env
(ODDSPAPI_API_KEY). БЮДЖЕТ ПРИНЦИПИАЛЬНО ИНАЧЕ УСТРОЕН, чем у
the-odds-api: там "1 запрос = весь тур одним вызовом", здесь "1 запрос
на список матчей БЕЗ цен (бесплатно по объёму), но 1 запрос НА КАЖДЫЙ
матч ЗА ЦЕНОЙ" (дорого). Бесплатный тариф — 250 запросов/месяц.

Поэтому порядок строго такой:
1. Смотрим топ-матчи по объёму на Polymarket (бесплатно, gamma-api).
2. Смотрим расписание OddsPapi по каждой дисциплине (1 запрос на
   дисциплину, без цены — просто список матчей, чтобы найти fixtureId).
3. Цену (dorogoy запрос) спрашиваем ТОЛЬКО для матчей, которые реально
   нашлись на Polymarket И ещё не начались.
4. MAX_MATCHES_PER_RUN ограничивает худший случай — сколько платных
   запросов может уйти за один прогон, чтобы не спалить месячный лимит
   за несколько дней.
"""

import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from sports_edge import teams_match

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
ODDSPAPI_API_KEY = os.environ.get("ODDSPAPI_API_KEY", "")

GAMMA = "https://gamma-api.polymarket.com"
ODDSPAPI = "https://api.oddspapi.io/v4"

# Префикс в заголовке события Polymarket -> sportId в OddsPapi.
GAME_TO_SPORTID = {
    "Counter-Strike": 17,
    "Dota 2": 16,
    "LoL": 18,
    "Valorant": 61,
}

RE_TITLE = re.compile(r"^(.+?): (.+?) vs (.+?) \(BO\d+\)")

# Верхняя граница платных запросов за один прогон: 4 дисциплины
# (бесплатно) + до 6 матчей за ценой (платно) = максимум 10 запросов.
# Раз в 2 дня (см. крон) это ~150/месяц из лимита 250 — с запасом на
# случай, если бесплатные вызовы дороже, чем ожидается.
MAX_MATCHES_PER_RUN = 6

# Живьём поймали 429 (Too Many Requests) при вызовах подряд без пауз —
# и, что важно, ошибочные запросы ВСЁ РАВНО списываются с месячного
# лимита (проверено по /v4/account до и после). Раз ошибка стоит
# столько же, сколько успех, — пауза между вызовами дешевле, чем риск
# спалить бюджет на 429.
ODDSPAPI_DELAY_S = 2.0


def fetch_polymarket_esports():
    r = requests.get(
        f"{GAMMA}/events",
        params={"closed": "false", "tag_slug": "esports", "limit": 100, "order": "volume24hr", "ascending": "false"},
        timeout=20,
    )
    r.raise_for_status()
    games = []
    for ev in r.json():
        title = ev.get("title", "")
        m = RE_TITLE.match(title)
        if not m:
            continue
        game, home_team, away_team = m.group(1), m.group(2).strip(), m.group(3).strip()
        if game not in GAME_TO_SPORTID:
            continue
        # Маркет на исход всей серии — это тот, чей question совпадает
        # с заголовком события (все остальные маркеты — по отдельным
        # играм/картам/статистике, их тут не сравниваем).
        series_market = next((mm for mm in ev.get("markets", []) if mm.get("question") == title), None)
        if series_market is None:
            continue
        try:
            outcomes = json.loads(series_market["outcomes"])
            prices = json.loads(series_market["outcomePrices"])
        except (KeyError, ValueError, TypeError):
            continue
        if outcomes[:2] != [home_team, away_team]:
            continue
        games.append(
            {
                "slug": ev["slug"],
                "game": game,
                "home_team": home_team,
                "away_team": away_team,
                "volume": ev.get("volume", 0),
                "prices": {"home": float(prices[0]), "away": float(prices[1])},
            }
        )
    return games


def fetch_oddspapi_fixtures(sport_id):
    # Окно в 2 дня — не только "сегодня", чтобы не терять матчи начала
    # завтрашних суток по UTC. Стоимость запроса не зависит от диапазона
    # дат (только от того, что 'from'/'to' заданы и их разница < 10 дней).
    today = datetime.now(timezone.utc).date()
    time.sleep(ODDSPAPI_DELAY_S)
    r = requests.get(
        f"{ODDSPAPI}/fixtures",
        params={
            "apiKey": ODDSPAPI_API_KEY,
            "sportId": sport_id,
            "from": today.isoformat(),
            "to": (today + timedelta(days=2)).isoformat(),
        },
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def fetch_oddspapi_pinnacle_moneyline(fixture_id):
    time.sleep(ODDSPAPI_DELAY_S)
    r = requests.get(f"{ODDSPAPI}/odds", params={"apiKey": ODDSPAPI_API_KEY, "fixtureId": fixture_id}, timeout=20)
    r.raise_for_status()
    data = r.json()
    pinnacle = data.get("bookmakerOdds", {}).get("pinnacle")
    if not pinnacle:
        return None
    # "0/moneyline" в конце bookmakerMarketId — исход всей серии, не
    # отдельной карты (там "/1/moneyline", "/2/moneyline" и т.п.).
    for market in pinnacle.get("markets", {}).values():
        if not market.get("bookmakerMarketId", "").endswith("/0/moneyline"):
            continue
        prices = {}
        for outcome in market.get("outcomes", {}).values():
            for player in outcome.get("players", {}).values():
                side = player.get("bookmakerOutcomeId")
                if side in ("home", "away") and player.get("price"):
                    prices[side] = float(player["price"])
        if "home" in prices and "away" in prices:
            return prices
    return None


def novig_2way(price_home, price_away):
    raw_home, raw_away = 1 / price_home, 1 / price_away
    total = raw_home + raw_away
    return {"home": raw_home / total, "away": raw_away / total}


def ensure_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS esports_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            game TEXT,
            home_team TEXT,
            away_team TEXT,
            commence_time TEXT,
            outcome TEXT,
            market_p REAL,
            pinnacle_p REAL,
            edge REAL,
            event_vol REAL,
            poly_slug TEXT
        )
        """
    )
    conn.commit()


def run():
    if not ODDSPAPI_API_KEY:
        print("ODDSPAPI_API_KEY не задан (.env) — нечем сравнивать", file=sys.stderr)
        return

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    ensure_schema(conn)
    now = datetime.now(timezone.utc)

    try:
        poly_games = fetch_polymarket_esports()
    except requests.RequestException as e:
        print(f"Polymarket: ошибка запроса — {e}", file=sys.stderr)
        conn.close()
        return
    print(f"Polymarket: {len(poly_games)} матчей по отслеживаемым играм")

    games_by_sport = {}
    for g in poly_games:
        games_by_sport.setdefault(GAME_TO_SPORTID[g["game"]], []).append(g)

    fixtures_by_sport = {}
    for sport_id in games_by_sport:
        try:
            fixtures_by_sport[sport_id] = fetch_oddspapi_fixtures(sport_id)
        except requests.RequestException as e:
            print(f"OddsPapi sportId={sport_id}: ошибка запроса — {e}", file=sys.stderr)
            fixtures_by_sport[sport_id] = []
        print(f"OddsPapi sportId={sport_id}: {len(fixtures_by_sport[sport_id])} матчей в расписании сегодня")

    rows = []
    odds_calls_made = 0
    for pg in poly_games:
        if odds_calls_made >= MAX_MATCHES_PER_RUN:
            break
        sport_id = GAME_TO_SPORTID[pg["game"]]
        candidate = next(
            (
                f
                for f in fixtures_by_sport.get(sport_id, [])
                if f.get("hasOdds")
                and datetime.fromisoformat(f["startTime"].replace("Z", "+00:00")) > now
                and teams_match(f["participant1Name"], pg["home_team"])
                and teams_match(f["participant2Name"], pg["away_team"])
            ),
            None,
        )
        if candidate is None:
            continue

        odds_calls_made += 1
        try:
            pinnacle_prices = fetch_oddspapi_pinnacle_moneyline(candidate["fixtureId"])
        except requests.RequestException as e:
            print(f"{pg['game']}: ошибка запроса цены — {e}", file=sys.stderr)
            continue
        if pinnacle_prices is None:
            print(f"{pg['game']}: {pg['home_team']} vs {pg['away_team']} — нет линии Pinnacle на эту серию")
            continue

        novig = novig_2way(pinnacle_prices["home"], pinnacle_prices["away"])
        for outcome in ("home", "away"):
            market_p = pg["prices"][outcome]
            pin_p = novig[outcome]
            edge = pin_p - market_p
            rows.append(
                (
                    now.isoformat(), pg["game"], pg["home_team"], pg["away_team"], candidate["startTime"],
                    outcome, market_p, pin_p, edge, pg["volume"], pg["slug"],
                )
            )
        max_edge = max(abs(novig[o] - pg["prices"][o]) for o in ("home", "away"))
        print(f"{pg['game']}: {pg['home_team']} vs {pg['away_team']} — сматчено, макс |edge|={max_edge:.3f}")

    if rows:
        conn.executemany(
            """
            INSERT INTO esports_snapshots
            (ts_utc, game, home_team, away_team, commence_time, outcome, market_p, pinnacle_p, edge, event_vol, poly_slug)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    print(f"Сматчено матчей: {len(rows) // 2}, запросов за цену потрачено: {odds_calls_made}")
    conn.close()


if __name__ == "__main__":
    run()
