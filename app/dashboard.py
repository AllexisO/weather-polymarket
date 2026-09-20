"""
Веб-дашборд поверх sqlite, который пишет weather_edge.py по крону.
Только чтение, ничего не торгует. Порт 8093, чтобы не пересекаться с
gold-sim (8090-8092).
"""

import json
import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# 2026-08-25: до этой отметки CITIES в weather_edge.py указывал на центр
# города, а не на станцию, по которой Polymarket реально резолвит маркет
# (аэропорт LaGuardia для NYC и т.д. — см. комментарий в weather_edge.py).
# Снимки/факты до фикса сравнивали модель не с той точкой на карте — не
# честная калибровка модели, а баг в сборе. В расчёт калибровки не берём,
# но из sqlite не удаляем — это свидетельство самого бага, не мусор.
WEATHER_COORD_FIX_TS = "2026-08-25T19:58:27+00:00"

app = FastAPI(title="polymarket-lab dashboard")


def db():
    # Только чтение: cron пишет отдельным короткоживущим контейнером,
    # долгих блокировок не бывает, отдельный write-lock тут не нужен.
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def unit_symbol(city_rows):
    return "°F" if city_rows and city_rows[0]["unit"] == "fahrenheit" else "°C"


def table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    conn = db()
    cities = [r["city"] for r in conn.execute("SELECT DISTINCT city FROM snapshots ORDER BY city")]

    cards = []
    for city in cities:
        latest_ts = conn.execute(
            "SELECT MAX(ts_utc) AS ts FROM snapshots WHERE city = ?", (city,)
        ).fetchone()["ts"]
        rows = conn.execute(
            """
            SELECT * FROM snapshots
            WHERE city = ? AND ts_utc = ?
            ORDER BY bucket_lo
            """,
            (city, latest_ts),
        ).fetchall()
        if not rows:
            continue
        best = max(rows, key=lambda r: abs(r["edge"]))
        cards.append(
            {
                "city": city,
                "local_date": rows[0]["local_date"],
                "local_hour": rows[0]["local_hour"],
                "unit": unit_symbol(rows),
                "best_edge": best["edge"],
                "best_lo": best["bucket_lo"],
                "best_hi": best["bucket_hi"],
                "market_p": best["market_p"],
                "model_p": best["model_p"],
                "event_vol": rows[0]["event_vol"],
                "n_snapshots": conn.execute(
                    "SELECT COUNT(DISTINCT ts_utc) AS n FROM snapshots WHERE city = ?", (city,)
                ).fetchone()["n"],
            }
        )
    weather_results = []
    if table_exists(conn, "weather_outcomes"):
        weather_results = compute_weather_results(conn)

    conn.close()
    cards.sort(key=lambda c: abs(c["best_edge"]), reverse=True)
    return TEMPLATES.TemplateResponse(
        "index.html", {"request": request, "cards": cards, "weather_results": weather_results}
    )


@app.get("/city/{city}", response_class=HTMLResponse)
def city_detail(request: Request, city: str):
    conn = db()
    latest_ts = conn.execute(
        "SELECT MAX(ts_utc) AS ts FROM snapshots WHERE city = ?", (city,)
    ).fetchone()["ts"]
    buckets = conn.execute(
        """
        SELECT * FROM snapshots WHERE city = ? AND ts_utc = ? ORDER BY bucket_lo
        """,
        (city, latest_ts),
    ).fetchall()

    history = conn.execute(
        """
        SELECT ts_utc, local_date, local_hour,
               MAX(ABS(edge)) AS max_abs_edge
        FROM snapshots
        WHERE city = ?
        GROUP BY ts_utc
        ORDER BY ts_utc DESC
        LIMIT 100
        """,
        (city,),
    ).fetchall()
    conn.close()

    return TEMPLATES.TemplateResponse(
        "city.html",
        {
            "request": request,
            "city": city,
            "unit": unit_symbol(buckets),
            "buckets": buckets,
            "history": history,
        },
    )


def _one_snapshot_per_day(rows):
    # 2026-08-31: раньше группировали по (city, local_date, ts_utc) —
    # то есть КАЖДЫЙ снимок в течение дня считался отдельным "случаем".
    # Крон дёргает коллектор каждые 2 часа, так что один день давал
    # 5-6 сильно скоррелированных строк подряд (тот же факт, почти тот
    # же прогноз) — n был раздут в разы, а не отражал число реально
    # независимых проверенных дней. Берём только САМЫЙ РАННИЙ снимок
    # дня — как уже делает compute_weather_results для /results.
    by_day = {}
    for r in rows:
        key = (r["city"], r["local_date"])
        by_day.setdefault(key, []).append(r)
    groups = {}
    for key, day_rows in by_day.items():
        first_ts = min(r["ts_utc"] for r in day_rows)
        groups[key] = [r for r in day_rows if r["ts_utc"] == first_ts]
    return groups


def compute_weather_calibration(rows, model_field="model_p"):
    groups = _one_snapshot_per_day(rows)
    n = 0
    model_hits = 0
    market_hits = 0
    for grp in groups.values():
        actual = grp[0]["actual_max"]
        model_pick = max(grp, key=lambda r: r[model_field])
        market_pick = max(grp, key=lambda r: r["market_p"])
        n += 1
        if model_pick["bucket_lo"] < actual <= model_pick["bucket_hi"]:
            model_hits += 1
        if market_pick["bucket_lo"] < actual <= market_pick["bucket_hi"]:
            market_hits += 1
    return {
        "n": n,
        "model_hit_rate": model_hits / n if n else None,
        "market_hit_rate": market_hits / n if n else None,
    }


