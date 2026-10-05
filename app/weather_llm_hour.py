"""
LLM-прогноз максимума дня каждый час (03.10, решение Alex; кошельки llm_gem и llm_ds).

Идея Alex: LLM каждый час смотрит всё, что известно о дне (замеры аэропорта с утра, почасовой прогноз облаков /
дождя / ветра, прогнозы 13 погодных моделей на максимум, разбор синоптиков NWS, цены рынка) и называет итоговый
максимум дня и шанс каждого варианта. Сверка с фактом — её прошлые прогнозы и ошибки по этому городу показываются ей
в каждом запросе («память ошибок»; сама модель через OpenRouter не дообучается — так делает и коллега Alex, ~$5/мес).

Кошельки (одна идея — две LLM, чтобы было видно, какая лучше):
  llm_gem — google/gemini-3.8-flash (как у коллеги Alex);
  llm_ds  — deepseek/deepseek-v4-pro (дешевле, для сравнения).
Города: 5 городов США с самыми большими деньгами (Чикаго, Атланта, Остин, Майами, Даллас), каждый час 05-19 местного
(с 04.10; ставки — только с 08:05).
Ставка: одна на город в день — в первый час, когда шанс LLM выше цены продавца на EDGE (за «да» или за «нет»),
цена 10-90¢, $2, исполнение по настоящему стакану с комиссией (polyexec.simulate_buy). Итог — weather_paper.settle.
Расход: OpenRouter возвращает цену каждого запроса (llm_hour_preds.cost); дошёл до MONTH_LIMIT за месяц — кошелёк
стоит до следующего месяца (видно в логе и на /status). На самом ключе ещё лимит $10.

Крон: 5 * * * * (в :05 — к этому времени вышли сводки :51-:53 часа). Порог решения — docs/PRD.md §9 п.16.
"""

import json
import os
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
HOURS = range(5, 20)    # местное время, в которое спрашиваем (04.10, просьба Alex: с 05:05 — прогноз и разбор с утра)
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
EXTRA_COLS = (("prompt", "TEXT"), ("answer_json", "TEXT"), ("hourly_json", "TEXT"), ("lesson", "TEXT"), ("reflection", "TEXT"))
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
   your notebook and your recent errors), predict the temperature for every remaining hour of today, and today's max.
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
    by_day = {}
    for d, h, p, a in rows:
        by_day.setdefault((d, a), []).append(f"{h:02d}h:{p:.0f}")
    errs = [p - a for _d, _h, p, a in rows if p is not None]
    lines = [f"{d}: actual max {a:.0f}; your forecasts by local hour: {' '.join(v)}" for (d, a), v in by_day.items()]
    lines.append(f"Your mean error (forecast - actual) over these days: {sum(errs) / len(errs):+.1f}, n={len(errs)}")
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


def prompt(conn, wallet, city, day, lnow, obs, hrs, mk, sun=None, now=None):
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
            "Market buckets and current price of YES (= market probability):",
            *[f"{b['label']}: {b['price'] * 100:.1f}¢" for b in mk],
            "Your past daily-max forecasts for this city and the actual daily max:", memory(conn, wallet, city, day)]
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
                    text, mx = prompt(conn, wallet, city, day, lnow, obs, hrs, mk, sun, now)
                    t0 = time.time()
                    ans, cost, tok = ask(model, text, EXTRA.get(wallet))
                    probs = probs_for(ans, mk, mx)
                    hourly_ans = {}
                    for k, v in (ans.get("hourly_f") or {}).items():
                        try:
                            hourly_ans[f"{int(str(k)[:2]):02d}"] = float(v)
                        except (TypeError, ValueError):
                            pass
                    conn.execute("""INSERT OR REPLACE INTO llm_hour_preds (wallet, city, local_date, local_hour, ts_utc, model, pred_max,
                        probs_json, market_json, max_so_far, reason, cost, tokens, prompt, answer_json, hourly_json, lesson, reflection)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                 (wallet, city, day.isoformat(), lnow.hour, now.isoformat(), model, ans.get("pred_max_f"),
                                  json.dumps(probs), json.dumps({b["label"]: b["price"] for b in mk}), mx,
                                  str(ans.get("reason", ""))[:300], cost, tok, text, json.dumps(ans, ensure_ascii=False),
                                  json.dumps(hourly_ans), str(ans.get("lesson", ""))[:500], str(ans.get("reflection", ""))[:600]))
                    nb = [str(x)[:200] for x in (ans.get("notebook") or []) if str(x).strip()][:NOTEBOOK_MAX]
                    if nb:
                        conn.execute("INSERT OR REPLACE INTO llm_notebook VALUES (?,?,?,?,?,?)",
                                     (wallet, city, now.isoformat(), day.isoformat(), lnow.hour, json.dumps(nb, ensure_ascii=False)))
                    conn.commit()
                    res = (bet(conn, wallet, city, day, now, mk, probs) if probs else "шансы не разобрать") if lnow.hour >= BET_FROM else None
                    print(f"{wallet} {city} {lnow:%H:%M}: максимум {ans.get('pred_max_f')}° (уже {mx}), "
                          f"${cost:.4f}, {time.time() - t0:.0f} с{'; ' + res if res else ''}", flush=True)
    conn.close()


if __name__ == "__main__":
    run()
