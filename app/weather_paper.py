"""
Виртуальный портфель по погоде: $100 виртуальных долларов, виртуальные
ставки по сигналам модели, расчёт по официальной резолюции Polymarket
(weather_poly_outcomes). Ничего не покупает — только пишет в sqlite.

Зачем (2026-09-22, просьба Alex): проценты попаданий трудно читать —
баланс в долларах сразу показывает, выигрываем мы или проигрываем и
на сколько.

Правила зафиксированы ДО первого результата (kill criterion — заранее,
не подгонять после):
- один раз в день на город: самый ранний снимок до 12:00 местного;
- бакет с максимальным edge (модель минус рынок);
- ставим, только если edge >= MIN_EDGE и цена в [MIN_PRICE, MAX_PRICE];
- с 2026-09-24 исполнение симулируется в polyexec.py (комиссия Polymarket,
  минимум 5 долей, задержка 2 с перед исполнением, выплата по цене
  закрытия доли, аварийная остановка файлом data/STOP); ставка $STAKE
  — вместе с комиссией;
- цена покупки — с 2026-09-23 по реальному стакану Yes (CLOB /book):
  покупаем от дешёвых заявок к дорогим, пока не потратим STAKE и пока
  цена не выше model_p - MIN_EDGE (дальше ставка уже невыгодна по
  нашим же правилам). Меньше MIN_FILL — ставки нет, строка 'nofill';
- ставка фиксированная STAKE долларов, при выигрыше бакет платит $1
  за каждую купленную долю, при проигрыше ставка сгорает;
- денег нет — ставок нет.

Три независимых кошелька — основная модель (GFS+ICON), EMOS и микс
~16 моделей (weather_multimodel.py, с 2026-09-23), чтобы
в долларах видеть, какая из них лучше. Запускается по крону после
weather_edge.py и weather_poly_resolve.py.
"""

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

import requests

from polyexec import final_price, maker_filled, place_limit, simulate_buy, trading_stopped, trades_since
from weather_edge import CITIES, GAMMA, month_day_year_slug, parse_bucket

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))

START_BALANCE = 100.0
# 2026-09-27 (решение Alex): повтору за сильными трейдерами — $300. Он покупает сразу по многим
# городам на 2 дня вперёд, и $100 кончались (39 ставок) — пропускал сигналы. Сравниваем кошельки
# по доходности в % от поставленного, а не в долларах.
START_BY_WALLET = {"copy": 300.0}


def start_balance(wallet):
    return START_BY_WALLET.get(wallet, START_BALANCE)
STAKE = 2.0  # 2026-09-25: было $5; при $5 и $100 на 48 городах кошельки упирались в деньги (решение Alex)
MIN_EDGE = 0.10
MIN_PRICE = 0.03  # дешевле — "лотерейные билеты", на истории почти не выигрывали
MAX_PRICE = 0.95
MIN_FILL = 1.0
CLOB = "https://clob.polymarket.com"
# Кошелёк стартует с этого момента: снимки раньше — не торгуем задним числом.
PAPER_START_TS = "2026-09-22T20:00:00+00:00"

WALLETS = {"main": "model_p", "emos": "emos_model_p", "mm": "mm_model_p",
           # 2026-09-25: обучаемая модель (weather_ml_live.py) — решает по
           # первому снимку с 08:00 местного, где у неё есть оценка
           "ml": "ml_model_p",
           # 2026-09-25: v2 — учит распределение (weather_ml_q.py), рядом с v1 для сравнения
           "ml2": "ml2_model_p",
           # 2026-09-25: v3 = v2 + мнение рынка в 08:00 (лучший вариант проверки пунктов 3-4)
           "ml3": "ml3_model_p",
           # 2026-09-25: v1, но только когда её центр не совпадает с рынком
           # (см. SHIFT_WALLETS) — отдельный кошелёк, сам ml не меняем
           "ml_shift": "ml_model_p",
           # 2026-09-26: смесь 35% v3 + 65% рынка, ставит при перевесе от 3 п.п. (EDGE_BY_WALLET)
           "ml3_cal": "ml3c_model_p",
           # 2026-09-26 (решение Alex): смесь, но размер ставки по перевесу (Келли ×0.25) — STAKE_BY_EDGE
           "ml3_cal_k": "ml3c_model_p",
           # 2026-09-26: v4 (v3 с 31 листом) — как ml3 и как смесь ml3_cal
           "ml4": "ml4_model_p", "ml4_cal": "ml4c_model_p",
           # 2026-09-27 (решение Alex, пункт 2): v4, усреднённая по 3 обучениям — как ml4 и ml4_cal
           "ml4e": "ml4e_model_p", "ml4e_cal": "ml4ec_model_p"}