def compute_weather_bias(rows):
    # Средний промах в градусах (факт минус середина топ-бакета), отдельно
    # для модели и для рынка. Открытые "служебные" бакеты ("35.5° и выше",
    # "26.5° и ниже") хранятся как (-999, X) / (X, 999) — их середина не
    # температура, а мусорное число, поэтому такие топ-пики в промах не
    # включаем (иначе один такой снимок утаскивает среднее в минус/плюс
    # сотни градусов, как уже случилось при ручном разборе). Один день —
    # один случай (самый ранний снимок), см. _one_snapshot_per_day.
    groups = _one_snapshot_per_day(rows)

    per_city = {}
    for grp in groups.values():
        city = grp[0]["city"]
        unit = grp[0]["unit"]
        actual = grp[0]["actual_max"]
        model_pick = max(grp, key=lambda r: r["model_p"])
        market_pick = max(grp, key=lambda r: r["market_p"])
        c = per_city.setdefault(
            city, {"unit": unit, "n": 0, "model_hits": 0, "market_hits": 0, "model_bias": [], "market_bias": []}
        )
        c["n"] += 1
        if model_pick["bucket_lo"] < actual <= model_pick["bucket_hi"]:
            c["model_hits"] += 1
        if market_pick["bucket_lo"] < actual <= market_pick["bucket_hi"]:
            c["market_hits"] += 1
        if model_pick["bucket_lo"] > -900 and model_pick["bucket_hi"] < 900:
            c["model_bias"].append(actual - (model_pick["bucket_lo"] + model_pick["bucket_hi"]) / 2)
        if market_pick["bucket_lo"] > -900 and market_pick["bucket_hi"] < 900:
            c["market_bias"].append(actual - (market_pick["bucket_lo"] + market_pick["bucket_hi"]) / 2)

    by_city = []
    model_bias_c, market_bias_c = [], []
    for city, c in sorted(per_city.items()):
        to_c = (lambda v: v * 5 / 9) if c["unit"] == "fahrenheit" else (lambda v: v)
        avg_model = sum(c["model_bias"]) / len(c["model_bias"]) if c["model_bias"] else None
        avg_market = sum(c["market_bias"]) / len(c["market_bias"]) if c["market_bias"] else None
        by_city.append(
            {
                "city": city,
                "unit_symbol": "°F" if c["unit"] == "fahrenheit" else "°C",
                "n": c["n"],
                "model_hit_rate": c["model_hits"] / c["n"],
                "market_hit_rate": c["market_hits"] / c["n"],
                "avg_model_bias": avg_model,
                "avg_market_bias": avg_market,
            }
        )
        if avg_model is not None:
            model_bias_c.append(to_c(avg_model))
        if avg_market is not None:
            market_bias_c.append(to_c(avg_market))

    return {
        "by_city": by_city,
        "overall_model_bias_c": sum(model_bias_c) / len(model_bias_c) if model_bias_c else None,
        "overall_market_bias_c": sum(market_bias_c) / len(market_bias_c) if market_bias_c else None,
    }


def compute_market_gap_stats(conn, table):
    # Для kalshi_snapshots/esports_snapshots резолвера фактов пока нет —
    # это фаза диагностики (см. CLAUDE.md): просто смотрим, насколько
    # большие расхождения между двумя рынками бывают и как часто, а не
    # "кто оказался прав". "Заметным" считаем расхождение от 3 п.п.
    rows = conn.execute(f"SELECT edge FROM {table}").fetchall()  # nosec: table — литерал, не ввод пользователя
    if not rows:
        return None
    edges = [abs(r["edge"]) for r in rows]
    n = len(edges)
    notable = sum(1 for e in edges if e >= 0.03)
    return {
        "n": n,
        "avg_abs_edge": sum(edges) / n,
        "max_abs_edge": max(edges),
        "notable_n": notable,
        "notable_share": notable / n,
    }


def compute_sports_calibration(rows):
    # 2026-09-18: тот же класс бага, что уже чинили в погоде
    # (_one_snapshot_per_day, см. CLAUDE.md, "Баг с задвоенным n") —
    # группировка по (матч, ts_utc) считала КАЖДЫЙ снимок матча отдельным
    # случаем. Футбол собирается 2 раза в день, киберспорт — раз в 2 дня;
    # матч, который ещё не начался к следующему прогону, снимается снова —
    # почти не изменившаяся линия Pinnacle/рынка считалась отдельным
    # "случаем" до 14 раз на одном и том же матче. Обнаружено при добавлении
    # esports_resolve.py, задним числом раздувало и футбольную калибровку
    # (159 уникальных матчей вместо якобы 480 "случаев"). Берём только
    # САМЫЙ ПОЗДНИЙ снимок перед матчем на матч — так же, как уже делает
    # compute_sports_results.
    by_match = {}
    for r in rows:
        key = (r["home_team"], r["away_team"], r["commence_time"])
        by_match.setdefault(key, []).append(r)

    n = 0
    pinnacle_hits = 0
    market_hits = 0
    for match_rows in by_match.values():
        last_ts = max(r["ts_utc"] for r in match_rows)
        grp = [r for r in match_rows if r["ts_utc"] == last_ts]
        actual = grp[0]["actual_outcome"]
        market_pick = max(grp, key=lambda r: r["market_p"])
        pinnacle_pick = max(grp, key=lambda r: r["pinnacle_p"])
        n += 1
        if market_pick["outcome"] == actual:
            market_hits += 1
        if pinnacle_pick["outcome"] == actual:
            pinnacle_hits += 1
    return {
        "n": n,
        "pinnacle_hit_rate": pinnacle_hits / n if n else None,
        "market_hit_rate": market_hits / n if n else None,
    }


