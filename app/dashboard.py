"""
Веб-дашборд поверх sqlite, который пишет weather_edge.py по крону.
Только чтение, ничего не торгует. Порт 8093, чтобы не пересекаться с
gold-sim (8090-8092).
"""

import json
import math
import os
import sqlite3
import time
from pathlib import Path

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from wallet_docs import WALLET_DOCS

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# 2026-08-25: до этой отметки CITIES в weather_edge.py указывал на центр
# города, а не на станцию, по которой Polymarket реально резолвит маркет
# (аэропорт LaGuardia для NYC и т.д. — см. комментарий в weather_edge.py).
# Снимки/факты до фикса сравнивали модель не с той точкой на карте — не
# честная калибровка модели, а баг в сборе. В расчёт калибровки не берём,
# но из sqlite не удаляем — это свидетельство самого бага, не мусор.
WEATHER_COORD_FIX_TS = "2026-08-25T19:58:27+00:00"

app = FastAPI(title="weather-lab dashboard")
# 2026-09-28: логотип и значок сайта (файлы Alex) — app/static
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(STATIC_DIR / "weather-lab-icon-512.png", media_type="image/png")


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
    if table_exists(conn, "weather_station_daily"):
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
        JOIN weather_station_daily o ON s.city = o.city AND s.local_date = o.local_date
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


VIEWER_TZ = ZoneInfo("Europe/Chisinau")


def expected_close(city, local_date):
    """Когда ждать результата открытой ставки: Polymarket закрывает маркет
    примерно через 1-3 часа после полуночи по местному времени города
    (ждёт официальные данные станции), плюс мы проверяем исходы раз в 2
    часа — по наблюдениям 22-23.09. Возвращает строку для страницы."""
    from weather_cities import OBS_CITIES

    cfg = OBS_CITIES.get(city)
    if cfg is None:
        return None
    d = date.fromisoformat(local_date) + timedelta(days=1)
    midnight = datetime(d.year, d.month, d.day, tzinfo=ZoneInfo(cfg["tz"]))
    # Верхняя граница окна (3 часа после полуночи) — к этому времени
    # результат обычно уже есть. Просто дата и время, без "сегодня/завтра".
    return (midnight + timedelta(hours=3)).astimezone(VIEWER_TZ).strftime("%d.%m %H:%M")


# Имена кошельков weather_paper.py -> подписи на странице.
PAPER_WALLETS = {
    "main": "Основная модель (GFS+ICON)", "emos": "EMOS", "mm": "Микс моделей",
    "ml": "Обучаемая модель (ML)",
    "ml2": "Обучаемая модель v2 (распределение)",
    "ml3": "Обучаемая модель v3 (v2 + мнение рынка)",
    "ml_shift": "Обучаемая v1 — только при расхождении с рынком",
    "ml3_cal": "Главная v3 + рынок (смесь)",
    "ml3_no": "Главная v3 — ставки «против»",
    "ml3_cal_k": "Смесь — ставка по перевесу",
    "ml4": "Обучаемая v4 (v3, 31 лист)",
    "ml4_cal": "v4 + рынок (смесь)",
    "ml4e": "v4 — среднее 3 обучений",
    "ml4e_cal": "v4 среднее 3 + рынок (смесь)",
    "ens": "6 ансамблей + рынок (смесь)",
    "ml3_cal15": "Смесь — не дешевле 15¢",
    "no_cheap": "Против лотерейных билетов",
    "copy": "Повтор за сильными трейдерами",
    # двойники: те же сигналы, но покупают своей заявкой (без комиссии, по нижней цене)
    "main_mk": "Основная модель — своя заявка", "emos_mk": "EMOS — своя заявка", "mm_mk": "Микс — своя заявка",
    "ml3_mk": "Главная v3 — своя заявка",
}
PAPER_START_BALANCE = 100.0
# свой старт у кошелька (как weather_paper.START_BY_WALLET; совпадение проверяет preflight.py)
WALLET_START = {"copy": 300.0, "ml": 300.0}  # как weather_paper.START_BY_WALLET (preflight сверяет)
PAPER_STAKE = 2.0  # ставка кошельков (weather_paper.STAKE): меньше на счёте — новых ставок нет
# 2026-09-25 (просьба Alex — "много кошельков, не понять что к чему"):
# одна главная модель наверху, остальные — компактно, по группам, с
# пояснением в одну строку. Таблицы ставок по умолчанию — только главная.
MAIN_WALLET = "ml3"
WALLET_INFO = {
    "ml3": ("Главная модель", "Главная модель — обучаемая v3",
            "Учится на 16 месяцах погоды: прогнозы 16 погодных моделей, утренние замеры, облачность, влажность, "
            "вчерашние ошибки и мнение рынка. Лучшая на истории — кандидат на реальные деньги."),
    "ml4": ("Другие версии обучаемой модели", "Обучаемая v4 (v3, 31 лист)",
            "Та же v3, но деревья крупнее: на проверке точнее v3 в августе и сентябре. Ставит как главная"),
    "ml4_cal": ("Другие версии обучаемой модели", "v4 + рынок (смесь)",
                "35% v4 + 65% рынка, ставит при перевесе от 3 п.п. — как «смесь», но на v4"),
    "ml4e": ("Другие версии обучаемой модели", "v4 — среднее 3 обучений",
             "Та же v4, но прогноз — среднее трёх обучений: на проверке точнее одного обучения везде. Ставит как главная"),
    "ml4e_cal": ("Другие версии обучаемой модели", "v4 среднее 3 + рынок (смесь)",
                 "35% v4 (среднее 3 обучений) + 65% рынка, ставит при перевесе от 3 п.п."),
    "ml3_cal_k": ("Другие версии обучаемой модели", "Смесь — ставка по перевесу",
                  "Как «смесь», но ставка от $0.5 до $10: чем больше перевес, тем больше ставка (Келли ×0.25)"),
    "ml3_no": ("Другие версии обучаемой модели", "Главная v3 — ставки «против»",
               "Покупает «нет» на вариант, который модель считает переоценённым (перевес от 10 п.п.)"),
    "ml3_cal15": ("Другие версии обучаемой модели", "Смесь — не дешевле 15¢",
                  "Как «смесь», но не ставит на варианты дешевле 15¢: на всём рынке они сбываются реже своей цены"),
    "ml3_cal": ("Другие версии обучаемой модели", "Главная v3 + рынок (смесь)",
                "35% главной модели + 65% рынка: без самоуверенности, ставит при перевесе от 3 п.п."),
    "ml2": ("Другие версии обучаемой модели", "Обучаемая v2", "То же без мнения рынка"),
    "ml": ("Другие версии обучаемой модели", "Обучаемая v1", "Первая версия: одно число и одинаковый разброс"),
    "ml_shift": ("Другие версии обучаемой модели", "Обучаемая v1 — только при расхождении",
                 "Та же v1, но ставит, только если ждёт другую температуру, чем рынок (≥0.5°C)"),
    "mm": ("Прогноз по формулам (раньше)", "Микс 16 погодных моделей", "Веса моделей по городу, без обучения"),
    "emos": ("Прогноз по формулам (раньше)", "EMOS", "Поправка ошибки GFS+ICON по истории"),
    "main": ("Прогноз по формулам (раньше)", "Основная модель", "GFS+ICON с поправкой на смещение — самый первый вариант"),
    "ml3_mk": ("Тот же сигнал, но покупка своей заявкой", "Главная v3 — своя заявка",
               "Сигнал главной модели, но своей заявкой: без комиссии, по нижней цене, снимается в 12:00"),
    "mm_mk": ("Тот же сигнал, но покупка своей заявкой", "Микс — своя заявка", "Без комиссии, по нижней цене, но не всегда исполняется"),
    "emos_mk": ("Тот же сигнал, но покупка своей заявкой", "EMOS — своя заявка", "То же для EMOS"),
    "main_mk": ("Тот же сигнал, но покупка своей заявкой", "Основная — своя заявка", "То же для основной модели"),
    "ens": ("Ансамбли погодных моделей", "6 ансамблей + рынок (смесь)",
            "212 вариантов прогноза от 6 ансамблей (ECMWF, нейросеть ECMWF, GFS, ICON, UKMO, GEM) + 65% рынка, "
            "перевес от 3 п.п. Проверка по заметкам: 12.10 и 28.10"),
    "no_cheap": ("Перекосы рынка", "Против лотерейных билетов",
                 "Покупает «нет» на вариант за 5-15¢, который смесь модели и рынка считает переоценённым: люди переплачивают за дешёвые варианты"),
    "copy": ("Повтор за сильными трейдерами", "Повтор за сильными трейдерами",
             "Повторяет покупки 30 лучших трейдеров погоды за 14 дней — только сделанные накануне дня маркета, не дороже их цены +2¢"),
    "obs": ("Живые замеры", "По живым замерам станции", "Ставка против варианта, который станция уже исключила"),
    "obs_fmi": ("Живые замеры", "Хельсинки: 10-минутные замеры FMI",
                "То же, что «по живым замерам», но по 10-минутным данным финской метеослужбы — раньше METAR"),
}
# кошельки по живым замерам (paper_obs_trades, колонка wallet) — weather_obs_live.py
OBS_WALLETS = ("obs", "obs_fmi")
# 2026-09-25: страница кошелька в стиле банковского приложения — города по-русски,
# у каждого счёта короткий значок вместо иконки.
CITY_RU = {
    "nyc": "Нью-Йорк", "toronto": "Торонто", "london": "Лондон", "paris": "Париж", "madrid": "Мадрид",
    "beijing": "Пекин", "atlanta": "Атланта", "miami": "Майами", "los_angeles": "Лос-Анджелес",
    "chicago": "Чикаго", "dallas": "Даллас", "san_francisco": "Сан-Франциско", "houston": "Хьюстон",
    "denver": "Денвер", "seattle": "Сиэтл", "austin": "Остин", "wellington": "Веллингтон",
    "sao_paulo": "Сан-Паулу", "panama_city": "Панама", "tokyo": "Токио", "shanghai": "Шанхай",
    "helsinki": "Хельсинки", "mexico_city": "Мехико", "buenos_aires": "Буэнос-Айрес", "seoul": "Сеул",
    "munich": "Мюнхен", "shenzhen": "Шэньчжэнь", "manila": "Манила", "warsaw": "Варшава", "busan": "Пусан",
    "qingdao": "Циндао", "guangzhou": "Гуанчжоу", "singapore": "Сингапур", "milan": "Милан",
    "amsterdam": "Амстердам", "lucknow": "Лакхнау", "chongqing": "Чунцин", "chengdu": "Чэнду",
    "kuala_lumpur": "Куала-Лумпур", "jeddah": "Джидда", "karachi": "Карачи", "cape_town": "Кейптаун",
    "ankara": "Анкара", "moscow": "Москва", "tel_aviv": "Тель-Авив", "istanbul": "Стамбул",
    "wuhan": "Ухань", "zhengzhou": "Чжэнчжоу",
}
WALLET_BADGE = {"ml3": "v3", "ml2": "v2", "ml": "v1", "ml_shift": "v1+", "mm": "MX", "emos": "EM", "main": "GI",
                "mm_mk": "MX", "emos_mk": "EM", "main_mk": "GI", "ml3_mk": "v3", "ml3_cal": "v3+", "ml3_no": "v3−", "ml3_cal_k": "v3$", "ml4": "v4", "ml4_cal": "v4+", "ml4e": "v4³", "ml4e_cal": "v4³+", "ens": "EN", "ml3_cal15": "v3+¢", "no_cheap": "НЕТ", "copy": "CP", "obs": "OB", "obs_fmi": "FI"}
