"""
Шестая гипотеза (2026-09-19): макро-данные "по факту" — CPI (годовая и
Core), безработица, JOLTS, ВВП. Эталон, как и в погоде/спорте, НЕ цена
самого Polymarket: эти маркеты резолвятся реальным отчётом BLS/BEA (см.
описание маркета в Gamma API), в отличие от закрытых ранее Fed decision/
GOLD/NDX/SPX/EURUSD, где эталон был бы производным от цены актива/другого
рынка.

Разведка (см. CLAUDE.md) искала готовый профессиональный nowcast (Cleveland
Fed Inflation Nowcasting) — он существует, но сайт не отдаёт данные
программно без JS (нет CSV/JSON, только интерактивный виджет). Вместо
ожидания или скрейпинга через голову — считаем СВОЙ, намеренно простой
nowcast прямо здесь, на бесплатных данных FRED (fredgraph.csv, без ключа,
без регистрации):

- CPI Annual / Core CPI YoY: индекс проецируется на месяц вперёд средним
  месячным % изменением за последние 3 месяца, потом считается YoY против
  индекса 12 месяцев назад (уже известен точно).
- Безработица / JOLTS: последнее известное значение + среднее изменение
  за последние 3 месяца.
- ВВП: не считаем сами — берём готовый профессиональный nowcast Atlanta
  Fed (GDPNow), он тоже свободно отдаётся через тот же FRED без ключа.

Неопределённость (std) для каждой модели — не выдумана, а посчитана
эмпирически: остаток такой же наивной модели "среднее за 3 предыдущих
месяца" при пошаговой (walk-forward, не задним числом по всей истории
разом — та же дисциплина, что и у EMOS в weather_bias.py) проверке на
предыдущих ~24 месяцах. Для ВВП — остаток GDPNow против уже случившегося
факта (A191RL1Q225SBEA), тоже по всей доступной истории на FRED.

Это первая, самая простая версия ("наивная модель") — не претендует на
класс Cleveland Fed, как и первый прогон weather_edge.py до всех поправок
(GFS+ICON, поправка на смещение, EMOS). Отдельная, ещё не проверенная
гипотеза — только сбор и логирование, ничего не покупает.
"""

import json
import math
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
GAMMA = "https://gamma-api.polymarket.com"
FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv"

MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}

RE_NUM = r"-?\d+\.?\d*"
RE_BETWEEN = re.compile(rf"between ({RE_NUM})[%M]? and ({RE_NUM})[%M]?")
RE_UPPER_FIRST = re.compile(rf"({RE_NUM})%? or (?:less|below)")
RE_LOWER_FIRST = re.compile(rf"({RE_NUM})%? or (?:more|above|higher)")
RE_UPPER_LAST = re.compile(rf"(?:less than|below|≤)\s*({RE_NUM})[%M]?")
RE_LOWER_LAST = re.compile(rf"(?:greater than|at least|≥)\s*({RE_NUM})[%M]?")
RE_EXACT = re.compile(rf"be ({RE_NUM})%")


def parse_bucket(question, step):
    m = RE_BETWEEN.search(question)
    if m:
        return float(m.group(1)) - step / 2, float(m.group(2)) + step / 2
    m = RE_UPPER_FIRST.search(question) or RE_UPPER_LAST.search(question)
    if m:
        return -999.0, float(m.group(1)) + step / 2
    m = RE_LOWER_FIRST.search(question) or RE_LOWER_LAST.search(question)
    if m:
        return float(m.group(1)) - step / 2, 999.0
    m = RE_EXACT.search(question)
    if m:
        v = float(m.group(1))
        return v - step / 2, v + step / 2
    return None


def add_months(date_str, n):
    y, m, _ = map(int, date_str.split("-"))
    m += n
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}-01"


def month_to_date(month_name, now, year=None):
    mnum = MONTHS[month_name]
    if year is None:
        year = now.year if mnum <= now.month else now.year - 1
    return f"{int(year):04d}-{mnum:02d}-01"


def fetch_fred_series(series_id):
    r = requests.get(FRED, params={"id": series_id}, timeout=20)
    r.raise_for_status()
    lines = r.text.strip().splitlines()[1:]
    out = []
    for line in lines:
        date, _, val = line.partition(",")
        if not val or val == ".":
            continue
        try:
            out.append((date, float(val)))
        except ValueError:
            continue
    return out


def _normal_cdf(x, mean, std):
    if std <= 0:
        return 1.0 if x >= mean else 0.0
    return 0.5 * (1 + math.erf((x - mean) / (std * math.sqrt(2))))


def bucket_prob(mean, std, lo, hi):
    lo_cdf = 0.0 if lo <= -900 else _normal_cdf(lo, mean, std)
    hi_cdf = 1.0 if hi >= 900 else _normal_cdf(hi, mean, std)
    return max(0.0, hi_cdf - lo_cdf)