def compute_news_validation(conn):
    # Пункт 2 из разговора с Alex: не "опередила ли новость рынок по
    # времени" (это отдельная, более сложная проверка — пункт 1), а проще —
    # "была ли новость вообще права по сути". Берём последний по времени
    # сигнал на каждую пару (команда, матч) — чтобы не считать один и тот
    # же найденный факт несколько раз просто потому, что новостной сборщик
    # находил его снова каждый день. Сравниваем факт (выиграла команда или
    # нет) с вероятностью, которую в неё закладывали Pinnacle/рынок В
    # ПОСЛЕДНЕМ снимке перед матчем — если "ослабляет" команды систематически
    # выигрывают реже, чем в них верили, сигнал что-то ловит по делу.
    rows = conn.execute(
        """
        SELECT n.ts_utc, n.home_team, n.away_team, n.commence_time, n.team, n.side, n.changes,
               o.actual_outcome
        FROM sports_news_signal n
        JOIN sports_outcomes o
          ON n.home_team = o.home_team AND n.away_team = o.away_team AND n.commence_time = o.commence_time
        WHERE n.severity != 'none'
        ORDER BY n.ts_utc
        """
    ).fetchall()

    latest = {}
    for r in rows:
        key = (r["home_team"], r["away_team"], r["commence_time"], r["team"])
        latest[key] = r  # строки идут по возрастанию ts_utc — последняя запись побеждает

    buckets = {"weakens": [], "strengthens": []}
    skipped_mixed = 0
    for (home_team, away_team, commence_time, team), r in latest.items():
        try:
            changes = json.loads(r["changes"]) if r["changes"] else []
        except (json.JSONDecodeError, TypeError):
            changes = []
        effects = {c.get("effect") for c in changes if c.get("effect") in ("weakens", "strengthens")}
        if len(effects) != 1:
            skipped_mixed += 1
            continue
        net_effect = next(iter(effects))

        snap = conn.execute(
            """
            SELECT market_p, pinnacle_p FROM sports_snapshots
            WHERE home_team = ? AND away_team = ? AND commence_time = ? AND outcome = ?
            ORDER BY ts_utc DESC LIMIT 1
            """,
            (home_team, away_team, commence_time, r["side"]),
        ).fetchone()
        if snap is None:
            continue

        actual_win = 1 if r["actual_outcome"] == r["side"] else 0
        buckets[net_effect].append(
            {
                "team": team,
                "home_team": home_team,
                "away_team": away_team,
                "actual_win": actual_win,
                "market_p": snap["market_p"],
                "pinnacle_p": snap["pinnacle_p"],
                "ts_utc": r["ts_utc"],
            }
        )

    def summarize(items):
        n = len(items)
        if n == 0:
            return {"n": 0, "avg_actual_win_rate": None, "avg_market_p": None, "diff_market": None, "items": []}
        avg_actual = sum(i["actual_win"] for i in items) / n
        avg_market = sum(i["market_p"] for i in items) / n
        return {
            "n": n,
            "avg_actual_win_rate": avg_actual,
            "avg_market_p": avg_market,
            "diff_market": avg_actual - avg_market,
            "items": items,
        }

    # combined_score: и для "ослабляет", и для "усиливает" — положительное
    # число значит "сигнал подтвердился" (у ослабленных факт хуже рынка,
    # у усиленных — лучше). Знак у "ослабляет" переворачиваем, чтобы обе
    # категории читались в одну сторону на одной шкале.
    combined = [-(i["actual_win"] - i["market_p"]) for i in buckets["weakens"]]
    combined += [(i["actual_win"] - i["market_p"]) for i in buckets["strengthens"]]

    return {
        "weakens": summarize(buckets["weakens"]),
        "strengthens": summarize(buckets["strengthens"]),
        "skipped_mixed": skipped_mixed,
        "combined_n": len(combined),
        "combined_score": sum(combined) / len(combined) if combined else None,
    }


def compute_news_timing(conn):
    # Пункт 1: опередила ли новость движение цены рынка, а не только "была
    # ли она в целом права" (это пункт 2 выше). Берём САМЫЙ РАННИЙ момент,
    # когда мы поймали конкретный сигнал (team+match+направление), и
    # сравниваем цену этой команды в последнем снимке ДО этого момента и в
    # первом снимке ПОСЛЕ. Если цена сдвинулась в сторону, которую
    # предсказывал сигнал, именно между этими двумя снимками — значит,
    # движение (если оно вообще было) случилось уже после того, как мы
    # заметили новость, а не до. Разрешение грубое (снимки цены — 2 раза
    # в день, сигнал — 1 раз в день), точнее сейчас всё равно не измерить.
    rows = conn.execute(
        """
        SELECT n.ts_utc, n.home_team, n.away_team, n.commence_time, n.team, n.side, n.changes
        FROM sports_news_signal n
        WHERE n.severity != 'none'
        ORDER BY n.ts_utc
        """
    ).fetchall()

    earliest = {}
    for r in rows:
        try:
            changes = json.loads(r["changes"]) if r["changes"] else []
        except (json.JSONDecodeError, TypeError):
            changes = []
        effects = {c.get("effect") for c in changes if c.get("effect") in ("weakens", "strengthens")}
        if len(effects) != 1:
            continue
        net_effect = next(iter(effects))
        key = (r["home_team"], r["away_team"], r["commence_time"], r["team"], r["side"], net_effect)
        earliest.setdefault(key, r["ts_utc"])  # первая по возрастанию ts_utc запись побеждает

    buckets = {"weakens": [], "strengthens": []}
    for (home_team, away_team, commence_time, team, side, net_effect), t_signal in earliest.items():
        before = conn.execute(
            """
            SELECT market_p FROM sports_snapshots
            WHERE home_team = ? AND away_team = ? AND commence_time = ? AND outcome = ? AND ts_utc < ?
            ORDER BY ts_utc DESC LIMIT 1
            """,
            (home_team, away_team, commence_time, side, t_signal),
        ).fetchone()
        after = conn.execute(
            """
            SELECT market_p FROM sports_snapshots
            WHERE home_team = ? AND away_team = ? AND commence_time = ? AND outcome = ? AND ts_utc > ?
            ORDER BY ts_utc ASC LIMIT 1
            """,
            (home_team, away_team, commence_time, side, t_signal),
        ).fetchone()
        if before is None or after is None:
            continue
        buckets[net_effect].append(after["market_p"] - before["market_p"])

    def summarize(moves):
        n = len(moves)
        if n == 0:
            return {"n": 0, "avg_move": None}
        return {"n": n, "avg_move": sum(moves) / n}

    combined = [-m for m in buckets["weakens"]] + list(buckets["strengthens"])

    return {
        "weakens": summarize(buckets["weakens"]),
        "strengthens": summarize(buckets["strengthens"]),
        "combined_n": len(combined),
        "combined_score": sum(combined) / len(combined) if combined else None,
    }


NEWS_MIN_N = 20
NEWS_MIN_GAP = 0.10


def news_verdict(combined_score, n):
    if n < NEWS_MIN_N or combined_score is None:
        return {"tone": "insufficient", "label": "мало данных"}
    if combined_score > NEWS_MIN_GAP:
        return {"tone": "good", "label": "сигнал подтверждается"}
    if combined_score < -NEWS_MIN_GAP:
        return {"tone": "bad", "label": "сигнал в обратную сторону"}
    return {"tone": "neutral", "label": "не подтверждается"}


def format_bucket(lo, hi, unit_symbol):
    if lo <= -900:
        return f"до {hi}{unit_symbol}"
    if hi >= 900:
        return f"от {lo}{unit_symbol}"
    return f"{lo}–{hi}{unit_symbol}"


