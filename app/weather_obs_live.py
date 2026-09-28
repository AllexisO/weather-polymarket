"""
Стратегия по живым замерам станции — четвёртый виртуальный кошелёк
(2026-09-23).

Идея: Polymarket резолвит маркет по максимуму METAR-сводок станции за
местный день. Если станция уже показала 31°C, то все бакеты ниже 31
проиграли наверняка. Проверка на наших снимках (1965 штук): в 32 случаях
рынок ещё оценивал такой "мёртвый" бакет в 3-87¢, из 9 крупнейших в 8
замеры оказались правы (один раз — Пекин 2026-09-20 — METAR показал 30,
а Polymarket засчитал 28: сбойная сводка, риск стратегии).

Это не прогноз и не циркулярность: мы читаем тот же источник, по
которому маркет резолвится, просто раньше части рынка.

Правила (зафиксированы заранее):
- крон каждые 2 минуты (было 10: бэктест показал, что рынок
  поправляется за 10-20 минут после сводки); сводки — aviationweather.gov (бесплатно, без ключа);
- максимум за местный день = максимум уже опубликованных сводок
  (°F — округление до целого, как у NOAA; °C — целые из METAR);
- бакет "мёртв", если его верхняя граница ниже этого максимума;
- если у мёртвого бакета лучшая заявка на покупку Yes (bestBid) >= MIN_BID,
  покупаем No — с 2026-09-23 по реальному стакану токена No (CLOB /book):
  идём по заявкам на продажу от дешёвых к дорогим, пока не потратим
  STAKE или цена не превысит 1 - MIN_BID. Сколько реально купили и по
  какой средней цене — это и есть ставка (иначе бэктест по "последней
  сделке" врёт про исполнимость, см. Мадрид 11.09);
- если купить можно меньше чем на MIN_FILL — ставки нет, но случай
  пишется со статусом 'nofill' (сколько сигналов упирается в пустой стакан);
- ставка по одной на бакет в день; без защиты от сбойных сводок
  (сначала честно меряем, как часто это стреляет в ногу);
- расчёт — по weather_poly_outcomes: No выигрывает, если бакет не выиграл;
  для городов вне прогнозных (weather_cities.OBS_CITIES) исход дочитываем
  сами.

С 2026-09-24 исполнение — через polyexec.py (комиссия Polymarket, минимум
5 долей, задержка 2 с, выплата по цене закрытия доли No, data/STOP).

2026-09-27 (решение Alex): второй кошелёк obs_fmi — та же логика, но по 10-минутным
замерам финской метеослужбы (FMI, аэропорт Хельсинки-Вантаа, бесплатно, без ключа) —
только Хельсинки. METAR там выходит раз в полчаса, FMI — каждые 10 мин и за 1-2 мин
после замера: промежуточный замер может показать превышение раньше METAR. Ретро-проверка
22-25.09: 12 из 12 таких сигналов METAR подтвердил, но рынок почти всегда уже стоял на
0-5¢ — кошелёк проверяет это вживую. Максимум FMI округляется до целого (как METAR).
Ставки обоих кошельков — в paper_obs_trades, колонка wallet.

5-минутные замеры станций США (api.weather.gov) НЕ используем: проверка
2026-09-23 — Polymarket в 3 из 4 спорных дней засчитал METAR, а не
5-минутный максимум (Даллас 21.09: 5 мин — 99°F, METAR — 97°F, выиграл
96-97°F). Ставка по 5-минутке там бы проиграла.
"""

from jobmark import item_guard
import json
import math
import os
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from polyexec import final_price, simulate_buy, trading_stopped
from weather_cities import OBS_CITIES
from weather_edge import GAMMA, month_day_year_slug, parse_bucket
from weather_poly_resolve import fetch_winning_bucket

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
METAR_API = "https://aviationweather.gov/api/data/metar"
CLOB = "https://clob.polymarket.com"