# у этих кошельков решение — по первому снимку, где у модели ЕСТЬ оценка
# (обучаемая модель считает только с 08:00 местного, нужны утренние замеры)
NEEDS_FIELD = {"ml", "ml2", "ml3", "ml_shift", "ml3_mk", "ml3_cal", "ml3_no", "ml3_cal_k", "ml4", "ml4_cal", "ml4e", "ml4e_cal"}
# 2026-09-25: у v1 разброс постоянный (~1°C), и когда её центр совпадает с
# рынком, она завышает соседние варианты. На истории (цена первой сделки
# после решения, $2): такие ставки июль-авг -$100, сентябрь -$82; ставки при
# центре v1, сдвинутом от рынка на >=0.5°C: +$344 и +$51. Порог выбран на
# июле-августе, сентябрь — проверка. Центры — средние по вероятностям бакетов.
SHIFT_WALLETS = {"ml_shift": 0.5}  # °C
SHIFT_START_TS = "2026-09-25T15:00:00+00:00"
# свой старт у новых кошельков — задним числом не ставят
START_TS = {"ml_shift": SHIFT_START_TS, "ml3_mk": "2026-09-26T12:00:00+00:00", "ml3_cal": "2026-09-26T12:00:00+00:00",
            "ml3_no": "2026-09-26T12:00:00+00:00",
            "ml3_cal_k": "2026-09-26T15:00:00+00:00",
            "ml4": "2026-09-26T21:30:00+00:00", "ml4_cal": "2026-09-26T21:30:00+00:00",
            "ml4e": "2026-09-27T09:30:00+00:00", "ml4e_cal": "2026-09-27T09:30:00+00:00"}
# свой минимальный перевес: у смеси с рынком перевес меньше, но честный — порог 3 п.п.
# (на июле-августе +$499 на 1411 ставок по цене A; сентябрь по реальным сделкам +$85 на 177)
EDGE_BY_WALLET = {"ml3_cal": 0.03, "ml3_cal_k": 0.03, "ml4_cal": 0.03, "ml4e_cal": 0.03}
# ставка по перевесу: доля Келли f = (шанс − цена) / (1 − цена), берём ×0.25 от $100, в пределах $0.5-$10.
# Проверка (смесь, 3 п.п.): июль-авг по реальным сделкам как у $2 (−31% против −30% от вложенного),
# сентябрь +$107 против +$89 при меньших вложениях. Слабый плюс — отдельный кошелёк для живой проверки.
STAKE_BY_EDGE = {"ml3_cal_k": (0.25, 100.0, 0.5, 10.0)}  # множитель, банк, мин, макс
# 2026-09-26: ставки «против» — покупка доли «нет» на вариант, который главная модель считает
# переоценённым (перевес ≥10 п.п.). Проверка: июль-авг по реальным сделкам +$48 (137 ставок),
# сентябрь +$68 (337 ставок, +10% от вложенного) — почти независимо от ставок «да».
NO_WALLETS = {"ml3_no": "ml3_model_p"}
# 2026-09-26: кошельки обучаемых моделей берут и быстрые снимки (weather_ml_fast.py, таблица
# snapshots_fast — решение в 08:00-08:30 местного вместо 08:00-10:00). Выбирается САМЫЙ РАННИЙ
# снимок дня из обеих таблиц; нет быстрого — как раньше, по обычному.
FAST_WALLETS = {"ml", "ml2", "ml3", "ml_shift", "ml3_mk", "ml3_cal", "ml3_no", "ml3_cal_k", "ml4", "ml4_cal", "ml4e", "ml4e_cal"}
# 2026-09-24: двойники тех же трёх моделей с теми же сигналами, но
# покупают СВОЕЙ заявкой (без комиссии, по нижней цене стакана) — чтобы
# сравнить с покупкой по чужим заявкам на одних и тех же днях.
MAKER_WALLETS = {"main_mk": "model_p", "emos_mk": "emos_model_p", "mm_mk": "mm_model_p",
                 # 2026-09-26 (решение Alex): тот же сигнал главной модели, но своей заявкой —
                 # у v3 много дешёвых вариантов с тонким стаканом, где комиссия заметна
                 "ml3_mk": "ml3_model_p"}