def row_verdict(source_hit, market_hit):
    # Построчный вердикт для /results — конкретный случай, не проценты.
    if source_hit and not market_hit:
        return {"tone": "good", "label": "мы правы, рынок ошибся"}
    if market_hit and not source_hit:
        return {"tone": "bad", "label": "рынок прав, мы ошиблись"}
    if source_hit and market_hit:
        return {"tone": "neutral", "label": "оба правы"}
    return {"tone": "insufficient", "label": "оба мимо"}


def compute_weather_results(conn):
    rows = conn.execute(
        """
        SELECT s.ts_utc, s.city, s.local_date, s.local_hour, s.unit, s.bucket_lo, s.bucket_hi,
               s.market_p, s.model_p, o.actual_max
        FROM snapshots s
        JOIN weather_outcomes o ON s.city = o.city AND s.local_date = o.local_date
        WHERE s.ts_utc >= ? AND s.local_hour < 12
        ORDER BY s.city, s.local_date, s.ts_utc
        """,
        (WEATHER_COORD_FIX_TS,),
    ).fetchall()

    groups = {}
    for r in rows:
        key = (r["city"], r["local_date"])
        groups.setdefault(key, []).append(r)

    results = []
    for (city, local_date), grp in groups.items():
        first_ts = grp[0]["ts_utc"]
        first_grp = [r for r in grp if r["ts_utc"] == first_ts]
        actual = first_grp[0]["actual_max"]
        unit_symbol = "°F" if first_grp[0]["unit"] == "fahrenheit" else "°C"
        model_pick = max(first_grp, key=lambda r: r["model_p"])
        market_pick = max(first_grp, key=lambda r: r["market_p"])
        model_hit = model_pick["bucket_lo"] < actual <= model_pick["bucket_hi"]
        market_hit = market_pick["bucket_lo"] < actual <= market_pick["bucket_hi"]
        results.append(
            {
                "city": city,
                "local_date": local_date,
                "local_hour": first_grp[0]["local_hour"],
                "actual": actual,
                "unit": unit_symbol,
                "model_range": format_bucket(model_pick["bucket_lo"], model_pick["bucket_hi"], unit_symbol),
                "market_range": format_bucket(market_pick["bucket_lo"], market_pick["bucket_hi"], unit_symbol),
                "verdict": row_verdict(model_hit, market_hit),
            }
        )
    results.sort(key=lambda r: (r["local_date"], r["city"]), reverse=True)
    return results


def compute_sports_results(conn):
    rows = conn.execute(
        """
        SELECT s.ts_utc, s.league, s.home_team, s.away_team, s.commence_time,
               s.outcome, s.market_p, s.pinnacle_p, o.actual_outcome
        FROM sports_snapshots s
        JOIN sports_outcomes o
          ON s.home_team = o.home_team AND s.away_team = o.away_team AND s.commence_time = o.commence_time
        ORDER BY s.home_team, s.away_team, s.commence_time, s.ts_utc
        """
    ).fetchall()

    groups = {}
    for r in rows:
        key = (r["home_team"], r["away_team"], r["commence_time"])
        groups.setdefault(key, []).append(r)

    outcome_ru = {"home": "победа хозяев", "draw": "ничья", "away": "победа гостей"}
    results = []
    for (home_team, away_team, commence_time), grp in groups.items():
        last_ts = grp[-1]["ts_utc"]
        last_grp = [r for r in grp if r["ts_utc"] == last_ts]
        actual = last_grp[0]["actual_outcome"]
        market_pick = max(last_grp, key=lambda r: r["market_p"])
        pinnacle_pick = max(last_grp, key=lambda r: r["pinnacle_p"])
        results.append(
            {
                "league": last_grp[0]["league"],
                "home_team": home_team,
                "away_team": away_team,
                "commence_time": commence_time,
                "actual": outcome_ru.get(actual, actual),
                "pinnacle_pick": outcome_ru.get(pinnacle_pick["outcome"], pinnacle_pick["outcome"]),
                "market_pick": outcome_ru.get(market_pick["outcome"], market_pick["outcome"]),
                "verdict": row_verdict(pinnacle_pick["outcome"] == actual, market_pick["outcome"] == actual),
            }
        )
    results.sort(key=lambda r: r["commence_time"], reverse=True)
    return results


def compute_esports_results(conn):
    # Та же логика, что compute_sports_results, но без ничьей (в BOn её не
    # бывает) и без лиги — вместо неё "игра" (CS2/Dota 2/LoL/Valorant).
    rows = conn.execute(
        """
        SELECT s.ts_utc, s.game, s.home_team, s.away_team, s.commence_time,
               s.outcome, s.market_p, s.pinnacle_p, o.actual_outcome
        FROM esports_snapshots s
        JOIN esports_outcomes o
          ON s.home_team = o.home_team AND s.away_team = o.away_team AND s.commence_time = o.commence_time
        ORDER BY s.home_team, s.away_team, s.commence_time, s.ts_utc
        """
    ).fetchall()

    groups = {}
    for r in rows:
        key = (r["home_team"], r["away_team"], r["commence_time"])
        groups.setdefault(key, []).append(r)

    outcome_ru = {"home": "победа хозяина", "away": "победа гостя"}
    results = []
    for (home_team, away_team, commence_time), grp in groups.items():
        last_ts = grp[-1]["ts_utc"]
        last_grp = [r for r in grp if r["ts_utc"] == last_ts]
        actual = last_grp[0]["actual_outcome"]
        market_pick = max(last_grp, key=lambda r: r["market_p"])
        pinnacle_pick = max(last_grp, key=lambda r: r["pinnacle_p"])
        results.append(
            {
                "game": last_grp[0]["game"],
                "home_team": home_team,
                "away_team": away_team,
                "commence_time": commence_time,
                "actual": outcome_ru.get(actual, actual),
                "pinnacle_pick": outcome_ru.get(pinnacle_pick["outcome"], pinnacle_pick["outcome"]),
                "market_pick": outcome_ru.get(market_pick["outcome"], market_pick["outcome"]),
                "verdict": row_verdict(pinnacle_pick["outcome"] == actual, market_pick["outcome"] == actual),
            }
        )
    results.sort(key=lambda r: r["commence_time"], reverse=True)
    return results


