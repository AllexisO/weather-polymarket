"""
LLM-прогноз максимума дня каждый час (03.10, решение Alex; кошельки llm_gem и llm_ds; с 06.10 — llm_mix, смесь Gemini и LightGBM).

Идея Alex: LLM каждый час смотрит всё, что известно о дне (замеры аэропорта с утра, почасовой прогноз облаков /
дождя / ветра, прогнозы 13 погодных моделей на максимум, разбор синоптиков NWS, цены рынка) и называет итоговый
максимум дня и шанс каждого варианта. Сверка с фактом — её прошлые прогнозы и ошибки по этому городу показываются ей
в каждом запросе («память ошибок»; сама модель через OpenRouter не дообучается — так делает и коллега Alex, ~$5/мес).

Кошельки (одна идея — две LLM, чтобы было видно, какая лучше):
  llm_gem — google/gemini-3.8-flash (как у коллеги Alex);
  llm_ds  — deepseek/deepseek-v4-pro (дешевле, для сравнения).
Города: 5 городов США с самыми большими деньгами (Чикаго, Атланта, Остин, Майами, Даллас), каждый час 08-19 местного
(04.10-06.10 — с 05:05; с 06.10 — с 08:05, решение Alex: минус 20% расхода, ставки и так с 08:05).
Ставка: одна на город в день — в первый час, когда шанс LLM выше цены продавца на EDGE (за «да» или за «нет»),
цена 10-90¢, $2, исполнение по настоящему стакану с комиссией (polyexec.simulate_buy). Итог — weather_paper.settle.
Расход: OpenRouter возвращает цену каждого запроса (llm_hour_preds.cost); дошёл до MONTH_LIMIT за месяц — кошелёк
стоит до следующего месяца (видно в логе и на /status). На самом ключе ещё лимит $10.

Крон: 5 * * * * (в :05 — к этому времени вышли сводки :51-:53 часа). Порог решения — docs/PRD.md §9 п.16.
"""

import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from jobmark import item_guard
from polyexec import simulate_buy, trading_stopped
from weather_cities import OBS_CITIES
from weather_edge import GAMMA, month_day_year_slug, parse_bucket
from weather_paper import cash

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
# 03.10: llm_gem — Gemini 3.8 Flash, как у коллеги Alex (первый час 03.10 15:08 UTC — 2.5 Flash; порог считается с 04.10)
WALLETS = {"llm_gem": "google/gemini-3.8-flash", "llm_ds": "deepseek/deepseek-v4-pro"}
MONTH_LIMIT = {"llm_gem": 8.5, "llm_ds": 1.5}   # $ в месяц на кошелёк, вместе ≤ $8.2 (04.10: Gemini $6, затем $7 под запуск с 05:05 — решения Alex)
# 03.10: DeepSeek рассуждает перед ответом — запрос до $0.007 и 70-110 с (effort low — всё ещё $0.003); предел рассуждения
# 800 токенов он не соблюдает (первые 18 запросов: в среднем $0.005, до $0.02 и 11 тыс. токенов, ~$9/мес) → без рассуждения
# Gemini 3.8 Flash тоже рассуждает: без предела $0.005 за запрос (~$9/мес), с пределом $0.002 (~$3.6/мес)
EXTRA = {"llm_ds": {"reasoning": {"enabled": False}}, "llm_gem": {"reasoning": {"max_tokens": 800}}}
CITIES = ("chicago", "atlanta", "austin", "miami", "dallas", "london")
THRESHOLD_CITIES = ("chicago", "atlanta", "austin", "miami", "dallas")   # порог PRD §9 п.16 — по 5 городам США; Лондон (°C) с 04.10 — отдельно
HOURS = range(8, 20)    # местное время, в которое спрашиваем (04.10: с 05:05; 06.10, решение Alex: с 08:05 — минус 20% расхода)
BET_FROM = 8            # ставки — как раньше, с 08:05 (ранние часы — только прогноз и учёба, правило ставок не меняется)
EDGE = 0.05             # шанс LLM выше цены продавца минимум на 5 п.п.
MIN_PRICE, MAX_PRICE = 0.10, 0.90
STAKE = 2.0
MEMORY_DAYS = 7         # сколько прошлых дней прогнозов показывать LLM
OR_URL = "https://openrouter.ai/api/v1/chat/completions"
AWC = "https://aviationweather.gov/api/data/metar"
OM = "https://api.open-meteo.com/v1/forecast"

SCHEMA = """CREATE TABLE IF NOT EXISTS llm_hour_preds (wallet TEXT, city TEXT, local_date TEXT, local_hour INTEGER,
    ts_utc TEXT, model TEXT, pred_max REAL, probs_json TEXT, market_json TEXT, max_so_far REAL, reason TEXT,
    cost REAL, tokens INTEGER, PRIMARY KEY (wallet, city, local_date, local_hour))"""
# 04.10 (просьба Alex «видеть, что мы ей дали и что она поняла»): письмо целиком, ответ целиком, прогноз по часам, вывод из ошибок
EXTRA_COLS = (("prompt", "TEXT"), ("answer_json", "TEXT"), ("hourly_json", "TEXT"), ("lesson", "TEXT"), ("reflection", "TEXT"),
              ("probs_raw_json", "TEXT"))   # 06.10: шансы как ответила LLM; probs_json — после поправки по проверке уверенности (по ним ставка)