# Основная модель без поправки на смещение по городу — "сырой" GFS+ICON,
# известный своим перекосом. 2026-09-24: после подключения 48 городов
# она сразу ставила в новых городах без поправки и проигрывала
# (Гуанчжоу: модель 47% против рынка 4%). Теперь ставит только там, где
# поправка уже работает (MIN_BIAS_N дней истории), как EMOS/микс,
# которые без истории и так не ставят.
BIAS_REQUIRED = {"main", "main_mk"}
ML_START_TS = "2026-09-24T22:00:00+00:00"
MAKER_CUTOFF_HOUR = 12  # не исполнилась к полудню по местному времени — снимаем
MAKER_START_TS = "2026-09-23T21:38:28+00:00"  # двойники стартуют с момента запуска, задним числом не ставят


def ensure_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            city TEXT NOT NULL,
            local_date TEXT NOT NULL,
            snapshot_ts TEXT NOT NULL,
            unit TEXT,
            bucket_lo REAL,
            bucket_hi REAL,
            model_p REAL,
            market_p REAL,
            price REAL,
            stake REAL,
            status TEXT NOT NULL DEFAULT 'open',
            payout REAL,
            settled_at TEXT,
            UNIQUE (wallet, city, local_date)
        )
        """
    )
    cols = [r[1] for r in conn.execute("PRAGMA table_info(paper_trades)")]
    if "reason" not in cols:
        # 2026-09-23 (просьба Alex): на странице объясняем, почему в какой-то
        # день ставки не было. Статусы без ставки: 'skip' (нет сигнала),
        # 'nofill' (сигнал есть, но купить не по чем).
        conn.execute("ALTER TABLE paper_trades ADD COLUMN reason TEXT")
    if "limit_price" not in cols:
        # своя заявка (MAKER_WALLETS): статус 'resting', пока ждёт продавца
        for col, typ in (("limit_price", "REAL"), ("want_shares", "REAL"), ("queue_ahead", "REAL"),
                         ("condition_id", "TEXT"), ("token_id", "TEXT"), ("placed_at", "TEXT")):
            conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {col} {typ}")
    if "side" not in cols:
        # 2026-09-26: сторона ставки — 'yes' (по умолчанию) или 'no' (кошельки NO_WALLETS)
        conn.execute("ALTER TABLE paper_trades ADD COLUMN side TEXT DEFAULT 'yes'")
    if "shares" not in cols:
        # 2026-09-24: исполнение через polyexec — доли, комиссия, стакан.
        # Старым сделкам доли и комиссию досчитываем (комиссия на погоде
        # действовала и тогда: rate 0.05, exponent 1).
        conn.execute("ALTER TABLE paper_trades ADD COLUMN shares REAL")
        conn.execute("ALTER TABLE paper_trades ADD COLUMN fee REAL DEFAULT 0")
        conn.execute("ALTER TABLE paper_trades ADD COLUMN book_json TEXT")
        conn.execute(
            """UPDATE paper_trades SET shares = stake / price, fee = (stake / price) * 0.05 * price * (1 - price)
               WHERE stake > 0 AND price > 0"""
        )
        # покупка (stake) у старых сделок не меняется, комиссия — сверху;
        # выплата выигравших = доли × 1 (раньше stake/price — то же самое)
    conn.commit()


def fmt_bucket(lo, hi, unit):
    sym = "°F" if unit == "fahrenheit" else "°C"
    if lo <= -900:
        return f"≤{hi - 0.5:.0f}{sym}"
    if hi >= 900:
        return f"≥{lo + 0.5:.0f}{sym}"
    a, b = lo + 0.5, hi - 0.5
    return f"{a:.0f}{sym}" if a == b else f"{a:.0f}–{b:.0f}{sym}"


def cents(p):
    return "меньше 1¢" if p < 0.01 else f"{p*100:.0f}¢"


def center_c(buckets, field, unit):
    """Средняя температура по вероятностям бакетов (крайние — +-0.5 от границы), в °C."""
    tot = num = 0.0
    for b in buckets:
        lo, hi, p = b["bucket_lo"], b["bucket_hi"], b[field] or 0.0
        x = hi - 0.5 if lo <= -900 else (lo + 0.5 if hi >= 900 else (lo + hi) / 2)
        tot += p
        num += x * p
    mean = num / tot
    return (mean - 32) * 5 / 9 if unit == "fahrenheit" else mean


def record_skip(conn, wallet, city, local_date, ts, status, reason):
    conn.execute(
        "INSERT OR IGNORE INTO paper_trades (wallet, city, local_date, snapshot_ts, stake, status, reason) VALUES (?, ?, ?, ?, 0, ?, ?)",
        (wallet, city, local_date, ts, status, reason),
    )
    conn.commit()


def cash(conn, wallet):
    """Свободные деньги: старт - (покупки + комиссии) + выплаты по закрытым."""
    spent, back = conn.execute(
        "SELECT COALESCE(SUM(stake + COALESCE(fee, 0)), 0), COALESCE(SUM(COALESCE(payout, 0)), 0) FROM paper_trades WHERE wallet = ?",
        (wallet,),
    ).fetchone()
    return start_balance(wallet) - spent + back


def settle(conn, now):
    """Выплата = доли × цена закрытия нашей доли Yes (1 — выиграли, 0 —
    проиграли, 0.5 — маркет отменён)."""
    from datetime import date
    cache, n = {}, 0
    for r in conn.execute("SELECT * FROM paper_trades WHERE status = 'open' AND stake > 0").fetchall():
        slug = f"highest-temperature-in-{CITIES[r['city']]['poly_slug']}-on-{month_day_year_slug(date.fromisoformat(r['local_date']))}"
        try:
            side = (r["side"] if "side" in r.keys() else None) or "yes"
            fp = final_price(slug, (r["bucket_lo"], r["bucket_hi"]), side, parse_bucket, cache)
        except (requests.RequestException, ValueError, KeyError):
            continue
        if fp is None:
            continue
        status = "won" if fp >= 0.99 else ("lost" if fp <= 0.01 else "void")
        conn.execute("UPDATE paper_trades SET status = ?, payout = ?, settled_at = ? WHERE id = ?",
                     (status, r["shares"] * fp, now, r["id"]))
        n += 1
    conn.commit()
    return n


def buy_yes(city, local_date, lo, hi, max_price, token_index=0, budget=None):
    """Покупка доли бакета (0 — «да», 1 — «нет») — симуляция реального исполнения (polyexec)."""
    from datetime import date
    slug = f"highest-temperature-in-{CITIES[city]['poly_slug']}-on-{month_day_year_slug(date.fromisoformat(local_date))}"
    events = requests.get(f"{GAMMA}/events", params={"slug": slug}, timeout=20).json()
    for m in (events[0]["markets"] if events else []):
        if parse_bucket(m["question"]) == (lo, hi) and not m.get("closed") and m.get("acceptingOrders", True):
            return simulate_buy(m, json.loads(m["clobTokenIds"])[token_index], budget or STAKE, max_price)
    return {"shares": 0.0, "cost": 0.0, "fee": 0.0, "avg": None, "min_ask": None, "book": None,
            "reason": "маркет уже закрыт или не принимает заявки"}


def find_market(city, local_date, lo, hi):
    from datetime import date
    slug = f"highest-temperature-in-{CITIES[city]['poly_slug']}-on-{month_day_year_slug(date.fromisoformat(local_date))}"
    events = requests.get(f"{GAMMA}/events", params={"slug": slug}, timeout=20).json()
    for m in (events[0]["markets"] if events else []):
        if parse_bucket(m["question"]) == (lo, hi) and not m.get("closed") and m.get("acceptingOrders", True):
            return m
    return None


def cutoff_ts(city, local_date):
    from datetime import date
    from zoneinfo import ZoneInfo
    d = date.fromisoformat(local_date)
    return datetime(d.year, d.month, d.day, MAKER_CUTOFF_HOUR, tzinfo=ZoneInfo(CITIES[city]["tz"])).timestamp()


def update_resting(conn, now):
    """Проверяем, исполнились ли наши заявки по реальным сделкам; после
    полудня по местному времени — снимаем неисполненный остаток."""
    now_ts = datetime.now().timestamp()
    for r in conn.execute("SELECT * FROM paper_trades WHERE status = 'resting'").fetchall():
        placed_ts = datetime.fromisoformat(r["placed_at"]).timestamp()
        cut = cutoff_ts(r["city"], r["local_date"])
        try:
            trades = trades_since(r["condition_id"], placed_ts, min(now_ts, cut))
        except (requests.RequestException, ValueError, KeyError):
            continue
        filled = maker_filled(trades, r["limit_price"], r["queue_ahead"], r["want_shares"])
        done = filled >= r["want_shares"] - 1e-9 or now_ts >= cut
        if not done:
            conn.execute("UPDATE paper_trades SET shares = ? WHERE id = ?", (filled, r["id"]))
            continue
        label = fmt_bucket(r["bucket_lo"], r["bucket_hi"], r["unit"])
        if filled * r["limit_price"] >= MIN_FILL:
            conn.execute("UPDATE paper_trades SET status = 'open', shares = ?, stake = ?, fee = 0, price = ? WHERE id = ?",
                         (filled, filled * r["limit_price"], r["limit_price"], r["id"]))
            print(f"{r['wallet']}: {r['city']} {r['local_date']} заявка исполнилась: {filled:.1f} долей {label} по {r['limit_price']:.3f}")
        else:
            conn.execute("UPDATE paper_trades SET status = 'nofill', shares = 0, stake = 0, reason = ? WHERE id = ?",
                         (f"сигнал на {label} (модель {r['model_p']*100:.0f}%, рынок {r['market_p']*100:.0f}%): своя заявка "
                          f"по {r['limit_price']*100:.1f}¢ до {MAKER_CUTOFF_HOUR}:00 не исполнилась"
                          + (f" (купили бы всего {filled:.1f} долей)" if filled else " — никто не продал по этой цене"),
                          r["id"]))
    conn.commit()


def place(conn, now, only=None):
    placed = 0
    if trading_stopped():
        print("Файл STOP — новые ставки не делаем")
        return 0
    from weather_bias import compute_city_bias
    from weather_edge import BIAS_SINCE_TS
    calibrated = set(compute_city_bias(conn, BIAS_SINCE_TS))
    has_fast = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'snapshots_fast'").fetchone() is not None
    snap_cols = {r[1] for r in conn.execute("PRAGMA table_info(snapshots)")}
    fast_cols = {r[1] for r in conn.execute("PRAGMA table_info(snapshots_fast)")} if has_fast else set()
    for wallet, field in {**WALLETS, **MAKER_WALLETS, **NO_WALLETS}.items():
        if only is not None and wallet not in only:
            continue
        maker = wallet in MAKER_WALLETS
        no_side = wallet in NO_WALLETS
        if field not in snap_cols:
            # 2026-09-27: колонки новой модели ещё нет (появится со следующим снимком weather_edge) —
            # пропускаем только этот кошелёк, а не весь запуск (раньше падали все)
            print(f"{wallet}: в снимках ещё нет колонки {field} — пропускаем")
            continue
        for city in CITIES:
            days = conn.execute(
                """
                SELECT s.local_date, MIN(s.ts_utc) AS ts FROM snapshots s
                LEFT JOIN paper_trades t ON t.wallet = ? AND t.city = s.city AND t.local_date = s.local_date
                WHERE s.city = ? AND s.ts_utc >= ? AND s.local_hour < 12 AND t.id IS NULL
                """ + (f" AND s.{field} IS NOT NULL" if wallet in NEEDS_FIELD else "") + """
                GROUP BY s.local_date
                """,
                (wallet, city, START_TS.get(wallet) or
                 (ML_START_TS if wallet in NEEDS_FIELD else (MAKER_START_TS if maker else PAPER_START_TS))),
            ).fetchall()
            days = [{"local_date": d["local_date"], "ts": d["ts"], "table": "snapshots"} for d in days]
            if wallet in FAST_WALLETS and has_fast and field in fast_cols:
                fast = conn.execute(
                    f"""SELECT s.local_date, MIN(s.ts_utc) AS ts FROM snapshots_fast s
                        LEFT JOIN paper_trades t ON t.wallet = ? AND t.city = s.city AND t.local_date = s.local_date
                        WHERE s.city = ? AND s.ts_utc >= ? AND s.local_hour < 12 AND t.id IS NULL AND s.{field} IS NOT NULL
                        GROUP BY s.local_date""",
                    (wallet, city, START_TS.get(wallet) or ML_START_TS)).fetchall()
                by_date = {d["local_date"]: d for d in days}
                for f in fast:
                    cur = by_date.get(f["local_date"])
                    if cur is None or f["ts"] < cur["ts"]:
                        by_date[f["local_date"]] = {"local_date": f["local_date"], "ts": f["ts"], "table": "snapshots_fast"}
                days = sorted(by_date.values(), key=lambda x: x["local_date"])
            for d in days:
                if maker and datetime.now().timestamp() >= cutoff_ts(city, d["local_date"]):
                    continue  # полдень в городе уже прошёл — заявку ставить поздно
                buckets = conn.execute(
                    f"SELECT * FROM {d['table']} WHERE city = ? AND local_date = ? AND ts_utc = ?",
                    (city, d["local_date"], d["ts"]),
                ).fetchall()
                if wallet in BIAS_REQUIRED and city not in calibrated:
                    record_skip(conn, wallet, city, d["local_date"], d["ts"], "skip",
                                "у основной модели ещё нет поправки на ошибку по этому городу "
                                "(нужно 15 дней истории) — сырой прогноз не используем")
                    continue
                buckets = [b for b in buckets if b[field] is not None]
                if no_side:
                    # «нет» на бакет: шансы и цена — со стороны «нет»; дальше та же логика выбора
                    buckets = [dict(b, **{field: 1 - b[field], "market_p": 1 - b["market_p"]}) for b in buckets]
                if not buckets:
                    record_skip(conn, wallet, city, d["local_date"], d["ts"], "skip",
                                "в утреннем снимке у этой модели не было оценки (мало истории по городу или модель ещё не была подключена)")
                    continue
                if wallet in SHIFT_WALLETS:
                    unit = buckets[0]["unit"]
                    shift = center_c(buckets, field, unit) - center_c(buckets, "market_p", unit)
                    if abs(shift) < SHIFT_WALLETS[wallet]:
                        record_skip(conn, wallet, city, d["local_date"], d["ts"], "skip",
                                    f"центр модели совпадает с рынком (разница {abs(shift):.1f}°C, нужно от "
                                    f"{SHIFT_WALLETS[wallet]:.1f}°C) — в такие дни v1 на истории проигрывала")
                        continue
                b = max(buckets, key=lambda r: r[field] - r["market_p"])
                label = ("против " if no_side else "") + fmt_bucket(b["bucket_lo"], b["bucket_hi"], b["unit"])
                edge = b[field] - b["market_p"]
                min_edge = EDGE_BY_WALLET.get(wallet, MIN_EDGE)
                if edge < min_edge:
                    record_skip(conn, wallet, city, d["local_date"], d["ts"], "skip",
                                f"нет перевеса: лучший вариант {label} — модель {b[field]*100:.0f}%, "
                                f"рынок {b['market_p']*100:.0f}%, разница {edge*100:.0f} п.п. (нужно от {min_edge*100:.0f})")
                    continue
                if not (MIN_PRICE <= b["market_p"] <= MAX_PRICE):
                    record_skip(conn, wallet, city, d["local_date"], d["ts"], "skip",
                                f"лучший вариант {label} стоит {cents(b['market_p'])} — вне допустимых "
                                f"{MIN_PRICE*100:.0f}–{MAX_PRICE*100:.0f}¢ (слишком дешёвые почти никогда не выигрывают)")
                    continue
                stake = STAKE
                if wallet in STAKE_BY_EDGE:
                    mult, bank, lo_s, hi_s = STAKE_BY_EDGE[wallet]
                    stake = round(max(lo_s, min(hi_s, bank * mult * edge / (1 - b["market_p"]))), 2)
                if cash(conn, wallet) < stake:
                    record_skip(conn, wallet, city, d["local_date"], d["ts"], "skip", f"в кошельке меньше ${stake:.2f}")
                    continue
                max_price = min(MAX_PRICE, b[field] - min_edge)
                if maker:
                    try:
                        m = find_market(city, d["local_date"], b["bucket_lo"], b["bucket_hi"])
                        lim = place_limit(m, json.loads(m["clobTokenIds"])[0], STAKE, max_price) if m else None
                    except (requests.RequestException, ValueError, KeyError) as e:
                        print(f"{wallet}: {city} ошибка стакана — {e}")
                        continue
                    if lim is None or lim["reason"]:
                        record_skip(conn, wallet, city, d["local_date"], d["ts"], "nofill",
                                    f"сигнал на {label}, но " + (lim["reason"] if lim else "маркет уже закрыт"))
                        continue
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO paper_trades
                        (wallet, city, local_date, snapshot_ts, unit, bucket_lo, bucket_hi, model_p, market_p, price, stake,
                         status, shares, fee, book_json, limit_price, want_shares, queue_ahead, condition_id, token_id, placed_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'resting', 0, 0, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (wallet, city, d["local_date"], d["ts"], b["unit"], b["bucket_lo"], b["bucket_hi"], b[field],
                         b["market_p"], lim["limit"], lim["shares"] * lim["limit"], lim["book"], lim["limit"], lim["shares"], lim["queue_ahead"],
                         m["conditionId"], json.loads(m["clobTokenIds"])[0], datetime.now().astimezone().isoformat()),
                    )
                    conn.commit()
                    print(f"{wallet}: {city} {d['local_date']} своя заявка на {label} по {lim['limit']*100:.1f}¢, "
                          f"{lim['shares']:.1f} долей, в очереди перед нами {lim['queue_ahead']:.0f}")
                    continue
                try:
                    ex = buy_yes(city, d["local_date"], b["bucket_lo"], b["bucket_hi"], max_price, 1 if no_side else 0, stake)
                except (requests.RequestException, ValueError, KeyError) as e:
                    print(f"{wallet}: {city} ошибка стакана — {e}")
                    continue
                filled = ex["cost"] >= MIN_FILL
                reason = None if filled else (
                    f"сигнал на {label} (модель {b[field]*100:.0f}%, рынок {b['market_p']*100:.0f}%), но "
                    + (ex["reason"] or f"купить можно было меньше чем на ${MIN_FILL:.0f}"))
                conn.execute(
                    """
                    INSERT OR IGNORE INTO paper_trades
                    (wallet, city, local_date, snapshot_ts, unit, bucket_lo, bucket_hi, model_p, market_p, price, stake,
                     status, reason, shares, fee, book_json, side)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (wallet, city, d["local_date"], d["ts"], b["unit"], b["bucket_lo"], b["bucket_hi"],
                     b[field], b["market_p"], ex["avg"], ex["cost"] if filled else 0.0,
                     "open" if filled else "nofill", reason, ex["shares"] if filled else 0.0,
                     ex["fee"] if filled else 0.0, ex["book"], "no" if no_side else "yes"),
                )
                conn.commit()
                placed += filled
                if filled:
                    print(f"{wallet}: {city} {d['local_date']} купили {ex['shares']:.1f} долей {label} по {ex['avg']:.3f} "
                          f"на ${ex['cost']:.2f} + комиссия ${ex['fee']:.2f} (модель {b[field]:.2f}, рынок {b['market_p']:.2f})")
                else:
                    print(f"{wallet}: {city} {d['local_date']} не купили — {reason}")
    return placed


def run(only=None):
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    now = datetime.now().astimezone().isoformat()
    settled = settle(conn, now)
    update_resting(conn, now)
    placed = place(conn, now, only)
    for wallet in {**WALLETS, **MAKER_WALLETS, **NO_WALLETS}:
        if only is None or wallet in only:
            print(f"{wallet}: баланс ${cash(conn, wallet):.2f} (рассчитано {settled}, новых ставок {placed})")
    if only is None:  # быстрый запуск (weather_ml_fast) отмечается сам
        from jobmark import mark
        mark(conn, "weather_paper")
    conn.close()


if __name__ == "__main__":
    run()