# Пороги для вердикта на /status — осознанно консервативные, чтобы не
# выдавать "опережаем" на шуме. n меньше MIN_N — вообще не судим, разница
# меньше MIN_GAP п.п. — считаем "наравне", даже если один процент выше
# другого: на такой выборке это ничего не значит.
VERDICT_MIN_N = 30
VERDICT_MIN_GAP = 0.05

# Макро резолвится раз в месяц на метрику (5 метрик = ~5 случаев/месяц) —
# при VERDICT_MIN_N=30 вердикта пришлось бы ждать полгода. Порог ниже,
# потому что это неизбежное следствие частоты резолва, а не поблажка себе.
MACRO_MIN_N = 10


def verdict(source_rate, market_rate, n, min_n=VERDICT_MIN_N):
    if n is None or n == 0 or source_rate is None or market_rate is None:
        return {"tone": "none", "label": "нет данных"}
    if n < min_n:
        return {"tone": "insufficient", "label": "мало данных"}
    gap = source_rate - market_rate
    if abs(gap) < VERDICT_MIN_GAP:
        return {"tone": "neutral", "label": "наравне с рынком"}
    if gap > 0:
        return {"tone": "good", "label": "опережаем рынок"}
    return {"tone": "bad", "label": "отстаём от рынка"}


@app.get("/status", response_class=HTMLResponse)
def status(request: Request):
    conn = db()

    weather_card = {"verdict": verdict(None, None, 0), "n": 0, "source_rate": None, "market_rate": None}
    if table_exists(conn, "weather_outcomes"):
        rows = conn.execute(
            """
            SELECT s.ts_utc, s.city, s.local_date, s.local_hour, s.unit, s.bucket_lo, s.bucket_hi,
                   s.market_p, s.model_p, o.actual_max
            FROM snapshots s
            JOIN weather_outcomes o ON s.city = o.city AND s.local_date = o.local_date
            WHERE s.ts_utc >= ? AND s.local_hour < 12
            """,
            (WEATHER_COORD_FIX_TS,),
        ).fetchall()
        stats = compute_weather_calibration(rows)
        weather_card = {
            "verdict": verdict(stats["model_hit_rate"], stats["market_hit_rate"], stats["n"]),
            "n": stats["n"],
            "source_rate": stats["model_hit_rate"],
            "market_rate": stats["market_hit_rate"],
        }

    sports_card = {"verdict": verdict(None, None, 0), "n": 0, "source_rate": None, "market_rate": None}
    if table_exists(conn, "sports_outcomes"):
        rows = conn.execute(
            """
            SELECT s.ts_utc, s.home_team, s.away_team, s.commence_time,
                   s.outcome, s.market_p, s.pinnacle_p, o.actual_outcome
            FROM sports_snapshots s
            JOIN sports_outcomes o
              ON s.home_team = o.home_team AND s.away_team = o.away_team AND s.commence_time = o.commence_time
            """
        ).fetchall()
        stats = compute_sports_calibration(rows)
        sports_card = {
            "verdict": verdict(stats["pinnacle_hit_rate"], stats["market_hit_rate"], stats["n"]),
            "n": stats["n"],
            "source_rate": stats["pinnacle_hit_rate"],
            "market_rate": stats["market_hit_rate"],
        }

    esports_card = {"verdict": verdict(None, None, 0), "n": 0, "source_rate": None, "market_rate": None}
    if table_exists(conn, "esports_outcomes"):
        rows = conn.execute(
            """
            SELECT s.ts_utc, s.home_team, s.away_team, s.commence_time,
                   s.outcome, s.market_p, s.pinnacle_p, o.actual_outcome
            FROM esports_snapshots s
            JOIN esports_outcomes o
              ON s.home_team = o.home_team AND s.away_team = o.away_team AND s.commence_time = o.commence_time
            """
        ).fetchall()
        stats = compute_sports_calibration(rows)
        esports_card = {
            "verdict": verdict(stats["pinnacle_hit_rate"], stats["market_hit_rate"], stats["n"]),
            "n": stats["n"],
            "source_rate": stats["pinnacle_hit_rate"],
            "market_rate": stats["market_hit_rate"],
        }

    macro_card = {"verdict": verdict(None, None, 0), "n": 0, "source_rate": None, "market_rate": None}
    if table_exists(conn, "macro_outcomes"):
        stats = compute_macro_calibration(conn)
        macro_card = {
            "verdict": verdict(stats["model_hit_rate"], stats["market_hit_rate"], stats["n"], min_n=MACRO_MIN_N),
            "n": stats["n"],
            "source_rate": stats["model_hit_rate"],
            "market_rate": stats["market_hit_rate"],
        }

    news_n = 0
    news_flagged = 0
    news_validation = {"combined_n": 0, "combined_score": None}
    news_card_verdict = news_verdict(None, 0)
    if table_exists(conn, "sports_news_signal"):
        news_n = conn.execute("SELECT COUNT(*) AS n FROM sports_news_signal").fetchone()["n"]
        news_flagged = conn.execute(
            "SELECT COUNT(*) AS n FROM sports_news_signal WHERE severity != 'none'"
        ).fetchone()["n"]
        if table_exists(conn, "sports_outcomes"):
            news_validation = compute_news_validation(conn)
            news_card_verdict = news_verdict(news_validation["combined_score"], news_validation["combined_n"])

    conn.close()
    return TEMPLATES.TemplateResponse(
        "status.html",
        {
            "request": request,
            "weather": weather_card,
            "sports": sports_card,
            "esports": esports_card,
            "macro": macro_card,
            "news_n": news_n,
            "news_flagged": news_flagged,
            "news_validation": news_validation,
            "news_verdict": news_card_verdict,
            "news_min_n": NEWS_MIN_N,
            "min_n": VERDICT_MIN_N,
            "macro_min_n": MACRO_MIN_N,
        },
    )


