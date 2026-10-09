"""
Веб-дашборд поверх sqlite, который пишет weather_edge.py по крону.
Только чтение, ничего не торгует. Порт 8093, чтобы не пересекаться с
gold-sim (8090-8092).
"""

import json
import math
import os
import re
import sqlite3
import time
from pathlib import Path

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
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


def table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


# 06.10 (решение Alex): вкладка «Погода» (/, /city/<город>) удалена — остаток первого прототипа: перевесы модели
# по формулам (кошелёк main), а не главной v3; ею не пользовались. Главная страница — кошельки.
@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse("/paper")


VIEWER_TZ = ZoneInfo("Europe/Chisinau")


def _kiev_time(ts):
    """Время из базы (UTC, ISO) → «06.10 18:05» по Кишинёву; нет — None."""
    if not ts:
        return None
    t = datetime.fromisoformat(ts)
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(VIEWER_TZ).strftime("%d.%m %H:%M")


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
    "ml3_cal30": "Смесь — не дешевле 30¢",
    "ml3_z": "Смесь — «да» 20-40¢",
    "no_cheap": "Против лотерейных билетов",
    "no_mid": "Против средних вариантов",
    "no_big": "Против сильно переоценённых",
    "techno": "Дешёвые «да» среди фаворитов",
    "ml3_conf": "Смесь — не спорить с уверенным рынком",
    "ml5_cal": "v5 «от рынка» + рынок (смесь)",
    "ml5": "v5 «от рынка» — сама модель",
    "ml_day": "Дневная модель (10/12/14 ч)",
    "fav": "Недооценённые фавориты",
    "ml3_city": "Смесь — лучшие города",
    "copy": "Повтор за сильными трейдерами",
    # двойники: те же сигналы, но покупают своей заявкой (без комиссии, по нижней цене)
    "main_mk": "Основная модель — своя заявка", "emos_mk": "EMOS — своя заявка", "mm_mk": "Микс — своя заявка",
    "ml3_mk": "Главная v3 — своя заявка",
}
PAPER_START_BALANCE = 100.0
# свой старт у кошелька (как weather_paper.START_BY_WALLET; совпадение проверяет preflight.py)
WALLET_START = {"copy": 1000.0, "ml": 300.0, "mm_mk": 300.0, "mm": 300.0}  # как weather_paper.START_BY_WALLET (preflight сверяет)
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
    "ml3_z": ("Другие версии обучаемой модели", "Смесь — «да» только 20-40¢",
              "Как «смесь», но «да» только за 20-40¢: единственная зона в плюсе и на честной истории (покупка по цене продавца), и на первой живой неделе"),
    "ml3_cal30": ("Другие версии обучаемой модели", "Смесь — не дешевле 30¢",
                  "Как «смесь», но выбирает только среди вариантов за 30¢ и дороже: дешёвые сбываются реже своей цены (разбор 27-29.09)"),
    "ml3_cal15": ("Другие версии обучаемой модели", "Смесь — не дешевле 15¢",
                  "Как «смесь», но не ставит на варианты дешевле 15¢: на всём рынке они сбываются реже своей цены"),
    "ml3_city": ("Другие версии обучаемой модели", "Смесь — лучшие города",
                 "Как «смесь», но только в половине городов, где она последние 45 дней была лучше рынка сильнее всего (пересчёт по понедельникам)"),
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
    "no_mid": ("Перекосы рынка", "Против средних вариантов",
               "Покупает «нет» на вариант за 30-55¢, если смесь модели и рынка считает его переоценённым на 3+ п.п."),
    "ml_day": ("Другие версии обучаемой модели", "Дневная модель (10/12/14 ч)",
               "LightGBM в 10, 12 и 14 часов видит замеры с утра и цену рынка в этот час; смесь 35/65 с рынком, перевес от 3 п.п., одна ставка на город в день"),
    "ml5": ("Другие версии обучаемой модели", "v5 «от рынка» — сама модель",
            "Чистая v5 без смеси с рынком, перевес от 10 п.п. — пара к главной v3 (ml3): какая модель сама по себе зарабатывает больше"),
    "ml5_cal": ("Другие версии обучаемой модели", "v5 «от рынка» + рынок (смесь)",
                "Модель учит не температуру, а поправку к рынку — где и насколько рынок ошибается; смесь 35/65 с рынком, перевес от 3 п.п."),
    "ml3_conf": ("Другие версии обучаемой модели", "Смесь — не спорить с уверенным рынком",
                 "Как смесь v3 + рынок, но без ставок в маркетах, где фаворит стоит 60¢ и дороже: там рынок обычно прав"),
    "no_big": ("Перекосы рынка", "Против сильно переоценённых",
               "Покупает «нет» на вариант за 25-80¢, если смесь модели и рынка считает его переоценённым на 8+ п.п. (правило бота AadiXD200)"),
    "techno": ("Перекосы рынка", "Дешёвые «да» среди фаворитов",
               "Покупает «да» за 8-30¢ на одном из 4 самых дорогих вариантов, если смесь модели и рынка не ниже цены (правило бота technosheen)"),
    "fav": ("Перекосы рынка", "Недооценённые фавориты",
            "Покупает «да» на вариант за 50-95¢, который смесь модели и рынка считает недооценённым: рынок недоплачивает за фаворитов"),
    "copy": ("Повтор за сильными трейдерами", "Повтор за сильными трейдерами",
             "Повторяет покупки 30 лучших трейдеров погоды за 14 дней — только сделанные накануне дня маркета, не дороже их цены +2¢"),
    "llm_gem": ("LLM каждый час", "LLM Gemini — прогноз каждый час",
                "Gemini 3.8 Flash каждый час 08-19 видит замеры с утра, почасовой прогноз облаков и дождя, прогнозы моделей, "
                "цены и свои прошлые ошибки — и называет максимум дня. 5 городов США, одна ставка на город в день"),
    "llm_ds": ("LLM каждый час", "LLM DeepSeek — прогноз каждый час",
               "То же, что LLM Gemini, но DeepSeek V4 Pro — для сравнения двух LLM"),
    "llm_cal": ("LLM каждый час", "Gemini + рынок",
                "Шансы Gemini этого часа 35% + цена рынка 65% — лекарство от самоуверенности LLM; ставит при перевесе от 10 п.п. Своих запросов нет — $0"),
    "llm_agy": ("LLM каждый час", "Gemini через Antigravity",
                "Gemini 3.8 Flash (Medium) через Antigravity CLI на сервере — бесплатно по аккаунту Google. Короткое письмо на английском, "
                "ответ — только максимум дня; шансы по вариантам считает код по разбросу прошлых ошибок. 5 городов США, одна ставка на город в день"),
    "llm_agy_bet": ("LLM каждый час", "Gemini ставит сама",
                    "Пара к «Gemini через Antigravity»: та же модель и данные, но сама решает — ставить или ждать, на что, «да» или «нет», "
                    "сколько ($0-5) и до какой цены. Код держит рамки риска: до $5, одна ставка на город в день, за день до 20% денег"),
    "llm_mix": ("LLM каждый час", "LLM + LightGBM — смесь",
                "Шансы Gemini этого часа и утренний прогноз LightGBM v3 поровну; ставит по тем же правилам, что LLM. Своих запросов к LLM нет — $0"),
    "obs": ("Живые замеры", "По живым замерам станции", "Ставка против варианта, который станция уже исключила"),
    "obs_fast": ("Живые замеры", "Быстрые замеры в минуту сводки",
                 "Как «по живым замерам», но узнаёт значение сводки METAR раньше её публикации: 5-минутные замеры аэропортов США (Synoptic), Токио (JMA), Амстердам (KNMI), Мюнхен (DWD), Хельсинки (FMI); плюс Гонконг по «максимуму с полуночи» обсерватории и Тайбэй по сводкам"),
    "obs_rt": ("Живые замеры", "Живые замеры — быстро",
               "Как «по живым замерам», но постоянный процесс: новая сводка METAR проверяется раз в секунду в минуты выхода сводки, и «нет» на уже невозможный вариант покупается сразу (как HighTempTation)"),
    "obs_wethr": ("Живые замеры", "Живые замеры — wethr (США)",
                  "Как «живые замеры — быстро», но сводки METAR 5 городов США (Чикаго, Атланта, Остин, Денвер, Майами) приходят платным потоком wethr.net (~на минуту раньше NOAA)"),
    "obs_fmi": ("Живые замеры", "Хельсинки: 10-минутные замеры FMI",
                "То же, что «по живым замерам», но по 10-минутным данным финской метеослужбы — раньше METAR"),
}
# кошельки по живым замерам (paper_obs_trades, колонка wallet) — weather_obs_live.py
OBS_WALLETS = ("obs", "obs_fmi", "obs_fast", "obs_rt", "obs_wethr")
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
    "wuhan": "Ухань", "zhengzhou": "Чжэнчжоу", "hong_kong": "Гонконг", "taipei": "Тайбэй",
}
WALLET_BADGE = {"ml3": "v3", "ml2": "v2", "ml": "v1", "ml_shift": "v1+", "mm": "MX", "emos": "EM", "main": "GI",
                "mm_mk": "MX", "emos_mk": "EM", "main_mk": "GI", "ml3_mk": "v3", "ml3_cal": "v3+", "ml3_z": "v3Z", "ml3_no": "v3−", "ml3_cal_k": "v3$", "ml4": "v4", "ml4_cal": "v4+", "ml4e": "v4³", "ml4e_cal": "v4³+", "ens": "EN", "ml3_cal15": "v3+¢", "ml3_cal30": "v3+30", "no_cheap": "НЕТ", "fav": "ФАВ", "no_mid": "НЕТ½", "no_big": "НЕТ+", "techno": "ДА¢", "ml3_conf": "v3+У", "ml5_cal": "v5+", "ml5": "v5", "ml_day": "ДН", "ml3_city": "v3+Г", "copy": "CP", "obs": "OB", "obs_fmi": "FI", "obs_fast": "OB+", "obs_rt": "OB⚡", "obs_wethr": "OBW", "llm_gem": "LG", "llm_ds": "LD"}
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
    series = [s for s in series if any(v is not None for v in s["values"])]  # пустой ряд не рисуем
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


SKILL_PARENT = {"main_mk": "main", "emos_mk": "emos", "mm_mk": "mm", "ml3_mk": "ml3", "ml3_cal_k": "ml3_cal", "ml3_cal15": "ml3_cal", "ml3_cal30": "ml3_cal", "ml3_z": "ml3_cal", "ml3_city": "ml3_cal"}  # «своя заявка» — сигнал родителя


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


LLM_WALLETS = ("llm_gem", "llm_ds")   # LLM со своими запросами — вкладки страницы
LLM_BET_WALLETS = (*LLM_WALLETS, "llm_mix", "llm_cal", "llm_agy", "llm_agy_bet")   # 10.10: llm_agy_bet — ставку решает сама LLM; 09.10: llm_agy — Gemini через Antigravity CLI, только число   # 08.10: llm_cal — Gemini 35% + рынок 65%, перевес от 10 п.п.   # 06.10: и смесь Gemini + LightGBM (weather_llm_hour.MIX_WALLET) — карточки, ставки, деньги
LLM_LIMIT = {"llm_gem": 13.5, "llm_ds": 4.0, "llm_mix": 0.0, "llm_cal": 0.0, "llm_agy": 0.0, "llm_agy_bet": 0.0}   # как weather_llm_hour.MONTH_LIMIT; у смеси своих запросов нет


def _smooth(pts):
    """Плавная линия через точки (Catmull-Rom → кривые Безье), без выбросов за точки по высоте."""
    if len(pts) < 2:
        return ""
    d = f"M{pts[0][0]},{pts[0][1]}"
    for i in range(len(pts) - 1):
        p0, p1, p2 = pts[max(i - 1, 0)], pts[i], pts[i + 1]
        p3 = pts[min(i + 2, len(pts) - 1)]
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        lo, hi = min(p1[1], p2[1]), max(p1[1], p2[1])
        c1 = (c1[0], min(max(c1[1], lo), hi))
        c2 = (c2[0], min(max(c2[1], lo), hi))
        d += f" C{c1[0]:.1f},{c1[1]:.1f} {c2[0]:.1f},{c2[1]:.1f} {p2[0]},{p2[1]}"
    return d


