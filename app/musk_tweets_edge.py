"""
Седьмая гипотеза (2026-09-19): число постов Илона Маска на X за месяц.
Эталон — реальный, объективно проверяемый счётчик, а не что-то, выведенное
из цены самого Polymarket маркета. Источник факта — xtracker.polymarket.com,
ОФИЦИАЛЬНЫЙ трекер самого Polymarket (у него есть публичный API без ключа,
`/docs` на самом сайте), которым эти же маркеты, судя по всему, и
резолвятся — значит методология подсчёта (что считается постом, что нет —
см. описание маркета: реплаи не считаются, кроме видимых в основной ленте)
там уже реализована правильно, повторять её вручную не нужно.

Это НЕ то же самое, что "цена = сигнал" (циркулярность крипто-маркетов):
уже случившиеся посты в течение месяца — это факт, который можно
объективно посчитать в любой момент, не выведенный из цены Polymarket.
Прогноз здесь — не что-то из будущего, а ПРОЕКЦИЯ уже известного темпа
постинга на оставшиеся дни месяца (то же самое, что делает GDPNow для ВВП
в macro_edge.py, только на своих данных).

Модель: дневной темп = среднее число постов в день за уже прошедшие дни
месяца (с começо месяца), разброс — эмпирическое стандартное отклонение
по тем же дням, спроецированное на оставшиеся дни (дисперсия дней
складывается, если считать дни независимыми — упрощение первой версии).
"""

import json
import math
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
GAMMA = "https://gamma-api.polymarket.com"
XTRACKER = "https://xtracker.polymarket.com/api"

MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}

RE_TITLE = re.compile(r"^Elon Musk # of tweets in (\w+) (\d{4})\?$")
RE_LESS = re.compile(r"less than (\d+) times")
RE_MORE = re.compile(r"(\d+) or more times")
RE_RANGE = re.compile(r"(\d+)-(\d+) times")


def parse_bucket(question):
    m = RE_LESS.search(question)
    if m:
        return -999.0, float(m.group(1)) - 0.5
    m = RE_MORE.search(question)
    if m:
        return float(m.group(1)) - 0.5, 999.0
    m = RE_RANGE.search(question)
    if m:
        return float(m.group(1)) - 0.5, float(m.group(2)) + 0.5
    return None


def add_months(y, m, n):
    m += n
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return y, m


