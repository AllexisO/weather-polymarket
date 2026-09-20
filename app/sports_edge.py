"""
То же самое, что weather_edge.py, только эталон другой: не прогноз погоды,
а "правильная" (sharp) линия букмекера Pinnacle через the-odds-api.com,
вместо толпы. Сравниваем no-vig вероятность Pinnacle с ценой на
Polymarket — футбол (1X2) и топ-лиги США (moneyline, 2 исхода). Ничего
не покупает — только считает и логирует.

Бюджет the-odds-api.com — 500 запросов/месяц на бесплатном тире, один
вызов на лигу стоит 1 запрос и отдаёт ВСЕ матчи лиги разом.

2026-09-19: добавлены NFL/NCAAF/NBA/WNBA (SPORTS_2WAY) — бюджет и так был
на пределе (380/500 к этому моменту), поэтому крон sports_edge.py убран
с 2 раз в день до 1 раза (см. crontab) — резолвер по-прежнему считает
только за реально ожидающие резолва лиги, его частоту не трогали.
Проверено, что для калибровки одного снимка в день достаточно —
compute_sports_calibration/compute_sports_results и так берут только
последний снимок перед матчем на матч.
"""

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")

GAMMA = "https://gamma-api.polymarket.com"
ODDS_API = "https://api.the-odds-api.com/v4/sports"

# the-odds-api sport_key -> человекочитаемая лига. Пять топ-лиг футбола,
# 1X2 (3 исхода) через fetch_polymarket_soccer.
SPORTS = {
    "soccer_epl": "EPL",
    "soccer_spain_la_liga": "La Liga",
    "soccer_italy_serie_a": "Serie A",
    "soccer_germany_bundesliga": "Bundesliga",
    "soccer_france_ligue_one": "Ligue 1",
}

# 2026-09-19: добавлены топ-лиги США — 2 исхода (moneyline), не 3, поэтому
# отдельный путь (fetch_polymarket_2way) и отдельный словарь: sport_key ->
# (лига, tag_slug на Polymarket). NBA пока вне сезона (сезон с октября) —
# запрос будет просто возвращать 0 матчей, ничего не сломает, само
# заработает, когда начнётся сезон. NCAAB не добавлен — у the-odds-api для
# него нет обычных отдельных матчей (только "championship winner"), не
# на чем сравнивать построчно.
SPORTS_2WAY = {
    "americanfootball_nfl": ("NFL", "nfl"),
    "americanfootball_ncaaf": ("NCAAF", "cfb"),
    "basketball_nba": ("NBA", "nba"),
    "basketball_wnba": ("WNBA", "wnba"),
}

# Для sports_resolve.py (там нужно только sport_key -> лига, без tag_slug —
# резолвер сверяет счёт через the-odds-api /scores, Polymarket не трогает).
ALL_SPORTS = {**SPORTS, **{k: v[0] for k, v in SPORTS_2WAY.items()}}

STOPWORDS = {"fc", "afc", "cf", "sc", "de", "club", "united", "city", "town", "athletic", "real", "cd"}


def normalize_team(name):
    tokens = re.findall(r"[a-z0-9]+", name.lower())
    return {t for t in tokens if t not in STOPWORDS}


def teams_match(a, b):
    na, nb = normalize_team(a), normalize_team(b)
    if not na or not nb:
        return False
    overlap = na & nb
    # 2026-09-19: было len(overlap)/min>=0.5 — хватало для футбольных
    # клубов с уникальными именами, но college-спорт (NCAAF) полон пар
    # вида "Utah" vs "Utah State" (общий токен "utah" — половина токенов
    # меньшей стороны), где это РАЗНЫЕ команды, играющие ДРУГ С ДРУГОМ.
    # Живая находка: "Utah State vs. Utah" на Polymarket ложно совпало со
    # "своим же" матчем в other stороны the-odds-api, перепутав домашнюю/
    # гостевую сторону и дав фиктивный edge 0.92. Теперь требуем ПОЛНОЕ
    # покрытие меньшего набора токенов, а не половину.
    return len(overlap) == min(len(na), len(nb))