def naive_diff_std(series, window=24):
    d = dict(series)
    dates = sorted(d)
    errors = []
    for i in range(4, len(dates)):
        prev3 = [d[dates[j]] - d[dates[j - 1]] for j in range(i - 3, i)]
        forecast = sum(prev3) / 3
        actual = d[dates[i]] - d[dates[i - 1]]
        errors.append(actual - forecast)
    errors = errors[-window:]
    if len(errors) < 6:
        return None
    mean_e = sum(errors) / len(errors)
    return (sum((e - mean_e) ** 2 for e in errors) / len(errors)) ** 0.5


def _yoy_series(series):
    """[(дата, YoY-темп)] по всем месяцам, где известен индекс и 12 месяцев
    назад тоже известен."""
    d = dict(series)
    out = []
    for dt in sorted(d):
        base = add_months(dt, -12)
        if base in d:
            out.append((dt, d[dt] / d[base] - 1))
    return out


def forecast_yoy(series, target_date, window=3):
    """CPI/Core CPI (ряды NSA — с естественной сезонностью внутри года):
    'персистентный' прогноз — YoY следующего ещё не вышедшего месяца
    примерно равен среднему YoY нескольких последних уже известных
    месяцев. Первая версия пыталась проецировать индекс через среднее
    сырых MoM% за 3 месяца — сломалось на одном аномальном месяце (июнь),
    потому что MoM% в NSA-рядах сам по себе сильно сезонный, а персистенция
    в терминах YoY эту сезонность гасит автоматически (числитель и
    знаменатель одного и того же календарного месяца сокращаются)."""
    yoys = _yoy_series(series)
    known = [(dt, v) for dt, v in yoys if dt < target_date]
    if len(known) < window + 12:
        return None
    point = sum(v for _, v in known[-window:]) / window * 100

    # std — остаток этой же модели ("прогноз = среднее предыдущих `window`
    # YoY") против факта, посчитанный walk-forward по истории.
    errors = []
    for i in range(window, len(known)):
        forecast = sum(known[j][1] for j in range(i - window, i)) / window
        errors.append(known[i][1] - forecast)
    errors = errors[-24:]
    if len(errors) < 6:
        return None
    mean_e = sum(errors) / len(errors)
    std = (sum((e - mean_e) ** 2 for e in errors) / len(errors)) ** 0.5 * 100
    return point, std


def forecast_level_diff(series, target_date, scale=1.0):
    d = dict(series)
    dates = sorted(d)
    known_before = [dt for dt in dates if dt < target_date]
    if len(known_before) < 4:
        return None
    idx = dates.index(known_before[-1])
    recent_diffs = [d[dates[j]] - d[dates[j - 1]] for j in range(idx - 2, idx + 1)]
    avg_diff = sum(recent_diffs) / len(recent_diffs)

    cursor = dates[idx]
    val = d[cursor]
    while cursor < target_date:
        cursor = add_months(cursor, 1)
        val += avg_diff

    std = naive_diff_std(series)
    if std is None:
        return None
    return val * scale, std * scale


def forecast_gdp(target_date):
    gdpnow = dict(fetch_fred_series("GDPNOW"))
    if target_date not in gdpnow:
        return None
    point = gdpnow[target_date]
    actual = dict(fetch_fred_series("A191RL1Q225SBEA"))
    errors = [actual[dt] - gdpnow[dt] for dt in gdpnow if dt in actual]
    if len(errors) >= 6:
        mean_e = sum(errors) / len(errors)
        std = (sum((e - mean_e) ** 2 for e in errors) / len(errors)) ** 0.5
    else:
        # Запасной вариант при нехватке общей истории — по опубликованным
        # оценкам точности GDPNow ближе к концу квартала (Atlanta Fed).
        std = 1.2
    return point, std


# metric -> (regex заголовка на Polymarket, функция извлечения target_date
# из совпадения regex, шаг бакета, функция прогноза).
def _cpi_annual_target(m, now):
    return month_to_date(m.group(1), now)


def _core_cpi_target(m, now):
    return month_to_date(m.group(1), now, year=int(m.group(2)))


def _jolts_target(m, now):
    return month_to_date(m.group(1), now, year=int(m.group(2)))


def _unrate_target(m, now):
    return month_to_date(m.group(1), now)


def _gdp_target(m, now):
    quarter, year = int(m.group(1)), int(m.group(2))
    return f"{year:04d}-{(quarter - 1) * 3 + 1:02d}-01"


