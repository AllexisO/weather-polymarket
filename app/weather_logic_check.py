"""
Проверка логики (11.10, просьба Alex: «такие проверки 2 раза в неделю, чтобы заранее выявить несоответствие»).
Повтор аудита 11.10 по крону — то, чего не видят ежечасный weather_audit и проверки утро/день/вечер:

1. Ставки «наверняка» (кошельки замеров) проиграли — каждая такая ставка = расхождение наших правил с итогом Polymarket
   (11.10: целые °C в °F-городах, полночь HKO, отбрасывание десятых в Гонконге).
2. Источники замеров против итога: максимум дня по каждому источнику — по ТЕМ ЖЕ правилам, что у кошельков, — не должен
   «убивать» выигравший вариант, даже если ставки не было.
3. Факт станции (weather_station_daily) против итога Polymarket — по городам.
4. Итог бота-мейкера — независимый пересчёт по исполнениям (сумма «исход − цена» × доли + возврат комиссии).
5. Ставки: настоящая цена далеко от цены снимка (снимок устарел), «да» дешевле 10¢ у кошельков моделей.
6. LLM: цена вне 10-90¢, две ставки на город-день, llm_agy_bet больше $5, ответы, которые не разбираются как JSON.
7. Итог маркета не пришёл больше 3 дней.
8. Правила маркетов Polymarket: текст описания (станция, точность, источник) по каждому городу — изменился с прошлого раза?
   Единица в вариантах (°F/°C) совпадает с нашей?

Только чтение рабочих баз + своя таблица logic_check (результат) и logic_rules (тексты правил). Результат показывает
утренняя/дневная/вечерняя проверка (weather_night_check.check_logic) — вкладки вверху /audit.
Крон: понедельник и четверг 09:00. Запуск: docker compose run --rm collector weather_logic_check.py
"""

import hashlib
import json
import math
import os
import re
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from jobmark import item_guard, mark
from weather_cities import OBS_CITIES

DB_PATH = Path(os.environ.get("POLY_LAB_DB", "/data/db/polymarket_lab.sqlite3"))
FAST_DB = DB_PATH.parent / "fastobs.sqlite3"
MM_DB = DB_PATH.parent / "mm.sqlite3"
DAYS = 14
# последнее исправление правил замеров (аудит 11.10: целые °C, Гонконг) — проигрыши до него уже разобраны; исправил снова — сдвинь
# 15.10: obs_fmi выключен, FMI в obs_fast только пишем, DWD запас 0.3 — проигрыши obs_fmi Хельсинки и obs_rt Лакхнау 13.10 разобраны
FIXED_FROM = "2026-10-15T12:00:00+00:00"
GAMMA = "https://gamma-api.polymarket.com"
CHECKS = []


def add(group, ok, title, detail="", fix=""):
    """ok: True — в порядке, "warn" — внимание, False — проблема (как weather_night_check.add)."""
    level = "ok" if ok is True else ("warn" if ok == "warn" else "bad")
    CHECKS.append({"group": group, "level": level, "title": title, "detail": detail, "fix": fix})


def short(items, n=6):
    items = list(items)
    return "; ".join(items[:n]) + (f" … и ещё {len(items) - n}" if len(items) > n else "")


def outcomes(c, since):
    return {(r[0], r[1]): (r[2], r[3]) for r in c.execute(
        "SELECT city, local_date, win_lo, win_hi FROM weather_poly_outcomes WHERE local_date >= ?", (since,))}


def check_obs_losses(c, since):
    """Только проигрыши, ставшие известны после прошлой проверки (старые уже разобраны) — иначе исправленное висело бы
    проблемой; ставки — после исправления правил (FIXED_FROM). 12.10: отбирали по времени ставки — ставка 10:04, итог
    23:08, а проверка шла в 23:02: проигрыш obs_fast Амстердам 10.10 не попал ни в один запуск. Теперь — по времени итога."""
    try:
        prev = c.execute("SELECT MAX(run_at) FROM logic_check").fetchone()[0]
    except sqlite3.Error:
        prev = None
    frm = max(prev or "", FIXED_FROM)
    rows = c.execute("""SELECT wallet, city, local_date, bucket_hi, obs_max FROM paper_obs_trades
                        WHERE status = 'lost' AND local_date >= ? AND placed_at >= ? AND settled_at >= ?
                        ORDER BY local_date""", (since, FIXED_FROM, prev or "")).fetchall()
    add("Логика", True if not rows else False, f"Ставки «наверняка» не проигрывают (кошельки замеров, итоги с {frm[8:10]}.{frm[5:7]})",
        short(f"{w} {ci} {d[8:]}.{d[5:7]}: замер {mx}, вариант до {hi}" for w, ci, d, hi, mx in rows) if rows else "проигрышей нет",
        "" if not rows else "правила замеров расходятся с итогом Polymarket — разобрать город и источник (округление, станция, полночь)")