# 04.10 (схема Alex «учится по каждому часу»): каждый час LLM разбирает прошлый час (сказала X на этот час — было Y, где
# ошиблась) и ведёт свою тетрадь правил по городу — тетрадь переходит из часа в час и из дня в день (llm_notebook).
NOTEBOOK_SQL = """CREATE TABLE IF NOT EXISTS llm_notebook (wallet TEXT, city TEXT, ts_utc TEXT, local_date TEXT, local_hour INTEGER,
    notes_json TEXT, PRIMARY KEY (wallet, city, local_date, local_hour))"""
NOTEBOOK_MAX = 12       # правил в тетради, не больше
HOUR_ERR_HOURS = 30     # разбор прогнозов «на час вперёд» за последние ~сутки


def ensure_schema(conn):
    conn.execute(SCHEMA)
    conn.execute(NOTEBOOK_SQL)
    have = {r[1] for r in conn.execute("PRAGMA table_info(llm_hour_preds)")}
    for col, typ in EXTRA_COLS:
        if col not in have:
            conn.execute(f"ALTER TABLE llm_hour_preds ADD COLUMN {col} {typ}")
    conn.commit()

SYSTEM = """You are an expert weather forecaster for one airport weather station, learning hour by hour, and trading
Polymarket "highest temperature today" markets. The market resolves on the official airport METAR maximum for the local
calendar day, in whole degrees of the market unit (°F in the US, °C elsewhere — the unit is given in the data).
Every hour you work in a loop:
1. REVIEW: compare what you predicted for the last hour(s) with the actual METAR temperature. Find where you went wrong
   and why (sun angle, sunset cooling, clouds arriving, rain, wind shift, dew point, sea breeze, front, station location).
2. NOTEBOOK: you keep a notebook of short rules about THIS station that you learned from your own errors. Keep rules that
   still hold, fix wrong ones, add new ones. The notebook is your memory: it is shown to you every hour and every day.
3. FORECAST: using everything (current observations, sun position, hourly model forecast, model daily maxima, NWS note,
   the LightGBM forecast, your notebook, your recent and systematic errors), predict the temperature for every remaining
   hour of today, and today's max. LightGBM is our model trained on 16 months of data: treat it as a strong starting point
   and move away from it only when today's observations give a reason.
4. CHANCES AND MONEY: your chances by bucket decide bets ($2 when your chance beats the price by 5+ points). You see your
   settled bets with results in dollars and how often your chances came true. Learn from both: which bets lose money, and
   whether you are overconfident. Your goal is accurate chances that make money over many days, not one lucky bet.
The final max can never be below the max already observed today.
Reply ONLY with JSON: {
"reflection": "<in Russian, 1-2 sentences: what you predicted for the last hour vs actual, where you went wrong and why;
if there is nothing to compare yet, say so>",
"notebook": ["<in Russian, short rule learned about this station>", ... at most 12 rules, the full updated list],
"hourly_f": {"<HH>": <temperature in the market unit at that local hour>, ... for every remaining hour of today up to 23},
"pred_max_f": <today's max in the MARKET UNIT (°C for °C markets despite the key name)>,
"probs": {"<bucket label exactly as given>": <probability 0-1>, ...},
"lesson": "<in Russian, one sentence: the main thing your errors taught you and how you adjust this forecast>",
"reason": "<in Russian, one short sentence: why this max>"}. Probabilities must cover every bucket and sum to 1."""


def local_now(city, now):
    return now.astimezone(ZoneInfo(OBS_CITIES[city]["tz"]))


def label(lo, hi, unit="fahrenheit"):
    u = "°F" if unit == "fahrenheit" else "°C"
    if lo <= -900:
        return f"{hi - 0.5:.0f}{u} or below"
    if hi >= 900:
        return f"{lo + 0.5:.0f}{u} or higher"
    if hi - lo <= 1.01:
        return f"{lo + 0.5:.0f}{u}"
    return f"{lo + 0.5:.0f}-{hi - 0.5:.0f}{u}"


def to_unit(temp_c, unit):
    return temp_c * 9 / 5 + 32 if unit == "fahrenheit" else temp_c


def markets(city, day):
    slug = f"highest-temperature-in-{OBS_CITIES[city]['poly_slug']}-on-{month_day_year_slug(day)}"
    ev = requests.get(f"{GAMMA}/events", params={"slug": slug}, timeout=20).json()
    out = []
    for m in (ev[0]["markets"] if ev else []):
        rng = parse_bucket(m["question"])
        if rng is None or m.get("closed"):
            continue
        try:
            yes = float(json.loads(m["outcomePrices"])[0])
        except (ValueError, KeyError, TypeError, IndexError):
            continue
        out.append({"lo": rng[0], "hi": rng[1], "label": label(*rng, OBS_CITIES[city]["unit"]), "price": yes,
                    "ask": float(m.get("bestAsk") or yes), "bid": float(m.get("bestBid") or yes), "m": m})
    return sorted(out, key=lambda x: x["lo"])