WALLET_GROUPS = ["Другие версии обучаемой модели", "Перекосы рынка", "Ансамбли погодных моделей", "Повтор за сильными трейдерами", "Прогноз по формулам (раньше)",
                 "Тот же сигнал, но покупка своей заявкой", "Живые замеры"]


def paper_bucket(lo, hi, unit):
    """Бакет по-человечески: 31°C, 84–85°F, ≤75°F, ≥94°F."""
    sym = "°F" if unit == "fahrenheit" else "°C"
    if lo is None:
        return ""
    if lo <= -900:
        return f"≤{hi - 0.5:.0f}{sym}"
    if hi >= 900:
        return f"≥{lo + 0.5:.0f}{sym}"
    x, y = lo + 0.5, hi - 0.5
    return f"{x:.0f}{sym}" if x == y else f"{x:.0f}–{y:.0f}{sym}"


def _fee(r):
    return (r["fee"] or 0) if "fee" in r.keys() else 0.0


def _pnl(r):
    """Итог сделки: выплата - покупка - комиссия Polymarket."""
    return (r["payout"] or 0) - r["stake"] - _fee(r)


def _wallet_card(label, rows, nofill=0, skipped=0):
    settled = [r for r in rows if r["status"] in ("won", "lost", "void")]
    open_ = [r for r in rows if r["status"] in ("open", "resting")]
    pnl = sum(_pnl(r) for r in settled)
    by_city = {}
    for r in settled:
        c = by_city.setdefault(r["city"], {"n": 0, "won": 0, "pnl": 0.0})
        c["n"] += 1
        c["won"] += r["status"] == "won"
        c["pnl"] += _pnl(r)
    return {
        "label": label, "balance": PAPER_START_BALANCE + pnl, "pnl": pnl,
        # 2026-09-27 (Alex: «видеть, насколько эффективно», а не сравнивать доллары): итог в % от поставленного
        "roi": 100 * pnl / staked if (staked := sum(r["stake"] + _fee(r) for r in settled)) else None,
        "in_play": sum(r["stake"] + _fee(r) for r in open_), "n": len(settled),
        "fees": sum(_fee(r) for r in rows if r["status"] in ("open", "won", "lost", "void")),
        "won": sum(1 for r in settled if r["status"] == "won"), "open": len(open_),
        "by_city": sorted(by_city.items()), "nofill": nofill, "skipped": skipped,
        # 2026-09-28 (Alex: «почему "лучше рынка", если большой минус?»): «лучше / хуже рынка» на карточке —
        # только по настоящим закрытым ставкам этого кошелька и в деньгах: купили по цене рынка, значит честная
        # выплата = потраченному. Выплата больше потраченного (без комиссии) — выбираем лучше рынка.
        # Раньше считалось по числу угаданных на истории модели — у ml «лучше рынка» при −$86.
        "paid": sum(r["payout"] or 0 for r in settled), "cost": sum(r["stake"] for r in settled),
        # 2026-09-28 (просьба Alex): случайность или нет — размах итога при чистом везении: ставка $s по цене p
        # выигрывает s/p с шансом p, разброс итога s²(1−p)/p; сумма по закрытым ставкам, корень — «±».
        "sd": math.sqrt(sum(r["stake"] ** 2 * (1 - r["price"]) / r["price"] for r in settled
                            if r["price"] and 0 < r["price"] < 1 and r["stake"])),
    }


def luck(pnl, sd):
    """Итог против случайного размаха: (текст, тон). До 1 размаха — случайность, 1-2 — «скорее», от 2 — точно."""
    if not sd:
        return None
    z = pnl / sd
    if abs(z) < 1:
        return f"в пределах случайности\u00a0(±\u2060${sd:.0f})", ""
    word = "плюс" if z > 0 else "минус"
    if abs(z) < 2:
        return f"скорее реальный {word}, но может быть и случайность\u00a0(±\u2060${sd:.0f})", "pos" if z > 0 else "neg"
    return f"{word} не случайный (случайность — до\u00a0±\u2060${sd:.0f})", "pos" if z > 0 else "neg"


VERDICT_MIN_N = 20  # меньше закрытых ставок — «лучше / хуже рынка» ещё не говорим (случайность)
REAL_MONEY_THRESHOLD = 150  # закрытых ставок главной модели до решения о реальных деньгах (CLAUDE.md)