METRICS = {
    "cpi_annual": {
        "label": "CPI (годовая)",
        "title_re": re.compile(r"^(\w+) Inflation US - Annual$"),
        "target_fn": _cpi_annual_target,
        "step": 0.1,
        # ВАЖНО: маркет прямо в описании резолвится по НЕсезонно
        # скорректированному CPI ("before seasonal adjustment") — сначала
        # взял CPIAUCSL (SA), получил фиктивные 42 п.п. edge. CPIAUCNS —
        # правильный (NSA) ряд.
        "forecast": lambda target: forecast_yoy(fetch_fred_series("CPIAUCNS"), target),
    },
    "core_cpi_yoy": {
        "label": "Core CPI (годовая)",
        "title_re": re.compile(r"^Core CPI YoY - (\w+) (\d{4})$"),
        "target_fn": _core_cpi_target,
        "step": 0.1,
        "forecast": lambda target: forecast_yoy(fetch_fred_series("CPILFENS"), target),  # NSA, см. выше
    },
    "unemployment": {
        "label": "Безработица (U-3)",
        "title_re": re.compile(r"^(\w+) Unemployment Rate$"),
        "target_fn": _unrate_target,
        "step": 0.1,
        "forecast": lambda target: forecast_level_diff(fetch_fred_series("UNRATE"), target),
    },
    "jolts": {
        "label": "JOLTS (вакансии)",
        "title_re": re.compile(r"^JOLTS Job Openings: (\w+) (\d{4})$"),
        "target_fn": _jolts_target,
        "step": 0.1,
        "forecast": lambda target: forecast_level_diff(fetch_fred_series("JTSJOL"), target, scale=0.001),
    },
    "gdp": {
        "label": "ВВП США (QoQ SAAR)",
        "title_re": re.compile(r"^US GDP growth in Q(\d) (\d{4})\??$"),
        "target_fn": _gdp_target,
        "step": 0.5,
        "forecast": lambda target: forecast_gdp(target),
    },
}


def fetch_economy_events():
    r = requests.get(
        f"{GAMMA}/events",
        params={"closed": "false", "tag_slug": "economy", "limit": 100, "order": "volume24hr", "ascending": "false"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def find_event(events, title_re):
    candidates = [(ev, title_re.match(ev.get("title", ""))) for ev in events]
    candidates = [(ev, m) for ev, m in candidates if m]
    if not candidates:
        return None, None
    candidates.sort(key=lambda pair: pair[0].get("endDate") or "9999")
    return candidates[0]


def fetch_buckets(event, step):
    buckets = []
    for m in event.get("markets", []):
        q = m.get("question", "")
        rng = parse_bucket(q, step)
        if rng is None:
            continue
        try:
            outcomes = json.loads(m["outcomes"])
            prices = json.loads(m["outcomePrices"])
            yes_p = float(prices[outcomes.index("Yes")])
        except (KeyError, ValueError, TypeError, IndexError):
            continue
        buckets.append({"lo": rng[0], "hi": rng[1], "market_p": yes_p})
    return buckets


def ensure_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS macro_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            metric TEXT NOT NULL,
            target_period TEXT NOT NULL,
            poly_slug TEXT,
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


def run():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    ensure_schema(conn)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    try:
        events = fetch_economy_events()
    except requests.RequestException as e:
        print(f"Polymarket: ошибка запроса — {e}", file=sys.stderr)
        conn.close()
        return

    rows = []
    for metric, cfg in METRICS.items():
        event, m = find_event(events, cfg["title_re"])
        if event is None:
            print(f"{metric}: маркет не найден на Polymarket", file=sys.stderr)
            continue
        target_date = cfg["target_fn"](m, now)

        try:
            forecast = cfg["forecast"](target_date)
        except requests.RequestException as e:
            print(f"{metric}: ошибка запроса FRED — {e}", file=sys.stderr)
            continue
        if forecast is None:
            print(f"{metric}: недостаточно истории FRED для прогноза на {target_date}", file=sys.stderr)
            continue
        mean, std = forecast

        buckets = fetch_buckets(event, cfg["step"])
        if not buckets:
            print(f"{metric}: не разобрались с бакетами маркета", file=sys.stderr)
            continue

        best_edge = 0.0
        for b in buckets:
            mp = bucket_prob(mean, std, b["lo"], b["hi"])
            edge = mp - b["market_p"]
            best_edge = max(best_edge, abs(edge))
            rows.append(
                (now_iso, metric, event["title"], event.get("slug"), b["lo"], b["hi"],
                 b["market_p"], mp, mean, std, edge, event.get("volume", 0))
            )
        print(f"{cfg['label']} ({event['title']}): модель={mean:.2f}±{std:.2f}, макс |edge|={best_edge:.3f}")

    if rows:
        conn.executemany(
            """
            INSERT INTO macro_snapshots
            (ts_utc, metric, target_period, poly_slug, bucket_lo, bucket_hi, market_p, model_p, model_mean, model_std, edge, event_vol)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()

    print(f"Метрик обработано: {len(rows) and len(set(r[1] for r in rows))}, строк записано: {len(rows)}")
    conn.close()


if __name__ == "__main__":
    run()