def metars(now):
    """Сводки 5 аэропортов за последние 30 ч (один запрос): сегодняшние — в письмо, вчерашний вечер — для разбора прогнозов.
    05.10: при обрыве сети падал с трассировкой — теперь две повторные попытки, потом понятная ошибка (без сводок
    не знаем уже достигнутый максимум — ставить нельзя, час пропускается)."""
    ids = ",".join(OBS_CITIES[c]["icao"] for c in CITIES)
    for wait in (10, 30, None):
        try:
            return requests.get(AWC, params={"ids": ids, "hours": HOUR_ERR_HOURS, "format": "json"}, timeout=20).json()
        except (requests.RequestException, ValueError) as e:
            if wait is None:
                sys.exit(f"сводки METAR (aviationweather.gov) недоступны — час пропущен ({type(e).__name__}: {str(e)[:150]})")
            time.sleep(wait)


def hourly(city):
    """Почасовой прогноз на сегодня. 03.10: Open-Meteo иногда отвечает ошибкой без 'hourly' (предел запросов в минуту, когда
    в ту же минуту работают другие скрипты) — две повторные попытки с паузой, потом без прогноза (LLM видит остальное)."""
    for wait in (10, 30, None):
        try:
            return _hourly(city)
        except (requests.RequestException, KeyError, ValueError) as e:
            if wait is None:
                print(f"{city}: почасовой прогноз Open-Meteo недоступен ({type(e).__name__}: {e}) — спрашиваем без него", flush=True)
                return [], {}
            time.sleep(wait)


def _hourly(city):
    c = OBS_CITIES[city]
    r = requests.get(OM, params={"latitude": c["lat"], "longitude": c["lon"], "timezone": c["tz"], "forecast_days": 1,
                                 "temperature_unit": c["unit"], "wind_speed_unit": "kn",
                                 "hourly": "temperature_2m,dew_point_2m,cloud_cover,precipitation_probability,precipitation,wind_speed_10m,wind_direction_10m,shortwave_radiation",
                                 "daily": "sunrise,sunset"},
                     timeout=20).json()
    if "hourly" not in r:
        raise KeyError(f"нет 'hourly': {str(r.get('reason') or r)[:120]}")
    day = {k: (v[0] if v else None) for k, v in (r.get("daily") or {}).items()}
    r = r["hourly"]
    return [{k: r[k][i] for k in r} for i in range(len(r["time"]))], day


def memory(conn, wallet, city, day):
    """Прошлые прогнозы LLM по городу и факт — для «учёта ошибок»."""
    rows = conn.execute("""SELECT p.local_date, p.local_hour, p.pred_max, d.actual_max FROM llm_hour_preds p
        JOIN weather_station_daily d ON d.city = p.city AND d.local_date = p.local_date
        WHERE p.wallet = ? AND p.city = ? AND p.local_date < ? AND p.local_date >= ? ORDER BY p.local_date, p.local_hour""",
                        (wallet, city, day.isoformat(), (day - timedelta(days=MEMORY_DAYS)).isoformat())).fetchall()
    if not rows:
        return "No past forecasts yet."
    # 06.10: коротко — первый прогноз дня, около полудня и последний; средние ошибки — в блоке систематических ошибок (bias_stats)
    by_day = {}
    for d, h, p, a in rows:
        if p is not None:
            by_day.setdefault((d, a), []).append((h, p))
    lines = []
    for (d, a), v in by_day.items():
        noon = min(v, key=lambda x: abs(x[0] - 12))
        pick = sorted({v[0], noon, v[-1]})
        lines.append(f"{d}: actual max {a:.0f}; you said " + ", ".join(f"{p:.0f} at {h:02d}h" for h, p in pick))
    return "\n".join(lines)


def fact_by_hour(obs, icao, tz, unit="fahrenheit"):
    """Факт по часам местного времени из сводок: замер :51-:53 относится к следующему целому часу. {(дата, час): в единицах маркета}"""
    out = {}
    for m in obs:
        if m["icaoId"] == icao and m.get("temp") is not None:
            t = (datetime.fromtimestamp(m["obsTime"], timezone.utc) + timedelta(minutes=10)).astimezone(tz)
            out[(t.date().isoformat(), t.hour)] = round(to_unit(m["temp"], unit), 1)
    return out


def hour_review(conn, wallet, city, now, obs):
    """Прогнозы LLM «на час вперёд» за последние сутки против факта — каждый час она разбирает именно это."""
    c = OBS_CITIES[city]
    tz = ZoneInfo(c["tz"])
    fact = fact_by_hour(obs, c["icao"], tz, c["unit"])
    u = "°F" if c["unit"] == "fahrenheit" else "°C"
    since = (now - timedelta(hours=HOUR_ERR_HOURS)).isoformat()
    rows = conn.execute("""SELECT local_date, local_hour, hourly_json, reflection FROM llm_hour_preds WHERE wallet = ? AND city = ?
                           AND ts_utc >= ? AND hourly_json IS NOT NULL ORDER BY ts_utc""", (wallet, city, since)).fetchall()
    lines, errs = [], []
    for d, h, hj, _refl in rows:
        fc = json.loads(hj or "{}")
        tgt = h + 1
        f, a = fc.get(f"{tgt:02d}"), fact.get((d, tgt))
        if f is None or a is None:
            continue
        errs.append(f - a)
        lines.append(f"{d} at {h:02d}:05 you predicted {tgt:02d}:00 = {f:.1f}{u}, actual {a:.1f}{u} (error {f - a:+.1f})")
    if not lines:
        return "No hour-ahead forecasts to compare yet."
    lines.append(f"Mean hour-ahead error over these hours: {sum(errs) / len(errs):+.1f}{u}, mean absolute {sum(abs(e) for e in errs) / len(errs):.1f}{u}")
    return "\n".join(lines[-14:] if len(lines) > 15 else lines)