def check_sources(c, since, out):
    """Максимум дня по каждому источнику по правилам кошельков против выигравшего варианта."""
    import weather_fastobs as fo
    from weather_obs_rt import value as metar_v
    try:
        mins = json.loads(fo.METAR_MIN.read_text())
    except (OSError, ValueError):
        mins = {}
    day_max = defaultdict(lambda: -999.0)   # (источник, город, дата) → максимум по правилам
    if FAST_DB.exists():
        f = sqlite3.connect(f"file:{FAST_DB}?mode=ro", uri=True, timeout=30)
        for s, city, t, v in f.execute("SELECT source, city, obs_utc, temp_c FROM fast_obs WHERE obs_utc >= ?", (since,)):
            cfg = OBS_CITIES.get(city)
            if not cfg or s == "wethr_hf" or s in fo.NO_BET_SOURCES:   # 5-мин wethr и KNMI (с 12.10) ставок не дают
                continue
            t = datetime.fromisoformat(t)
            if s == "hko":
                d, mv = (t - timedelta(minutes=1)).astimezone(ZoneInfo(cfg["tz"])).date().isoformat(), math.floor(v)
            else:
                if not any(min((t.minute - m) % 60, (m - t.minute) % 60) <= fo.WINDOW_BY_SOURCE.get(s, fo.WINDOW)
                           for m in mins.get(city, [0, 30])):
                    continue
                d, mv = t.astimezone(ZoneInfo(cfg["tz"])).date().isoformat(), fo.metar_value(v - fo.ROUND_MARGIN.get(s, 0.0), cfg["unit"])
            day_max[(s, city, d)] = max(day_max[(s, city, d)], mv)
        f.close()
    icao = {cfg["icao"]: city for city, cfg in OBS_CITIES.items() if cfg.get("icao")}
    for ic, t, s, v in c.execute("SELECT icao, obs_time_utc, source, temp_c FROM metar_seen_src WHERE obs_time_utc >= ? AND temp_c IS NOT NULL",
                                 (since,)):
        city = icao.get(ic)
        if not city:
            continue
        cfg = OBS_CITIES[city]
        d = datetime.fromisoformat(t).astimezone(ZoneInfo(cfg["tz"])).date().isoformat()
        day_max[(s, city, d)] = max(day_max[(s, city, d)], metar_v(v, cfg))
    bad, n = [], Counter()
    for (s, city, d), mv in day_max.items():
        o = out.get((city, d))
        if o is None:
            continue
        n[s] += 1
        if o[1] < mv:   # выигравший вариант по нашим правилам «мёртв»
            bad.append((s, city, d, mv, o))
    by_src = Counter(b[0] for b in bad)
    # единичный день (Париж 07.10: METAR 19, итог 18) — внимание; повторяется в городе или > 1% по источнику — проблема
    serious = any(k >= 2 and k / n[s] > 0.01 for s, k in by_src.items()) or len({b[1] for b in bad}) < len(bad) / max(len(by_src), 1)
    add("Логика", True if not bad else (False if serious else "warn"), f"Замеры по правилам кошельков не «убивают» выигравший вариант ({DAYS} дней)",
        (f"проверено город-дней по источникам: {', '.join(f'{s} {k}' for s, k in sorted(n.items()))}" if not bad else
         f"по источникам: {', '.join(f'{s} {k} из {n[s]}' for s, k in by_src.most_common())} — "
         + short(f"{s} {ci} {d[8:]}.{d[5:7]}: {mv} при итоге до {o[1]}" for s, ci, d, mv, o in sorted(bad, key=lambda x: x[2]))),
        "" if not bad else "если по источнику так ставил бы кошелёк — он проиграет; найти, чем правило отличается от итога")