@app.get("/calibration", response_class=HTMLResponse)
def calibration(request: Request):
    conn = db()

    weather_stats = {"all": None, "early": None}
    weather_bias = None
    weather_pre_fix_n = 0
    if table_exists(conn, "weather_outcomes"):
        weather_pre_fix_n = conn.execute(
            """
            SELECT COUNT(*) AS n FROM snapshots s
            JOIN weather_outcomes o ON s.city = o.city AND s.local_date = o.local_date
            WHERE s.ts_utc < ?
            """,
            (WEATHER_COORD_FIX_TS,),
        ).fetchone()["n"]
        for label, extra_filter in (("all", ""), ("early", "AND s.local_hour < 12")):
            rows = conn.execute(
                f"""
                SELECT s.ts_utc, s.city, s.local_date, s.local_hour, s.unit, s.bucket_lo, s.bucket_hi,
                       s.market_p, s.model_p, o.actual_max
                FROM snapshots s
                JOIN weather_outcomes o ON s.city = o.city AND s.local_date = o.local_date
                WHERE s.ts_utc >= ? {extra_filter}
                """,
                (WEATHER_COORD_FIX_TS,),
            ).fetchall()
            weather_stats[label] = compute_weather_calibration(rows)
            if label == "early":
                weather_bias = compute_weather_bias(rows)

    wn2_stats = None
    if table_exists(conn, "weather_outcomes"):
        rows = conn.execute(
            """
            SELECT s.ts_utc, s.city, s.local_date, s.local_hour, s.unit, s.bucket_lo, s.bucket_hi,
                   s.market_p, s.wn2_model_p, o.actual_max
            FROM snapshots s
            JOIN weather_outcomes o ON s.city = o.city AND s.local_date = o.local_date
            WHERE s.local_hour < 12 AND s.wn2_model_p IS NOT NULL
            """
        ).fetchall()
        if rows:
            wn2_stats = compute_weather_calibration(rows, model_field="wn2_model_p")

    emos_stats = None
    if table_exists(conn, "weather_outcomes"):
        rows = conn.execute(
            """
            SELECT s.ts_utc, s.city, s.local_date, s.local_hour, s.unit, s.bucket_lo, s.bucket_hi,
                   s.market_p, s.emos_model_p, o.actual_max
            FROM snapshots s
            JOIN weather_outcomes o ON s.city = o.city AND s.local_date = o.local_date
            WHERE s.local_hour < 12 AND s.emos_model_p IS NOT NULL
            """
        ).fetchall()
        if rows:
            emos_stats = compute_weather_calibration(rows, model_field="emos_model_p")

    sports_stats = None
    if table_exists(conn, "sports_outcomes"):
        rows = conn.execute(
            """
            SELECT s.ts_utc, s.home_team, s.away_team, s.commence_time,
                   s.outcome, s.market_p, s.pinnacle_p, o.actual_outcome
            FROM sports_snapshots s
            JOIN sports_outcomes o
              ON s.home_team = o.home_team AND s.away_team = o.away_team AND s.commence_time = o.commence_time
            """
        ).fetchall()
        sports_stats = compute_sports_calibration(rows)

    news_validation = None
    news_timing = None
    if table_exists(conn, "sports_news_signal") and table_exists(conn, "sports_outcomes"):
        news_validation = compute_news_validation(conn)
    if table_exists(conn, "sports_news_signal"):
        news_timing = compute_news_timing(conn)

    kalshi_stats = None
    if table_exists(conn, "kalshi_snapshots"):
        kalshi_stats = compute_market_gap_stats(conn, "kalshi_snapshots")

    esports_stats = None
    if table_exists(conn, "esports_snapshots"):
        esports_stats = compute_market_gap_stats(conn, "esports_snapshots")

    esports_calibration = None
    if table_exists(conn, "esports_outcomes"):
        rows = conn.execute(
            """
            SELECT s.ts_utc, s.home_team, s.away_team, s.commence_time,
                   s.outcome, s.market_p, s.pinnacle_p, o.actual_outcome
            FROM esports_snapshots s
            JOIN esports_outcomes o
              ON s.home_team = o.home_team AND s.away_team = o.away_team AND s.commence_time = o.commence_time
            """
        ).fetchall()
        esports_calibration = compute_sports_calibration(rows)

    macro_calibration = None
    if table_exists(conn, "macro_outcomes"):
        macro_calibration = compute_macro_calibration(conn)

    musk_calibration = None
    if table_exists(conn, "musk_tweets_outcomes"):
        musk_calibration = compute_musk_calibration(conn)

    conn.close()
    return TEMPLATES.TemplateResponse(
        "calibration.html",
        {
            "request": request,
            "weather": weather_stats,
            "weather_bias": weather_bias,
            "weather_pre_fix_n": weather_pre_fix_n,
            "wn2_stats": wn2_stats,
            "emos_stats": emos_stats,
            "sports": sports_stats,
            "news_validation": news_validation,
            "news_timing": news_timing,
            "kalshi_stats": kalshi_stats,
            "esports_stats": esports_stats,
            "esports_calibration": esports_calibration,
            "macro_calibration": macro_calibration,
            "musk_calibration": musk_calibration,
        },
    )


@app.get("/news", response_class=HTMLResponse)
def news(request: Request):
    conn = db()
    rows = []
    if table_exists(conn, "sports_news_signal"):
        latest_ts = conn.execute("SELECT MAX(ts_utc) AS ts FROM sports_news_signal").fetchone()["ts"]
        if latest_ts:
            raw_rows = conn.execute(
                "SELECT * FROM sports_news_signal WHERE ts_utc = ? ORDER BY severity DESC, home_team",
                (latest_ts,),
            ).fetchall()
            for r in raw_rows:
                row = dict(r)
                try:
                    row["changes_parsed"] = json.loads(row["changes"]) if row["changes"] else []
                except (json.JSONDecodeError, TypeError):
                    row["changes_parsed"] = []
                rows.append(row)

    news_results = []
    if table_exists(conn, "sports_news_signal") and table_exists(conn, "sports_outcomes"):
        nv = compute_news_validation(conn)
        for i in nv["weakens"]["items"]:
            confirmed = i["actual_win"] == 0
            news_results.append(
                {
                    "team": i["team"],
                    "match": f"{i['home_team']} — {i['away_team']}",
                    "effect": "ослабляет",
                    "actual": "не выиграла" if i["actual_win"] == 0 else "выиграла",
                    "market_p": i["market_p"],
                    "ts_utc": i["ts_utc"],
                    "verdict": {"tone": "good", "label": "подтвердилось"} if confirmed
                    else {"tone": "bad", "label": "не подтвердилось"},
                }
            )
        for i in nv["strengthens"]["items"]:
            confirmed = i["actual_win"] == 1
            news_results.append(
                {
                    "team": i["team"],
                    "match": f"{i['home_team']} — {i['away_team']}",
                    "effect": "усиливает",
                    "actual": "не выиграла" if i["actual_win"] == 0 else "выиграла",
                    "market_p": i["market_p"],
                    "ts_utc": i["ts_utc"],
                    "verdict": {"tone": "good", "label": "подтвердилось"} if confirmed
                    else {"tone": "bad", "label": "не подтвердилось"},
                }
            )
        news_results.sort(key=lambda r: r["ts_utc"], reverse=True)

    conn.close()
    return TEMPLATES.TemplateResponse("news.html", {"request": request, "rows": rows, "news_results": news_results})