def notebook(conn, wallet, city):
    r = conn.execute("SELECT notes_json FROM llm_notebook WHERE wallet = ? AND city = ? ORDER BY ts_utc DESC LIMIT 1", (wallet, city)).fetchone()
    return json.loads(r[0]) if r and r[0] else []


# 06.10 (решение Alex «чтобы она обучалась и ставки были прибыльные»): обучение v2 — то, что LLM сама не удержит в тетради
# (12 правил, 7 дней), считает код по всей истории и подаёт готовым: прогноз LightGBM, её систематические ошибки,
# итоги её ставок в деньгах и насколько её «70%» сбываются; шансы перед ставкой поправляются по этой проверке.
V2_FROM = "2026-10-06T09:45:00+00:00"   # с этого запуска — обучение v2 (граница «до/после» на /llm)
CAL_BINS = (0.0, 0.05, 0.2, 0.4, 0.6, 0.8, 0.95, 1.01)
CAL_K = 30              # поправка по проверке уверенности — с весом n/(n+K): на малой истории почти не трогаем
BIAS_BLOCKS = ((5, 9, "05-09"), (10, 13, "10-13"), (14, 19, "14-19"))


def lgbm_rows(conn, city, day):
    """Шансы главной модели v3 (LightGBM) по вариантам — первый быстрый снимок дня (weather_ml_fast, 08:00 местного): [(lo, hi, шанс)]."""
    try:
        rows = conn.execute("""SELECT bucket_lo, bucket_hi, ml3_model_p FROM snapshots_fast WHERE city = ? AND local_date = ?
                               AND ts_utc = (SELECT MIN(ts_utc) FROM snapshots_fast WHERE city = ? AND local_date = ?)""",
                            (city, day.isoformat(), city, day.isoformat())).fetchall()
    except sqlite3.OperationalError:
        return []
    return sorted((lo, hi, p) for lo, hi, p in rows if p is not None)


def lgbm_forecast(conn, city, day):
    """Утренний прогноз LightGBM строкой для письма."""
    c = OBS_CITIES[city]
    u = "°F" if c["unit"] == "fahrenheit" else "°C"
    bk = lgbm_rows(conn, city, day)
    s = sum(p for *_, p in bk)
    if not s:
        return "LightGBM v3 forecast: not yet (it is made once a day at 08:00 local)."
    cum, med = 0.0, None
    for lo, hi, p in bk:
        cum += p / s
        if cum >= 0.5:
            med = label(lo, hi, c["unit"])
            break
    parts = [f"{label(lo, hi, c['unit'])} {100 * p / s:.0f}%" for lo, hi, p in bk if p / s >= 0.03]
    return (f"LightGBM v3 forecast (our trained model, made at 08:00 local; in the morning it is at least as accurate as you): "
            f"most likely around {med}; chances by bucket ({u}): " + ", ".join(parts))


def bias_stats(conn, wallet, city, day):
    """Её систематическая ошибка максимума дня по всей истории (не только 7 дней): по городу и по часам дня, и по всем городам."""
    rows = conn.execute("""SELECT p.city, p.local_hour, p.pred_max - d.actual_max FROM llm_hour_preds p
        JOIN weather_station_daily d ON d.city = p.city AND d.local_date = p.local_date
        WHERE p.wallet = ? AND p.local_date < ? AND p.pred_max IS NOT NULL AND d.actual_max IS NOT NULL""",
                        (wallet, day.isoformat())).fetchall()
    here = [(h, e) for c, h, e in rows if c == city]
    if not here:
        return "Not enough history yet."
    out = []
    for lo, hi, name in BIAS_BLOCKS:
        es = [e for h, e in here if lo <= h <= hi]
        if es:
            out.append(f"at {name} local: mean error {sum(es) / len(es):+.1f}, mean absolute {sum(abs(e) for e in es) / len(es):.1f} (n={len(es)})")
    days = conn.execute("""SELECT COUNT(DISTINCT p.local_date) FROM llm_hour_preds p JOIN weather_station_daily d
                           ON d.city = p.city AND d.local_date = p.local_date WHERE p.wallet = ? AND p.city = ? AND p.local_date < ?""",
                        (wallet, city, day.isoformat())).fetchone()[0]
    out.insert(0, f"This city, all {days} past days (error = your daily-max forecast minus actual; + means you were too warm):")
    alle = [e for _c, _h, e in rows]
    out.append(f"All your cities together: mean error {sum(alle) / len(alle):+.1f} (n={len(alle)}). "
               "Correct a clear bias, but a few days are weak evidence.")
    return "\n".join(out)


def _pnl(r):
    return (r["payout"] or 0) - r["stake"] - (r["fee"] or 0)