def check_station(c, since, out):
    bad = defaultdict(list)
    for city, d, a in c.execute("SELECT city, local_date, actual_max FROM weather_station_daily WHERE local_date >= ? AND actual_max IS NOT NULL",
                                (since,)):
        o = out.get((city, d))
        if o and not (o[0] <= a < o[1]):
            bad[city].append(f"{d[8:]}.{d[5:7]} станция {a:g}, итог {o[0] + 0.5:g}-{o[1] - 0.5:g}")
    many = {k: v for k, v in bad.items() if len(v) >= 2}
    add("Логика", "warn" if many else True, f"Факт станции совпадает с итогом Polymarket ({DAYS} дней)",
        short(f"{k}: {len(v)} раз ({v[-1]})" for k, v in sorted(many.items(), key=lambda x: -len(x[1])))
        if many else f"расхождений по 2+ раза нет (единичных: {sum(len(v) for v in bad.values())})",
        "модели учатся на факте станции — если итог считается по другой станции/правилу, город учится не тому" if many else "")


def check_maker(c):
    if not MM_DB.exists():
        return
    m = sqlite3.connect(f"file:{MM_DB}?mode=ro", uri=True, timeout=30)
    from weather_mm_settle import L_MAX, REBATE
    win = {(r[0], r[1]): r[2] for r in c.execute("SELECT city, local_date, win_lo FROM weather_poly_outcomes")}
    res = {(w, cid): p for w, cid, p in m.execute("SELECT wallet, condition_id, pnl FROM mm_results WHERE wallet LIKE 'mm_ws%' OR wallet LIKE 'mm100%'")}
    fills, meta = defaultdict(list), {}
    for w, cid, city, ld, lo, side, pr, k in m.execute("""SELECT wallet, condition_id, city, local_date, bucket_lo, side, price, size
                                                           FROM mm_ws_fills ORDER BY ts, rowid"""):
        fills[(w, cid)].append((side, pr, k))
        meta[(w, cid)] = (city, ld, lo)
    m.close()
    diff, n = [], 0
    for key, fs in fills.items():
        city, ld, lo = meta[key]
        if key not in res or (city, ld) not in win or lo is None:
            continue
        fin = 1.0 if abs(win[(city, ld)] - lo) < 1e-6 else 0.0
        oy = on = pnl = 0.0
        for side, pr, k in fs:
            k = max(0.0, min(k, L_MAX - (oy - on) if side == "yes" else L_MAX - (on - oy)))
            if k <= 1e-9:
                continue
            oy, on = (oy + k, on) if side == "yes" else (oy, on + k)
            mm = min(oy, on)
            oy, on = oy - mm, on - mm
            pnl += k * ((fin if side == "yes" else 1 - fin) - pr) + (0 if key[0] in ("mm100", "mm100f") else REBATE * pr * (1 - pr) * k)
        n += 1
        if abs(pnl - res[key]) > 0.01:
            diff.append(f"{key[0]} {city} {ld}: пересчёт {pnl:+.2f}, записано {res[key]:+.2f}")
    add("Логика", True if not diff else False, "Итог бота-мейкера сходится с независимым пересчётом",
        f"маркетов {n}" + (f", расхождений {len(diff)}: " + short(diff) if diff else ", расхождений нет"),
        "" if not diff else "weather_mm_settle считает итог не так, как исполнения — решение по мейкеру по неверным цифрам")


def check_trades(c, since):
    rows = c.execute("""SELECT wallet, city, local_date, side, price, market_p, placed_at, snapshot_ts FROM paper_trades
                        WHERE status IN ('open', 'won', 'lost', 'void') AND local_date >= ? AND price IS NOT NULL AND market_p IS NOT NULL""",
                     (since,)).fetchall()
    import weather_paper as wp
    taker_models = set(wp.WALLETS) - set(wp.YES_BAND)
    stale = [f"{w} {ci} {d[8:]}.{d[5:7]}: купили по {100 * p:.0f}¢, в снимке {100 * mk:.0f}¢" for w, ci, d, sd, p, mk, *_ in rows
             if w in taker_models | set(wp.NO_WALLETS) and abs(p - mk) > 0.15]
    cheap = [f"{w} {ci} {d[8:]}.{d[5:7]}: «да» по {100 * p:.1f}¢" for w, ci, d, sd, p, mk, pa, ts in rows
             if w in taker_models and (sd or "yes") == "yes" and p < wp.YES_MIN - 1e-9 and (pa or ts) >= wp.YES_MIN_FROM]
    add("Логика", "warn" if stale else True, "Настоящая цена покупки близка к цене снимка (±15 п.п.)",
        short(stale) if stale else f"ставок {len(rows)}", "снимок цены устарел — решение принято по чужой цене" if stale else "")
    add("Логика", "warn" if cheap else True, "Кошельки моделей не покупают «да» дешевле 10¢ (по настоящей цене)",
        short(cheap) if cheap else "нет", "правило 10¢ проверяется по цене снимка, а купили дешевле — проверить потолок" if cheap else "")