@app.get("/sports", response_class=HTMLResponse)
def sports(request: Request):
    conn = db()
    matches = []
    if table_exists(conn, "sports_snapshots"):
        latest_ts = conn.execute("SELECT MAX(ts_utc) AS ts FROM sports_snapshots").fetchone()["ts"]
        if latest_ts:
            rows = conn.execute(
                "SELECT * FROM sports_snapshots WHERE ts_utc = ? ORDER BY poly_slug", (latest_ts,)
            ).fetchall()
            by_slug = {}
            for r in rows:
                m = by_slug.setdefault(
                    r["poly_slug"],
                    {
                        "league": r["league"],
                        "home_team": r["home_team"],
                        "away_team": r["away_team"],
                        "commence_time": r["commence_time"],
                        "event_vol": r["event_vol"],
                        "outcomes": {},
                    },
                )
                m["outcomes"][r["outcome"]] = {"market_p": r["market_p"], "pinnacle_p": r["pinnacle_p"], "edge": r["edge"]}
            matches = list(by_slug.values())
            for m in matches:
                m["best_abs_edge"] = max(abs(o["edge"]) for o in m["outcomes"].values())
            matches.sort(key=lambda m: m["best_abs_edge"], reverse=True)

    sports_results = []
    if table_exists(conn, "sports_outcomes"):
        sports_results = compute_sports_results(conn)

    conn.close()
    return TEMPLATES.TemplateResponse(
        "sports.html", {"request": request, "matches": matches, "sports_results": sports_results}
    )


def compute_macro_calibration(conn):
    rows = conn.execute(
        """
        SELECT s.ts_utc, s.metric, s.target_period, s.bucket_lo, s.bucket_hi, s.market_p, s.model_p, o.actual_value
        FROM macro_snapshots s
        JOIN macro_outcomes o ON s.metric = o.metric AND s.target_period = o.target_period
        """
    ).fetchall()
    # Один резолвленный период может иметь несколько снимков (крон гоняет
    # macro_edge.py регулярно, пока маркет не закроется) — тот же класс
    # бага, что уже чинили у спорта: берём только последний снимок перед
    # резолвом на период, не все снимки разом.
    by_case = {}
    for r in rows:
        key = (r["metric"], r["target_period"])
        by_case.setdefault(key, []).append(r)

    n = 0
    model_hits = 0
    market_hits = 0
    for grp in by_case.values():
        last_ts = max(r["ts_utc"] for r in grp)
        latest = [r for r in grp if r["ts_utc"] == last_ts]
        actual = latest[0]["actual_value"]
        model_pick = max(latest, key=lambda r: r["model_p"])
        market_pick = max(latest, key=lambda r: r["market_p"])
        n += 1
        if model_pick["bucket_lo"] < actual <= model_pick["bucket_hi"]:
            model_hits += 1
        if market_pick["bucket_lo"] < actual <= market_pick["bucket_hi"]:
            market_hits += 1
    return {
        "n": n,
        "model_hit_rate": model_hits / n if n else None,
        "market_hit_rate": market_hits / n if n else None,
    }


def compute_macro_results(conn):
    rows = conn.execute(
        """
        SELECT s.ts_utc, s.metric, s.target_period, s.bucket_lo, s.bucket_hi, s.market_p, s.model_p,
               s.model_mean, s.model_std, o.actual_value
        FROM macro_snapshots s
        JOIN macro_outcomes o ON s.metric = o.metric AND s.target_period = o.target_period
        ORDER BY s.metric, s.target_period, s.ts_utc
        """
    ).fetchall()
    groups = {}
    for r in rows:
        key = (r["metric"], r["target_period"])
        groups.setdefault(key, []).append(r)

    results = []
    for (metric, target_period), grp in groups.items():
        last_ts = grp[-1]["ts_utc"]
        last_grp = [r for r in grp if r["ts_utc"] == last_ts]
        actual = last_grp[0]["actual_value"]
        model_mean = last_grp[0]["model_mean"]
        model_pick = max(last_grp, key=lambda r: r["model_p"])
        market_pick = max(last_grp, key=lambda r: r["market_p"])
        model_hit = model_pick["bucket_lo"] < actual <= model_pick["bucket_hi"]
        market_hit = market_pick["bucket_lo"] < actual <= market_pick["bucket_hi"]
        results.append(
            {
                "metric": METRIC_LABELS.get(metric, metric),
                "target_period": target_period,
                "model_mean": model_mean,
                "actual": actual,
                "verdict": row_verdict(model_hit, market_hit),
            }
        )
    results.sort(key=lambda r: r["target_period"], reverse=True)
    return results


METRIC_LABELS = {
    "cpi_annual": "CPI (годовая)",
    "core_cpi_yoy": "Core CPI (годовая)",
    "unemployment": "Безработица (U-3)",
    "jolts": "JOLTS (вакансии)",
    "gdp": "ВВП США (QoQ SAAR)",
}