def fetch_pinnacle_odds(sport_key):
    r = requests.get(
        f"{ODDS_API}/{sport_key}/odds/",
        params={"apiKey": ODDS_API_KEY, "regions": "eu", "markets": "h2h", "oddsFormat": "decimal"},
        timeout=20,
    )
    r.raise_for_status()
    remaining = r.headers.get("x-requests-remaining")
    matches = []
    for ev in r.json():
        pinnacle = next((b for b in ev["bookmakers"] if b["key"] == "pinnacle"), None)
        if pinnacle is None:
            continue
        outcomes = pinnacle["markets"][0]["outcomes"]
        prices = {o["name"]: o["price"] for o in outcomes}
        if ev["home_team"] not in prices or ev["away_team"] not in prices:
            continue
        # no-vig: инвертируем decimal-коэффициенты и нормируем сумму к 1.
        # "Draw" не обязателен — 2026-09-19: американские лиги (NFL/NBA/
        # NCAAF/WNBA) в moneyline двухисходные, ничьей в прайсинге нет.
        raw = {
            "home": 1 / prices[ev["home_team"]],
            "away": 1 / prices[ev["away_team"]],
        }
        if "Draw" in prices:
            raw["draw"] = 1 / prices["Draw"]
        total = sum(raw.values())
        novig = {k: v / total for k, v in raw.items()}
        matches.append(
            {
                "home_team": ev["home_team"],
                "away_team": ev["away_team"],
                "commence_time": ev["commence_time"],
                "novig": novig,
            }
        )
    return matches, remaining


RE_WIN = re.compile(r"Will (.+?) win on")
RE_DRAW = re.compile(r"end in a draw")


def fetch_polymarket_soccer():
    r = requests.get(
        f"{GAMMA}/events",
        params={"closed": "false", "tag_slug": "soccer", "limit": 100, "order": "volume24hr", "ascending": "false"},
        timeout=20,
    )
    r.raise_for_status()
    games = []
    for ev in r.json():
        if "more-markets" in ev.get("slug", "") or "exact-score" in ev.get("slug", ""):
            continue
        title = ev.get("title", "")
        if " vs. " not in title:
            continue
        home_title, away_title = [t.strip() for t in title.split(" vs. ", 1)]

        prices = {}
        for m in ev.get("markets", []):
            q = m.get("question", "")
            try:
                outcomes = json.loads(m["outcomes"])
                outcome_prices = json.loads(m["outcomePrices"])
                yes_p = float(outcome_prices[outcomes.index("Yes")])
            except (KeyError, ValueError, TypeError):
                continue
            if RE_DRAW.search(q):
                prices["draw"] = yes_p
            else:
                m_win = RE_WIN.search(q)
                if m_win:
                    who = m_win.group(1)
                    if who in home_title:
                        prices["home"] = yes_p
                    elif who in away_title:
                        prices["away"] = yes_p

        if len(prices) == 3:
            games.append(
                {
                    "slug": ev["slug"],
                    "home_title": home_title,
                    "away_title": away_title,
                    "end_date": ev.get("endDate"),
                    "volume": ev.get("volume", 0),
                    "prices": prices,
                }
            )
    return games


def fetch_polymarket_2way(tag_slug):
    # Та же схема, что fetch_polymarket_esports в esports_edge.py: серия/игра
    # целиком — маркет, чей question совпадает с заголовком события (не
    # спреды/тоталы/пропсы отдельных игроков — их в этих же событиях
    # десятки).
    r = requests.get(
        f"{GAMMA}/events",
        params={"closed": "false", "tag_slug": tag_slug, "limit": 100, "order": "volume24hr", "ascending": "false"},
        timeout=20,
    )
    r.raise_for_status()
    games = []
    for ev in r.json():
        title = ev.get("title", "")
        if " vs. " not in title:
            continue
        home_title, away_title = [t.strip() for t in title.split(" vs. ", 1)]
        market = next((m for m in ev.get("markets", []) if m.get("question") == title), None)
        if market is None:
            continue
        try:
            outcomes = json.loads(market["outcomes"])
            prices = json.loads(market["outcomePrices"])
        except (KeyError, ValueError, TypeError):
            continue
        if outcomes[:2] != [home_title, away_title] or len(prices) < 2:
            continue
        games.append(
            {
                "slug": ev["slug"],
                "home_title": home_title,
                "away_title": away_title,
                "volume": ev.get("volume", 0),
                "prices": {"home": float(prices[0]), "away": float(prices[1])},
            }
        )
    return games