def check_llm(c, since):
    rows = c.execute("""SELECT wallet, city, local_date, price, stake, COALESCE(fee, 0) FROM paper_trades WHERE wallet LIKE 'llm%'
                        AND status IN ('open', 'won', 'lost', 'void') AND local_date >= ?""", (since,)).fetchall()
    bad = [f"{w} {ci} {d[8:]}.{d[5:7]}: цена {100 * p:.1f}¢" for w, ci, d, p, *_ in rows if p is not None and not 0.10 - 1e-9 <= p <= 0.90 + 1e-9]
    bad += [f"llm_agy_bet {ci} {d[8:]}.{d[5:7]}: ${st + fee:.2f}" for w, ci, d, p, st, fee in rows if w == "llm_agy_bet" and st + fee > 5.5]
    bad += [f"{k[0]} {k[1]} {k[2]}: {v} ставки" for k, v in Counter((w, ci, d) for w, ci, d, *_ in rows).items() if v > 1]
    nojson = 0
    for (a,) in c.execute("SELECT answer_json FROM llm_hour_preds WHERE local_date >= ? AND answer_json IS NOT NULL", (since,)):
        try:
            # 15.10: так же, как разбирает сам кошелёк (weather_llm_hour._parse): по крайним скобкам — ответ в ```json``` разобран верно
            i, j = a.find("{"), a.rfind("}")
            json.loads(a[i:j + 1] if i >= 0 and j > i else a, strict=False)
        except ValueError:
            nojson += 1
    add("Логика", True if not bad else False, "Ставки LLM в рамках правил (10-90¢, одна в день, llm_agy_bet ≤ $5)",
        short(bad) if bad else f"ставок {len(rows)}", "правило кошелька LLM нарушено — найти, где код его обходит" if bad else "")
    add("Логика", "warn" if nojson else True, "Ответы LLM разбираются (JSON)", f"не разбираются: {nojson}" if nojson else "все",
        "страница /llm и разбор ответа могут спотыкаться" if nojson else "")


def check_pending(c):
    from weather_edge import CITIES   # итоги собираются только по 48 городам (Гонконг, Тайбэй — только для замеров)
    rows = c.execute("""SELECT s.city, s.local_date FROM snapshots s LEFT JOIN weather_poly_outcomes p ON s.city = p.city
                        AND s.local_date = p.local_date WHERE p.city IS NULL AND s.local_date < date('now', '-3 day')
                        AND s.local_date >= date('now', '-30 day') GROUP BY 1, 2""").fetchall()
    rows = [r for r in rows if r[0] in CITIES]
    add("Логика", "warn" if rows else True, "Итоги маркетов приходят (нет зависших дольше 3 дней)",
        short(f"{ci} {d[8:]}.{d[5:7]}" for ci, d in rows) if rows else "нет",
        "маркет не закрылся или закрылся неоднозначно — ставки по нему не считаются" if rows else "")