@app.get("/macro", response_class=HTMLResponse)
def macro(request: Request):
    conn = db()
    metrics = []
    if table_exists(conn, "macro_snapshots"):
        rows = conn.execute(
            """
            SELECT s.* FROM macro_snapshots s
            INNER JOIN (
                SELECT metric, MAX(ts_utc) AS max_ts FROM macro_snapshots GROUP BY metric
            ) latest ON s.metric = latest.metric AND s.ts_utc = latest.max_ts
            ORDER BY s.metric, s.bucket_lo
            """
        ).fetchall()
        by_metric = {}
        for r in rows:
            m = by_metric.setdefault(
                r["metric"],
                {
                    "metric": METRIC_LABELS.get(r["metric"], r["metric"]),
                    "target_period": r["target_period"],
                    "model_mean": r["model_mean"],
                    "model_std": r["model_std"],
                    "event_vol": r["event_vol"],
                    "buckets": [],
                },
            )
            m["buckets"].append(
                {"lo": r["bucket_lo"], "hi": r["bucket_hi"], "market_p": r["market_p"], "model_p": r["model_p"], "edge": r["edge"]}
            )
        metrics = list(by_metric.values())
        for m in metrics:
            m["best_abs_edge"] = max(abs(b["edge"]) for b in m["buckets"])

    macro_results = []
    if table_exists(conn, "macro_outcomes"):
        macro_results = compute_macro_results(conn)

    conn.close()
    return TEMPLATES.TemplateResponse(
        "macro.html", {"request": request, "metrics": metrics, "macro_results": macro_results}
    )


def compute_musk_calibration(conn):
    rows = conn.execute(
        """
        SELECT s.ts_utc, s.target_period, s.bucket_lo, s.bucket_hi, s.market_p, s.model_p, o.actual_value
        FROM musk_tweets_snapshots s
        JOIN musk_tweets_outcomes o ON s.target_period = o.target_period
        """
    ).fetchall()
    by_case = {}
    for r in rows:
        by_case.setdefault(r["target_period"], []).append(r)
    n = 0
    model_hits = 0
    market_hits = 0
    for grp in by_case.values():
        last_ts = max(r["ts_utc"] for r in grp)
        latest = [r for r in grp if r["ts_utc"] == last_ts]
        actual = latest[0]["actual_value"]
        model_pick = max(latest, key=lambda r: r["model_p"])
        market_pick = max(latest, key=lambda r: r["market_p"])
        n += 1
        if model_pick["bucket_lo"] < actual <= model_pick["bucket_hi"]:
            model_hits += 1
        if market_pick["bucket_lo"] < actual <= market_pick["bucket_hi"]:
            market_hits += 1
    return {"n": n, "model_hit_rate": model_hits / n if n else None, "market_hit_rate": market_hits / n if n else None}


@app.get("/musk", response_class=HTMLResponse)
def musk(request: Request):
    conn = db()
    buckets = []
    meta = None
    if table_exists(conn, "musk_tweets_snapshots"):
        latest_ts = conn.execute("SELECT MAX(ts_utc) AS ts FROM musk_tweets_snapshots").fetchone()["ts"]
        if latest_ts:
            rows = conn.execute(
                "SELECT * FROM musk_tweets_snapshots WHERE ts_utc = ? ORDER BY bucket_lo", (latest_ts,)
            ).fetchall()
            if rows:
                meta = {
                    "target_period": rows[0]["target_period"],
                    "actual_so_far": rows[0]["actual_so_far"],
                    "days_elapsed": rows[0]["days_elapsed"],
                    "days_remaining": rows[0]["days_remaining"],
                    "model_mean": rows[0]["model_mean"],
                    "model_std": rows[0]["model_std"],
                }
                for r in rows:
                    buckets.append(
                        {"lo": r["bucket_lo"], "hi": r["bucket_hi"], "market_p": r["market_p"],
                         "model_p": r["model_p"], "edge": r["edge"]}
                    )
    conn.close()
    return TEMPLATES.TemplateResponse("musk.html", {"request": request, "buckets": buckets, "meta": meta})


@app.get("/esports", response_class=HTMLResponse)
def esports(request: Request):
    conn = db()
    matches = []
    if table_exists(conn, "esports_snapshots"):
        latest_ts = conn.execute("SELECT MAX(ts_utc) AS ts FROM esports_snapshots").fetchone()["ts"]
        if latest_ts:
            rows = conn.execute(
                "SELECT * FROM esports_snapshots WHERE ts_utc = ? ORDER BY poly_slug", (latest_ts,)
            ).fetchall()
            by_slug = {}
            for r in rows:
                m = by_slug.setdefault(
                    r["poly_slug"],
                    {
                        "game": r["game"],
                        "home_team": r["home_team"],
                        "away_team": r["away_team"],
                        "commence_time": r["commence_time"],
                        "event_vol": r["event_vol"],
                        "outcomes": {},
                    },
                )
                m["outcomes"][r["outcome"]] = {"market_p": r["market_p"], "pinnacle_p": r["pinnacle_p"], "edge": r["edge"]}
            matches = list(by_slug.values())
            for m in matches:
                m["best_abs_edge"] = max(abs(o["edge"]) for o in m["outcomes"].values())
            matches.sort(key=lambda m: m["best_abs_edge"], reverse=True)

    esports_results = []
    if table_exists(conn, "esports_outcomes"):
        esports_results = compute_esports_results(conn)

    conn.close()
    return TEMPLATES.TemplateResponse(
        "esports.html", {"request": request, "matches": matches, "esports_results": esports_results}
    )


@app.get("/arbitrage", response_class=HTMLResponse)
def arbitrage(request: Request):
    conn = db()
    matches = []
    if table_exists(conn, "kalshi_snapshots"):
        latest_ts = conn.execute("SELECT MAX(ts_utc) AS ts FROM kalshi_snapshots").fetchone()["ts"]
        if latest_ts:
            rows = conn.execute(
                "SELECT * FROM kalshi_snapshots WHERE ts_utc = ? ORDER BY home_team", (latest_ts,)
            ).fetchall()
            by_game = {}
            for r in rows:
                m = by_game.setdefault(
                    (r["home_team"], r["away_team"], r["commence_time"]),
                    {
                        "league": r["league"],
                        "home_team": r["home_team"],
                        "away_team": r["away_team"],
                        "commence_time": r["commence_time"],
                        "outcomes": {},
                    },
                )
                m["outcomes"][r["outcome"]] = {"market_p": r["market_p"], "kalshi_p": r["kalshi_p"], "edge": r["edge"]}
            matches = list(by_game.values())
            for m in matches:
                m["best_abs_edge"] = max(abs(o["edge"]) for o in m["outcomes"].values())
            matches.sort(key=lambda m: m["best_abs_edge"], reverse=True)
    conn.close()
    return TEMPLATES.TemplateResponse("arbitrage.html", {"request": request, "matches": matches})