def bet_summary(conn, wallet, city):
    """Её ставки и итоги в деньгах — чтобы училась не только температуре, но и тому, какие ставки приносят деньги."""
    conn.row_factory = sqlite3.Row
    try:
        rs = conn.execute("""SELECT * FROM paper_trades WHERE wallet = ? AND status IN ('won', 'lost', 'void') ORDER BY settled_at""",
                          (wallet,)).fetchall()
        op = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE wallet = ? AND status = 'open'", (wallet,)).fetchone()[0]
    finally:
        conn.row_factory = None
    if not rs:
        return "No settled bets yet."

    def line(name, xs):
        if not xs:
            return None
        pnl, cost = sum(_pnl(r) for r in xs), sum(r["stake"] + (r["fee"] or 0) for r in xs)
        return f"{name}: {len(xs)} bets, won {sum(r['status'] == 'won' for r in xs)}, result {pnl:+.2f}$ ({100 * pnl / cost:+.0f}% of money spent)"
    side = lambda r: r["side"] or "yes"
    out = [line("ALL your settled bets (all cities)", rs) + f"; {op} still open",
           line('  buying "yes"', [r for r in rs if side(r) == "yes"]), line('  buying "no"', [r for r in rs if side(r) == "no"]),
           line("  price below 30c", [r for r in rs if r["price"] < 0.30]), line("  price 30-70c", [r for r in rs if 0.30 <= r["price"] <= 0.70]),
           line("  price above 70c", [r for r in rs if r["price"] > 0.70]), line(f"  this city", [r for r in rs if r["city"] == city])]
    out.append("Last settled bets (date, city, what you bought, price, your chance, result):")
    u = lambda r: "°F" if r["unit"] == "fahrenheit" else "°C"
    for r in rs[-8:]:
        out.append(f"  {r['local_date']} {r['city']}: \"{side(r)}\" {label(r['bucket_lo'], r['bucket_hi'], r['unit'] or 'fahrenheit')} "
                   f"at {r['price'] * 100:.0f}c, your chance {(r['model_p'] or 0) * 100:.0f}% -> {r['status']} {_pnl(r):+.2f}$")
    if len(rs) < 30:
        out.append(f"Only {len(rs)} settled bets: luck dominates. Learn patterns, but do not write absolute rules from a few bets.")
    return "\n".join(x for x in out if x)


def calibration(conn, wallet, day):
    """Насколько сбываются её шансы: по всем её прогнозам 08-19 местного за прошлые дни; уже невозможные варианты не в счёт.
    → [(от, до, сколько, средний сказанный шанс, как часто сбылось)]."""
    rows = conn.execute("""SELECT p.probs_json, p.max_so_far, d.actual_max FROM llm_hour_preds p
        JOIN weather_station_daily d ON d.city = p.city AND d.local_date = p.local_date
        WHERE p.wallet = ? AND p.local_date < ? AND p.local_hour BETWEEN 8 AND 19 AND p.probs_json IS NOT NULL
        AND d.actual_max IS NOT NULL""", (wallet, day.isoformat())).fetchall()
    st = [[0, 0.0, 0] for _ in CAL_BINS[:-1]]
    for pj, mx, a in rows:
        for lab, p in (json.loads(pj) or {}).items():
            rng = label_range(lab)
            if rng is None or (mx is not None and rng[1] < mx):
                continue
            i = next(k for k in range(len(CAL_BINS) - 1) if CAL_BINS[k] <= p < CAL_BINS[k + 1])
            st[i][0] += 1
            st[i][1] += p
            st[i][2] += rng[0] <= a < rng[1]
    return [(CAL_BINS[i], CAL_BINS[i + 1], n, s / n, h / n) for i, (n, s, h) in enumerate(st) if n]


def calibration_text(cal):
    if not cal:
        return "Not enough history yet."
    return ("\n".join(f"when you said {lo * 100:.0f}-{min(hi, 1) * 100:.0f}% (on average {said * 100:.0f}%): it happened {got * 100:.0f}% of the time (n={n})"
                      for lo, hi, n, said, got in cal)
            + "\nIf you say 80% and it happens 40%, you are overconfident — spread your chances wider. Before betting, the code also "
              "corrects your chances by this table.")


def calibrate(probs, cal, mk, mx):
    """Поправка шансов по проверке уверенности: сдвиг к тому, как часто сбывалось, с весом n/(n+CAL_K); невозможные — 0, сумма — 1."""
    if not probs or not cal:
        return probs
    out = {}
    for b in mk:
        p = probs.get(b["label"], 0.0)
        if mx is not None and b["hi"] < mx:
            out[b["label"]] = 0.0
            continue
        row = next((r for r in cal if r[0] <= p < r[1]), None)
        if row:
            _lo, _hi, n, said, got = row
            p = p + (got - said) * n / (n + CAL_K)
        out[b["label"]] = min(1.0, max(0.0, p))
    s = sum(out.values())
    return {k: v / s for k, v in out.items()} if s > 0 else probs


# 06.10 (идея Alex «объединить LLM и LightGBM»): кошелёк llm_mix — без своих запросов к LLM. Берёт шансы Gemini этого часа
# (после поправки уверенности) и утренние шансы LightGBM v3, смешивает поровну и ставит по тем же правилам, что LLM.
# Веса 50/50 — без подбора (утром обе модели примерно равны: 44% и 42% на верный вариант); подбор по истории — когда она будет.
MIX_WALLET, MIX_FROM, MIX_W = "llm_mix", "llm_gem", 0.5   # MIX_W — доля LLM