def fetch_posts(start_date, end_date):
    r = requests.get(
        f"{XTRACKER}/users/elonmusk/posts",
        params={"platform": "X", "startDate": start_date, "endDate": end_date, "timezone": "EST"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(f"xtracker: {data.get('message')}")
    return data["data"]


def _normal_cdf(x, mean, std):
    if std <= 0:
        return 1.0 if x >= mean else 0.0
    return 0.5 * (1 + math.erf((x - mean) / (std * math.sqrt(2))))


def bucket_prob(mean, std, lo, hi):
    lo_cdf = 0.0 if lo <= -900 else _normal_cdf(lo, mean, std)
    hi_cdf = 1.0 if hi >= 900 else _normal_cdf(hi, mean, std)
    return max(0.0, hi_cdf - lo_cdf)


def ensure_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS musk_tweets_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            target_period TEXT NOT NULL,
            poly_slug TEXT,
            actual_so_far INTEGER,
            days_elapsed INTEGER,
            days_remaining INTEGER,
            bucket_lo REAL,
            bucket_hi REAL,
            market_p REAL,
            model_p REAL,
            model_mean REAL,
            model_std REAL,
            edge REAL,
            event_vol REAL
        )
        """
    )
    conn.commit()


def fetch_event():
    r = requests.get(
        f"{GAMMA}/events",
        params={"closed": "false", "tag_slug": "elon-musk", "limit": 100},
        timeout=20,
    )
    r.raise_for_status()
    for ev in r.json():
        if RE_TITLE.match(ev.get("title", "")):
            return ev
    return None


def run():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    ensure_schema(conn)
    now = datetime.now(timezone.utc)
    et_today = now.astimezone(ZoneInfo("America/New_York")).date()

    try:
        event = fetch_event()
    except requests.RequestException as e:
        print(f"Polymarket: ошибка запроса — {e}", file=sys.stderr)
        conn.close()
        return
    if event is None:
        print("Маркет 'Elon Musk # of tweets' не найден", file=sys.stderr)
        conn.close()
        return

    m = RE_TITLE.match(event["title"])
    month_name, year = m.group(1), int(m.group(2))
    month_num = MONTHS[month_name]
    month_start = f"{year:04d}-{month_num:02d}-01"
    next_y, next_m = add_months(year, month_num, 1)
    month_end_date = datetime(next_y, next_m, 1, tzinfo=ZoneInfo("America/New_York")).date() - timedelta(days=1)

    try:
        posts = fetch_posts(month_start, et_today.isoformat())
    except (requests.RequestException, RuntimeError) as e:
        print(f"xtracker: ошибка запроса — {e}", file=sys.stderr)
        conn.close()
        return

    # Дневные счётчики с начала месяца по календарным дням Восточного
    # времени (так же, как считает сам трекер — см. докстринг). Живая
    # находка: startDate/endDate у xtracker фильтруют не строго по ET —
    # один пост от 31 августа (по ET) проскочил через startDate=01.09.
    # Поэтому границу месяца перепроверяем сами после группировки, а не
    # доверяем фильтрации API.
    month_start_date = datetime(year, month_num, 1).date()
    daily_counts = {}
    for p in posts:
        dt = datetime.fromisoformat(p["createdAt"].replace("Z", "+00:00")).astimezone(ZoneInfo("America/New_York"))
        d = dt.date()
        if d < month_start_date or d > month_end_date:
            continue
        daily_counts.setdefault(d, 0)
        daily_counts[d] += 1

    actual_so_far = sum(daily_counts.values())
    # Сегодняшний (ещё не закончившийся) день не считаем законченным наблюдением —
    # иначе неполный день занизит средний темп.
    completed_days = [d for d in daily_counts if d < et_today]
    if len(completed_days) < 3:
        print(f"Недостаточно прошедших дней месяца ({len(completed_days)}) для прогноза", file=sys.stderr)
        conn.close()
        return

    counts = [daily_counts[d] for d in completed_days]
    daily_rate = sum(counts) / len(counts)
    mean_c = daily_rate
    daily_std = (sum((c - mean_c) ** 2 for c in counts) / len(counts)) ** 0.5

    days_remaining = (month_end_date - et_today).days  # сегодняшний день ещё идёт, его считаем отдельно ниже
    today_so_far = daily_counts.get(et_today, 0)
    # Ожидаемые оставшиеся посты: остаток сегодняшнего дня (по среднему темпу,
    # без учёта уже написанного сегодня — оно уже в actual_so_far) + полные
    # оставшиеся дни.
    projected_remaining = daily_rate * days_remaining
    model_mean = actual_so_far + projected_remaining
    # Дисперсия дней складывается (считаем дни условно независимыми —
    # упрощение первой версии, не учитывает автокорреляцию/vиральные серии).
    model_std = daily_std * (days_remaining ** 0.5) if days_remaining > 0 else max(daily_std, 1.0)

    buckets = []
    for mk in event.get("markets", []):
        q = mk.get("question", "")
        rng = parse_bucket(q)
        if rng is None:
            continue
        try:
            outcomes = json.loads(mk["outcomes"])
            prices = json.loads(mk["outcomePrices"])
            yes_p = float(prices[outcomes.index("Yes")])
        except (KeyError, ValueError, TypeError, IndexError):
            continue
        buckets.append({"lo": rng[0], "hi": rng[1], "market_p": yes_p})

    if not buckets:
        print("Не разобрались с бакетами маркета", file=sys.stderr)
        conn.close()
        return

    rows = []
    best_edge = 0.0
    for b in buckets:
        mp = bucket_prob(model_mean, model_std, b["lo"], b["hi"])
        edge = mp - b["market_p"]
        best_edge = max(best_edge, abs(edge))
        rows.append(
            (now.isoformat(), event["title"], event.get("slug"), actual_so_far, len(completed_days), days_remaining,
             b["lo"], b["hi"], b["market_p"], mp, model_mean, model_std, edge, event.get("volume", 0))
        )

    conn.executemany(
        """
        INSERT INTO musk_tweets_snapshots
        (ts_utc, target_period, poly_slug, actual_so_far, days_elapsed, days_remaining,
         bucket_lo, bucket_hi, market_p, model_p, model_mean, model_std, edge, event_vol)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    print(f"{event['title']}: уже {actual_so_far} постов ({len(completed_days)} полных дней, "
          f"темп {daily_rate:.1f}/день), прогноз на месяц {model_mean:.0f}±{model_std:.0f}, "
          f"макс |edge|={best_edge:.3f}")
    conn.close()


if __name__ == "__main__":
    run()