START_BALANCE = 100.0
STAKE = 2.0  # 2026-09-25: было $5; при $5 и $100 на 48 городах кошельки упирались в деньги (решение Alex)
MIN_BID = 0.05  # ниже — прибыль на ставку копеечная, не стоит риска сбойной сводки
MIN_FILL = 1.0  # меньше доллара исполнения — считаем, что купить было нельзя
FMI_WFS = "https://opendata.fmi.fi/wfs"
FMI_STATIONS = {"helsinki": 100968}  # кошелёк obs_fmi: город -> fmisid (Хельсинки-Вантаа, та же площадка, что EFHK)


def ensure_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_obs_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            city TEXT NOT NULL,
            local_date TEXT NOT NULL,
            unit TEXT,
            bucket_lo REAL,
            bucket_hi REAL,
            obs_max REAL,
            obs_time_utc TEXT,
            placed_at TEXT,
            yes_bid REAL,
            price REAL,
            stake REAL,
            status TEXT NOT NULL DEFAULT 'open',
            payout REAL,
            settled_at TEXT,
            UNIQUE (city, local_date, bucket_lo)
        )
        """
    )
    # Когда мы ВПЕРВЫЕ увидели каждую сводку — задержка от времени замера
    # до нашего получения. Бэктест показал, что рынок реагирует через 10-12
    # минут после замера (Денвер), так что всё решает эта задержка.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS metar_seen (
            icao TEXT NOT NULL,
            obs_time_utc TEXT NOT NULL,
            first_seen_utc TEXT NOT NULL,
            temp_c REAL,
            PRIMARY KEY (icao, obs_time_utc)
        )
        """
    )
    # 2026-09-24: какой источник METAR присылает сводку первым — по
    # каждому источнику отдельно (aviationweather / NOAA tgftp).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS metar_seen_src (
            icao TEXT NOT NULL,
            obs_time_utc TEXT NOT NULL,
            source TEXT NOT NULL,
            first_seen_utc TEXT NOT NULL,
            temp_c REAL,
            PRIMARY KEY (icao, obs_time_utc, source)
        )
        """
    )
    cols = [r[1] for r in conn.execute("PRAGMA table_info(paper_obs_trades)")]
    if "reason" not in cols:
        conn.execute("ALTER TABLE paper_obs_trades ADD COLUMN reason TEXT")
    if "shares" not in cols:
        # 2026-09-24: исполнение через polyexec (комиссия, мин. 5 долей, задержка 2 с)
        conn.execute("ALTER TABLE paper_obs_trades ADD COLUMN shares REAL")
        conn.execute("ALTER TABLE paper_obs_trades ADD COLUMN fee REAL DEFAULT 0")
        conn.execute("ALTER TABLE paper_obs_trades ADD COLUMN book_json TEXT")
        conn.execute("""UPDATE paper_obs_trades SET shares = stake / price, fee = (stake / price) * 0.05 * price * (1 - price)
                        WHERE stake > 0 AND price > 0""")
    if "wallet" not in cols:
        # 2026-09-27: несколько кошельков по замерам (obs, obs_fmi) — у каждого свои ставки
        # на тот же вариант. SQLite не меняет UNIQUE у таблицы — пересоздаём (строк мало).
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ALTER TABLE paper_obs_trades RENAME TO paper_obs_trades_old")
        conn.execute(
            """
            CREATE TABLE paper_obs_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL DEFAULT 'obs',
                city TEXT NOT NULL, local_date TEXT NOT NULL, unit TEXT, bucket_lo REAL, bucket_hi REAL,
                obs_max REAL, obs_time_utc TEXT, placed_at TEXT, yes_bid REAL, price REAL, stake REAL,
                status TEXT NOT NULL DEFAULT 'open', payout REAL, settled_at TEXT, reason TEXT,
                shares REAL, fee REAL DEFAULT 0, book_json TEXT, ask_depth_usd REAL,
                UNIQUE (wallet, city, local_date, bucket_lo)
            )
            """
        )
        old = [r[1] for r in conn.execute("PRAGMA table_info(paper_obs_trades_old)")]
        keep = ", ".join(c for c in old)
        conn.execute(f"INSERT INTO paper_obs_trades ({keep}) SELECT {keep} FROM paper_obs_trades_old")
        conn.execute("DROP TABLE paper_obs_trades_old")
        conn.commit()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(paper_obs_trades)")]
    if "ask_depth_usd" not in cols:
        # сколько долларов No продавалось по цене не дороже 1 - MIN_BID в момент сигнала
        conn.execute("ALTER TABLE paper_obs_trades ADD COLUMN ask_depth_usd REAL")
    conn.commit()


def cash(conn, wallet="obs"):
    spent, back = conn.execute(
        "SELECT COALESCE(SUM(stake + COALESCE(fee, 0)), 0), COALESCE(SUM(COALESCE(payout, 0)), 0) "
        "FROM paper_obs_trades WHERE wallet = ?", (wallet,)
    ).fetchone()
    return START_BALANCE - spent + back


def fetch_missing_outcomes(conn, now):
    for r in conn.execute(
        """
        SELECT DISTINCT t.city, t.local_date FROM paper_obs_trades t
        LEFT JOIN weather_poly_outcomes p ON t.city = p.city AND t.local_date = p.local_date
        WHERE t.status = 'open' AND p.city IS NULL
        """
    ).fetchall():
        try:
            win = fetch_winning_bucket(OBS_CITIES[r["city"]]["poly_slug"], datetime.fromisoformat(r["local_date"]).date())
        except requests.RequestException:
            continue
        if win:
            conn.execute(
                "INSERT OR IGNORE INTO weather_poly_outcomes (city, local_date, win_lo, win_hi, resolved_at) VALUES (?, ?, ?, ?, ?)",
                (r["city"], r["local_date"], win[0], win[1], now),
            )
    conn.commit()


def settle(conn, now):
    """Выплата = доли No × цена закрытия доли No (1 / 0 / 0.5 при отмене)."""
    cache = {}
    for r in conn.execute("SELECT * FROM paper_obs_trades WHERE status = 'open' AND stake > 0").fetchall():
        slug = (f"highest-temperature-in-{OBS_CITIES[r['city']]['poly_slug']}-on-"
                f"{month_day_year_slug(datetime.fromisoformat(r['local_date']).date())}")
        try:
            fp = final_price(slug, (r["bucket_lo"], r["bucket_hi"]), "no", parse_bucket, cache)
        except (requests.RequestException, ValueError, KeyError):
            continue
        if fp is None:
            continue
        status = "won" if fp >= 0.99 else ("lost" if fp <= 0.01 else "void")
        conn.execute("UPDATE paper_obs_trades SET status = ?, payout = ?, settled_at = ? WHERE id = ?",
                     (status, r["shares"] * fp, now, r["id"]))
    conn.commit()


TGFTP = "https://tgftp.nws.noaa.gov/data/observations/metar/stations/{}.TXT"
RE_TEMP = re.compile(r" (M?\d{2})/(M?\d{2})? ")
RE_TGROUP = re.compile(r" T([01])(\d{3})([01])(\d{3})")


def parse_metar_temp(raw):
    """Температура °C из текста METAR: T-группа (десятые, США) или группа TT/DD."""
    m = RE_TGROUP.search(raw)
    if m:
        v = int(m.group(2)) / 10
        return -v if m.group(1) == "1" else v
    m = RE_TEMP.search(" " + raw + " ")
    if m:
        t = m.group(1)
        return -int(t[1:]) if t.startswith("M") else int(t)
    return None


def fetch_tgftp(icao):
    """Последняя сводка станции с сервера NOAA: (время замера UTC, °C) или None."""
    try:
        txt = requests.get(TGFTP.format(icao), timeout=15).text.strip().splitlines()
        t = datetime.strptime(txt[0].strip(), "%Y/%m/%d %H:%M").replace(tzinfo=timezone.utc)
        return t, parse_metar_temp(txt[1])
    except (requests.RequestException, ValueError, IndexError):
        return None


def observed_max_today(metars, cfg):
    tz = ZoneInfo(cfg["tz"])
    today = datetime.now(tz).date()
    best = None
    for m in metars:
        if m.get("temp") is None or not m.get("obsTime"):
            continue
        t = datetime.fromtimestamp(m["obsTime"], timezone.utc)
        if t.astimezone(tz).date() != today:
            continue
        v = round(m["temp"] * 9 / 5 + 32) if cfg["unit"] == "fahrenheit" else round(m["temp"])
        if best is None or v > best[0]:
            best = (v, t)
    return today, best


def run():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    now = datetime.now(timezone.utc)
    fetch_missing_outcomes(conn, now.isoformat())
    settle(conn, now.isoformat())

    icaos = [cfg["icao"] for cfg in OBS_CITIES.values()]
    # 2026-09-28: aviationweather не ответил за 30 с — раньше падал весь запуск; теперь идём дальше на втором
    # источнике (tgftp ниже), а сбой — пропуск («с пропусками» на /status), не падение.
    try:
        r = requests.get(METAR_API, params={"ids": ",".join(icaos), "hours": 30, "format": "json"}, timeout=30)
        r.raise_for_status()
        awc = r.json()
        if not isinstance(awc, list):
            raise ValueError(f"ответ не список: {str(awc)[:100]}")
    except (requests.RequestException, ValueError) as e:
        import jobmark
        jobmark.ITEM_ERRORS.append(f"aviationweather: {e}")
        print(f"aviationweather недоступен — только tgftp: {e}")
        awc = []
    by_station = {}
    for m in awc:
        by_station.setdefault(m["icaoId"], []).append(m)
        if m.get("obsTime"):
            t_iso = datetime.fromtimestamp(m["obsTime"], timezone.utc).isoformat()
            conn.execute(
                "INSERT OR IGNORE INTO metar_seen (icao, obs_time_utc, first_seen_utc, temp_c) VALUES (?, ?, ?, ?)",
                (m["icaoId"], t_iso, now.isoformat(), m.get("temp")),
            )
            conn.execute(
                "INSERT OR IGNORE INTO metar_seen_src (icao, obs_time_utc, source, first_seen_utc, temp_c) VALUES (?, ?, 'awc', ?, ?)",
                (m["icaoId"], t_iso, now.isoformat(), m.get("temp")),
            )
    # Второй источник — NOAA tgftp (файл с последней сводкой по станции).
    # Если он прислал сводку раньше aviationweather — используем её тоже.
    with ThreadPoolExecutor(12) as pool:
        tg = dict(zip(icaos, pool.map(fetch_tgftp, icaos)))
    for icao, got in tg.items():
        if not got or got[1] is None:
            continue
        t, temp = got
        conn.execute(
            "INSERT OR IGNORE INTO metar_seen_src (icao, obs_time_utc, source, first_seen_utc, temp_c) VALUES (?, ?, 'tgftp', ?, ?)",
            (icao, t.isoformat(), now.isoformat(), temp),
        )
        known = {mm.get("obsTime") for mm in by_station.get(icao, [])}
        if int(t.timestamp()) not in known:
            by_station.setdefault(icao, []).append({"icaoId": icao, "obsTime": int(t.timestamp()), "temp": temp, "src": "tgftp"})
    conn.commit()

    events_cache = {}
    for city, cfg in OBS_CITIES.items():
        with item_guard(city, conn):
            today, best = observed_max_today(by_station.get(cfg["icao"], []), cfg)
            if best is not None:
                bet_dead_buckets(conn, "obs", city, cfg, today, best[0], best[1], now, events_cache,
                                 f"станция уже {best[0]} (сводка {best[1]:%H:%M}Z)")
    for city, fmisid in FMI_STATIONS.items():
        with item_guard(f"obs_fmi {city}", conn):
            cfg = OBS_CITIES[city]
            try:
                today, best = fmi_max_today(fmisid, cfg)
            except (requests.RequestException, ValueError) as e:
                print(f"obs_fmi: {city} ошибка FMI — {e}", file=sys.stderr)
                continue
            if best is not None:
                bet_dead_buckets(conn, "obs_fmi", city, cfg, today, best[0], best[1], now, events_cache,
                                 f"10-мин замер FMI уже {best[0]} ({best[1]:%H:%M}Z)")
    for w in ("obs", "obs_fmi"):
        print(f"{w}: баланс ${cash(conn, w):.2f}")
    conn.close()


def fmi_max_today(fmisid, cfg):
    """Максимум 10-минутных замеров FMI за местный день (°C, до целого — как в METAR): (день, (макс, время))."""
    tz = ZoneInfo(cfg["tz"])
    today = datetime.now(tz).date()
    start = datetime.combine(today, datetime.min.time(), tz).astimezone(timezone.utc)
    r = requests.get(FMI_WFS, params={
        "service": "WFS", "version": "2.0.0", "request": "getFeature",
        "storedquery_id": "fmi::observations::weather::simple", "fmisid": fmisid,
        "parameters": "t2m", "starttime": start.strftime("%Y-%m-%dT%H:%M:%SZ")}, timeout=20)
    r.raise_for_status()
    times = re.findall(r"<BsWfs:Time>([^<]+)", r.text)
    vals = re.findall(r"<BsWfs:ParameterValue>([^<]+)", r.text)
    best = None
    for t, v in zip(times, vals):
        if v == "NaN":
            continue
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        if dt.astimezone(tz).date() != today:
            continue
        rounded = math.floor(float(v) + 0.5)
        if best is None or rounded > best[0]:
            best = (rounded, dt)
    return today, best


def bet_dead_buckets(conn, wallet, city, cfg, today, obs_max, obs_time, now, events_cache, why):
    """Против каждого варианта ниже уже замеренного максимума, пока рынок за него ещё платит ≥ MIN_BID."""
    # Уже решённые сегодня бакеты этого кошелька (купили или стакан был пуст) — не трогаем повторно.
    seen = {row[0] for row in conn.execute(
        "SELECT bucket_lo FROM paper_obs_trades WHERE wallet = ? AND city = ? AND local_date = ?",
        (wallet, city, today.isoformat()))}
    slug = f"highest-temperature-in-{cfg['poly_slug']}-on-{month_day_year_slug(today)}"
    if slug not in events_cache:
        try:
            events_cache[slug] = requests.get(f"{GAMMA}/events", params={"slug": slug}, timeout=20).json()
        except requests.RequestException as e:
            print(f"{wallet}: {city} ошибка Polymarket — {e}", file=sys.stderr)
            return
    events = events_cache[slug]
    if not events:
        return
    for m in events[0]["markets"]:
        rng = parse_bucket(m["question"])
        if rng is None or rng[1] >= obs_max or m.get("closed") or rng[0] in seen:
            continue
        bid = m.get("bestBid")
        if bid in (None, "") or float(bid) < MIN_BID:
            continue
        bid = float(bid)
        if cash(conn, wallet) < STAKE:
            print(f"{wallet}: денег нет")
            break
        if trading_stopped():
            print(f"{wallet}: файл STOP — новые ставки не делаем")
            break
        try:
            ex = simulate_buy(m, json.loads(m["clobTokenIds"])[1], STAKE, 1 - MIN_BID)
        except (requests.RequestException, ValueError, KeyError) as e:
            print(f"{wallet}: {city} ошибка стакана — {e}", file=sys.stderr)
            continue
        filled = ex["cost"] >= MIN_FILL
        reason = None if filled else (
            f"по данным gamma рынок ещё давал {bid*100:.0f}¢ за уже невозможный вариант, но в живом стакане ставку против: "
            + (ex["reason"] or f"можно было купить меньше чем на ${MIN_FILL:.0f}"))
        conn.execute(
            """
            INSERT OR IGNORE INTO paper_obs_trades
            (wallet, city, local_date, unit, bucket_lo, bucket_hi, obs_max, obs_time_utc, placed_at, yes_bid,
             price, stake, status, reason, shares, fee, book_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (wallet, city, today.isoformat(), cfg["unit"], rng[0], rng[1], obs_max, obs_time.isoformat(),
             now.isoformat(), bid, ex["avg"], ex["cost"] if filled else 0.0, "open" if filled else "nofill",
             reason, ex["shares"] if filled else 0.0, ex["fee"] if filled else 0.0, ex["book"]),
        )
        conn.commit()
        if filled:
            print(f"{wallet}: {city} {today} {why} — бакет "
                  f"{rng[0]}..{rng[1]} ещё стоит {bid:.2f}; купили {ex['shares']:.1f} долей No по {ex['avg']:.3f} "
                  f"на ${ex['cost']:.2f} + комиссия ${ex['fee']:.3f}")
        else:
            print(f"{wallet}: {city} {today} {reason}")

if __name__ == "__main__":
    run()