def mix_probs(conn, city, day, mk, mx, probs):
    """Смесь шансов LLM и LightGBM по вариантам маркета; LightGBM ещё нет (до 08:00) — None."""
    lg = {(lo, hi): p for lo, hi, p in lgbm_rows(conn, city, day)}
    s = sum(lg.get((b["lo"], b["hi"]), 0.0) for b in mk)
    if not probs or not s:
        return None
    out = {}
    for b in mk:
        v = MIX_W * probs.get(b["label"], 0.0) + (1 - MIX_W) * lg.get((b["lo"], b["hi"]), 0.0) / s
        out[b["label"]] = 0.0 if mx is not None and b["hi"] < mx else v
    t = sum(out.values())
    return {k: v / t for k, v in out.items()} if t > 0 else None


def label_range(lab):
    """Обратно к label(): «66-67°F» → (65.5, 67.5), «69°F or below» → (-999, 69.5), «22°C» → (21.5, 22.5)."""
    m = RE_LABEL.match(lab.strip())
    if not m:
        return None
    a, b, tail = float(m.group(1)), m.group(2), m.group(3)
    if tail == " or below":
        return (-999.0, a + 0.5)
    if tail == " or higher":
        return (a - 0.5, 999.0)
    return (a - 0.5, (float(b) if b else a) + 0.5)


RE_LABEL = re.compile(r"^(-?\d+)(?:-(-?\d+))?°[FC]( or below| or higher)?$")


def prompt(conn, wallet, city, day, lnow, obs, hrs, mk, sun=None, now=None, cal=None):
    c = OBS_CITIES[city]
    tz = ZoneInfo(c["tz"])
    o_lines, mx = [], None
    u = "°F" if c["unit"] == "fahrenheit" else "°C"
    for m in sorted(obs, key=lambda x: x["obsTime"]):
        t = datetime.fromtimestamp(m["obsTime"], timezone.utc).astimezone(tz)
        if m["icaoId"] != c["icao"] or t.date() != day or m.get("temp") is None:
            continue
        f = to_unit(m["temp"], c["unit"])
        mx = max(mx, round(f)) if mx is not None else round(f)
        cl = " ".join(f"{x['cover']}{x.get('base') or ''}" for x in m.get("clouds") or [])
        dew = f"{to_unit(m['dewp'], c['unit']):.0f}{u}" if m.get("dewp") is not None else "-"
        o_lines.append(f"{t:%H:%M} temp {f:.1f}{u} dew {dew} wind {m.get('wdir')}/{m.get('wspd')}kt "
                       f"clouds {cl or '-'} {m.get('wxString') or ''}".rstrip())
    f_lines = [f"{h['time'][-5:]} {h['temperature_2m']}{u} dew {h['dew_point_2m']} cloud {h['cloud_cover']}% "
               f"rain {h['precipitation_probability']}% {h['precipitation']}mm wind {h['wind_direction_10m']}/{h['wind_speed_10m']}kt sun {h.get('shortwave_radiation')}W/m2"
               for h in hrs if int(h["time"][11:13]) >= lnow.hour]
    models = conn.execute("SELECT model, fcst_max FROM mm_forecasts WHERE city = ? AND local_date = ? AND lead = 'live'",
                          (city, day.isoformat())).fetchall()
    afd = conn.execute("SELECT high_f, vs_guidance, rain_today, front_today, clouds_limit, sea_breeze, confidence FROM afd_signals "
                       "WHERE city = ? AND local_date = ?", (city, day.isoformat())).fetchone()
    st = next((m for m in obs if m["icaoId"] == c["icao"]), {})
    nb = notebook(conn, wallet, city)
    text = [f"City: {city} (airport {c['icao']}, {st.get('name', '')}). Local time now: {lnow:%Y-%m-%d %H:%M}.",
            f"Station: lat {st.get('lat', c['lat'])}, lon {st.get('lon', c['lon'])}, elevation {st.get('elev', '?')} m. "
            f"Sunrise {str((sun or {}).get('sunrise') or '?')[-5:]}, sunset {str((sun or {}).get('sunset') or '?')[-5:]} (local).",
            "YOUR NOTEBOOK (rules you wrote for yourself about this station):",
            *([f"- {x}" for x in nb] or ["(empty — start it)"]),
            "YOUR HOUR-AHEAD FORECASTS vs ACTUAL (review these first):", hour_review(conn, wallet, city, now or datetime.now(timezone.utc), obs),
            f"Market unit: {u} (buckets and all temperatures below are in {u}).",
            f"Max observed so far today (METAR, rounded {u}): {mx if mx is not None else 'none yet'}",
            "Today's METAR observations:", *(o_lines or ["none yet"]),
            "Remaining hourly forecast (Open-Meteo):", *(f_lines or ["none"]),
            f"Daily max forecasts by weather model ({u}): " + (", ".join(f"{m} {v:.1f}" for m, v in models) or "none"),
            ("NWS forecaster note: high %s°F, vs guidance %s, rain today %s, front %s, clouds limit heating %s, sea breeze %s, confidence %s" % tuple(afd))
            if afd else "NWS forecaster note: none",
            lgbm_forecast(conn, city, day),
            "Market buckets and current price of YES (= market probability):",
            *[f"{b['label']}: {b['price'] * 100:.1f}¢" for b in mk],
            "Your past daily-max forecasts for this city and the actual daily max:", memory(conn, wallet, city, day),
            "YOUR SYSTEMATIC ERRORS (computed by code over your whole history):", bias_stats(conn, wallet, city, day),
            "YOUR BETS AND MONEY (you bet $2 when your chance beats the price by 5+ points):", bet_summary(conn, wallet, city),
            "HOW OFTEN YOUR CHANCES CAME TRUE (all your forecasts 08-19 local, past days):", calibration_text(cal)]
    return "\n".join(text), mx