def _find_2way_match(poly_games, om):
    # the-odds-api и Polymarket не гарантированно указывают команды в одном
    # порядке (дома/в гостях) — пробуем оба варианта, а не только прямой.
    direct = next(
        (g for g in poly_games if teams_match(g["home_title"], om["home_team"]) and teams_match(g["away_title"], om["away_team"])),
        None,
    )
    if direct is not None:
        return direct, False
    reversed_match = next(
        (g for g in poly_games if teams_match(g["home_title"], om["away_team"]) and teams_match(g["away_title"], om["home_team"])),
        None,
    )
    return (reversed_match, True) if reversed_match is not None else (None, False)


def ensure_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sports_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            league TEXT,
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
    if not ODDS_API_KEY:
        print("ODDS_API_KEY не задан (.env) — нечем сравнивать", file=sys.stderr)
        return

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    ensure_schema(conn)
    now = datetime.now(timezone.utc).isoformat()

    try:
        poly_games = fetch_polymarket_soccer()
    except requests.RequestException as e:
        print(f"Polymarket: ошибка запроса — {e}", file=sys.stderr)
        conn.close()
        return
    print(f"Polymarket: {len(poly_games)} футбольных матчей с полным 1X2")

    rows = []
    matched_count = 0
    last_remaining = None
    for sport_key, league in SPORTS.items():
        try:
            odds_matches, remaining = fetch_pinnacle_odds(sport_key)
            last_remaining = remaining
        except requests.RequestException as e:
            print(f"{league}: ошибка odds-api — {e}", file=sys.stderr)
            continue

        for om in odds_matches:
            # Только матчи, которые ещё не начались: после стартового свистка
            # цена Polymarket уже отражает реальный ход игры, а коэффициент
            # Pinnacle отсюда — предматчевый и устаревший. Сравнивать их
            # после начала — не находить edge, а путать факт с прогнозом
            # (та же ловушка, что уже задокументирована в gold-sim).
            commence = datetime.fromisoformat(om["commence_time"].replace("Z", "+00:00"))
            if commence <= datetime.now(timezone.utc):
                continue
            pg = next(
                (
                    g
                    for g in poly_games
                    if teams_match(g["home_title"], om["home_team"]) and teams_match(g["away_title"], om["away_team"])
                ),
                None,
            )
            if pg is None:
                continue
            matched_count += 1
            for outcome in ("home", "draw", "away"):
                market_p = pg["prices"][outcome]
                pin_p = om["novig"][outcome]
                edge = pin_p - market_p
                rows.append(
                    (now, league, om["home_team"], om["away_team"], om["commence_time"], outcome,
                     market_p, pin_p, edge, pg["volume"], pg["slug"])
                )
            print(f"{league}: {om['home_team']} vs {om['away_team']} — сматчено, "
                  f"макс |edge|={max(abs(pin_p - pg['prices'][o]) for o, pin_p in om['novig'].items()):.3f}")

    for sport_key, (league, tag_slug) in SPORTS_2WAY.items():
        try:
            poly_games = fetch_polymarket_2way(tag_slug)
        except requests.RequestException as e:
            print(f"{league}: ошибка Polymarket — {e}", file=sys.stderr)
            continue
        try:
            odds_matches, remaining = fetch_pinnacle_odds(sport_key)
            last_remaining = remaining
        except requests.RequestException as e:
            print(f"{league}: ошибка odds-api — {e}", file=sys.stderr)
            continue

        for om in odds_matches:
            commence = datetime.fromisoformat(om["commence_time"].replace("Z", "+00:00"))
            if commence <= datetime.now(timezone.utc):
                continue
            pg, swapped = _find_2way_match(poly_games, om)
            if pg is None:
                continue
            matched_count += 1
            best_edge = 0.0
            for outcome in ("home", "away"):
                om_key = ("away" if outcome == "home" else "home") if swapped else outcome
                market_p = pg["prices"][outcome]
                pin_p = om["novig"][om_key]
                edge = pin_p - market_p
                best_edge = max(best_edge, abs(edge))
                rows.append(
                    (now, league, om["home_team"], om["away_team"], om["commence_time"], outcome,
                     market_p, pin_p, edge, pg["volume"], pg["slug"])
                )
            print(f"{league}: {om['home_team']} vs {om['away_team']} — сматчено, макс |edge|={best_edge:.3f}")

    if rows:
        conn.executemany(
            """
            INSERT INTO sports_snapshots
            (ts_utc, league, home_team, away_team, commence_time, outcome, market_p, pinnacle_p, edge, event_vol, poly_slug)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()

    print(f"Сматчено матчей: {matched_count}, записано строк: {len(rows)}, "
          f"остаток запросов the-odds-api: {last_remaining}")
    conn.close()


if __name__ == "__main__":
    run()