def norm_rules(text):
    """Описание маркета без дат и чисел — чтобы сравнивать правила, а не день."""
    t = re.sub(r"\d+", "#", text or "")
    t = re.sub(r"\b(January|February|March|April|May|June|July|August|September|October|November|December|"
               r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\b", "<m>", t)
    return re.sub(r"\s+", " ", t).strip()


def check_rules(w):
    from weather_edge import month_day_year_slug
    w.execute("CREATE TABLE IF NOT EXISTS logic_rules (city TEXT PRIMARY KEY, hash TEXT, text TEXT, unit TEXT, seen_at TEXT)")
    known = {r[0]: (r[1], r[2]) for r in w.execute("SELECT city, hash, text FROM logic_rules")}
    changed, unit_bad, new, miss = [], [], 0, []
    day = date.today() + timedelta(days=1)
    for city, cfg in OBS_CITIES.items():
        slug = f"highest-temperature-in-{cfg['poly_slug']}-on-{month_day_year_slug(day)}"
        try:
            ev = requests.get(f"{GAMMA}/events", params={"slug": slug}, timeout=20).json()
        except (requests.RequestException, ValueError):
            miss.append(city)
            continue
        if not ev:
            miss.append(city)
            continue
        text = norm_rules(ev[0].get("description"))
        qs = " ".join(m.get("question", "") for m in ev[0].get("markets", []))
        unit = "fahrenheit" if "°F" in qs else ("celsius" if "°C" in qs else None)
        if unit and unit != cfg["unit"]:
            unit_bad.append(f"{city}: у нас {cfg['unit']}, в маркете {unit}")
        h = hashlib.sha1(text.encode()).hexdigest()
        if city in known and known[city][0] != h:
            old = known[city][1] or ""
            i = next((k for k, (a, b) in enumerate(zip(old, text)) if a != b), min(len(old), len(text)))
            changed.append(f"{city}: «…{text[max(0, i - 40):i + 120]}…»")
        if city not in known:
            new += 1
        if city not in known or known[city][0] != h:
            w.execute("INSERT OR REPLACE INTO logic_rules VALUES (?, ?, ?, ?, ?)", (city, h, text, unit, datetime.now(timezone.utc).isoformat()))
        time.sleep(0.2)
    w.commit()
    add("Логика", True if not changed else False, "Правила маркетов Polymarket не менялись (станция, источник, точность)",
        f"изменилось у {len(changed)} из {len(OBS_CITIES) - len(miss)}: " + short(changed, 4) if changed else f"городов {len(OBS_CITIES) - len(miss)}" + (f", впервые записано {new}" if new else "")
        + (f"; маркета на завтра нет: {', '.join(miss[:8])}" if miss else ""),
        "Polymarket поменял правила итога — проверить станцию/округление/полночь у моделей и кошельков замеров" if changed else "")
    add("Логика", True if not unit_bad else False, "Единица вариантов (°F/°C) совпадает с нашей", short(unit_bad) if unit_bad else "да",
        "модели считают шансы не в той единице" if unit_bad else "")


def main():
    t0 = time.time()
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=DAYS)).date().isoformat()
    c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=60)
    out = outcomes(c, since)
    for fn, args in ((check_obs_losses, (c, since)), (check_sources, (c, since, out)), (check_station, (c, since, out)),
                     (check_maker, (c,)), (check_trades, (c, since)), (check_llm, (c, since)), (check_pending, (c,))):
        with item_guard(fn.__name__):
            fn(*args)
    c.close()
    w = sqlite3.connect(DB_PATH, timeout=60)
    with item_guard("check_rules"):
        check_rules(w)
    import jobmark
    for e in jobmark.ITEM_ERRORS:
        add("Логика", False, "Часть проверки логики не выполнилась", e, "посмотреть data/logs/weather_logic_check.log")
    bad = sum(x["level"] == "bad" for x in CHECKS)
    warn = sum(x["level"] == "warn" for x in CHECKS)
    w.execute("CREATE TABLE IF NOT EXISTS logic_check (run_at TEXT PRIMARY KEY, bad INTEGER, warn INTEGER, details TEXT)")
    w.execute("INSERT OR REPLACE INTO logic_check VALUES (?, ?, ?, ?)",
              (now.isoformat(), bad, warn, json.dumps({"took_s": round(time.time() - t0, 1), "checks": CHECKS}, ensure_ascii=False)))
    w.execute("DELETE FROM logic_check WHERE run_at < ?", ((now - timedelta(days=90)).isoformat(),))
    mark(w, "weather_logic_check")
    w.commit()
    w.close()
    print(f"проверка логики: {len(CHECKS)} проверок, проблем {bad}, внимание {warn}, за {time.time() - t0:.0f} с")
    for x in CHECKS:
        print(f"  {'ok' if x['level'] == 'ok' else ('ПРОБЛЕМА' if x['level'] == 'bad' else 'внимание')}: {x['title']} — {x['detail']}")


if __name__ == "__main__":
    main()