def llm_chart(labels, series, fmt, w=760, h=300, tip=None, wide=False):
    """04.10 (просьба Alex): график страницы /llm — плавные линии, точка на каждом значении с подсказкой, подпись на конце
    линии с последним значением. series: [{"name", "cls", "values"}]; tip(i) — заголовок подсказки для столбца i.
    wide (05.10, просьба Alex) — во всю ширину без поля справа, подпись каждого столбца; подпись у правого края — слева от точки."""
    series = [s_ for s_ in series if any(v is not None for v in s_["values"])]
    vals = [v for s_ in series for v in s_["values"] if v is not None]
    if len(labels) < 2 or not vals:
        return None
    lo, hi = min(vals), max(vals)
    step = _nice_step(hi - lo if hi > lo else 1)
    y0, y1 = step * ((lo - step * 0.3) // step), step * (-(-(hi + step * 0.3) // step))
    L, R, T, B = (34, 16, 18, 34) if wide else (56, 150, 16, 40)
    pw, ph = w - L - R, h - T - B
    X = lambda i: round(L + pw * i / (len(labels) - 1), 1)
    Y = lambda v: round(T + ph * (1 - (v - y0) / (y1 - y0)), 1)
    out = []
    for s_ in series:
        segs, cur, dots = [], [], []
        for i, v in enumerate(s_["values"]):
            if v is None:
                if cur:
                    segs.append(cur)
                cur = []
                continue
            cur.append((X(i), Y(v)))
            dots.append({"x": X(i), "y": Y(v), "tip": tip(i) if tip else f"{labels[i]} · {s_['name']}: {fmt(v)}"})
        if cur:
            segs.append(cur)
        last_i = max(i for i, v in enumerate(s_["values"]) if v is not None)
        out.append({"name": s_["name"], "cls": s_["cls"], "paths": [_smooth(sg) if len(sg) > 1 else "" for sg in segs], "dots": dots,
                    "end": (X(last_i), Y(s_["values"][last_i])), "last": fmt(s_["values"][last_i])})
    ends = sorted(out, key=lambda s_: s_["end"][1])
    for i, e in enumerate(ends):
        e["ly"] = e["end"][1] if i == 0 else max(e["end"][1], ends[i - 1]["ly"] + 18)
        e["lx"], e["anchor"] = e["end"][0] + 12, "start"
        if wide and e["end"][0] > w - R - 150:
            e["lx"], e["anchor"], e["ly"] = e["end"][0] - 10, "end", e["ly"] - 14
    ticks, t = [], y0
    while t <= y1 + 1e-9:
        ticks.append({"y": Y(t), "label": f"{t:.0f}°" if step >= 1 else f"{t:.1f}°"})
        t += step
    every = max(1, round(len(labels) / (24 if wide else 8)))
    xl = [{"x": X(i), "label": lab} for i, lab in enumerate(labels) if i % every == 0 or i == len(labels) - 1]
    return {"w": w, "h": h, "L": L, "R": w - R, "T": T, "B": T + ph, "series": out, "ticks": ticks, "xl": xl,
            "ax_x": 0 if wide else L - 8, "ax_a": "start" if wide else "end",
            # лента часов под графиком: столбец i — точно под точкой i (отступ слева в % ширины графика)
            "strip_left": round(100 * (L - pw / (len(labels) - 1) / 2) / w, 2)}


def _llm_center(lab):
    """Центр варианта по подписи из weather_llm_hour.label: «66-67°F» → 66.5, «69°F or below» → 69."""
    t = lab.replace("°F", "").replace("°C", "").replace(" or below", "").replace(" or higher", "")
    a, _, b = t.partition("-")
    return (float(a) + float(b)) / 2 if b else float(a)


_RE_LLM_LAB = re.compile(r"^(-?\d+)(?:-(-?\d+))?°[FC]( or below| or higher)?$")


def _llm_range(lab):
    """Границы варианта по подписи — как weather_edge.parse_bucket: «66-67°F» → (65.5, 67.5), «69°F or below» → (-999, 69.5)."""
    m = _RE_LLM_LAB.match(lab.strip())
    if not m:
        return None
    a, b, tail = float(m.group(1)), m.group(2), m.group(3)
    if tail == " or below":
        return (-999.0, a + 0.5)
    if tail == " or higher":
        return (a - 0.5, 999.0)
    return (a - 0.5, (float(b) if b else a) + 0.5)


LLM_V2_FROM = "2026-10-06T09:45:00+00:00"   # как weather_llm_hour.V2_FROM: с этого запуска — обучение v2 (LightGBM, ошибки, деньги, поправка шансов)
LLM_KEY_LIMIT = 19.95  # всего внесено на OpenRouter: 08.10 баланс $16.70 после пополнения на $10 + потрачено $3.25 (было $10)
LLM_CITIES = ("chicago", "atlanta", "austin", "miami", "dallas", "london")   # как weather_llm_hour.CITIES (Лондон с 04.10)
LLM_MODEL_NAME = {"llm_gem": "Gemini 3.8 Flash", "llm_ds": "DeepSeek V4 Pro", "llm_mix": "Смесь Gemini + LightGBM", "llm_cal": "Gemini + рынок", "llm_agy": "Gemini через Antigravity", "llm_agy_bet": "Gemini ставит сама"}


def _llm_fact_hourly(conn, city, day):
    """Температура по факту по часам местного дня (в единицах маркета): сводки METAR (metar_seen_src), замер :51-:53 относим к следующему часу."""
    from weather_cities import OBS_CITIES
    cfg = OBS_CITIES.get(city)
    if not cfg or not table_exists(conn, "metar_seen_src"):
        return {}
    tz = ZoneInfo(cfg["tz"])
    d0 = datetime.fromisoformat(day + "T00:00").replace(tzinfo=tz)
    a, b = (d0 - timedelta(hours=1)).astimezone(timezone.utc).isoformat(), (d0 + timedelta(hours=25)).astimezone(timezone.utc).isoformat()
    out = {}
    for t, tc in conn.execute("""SELECT obs_time_utc, MAX(temp_c) FROM metar_seen_src WHERE icao = ? AND obs_time_utc >= ? AND obs_time_utc < ?
                                 AND temp_c IS NOT NULL GROUP BY obs_time_utc""", (cfg["icao"], a, b)):
        lt = (datetime.fromisoformat(t) + timedelta(minutes=10)).astimezone(tz)
        if lt.date().isoformat() == day:
            out[lt.hour] = round(tc * 9 / 5 + 32, 1) if cfg["unit"] == "fahrenheit" else round(tc, 1)
    return out


def _city_tz(city):
    from weather_cities import OBS_CITIES
    return OBS_CITIES[city]["tz"]


def _llm_clock(city):
    """04.10 (просьба Alex): местное время города и работает ли LLM (запросы в :05 с 08 до 19 местного, weather_llm_hour.HOURS; до 06.10 — с 05)."""
    from weather_cities import OBS_CITIES
    now = datetime.now(ZoneInfo(OBS_CITIES[city]["tz"]))
    on = 8 <= now.hour < 20
    if on:
        nxt = now.replace(minute=5, second=0) + (timedelta(hours=1) if now.minute >= 5 else timedelta(0))
        state = f"работает, следующий прогноз в {nxt:%H:%M}" if nxt.hour < 20 else "последний прогноз дня был в 19:05"
    else:
        state = "спит, первый прогноз в 08:05"
    return {"time": f"{now:%H:%M}", "on": on, "state": state, "tz": now.strftime("%Z")}


def llm_page_data(conn, key, city, day):
    """04.10 (просьба Alex): LLM — отдельный раздел. Деньги и расход обеих LLM, график 24 ч и месяц (факт против прогноза),
    что LLM видела в последний час и что ответила, как учится — по дням."""
    have = table_exists(conn, "llm_hour_preds")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(llm_hour_preds)")} if have else set()
    month = datetime.now(timezone.utc).strftime("%Y-%m-01")
    cards, spend_all, spend_month = [], 0.0, 0.0
    for k in LLM_BET_WALLETS:
        rows = conn.execute("SELECT * FROM paper_trades WHERE wallet = ? AND status != 'skip'", (k,)).fetchall() if table_exists(conn, "paper_trades") else []
        c = _wallet_card(WALLET_INFO[k][1], [r for r in rows if r["status"] != "nofill"], nofill=0, skipped=0)
        sp = conn.execute("SELECT COUNT(*), COALESCE(SUM(cost), 0), COALESCE(SUM(CASE WHEN ts_utc >= ? THEN cost END), 0) FROM llm_hour_preds WHERE wallet = ?",
                          (month, k)).fetchone() if have else (0, 0.0, 0.0)
        c.update(key=k, model=LLM_MODEL_NAME[k], calls=sp[0], spent=sp[1], spent_month=sp[2], limit=LLM_LIMIT[k],
                 free=c["balance"] - c["in_play"], per_call=sp[1] / sp[0] if sp[0] else None)
        # 06.10: итог до и после обучения v2 — по времени ставки
        for part, sel in (("before", lambda r: (r["placed_at"] or "") < LLM_V2_FROM), ("after", lambda r: (r["placed_at"] or "") >= LLM_V2_FROM)):
            st = [r for r in rows if r["status"] in ("won", "lost", "void") and sel(r)]
            pnl, cost = sum(_pnl(r) for r in st), sum(r["stake"] + _fee(r) for r in st)
            c[part] = {"n": len(st), "won": sum(r["status"] == "won" for r in st), "pnl": pnl, "roi": 100 * pnl / cost if cost else None,
                       "open": sum(1 for r in rows if r["status"] == "open" and sel(r))}
        spend_all += sp[1]
        spend_month += sp[2]
        cards.append(c)
    out = {"v2_from": LLM_V2_FROM, "cards": cards, "spend": {"all": spend_all, "month": spend_month, "limit_month": sum(LLM_LIMIT.values()), "key_limit": LLM_KEY_LIMIT},
           "key": key, "city": city, "cities": [(c, CITY_RU.get(c, c), _llm_clock(c)) for c in LLM_CITIES], "model": LLM_MODEL_NAME[key]}
    if not have:
        return out
    days = [r[0] for r in conn.execute("SELECT DISTINCT local_date FROM llm_hour_preds WHERE wallet = ? AND city = ? ORDER BY local_date", (key, city))]
    if not days:
        return out
    day = day if day in days else days[-1]
    out["day"], out["days"] = day, days
    sel = ", ".join(c if c in cols else f"NULL AS {c}" for c in ("prompt", "answer_json", "hourly_json", "lesson", "reflection"))
    preds = conn.execute(f"""SELECT local_date, local_hour, pred_max, probs_json, market_json, max_so_far, reason, cost, ts_utc, {sel}
                             FROM llm_hour_preds WHERE wallet = ? AND city = ? ORDER BY local_date, local_hour""", (key, city)).fetchall()
    fact_max = {r[0]: r[1] for r in conn.execute("SELECT local_date, actual_max FROM weather_station_daily WHERE city = ?", (city,))}
    # 24 часа: факт по часам и прогноз LLM на этот час из последнего запроса до него
    fact = _llm_fact_hourly(conn, city, day)
    from weather_cities import OBS_CITIES
    u = "°F" if OBS_CITIES[city]["unit"] == "fahrenheit" else "°C"
    out["unit"] = u
    today = [p for p in preds if p["local_date"] == day]
    # 05.10 (решение Alex): за день — только две линии. Факт — все часы; LLM — в каждом часу её прогноз на этот час, сделанный
    # в :05 предыдущего часа (точка появляется заранее, факт догоняет). Только часы, когда LLM работала.
    by_hour = {p["local_hour"]: p for p in today}
    llm_h = {}
    for h in range(24):
        p = by_hour.get(h - 1)
        if p is not None and p["hourly_json"]:
            v = json.loads(p["hourly_json"]).get(f"{h:02d}")
            if v is not None:
                llm_h[h] = v
    fc = llm_h
    out["day_chart"] = llm_chart([f"{h:02d}" for h in range(24)], [
        {"name": "Факт", "cls": "s-real", "values": [fact.get(h) for h in range(24)]},
        {"name": "LLM", "cls": "s-model", "values": [llm_h.get(h) for h in range(24)]}], lambda v: f"{v:.1f}{u}",
        tip=lambda i: f"{i:02d}:00" + (f" · факт {fact[i]:.1f}{u}" if i in fact else " · факт ещё не известен") + (f" · LLM {llm_h[i]:.1f}{u}" if i in llm_h else "")
        + (f" · ошибка {llm_h[i] - fact[i]:+.1f}°" if i in fact and i in llm_h else ""), wide=True)
    out["day_rows"] = [{"h": h, "fact": fact.get(h), "llm": fc.get(h), "err": fc[h] - fact[h] if fc.get(h) is not None and h in fact else None,
                        "naive_err": fact[h - 1] - fact[h] if h in fact and (h - 1) in fact and fc.get(h) is not None else None}
                       for h in range(24)]
    out["day_max"] = {"fact": fact_max.get(day), "seen": max(fact.values()) if fact else None,
                      "pred": [(p["local_hour"], p["pred_max"]) for p in today]}
    # месяц: максимум дня по факту, прогноз LLM в первый час дня и рынок в тот же час
    mdays = days[-31:]
    first = {}
    for p in preds:
        first.setdefault(p["local_date"], p)
    def mkt_center(p):
        mk = json.loads(p["market_json"] or "{}")
        return _llm_center(max(mk, key=mk.get)) if mk else None
    out["month_chart"] = llm_chart([f"{d[8:10]}.{d[5:7]}" for d in mdays], [
        {"name": "Факт", "cls": "s-real", "values": [fact_max.get(d) for d in mdays]},
        {"name": "LLM утром", "cls": "s-model", "values": [first[d]["pred_max"] for d in mdays]},
        {"name": "Рынок утром", "cls": "s-market", "values": [mkt_center(first[d]) for d in mdays]}], lambda v: f"{v:.1f}{u}")
    # как учится: по дням — прогнозы по часам, факт, ошибка и что LLM поняла
    learn = []
    for d in reversed(days[-14:]):
        ps = [p for p in preds if p["local_date"] == d]
        fm = fact_max.get(d)
        errs = [p["pred_max"] - fm for p in ps if fm is not None and p["pred_max"] is not None]
        lessons = [p["lesson"] for p in ps if p["lesson"]]
        learn.append({"date": d, "fact": fm, "preds": [(p["local_hour"], p["pred_max"]) for p in ps],
                      "err": sum(errs) / len(errs) if errs else None, "abs": sum(abs(e) for e in errs) / len(errs) if errs else None,
                      "lesson": lessons[-1] if lessons else None, "lesson_first": lessons[0] if lessons else None})
    out["learn"] = learn
    # 04.10 (схема Alex «учится по каждому часу»): разбор каждого часа — прогноз на час вперёд против факта и что поняла
    review, by_day_err, by_day_naive, fact_cache = [], {}, {}, {day: fact}
    for p in preds:
        if not p["hourly_json"]:
            continue
        tgt = p["local_hour"] + 1
        f = json.loads(p["hourly_json"]).get(f"{tgt:02d}")
        if p["local_date"] not in fact_cache:
            fact_cache[p["local_date"]] = _llm_fact_hourly(conn, city, p["local_date"])
        a = fact_cache[p["local_date"]].get(tgt)
        prev = fact_cache[p["local_date"]].get(p["local_hour"])
        if f is not None and a is not None and prev is not None:  # только часы, где есть с чем сравнить обе
            by_day_err.setdefault(p["local_date"], []).append(abs(f - a))
            by_day_naive.setdefault(p["local_date"], []).append(abs(prev - a))
        if p["local_date"] == day:
            review.append({"h": p["local_hour"], "tgt": tgt, "f": f, "a": a, "refl": p["reflection"]})
    out["review"] = review
    ed = sorted(by_day_err)
    avg = lambda xs: sum(xs) / len(xs)
    out["hour_err_chart"] = llm_chart([f"{d[8:10]}.{d[5:7]}" for d in ed], [
        {"name": "LLM", "cls": "s-model", "values": [avg(by_day_err[d]) for d in ed]},
        {"name": "Повтор замера", "cls": "s-market", "values": [avg(by_day_naive[d]) for d in ed]}],
        lambda v: f"{v:.1f}°", h=240, wide=True,
        tip=lambda i: f"{ed[i][8:10]}.{ed[i][5:7]} · сравнено часов: {len(by_day_err[ed[i]])} · LLM {avg(by_day_err[ed[i]]):.1f}°"
        f" · повтор замера {avg(by_day_naive[ed[i]]):.1f}°") if len(ed) >= 2 else None
    out["hour_err_now"] = (avg(by_day_err[ed[-1]]), len(by_day_err[ed[-1]]), avg(by_day_naive[ed[-1]])) if ed else None
    # 05.10 (вопрос Alex «LLM лучше?»): итог за все дни — сколько часов, средние ошибки, в скольких часах LLM точнее
    le, ne = [x for d in ed for x in by_day_err[d]], [x for d in ed for x in by_day_naive[d]]
    out["hour_err_sum"] = {"n": len(le), "llm": avg(le), "naive": avg(ne), "wins": sum(a < b for a, b in zip(le, ne)),
                           "ties": sum(a == b for a, b in zip(le, ne)), "days": [(f"{d[8:10]}.{d[5:7]}", len(by_day_err[d])) for d in ed]} if le else None
    # тетрадь: текущие правила и как менялась
    if table_exists(conn, "llm_notebook"):
        nbs = conn.execute("SELECT local_date, local_hour, notes_json FROM llm_notebook WHERE wallet = ? AND city = ? ORDER BY ts_utc",
                           (key, city)).fetchall()
        out["notebook"] = json.loads(nbs[-1]["notes_json"]) if nbs else []
        out["notebook_when"] = f"{nbs[-1]['local_date'][8:10]}.{nbs[-1]['local_date'][5:7]} {nbs[-1]['local_hour']:02d}:05" if nbs else None
        seen, hist = set(), []
        for r in nbs:
            for x in json.loads(r["notes_json"]):
                if x not in seen:
                    seen.add(x)
                    hist.append({"when": f"{r['local_date'][8:10]}.{r['local_date'][5:7]} {r['local_hour']:02d}:05", "rule": x,
                                 "kept": x in (out["notebook"] or [])})
        out["notebook_hist"] = list(reversed(hist))[:30]
    # что LLM видела в последний час и что ответила
    last = preds[-1]
    ans = json.loads(last["answer_json"]) if last["answer_json"] else {}
    probs = json.loads(last["probs_json"] or "{}") or {}
    mk = json.loads(last["market_json"] or "{}")
    out["last"] = {"when": f"{last['local_date'][8:10]}.{last['local_date'][5:7]} {last['local_hour']:02d}:05 местного", "prompt": last["prompt"], "reflection": last["reflection"],
                   "pred": last["pred_max"], "seen": last["max_so_far"], "reason": last["reason"], "lesson": last["lesson"], "cost": last["cost"],
                   "hourly": sorted((json.loads(last["hourly_json"]) if last["hourly_json"] else {}).items()),
                   "probs": [(lab, p, mk.get(lab)) for lab, p in probs.items() if p >= 0.01 or (mk.get(lab) or 0) >= 0.01]}
    bets = conn.execute("SELECT * FROM paper_trades WHERE wallet = ? AND status NOT IN ('skip', 'nofill') ORDER BY placed_at DESC LIMIT 40", (key,)).fetchall()
    # 04.10 (просьба Alex «ставит LLM или нет»): открытые ставки обеих LLM и ставка по выбранному городу сегодня
    ob = conn.execute(f"SELECT * FROM paper_trades WHERE wallet IN ({','.join('?' * len(LLM_BET_WALLETS))}) AND status = 'open' ORDER BY placed_at DESC",
                      LLM_BET_WALLETS).fetchall()
    out["open_bets"] = [{"model": LLM_MODEL_NAME[b["wallet"]], "date": f"{b['local_date'][8:10]}.{b['local_date'][5:7]}", "city": CITY_RU.get(b["city"], b["city"]),
                         "side": "да" if (b["side"] or "yes") == "yes" else "нет", "bucket": paper_bucket(b["bucket_lo"], b["bucket_hi"], b["unit"]),
                         "price": b["price"], "chance": b["model_p"], "cost": b["stake"] + (b["fee"] or 0), "win": b["shares"],
                         "when": (datetime.fromisoformat(b["placed_at"]).astimezone(ZoneInfo(_city_tz(b["city"])))).strftime("%H:%M") if b["placed_at"] else ""} for b in ob]
    tb = conn.execute("SELECT * FROM paper_trades WHERE wallet = ? AND city = ? AND local_date = ? AND status NOT IN ('skip', 'nofill')", (key, city, day)).fetchone()
    edge = None
    if last["local_date"] == day and last["probs_json"]:
        pr, mk = json.loads(last["probs_json"]) or {}, json.loads(last["market_json"] or "{}")
        cand = [(pr[l] - mk[l], "да", l) for l in pr if l in mk and 0.10 <= mk[l] <= 0.90] + \
               [((1 - pr[l]) - (1 - mk[l]), "нет", l) for l in pr if l in mk and 0.10 <= 1 - mk[l] <= 0.90]
        edge = max(cand) if cand else None
    out["city_bet"] = {"bet": {"side": "да" if (tb["side"] or "yes") == "yes" else "нет", "bucket": paper_bucket(tb["bucket_lo"], tb["bucket_hi"], tb["unit"]),
                               "price": tb["price"], "chance": tb["model_p"], "status": tb["status"], "cost": tb["stake"] + (tb["fee"] or 0), "win": tb["shares"],
                               "pnl": (tb["payout"] or 0) - tb["stake"] - (tb["fee"] or 0) if tb["status"] in ("won", "lost", "void") else None} if tb else None,
                       "edge": edge}
    out["bets"] = [{"date": f"{b['local_date'][8:10]}.{b['local_date'][5:7]}", "city": CITY_RU.get(b["city"], b["city"]), "side": "да" if (b["side"] or "yes") == "yes" else "нет",
                    "bucket": paper_bucket(b["bucket_lo"], b["bucket_hi"], b["unit"]),
                    "price": b["price"], "chance": b["model_p"], "cost": b["stake"] + (b["fee"] or 0), "status": b["status"],
                    "pnl": (b["payout"] or 0) - b["stake"] - (b["fee"] or 0) if b["status"] in ("won", "lost", "void") else None, "why": b["reason"],
                    # 06.10 (просьба Alex «подставлять дату и время»): когда купили и когда пришёл итог (или когда ждать) — по Кишинёву
                    "placed": _kiev_time(b["placed_at"]), "settled": _kiev_time(b["settled_at"]),
                    "closes": expected_close(b["city"], b["local_date"]) if b["status"] == "open" else None} for b in bets]
    return out


LLM_VS_HOURS = (8, 10, 12, 14, 16, 18)


def _bucket_score(bk, a):
    """bk — [(lo, hi, шанс)], a — факт. → (шанс на верный вариант, угадан ли самый вероятный, ошибка середины распределения)."""
    s = sum(p for *_, p in bk)
    if not s:
        return None
    bk = sorted((lo, hi, p / s) for lo, hi, p in bk)
    top, cum, mid = max(bk, key=lambda x: x[2]), 0.0, None
    for lo, hi, p in bk:
        cum += p
        if cum >= 0.5:
            mid = hi - 1 if lo < -900 else (lo + 1 if hi > 900 else (lo + hi) / 2)
            break
    return sum(p for lo, hi, p in bk if lo <= a < hi), top[0] <= a < top[1], abs(mid - a)


def llm_vs_ml(conn):
    """06.10 (вопрос Alex «LightGBM точнее LLM?»): обе LLM против главной модели v3 (LightGBM) и рынка на одних и тех же городах и днях.
    v3 решает один раз в день — в 08:00 местного (snapshots_fast), LLM — каждый час, поэтому честное сравнение — 08:00 против 08:05.
    По часам дня: LLM видит всё больше замеров, сравниваем её с рынком в тот же час (snapshots, ±1 ч) и с утренней v3.
    Деньги — закрытые ставки ml3 и LLM в тех же городах с первого дня LLM."""
    if not all(table_exists(conn, t) for t in ("llm_hour_preds", "snapshots_fast", "weather_station_daily")):
        return None
    start = conn.execute("SELECT MIN(local_date) FROM llm_hour_preds").fetchone()[0]
    if not start:
        return None
    qc = ",".join("?" * len(LLM_CITIES))
    act = {(c, d): a for c, d, a in conn.execute(f"SELECT city, local_date, actual_max FROM weather_station_daily WHERE local_date >= ? AND city IN ({qc})",
                                                 (start, *LLM_CITIES)) if a is not None}
    ml, first_ts = {}, {}
    for c, d, ts, lo, hi, p, m in conn.execute(f"""SELECT city, local_date, ts_utc, bucket_lo, bucket_hi, ml3_model_p, market_p FROM snapshots_fast
                                                   WHERE local_date >= ? AND city IN ({qc}) ORDER BY ts_utc""", (start, *LLM_CITIES)):
        if first_ts.setdefault((c, d), ts) == ts:   # первый быстрый снимок дня — по нему v3 и ставит
            ml.setdefault((c, d), []).append((lo, hi, p, m))
    mkt = {}
    if table_exists(conn, "snapshots"):
        for c, d, h, lo, hi, m in conn.execute(f"""SELECT city, local_date, local_hour, bucket_lo, bucket_hi, market_p FROM snapshots
                                                   WHERE local_date >= ? AND city IN ({qc}) AND market_p IS NOT NULL ORDER BY ts_utc""", (start, *LLM_CITIES)):
            mkt.setdefault((c, d, h), {})[(lo, hi)] = m   # последний снимок часа
    llm = {}
    for w, c, d, h, pj in conn.execute(f"SELECT wallet, city, local_date, local_hour, probs_json FROM llm_hour_preds WHERE probs_json IS NOT NULL AND city IN ({qc})",
                                       LLM_CITIES):
        bk = [(*r, p) for lab, p in json.loads(pj).items() if (r := _llm_range(lab))]
        if bk:
            llm[(w, c, d, h)] = bk
    avg = lambda xs: sum(xs) / len(xs) if xs else None

    def pack(scores):
        return {"chance": avg([s[0] for s in scores]), "hits": sum(s[1] for s in scores), "err": avg([s[2] for s in scores])} if scores else None

    # 1) утро: одни и те же город-дни у всех четырёх
    keys = sorted(k for k in act if k in ml and all((w, *k, 8) in llm for w in LLM_WALLETS)
                  and any(p is not None for _l, _h, p, _m in ml[k]))
    morning = []
    if keys:
        rows = {"v3": [], "mkt": [], **{w: [] for w in LLM_WALLETS}}
        for k in keys:
            rows["v3"].append(_bucket_score([(lo, hi, p) for lo, hi, p, _m in ml[k] if p is not None], act[k]))
            rows["mkt"].append(_bucket_score([(lo, hi, m) for lo, hi, _p, m in ml[k] if m is not None], act[k]))
            for w in LLM_WALLETS:
                rows[w].append(_bucket_score(llm[(w, *k, 8)], act[k]))
        names = [("v3", "LightGBM v3 (главная)", "08:00"), *[(w, LLM_MODEL_NAME[w], "08:05") for w in LLM_WALLETS], ("mkt", "Рынок", "08:00")]
        morning = [{"key": k, "name": n, "when": t, **pack([s for s in rows[k] if s])} for k, n, t in names]
        best = max(morning, key=lambda r: r["chance"] if r["chance"] is not None else -1)
        for r in morning:
            r["best"] = r is best
    # 2) по часам: LLM в :05 против рынка в этот час (снимок часом раньше или в тот же час) и утренней v3
    hours = []
    for H in LLM_VS_HOURS:
        sc = {"v3": [], "mkt": [], **{w: [] for w in LLM_WALLETS}}
        for k in act:
            if k not in ml or not all((w, *k, H) in llm for w in LLM_WALLETS):
                continue
            mk = mkt.get((*k, H)) or mkt.get((*k, H - 1))
            if not mk:
                continue
            sc["mkt"].append(_bucket_score([(lo, hi, m) for (lo, hi), m in mk.items()], act[k]))
            sc["v3"].append(_bucket_score([(lo, hi, p) for lo, hi, p, _m in ml[k] if p is not None], act[k]))
            for w in LLM_WALLETS:
                sc[w].append(_bucket_score(llm[(w, *k, H)], act[k]))
        if sc["mkt"]:
            hours.append({"h": H, "n": len(sc["mkt"]), **{k: pack([s for s in v if s]) for k, v in sc.items()}})
    # 3) деньги: закрытые ставки в тех же городах с первого дня LLM
    money_rows = []
    if table_exists(conn, "paper_trades"):
        for w, n in (("ml3", "LightGBM v3 (главная)"), *[(w, LLM_MODEL_NAME[w]) for w in LLM_BET_WALLETS]):
            rs = conn.execute(f"""SELECT * FROM paper_trades WHERE wallet = ? AND local_date >= ? AND city IN ({qc})
                                  AND status IN ('won', 'lost', 'void')""", (w, start, *LLM_CITIES)).fetchall()
            pnl, cost = sum(_pnl(r) for r in rs), sum(r["stake"] + _fee(r) for r in rs)
            money_rows.append({"name": n, "n": len(rs), "won": sum(r["status"] == "won" for r in rs), "pnl": pnl,
                               "roi": 100 * pnl / cost if cost else None})
    return {"start": start, "n_days": len(keys), "morning": morning, "hours": hours, "money": money_rows,
            "dates": sorted({d for _c, d in keys})}


@app.get("/llm", response_class=HTMLResponse)
def llm_page(request: Request, w: str = "llm_gem", city: str = "chicago", d: str = ""):
    conn = db()
    w = w if w in LLM_WALLETS else LLM_WALLETS[0]
    city = city if city in LLM_CITIES else LLM_CITIES[0]
    data = llm_page_data(conn, w, city, d)
    data["vs"] = llm_vs_ml(conn)
    data["progress"] = llm_progress(conn)
    conn.close()
    return TEMPLATES.TemplateResponse("llm.html", {"request": request, **data})


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
        src = {"obs_fmi": "10-мин замер FMI", "obs_fast": "быстрый замер в минуту сводки", "obs_rt": "свежая сводка METAR", "obs_wethr": "свежая сводка METAR (wethr)"}.get(okey, "станция")
        card = _wallet_card(label, obs, nofill=sum(r["status"] == "nofill" for r in obs))
        card["key"] = okey
        wallets.append(card)
        for r in obs:
            unit = "°F" if r["unit"] == "fahrenheit" else "°C"
            bucket = paper_bucket(r["bucket_lo"], r["bucket_hi"], r["unit"])
            if r["status"] == "nofill":
                skips.append({"local_date": r["local_date"], "city": r["city"], "wallet": label, "wkey": okey,
                              "kind": "не смогли купить",
                              "reason": (f"{src} уже показал{'' if okey in ('obs_fmi', 'obs_fast', 'obs_rt', 'obs_wethr') else 'а'} {r['obs_max']:.0f}{unit}, значит {bucket} невозможно; "
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


PROGRESS_LINES = (("ml3", "v3 — главная", "#FFD58A"), ("ml5", "v5 — «от рынка»", "#A3E39A"), ("blend", "смесь v3 35/65 с рынком", "#D3DDFB"),
                  ("day", "дневная модель, смесь (на неё ставит ml_day)", "#F0A6E0"))
PROGRESS_STEP = 0.01   # сдвиг среднего «отрыва» за 5 ночей меньше 0.01 — «без изменений» (одна ночь гуляет на ±0.015)


def training_progress(runs, day_runs=None):
    """2026-10-08 (просьба Alex: «видно ли, что модель учится»): отрыв от рынка по ночам — логошибка рынка минус
    логошибка модели на тех же экзаменационных днях (выше нуля — точнее рынка). Отрыв, а не сама логошибка:
    экзаменационные дни каждую ночь сдвигаются, и сама логошибка гуляет вместе с погодой, а рынок сдаёт те же дни.
    Итог по линии — среднее последних 5 ночей против 5 ночей до них."""
    pts = []
    for r in reversed(runs):
        e = r.get("exam") or {}
        if r.get("dry_run") or e.get("ll_market") is None or e.get("ll_model") is None:
            continue
        v = {"ml3": e["ll_market"] - e["ll_model"]}
        if e.get("ll_blend") is not None:
            v["blend"] = e["ll_market"] - e["ll_blend"]
        for k, x in ((r.get("exam_all") or {}).get("versions") or {}).items():
            if k != "ml3":
                v[k] = r["exam_all"]["ll_market"] - x["ll_model"]
        pts.append((r["when"][:5], v))
    # 09.10: дневная модель — свой экзамен (ml_day_exam), ставится на ночь с той же датой; дней без утреннего отчёта не бывает
    for d in day_runs or []:
        e = d.get("exam") or {}
        p = next((v for w, v in pts if w == d["when"][:5]), None)
        if p is not None and e.get("ll_blend") is not None:
            p["day"] = e["ll_market"] - e["ll_blend"]
    return progress_chart(pts, PROGRESS_LINES)


def _plural(n, words):
    """1 ночь / 2 ночи / 5 ночей."""
    return words[0] if n % 10 == 1 and n % 100 != 11 else words[1] if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else words[2]


def progress_chart(pts, line_defs, step=PROGRESS_STEP, words=("ночь", "ночи", "ночей"), vline=None, vline_label=None):
    """График «отрыв от рынка» (выше нуля — точнее рынка): pts — [(«дд.мм», {ключ линии: отрыв})], line_defs — [(ключ, имя, цвет)].
    Стрелка по линии — среднее последних n точек против n до них; сдвиг меньше step — «без изменений».
    vline — «дд.мм», с которой рисуется вертикальная отметка (например, улучшение LLM v2)."""
    if len(pts) < 2:
        return None
    vals = [x for _, v in pts for k, _, _ in line_defs for x in [v.get(k)] if x is not None] + [0.0]
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.12 or 0.01
    lo, hi = lo - pad, hi + pad
    W, H, L, R, T, B = 760, 240, 52, 16, 14, 30
    X = lambda i: L + (W - L - R) * i / (len(pts) - 1)
    Y = lambda x: T + (H - T - B) * (hi - x) / (hi - lo)
    lines = []
    for k, name, color in line_defs:
        ser = [(i, v[k]) for i, (_, v) in enumerate(pts) if v.get(k) is not None]
        if not ser:
            continue
        n = min(5, len(ser) // 2)
        if n >= 3:
            last = sum(x for _, x in ser[-n:]) / n
            prev = sum(x for _, x in ser[-2 * n:-n]) / n
            d = last - prev
            trend = ("up", f"растёт: +{d:.3f}") if d >= step else ("down", f"падает: −{-d:.3f}") if d <= -step else ("flat", "без изменений")
        else:
            n, last, trend = 0, ser[-1][1], ("flat", f"рано судить: {len(ser)} {_plural(len(ser), words)} из 6")
        lines.append({"name": name, "color": color, "now": ser[-1][1], "last": last, "n": n, "trend": trend, "nw": _plural(n, words),
                      "path": " ".join(f"{'M' if j == 0 else 'L'}{X(i):.1f},{Y(x):.1f}" for j, (i, x) in enumerate(ser)),
                      "dots": [{"x": round(X(i), 1), "y": round(Y(x), 1), "t": f"{pts[i][0]}: {name} {x:+.3f}"} for i, x in ser]})
    step = max(1, len(pts) // 8)
    xt = [{"x": round(X(i), 1), "t": d} for i, (d, _) in enumerate(pts) if i % step == 0 or i == len(pts) - 1]
    yt = []
    span = hi - lo
    unit = next(u for u in (0.005, 0.01, 0.02, 0.05, 0.1, 0.2) if span / u <= 6)
    y = math.ceil(lo / unit) * unit
    while y <= hi:
        yt.append({"y": round(Y(y), 1), "t": f"{y:+.2f}" if abs(y) > 1e-9 else "рынок"})
        y += unit
    vi = next((i for i, (d, _) in enumerate(pts) if d == vline), None) if vline else None
    return {"lines": lines, "xt": xt, "yt": yt, "zero": round(Y(0), 1), "W": W, "H": H, "L": L, "R": R, "T": T, "B": B, "n": len(pts),
            "first": pts[0][0], "lastd": pts[-1][0], "vx": round(X(vi), 1) if vi else None, "vlabel": vline_label}


# 09.10 (просьба Alex: «учится ли LLM»): тот же график для LLM — по дням, на её поправленных шансах (на них ставки),
# часы 08-19 (с 06.10 других нет), все города; день — когда известен настоящий максимум.
LLM_PROGRESS_LINES = (("llm_gem", "Gemini", "#FFD58A"), ("llm_ds", "DeepSeek", "#D3DDFB"),
                      ("llm_mix", "Gemini + LightGBM", "#A3E39A"), ("llm_cal", "Gemini + рынок", "#F0A6E0"),
                      ("llm_agy", "Gemini через Antigravity", "#F4A77F"))
LLM_PROGRESS_STEP = 0.03   # один день LLM гуляет сильнее экзамена моделей (~±0.1)
LLM_P_FLOOR = 1e-3         # шанс 0 правильному ответу считаем 0.1% — иначе один час бесконечно портит день


def llm_progress(conn):
    if not table_exists(conn, "llm_hour_preds"):
        return None
    act = {(c, d): a for c, d, a in conn.execute("SELECT city, local_date, actual_max FROM weather_station_daily WHERE local_date >= '2026-10-01'")}
    acc, wait = {}, set()
    for w, city, d, pj, mj in conn.execute("""SELECT wallet, city, local_date, probs_json, market_json FROM llm_hour_preds
            WHERE local_hour BETWEEN 8 AND 19 AND probs_json IS NOT NULL AND market_json IS NOT NULL"""):
        a = act.get((city, d))
        if a is None:   # день — только целиком: пока хоть у одного города нет итога, день не показываем
            wait.add(d)
            continue
        pr, mk = json.loads(pj or "{}"), json.loads(mj or "{}")
        tot = sum(mk.values()) or 1.0
        lab = next((lab for lab in mk if (rg := _llm_range(lab)) and rg[0] <= a < rg[1]), None)
        if lab is None or lab not in pr:
            continue
        x = acc.setdefault(d, {}).setdefault(w, [0, 0.0])
        x[0] += 1
        x[1] += math.log(max(pr[lab], LLM_P_FLOOR)) - math.log(max(mk[lab] / tot, LLM_P_FLOOR))
    pts = [(f"{d[8:10]}.{d[5:7]}", {w: s_ / n for w, (n, s_) in acc[d].items() if n >= 10}) for d in sorted(acc) if d not in wait]
    return progress_chart(pts, LLM_PROGRESS_LINES, step=LLM_PROGRESS_STEP, words=("день", "дня", "дней"),
                          vline=f"{LLM_V2_FROM[8:10]}.{LLM_V2_FROM[5:7]}", vline_label="v2")


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
    day_runs = _day_exam_runs(conn, "ml_day_exam")
    conn.close()
    ea = next((r.get("exam_all") for r in runs if r.get("exam_all")), None)
    if ea:
        for v in ea["versions"].values():
            v["gap"] = ea["ll_market"] - v["ll_model"]
            v["gap_b"] = ea["ll_market"] - v["ll_blend"]
            v["verdict"] = _gap_verdict({"ll_model": v["ll_model"], "ll_market": ea["ll_market"]}, "model")
            v["verdict_b"] = _gap_verdict({"ll_blend": v["ll_blend"], "ll_market": ea["ll_market"]}, "blend")
    return TEMPLATES.TemplateResponse("training.html", {"request": request, "last": last, "runs": runs, "alerts": alerts, "exam_all": ea,
                                                         "progress": training_progress(runs, day_runs)})


# ---- /models: всё о каждой модели (2026-09-29, просьба Alex: «хочу видеть ВСЁ! От и до!») ----
# Модель ≠ кошелёк: список моделей, их кошельки и описания — models_info.py. Мерило «опережает / отстаёт» —
# логошибка по дням (ml_skill: шанс, который модель и рынок дали выигравшему варианту), как на /training.
MODEL_EVEN_LL = EXAM_EVEN_LL
MODEL_FEW_DAYS = 300  # меньше город-дней — «мало дней, может быть случайность»


def _ll(p):
    return -math.log(max(p or 0.0, 0.001))


def _gap_word(gap):
    """gap = логошибка рынка − модели (плюс — модель лучше)."""
    if gap is None:
        return {"tone": "none", "label": "нет данных"}
    if abs(gap) < MODEL_EVEN_LL:
        return {"tone": "even", "label": "наравне с рынком"}
    return {"tone": "ahead", "label": f"опережает на {gap:.3f}"} if gap > 0 else {"tone": "behind", "label": f"отстаёт на {-gap:.3f}"}


def _skill_rows(conn, key):
    if not key or not table_exists(conn, "ml_skill"):
        return []
    return conn.execute("SELECT city, date, source, p_model, p_market, hit_model, hit_market, bet_p_model, bet_p_market, bet_won "
                        "FROM ml_skill WHERE model = ? ORDER BY date", (key,)).fetchall()


def _track_sum(rows):
    """Итог по набору город-дней: логошибки, разница с рынком, в скольких днях ближе к правде."""
    if not rows:
        return None
    n = len(rows)
    llm, llk = sum(_ll(r["p_model"]) for r in rows) / n, sum(_ll(r["p_market"]) for r in rows) / n
    bets = [r for r in rows if r["bet_won"] is not None]
    # меньше ~300 город-дней разница с рынком ±0.03 — во многом случайность (разброс по неделям на истории)
    return {"n": n, "ll_model": llm, "ll_market": llk, "gap": llk - llm, "verdict": _gap_word(llk - llm), "few": n < MODEL_FEW_DAYS,
            "closer": 100 * sum(r["p_model"] > r["p_market"] for r in rows) / n,
            "hit_model": 100 * sum(r["hit_model"] for r in rows) / n, "hit_market": 100 * sum(r["hit_market"] for r in rows) / n,
            "pm": 100 * sum(r["p_model"] for r in rows) / n, "pk": 100 * sum(r["p_market"] for r in rows) / n,
            "bets": len(bets), "won": sum(r["bet_won"] for r in bets), "ek": sum(r["bet_p_market"] for r in bets),
            "em": sum(r["bet_p_model"] for r in bets), "first": rows[0]["date"], "last": rows[-1]["date"]}


def _weekly(rows):
    wk = {}
    for r in rows:
        d = date.fromisoformat(r["date"])
        wk.setdefault((d - timedelta(days=d.weekday())).isoformat(), []).append(r)
    return wk


def gap_bars(labels, series, w=760, h=260, vline=None):
    """Столбики «опережает / отстаёт» по неделям: вверх (зелёный) — модель лучше рынка, вниз — хуже.
    series: [{"name", "cls", "values"}] — до двух рядов рядом (модель и смесь)."""
    vals = [v for s_ in series for v in s_["values"] if v is not None]
    if not labels or not vals:
        return None
    m = max(max(abs(v) for v in vals), MODEL_EVEN_LL * 2)
    step = _nice_step(m, 2)
    top = step * (-(-m // step))
    L, R, T, B = 56, 12, 12, 30
    pw, ph = w - L - R, h - T - B
    Y = lambda v: T + ph * (1 - (v + top) / (2 * top))
    cw = pw / len(labels)
    k = len(series)
    bw = min(22, cw * 0.72 / k)
    bars, cols = [], []
    for i, lab in enumerate(labels):
        x0 = L + cw * i
        for j, s_ in enumerate(series):
            v = s_["values"][i]
            if v is None:
                continue
            x = x0 + cw / 2 - bw * k / 2 + bw * j
            y0, y1 = Y(max(v, 0)), Y(min(v, 0))
            bars.append({"x": round(x + 1, 1), "y": round(y0, 1), "w": round(bw - 2, 1), "h": round(max(y1 - y0, 1.5), 1),
                         "cls": s_["cls"] + (" up" if v >= MODEL_EVEN_LL else (" down" if v <= -MODEL_EVEN_LL else " even"))})
        cols.append({"x": round(x0 + cw / 2, 1), "x0": round(x0, 1), "cw": round(cw, 1), "label": lab,
                     "tip": " · ".join(f"{s_['name']}: {_gap_word(s_['values'][i])['label']}" for s_ in series if s_["values"][i] is not None)})
    ticks = [{"y": round(Y(t), 1), "label": f"{t:+.2f}" if t else "0"} for t in (top, top / 2, 0, -top / 2, -top)]
    return {"w": w, "h": h, "L": L, "R": w - R, "T": T, "B": T + ph, "zero": round(Y(0), 1), "bars": bars, "cols": cols,
            "ticks": ticks, "first": labels[0], "last": labels[-1],
            "even": (round(Y(MODEL_EVEN_LL), 1), round(Y(-MODEL_EVEN_LL), 1)),
            "vline": round(L + cw * vline, 1) if vline is not None else None}


def model_charts(conn, info):
    """Ряды модели и смеси из ml_skill → итоги (история / вживую), недельные графики, накопленное преимущество, города."""
    own, blend = _skill_rows(conn, info["skill"]), _skill_rows(conn, info["skill_blend"])
    base = own or blend
    if not base:
        return None
    parts = [("Модель", "s-model", own), ("Смесь с рынком", "s-real", blend)]
    parts = [p for p in parts if p[2]]
    out = {"parts": []}
    for name, cls, rows in parts:
        live = [r for r in rows if r["source"] == "live"]
        hist = [r for r in rows if r["source"] == "history"]
        out["parts"].append({"name": name, "cls": cls, "all": _track_sum(rows), "live": _track_sum(live), "hist": _track_sum(hist)})
    # недели: логошибка модели / смеси / рынка и разница с рынком
    weeks = {name: _weekly(rows) for name, _, rows in parts}
    keys = sorted({k for w_ in weeks.values() for k in w_})
    labels = [date.fromisoformat(k).strftime("%d.%m") for k in keys]
    live_i = next((i for i, k in enumerate(keys) if any(r["source"] == "live" for w_ in weeks.values() for r in w_.get(k, []))), None)
    if live_i == 0:
        live_i = None
    avg = lambda rs, f: sum(_ll(r[f]) for r in rs) / len(rs) if rs else None
    series = [{"name": name, "cls": cls, "values": [avg(weeks[name].get(k), "p_model") for k in keys]} for name, cls, _ in parts]
    mk = [avg(next((weeks[n].get(k) for n, _, _ in parts if weeks[n].get(k)), None), "p_market") for k in keys]
    series.append({"name": "Рынок", "cls": "s-market", "values": mk})
    out["ll"] = line_chart(labels, series, lambda v: f"{v:.2f}", w=760, h=300, vline=live_i)
    out["gap"] = gap_bars(labels, [{"name": s_["name"], "cls": s_["cls"],
                                    "values": [(mk[i] - v) if v is not None and mk[i] is not None else None for i, v in enumerate(s_["values"])]}
                                   for s_ in series[:-1]], vline=live_i)
    # накопленное преимущество по дням: сумма (логошибка рынка − модели) — растёт, когда модель ближе к правде
    days = sorted({r["date"] for _, _, rows in parts for r in rows})
    cum = []
    for name, cls, rows in parts:
        by = {}
        for r in rows:
            by[r["date"]] = by.get(r["date"], 0.0) + _ll(r["p_market"]) - _ll(r["p_model"])
        acc, vals = 0.0, []
        for d in days:
            acc += by.get(d, 0.0)
            vals.append(acc if d >= rows[0]["date"] else None)
        cum.append({"name": name, "cls": cls, "values": vals})
    d_live = next((i for i, d in enumerate(days) if any(r["source"] == "live" and r["date"] == d for _, _, rows in parts for r in rows)), None)
    out["cum"] = line_chart([f"{d[8:10]}.{d[5:7]}" for d in days], cum, lambda v: f"{v:+.0f}", w=760, h=300,
                            vline=d_live if d_live else None)
    # последние 14 дней вживую: сколько дней лучше / хуже рынка
    # города: где модель (или смесь, если своей нет) лучше и хуже рынка
    rows = base
    by_city = {}
    for r in rows:
        by_city.setdefault(r["city"], []).append(r)
    cities = []
    for c, rs in by_city.items():
        t = _track_sum(rs)
        cities.append({"city": c, "ru": CITY_RU.get(c, c), "n": t["n"], "gap": t["gap"], "closer": t["closer"], "v": t["verdict"]})
    cities.sort(key=lambda x: -x["gap"])
    top = max((abs(c["gap"]) for c in cities), default=1) or 1
    for c in cities:
        c["bar"] = round(50 * abs(c["gap"]) / top)
    out["cities"] = cities
    out["cities_of"] = parts[0][0] if own else "Смесь с рынком"
    return out


def _money_of(conn, wallets):
    """Деньги кошельков модели: карточки, общий итог и график итога всех её кошельков вместе."""
    if not table_exists(conn, "paper_trades") or not wallets:
        return None
    q = ",".join("?" * len(wallets))
    rows = conn.execute(f"SELECT * FROM paper_trades WHERE wallet IN ({q})", wallets).fetchall()
    cards, ev = [], []
    for k in wallets:
        wr = [r for r in rows if r["wallet"] == k]
        c = _wallet_card(PAPER_WALLETS.get(k, k), wr)
        c["key"], c["name"] = k, WALLET_INFO.get(k, (None, PAPER_WALLETS.get(k, k), ""))[1].replace("—", "-")
        c["desc"] = WALLET_INFO.get(k, (None, None, ""))[2]
        c["start"] = WALLET_START.get(k, PAPER_START_BALANCE)
        c["balance"] = c["start"] + c["pnl"]
        st = [r for r in wr if r["status"] in ("won", "lost", "void")]
        c["spark"] = spark([(r["local_date"], _pnl(r)) for r in st], c["start"])
        c["luck"] = luck(c["pnl"], c.get("sd"))
        c["winrate"] = round(100 * c["won"] / c["n"]) if c["n"] else None
        cards.append(c)
        ev += [(r["settled_at"] or r["local_date"], _pnl(r), r["city"], r["status"]) for r in st]
    cards.sort(key=lambda c: (c["key"] != wallets[0], -(c["roi"] if c["roi"] is not None else -999)))
    staked = sum(r["stake"] + _fee(r) for r in rows if r["status"] in ("won", "lost", "void"))
    pnl = sum(c["pnl"] for c in cards)
    chart = balance_chart(ev, 0.0, w=760, h=300)
    if chart:
        for t in chart["ticks"]:
            t["label"] = t["label"].replace("$-", "−$")
    return {"cards": cards, "pnl": pnl, "roi": 100 * pnl / staked if staked else None, "n": sum(c["n"] for c in cards),
            "won": sum(c["won"] for c in cards), "open": sum(c["open"] for c in cards), "in_play": sum(c["in_play"] for c in cards),
            "chart": chart, "luck": luck(pnl, math.sqrt(sum((c.get("sd") or 0) ** 2 for c in cards)))}


def _day_exam_runs(conn, table):
    """09.10: ночные экзамены модели со своей таблицей (дневная — ml_day_exam, пишет weather_ml_day.py) в виде строк _train_runs."""
    if not table_exists(conn, table):
        return []
    runs = []
    for r in conn.execute(f"SELECT trained_at, ok, details FROM {table} ORDER BY trained_at"):
        try:
            d = json.loads(r["details"])
        except ValueError:
            continue
        ex = d.get("exam")
        runs.append({"when": datetime.fromisoformat(r["trained_at"]).astimezone(VIEWER_TZ).strftime("%d.%m %H:%M"),
                     "ok": bool(r["ok"]), "dry": False, "dur": d.get("duration_s") or 0, "rows": d.get("rows"),
                     "features": d.get("features"), "note": None, "data": d.get("data", {}), "exam": ex,
                     "verdict": exam_verdict(ex) if ex else None, "importance": None, "exam_all": None, "detail": None})
    return runs


def _train_runs(conn, info):
    """Ночные обучения, в которых была эта версия: когда, сколько длилось, данные, экзамен (для v3)."""
    if info.get("exam_table"):
        return _day_exam_runs(conn, info["exam_table"])
    if not info.get("train_key") or not table_exists(conn, "ml_train_log"):
        return []
    runs = []
    for r in conn.execute("SELECT trained_at, ok, details FROM ml_train_log ORDER BY trained_at"):
        try:
            d = json.loads(r["details"])
        except ValueError:
            continue
        v = next((x for x in d.get("versions", []) if x.get("key") == info["train_key"]), None)
        if v is None:
            continue
        ex = d.get("exam") if info.get("exam") else None
        # 30.09: экзамен всех версий (exam_all) — у каждой версии свой, на тех же днях, что у v3
        va = ((d.get("exam_all") or {}).get("versions") or {}).get(info["train_key"])
        if va and d.get("exam"):
            ex = {**d["exam"], "ll_model": va["ll_model"], "ll_blend": va["ll_blend"], "err_model": va["err"],
                  "ll_market": d["exam_all"].get("ll_market", d["exam"].get("ll_market"))}
        runs.append({"when": datetime.fromisoformat(r["trained_at"]).astimezone(VIEWER_TZ).strftime("%d.%m %H:%M"),
                     "ok": bool(r["ok"]), "dry": d.get("dry_run"), "dur": d.get("duration_s") or 0, "rows": v.get("rows"),
                     "features": v.get("features"), "note": v.get("note"), "data": d.get("data", {}), "exam": ex,
                     "verdict": exam_verdict(ex) if ex else None, "importance": d.get("importance") if info.get("exam") else None,
                     "exam_all": d.get("exam_all"), "detail": (d.get("version_detail") or {}).get(info["train_key"])})
    return runs


def _exam_charts(runs):
    ex = [r for r in runs if r["exam"] and r["exam"].get("ll_model") is not None]
    out = {}
    if len(ex) >= 2:
        labels = [r["when"][:5] for r in ex]
        out["ll"] = line_chart(labels, [
            {"name": "Модель", "cls": "s-model", "values": [r["exam"]["ll_model"] for r in ex]},
            {"name": "Смесь", "cls": "s-real", "values": [r["exam"].get("ll_blend") for r in ex]},
            {"name": "Рынок", "cls": "s-market", "values": [r["exam"]["ll_market"] for r in ex]}], lambda v: f"{v:.3f}", w=760, h=280)
    er = [r for r in runs if r["exam"] and r["exam"].get("err_fc") is not None]
    if len(er) >= 2:
        out["err"] = line_chart([r["when"][:5] for r in er], [
            {"name": "Среднее 16 моделей", "cls": "s-fc", "values": [r["exam"]["err_fc"] for r in er]},
            {"name": "Модель", "cls": "s-model", "values": [r["exam"]["err_model"] for r in er]},
            {"name": "Рынок", "cls": "s-market", "values": [r["exam"].get("err_market") for r in er]}], lambda v: f"{v:.2f}°", w=760, h=280)
    if len(runs) >= 2:
        out["rows"] = line_chart([r["when"][:5] for r in runs], [
            {"name": "Город-дней", "cls": "s-model", "values": [r["rows"] for r in runs]}], lambda v: f"{v:,.0f}".replace(",", " "), w=760, h=220)
    return out


def _model_card(conn, key, info):
    ch = model_charts(conn, info)
    money = _money_of(conn, info["wallets"])
    runs = _train_runs(conn, info)
    head = None
    if ch:
        p = ch["parts"][-1] if info["skill_blend"] else ch["parts"][0]  # на что ставит большинство кошельков
        t = p["live"] or p["all"]
        head = {"who": p["name"], "src": "вживую" if p["live"] else "на истории", **t}
        # мини-график: накопленное преимущество по дням
        rows = _skill_rows(conn, info["skill_blend"] or info["skill"])
        by = {}
        for r in rows:
            by[r["date"]] = by.get(r["date"], 0.0) + _ll(r["p_market"]) - _ll(r["p_model"])
        head["spark"] = spark(sorted(by.items()), 0.0)
    info = {**info, "exam": info.get("exam") or any(r["exam"] for r in runs)}  # 30.09: экзамен всех версий
    last_train = runs[-1]["when"] if runs else None
    if info.get("meta"):   # 09.10: модель вне ночного отчёта (дневная) — время обучения из её файла
        try:
            meta = json.loads((DB_PATH.parent.parent / "ml" / info["meta"]).read_text())
            last_train = datetime.fromisoformat(meta["trained_at"]).astimezone(VIEWER_TZ).strftime("%d.%m %H:%M")
            info = {**info, "meta_rows": meta.get("rows"), "meta_features": meta.get("features")}
        except (OSError, ValueError, KeyError):
            pass
    return {"key": key, **info, "charts": ch, "money": money, "runs": runs, "head": head, "last_train": last_train}


@app.get("/models", response_class=HTMLResponse)
def models_page(request: Request):
    from models_info import MODELS as MODEL_INFO
    conn = db()
    cards = [_model_card(conn, k, v) for k, v in MODEL_INFO.items()]
    alerts = active_alerts(conn)
    conn.close()
    return TEMPLATES.TemplateResponse("models.html", {"request": request, "models": cards, "m": None, "alerts": alerts})


@app.get("/models/{key}", response_class=HTMLResponse)
def model_page(request: Request, key: str):
    from models_info import MODELS as MODEL_INFO
    if key not in MODEL_INFO:
        return HTMLResponse("Нет такой модели", status_code=404)
    conn = db()
    m = _model_card(conn, key, MODEL_INFO[key])
    m["exam_charts"] = _exam_charts(m["runs"])
    det = next((r for r in reversed(m["runs"]) if r.get("detail")), None)
    if det:  # 30.09: как училась именно эта версия (weather_ml_report.version_detail)
        m["learn"] = {**det["detail"], "when": det["when"], "night_dur": det["dur"]}
        sg = det["detail"].get("sigmas")
        if sg:
            m["learn"]["sig_top"] = [{"ru": CITY_RU.get(x["city"], x["city"]), "s": x["s"]} for x in sg["cities"][:5]]
            m["learn"]["sig_low"] = [{"ru": CITY_RU.get(x["city"], x["city"]), "s": x["s"]} for x in sg["cities"][-5:]]
    last_imp = (det["detail"]["importance"] if det and det["detail"].get("importance")
                else next((r["importance"] for r in reversed(m["runs"]) if r.get("importance")), None))
    if last_imp:
        top = max(i["pct"] for i in last_imp) or 1
        m["importance"] = [{"ru": feature_ru(i["name"]), "pct": i["pct"], "bar": round(100 * i["pct"] / top)} for i in last_imp]
    others = [{"key": k, "name": v["name"], "badge": v["badge"]} for k, v in MODEL_INFO.items()]
    alerts = active_alerts(conn)
    conn.close()
    return TEMPLATES.TemplateResponse("models.html", {"request": request, "models": None, "m": m, "others": others, "alerts": alerts})


# ---- /mm: виртуальный бот-мейкер по схеме Poligarch (2026-09-30, решение Alex) — weather_mm_paper.py / weather_mm_settle.py ----
MM_DB = DB_PATH.parent / "mm.sqlite3"
# 02.10 (решение Alex): сверху — по чему решаем (mm100) и тот же бот «в идеальном мире»; остальные — исследование
MM_WALLETS = {
    'mm100': ('Счёт $100 — строго как вживую', 'Банк ровно $100, заявки по 5 долей только на дешёвую сторону и только пока хватает свободных денег (заявка замораживает деньги, как на Polymarket). Исполнение — только гарантированное, отменённая заявка ещё 1 с может быть «подобрана», без возврата комиссии. С 02.10'),
    'mm100f': ('Сосредоточенный $100 — строго как вживую', 'Как «Счёт $100», но только в 5 городах с самой большой торговлей за прошлые 7 дней и без пары не больше 5 долей одной стороны — чтобы складывались пары. С 07.10, порог на 08-21.10 (страница «Реальные деньги»)'),
    'mm_ws_zone': ('Тот же бот в идеальном мире', 'Правила как у «Счёт $100», но без ограничения денег и с допущением «наша заявка всегда первая в очереди» — верхняя граница, вживую столько не будет. Разница со «Счёт $100» показывает, сколько прибыли держится на этом допущении'),
    'mm_ws_zs': ('Живой поток: дешёвая сторона, строго', 'Как «только дешёвая сторона», но исполнение засчитывается, только если оно гарантировано вживую: заявка стояла ≥ 1 с и сделка прошла хуже нашей цены (весь наш уровень съеден). Нижняя оценка — с 02.10'),
    'mm_ws_z30': ('Живой поток: только дешевле 30¢', 'Покупает только сторону дешевле 30¢: лучшая граница на первой половине истории (+17.9%), на проверке +18.4%, в худшем случае очереди +10.8%'),
    'mm_ws_sel': ('Живой поток: выгодные зоны', 'Живой поток, только в выгодных зонах цены и времени'),
    'mm_ws_all': ('Живой поток: все заявки', 'Те же заявки, но переставляются сразу при каждом изменении стакана (задержка ~0.03 с), исполнение — по живой сделке в ту же секунду'),
    'mm_all': ('Все заявки бота', 'Заявки на «да» и «нет» по всем погодным вариантам, кроме пауз (сводки METAR, вечер дня маркета)'),
    'mm_sel': ('Только выгодные зоны', 'Те же заявки, но только в зонах цены и времени, где стоять с заявкой было выгодно в обоих периодах истории'),
    'mm_pol': ('Политика и прочее', 'Те же заявки на не погодных маркетах, где Poligarch торговал за 7 дней; открытые маркеты — по текущей цене, пересчёт каждый день'),
    'mm_own': ('Свой выбор маркетов', 'Те же заявки на 40 не погодных маркетах, выбранных самим ботом: Polymarket платит за заявки, торговля за сутки от $5 000, до итога больше недели. Без Poligarch'),
}
MM_MAIN = ("mm100", "mm_ws_zone")
MM_OFF = {"mm_all", "mm_sel", "mm_pol", "mm_own"}   # 02.10: остановлены (старый бот с опросом, не погода)
MM_DECIDE = "2026-10-14"
# 01.10 (Alex: «при клике видеть все заявки», «когда получили обновление»): откуда брать заявки и исполнения каждого бота.
# Бот на живом потоке заявки держит в памяти (переставляет за доли секунды) — сохраняются только исполнения.
MM_QUOTES = {"mm_all": "city NOT IN ('pol', 'own')", "mm_sel": "city NOT IN ('pol', 'own') AND (sel_yes = 1 OR sel_no = 1)",
             "mm_pol": "city = 'pol'", "mm_own": "city = 'own'"}
MM_PAGE = 200


def _mm_dt(v):
    """Время из unix-секунд или ISO-строки → «01.10 15:25» в часовом поясе сайта."""
    if v is None:
        return None
    try:
        d = datetime.fromtimestamp(float(v), VIEWER_TZ) if isinstance(v, (int, float)) else datetime.fromisoformat(v).astimezone(VIEWER_TZ)
    except (ValueError, OSError):
        return None
    return d.strftime("%d.%m %H:%M")


def _mm_fills_table(w):
    return "mm_ws_fills" if w.startswith("mm_ws_") else "mm_fills"


def _mm_updates(conn, w):
    """Когда бот последний раз что-то получил: заявка (бот на опросе), исполнение, пересчёт итога."""
    out = []
    if w in MM_QUOTES and table_exists(conn, "mm_quotes"):
        v = conn.execute(f"SELECT MAX(ts_to) FROM mm_quotes WHERE {MM_QUOTES[w]}").fetchone()[0]
        out.append(("последняя заявка", _mm_dt(v) or "—"))
    ft = _mm_fills_table(w)
    if table_exists(conn, ft):
        v = conn.execute(f"SELECT MAX(ts) FROM {ft} WHERE wallet = ?", (w,)).fetchone()[0]
        out.append(("последнее исполнение", _mm_dt(v) or ("считается при расчёте итога" if w in ("mm_all", "mm_sel") else "—")))
    if table_exists(conn, "mm_results"):
        v = conn.execute("SELECT MAX(settled_at) FROM mm_results WHERE wallet = ?", (w,)).fetchone()[0]
        out.append(("итог пересчитан", _mm_dt(v) or "ещё не было"))
    return out


def _mm_card(conn, w):
    name, desc = MM_WALLETS[w]
    c = {"key": w, "name": name, "desc": desc, "n": 0}
    if table_exists(conn, "mm_results"):
        r = conn.execute("""SELECT COUNT(*) AS m, SUM(n_fills > 0) AS mf, SUM(n_fills) AS n, SUM(sh_yes + sh_no) AS sh, SUM(spent) AS spent,
                            SUM(merged) AS merged, SUM(merge_pnl) AS mp, SUM(inv_pnl) AS ip, SUM(rebate) AS rb, SUM(pnl) AS pnl
                            FROM mm_results WHERE wallet = ?""", (w,)).fetchone()
        c.update({k: (r[k] or 0) for k in r.keys()})
        c["roi"] = 100 * c["pnl"] / c["spent"] if c["spent"] else None
        c["cps"] = 100 * c["pnl"] / c["sh"] if c["sh"] else None
    c["upd"] = _mm_updates(conn, w)
    return c


def _mm_titles():
    """Вопросы не погодных маркетов (кэш списков бота) — чтобы на странице было понятно, что за маркет."""
    out = {}
    for f in ("mm_pol_markets.json", "mm_own_markets.json"):
        try:
            for m in json.loads((DB_PATH.parent / f).read_text()):
                out[m["cid"]] = m.get("q")
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return out


def _mm_zone_rows(conn, wallet, expr, order=None):
    rows = conn.execute(f"""SELECT {expr} AS k, COUNT(*) AS n, SUM(size) AS sh, SUM(price * size) AS spent, SUM(pnl_final) AS pnl
                            FROM mm_fills WHERE wallet = ? GROUP BY k""", (wallet,)).fetchall()
    out = [{"k": r["k"], "n": r["n"], "sh": r["sh"] or 0, "spent": r["spent"] or 0, "pnl": r["pnl"] or 0,
            "c": 100 * (r["pnl"] or 0) / r["sh"] if r["sh"] else 0} for r in rows]
    # 02.10: корзина цены 0 (дешевле 10¢) превращалась в "" и не сравнивалась с числами — страница падала
    return sorted(out, key=order or (lambda x: (x["k"] is None, x["k"] if x["k"] is not None else "")))


@app.get("/mm", response_class=HTMLResponse)
def mm_page(request: Request):
    ctx = {"request": request, "wallets": [], "live": None, "decide": MM_DECIDE, "chart": None, "recent": [], "zones": [], "prices": [], "cities": []}
    if MM_DB.exists():
        conn = sqlite3.connect(f"file:{MM_DB}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            now = int(time.time())
            q = conn.execute("""SELECT COUNT(*) AS n, SUM(yes_bid IS NOT NULL) + SUM(no_bid IS NOT NULL) AS sides, SUM(sel_yes) + SUM(sel_no) AS sel,
                                MAX(ts_to) AS last, COUNT(DISTINCT city) AS cities FROM mm_quotes WHERE ts_to >= ?""", (now - 90,)).fetchone()
            first = conn.execute("SELECT MIN(ts_from), MAX(ts_to), COUNT(DISTINCT condition_id) FROM mm_quotes").fetchone()
            st100 = conn.execute("SELECT ts, cash, reserved, quotes FROM mm100_state ORDER BY ts DESC LIMIT 1").fetchone() \
                if table_exists(conn, "mm100_state") else None
            ctx["live100"] = {"on": bool(st100 and now - st100["ts"] < 300), "quotes": st100["quotes"] if st100 else 0,
                              "reserved": st100["reserved"] if st100 else 0.0, "last": _mm_dt(st100["ts"]) if st100 else None}
            ctx["live"] = {"n": q["n"] or 0, "sides": q["sides"] or 0, "sel": q["sel"] or 0, "cities": q["cities"] or 0,
                           "since": datetime.fromtimestamp(first[0], VIEWER_TZ).strftime("%d.%m %H:%M") if first[0] else None,
                           "last": datetime.fromtimestamp(first[1], VIEWER_TZ).strftime("%d.%m %H:%M") if first[1] else None,
                           "markets": first[2] or 0, "on": bool(q["last"])}
            has_res = table_exists(conn, "mm_results")
            series, days_all = [], set()
            for w in MM_WALLETS:
                c = _mm_card(conn, w)
                if w == "mm100" and table_exists(conn, "mm100_state"):   # 02.10: счёт как в банке
                    st = conn.execute("SELECT ts, cash, reserved, quotes FROM mm100_state ORDER BY ts DESC LIMIT 1").fetchone()
                    if st:
                        settled = conn.execute("SELECT COALESCE(SUM(pnl), 0) FROM mm_results WHERE wallet = 'mm100'").fetchone()[0] if has_res else 0.0
                        acct = 100.0 + settled
                        c["bank"] = {"acct": acct, "free": st["cash"] - st["reserved"], "orders": st["reserved"], "quotes": st["quotes"],
                                     "pos": max(0.0, acct - st["cash"]), "when": _mm_dt(st["ts"])}
                if has_res:
                    by = dict(conn.execute("SELECT local_date, SUM(pnl) FROM mm_results WHERE wallet = ? GROUP BY local_date", (w,)).fetchall())
                    if w not in ("mm_pol", "mm_own"):  # у не погоды «день» — дата окончания маркета, на график по дням не кладём
                        days_all |= set(by)
                    c["by_day"] = by
                c["main"] = w in MM_MAIN
                c["off"] = w in MM_OFF
                ctx["wallets"].append(c)
            days = sorted(days_all)
            if len(days) >= 2:
                for c, cls in zip(ctx["wallets"], ("s-model", "s-real")):
                    acc, vals = 0.0, []
                    for d in days:
                        acc += c.get("by_day", {}).get(d, 0.0)
                        vals.append(acc)
                    series.append({"name": c["name"], "cls": cls, "values": vals})
                ctx["chart"] = line_chart([f"{d[8:10]}.{d[5:7]}" for d in days], series, lambda v: f"{'−' if v < 0 else '+'}${abs(v):.0f}", w=760, h=280)
            if has_res and table_exists(conn, "mm_fills"):
                order_z = ["накануне", "0-6", "6-9", "9-12", "12-15", "15-18", "18-24"]
                ctx["zones"] = _mm_zone_rows(conn, "mm_all", "zone", lambda x: order_z.index(x["k"]) if x["k"] in order_z else 99)
                ctx["prices"] = _mm_zone_rows(conn, "mm_all", "CAST(MIN(price * 10, 9) AS INTEGER)")
                cs = _mm_zone_rows(conn, "mm_all", "city", lambda x: -x["pnl"])
                for x in cs:
                    x["ru"] = CITY_RU.get(x["k"], x["k"])
                ctx["cities"] = cs
                ctx["recent"] = [dict(r) | {"city_ru": CITY_RU.get(r["city"], r["city"])} for r in conn.execute(
                    """SELECT * FROM mm_results WHERE wallet = 'mm100' AND n_fills > 0 ORDER BY local_date DESC, pnl DESC LIMIT 40""")]
                for r in ctx["recent"]:
                    from weather_cities import OBS_CITIES
                    r["what"] = paper_bucket(r["bucket_lo"], r["bucket_hi"], OBS_CITIES.get(r["city"], {}).get("unit", "celsius"))
        except sqlite3.Error as e:
            ctx["error"] = str(e)
        finally:
            conn.close()
    return TEMPLATES.TemplateResponse("mm.html", ctx)


@app.get("/mm/{key}", response_class=HTMLResponse)
def mm_wallet_page(request: Request, key: str, tab: str = "markets", p: int = 1):
    """Страница одного бота: все маркеты с итогом, все исполнения, все заявки (где они хранятся) — по 200 на страницу."""
    if key not in MM_WALLETS:
        raise HTTPException(status_code=404, detail="нет такого бота")
    from weather_cities import OBS_CITIES
    ctx = {"request": request, "w": None, "tab": tab, "p": max(p, 1), "rows": [], "pages": 1, "total": 0,
           "has_quotes": key in MM_QUOTES, "decide": MM_DECIDE}
    if not MM_DB.exists():
        return TEMPLATES.TemplateResponse("mm_wallet.html", ctx)
    conn = sqlite3.connect(f"file:{MM_DB}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    titles = _mm_titles()

    def what(city, lo, hi, cid):
        if city in ("pol", "own"):
            return titles.get(cid) or (cid[:10] + "…")
        return paper_bucket(lo, hi, OBS_CITIES.get(city, {}).get("unit", "celsius"))

    try:
        ctx["w"] = _mm_card(conn, key)
        off = (ctx["p"] - 1) * MM_PAGE
        if tab == "fills":
            ft = _mm_fills_table(key)
            if table_exists(conn, ft):
                ctx["total"] = conn.execute(f"SELECT COUNT(*) FROM {ft} WHERE wallet = ?", (key,)).fetchone()[0]
                rows = conn.execute(f"SELECT * FROM {ft} WHERE wallet = ? ORDER BY ts DESC LIMIT ? OFFSET ?", (key, MM_PAGE, off)).fetchall()
                cache = {}
                for r in rows:
                    d = dict(r)
                    if "bucket_lo" not in d:   # mm_fills: варианта нет в строке — берём из заявок
                        if d["condition_id"] not in cache:
                            q = conn.execute("SELECT bucket_lo, bucket_hi FROM mm_quotes WHERE condition_id = ? LIMIT 1", (d["condition_id"],)).fetchone()
                            cache[d["condition_id"]] = (q[0], q[1]) if q else (None, None)
                        d["bucket_lo"], d["bucket_hi"] = cache[d["condition_id"]]
                    d["when"] = _mm_dt(d["ts"])
                    d["city_ru"] = {"pol": "политика и прочее", "own": "свой выбор"}.get(d["city"], CITY_RU.get(d["city"], d["city"]))
                    d["what"] = what(d["city"], d["bucket_lo"], d["bucket_hi"], d["condition_id"])
                    ctx["rows"].append(d)
        elif tab == "quotes" and key in MM_QUOTES and table_exists(conn, "mm_quotes"):
            ctx["total"] = conn.execute(f"SELECT COUNT(*) FROM mm_quotes WHERE {MM_QUOTES[key]}").fetchone()[0]
            for r in conn.execute(f"SELECT * FROM mm_quotes WHERE {MM_QUOTES[key]} ORDER BY ts_from DESC LIMIT ? OFFSET ?", (MM_PAGE, off)):
                d = dict(r)
                d["from"], d["to"] = _mm_dt(d["ts_from"]), _mm_dt(d["ts_to"])
                d["city_ru"] = {"pol": "политика и прочее", "own": "свой выбор"}.get(d["city"], CITY_RU.get(d["city"], d["city"]))
                d["what"] = what(d["city"], d["bucket_lo"], d["bucket_hi"], d["condition_id"])
                if key == "mm_sel":
                    d["yes_bid"] = d["yes_bid"] if d["sel_yes"] else None
                    d["no_bid"] = d["no_bid"] if d["sel_no"] else None
                ctx["rows"].append(d)
        else:
            ctx["tab"] = "markets"
            if table_exists(conn, "mm_results"):
                ctx["total"] = conn.execute("SELECT COUNT(*) FROM mm_results WHERE wallet = ?", (key,)).fetchone()[0]
                for r in conn.execute("SELECT * FROM mm_results WHERE wallet = ? ORDER BY local_date DESC, settled_at DESC LIMIT ? OFFSET ?",
                                      (key, MM_PAGE, off)):
                    d = dict(r)
                    d["city_ru"] = {"pol": "политика и прочее", "own": "свой выбор"}.get(d["city"], CITY_RU.get(d["city"], d["city"]))
                    d["what"] = what(d["city"], d["bucket_lo"], d["bucket_hi"], d["condition_id"])
                    d["settled"] = _mm_dt(d["settled_at"])
                    ctx["rows"].append(d)
        ctx["pages"] = max(1, -(-ctx["total"] // MM_PAGE))
    except sqlite3.Error as e:
        ctx["error"] = str(e)
    finally:
        conn.close()
    return TEMPLATES.TemplateResponse("mm_wallet.html", ctx)


# ---- /bets: все ставки всех кошельков — открытые и закрытые отдельно (2026-09-27, просьба Alex) ----
# Оформление по пяти присланным макетам: пастельные плитки (Payoneer), «движение денег» по дням (Fundcy),
# «последние» плиткой 2×2 со статусами (Finance Health), «ждут итога» с датой квадратиком (Upcoming Payments),
# закрытые списком с круглым значком (Analytics / VISA).
GROUP_TONE = {"Главная модель": "g1", "Другие версии обучаемой модели": "g2", "Прогноз по формулам (раньше)": "g3",
              "Тот же сигнал, но покупка своей заявкой": "g4", "Повтор за сильными трейдерами": "g5", "Живые замеры": "g5", "LLM каждый час": "g2"}


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


# ---- города (2026-09-29, просьба Alex: «статистика каждого города, где играли, + или −; по клику — всё по городу:
# все ставки, на что смотрели, почему ставили») ----
def _city_ru(city):
    return CITY_RU.get(city, city.replace("_", " ").title())


def _sd(bets):
    return math.sqrt(sum(b["cost"] ** 2 * (1 - b["price"]) / b["price"] for b in bets
                         if b["price"] and 0 < b["price"] < 1 and b["cost"]))


def _city_sum(bs):
    done = [b for b in bs if b["pnl"] is not None]
    pnl = sum(b["pnl"] for b in done)
    staked = sum(b["cost"] for b in done)
    won = sum(1 for b in done if b["pnl"] > 0.005)
    return {"n": len(done), "won": won, "winrate": round(100 * won / len(done)) if done else None, "pnl": pnl,
            "roi": 100 * pnl / staked if staked else None, "open": sum(1 for b in bs if b["pnl"] is None),
            "luck": luck(pnl, _sd(done)), "last": max((b["local_date"] for b in bs), default=None)}


@app.get("/cities", response_class=HTMLResponse)
def cities_page(request: Request, w: str = ""):
    conn = db()
    try:
        bets = all_bets(conn)
    finally:
        conn.close()
    wallets = sorted({(b["w"]["key"], b["w"]["name"]) for b in bets}, key=lambda x: x[1])
    if w:
        bets = [b for b in bets if b["w"]["key"] == w]
    by = {}
    for b in bets:
        by.setdefault(b["city"], []).append(b)
    rows = [dict(_city_sum(bs), city=c, name=_city_ru(c), wallets=len({b["w"]["key"] for b in bs})) for c, bs in by.items()]
    rows.sort(key=lambda r: -r["pnl"])
    tot = _city_sum(bets)
    return TEMPLATES.TemplateResponse("cities.html", {"request": request, "rows": rows, "tot": tot, "wallets": wallets, "w": w,
                                                      "n_plus": sum(r["pnl"] > 0.005 for r in rows),
                                                      "n_minus": sum(r["pnl"] < -0.005 for r in rows)})


@app.get("/cities/{city}", response_class=HTMLResponse)
def city_bets_page(request: Request, city: str):
    """Всё по городу: итог по кошелькам и каждый день — итог, что показывали рынок и главная модель в 08:00,
    решение каждого кошелька с причиной (ставка: шанс модели против цены; пропуск/не купили: причина из записи)."""
    from weather_cities import OBS_CITIES
    conn = db()
    try:
        bets = [b for b in all_bets(conn) if b["city"] == city]
        rows = []
        if table_exists(conn, "paper_trades"):
            rows += [dict(r, _tbl="p") for r in conn.execute("SELECT * FROM paper_trades WHERE city = ? ORDER BY local_date DESC",
                                                             (city,))]
        if table_exists(conn, "paper_obs_trades"):
            rows += [dict(r, _tbl="o") for r in conn.execute("SELECT * FROM paper_obs_trades WHERE city = ? ORDER BY local_date DESC",
                                                             (city,))]
        outcomes = {r["local_date"]: (r["win_lo"], r["win_hi"]) for r in
                    conn.execute("SELECT local_date, win_lo, win_hi FROM weather_poly_outcomes WHERE city = ?", (city,))} \
            if table_exists(conn, "weather_poly_outcomes") else {}
        facts = {r["local_date"]: r["actual_max"] for r in
                 conn.execute("SELECT local_date, actual_max FROM weather_station_daily WHERE city = ?", (city,))} \
            if table_exists(conn, "weather_station_daily") else {}
        dates = sorted({r["local_date"] for r in rows}, reverse=True)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(snapshots)")}
        mcol = "ml3_model_p" if "ml3_model_p" in cols else "model_p"
        seen = {}
        for d in dates:  # что показывали рынок и главная модель утром: первый снимок дня с 08:00 местного
            snap = conn.execute(f"""SELECT bucket_lo, bucket_hi, unit, market_p, {mcol} AS mp FROM snapshots
                                    WHERE city = ? AND local_date = ? AND ts_utc = (SELECT MIN(ts_utc) FROM snapshots
                                    WHERE city = ? AND local_date = ? AND local_hour >= 8 AND local_hour < 12)""",
                                (city, d, city, d)).fetchall()
            if snap:
                top_m = max(snap, key=lambda s: s["market_p"] or 0)
                top_v = max((s for s in snap if s["mp"] is not None), key=lambda s: s["mp"], default=None)
                seen[d] = {"mkt": (paper_bucket(top_m["bucket_lo"], top_m["bucket_hi"], top_m["unit"]), top_m["market_p"]),
                           "model": (paper_bucket(top_v["bucket_lo"], top_v["bucket_hi"], top_v["unit"]), top_v["mp"]) if top_v else None}
    finally:
        conn.close()
    unit = OBS_CITIES.get(city, {}).get("unit", "celsius")
    sym = "°F" if unit == "fahrenheit" else "°C"
    days = []
    for d in dates:
        items = []
        for r in (x for x in rows if x["local_date"] == d):
            is_no = r["_tbl"] == "o" or r.get("side") == "no"
            bucket = paper_bucket(r["bucket_lo"], r["bucket_hi"], r["unit"]) if r.get("bucket_lo") is not None else None
            it = {"w": _wallet_meta(r["wallet"]), "status": r["status"], "what": (("против " if is_no else "на ") + bucket) if bucket else None,
                  "reason": r.get("reason"), "pnl": _pnl(r) if r["status"] in ("won", "lost", "void") else None,
                  "cost": (r["stake"] or 0) + _fee(r) if r["status"] in ("open", "resting", "won", "lost", "void") else None,
                  "price": r.get("price")}
            mp, kp = r.get("model_p"), r.get("market_p")
            if r["_tbl"] == "o":
                it["why"] = r.get("reason") or "станция уже исключила этот вариант"
            elif r["status"] in ("open", "resting", "won", "lost", "void") and mp is not None and kp is not None:
                it["why"] = (f"{'«нет» — ' if is_no else ''}модель {mp * 100:.0f}%, рынок {kp * 100:.0f}% — перевес "
                             f"{(mp - kp) * 100:+.0f} п.п.; купили по {r['price'] * 100:.1f}¢" if r.get("price") else "")
            else:
                it["why"] = r.get("reason") or ""
            items.append(it)
        placed = [i for i in items if i["status"] in ("open", "resting", "won", "lost", "void")]
        placed.sort(key=lambda i: (i["pnl"] is None, -(i["pnl"] or 0)))
        other = [i for i in items if i not in placed]
        oc = outcomes.get(d)
        days.append({"date": d, "placed": placed, "other": other,
                     "pnl": sum(i["pnl"] for i in placed if i["pnl"] is not None),
                     "outcome": paper_bucket(oc[0], oc[1], unit) if oc else None,
                     "fact": facts.get(d), "sym": sym, "seen": seen.get(d)})
    by_w = {}
    for b in bets:
        by_w.setdefault(b["w"]["key"], []).append(b)
    wal = [dict(_city_sum(bs), w=bs[0]["w"]) for bs in by_w.values()]
    wal.sort(key=lambda r: -r["pnl"])
    return TEMPLATES.TemplateResponse("city_bets.html", {"request": request, "city": city, "name": _city_ru(city),
                                                         "tot": _city_sum(bets), "wal": wal, "days": days})


# ---- трейдеры (2026-09-29, просьба Alex: «за кем повторяем, кто даёт плюс, кто минус — чтобы исключить при реальных
# деньгах»). Ставки кошелька copy: в reason — «повтор за 0x12345678…» (weather_copy.py), по началу адреса сопоставляем
# с рейтингом sharp_wallets и именами trader_names (weather_sharp_rank.py). ----
import re as _re
_COPY_RE = _re.compile(r"повтор за (0x[0-9a-fA-F]{8})")


def _copy_bets(conn):
    out = []
    if not table_exists(conn, "paper_trades"):
        return out
    for r in conn.execute("SELECT * FROM paper_trades WHERE wallet = 'copy' AND status IN ('open', 'resting', 'won', 'lost', 'void')"):
        r = dict(r)
        m = _COPY_RE.search(r.get("reason") or "")
        if not m:
            continue
        their = _re.search(r"по (\d+)¢", r["reason"])
        lag = _re.search(r"через (\d+) с", r["reason"])
        out.append({"pref": m.group(1).lower(), "city": r["city"], "city_ru": _city_ru(r["city"]), "local_date": r["local_date"],
                    "what": "на " + paper_bucket(r["bucket_lo"], r["bucket_hi"], r["unit"]), "status": r["status"],
                    "price": r["price"], "cost": (r["stake"] or 0) + _fee(r),
                    "pnl": _pnl(r) if r["status"] in ("won", "lost", "void") else None,
                    "their": int(their.group(1)) / 100 if their else None, "lag": int(lag.group(1)) if lag else None,
                    "via": "слушатель" if "слушатель" in r["reason"] else "опрос", "placed": _dt(r.get("placed_at") or r.get("snapshot_ts"))})
    return out


def _traders_meta(conn):
    sharp, names = {}, {}
    if table_exists(conn, "sharp_wallets"):
        for r in conn.execute("SELECT wallet, n, pnl, turnover, ranked_at FROM sharp_wallets"):
            sharp[r["wallet"].lower()] = dict(r)
    if table_exists(conn, "trader_names"):
        names = {r["wallet"].lower(): r["name"] for r in conn.execute("SELECT wallet, name FROM trader_names")}
    return sharp, names


@app.get("/traders", response_class=HTMLResponse)
def traders_page(request: Request):
    conn = db()
    try:
        bets = _copy_bets(conn)
        sharp, names = _traders_meta(conn)
    finally:
        conn.close()
    by = {}
    for b in bets:
        by.setdefault(b["pref"], []).append(b)
    full = {w[:10]: w for w in list(sharp) + list(names)}
    rows = []
    for pref in set(by) | {w[:10] for w in sharp}:
        w = full.get(pref, pref)
        s = sharp.get(w)
        rows.append(dict(_city_sum(by.get(pref, [])), pref=pref, wallet=w, name=names.get(w) or (pref + "…"),
                         listed=s is not None, their_pnl=s["pnl"] if s else None, their_n=s["n"] if s else None,
                         their_pct=100 * s["pnl"] / s["turnover"] if s and s["turnover"] else None))
    rows.sort(key=lambda r: (-(r["n"] + r["open"] > 0), -r["pnl"], -(r["their_pnl"] or 0)))
    return TEMPLATES.TemplateResponse("traders.html", {"request": request, "rows": rows, "tot": _city_sum(bets),
                                                       "n_plus": sum(r["pnl"] > 0.005 for r in rows),
                                                       "n_minus": sum(r["pnl"] < -0.005 for r in rows),
                                                       "n_listed": len(sharp)})


@app.get("/traders/{pref}", response_class=HTMLResponse)
def trader_page(request: Request, pref: str):
    pref = pref.lower()[:10]
    conn = db()
    try:
        bets = [b for b in _copy_bets(conn) if b["pref"] == pref]
        sharp, names = _traders_meta(conn)
    finally:
        conn.close()
    w = next((x for x in list(sharp) + list(names) if x.startswith(pref)), pref)
    bets.sort(key=lambda b: b["placed"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    nv = datetime.now(VIEWER_TZ)
    for b in bets:
        b["placed_txt"] = _when(b["placed"], nv) if b["placed"] else "—"
    return TEMPLATES.TemplateResponse("trader.html", {"request": request, "pref": pref, "wallet": w,
                                                      "name": names.get(w) or (pref + "…"), "s": sharp.get(w),
                                                      "tot": _city_sum(bets), "bets": bets})


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
        # 2026-09-29 (Alex, Чунцин): оценка прошла, а Polymarket ещё не подтвердил итог (оракул UMA: «предложен» →
        # период оспаривания) — пишем «ждём подтверждения», а не время в прошлом
        b["overdue"] = bool(c and c < now)
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


_LOG_NOISE = ("Found orphan containers", "level=warning msg=")


def _clean_log(text, max_lines=120):
    """Лог для показа: без служебного шума docker, хвост до max_lines строк."""
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip() and not any(x in ln for x in _LOG_NOISE)]
    cut = len(lines) - max_lines
    return (f"… (ещё {cut} строк выше)\n" if cut > 0 else "") + "\n".join(lines[-max_lines:])


def _log_tail(name, max_bytes=16000, max_lines=15):
    try:
        with open(DB_PATH.parent.parent / "logs" / name, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - max_bytes))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    return _clean_log(text, max_lines) or None


def system_events(conn, days=3):
    from jobs_info import JOBS
    label = {k: l for k, l, *_ in JOBS}
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    ev = []

    def add(t, kind, title, detail="", link=None, more=None):
        if t and t >= since:
            ev.append({"t": t, "kind": kind, "title": title, "detail": detail, "link": link, "more": more})

    # 2026-09-29 (просьба Alex: «для каждого уведомления видеть, что конкретно произошло»): у события — подробности
    # (more): лог запуска (job_log.output, пишет job_wrap.py с 29.09), маркеты с итогом, ставки по одной, итоги обучения.
    logfile = {k: lg for k, _l, _s, _sc, _a, lg in JOBS}
    latest_job = {}

    def log_more(job, output, t):
        if output and output.strip():
            return {"type": "log", "text": _clean_log(output)}
        if latest_job.get(job) == t and logfile.get(job):  # до 29.09 вывод не сохранялся — у последнего запуска хвост файла
            tail = _log_tail(logfile[job])
            if tail:
                return {"type": "log", "text": tail, "note": f"Последние строки файла data/logs/{logfile[job]}: вывод этого запуска отдельно ещё "
                                                                   "не сохранён (сохраняется с 29.09 вечера), поэтому часть строк может быть от прошлых запусков."}
        return {"type": "log", "text": None}

    from fixes import is_fixed, last_fixes
    fx = last_fixes(conn)
    if table_exists(conn, "job_log"):
        ie = ", item_errors" if "item_errors" in [c[1] for c in conn.execute("PRAGMA table_info(job_log)")] else ", 0 AS item_errors"
        oc = ", output" if "output" in [c[1] for c in conn.execute("PRAGMA table_info(job_log)")] else ", NULL AS output"
        jl = conn.execute(f"SELECT job, started_at, finished_at, rc, duration_s, om_calls{ie}{oc} FROM job_log WHERE finished_at >= ? ORDER BY finished_at",
                          (since.isoformat(),)).fetchall()
        for r in jl:
            latest_job[r["job"]] = _dt(r["finished_at"])
        for r in jl:
            t = _dt(r["finished_at"])
            lm = log_more(r["job"], r["output"], t)
            dur = f"{r['duration_s']:.0f} с" if r["duration_s"] < 90 else f"{r['duration_s'] / 60:.0f} мин"
            if r["job"] not in label:
                continue  # ручные запуски (проверки, пробное обучение) — не события системы
            if (r["rc"] != 0 or r["item_errors"]) and is_fixed(fx, r["job"], r["finished_at"]):
                # 2026-09-28: исправленные ошибки — в истории остаются, но зелёным и с тем, что сделали (fixes.py)
                what = "упал" if r["rc"] != 0 else f"отработал с пропусками ({r['item_errors']})"
                add(t, "ok", f"Исправлено: «{label.get(r['job'], r['job'])}» {what}",
                    f"исправлено {fx[r['job']][0].astimezone(VIEWER_TZ):%d.%m %H:%M}: {fx[r['job']][1]}", None, lm)
            elif r["rc"] != 0:
                why = "остановлен по пределу времени" if r["rc"] in (124, 137, 143) else f"код выхода {r['rc']}"
                add(t, "fail", f"Ошибка: «{label.get(r['job'], r['job'])}»", f"{why} · шёл {dur} · подробности на странице «Здоровье системы»", "/status", lm)
            elif r["item_errors"]:
                add(t, "fail", f"С пропусками: «{label.get(r['job'], r['job'])}»",
                    f"отработал, но пропущено из-за ошибок: {r['item_errors']} — остальное обработано · подробности на странице «Здоровье системы»", "/status", lm)
            elif r["job"] in JOB_DONE and r["job"] not in FREQUENT_JOBS and r["job"] != "weather_ml_train":
                extra = f" · запросов к Open-Meteo: {r['om_calls']}" if r["om_calls"] else ""
                add(t, "data", JOB_DONE.get(r["job"], f"Отработал «{label.get(r['job'], r['job'])}»"), f"за {dur}{extra}", None, lm)
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
            ex = d.get("exam") or {}
            add(_dt(r["trained_at"]), "train", "Пробное обучение модели" if d.get("dry_run") else "Модель переобучилась",
                " · ".join(parts), "/training",
                {"type": "train", "data": d.get("data", {}), "versions": d.get("versions", []), "checks": d.get("checks", []),
                 "exam": ex, "verdict": v, "dur": d.get("duration_s") or 0,
                 "imp": [{"ru": feature_ru(i["name"]), "pct": i["pct"]} for i in (d.get("importance") or [])[:6]]})
    if table_exists(conn, "weather_poly_outcomes"):
        from weather_cities import OBS_CITIES
        groups = {}
        for r in conn.execute("SELECT city, local_date, win_lo, win_hi, resolved_at FROM weather_poly_outcomes WHERE resolved_at >= ?", (since.isoformat(),)):
            t = _dt(r["resolved_at"])
            if t:
                groups.setdefault(t.replace(second=0, microsecond=0), []).append(r)
        mk_bets = {}
        for b in all_bets(conn):
            if b["pnl"] is not None:
                mk_bets.setdefault((b["city"], b["local_date"]), []).append(b)
        for t, rs in groups.items():
            n = len(rs)
            cities = [CITY_RU.get(r["city"], r["city"]) for r in rs]
            rows = []
            for r in sorted(rs, key=lambda r: CITY_RU.get(r["city"], r["city"])):
                bs = mk_bets.get((r["city"], r["local_date"]), [])
                unit = OBS_CITIES.get(r["city"], {}).get("unit", "celsius")
                rows.append({"city": CITY_RU.get(r["city"], r["city"]), "slug": r["city"], "date": f"{r['local_date'][8:10]}.{r['local_date'][5:7]}",
                             "win": paper_bucket(r["win_lo"], r["win_hi"], unit) or "?", "n": len(bs),
                             "won": sum(1 for b in bs if b["pnl"] > 0.005), "pnl": sum(b["pnl"] for b in bs)})
            add(t, "result", f"Пришли итоги {n} {'маркета' if n % 10 == 1 and n % 100 != 11 else 'маркетов'}",
                ", ".join(sorted(cities)[:12]) + (f" и ещё {n - 12}" if n > 12 else ""), None, {"type": "markets", "rows": rows})
    # ставки — сводкой по каждому запуску; подробно — на странице «Ставки»
    bets = all_bets(conn)
    runs_open, runs_close = {}, {}
    for b in bets:
        if b["placed"] and b["placed"] >= since:
            # по часам: кошелёк copy покупает по одной ставке — иначе лента тонет в «открыто: 1»
            runs_open.setdefault(b["placed"].replace(minute=0, second=0, microsecond=0), []).append(b)
        if b["settled"] and b["settled"] >= since:
            runs_close.setdefault(b["settled"].replace(second=0, microsecond=0), []).append(b)
    def wallet_link(bs):
        # 2026-09-29 (Alex): «подробнее» у ставок ведёт в кошелёк; если кошельков несколько — на обзор кошельков
        keys = {b["w"]["key"] for b in bs}
        return f"/paper?w={next(iter(keys))}" if len(keys) == 1 else "/paper"

    def wallet_names(bs):
        # 2026-09-29 (Alex): рядом с названием — код кошелька (mm_mk, ml3 ...), чтобы сразу было видно, какой это
        ws = sorted({f'{b["w"]["name"]} ({b["w"]["key"]})' for b in bs})
        return ", ".join(ws[:4]) + (f" и ещё {len(ws) - 4}" if len(ws) > 4 else "")

    def bet_rows(bs):
        return {"type": "bets", "rows": [
            {"code": b["w"]["key"], "wallet": b["w"]["name"], "city": b["city_ru"], "slug": b["city"],
             "date": f"{b['local_date'][8:10]}.{b['local_date'][5:7]}", "what": b["what"], "price": b["price"], "cost": b["cost"],
             "pnl": b["pnl"], "status": b["status"], "win_amt": b["win_amt"]}
            for b in sorted(bs, key=lambda b: (b["w"]["key"], b["city_ru"]))]}

    for t, bs in runs_open.items():
        last = max(b["placed"] for b in bs)
        add(last, "bets", f"Открыто ставок: {len(bs)} на ${sum(b['cost'] for b in bs):.2f}", wallet_names(bs), wallet_link(bs), bet_rows(bs))
    for t, bs in runs_close.items():
        pnl = sum(b["pnl"] for b in bs)
        won = sum(1 for b in bs if b["pnl"] > 0.005)
        add(t, "bets", f"Закрыто ставок: {len(bs)} · угадано {won} · итог {'+' if pnl >= 0 else '−'}${abs(pnl):.2f}",
            wallet_names(bs), wallet_link(bs), bet_rows(bs))
    if table_exists(conn, "alerts"):
        for r in conn.execute("SELECT message, first_seen, resolved_at FROM alerts"):
            am = {"type": "log", "text": r["message"]}
            add(_dt(r["first_seen"]), "alert", "Тревога", r["message"], "/status", am)
            if r["resolved_at"]:
                add(_dt(r["resolved_at"]), "ok", "Тревога снята", r["message"], None, am)
    f = DB_PATH.parent.parent / "ALERT_DB_LOCKED"
    try:
        txt = f.read_text().strip()
        add(datetime.fromtimestamp(f.stat().st_mtime, timezone.utc), "fail", "Сторож базы: база была занята", txt[:300], "/status",
            {"type": "log", "text": txt[-6000:]})
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
    # 2026-10-02: резервная копия на HDD (backup_db.sh, крон 03:10) — итог последней копии
    backup = None
    try:
        backup = json.loads((DB_PATH.parent.parent / "backup_status.json").read_text())
        bt = datetime.fromisoformat(backup["at"])
        backup.update(when=_when(bt, datetime.now(VIEWER_TZ)), size_txt=_size(backup.get("size_bytes", 0)),
                      fresh=backup.get("status") == "ok" and (datetime.now(timezone.utc) - bt).total_seconds() < 26 * 3600)
    except (OSError, ValueError, KeyError):
        backup = None
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
    return TEMPLATES.TemplateResponse("audit.html", {"request": request, "a": last, "history": history, "night": night, "backup": backup})


# ---- живое обновление (2026-09-28, просьба Alex: «видеть, что сайт обновился, без перезагрузки») ----
# Страница держит соединение /api/stream (Server-Sent Events): сервер раз в 3 с сверяет отпечаток данных
# (api_version) и при изменении сразу шлёт его — base.html тут же подгружает эту же страницу и подменяет содержимое
# (с сохранением прокрутки, вкладок и фильтров). Частые скрипты (живые замеры каждые
# 2 мин, повтор трейдеров каждые 5 мин) в отпечаток не входят, пока не сделали ставку, — иначе страница дёргалась бы.
_VER_CACHE = {"t": 0.0, "v": None}
_VER_SQL = (
    "SELECT COUNT(*), MAX(placed_at), MAX(settled_at), SUM(status = 'open') FROM paper_trades",
    "SELECT COUNT(*), MAX(placed_at), MAX(settled_at), SUM(status = 'open') FROM paper_obs_trades",
    "SELECT MAX(rowid) FROM snapshots",
    "SELECT MAX(rowid) FROM snapshots_fast",
    "SELECT MAX(rowid) FROM weather_poly_outcomes",
    "SELECT MAX(run_at) FROM audit_log",
    "SELECT MAX(run_at) FROM night_check",
    "SELECT MAX(trained_at) FROM ml_train_log",
    "SELECT COUNT(*), MAX(rowid) FROM alerts",
    "SELECT rowid FROM job_log WHERE job NOT IN ('weather_obs_live', 'weather_copy', 'weather_alerts') ORDER BY rowid DESC LIMIT 1",
)


@app.get("/api/version")
def api_version():
    if _VER_CACHE["v"] is not None and time.time() - _VER_CACHE["t"] < 2.5:
        return _VER_CACHE["v"]
    import hashlib
    parts = []
    try:
        conn = db()
        try:
            for q in _VER_SQL:
                try:
                    r = conn.execute(q).fetchone()
                    parts.append(repr(tuple(r) if r is not None else None))  # tuple: repr(Row) — адрес в памяти, менялся бы каждый раз
                except sqlite3.Error:
                    parts.append("-")
        finally:
            conn.close()
    except sqlite3.Error:
        parts.append("db")
    try:
        import notes
        ns = notes.all_notes()
        parts.append(repr([(n["id"], n["due"], n["done_at"]) for n in ns]))
    except sqlite3.Error:
        parts.append("notes")
    v = {"v": hashlib.md5("|".join(parts).encode()).hexdigest()[:12], "at": datetime.now(VIEWER_TZ).strftime("%H:%M")}
    _VER_CACHE.update(t=time.time(), v=v)
    return v


@app.get("/api/stream")
async def api_stream(request: Request):
    import asyncio
    from fastapi.responses import StreamingResponse
    from starlette.concurrency import run_in_threadpool

    async def gen():
        last, quiet = None, 0
        while True:
            if await request.is_disconnected():
                break
            v = (await run_in_threadpool(api_version))["v"]
            if v != last:
                last, quiet = v, 0
                yield f"data: {v}\n\n"
            else:
                quiet += 1
                if quiet >= 8:  # ~25 с тишины — пустая строка, чтобы прокси и браузер не закрыли соединение
                    quiet = 0
                    yield ": ping\n\n"
            await asyncio.sleep(3)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# ---- заметки с датой (2026-09-28, просьба Alex: «до 12 октября я всё забуду») — notes.py ----
@app.get("/services", response_class=HTMLResponse)
def services_page(request: Request):
    """05.10 (просьба Alex): все внешние сервисы и подписки — список в services_info.py; расход OpenRouter — живой из базы."""
    from services_info import SERVICES, SUBSCRIPTIONS
    today = datetime.now(VIEWER_TZ).date()
    subs = []
    for x in SUBSCRIPTIONS:
        x = dict(x)
        x["days"] = (date.fromisoformat(x["ends"]) - today).days if x.get("ends") else None
        if x.get("openrouter"):
            conn = db()
            month = datetime.now(timezone.utc).strftime("%Y-%m-01")
            x["spent_month"] = conn.execute("SELECT COALESCE(SUM(cost), 0) FROM llm_hour_preds WHERE ts_utc >= ?", (month,)).fetchone()[0] \
                if table_exists(conn, "llm_hour_preds") else 0.0
            conn.close()
            x["limit"] = sum(LLM_LIMIT.values())
        subs.append(x)
    monthly = sum(x["monthly"] for x in subs if x["status"] == "active" and x.get("monthly"))
    n = sum(len(items) for _, items in SERVICES)
    live = sum(1 for _, items in SERVICES for i in items if i["mode"] == "live")
    return TEMPLATES.TemplateResponse("services.html", {"request": request, "groups": SERVICES, "subs": subs, "monthly": monthly,
                                                        "n": n, "live": live})


# 06.10 (просьба Alex: «довести до конца, чтобы приносило прибыль»): одна страница — кандидаты на реальные деньги, где каждый
# сейчас, порог (записан заранее, docs/PRD.md §4а), сколько осталось до решения. Кандидаты, выбранные ПОСЛЕ того, как увидели их
# плюс (techno), судим только по свежим ставкам — с 07.10.
REAL_CANDIDATES = [
    {"key": "mm", "name": "Бот-мейкер, счёт $100", "wallets": ["mm100"], "decide": "2026-10-14",
     "what": "Ставит заявки на дешёвую сторону и зарабатывает на склейке «да» + «нет». Строго как вживую: банк $100, 5 долей, только гарантированные исполнения.",
     "rule": "на счёте больше $101 к 14.10 и плюс в обе недели (02-08.10 и 09-13.10)", "since": "2026-10-02",
     "then": "живой тест $100 рядом с бумажным mm100"},
    {"key": "mmf", "name": "Бот-мейкер, сосредоточенный $100", "wallets": ["mm100f"], "decide": "2026-10-21",
     "what": "Тот же строгий бот с банком $100, но только в 5 самых оживлённых городах (по прошлым дням) и без пары не больше 5 долей одной стороны — чтобы складывались пары, а не лотерея, как у mm100. На прошлых днях выбранный заранее вариант (2 города) не прошёл (−$7), 5 городов были в плюсе — но выбраны задним числом, поэтому проверяем на будущих днях.",
     "rule": "за 08-21.10: итог > 0 и плюс в обе недели (08-14.10 и 15-21.10)", "since": "2026-10-08",
     "then": "живой тест $100 рядом с бумажным mm100f"},
    {"key": "llm", "name": "LLM каждый час", "wallets": ["llm_gem", "llm_ds", "llm_mix", "llm_cal"], "decide": "2026-10-18",
     "what": "Gemini и DeepSeek каждый час видят всё о дне и называют шансы; смесь Gemini + LightGBM и Gemini + рынок (с 08.10) — без своих запросов.",
     "rule": "за 04-17.10: ≥ 30 закрытых ставок и итог ≥ +5%; шанс на верный вариант выше рынка в тот же час на 3+ процентных пункта", "since": "2026-10-04",
     "until": "2026-10-17", "need_n": 30, "need_roi": 5.0, "acc": True, "then": "больше городов, потом малый живой тест"},
    {"key": "day", "name": "Дневная модель", "wallets": ["ml_day"], "decide": "2026-10-20",
     "what": "LightGBM в 10/12/14 ч видит замеры с утра и цену рынка в этот час. На проверке точнее рынка все 8 недель.",
     "rule": "с 06.10: ≥ 40 закрытых ставок и итог ≥ +2% (на проверке свежий период дал +3%)", "since": "2026-10-06",
     "need_n": 40, "need_roi": 2.0, "then": "малый живой тест рядом с бумажным ml_day"},
    {"key": "techno", "name": "Дешёвые «да» среди фаворитов", "wallets": ["techno"], "decide": "2026-10-20",
     "what": "«Да» за 8-30¢ на одном из 4 самых дорогих вариантов, если смесь модели и рынка не ниже цены. До 06.10: 34 ставки +71% — выбран по результату, поэтому судим только свежие ставки.",
     "rule": "только ставки с 07.10: ≥ 30 закрытых и итог ≥ +5%", "since": "2026-10-07", "need_n": 30, "need_roi": 5.0,
     "then": "малый живой тест"},
    {"key": "z", "name": "Смесь v3, «да» 20-40¢", "wallets": ["ml3_z"], "decide": "2026-10-17",
     "what": "Смесь главной модели с рынком, только «да» за 20-40¢ — на истории единственная прибыльная зона.",
     "rule": "к 17.10: ≥ 40 закрытых ставок и итог ≥ +5%", "since": "2026-10-03", "until": "2026-10-17", "need_n": 40, "need_roi": 5.0,
     "then": "малый живой тест"},
]


def _real_paper(conn, wallet, since, until=None):
    rs = conn.execute("SELECT * FROM paper_trades WHERE wallet = ? AND local_date >= ? AND local_date <= ? AND status NOT IN ('skip', 'nofill')",
                      (wallet, since, until or "9999")).fetchall()
    st = [r for r in rs if r["status"] in ("won", "lost", "void")]
    pnl, cost = sum(_pnl(r) for r in st), sum(r["stake"] + _fee(r) for r in st)
    sd = math.sqrt(sum(r["stake"] ** 2 * (1 - r["price"]) / r["price"] for r in st if r["price"] and 0 < r["price"] < 1 and r["stake"]))
    return {"wallet": wallet, "label": WALLET_INFO.get(wallet, (None, wallet))[1], "n": len(st), "won": sum(r["status"] == "won" for r in st),
            "pnl": pnl, "roi": 100 * pnl / cost if cost else None, "open": sum(r["status"] == "open" for r in rs), "luck": luck(pnl, sd)}


def _llm_accuracy(conn, wallet, since, until):
    """Средний шанс LLM на выигравший вариант минус цена этого варианта в тот же час (часы 08-19), п.п."""
    if not table_exists(conn, "llm_hour_preds"):
        return None
    diffs = []
    for city, d, pj, mj in conn.execute("""SELECT p.city, p.local_date, p.probs_json, p.market_json FROM llm_hour_preds p
            WHERE p.wallet = ? AND p.local_date BETWEEN ? AND ? AND p.local_hour BETWEEN 8 AND 19 AND p.probs_json IS NOT NULL""",
                                        (wallet, since, until)):
        a = conn.execute("SELECT actual_max FROM weather_station_daily WHERE city = ? AND local_date = ?", (city, d)).fetchone()
        if not a or a[0] is None:
            continue
        pr, mk = json.loads(pj or "{}"), json.loads(mj or "{}")
        tot = sum(mk.values()) or 1.0
        for lab, p in pr.items():
            rg = _llm_range(lab)
            if rg and rg[0] <= a[0] < rg[1] and lab in mk:
                diffs.append(100 * (p - mk[lab] / tot))
    return (sum(diffs) / len(diffs), len(diffs)) if diffs else None


def real_page_data(conn):
    today = datetime.now(VIEWER_TZ).date()
    out = []
    for c in REAL_CANDIDATES:
        c = dict(c)
        c["days_left"] = (date.fromisoformat(c["decide"]) - today).days
        if c["key"] == "mm":
            res, st, has = None, None, MM_DB.exists()
            if has:
                mc = sqlite3.connect(f"file:{MM_DB}?mode=ro", uri=True, timeout=10)
                try:
                    tot = mc.execute("SELECT COALESCE(SUM(pnl), 0), COALESCE(SUM(n_fills), 0), COUNT(*) FROM mm_results WHERE wallet = 'mm100'").fetchone()
                    w1 = mc.execute("SELECT COALESCE(SUM(pnl), 0) FROM mm_results WHERE wallet = 'mm100' AND local_date BETWEEN '2026-10-02' AND '2026-10-08'").fetchone()[0]
                    w2 = mc.execute("SELECT COALESCE(SUM(pnl), 0), COUNT(*) FROM mm_results WHERE wallet = 'mm100' AND local_date BETWEEN '2026-10-09' AND '2026-10-13'").fetchone()
                    st = mc.execute("SELECT cash, reserved, quotes FROM mm100_state ORDER BY ts DESC LIMIT 1").fetchone()
                    zs = mc.execute("SELECT COALESCE(SUM(pnl), 0), COALESCE(SUM(spent), 0), COALESCE(SUM(n_fills), 0) FROM mm_results WHERE wallet = 'mm_ws_zs'").fetchone()
                finally:
                    mc.close()
                bal = 100.0 + tot[0]
                c["rows"] = [{"label": "Счёт", "value": f"${bal:.2f}", "tone": "pos" if bal > 100.005 else ("neg" if bal < 99.995 else ""),
                              "note": f"{tot[2]} маркетов, исполнений {tot[1]}"},
                             {"label": "Неделя 02-08.10", "value": money_str(w1), "tone": "pos" if w1 > 0 else ("neg" if w1 < 0 else "")},
                             {"label": "Неделя 09-13.10", "value": money_str(w2[0]) if w2[1] else "ещё не началась",
                              "tone": ("pos" if w2[0] > 0 else "neg") if w2[1] else ""},
                             # 07.10: для сравнения — тот же бот без ограничения банка, строгая оценка исполнения (порог 02.10 сначала был по нему)
                             {"label": "Для сравнения: тот же бот без банка $100 (mm_ws_zs)", "value": money_str(zs[0]),
                              "tone": "pos" if zs[0] > 0 else ("neg" if zs[0] < 0 else ""),
                              "note": (f"{100 * zs[0] / zs[1]:+.1f}% от потраченного, исполнений {zs[2]}" if zs[1] else None)}]
                if st is not None:
                    c["warn"] = (f"Сейчас свободно ${st[0]:.2f}: деньги в открытых позициях, новые заявки — когда маркеты закроются и деньги "
                                 f"вернутся (так каждый день: бот упирается в банк $100)." if st[0] < 1 else None)
                ok = bal > 101 and w1 > 0 and (w2[1] and w2[0] > 0)
                c["status"] = ("pass" if ok else ("bad" if bal < 100 else "wait"))
                need = ([f"ещё +${101 - bal:.2f} к счёту"] if bal <= 101 else [f"счёт уже ${bal:.2f} (> $101)"]) + \
                       ([] if w1 > 0 else ["плюс в первой неделе"]) + ([] if (w2[1] and w2[0] > 0) else ["плюс во второй неделе (09-13.10)"])
                c["status_text"] = "проходит" if ok else "; ".join(need[:1] + [("нужен " if bal > 101 else "и ") + "; ".join(need[1:])] if len(need) > 1 else need)
        elif c["key"] == "mmf":
            if MM_DB.exists():
                mc = sqlite3.connect(f"file:{MM_DB}?mode=ro", uri=True, timeout=10)
                try:
                    q = "SELECT COALESCE(SUM(pnl), 0), COUNT(*), COALESCE(SUM(merged), 0) FROM mm_results WHERE wallet = 'mm100f' AND local_date BETWEEN ? AND ?"
                    tot, w1, w2 = (mc.execute(q, a).fetchone() for a in (("2026-10-08", "2026-10-21"), ("2026-10-08", "2026-10-14"), ("2026-10-15", "2026-10-21")))
                    st = mc.execute("SELECT cash FROM mm100f_state ORDER BY ts DESC LIMIT 1").fetchone() if \
                        mc.execute("SELECT 1 FROM sqlite_master WHERE name = 'mm100f_state'").fetchone() else None
                finally:
                    mc.close()
                bal = 100.0 + tot[0]
                wk = lambda w: (money_str(w[0]) if w[1] else "ещё нет закрытых", ("pos" if w[0] > 0 else "neg") if w[1] else "")
                c["rows"] = [{"label": "Счёт", "value": f"${bal:.2f}", "tone": "pos" if bal > 100.005 else ("neg" if bal < 99.995 else ""),
                              "note": f"{tot[1]} закрытых маркетов, склеено пар {tot[2]:.0f}" + (f", свободно ${st[0]:.2f}" if st else "")},
                             {"label": "Неделя 08-14.10", "value": wk(w1)[0], "tone": wk(w1)[1]},
                             {"label": "Неделя 15-21.10", "value": wk(w2)[0], "tone": wk(w2)[1]}]
                ok = bal > 100 and w1[1] and w1[0] > 0 and w2[1] and w2[0] > 0
                c["status"] = "pass" if ok else ("wait" if not tot[1] or not w2[1] else "bad")
                c["status_text"] = "проходит" if ok else ("ждём закрытых маркетов" if not tot[1] else
                                                         f"счёт ${bal:.2f}; нужен итог > 0 и плюс в обе недели")
        else:
            rows = [_real_paper(conn, w, c["since"], c.get("until")) for w in c["wallets"]]
            for r in rows:
                if c.get("acc") and r["wallet"] in ("llm_gem", "llm_ds"):
                    r["acc"] = _llm_accuracy(conn, r["wallet"], c["since"], c["until"])
            c["paper"] = rows
            best = max(rows, key=lambda r: (r["n"] >= c["need_n"] and (r["roi"] or -999) >= c["need_roi"], r["roi"] or -999))
            ok_n, ok_roi = best["n"] >= c["need_n"], (best["roi"] is not None and best["roi"] >= c["need_roi"])
            ok_acc = (not c.get("acc")) or best["wallet"] in ("llm_mix", "llm_cal") or (best.get("acc") and best["acc"][0] >= 3.0)
            c["status"] = "pass" if ok_n and ok_roi and ok_acc else ("wait" if not ok_n else "bad")
            need = []
            if not ok_n:
                need.append(f"ещё {c['need_n'] - best['n']} закрытых ставок")
            if not ok_roi:
                need.append(f"итог {best['roi']:+.0f}% → нужно ≥ +{c['need_roi']:.0f}%" if best["roi"] is not None else f"итог ≥ +{c['need_roi']:.0f}%")
            if not ok_acc:
                need.append("точность выше рынка на 3+ п.п.")
            c["status_text"] = "проходит" if c["status"] == "pass" else "; ".join(need)
            c["progress"] = min(100, round(100 * best["n"] / c["need_n"]))
            c["best"] = best["label"]
        out.append(c)
    return {"cands": out, "today": today.strftime("%d.%m")}


@app.get("/real", response_class=HTMLResponse)
def real_page(request: Request):
    conn = db()
    data = real_page_data(conn)
    conn.close()
    return TEMPLATES.TemplateResponse("real.html", {"request": request, **data})


@app.get("/notes", response_class=HTMLResponse)
def notes_page(request: Request):
    import notes
    today = notes.today()
    items = notes.all_notes()
    now_t = datetime.now(notes.TZ).strftime("%H:%M")
    for n in items:
        n["days"] = (date.fromisoformat(n["due"]) - date.fromisoformat(today)).days
        n["due_time"] = n.get("due_time") or notes.DEFAULT_TIME
        n["time_passed"] = n["days"] < 0 or (n["days"] == 0 and now_t >= n["due_time"])   # 03.10: время уже наступило
    groups = [("Сегодня и просрочено", [n for n in items if not n["done_at"] and n["days"] <= 0], "due"),
              ("Впереди", [n for n in items if not n["done_at"] and n["days"] > 0], "next"),
              ("Сделано", [n for n in items if n["done_at"]], "done")]
    return TEMPLATES.TemplateResponse("notes.html", {"request": request, "groups": groups, "today": today})


@app.post("/notes/add")
async def notes_add(request: Request):
    import notes
    d = await request.json()
    try:
        every = d.get("every_days")
        nid = notes.add(str(d.get("due", "")), str(d.get("title", "")), str(d.get("body", "")),
                        int(every) if every else None, str(d.get("due_time") or "") or None)
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
        due = notes.due_notes()
        v["notes"] = len(due)  # на сегодня и просроченные
        v["notes_due"] = [n["title"] for n in due][:3]  # плашка вверху всех страниц
    except sqlite3.Error:
        v["notes"], v["notes_due"] = 0, []
    try:
        conn = db()
        try:
            n = 0
            if table_exists(conn, "paper_trades"):
                n += conn.execute("SELECT COUNT(*) FROM paper_trades WHERE status IN ('open', 'resting')").fetchone()[0]
            if table_exists(conn, "paper_obs_trades"):
                n += conn.execute("SELECT COUNT(*) FROM paper_obs_trades WHERE status = 'open'").fetchone()[0]
            v["open"] = n
            if table_exists(conn, "paper_trades"):  # 2026-09-29: сколько городов, где мы ставили
                v["cities"] = conn.execute("SELECT COUNT(DISTINCT city) FROM paper_trades WHERE status IN ('open', 'won', 'lost', 'void')").fetchone()[0]
            if table_exists(conn, "sharp_wallets"):  # 2026-09-29: сколько трейдеров в списке повтора
                v["traders"] = conn.execute("SELECT COUNT(*) FROM sharp_wallets").fetchone()[0]
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