BAD_JSON_DIR = "/data/logs/llm_bad_json"
FIX_NOTE = ("\n\nВАЖНО: прошлый ответ был не разобран — сломанный JSON. Ответь строго валидным JSON: кавычки внутри текста "
            "экранируй (\\\") или заменяй на «», без комментариев и запятых в конце.")


def _ask_once(model, text, extra):
    r = requests.post(OR_URL, timeout=120, headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
                      json={"model": model, "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}],
                            "response_format": {"type": "json_object"}, "temperature": 0.2, "usage": {"include": True}, **(extra or {})})
    r.raise_for_status()
    d = r.json()
    raw = d["choices"][0]["message"]["content"].strip().removeprefix("```json").removesuffix("```")
    u = d.get("usage") or {}
    return raw, float(u.get("cost") or 0), int(u.get("total_tokens") or 0)


def _parse(raw):
    # 05.10: Gemini иногда пишет перенос строки прямо внутри текста (разбор, тетрадь) — strict=False принимает такие символы;
    # текст до/после JSON отрезаем по крайним скобкам (раньше город пропускался: «Invalid control character»)
    a, b = raw.find("{"), raw.rfind("}")
    return json.loads(raw[a:b + 1] if a >= 0 and b > a else raw, strict=False)


def ask(model, text, extra=None):
    """05.10: сломанный JSON (Gemini, Лондон: «Expecting ',' delimiter» — кавычка внутри текста) — ответ сохраняем
    в BAD_JSON_DIR для разбора и один раз переспрашиваем с просьбой о валидном JSON; цена обоих запросов идёт в расход."""
    raw, cost, tok = _ask_once(model, text, extra)
    try:
        return _parse(raw), cost, tok
    except json.JSONDecodeError as e:
        try:
            os.makedirs(BAD_JSON_DIR, exist_ok=True)
            with open(f"{BAD_JSON_DIR}/{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{model.split('/')[-1]}.txt", "w") as fh:
                fh.write(f"{e}\n\n{raw}")
        except OSError:
            pass
        print(f"{model}: сломанный JSON ({e}) — переспрашиваю", flush=True)
        raw2, cost2, tok2 = _ask_once(model, text + FIX_NOTE, extra)
        return _parse(raw2), cost + cost2, tok + tok2


def probs_for(ans, mk, mx):
    """Шансы по вариантам: невозможные (ниже уже измеренного максимума) — 0, сумма — 1."""
    p = {}
    got = ans.get("probs") or {}
    for b in mk:
        v = got.get(b["label"])
        try:
            v = max(0.0, float(v))
        except (TypeError, ValueError):
            v = 0.0
        if mx is not None and b["hi"] < mx:
            v = 0.0
        p[b["label"]] = v
    s = sum(p.values())
    return {k: v / s for k, v in p.items()} if s > 0 else None


def month_cost(conn, wallet, now):
    return conn.execute("SELECT COALESCE(SUM(cost), 0) FROM llm_hour_preds WHERE wallet = ? AND ts_utc >= ?",
                        (wallet, now.strftime("%Y-%m-01"))).fetchone()[0]


def bet(conn, wallet, city, day, now, mk, probs):
    """Одна ставка на город в день: лучший перевес «да» или «нет» над ценой продавца."""
    if conn.execute("SELECT 1 FROM paper_trades WHERE wallet = ? AND city = ? AND local_date = ?",
                    (wallet, city, day.isoformat())).fetchone():
        return None
    best = None
    for b in mk:
        p = probs[b["label"]]
        for side, price, q in (("yes", b["ask"], p), ("no", 1 - b["bid"], 1 - p)):
            if MIN_PRICE <= price <= MAX_PRICE and q - price >= EDGE and (best is None or q - price > best[0]):
                best = (q - price, side, price, q, b)
    if best is None or trading_stopped() or cash(conn, wallet) < STAKE * 1.1:
        return None
    edge, side, price, q, b = best
    tok = json.loads(b["m"]["clobTokenIds"])[0 if side == "yes" else 1]
    f = simulate_buy(b["m"], tok, STAKE, q - EDGE / 2)
    if f["shares"] <= 0:
        return f"не купили {side} {b['label']}: {f['reason']}"
    conn.execute("""INSERT OR IGNORE INTO paper_trades (wallet, city, local_date, snapshot_ts, unit, bucket_lo, bucket_hi,
        model_p, market_p, price, stake, status, reason, shares, fee, book_json, condition_id, token_id, placed_at, side)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?)""",
                 (wallet, city, day.isoformat(), now.isoformat(), OBS_CITIES[city]["unit"], b["lo"], b["hi"], q, price, f["avg"], f["cost"],
                  f"LLM {q * 100:.0f}% против цены {price * 100:.0f}¢ ({side}), {local_now(city, now):%H:%M} местного",
                  f["shares"], f["fee"], f["book"], b["m"].get("conditionId"), tok, now.isoformat(), side))
    conn.commit()
    return f"купили {side} {b['label']} {f['shares']:.1f} долей по {f['avg'] * 100:.1f}¢ (LLM {q * 100:.0f}%)"