def _nice_step(span, target=4):
    raw = max(span, 1e-9) / target
    for m in (1, 2, 2.5, 5, 10, 20, 25, 50, 100):
        if m >= raw:
            return m
    return 100 * (raw // 100 + 1)


def smooth_path(pts):
    """Плавная линия через точки (2026-09-27, просьба Alex): монотонная кубическая кривая
    (Фритч-Карлсон) — проходит точно через каждую точку и не «перелетает» выше/ниже соседних
    значений, то есть не рисует баланс, которого не было."""
    n = len(pts)
    if n < 3:
        return " ".join(("M" if i == 0 else "L") + f"{x},{y}" for i, (x, y) in enumerate(pts))
    dx = [pts[i + 1][0] - pts[i][0] for i in range(n - 1)]
    d = [(pts[i + 1][1] - pts[i][1]) / dx[i] if dx[i] else 0.0 for i in range(n - 1)]
    m = [d[0]] + [0.0 if d[i - 1] * d[i] <= 0 else (d[i - 1] + d[i]) / 2 for i in range(1, n - 1)] + [d[-1]]
    for i in range(n - 1):
        if d[i] == 0:
            m[i] = m[i + 1] = 0.0
            continue
        a, b = m[i] / d[i], m[i + 1] / d[i]
        if a * a + b * b > 9:
            t = 3 / (a * a + b * b) ** 0.5
            m[i], m[i + 1] = t * a * d[i], t * b * d[i]
    out = [f"M{pts[0][0]},{pts[0][1]}"]
    for i in range(n - 1):
        (x0, y0), (x1, y1) = pts[i], pts[i + 1]
        h = dx[i] / 3
        out.append(f"C{x0 + h:.1f},{y0 + m[i] * h:.1f} {x1 - h:.1f},{y1 - m[i + 1] * h:.1f} {x1},{y1}")
    return " ".join(out)


def balance_chart(rows, start=100.0, w=500, h=330):
    """Баланс по моментам, когда он менялся (2026-09-27, просьба Alex): точка — каждый
    расчёт ставок (settled_at, с точностью до минуты), ступенька — баланс между
    расчётами не меняется. rows: (settled_at, итог, город, won/lost/void).
    Точки идут через равный шаг (иначе расчёты одного дня слипаются), время — в подсказке."""
    ev = {}
    for ts, pnl, city, status in rows:
        k = (ts or "")[:16]
        e = ev.setdefault(k, {"pnl": 0.0, "won": 0, "n": 0, "cities": []})
        e["pnl"] += pnl
        e["n"] += 1
        e["won"] += status == "won"
        e["cities"].append(CITY_RU.get(city, city))
    if not ev:
        return None
    fmt = lambda k: f"{k[8:10]}.{k[5:7]} {k[11:16]}" if len(k) >= 16 else k
    bal, pts = start, [{"t": "старт", "v": start, "chg": 0.0, "sub": "стартовый баланс"}]
    for k in sorted(ev):
        e = ev[k]
        bal += e["pnl"]
        who = ", ".join(sorted(set(e["cities"])))
        sub = f"{money_str(e['pnl'])} · угадано {e['won']} из {e['n']}: {who}"
        pts.append({"t": fmt(k), "v": bal, "chg": e["pnl"], "sub": sub})
    lo, hi = min(p["v"] for p in pts), max(p["v"] for p in pts)
    step = _nice_step(hi - lo if hi > lo else 10)
    y0 = step * ((lo - step * 0.25) // step)
    y1 = step * (-(-(hi + step * 0.25) // step))
    L, R, T, B = 52, 20, 14, 30
    pw, ph = w - L - R, h - T - B
    X = lambda i: L + pw * i / (len(pts) - 1)
    Y = lambda v: T + ph * (1 - (v - y0) / (y1 - y0))
    for i, p in enumerate(pts):
        p["x"], p["y"] = round(X(i), 1), round(Y(p["v"]), 1)
    path = smooth_path([(p["x"], p["y"]) for p in pts])
    area = path + f" L{pts[-1]['x']},{T + ph} L{pts[0]['x']},{T + ph} Z"
    ticks = []
    t = y0
    while t <= y1 + 1e-9:
        ticks.append({"y": round(Y(t), 1), "label": f"${t:,.0f}" if step >= 1 else f"${t:.1f}"})
        t += step
    return {"w": w, "h": h, "L": L, "R": w - R, "T": T, "B": T + ph, "path": path, "area": area, "points": pts,
            "ticks": ticks, "start_y": round(Y(start), 1), "last": pts[-1], "first_t": pts[1]["t"], "last_t": pts[-1]["t"]}


def money_str(v):
    return f"{'+' if v >= 0 else '−'}${abs(v):.2f}"


def _pnl_distribution(bets, step=0.1):
    """Точное распределение итога ставок (каждая выигрывает независимо со своим
    шансом): {итог в шагах step: вероятность}. bets: (шанс, выигрыш, проигрыш)."""
    dist = {0: 1.0}
    for p, win, loss in bets:
        w, l = round(win / step), round(loss / step)
        nxt = {}
        for k, q in dist.items():
            nxt[k + w] = nxt.get(k + w, 0.0) + q * p
            nxt[k + l] = nxt.get(k + l, 0.0) + q * (1 - p)
        dist = nxt
    return dist


def scenarios(items):
    """2026-09-25 (просьба Alex — «сколько по факту выиграем или проиграем»):
    сценарии по числу угаданных ставок. Для каждого «угадаем k из n» — шанс
    (по ценам рынка: на истории рынок откалиброван) и итог: от «угадали самые
    дешёвые выплаты» до «угадали самые дорогие». Хвост маловероятных сценариев
    (<0.5% вместе) сворачивается в «k и больше», чтобы не показывать
    бессмысленные «все выиграли». Заявки, ещё не купленные, не считаем."""
    bets = [t for t in items if t.get("bought") and t.get("chance") is not None]
    if not bets:
        return None
    n, cost = len(bets), sum(t["cost"] for t in bets)
    pay = sorted(t["shares"] for t in bets)
    probs = [1.0]
    for t in bets:
        p = min(max(t["chance"], 0.0), 1.0)
        nxt = [0.0] * (len(probs) + 1)
        for k, q in enumerate(probs):
            nxt[k] += q * (1 - p)
            nxt[k + 1] += q * p
        probs = nxt
    tail, kmax = 0.0, n
    for k in range(n, 0, -1):
        if tail + probs[k] >= 0.005:
            kmax = k
            break
        tail += probs[k]
    rows = []
    for k in range(0, kmax + 1):
        last = k == kmax and kmax < n
        pr = sum(probs[k:]) if last else probs[k]
        lo = -cost + sum(pay[:k])
        hi = -cost + sum(pay[-k:]) if k else lo
        rows.append({"k": k, "label": f"{k}+" if last else str(k), "prob": pr, "lo": lo, "hi": None if last else hi})
    top = max(r["prob"] for r in rows)
    for r in rows:
        r["bar"] = round(100 * r["prob"] / top)
        r["tone"] = "neg" if (r["hi"] if r["hi"] is not None else r["lo"]) < 0 else ("pos" if r["lo"] > 0 else "mix")
    # «скорее всего»: самое вероятное число угаданных и соседи, пока вместе ≥60%
    i = max(range(len(rows)), key=lambda j: rows[j]["prob"])
    a = b = i
    acc = rows[i]["prob"]
    while acc < 0.6 and (a > 0 or b < len(rows) - 1):
        left = rows[a - 1]["prob"] if a > 0 else -1
        right = rows[b + 1]["prob"] if b < len(rows) - 1 else -1
        if left >= right:
            a -= 1
            acc += left
        else:
            b += 1
            acc += right
    for j, r in enumerate(rows):
        r["likely"] = a <= j <= b
    dist = _pnl_distribution([(min(max(t["chance"], 0.0), 1.0), t["shares"] - t["cost"], -t["cost"]) for t in bets])
    return {"n": n, "cost": cost, "rows": rows, "likely": (rows[a]["label"], rows[b]["label"]), "likely_p": acc,
            "p_plus": sum(q for k, q in dist.items() if k > 0)}


def line_chart(labels, series, fmt, w=520, h=300, vline=None):
    """Несколько линий на одной оси. series: [{"name", "cls", "values"}].
    Геометрия для SVG + подписи на концах линий (раздвинуты, чтобы не слипались)."""
    vals = [v for s in series for v in s["values"] if v is not None]
    if len(labels) < 2 or not vals:
        return None
    lo, hi = min(vals), max(vals)
    step = _nice_step(hi - lo if hi > lo else 1)
    y0 = step * ((lo - step * 0.2) // step)
    if lo >= 0:
        y0 = max(y0, 0)  # счётчики не бывают отрицательными — ось от нуля
    y1 = step * (-(-(hi + step * 0.2) // step))
    L, R, T, B = 50, 175, 14, 30
    pw, ph = w - L - R, h - T - B
    X = lambda i: L + pw * i / (len(labels) - 1)
    Y = lambda v: T + ph * (1 - (v - y0) / (y1 - y0))
    out = []
    for s_ in series:
        pts = [(round(X(i), 1), round(Y(v), 1)) for i, v in enumerate(s_["values"]) if v is not None]
        out.append({"name": s_["name"], "cls": s_["cls"], "path": " ".join(("M" if i == 0 else "L") + f"{x},{y}" for i, (x, y) in enumerate(pts)),
                    "end": pts[-1], "last": s_["values"][-1]})
    ends = sorted(out, key=lambda s_: s_["end"][1])
    for i in range(1, len(ends)):  # подписи на концах — не ближе 16px друг к другу
        ends[i]["ly"] = max(ends[i]["end"][1], ends[i - 1].get("ly", ends[i - 1]["end"][1]) + 16)
    ends[0]["ly"] = ends[0]["end"][1]
    ticks, t = [], y0
    while t <= y1 + 1e-9:
        ticks.append({"y": round(Y(t), 1), "label": fmt(t)})
        t += step
    cw = pw / (len(labels) - 1)
    cols = [{"x": round(X(i), 1), "x0": round(X(i) - cw / 2, 1), "cw": round(cw, 1), "label": lab,
             "vals": [(s_["name"], fmt(s_["values"][i]) if s_["values"][i] is not None else "—") for s_ in series]}
            for i, lab in enumerate(labels)]
    return {"w": w, "h": h, "L": L, "R": w - R, "T": T, "B": T + ph, "series": out, "ticks": ticks, "cols": cols,
            "first": labels[0], "last": labels[-1], "vline": round(X(vline), 1) if vline is not None else None}


SKILL_PARENT = {"main_mk": "main", "emos_mk": "emos", "mm_mk": "mm", "ml3_mk": "ml3", "ml3_cal_k": "ml3_cal", "ml3_cal15": "ml3_cal"}  # «своя заявка» — сигнал родителя


def model_skill(conn, key):
    """Насколько главная модель права по сравнению с рынком (таблица ml_skill,
    weather_ml_skill.py). По неделям: средний шанс, который модель и рынок
    давали правильному ответу; и на днях ставок — сколько угадано реально,
    сколько ожидал рынок и сколько обещала модель (накопительно)."""
    if not table_exists(conn, "ml_skill"):
        return None
    cols = [r[1] for r in conn.execute("PRAGMA table_info(ml_skill)")]
    if "model" not in cols:
        return None
    rows = conn.execute("""SELECT date, source, p_model, p_market, bet_p_model, bet_p_market, bet_won FROM ml_skill
                           WHERE model = ? ORDER BY date""", (SKILL_PARENT.get(key, key),)).fetchall()
    if not rows:
        return None
    weeks = {}
    for r in rows:
        d = date.fromisoformat(r["date"])
        wk = (d - timedelta(days=d.weekday())).isoformat()
        g = weeks.setdefault(wk, {"n": 0, "pm": 0.0, "pk": 0.0, "bets": 0, "won": 0, "em": 0.0, "ek": 0.0, "live": False})
        g["n"] += 1
        g["pm"] += r["p_model"]
        g["pk"] += r["p_market"]
        g["live"] |= r["source"] == "live"
        if r["bet_won"] is not None:
            g["bets"] += 1
            g["won"] += r["bet_won"]
            g["em"] += r["bet_p_model"]
            g["ek"] += r["bet_p_market"]
    keys = sorted(weeks)
    labels = [date.fromisoformat(k).strftime("%d.%m") for k in keys]
    live_i = next((i for i, k in enumerate(keys) if weeks[k]["live"]), None)
    if live_i == 0:
        live_i = None  # только живые дни — отмечать начало «вживую» незачем
    acc = [100 * weeks[k]["pm"] / weeks[k]["n"] for k in keys], [100 * weeks[k]["pk"] / weeks[k]["n"] for k in keys]
    cum, won, em, ek = [], 0, 0.0, 0.0
    for k in keys:
        won += weeks[k]["won"]
        em += weeks[k]["em"]
        ek += weeks[k]["ek"]
        cum.append((won, ek, em))
    n = len(rows)
    tot = {"n": n, "pm": 100 * sum(r["p_model"] for r in rows) / n, "pk": 100 * sum(r["p_market"] for r in rows) / n,
           "better": 100 * sum(r["p_model"] > r["p_market"] for r in rows) / n,
           "bets": sum(r["bet_won"] is not None for r in rows), "won": won, "ek": ek, "em": em,
           "first": rows[0]["date"], "last": rows[-1]["date"], "live": sum(r["source"] == "live" for r in rows),
           "parent": SKILL_PARENT.get(key)}
    tot["history"] = n - tot["live"]
    return {
        "tot": tot,
        "acc": line_chart(labels, [{"name": "Модель", "cls": "s-model", "values": acc[0]},
                                   {"name": "Рынок", "cls": "s-market", "values": acc[1]}],
                          lambda v: f"{v:.0f}%", vline=live_i),
        "bets": line_chart(labels, [{"name": "Реально угадано", "cls": "s-real", "values": [c[0] for c in cum]},
                                    {"name": "Ожидал рынок", "cls": "s-market", "values": [c[1] for c in cum]},
                                    {"name": "Обещала модель", "cls": "s-model", "values": [c[2] for c in cum]}],
                           lambda v: f"{v:.0f}", vline=live_i),
    }


# 2026-09-26 (просьба Alex): когда обновлялись данные. Время — из job_runs
# (jobmark.py, пишут сами скрипты), а пока его нет — из самих данных.
# (ключ, подпись, как часто по крону, следующий запуск по расписанию: (часы или None=каждые 2 ч, минута))
FRESH_JOBS = [
    ("weather_edge", "Цены и прогнозы", timedelta(hours=2), (None, 0)),
    ("weather_poly_resolve", "Итоги маркетов", timedelta(hours=2), (None, 5)),
    ("weather_paper", "Ставки и расчёт кошельков", timedelta(hours=2), (None, 10)),
    ("weather_ml_train", "Обучение моделей", timedelta(days=1), (5, 20)),
    ("weather_ml_skill", "«Насколько модель права»", timedelta(days=1), (5, 50)),
    ("weather_trades_history", "Настоящие сделки", timedelta(days=1), (4, 30)),
]


def _next_run(now, hour, minute):
    if hour is None:  # каждые 2 часа в чётные часы
        t = now.replace(minute=minute, second=0, microsecond=0)
        while t <= now or t.hour % 2:
            t += timedelta(hours=1)
        return t
    t = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return t if t > now else t + timedelta(days=1)


def _when(dt, now):
    d = dt.astimezone(VIEWER_TZ)
    if d.date() == now.date():
        return f"сегодня {d:%H:%M}"
    if d.date() == now.date() - timedelta(days=1):
        return f"вчера {d:%H:%M}"
    return f"{d:%d.%m %H:%M}"


def wallet_updates(conn):
    """2026-09-27 (просьба Alex): время на карточке кошелька — когда последний раз закрылась
    ставка (settled_at); новые ставки его не меняют (время последней новой — только в подсказке).
    Нет закрытых ставок — времени нет. Время в базе с разными поясами — сравниваем как даты."""
    out = {}

    def upd(w, kind, ts):
        if not ts:
            return
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            return
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        cur = out.setdefault(w, {})
        if kind not in cur or dt > cur[kind]:
            cur[kind] = dt

    for tbl, wcol, bet_ts in (("paper_trades", "wallet", "COALESCE(placed_at, snapshot_ts)"),
                              ("paper_obs_trades", "wallet", "placed_at")):
        if table_exists(conn, tbl):
            for r in conn.execute(f"SELECT {wcol}, settled_at, {bet_ts} FROM {tbl} "
                                  f"WHERE status IN ('open', 'resting', 'won', 'lost', 'void')"):
                upd(r[0], "settled", r[1])
                upd(r[0], "bet", r[2])
    res = {}
    fmt = lambda dt: dt.astimezone(VIEWER_TZ).strftime("%d.%m %H:%M")
    for w, d in out.items():
        if "settled" not in d:
            continue
        res[w] = {"when": fmt(d["settled"]), "what": "закрыта ставка",
                  "settled": fmt(d["settled"]), "bet": fmt(d["bet"]) if "bet" in d else None}
    return res


def active_alerts(conn):
    """Активные предупреждения (weather_alerts.py) + сторож базы (db_watchdog.sh, 24 часа)."""
    out = []
    f = DB_PATH.parent.parent / "ALERT_DB_LOCKED"
    try:
        if time.time() - f.stat().st_mtime < 86400:
            out.append({"msg": f.read_text().strip(), "since": datetime.fromtimestamp(f.stat().st_mtime, VIEWER_TZ).strftime("%d.%m %H:%M")})
    except OSError:
        pass
    if not table_exists(conn, "alerts"):
        return out
    for r in conn.execute("SELECT message, first_seen FROM alerts WHERE resolved_at IS NULL ORDER BY first_seen"):
        out.append({"msg": r["message"], "since": datetime.fromisoformat(r["first_seen"]).astimezone(VIEWER_TZ).strftime("%d.%m %H:%M")})
    return out


def data_freshness(conn):
    now = datetime.now(VIEWER_TZ)
    runs = {}
    if table_exists(conn, "job_runs"):
        runs = {r["job"]: r["finished_at"] for r in conn.execute("SELECT job, finished_at FROM job_runs")}
    fallback = {}
    if table_exists(conn, "snapshots"):
        fallback["weather_edge"] = conn.execute("SELECT MAX(ts_utc) FROM snapshots").fetchone()[0]
    if table_exists(conn, "weather_poly_outcomes"):
        fallback["weather_poly_resolve"] = conn.execute("SELECT MAX(resolved_at) FROM weather_poly_outcomes").fetchone()[0]
    try:
        fallback["weather_ml_train"] = json.loads((DB_PATH.parent.parent / "ml" / "meta.json").read_text())["trained_at"]
    except (OSError, ValueError, KeyError):
        pass
    out = []
    for job, label, every, (hh, mm) in FRESH_JOBS:
        raw = runs.get(job) or fallback.get(job)
        item = {"label": label, "when": None, "stale": False, "next": f"{_next_run(now, hh, mm):%H:%M}"}
        if raw:
            try:
                dt = datetime.fromisoformat(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                item["when"] = _when(dt, now)
                # «задерживается» — только по настоящей метке запуска: запасное время из данных
                # (например, последний пришедший итог) может быть старым и при работающем кроне
                item["stale"] = job in runs and now - dt > every + timedelta(minutes=40)
            except ValueError:
                pass
        out.append(item)
    return out


# ---- /status: здоровье системы (2026-09-27, просьба Alex) ----
# Скрипты крона — jobs_info.py; каждый запуск пишет обёртка job_wrap.py в job_log
# (итог, длительность, запросы к Open-Meteo); последний успех — job_runs.
OPEN_METEO_DAILY = 10000  # бесплатный лимит вызовов в сутки
ERR_WORDS = ("Traceback", "Error", "ошибка", "ОШИБКА", "ПРЕРВАНО", "locked")


def _dt(raw):
    try:
        d = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _ago(dt, now):
    m = int((now - dt).total_seconds() // 60)
    if m < 1:
        return "только что"
    if m < 60:
        return f"{m} мин назад"
    if m < 48 * 60:
        return f"{m // 60} ч {m % 60:02d} мин назад"
    return f"{m // 1440} дн назад"


def _log_error(log):
    """Последняя строка с ошибкой в конце лога (data/logs/<log>) — подсказка, что сломалось."""
    if not log:
        return None
    try:
        with open(DB_PATH.parent.parent / "logs" / log, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 20000))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if any(w in line for w in ERR_WORDS):
            return line.strip()[:300]
    return None


def _size(n):
    return f"{n / 1e9:.1f} ГБ" if n >= 1e9 else f"{n / 1e6:.0f} МБ"


def system_health(conn):
    import shutil
    from jobs_info import JOBS
    now = datetime.now(timezone.utc)
    day_ago = (now - timedelta(days=1)).isoformat()
    runs = {r["job"]: _dt(r["finished_at"]) for r in conn.execute("SELECT job, finished_at FROM job_runs")} \
        if table_exists(conn, "job_runs") else {}
    has_log = table_exists(conn, "job_log")
    from fixes import is_fixed, last_fixes
    fx = last_fixes(conn)
    jobs, om_by_job = [], []
    for key, label, script, sched, max_age, log in JOBS:
        j = {"key": key, "label": label, "script": script, "sched": sched, "state": "unknown",
             "ok_ago": None, "ok_when": None, "last": None, "fails": 0, "runs": 0, "err": None, "om": 0, "dur": None,
             "fixed_n": 0, "fix_note": None}
        ok = runs.get(key)
        if ok:
            j["ok_ago"], j["ok_when"] = _ago(ok, now), _when(ok, now.astimezone(VIEWER_TZ))
        if has_log:
            has_ie = "item_errors" in [r[1] for r in conn.execute("PRAGMA table_info(job_log)")]
            last = conn.execute(f"SELECT rc, duration_s, finished_at{', item_errors' if has_ie else ', 0 AS item_errors'} FROM job_log WHERE job = ? "
                                "ORDER BY finished_at DESC LIMIT 1", (key,)).fetchone()
            # 2026-09-28: падения до отметки «исправлено» (fixes.py) — не проблема, показываем как исправленные
            bad_rows = conn.execute("SELECT finished_at FROM job_log WHERE job = ? AND finished_at >= ? AND rc != 0",
                                    (key, day_ago)).fetchall()
            j["fixed_n"] = sum(1 for (t,) in bad_rows if is_fixed(fx, key, t))
            j["fix_note"] = fx[key][1] if key in fx and j["fixed_n"] else None
            agg = conn.execute("SELECT COUNT(*) n, SUM(rc != 0) f, SUM(om_calls) om, AVG(duration_s) d "
                               "FROM job_log WHERE job = ? AND finished_at >= ?", (key, day_ago)).fetchone()
            j["runs"], j["fails"], j["om"] = agg["n"], agg["f"] or 0, agg["om"] or 0
            j["dur"] = agg["d"]
            if last:
                j["last"] = {"rc": last["rc"], "dur": last["duration_s"], "item_errors": last["item_errors"] or 0}
        late = ok is None or now - ok > timedelta(minutes=max_age)
        last_fixed = j["last"] is not None and last is not None and is_fixed(fx, key, last["finished_at"])
        failed = j["last"] is not None and j["last"]["rc"] != 0 and not last_fixed
        if failed:
            j["state"] = "fail"
            j["err"] = ("остановлен по пределу времени" if j["last"]["rc"] in (124, 137, 143)
                        else _log_error(log)) or f"код выхода {j['last']['rc']}"
        elif j["last"] and j["last"]["item_errors"] and not last_fixed:
            # 2026-09-27: отработал, но часть городов / трейдеров / дней пропущена из-за ошибок (item_guard)
            j["state"] = "partial"
            j["err"] = f"пропущено из-за ошибок: {j['last']['item_errors']} — подробности в логе: " + (_log_error(log) or "см. data/logs/")
        elif ok is None:
            j["state"] = "unknown"
        elif late:
            j["state"] = "late"
        else:
            j["state"] = "ok"
        if j["om"]:
            om_by_job.append((label, j["om"]))
        jobs.append(j)

    alerts_now, alerts_week = [], []
    if table_exists(conn, "alerts"):
        week = (now - timedelta(days=7)).isoformat()
        for r in conn.execute("SELECT key, message, first_seen, resolved_at FROM alerts "
                              "WHERE resolved_at IS NULL OR resolved_at >= ? ORDER BY first_seen DESC", (week,)):
            a = {"key": r["key"], "msg": r["message"], "since": _when(_dt(r["first_seen"]), now.astimezone(VIEWER_TZ)),
                 "until": _when(_dt(r["resolved_at"]), now.astimezone(VIEWER_TZ)) if r["resolved_at"] else None}
            (alerts_now if r["resolved_at"] is None else alerts_week).append(a)
    lock_file = DB_PATH.parent.parent / "ALERT_DB_LOCKED"
    watchdog = None
    try:
        st = lock_file.stat()
        watchdog = {"when": _when(datetime.fromtimestamp(st.st_mtime, timezone.utc), now.astimezone(VIEWER_TZ)),
                    "recent": time.time() - st.st_mtime < 86400, "msg": lock_file.read_text().strip()[:300]}
        if watchdog["recent"]:
            alerts_now.insert(0, {"key": "db_locked", "msg": watchdog["msg"], "since": watchdog["when"], "until": None})
    except OSError:
        pass

    def fsize(p):
        try:
            return p.stat().st_size
        except OSError:
            return 0
    du = shutil.disk_usage(DB_PATH.parent)
    storage = {"db": _size(fsize(DB_PATH)), "wal": _size(fsize(Path(str(DB_PATH) + "-wal"))),
               "free": _size(du.free), "free_pct": du.free / du.total * 100}

    stop = (DB_PATH.parent.parent / "STOP").exists()
    copier = next(j for j in jobs if j["key"] == "weather_copy_live")
    pre_alert = next((a for a in alerts_now if a["key"] == "preflight"), None)
    al = next(j for j in jobs if j["key"] == "weather_alerts")
    preflight = {"ok": pre_alert is None, "msg": pre_alert["msg"] if pre_alert else None,
                 "when": al["ok_when"] or (runs.get("weather_alerts") and _when(runs["weather_alerts"], now.astimezone(VIEWER_TZ)))}
    om_total = sum(n for _l, n in om_by_job)
    om = {"total": om_total, "pct": om_total / OPEN_METEO_DAILY * 100, "limit": OPEN_METEO_DAILY,
          "by_job": sorted(om_by_job, key=lambda x: -x[1]), "counted": has_log and any(j["runs"] for j in jobs)}

    problems = []
    for j in jobs:
        if j["state"] == "fail":
            problems.append(f"«{j['label']}»: последний запуск с ошибкой")
        elif j["state"] == "partial":
            problems.append(f"«{j['label']}»: последний запуск с пропусками ({j['last']['item_errors']})")
        elif j["state"] == "late":
            problems.append(f"«{j['label']}» опаздывает — последний успех {j['ok_ago'] or 'не было'}")
    # «скрипт опаздывает» из тревог уже есть в строках выше — не дублируем
    problems += [a["msg"] for a in alerts_now if not a["key"].startswith("stale:")]
    if stop:
        problems.append("Включён стоп: новые ставки не делаются (файл data/STOP)")
    if storage["free_pct"] < 10:
        problems.append(f"Мало места на диске: свободно {storage['free']}")
    if om["pct"] > 90:
        problems.append(f"Open-Meteo: за сутки {om_total} запросов — близко к лимиту")
    counts = {s: sum(j["state"] == s for j in jobs) for s in ("ok", "late", "fail", "unknown", "partial")}
    return {"problems": problems, "jobs": jobs, "counts": counts, "alerts_now": alerts_now,
            "alerts_week": alerts_week, "storage": storage, "watchdog": watchdog, "stop": stop,
            "copier": copier, "preflight": preflight, "om": om,
            "checked": now.astimezone(VIEWER_TZ).strftime("%H:%M")}


@app.get("/status", response_class=HTMLResponse)
def status(request: Request):
    conn = db()
    try:
        h = system_health(conn)
    finally:
        conn.close()
    return TEMPLATES.TemplateResponse("status.html", {"request": request, "h": h})


def last_move(events):
    """2026-09-27 (просьба Alex): на сколько изменился баланс при последнем расчёте ставок —
    ▲/▼ рядом с балансом. Один расчёт = ставки, закрытые в одну и ту же минуту.
    events: (settled_at, итог $, город, статус)."""
    ev = [(_dt(t), v) for t, v, *_ in events if t]
    ev = [(t, v) for t, v in ev if t]
    if not ev:
        return None
    last = max(t for t, _v in ev).replace(second=0, microsecond=0)
    batch = [v for t, v in ev if t.replace(second=0, microsecond=0) == last]
    total = sum(batch)
    return {"pnl": total, "n": len(batch), "dir": "up" if total > 0.005 else ("down" if total < -0.005 else "flat"),
            "when": last.astimezone(VIEWER_TZ).strftime("%d.%m %H:%M")}


WEEKDAY_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def week_bars(rows):
    """2026-09-27 (просьба Alex, по макету Payoneer): итог закрытых ставок по дням маркета — последние
    7 дней, заканчивая последним днём с закрытыми ставками. rows: (local_date, итог $)."""
    if not rows:
        return None
    by_day = {}
    for d, v in rows:
        s = by_day.setdefault(d, [0.0, 0])
        s[0] += v
        s[1] += 1
    end = date.fromisoformat(max(by_day))
    days = [end - timedelta(days=i) for i in range(6, -1, -1)]
    vals = [by_day.get(d.isoformat(), [0.0, 0]) for d in days]
    top = max((abs(v) for v, _n in vals), default=0) or 1.0
    out = [{"wd": WEEKDAY_RU[d.weekday()], "date": f"{d:%d.%m}", "pnl": v, "n": n,
            "h": round(100 * abs(v) / top) if n else 0} for d, (v, n) in zip(days, vals)]
    total = sum(v for v, _n in vals)
    return {"days": out, "total": total, "n": sum(n for _v, n in vals),
            "won_days": sum(1 for v, n in vals if n and v > 0.005), "lost_days": sum(1 for v, n in vals if n and v < -0.005)}


def spark(rows, start=100.0, w=160, h=44):
    """Мини-график баланса для карточки кошелька: путь SVG или None (меньше 2 точек)."""
    by_day = {}
    for d, pnl in rows:
        by_day[d] = by_day.get(d, 0.0) + pnl
    vals, bal = [start], start
    for d in sorted(by_day):
        bal += by_day[d]
        vals.append(bal)
    if len(vals) < 2:
        return None
    lo, hi = min(vals + [start]), max(vals + [start])
    span = (hi - lo) or 1.0
    X = lambda i: 2 + (w - 4) * i / (len(vals) - 1)
    Y = lambda v: 3 + (h - 6) * (1 - (v - lo) / span)
    return {"w": w, "h": h, "path": smooth_path([(round(X(i), 1), round(Y(v), 1)) for i, v in enumerate(vals)]),
            "base": round(Y(start), 1), "end": (round(X(len(vals) - 1), 1), round(Y(vals[-1]), 1)), "up": vals[-1] >= start}


@app.get("/paper", response_class=HTMLResponse)
def paper(request: Request, w: str = ""):
    """Три таблицы (просьба Alex, 2026-09-23): ждём результата / результаты
    с объяснением / почему не ставили. Ставка после результата уходит из
    "ждём" в "результаты"; сам город продолжает торговаться дальше."""
    conn = db()
    wallets, waiting, results, skips = [], [], [], []
    outcomes = {}
    if table_exists(conn, "weather_poly_outcomes"):
        outcomes = {(r["city"], r["local_date"]): (r["win_lo"], r["win_hi"])
                    for r in conn.execute("SELECT city, local_date, win_lo, win_hi FROM weather_poly_outcomes")}
    has_reason = False
    if table_exists(conn, "paper_trades"):
        rows = conn.execute("SELECT * FROM paper_trades ORDER BY local_date DESC, city").fetchall()
        has_reason = "reason" in rows[0].keys() if rows else False
        for key, label in PAPER_WALLETS.items():
            wr = [r for r in rows if r["wallet"] == key]
            card = _wallet_card(label, wr, nofill=sum(r["status"] == "nofill" for r in wr),
                                skipped=sum(r["status"] == "skip" for r in wr))
            card["key"] = key
            wallets.append(card)
        for r in rows:
            wallet = WALLET_INFO.get(r["wallet"], (None, PAPER_WALLETS.get(r["wallet"], r["wallet"])))[1]
            if r["status"] in ("skip", "nofill"):
                skips.append({"local_date": r["local_date"], "city": r["city"], "wallet": wallet, "wkey": r["wallet"],
                              "kind": "сигнала нет" if r["status"] == "skip" else "не смогли купить",
                              "reason": r["reason"] if has_reason else None})
                continue
            bucket = paper_bucket(r["bucket_lo"], r["bucket_hi"], r["unit"])
            is_no = "side" in r.keys() and r["side"] == "no"
            item = {"local_date": r["local_date"], "city": r["city"], "wallet": wallet, "wkey": r["wallet"],
                    "what": f"против {bucket}" if is_no else f"на {bucket}", "price": r["price"], "stake": r["stake"], "fee": _fee(r),
                    "model_p": r["model_p"], "market_p": r["market_p"]}
            # 2026-09-25 (просьба Alex): сколько выиграем / проиграем по каждой открытой ставке
            sh = (r["want_shares"] if r["status"] == "resting" else r["shares"]) or 0.0
            item["shares"] = sh
            item["cost"] = r["stake"] + _fee(r)
            item["win_amt"] = sh - item["cost"]
            item["chance"] = r["market_p"]
            item["bought"] = r["status"] == "open"
            if r["status"] == "resting":
                item["what"] = (f"заявка на {bucket} по {r['limit_price']*100:.1f}¢, куплено "
                                f"{(r['shares'] or 0):.0f} из {r['want_shares']:.0f} долей")
                item["closes"] = "ждём продавца до 12:00 местного"
                waiting.append(item)
            elif r["status"] == "open":
                item["closes"] = expected_close(r["city"], r["local_date"])
                waiting.append(item)
            else:
                win = outcomes.get((r["city"], r["local_date"]))
                actual = paper_bucket(win[0], win[1], r["unit"]) if win else "?"
                item["result"] = _pnl(r)
                if is_no:
                    item["why"] = (f"Было {actual}, а не {bucket} — как и ставили" if r["status"] == "won"
                                   else "Маркет отменён — вернули половину" if r["status"] == "void"
                                   else f"Было ровно {actual} — против этого и ставили")
                else:
                    item["why"] = (f"Было {actual} — ровно то, на что ставили" if r["status"] == "won"
                                   else "Маркет отменён — вернули половину" if r["status"] == "void"
                                   else f"Было {actual}, а ставили на {bucket}")
                results.append(item)
    for okey in OBS_WALLETS if table_exists(conn, "paper_obs_trades") else ():
        obs = conn.execute("SELECT * FROM paper_obs_trades WHERE wallet = ? ORDER BY placed_at DESC", (okey,)).fetchall()
        label = WALLET_INFO[okey][1]
        src = "10-мин замер FMI" if okey == "obs_fmi" else "станция"
        card = _wallet_card(label, obs, nofill=sum(r["status"] == "nofill" for r in obs))
        card["key"] = okey
        wallets.append(card)
        for r in obs:
            unit = "°F" if r["unit"] == "fahrenheit" else "°C"
            bucket = paper_bucket(r["bucket_lo"], r["bucket_hi"], r["unit"])
            if r["status"] == "nofill":
                skips.append({"local_date": r["local_date"], "city": r["city"], "wallet": label, "wkey": okey,
                              "kind": "не смогли купить",
                              "reason": (f"{src} уже показал{'' if okey == 'obs_fmi' else 'а'} {r['obs_max']:.0f}{unit}, значит {bucket} невозможно; "
                                         + (r["reason"] if "reason" in r.keys() and r["reason"] else ""))})
                continue
            item = {"local_date": r["local_date"], "city": r["city"], "wallet": label, "wkey": okey,
                    "what": f"против {bucket} ({src} уже {r['obs_max']:.0f}{unit})",
                    "price": r["price"], "stake": r["stake"], "fee": _fee(r), "model_p": None, "market_p": None}
            if r["status"] == "open":
                item["closes"] = expected_close(r["city"], r["local_date"])
                item["shares"] = r["shares"] or 0.0
                item["cost"] = r["stake"] + _fee(r)
                item["win_amt"] = item["shares"] - item["cost"]
                item["chance"] = r["price"]  # против варианта: шанс по рынку ≈ цена купленной доли
                item["bought"] = True
                waiting.append(item)
            else:
                win = outcomes.get((r["city"], r["local_date"]))
                actual = paper_bucket(win[0], win[1], r["unit"]) if win else "?"
                item["result"] = _pnl(r)
                item["why"] = (f"Было {actual} — {bucket} и правда не случилось" if r["status"] == "won"
                               else f"Было {actual} — официальный итог разошёлся с замером станции")
                results.append(item)
    # 2026-09-25: аналитика счёта и данные рынка (идеи «инвест-платформы», просьба Alex)
    settled_by_wallet, settle_events = {}, {}
    for tbl, wcol in (("paper_trades", "wallet"), ("paper_obs_trades", "wallet")):
        if table_exists(conn, tbl):
            for r in conn.execute(f"SELECT {wcol} AS wallet, local_date, stake, fee, payout, settled_at, city, status "
                                  f"FROM {tbl} WHERE status IN ('won','lost','void')"):
                settled_by_wallet.setdefault(r["wallet"], []).append((r["local_date"], _pnl(r)))
                settle_events.setdefault(r["wallet"], []).append(
                    (r["settled_at"] or r["local_date"], _pnl(r), r["city"], r["status"]))
    # 2026-09-26 (просьба Alex): /paper — обзор всех кошельков, /paper?w=<ключ> — страница одного
    known = set(PAPER_WALLETS) | set(OBS_WALLETS)
    skills = {k: model_skill(conn, k) for k in known}
    skill = skills.get(w)
    fresh = data_freshness(conn)
    alerts = active_alerts(conn)
    upd = wallet_updates(conn)
    conn.close()
    for lst in (waiting, results, skips):
        lst.sort(key=lambda t: (t["local_date"], t["city"]), reverse=True)
    for c in wallets:
        info = WALLET_INFO.get(c["key"], ("Другое", c["label"], ""))
        c["group"], c["name"], c["desc"] = info
        c["badge"] = WALLET_BADGE.get(c["key"], c["key"][:2].upper())
        c["winrate"] = round(100 * c["won"] / c["n"]) if c["n"] else None
    for lst in (waiting, results, skips):
        for t in lst:
            t["city_ru"] = CITY_RU.get(t["city"], t["city"].replace("_", " ").title())
    main = next((c for c in wallets if c["key"] == MAIN_WALLET), None)
    groups = [(g, [c for c in wallets if c["group"] == g]) for g in WALLET_GROUPS]
    groups = [(g, cs) for g, cs in groups if cs]
    # 2026-09-25 (просьба Alex — переделать страницу целиком): страница всегда
    # про ОДИН кошелёк (по умолчанию главный), остальные — сравнение внизу.
    for c in wallets:
        c["upd"] = upd.get(c["key"])
        c["start"] = WALLET_START.get(c["key"], PAPER_START_BALANCE)
        c["balance"] = c["start"] + c["pnl"]
        c["spark"] = spark(settled_by_wallet.get(c["key"], []), c["start"])
        c["move"] = last_move(settle_events.get(c["key"], []))
        # 2026-09-27 (просьба Alex): сколько осталось на новые ставки — чтобы не пропустить, что кошелёк встал
        c["cash"] = c["balance"] - c["in_play"]
        c["low_cash"] = c["cash"] < PAPER_STAKE
        sk = skills.get(c["key"])
        c["skill"] = sk["tot"] if sk else None
        c["vs_mkt"] = 100 * (c["paid"] / c["cost"] - 1) if c.get("cost") else None
        c["luck"] = luck(c["pnl"], c.get("sd"))
    sel = w if any(c["key"] == w for c in wallets) else None
    cur = next((c for c in wallets if c["key"] == sel), None)
    pick = lambda lst: [t for t in lst if t.get("wkey") == sel]
    my_waiting = pick(waiting)
    my_waiting.sort(key=lambda t: (not t.get("bought"), -(t.get("chance") or 0)))
    closes = sorted(t["closes"] for t in my_waiting if t.get("bought") and t.get("closes"))
    goal = {"n": main["n"] if main else 0, "need": REAL_MONEY_THRESHOLD}
    goal["pct"] = min(100, round(100 * goal["n"] / goal["need"]))
    return TEMPLATES.TemplateResponse(
        "paper.html",
        {"request": request, "main": main, "cur": cur, "groups": groups, "wallets": wallets, "sel": sel,
         "scen": scenarios(my_waiting), "skill": skill, "fresh": fresh, "alerts": alerts, "closes": (closes[0], closes[-1]) if closes else None,
         "chart": balance_chart(settle_events.get(sel, []), cur["start"] if cur else PAPER_START_BALANCE), "goal": goal,
         "waiting": my_waiting, "results": pick(results), "skips": pick(skips)[:150],
         "start": cur["start"] if cur else PAPER_START_BALANCE, "doc": WALLET_DOCS.get(sel) if sel else None,
         "week": week_bars(settled_by_wallet.get(sel, [])) if sel else None},
    )


# 2026-09-26 (просьба Alex): страница «Обучение модели» — что делало ночное
# обучение (ml_train_log, пишет weather_ml_report.py из weather_ml_live --train).
FC_MODEL_RU = {
    "ecmwf_ifs025": "ECMWF (Европа)", "ecmwf_aifs025_single": "ECMWF AI (Европа)", "ukmo_seamless": "UK Met Office",
    "meteofrance_seamless": "Météo-France", "icon_seamless": "ICON (Германия)", "gfs_seamless": "GFS (США)",
    "gem_seamless": "GEM (Канада)", "jma_seamless": "JMA (Япония)", "cma_grapes_global": "CMA (Китай)",
    "ncep_nbm_conus": "NBM (США)", "knmi_seamless": "KNMI (Нидерланды)", "metno_seamless": "MET Norway",
    "dmi_seamless": "DMI (Дания)", "meteoswiss_icon_ch1": "MeteoSwiss", "italia_meteo_arpae_icon_2i": "ItaliaMeteo",
    "bom_access_global": "BOM (Австралия)",
}
FEATURE_RU = {
    "city_id": "город", "lat": "широта города", "lon": "долгота города", "doy_sin": "время года", "doy_cos": "время года (2)",
    "fc_mean": "среднее 16 погодных моделей", "fc_std": "насколько модели расходятся", "fc_min": "самый холодный прогноз",
    "fc_max": "самый тёплый прогноз", "fv_cloud_cover": "прогноз облачности днём", "fv_dew_point_2m": "прогноз точки росы",
    "fv_precipitation": "прогноз осадков", "fv_relative_humidity_2m": "прогноз влажности", "fv_shortwave_radiation": "прогноз солнца",
    "fv_wind_dir_cos": "прогноз направления ветра", "fv_wind_dir_sin": "прогноз направления ветра (2)",
    "fv_wind_speed_10m": "прогноз скорости ветра", "obs_t": "утренняя температура на станции", "obs_dew": "утренняя точка росы",
    "obs_spread": "утром: температура минус точка росы", "obs_tmin": "ночной минимум", "obs_vs_fc": "утренний замер против прогноза",
    "obs_alti": "давление утром", "obs_wind": "ветер утром", "obs_wind_sin": "направление ветра утром",
    "obs_wind_cos": "направление ветра утром (2)", "obs_cloud": "облака утром", "obs_dt3h": "как менялась температура за 3 ч",
    "obs_dalti3h": "как менялось давление за 3 ч", "prev_actual_vs_fc": "вчерашний максимум против прогноза",
    "mix_vs_fc": "микс моделей против среднего", "prev_err": "вчерашняя ошибка прогноза",
    "mkt_mean_vs_fc": "мнение рынка: ожидаемый максимум", "mkt_std": "мнение рынка: неуверенность",
    "mkt_top_p": "мнение рынка: шанс лидера",
}


def feature_ru(name):
    if name in FEATURE_RU:
        return FEATURE_RU[name]
    if name.startswith("fc_"):
        return "прогноз " + FC_MODEL_RU.get(name[3:], name[3:])
    return name


EXAM_EVEN_PP = 0.5    # старые отчёты без логошибки: разница в шансе меньше 0.5 п.п. — «наравне»
EXAM_EVEN_LL = 0.015  # логошибка: меньше 0.015 — «наравне» (обычный разброс между переобучениями)


def _gap_verdict(ex, who):
    """Сравнение с рынком: по логошибке (ниже — лучше), в старых отчётах — по среднему шансу."""
    ll, llm = ex.get(f"ll_{who}"), ex.get("ll_market")
    if ll is not None and llm is not None:
        gap = llm - ll
        even, fmt = EXAM_EVEN_LL, f"{abs(gap):.3f}"
        hint = f"логошибка {ll:.3f}, у рынка {llm:.3f} (меньше — лучше)"
    else:
        gap = ex[f"p_{who}"] - ex["p_market"]
        even, fmt = EXAM_EVEN_PP, f"{abs(gap):.1f} п.п."
        hint = f"шанс тому, что случилось: {ex[f'p_{who}']:.1f}%, у рынка {ex['p_market']:.1f}%"
    if abs(gap) < even:
        v = {"tone": "even", "label": "наравне с рынком"}
    elif gap > 0:
        v = {"tone": "ahead", "label": f"опережаем на {fmt}"}
    else:
        v = {"tone": "behind", "label": f"отстаём на {fmt}"}
    v["hint"] = hint
    return v


def exam_verdict(exam):
    """2026-09-27 (просьба Alex): итог экзамена — опережаем рынок или отстаём, для модели и отдельной
    строкой для смеси 35% модели + 65% рынка (на неё ставят кошельки «+ рынок»).
    Мерило — логошибка: штрафует уверенность в неправильном ответе; средний шанс поощряет
    самоуверенность, поэтому в отчётах без логошибки (до 27.09) итог по нему — ориентировочный."""
    if not exam or exam.get("p_model") is None or exam.get("p_market") is None:
        return None
    v = _gap_verdict(exam, "model")
    em, ek = exam.get("err_model"), exam.get("err_market")
    if em is not None and ek is not None:
        v["hint"] += f"; ошибка в градусах: модель {em:.2f}°, рынок {ek:.2f}°"
    if exam.get("p_blend") is not None:
        v["blend"] = _gap_verdict(exam, "blend")
    return v


@app.get("/training", response_class=HTMLResponse)
def training(request: Request):
    conn = db()
    runs = []
    if table_exists(conn, "ml_train_log"):
        for r in conn.execute("SELECT trained_at, ok, details FROM ml_train_log ORDER BY trained_at DESC LIMIT 60"):
            try:
                d = json.loads(r["details"])
            except ValueError:
                continue
            d["ok"] = bool(r["ok"])
            d["when"] = datetime.fromisoformat(r["trained_at"]).astimezone(VIEWER_TZ).strftime("%d.%m %H:%M")
            for it in d.get("importance", []):
                it["ru"] = feature_ru(it["name"])
            d["verdict"] = exam_verdict(d.get("exam"))
            runs.append(d)
    conn.close()
    last = runs[0] if runs else None
    if last and last.get("importance"):
        top = max(i["pct"] for i in last["importance"]) or 1
        for i in last["importance"]:
            i["bar"] = round(100 * i["pct"] / top)
    conn = db()
    alerts = active_alerts(conn)
    conn.close()
    return TEMPLATES.TemplateResponse("training.html", {"request": request, "last": last, "runs": runs, "alerts": alerts})


# ---- /bets: все ставки всех кошельков — открытые и закрытые отдельно (2026-09-27, просьба Alex) ----
# Оформление по пяти присланным макетам: пастельные плитки (Payoneer), «движение денег» по дням (Fundcy),
# «последние» плиткой 2×2 со статусами (Finance Health), «ждут итога» с датой квадратиком (Upcoming Payments),
# закрытые списком с круглым значком (Analytics / VISA).
GROUP_TONE = {"Главная модель": "g1", "Другие версии обучаемой модели": "g2", "Прогноз по формулам (раньше)": "g3",
              "Тот же сигнал, но покупка своей заявкой": "g4", "Повтор за сильными трейдерами": "g5", "Живые замеры": "g5"}


def _close_dt(city, local_date):
    from weather_cities import OBS_CITIES
    cfg = OBS_CITIES.get(city)
    if cfg is None:
        return None
    d = date.fromisoformat(local_date) + timedelta(days=1)
    return datetime(d.year, d.month, d.day, tzinfo=ZoneInfo(cfg["tz"])) + timedelta(hours=3)


def _wallet_meta(key):
    info = WALLET_INFO.get(key, ("Другое", PAPER_WALLETS.get(key, key), ""))
    group = "Главная модель" if key == "ml3" else info[0]
    return {"key": key, "name": info[1], "badge": WALLET_BADGE.get(key, key[:2].upper()), "tone": GROUP_TONE.get(group, "g2")}


def all_bets(conn):
    rows = []
    if table_exists(conn, "paper_trades"):
        rows += [dict(r, _tbl="p") for r in conn.execute(
            "SELECT * FROM paper_trades WHERE status IN ('open', 'resting', 'won', 'lost', 'void')")]
    if table_exists(conn, "paper_obs_trades"):
        rows += [dict(r, _tbl="o") for r in conn.execute(
            "SELECT * FROM paper_obs_trades WHERE status IN ('open', 'won', 'lost', 'void')")]
    out = []
    for r in rows:
        is_no = r["_tbl"] == "o" or r.get("side") == "no"
        bucket = paper_bucket(r["bucket_lo"], r["bucket_hi"], r["unit"])
        cost = (r["stake"] or 0) + _fee(r)
        sh = (r.get("want_shares") if r["status"] == "resting" else r.get("shares")) or 0.0
        placed = _dt(r.get("placed_at") or r.get("snapshot_ts"))
        b = {"w": _wallet_meta(r["wallet"]), "city": r["city"], "city_ru": CITY_RU.get(r["city"], r["city"].replace("_", " ").title()),
             "local_date": r["local_date"], "what": ("против " if is_no else "на ") + bucket, "price": r["price"],
             "cost": cost, "shares": sh, "win_amt": sh - cost, "status": r["status"], "placed": placed,
             "settled": _dt(r.get("settled_at")), "pnl": _pnl(r) if r["status"] in ("won", "lost", "void") else None,
             "close": _close_dt(r["city"], r["local_date"])}
        out.append(b)
    return out


MONTH_RU = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


@app.get("/bets", response_class=HTMLResponse)
def bets_page(request: Request, w: str = "", m: str = ""):
    conn = db()
    try:
        bets = all_bets(conn)
    finally:
        conn.close()
    wallets = sorted({(b["w"]["key"], b["w"]["name"]) for b in bets}, key=lambda x: x[1])
    if w:
        bets = [b for b in bets if b["w"]["key"] == w]
    now = datetime.now(timezone.utc)
    nv = now.astimezone(VIEWER_TZ)
    day_ago = now - timedelta(days=1)
    # коротко: сегодня — только время, иначе «вчера 20:10» / дата (на телефоне длинное не влезает)
    short = lambda t: f"{t.astimezone(VIEWER_TZ):%H:%M}" if t.astimezone(VIEWER_TZ).date() == nv.date() else _when(t, nv)
    opened = [b for b in bets if b["status"] in ("open", "resting")]
    for b in opened:
        c = b["close"]
        b["close_badge"] = c.astimezone(VIEWER_TZ) if c else None
        b["close_txt"] = _when(c, nv) if c else "—"
        b["placed_txt"] = short(b["placed"]) if b["placed"] else "—"
    opened.sort(key=lambda b: (b["close"] or now + timedelta(days=9), b["w"]["name"]))
    closed = sorted([b for b in bets if b["settled"]], key=lambda b: b["settled"], reverse=True)
    for b in closed:
        b["settled_txt"] = short(b["settled"])
    week_ago = now - timedelta(days=7)
    closed_week = [b for b in closed if b["settled"] >= week_ago]
    # «только что» — последние события: открытия и закрытия вперемешку
    ev = [(b["placed"], "open", b) for b in bets if b["placed"]] + [(b["settled"], "closed", b) for b in closed]
    ev.sort(key=lambda x: x[0], reverse=True)
    latest = [{"kind": k if k == "open" else ("in" if b["pnl"] > 0.005 else "out"), "b": b, "when": short(t)} for t, k, b in ev[:4]]
    # движение денег за 14 дней: поставлено (по дню покупки) и вернулось (по дню расчёта)
    # 2026-09-27 (просьба Alex): период — последние 14 дней или выбранный месяц (m=ГГГГ-ММ)
    first_day = min((b["placed"].astimezone(VIEWER_TZ).date() for b in bets if b["placed"]), default=nv.date())
    months, y, mo = [], first_day.year, first_day.month
    while (y, mo) <= (nv.year, nv.month):
        months.append({"key": f"{y}-{mo:02d}", "name": f"{MONTH_RU[mo - 1]} {y}"})
        y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
    month = next((x for x in months if x["key"] == m), None)
    if month:
        y, mo = map(int, month["key"].split("-"))
        d0 = date(y, mo, 1)
        d1 = min((date(y + (mo == 12), mo % 12 + 1, 1) - timedelta(days=1)), nv.date())
        days = [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]
    else:
        days = [(nv - timedelta(days=i)).date() for i in range(13, -1, -1)]
    staked = {d: 0.0 for d in days}
    back = {d: 0.0 for d in days}
    # 2026-09-27 (просьба Alex): по нажатию на день — сколько в плюс и сколько в минус
    det = {d: {"open_n": 0, "won_n": 0, "won": 0.0, "lost_n": 0, "lost": 0.0} for d in days}
    for b in bets:
        if b["placed"]:
            d = b["placed"].astimezone(VIEWER_TZ).date()
            if d in staked:
                staked[d] += b["cost"]
                det[d]["open_n"] += 1
        if b["settled"]:
            d = b["settled"].astimezone(VIEWER_TZ).date()
            if d in back:
                back[d] += b["pnl"] + b["cost"]
                if b["pnl"] > 0.005:
                    det[d]["won_n"] += 1
                    det[d]["won"] += b["pnl"]
                elif b["pnl"] < -0.005:
                    det[d]["lost_n"] += 1
                    det[d]["lost"] += b["pnl"]
    top = max([*staked.values(), *back.values(), 0.01])
    flow = [{"wd": WEEKDAY_RU[d.weekday()], "date": f"{d:%d.%m}", "staked": staked[d], "back": back[d],
             "hs": round(100 * staked[d] / top), "hb": round(100 * back[d] / top), **det[d],
             "net": det[d]["won"] + det[d]["lost"], "day": d.day,
             "label": (not month) or d.day in (1, 5, 10, 15, 20, 25) or d == days[-1]} for d in days]
    kpi = {
        "open24_n": sum(1 for b in bets if b["placed"] and b["placed"] >= day_ago),
        "open24_usd": sum(b["cost"] for b in bets if b["placed"] and b["placed"] >= day_ago),
        "closed24_n": sum(1 for b in closed if b["settled"] >= day_ago),
        "closed24_pnl": sum(b["pnl"] for b in closed if b["settled"] >= day_ago),
        "wait_n": len(opened), "wait_usd": sum(b["cost"] for b in opened),
        "flow_staked": sum(staked.values()), "flow_back": sum(back.values()),
    }
    return TEMPLATES.TemplateResponse("bets.html", {
        "request": request, "opened": opened, "closed": closed_week, "latest": latest, "flow": flow, "kpi": kpi,
        "wallets": wallets, "sel": w, "sel_name": dict(wallets).get(w), "months": months, "month": month})


# ---- /events: что делает система — обучение, данные, итоги, тревоги (2026-09-27, просьба Alex) ----
FREQUENT_JOBS = {"weather_obs_live", "weather_copy", "weather_alerts", "weather_ml_fast", "weather_poly_resolve", "weather_paper"}
JOB_DONE = {
    "weather_edge": "Обновлены цены рынка и прогнозы всех моделей",
    "weather_station_obs": "Загружены замеры станций (METAR) — факт температуры",
    "weather_multimodel": "Загружены прогнозы 16 погодных моделей",
    "weather_ml_data": "Загружены прогнозные условия (облака, ветер, влажность)",
    "weather_ens": "Собраны ансамбли прогнозов",
    "weather_trades_history": "Собраны настоящие сделки Polymarket за 7 дней",
    "weather_sharp_rank": "Обновлён рейтинг сильных трейдеров",
    "weather_price_history": "Загружена история цен",
    "weather_ml_skill": "Пересчитано «насколько модель права»",
    "weather_ml_train": "Ночное обучение: скрипт отработал",
}


def system_events(conn, days=3):
    from jobs_info import JOBS
    label = {k: l for k, l, *_ in JOBS}
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    ev = []

    def add(t, kind, title, detail="", link=None):
        if t and t >= since:
            ev.append({"t": t, "kind": kind, "title": title, "detail": detail, "link": link})

    from fixes import is_fixed, last_fixes
    fx = last_fixes(conn)
    if table_exists(conn, "job_log"):
        ie = ", item_errors" if "item_errors" in [c[1] for c in conn.execute("PRAGMA table_info(job_log)")] else ", 0 AS item_errors"
        for r in conn.execute(f"SELECT job, started_at, finished_at, rc, duration_s, om_calls{ie} FROM job_log WHERE finished_at >= ?",
                              (since.isoformat(),)):
            t = _dt(r["finished_at"])
            dur = f"{r['duration_s']:.0f} с" if r["duration_s"] < 90 else f"{r['duration_s'] / 60:.0f} мин"
            if r["job"] not in label:
                continue  # ручные запуски (проверки, пробное обучение) — не события системы
            if (r["rc"] != 0 or r["item_errors"]) and is_fixed(fx, r["job"], r["finished_at"]):
                # 2026-09-28: исправленные ошибки — в истории остаются, но зелёным и с тем, что сделали (fixes.py)
                what = "упал" if r["rc"] != 0 else f"отработал с пропусками ({r['item_errors']})"
                add(t, "ok", f"Исправлено: «{label.get(r['job'], r['job'])}» {what}",
                    f"исправлено {fx[r['job']][0].astimezone(VIEWER_TZ):%d.%m %H:%M}: {fx[r['job']][1]}")
            elif r["rc"] != 0:
                why = "остановлен по пределу времени" if r["rc"] in (124, 137, 143) else f"код выхода {r['rc']}"
                add(t, "fail", f"Ошибка: «{label.get(r['job'], r['job'])}»", f"{why} · шёл {dur} · подробности на странице «Здоровье системы»", "/status")
            elif r["item_errors"]:
                add(t, "fail", f"С пропусками: «{label.get(r['job'], r['job'])}»",
                    f"отработал, но пропущено из-за ошибок: {r['item_errors']} — остальное обработано · подробности на странице «Здоровье системы»", "/status")
            elif r["job"] in JOB_DONE and r["job"] not in FREQUENT_JOBS and r["job"] != "weather_ml_train":
                extra = f" · запросов к Open-Meteo: {r['om_calls']}" if r["om_calls"] else ""
                add(t, "data", JOB_DONE.get(r["job"], f"Отработал «{label.get(r['job'], r['job'])}»"), f"за {dur}{extra}")
    if table_exists(conn, "ml_train_log"):
        for r in conn.execute("SELECT trained_at, ok, details FROM ml_train_log WHERE trained_at >= ?", (since.isoformat(),)):
            try:
                d = json.loads(r["details"])
            except ValueError:
                continue
            v = exam_verdict(d.get("exam"))
            parts = [f"данных: {d['data']['rows']} город-дней" + (f" (+{d['data']['new_rows']})" if d["data"].get("new_rows") else "")]
            if v:
                parts.append(f"экзамен: модель — {v['label']}" + (f", смесь — {v['blend']['label']}" if v.get("blend") else ""))
            if not r["ok"]:
                parts.append("не все проверки пройдены")
            add(_dt(r["trained_at"]), "train", "Пробное обучение модели" if d.get("dry_run") else "Модель переобучилась",
                " · ".join(parts), "/training")
    if table_exists(conn, "weather_poly_outcomes"):
        groups = {}
        for r in conn.execute("SELECT city, resolved_at FROM weather_poly_outcomes WHERE resolved_at >= ?", (since.isoformat(),)):
            t = _dt(r["resolved_at"])
            if t:
                groups.setdefault(t.replace(second=0, microsecond=0), []).append(CITY_RU.get(r["city"], r["city"]))
        for t, cities in groups.items():
            n = len(cities)
            add(t, "result", f"Пришли итоги {n} {'маркета' if n % 10 == 1 and n % 100 != 11 else 'маркетов'}",
                ", ".join(sorted(cities)[:12]) + (f" и ещё {n - 12}" if n > 12 else ""))
    # ставки — сводкой по каждому запуску; подробно — на странице «Ставки»
    bets = all_bets(conn)
    runs_open, runs_close = {}, {}
    for b in bets:
        if b["placed"] and b["placed"] >= since:
            # по часам: кошелёк copy покупает по одной ставке — иначе лента тонет в «открыто: 1»
            runs_open.setdefault(b["placed"].replace(minute=0, second=0, microsecond=0), []).append(b)
        if b["settled"] and b["settled"] >= since:
            runs_close.setdefault(b["settled"].replace(second=0, microsecond=0), []).append(b)
    for t, bs in runs_open.items():
        ws = sorted({b["w"]["name"] for b in bs})
        last = max(b["placed"] for b in bs)
        add(last, "bets", f"Открыто ставок: {len(bs)} на ${sum(b['cost'] for b in bs):.2f}",
            ", ".join(ws[:4]) + (f" и ещё {len(ws) - 4}" if len(ws) > 4 else ""), "/bets")
    for t, bs in runs_close.items():
        pnl = sum(b["pnl"] for b in bs)
        won = sum(1 for b in bs if b["pnl"] > 0.005)
        add(t, "bets", f"Закрыто ставок: {len(bs)} · угадано {won} · итог {'+' if pnl >= 0 else '−'}${abs(pnl):.2f}",
            ", ".join(sorted({b["w"]["name"] for b in bs})[:4]), "/bets")
    if table_exists(conn, "alerts"):
        for r in conn.execute("SELECT message, first_seen, resolved_at FROM alerts"):
            add(_dt(r["first_seen"]), "alert", "Тревога", r["message"], "/status")
            if r["resolved_at"]:
                add(_dt(r["resolved_at"]), "ok", "Тревога снята", r["message"])
    f = DB_PATH.parent.parent / "ALERT_DB_LOCKED"
    try:
        add(datetime.fromtimestamp(f.stat().st_mtime, timezone.utc), "fail", "Сторож базы: база была занята", f.read_text().strip()[:300], "/status")
    except OSError:
        pass
    ev.sort(key=lambda e: e["t"], reverse=True)
    nv = now.astimezone(VIEWER_TZ)
    days_out = []
    for e in ev:
        d = e["t"].astimezone(VIEWER_TZ)
        e["hm"] = f"{d:%H:%M}"
        key = d.date()
        head = "Сегодня" if key == nv.date() else ("Вчера" if key == nv.date() - timedelta(days=1) else f"{d:%d.%m}")
        if not days_out or days_out[-1]["key"] != key:
            days_out.append({"key": key, "head": head, "rows": []})
        days_out[-1]["rows"].append(e)
    counts = {k: sum(1 for e in ev if e["kind"] == k and e["t"] >= now - timedelta(days=1))
              for k in ("train", "data", "result", "bets", "alert", "fail", "ok")}
    return days_out, counts


@app.get("/events", response_class=HTMLResponse)
def events_page(request: Request):
    conn = db()
    try:
        days, counts = system_events(conn)
    finally:
        conn.close()
    return TEMPLATES.TemplateResponse("events.html", {"request": request, "days": days, "counts": counts})


# ---- /audit: проверка кошельков, моделей и базы (2026-09-27, просьба Alex) ----
# Считает weather_audit.py по крону (audit_log); здесь — только показ последнего результата.
@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request):
    conn = db()
    last, history = None, []
    try:
        if table_exists(conn, "audit_log"):
            rows = conn.execute("SELECT run_at, ok, deep, details FROM audit_log ORDER BY run_at DESC LIMIT 36").fetchall()
            if rows:
                last = json.loads(rows[0]["details"])
            nv = datetime.now(VIEWER_TZ)
            history = [{"ok": bool(r["ok"]), "deep": bool(r["deep"]), "when": _when(_dt(r["run_at"]), nv)} for r in reversed(rows)]
    finally:
        conn.close()
    if last:
        nv = datetime.now(VIEWER_TZ)
        last["when"] = _when(_dt(last["run_at"]), nv)
        for w in last["wallets"]:
            w["name"] = _wallet_meta(w["key"])["name"]
            w["main"] = w["key"] == "ml3"
        last["wallets"].sort(key=lambda w: (not w["main"], -w["n_viol"], w["name"]))
        db_ = last["db"]
        db_["size_txt"] = _size(db_.get("size", 0))
        if db_.get("quick_check_at"):
            db_["qc_when"] = _when(_dt(db_["quick_check_at"]), nv)
        tr = last["data"].get("train")
        if tr:
            tr["when"] = _when(_dt(tr["at"]), nv)
            tr["verdict"] = exam_verdict(tr.get("exam"))
        if last["data"].get("last_snap"):
            last["data"]["last_snap_when"] = _when(_dt(last["data"]["last_snap"]), nv)
        if last["cron"].get("since"):
            last["cron"]["since_when"] = _when(_dt(last["cron"]["since"]), nv)
    # 2026-09-28: вечерняя проверка перед ночью (weather_night_check.py, 23:30)
    night = None
    conn = db()
    try:
        if table_exists(conn, "night_check"):
            # 2026-09-28: три проверки в день — последняя каждого режима; вкладки «Утро / День / Вечер»
            has_mode = "mode" in [r[1] for r in conn.execute("PRAGMA table_info(night_check)")]
            night = []
            for mode, name, at in (("morning", "Утро", "07:00"), ("midday", "День", "13:00"), ("evening", "Вечер", "23:30")):
                r = conn.execute("SELECT details FROM night_check " + ("WHERE mode = ? " if has_mode else "WHERE ? = 'evening' ")
                                 + "ORDER BY run_at DESC LIMIT 1", (mode,)).fetchone()
                n = {"mode": mode, "mode_name": name, "at": at, "empty": True}
                if r:
                    n = json.loads(r["details"])
                    n.update(mode=mode, mode_name=name, at=at, empty=False, when=_when(_dt(n["run_at"]), datetime.now(VIEWER_TZ)))
                    groups = {}
                    for x in n["checks"]:
                        groups.setdefault(x["group"], []).append(x)
                    n["groups"] = [(g, xs, sum(x["level"] != "ok" for x in xs)) for g, xs in groups.items()]
                    n["problems"] = [x for x in n["checks"] if x["level"] != "ok"]
                    n["_t"] = n["run_at"]
                night.append(n)
            latest = max((n for n in night if not n["empty"]), key=lambda n: n["_t"], default=None)
            for n in night:
                n["active"] = latest is not None and n["mode"] == latest["mode"]
    finally:
        conn.close()
    return TEMPLATES.TemplateResponse("audit.html", {"request": request, "a": last, "history": history, "night": night})


# ---- заметки с датой (2026-09-28, просьба Alex: «до 12 октября я всё забуду») — notes.py ----
@app.get("/notes", response_class=HTMLResponse)
def notes_page(request: Request):
    import notes
    today = notes.today()
    items = notes.all_notes()
    for n in items:
        n["days"] = (date.fromisoformat(n["due"]) - date.fromisoformat(today)).days
    groups = [("Сегодня и просрочено", [n for n in items if not n["done_at"] and n["days"] <= 0], "due"),
              ("Впереди", [n for n in items if not n["done_at"] and n["days"] > 0], "next"),
              ("Сделано", [n for n in items if n["done_at"]], "done")]
    return TEMPLATES.TemplateResponse("notes.html", {"request": request, "groups": groups, "today": today})


@app.post("/notes/add")
async def notes_add(request: Request):
    import notes
    d = await request.json()
    try:
        nid = notes.add(str(d.get("due", "")), str(d.get("title", "")), str(d.get("body", "")))
    except ValueError as e:
        return {"ok": False, "error": f"не сохранено: {e}"}
    _NAV_CACHE["v"] = None
    return {"ok": True, "id": nid}


@app.post("/notes/{note_id}/{action}")
def notes_action(note_id: int, action: str):
    import notes
    if action == "done":
        notes.set_done(note_id, True)
    elif action == "undo":
        notes.set_done(note_id, False)
    elif action == "delete":
        notes.delete(note_id)
    else:
        return {"ok": False}
    _NAV_CACHE["v"] = None
    return {"ok": True}


# ---- счётчики боковой панели (2026-09-27, панель «Меню и инструменты», просьба Alex) ----
_NAV_CACHE = {"t": 0.0, "v": None}


def nav_counts():
    """Кошельков, открытых ставок, активных тревог, итог последней проверки — для панели на всех страницах.
    Кэш 60 с; база занята или ошибка — панель просто без счётчиков."""
    if _NAV_CACHE["v"] is not None and time.time() - _NAV_CACHE["t"] < 60:
        return _NAV_CACHE["v"]
    v = {"wallets": len(set(PAPER_WALLETS) | set(OBS_WALLETS)), "open": None, "alerts": 0, "audit": None}
    try:
        import notes
        v["notes"] = len(notes.due_notes())  # на сегодня и просроченные
    except sqlite3.Error:
        v["notes"] = 0
    try:
        conn = db()
        try:
            n = 0
            if table_exists(conn, "paper_trades"):
                n += conn.execute("SELECT COUNT(*) FROM paper_trades WHERE status IN ('open', 'resting')").fetchone()[0]
            if table_exists(conn, "paper_obs_trades"):
                n += conn.execute("SELECT COUNT(*) FROM paper_obs_trades WHERE status = 'open'").fetchone()[0]
            v["open"] = n
            v["alerts"] = len(active_alerts(conn))
            if table_exists(conn, "audit_log"):
                r = conn.execute("SELECT ok, details FROM audit_log ORDER BY run_at DESC LIMIT 1").fetchone()
                if r:
                    v["audit"] = 0 if r["ok"] else json.loads(r["details"]).get("n_violations", 1)
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    _NAV_CACHE.update(t=time.time(), v=v)
    return v


TEMPLATES.env.globals["nav_counts"] = nav_counts
TEMPLATES.env.globals["verdict_min_n"] = VERDICT_MIN_N