def run():
    now = datetime.now(timezone.utc)
    act = [c for c in CITIES if local_now(c, now).hour in HOURS]
    if not act:
        print("llm_hour: ни в одном городе сейчас не 08-19 местного — пропуск")
        return
    if not os.environ.get("OPENROUTER_API_KEY"):
        sys.exit("OPENROUTER_API_KEY нет в .env")
    conn = sqlite3.connect(DB_PATH, timeout=60)
    ensure_schema(conn)
    obs = metars(now)
    for city in act:
        with item_guard(city, conn):
            lnow = local_now(city, now)
            day = lnow.date()
            mk = markets(city, day)
            if not mk:
                print(f"{city}: маркета на {day} нет")
                continue
            hrs, sun = hourly(city)
            for wallet, model in WALLETS.items():
                with item_guard(f"{city}/{wallet}", conn):
                    if conn.execute("SELECT 1 FROM llm_hour_preds WHERE wallet = ? AND city = ? AND local_date = ? AND local_hour = ?",
                                    (wallet, city, day.isoformat(), lnow.hour)).fetchone():
                        continue
                    spent = month_cost(conn, wallet, now)
                    if spent >= MONTH_LIMIT[wallet]:
                        print(f"{wallet}: лимит месяца ${MONTH_LIMIT[wallet]} исчерпан (${spent:.2f}) — пропуск")
                        continue
                    cal = calibration(conn, wallet, day)
                    text, mx = prompt(conn, wallet, city, day, lnow, obs, hrs, mk, sun, now, cal)
                    t0 = time.time()
                    ans, cost, tok = ask(model, text, EXTRA.get(wallet))
                    raw = probs_for(ans, mk, mx)
                    probs = calibrate(raw, cal, mk, mx)   # 06.10: ставка — по шансам после поправки
                    hourly_ans = {}
                    for k, v in (ans.get("hourly_f") or {}).items():
                        try:
                            hourly_ans[f"{int(str(k)[:2]):02d}"] = float(v)
                        except (TypeError, ValueError):
                            pass
                    conn.execute("""INSERT OR REPLACE INTO llm_hour_preds (wallet, city, local_date, local_hour, ts_utc, model, pred_max,
                        probs_json, market_json, max_so_far, reason, cost, tokens, prompt, answer_json, hourly_json, lesson, reflection,
                        probs_raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                 (wallet, city, day.isoformat(), lnow.hour, now.isoformat(), model, ans.get("pred_max_f"),
                                  json.dumps(probs), json.dumps({b["label"]: b["price"] for b in mk}), mx,
                                  str(ans.get("reason", ""))[:300], cost, tok, text, json.dumps(ans, ensure_ascii=False),
                                  json.dumps(hourly_ans), str(ans.get("lesson", ""))[:500], str(ans.get("reflection", ""))[:600],
                                  json.dumps(raw)))
                    nb = [str(x)[:200] for x in (ans.get("notebook") or []) if str(x).strip()][:NOTEBOOK_MAX]
                    if nb:
                        conn.execute("INSERT OR REPLACE INTO llm_notebook VALUES (?,?,?,?,?,?)",
                                     (wallet, city, now.isoformat(), day.isoformat(), lnow.hour, json.dumps(nb, ensure_ascii=False)))
                    conn.commit()
                    res = (bet(conn, wallet, city, day, now, mk, probs) if probs else "шансы не разобрать") if lnow.hour >= BET_FROM else None
                    print(f"{wallet} {city} {lnow:%H:%M}: максимум {ans.get('pred_max_f')}° (уже {mx}), "
                          f"${cost:.4f}, {time.time() - t0:.0f} с{'; ' + res if res else ''}", flush=True)
                    if wallet == MIX_FROM and lnow.hour >= BET_FROM:
                        with item_guard(f"{city}/{MIX_WALLET}", conn):
                            mp = mix_probs(conn, city, day, mk, mx, probs)
                            if mp is None:
                                print(f"{MIX_WALLET} {city} {lnow:%H:%M}: прогноза LightGBM на сегодня нет — без ставки", flush=True)
                            else:
                                conn.execute("""INSERT OR REPLACE INTO llm_hour_preds (wallet, city, local_date, local_hour, ts_utc, model,
                                    probs_json, market_json, max_so_far, reason, cost, tokens) VALUES (?,?,?,?,?,?,?,?,?,?,0,0)""",
                                             (MIX_WALLET, city, day.isoformat(), lnow.hour, now.isoformat(), f"mix:{model}+lgbm_v3",
                                              json.dumps(mp), json.dumps({b["label"]: b["price"] for b in mk}), mx,
                                              f"{MIX_W:.0%} Gemini + {1 - MIX_W:.0%} LightGBM v3"))
                                conn.commit()
                                mres = bet(conn, MIX_WALLET, city, day, now, mk, mp)
                                print(f"{MIX_WALLET} {city} {lnow:%H:%M}: смесь Gemini + LightGBM{'; ' + mres if mres else ''}", flush=True)
    conn.close()


if __name__ == "__main__":
    run()
