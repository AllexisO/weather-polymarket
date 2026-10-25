"""
Тесты данных (15.10, третья проверка проекта — Alex: «зафиксируй всё тестами, проверь, что данные полностью соответствуют»).
preflight.py их вызывает: ./check.sh, каждые 30 мин (weather_alerts) и проверки 3 раза в день (night_check).
База только на чтение. Каждый тест — (ok, название, подробности); на 15.10 все проходят — любое «✗» значит новое расхождение.

1. закрытые ставки (paper_trades, paper_obs_trades): статус и выплата — из итога Polymarket;
2. открытых ставок с известным итогом нет; дублей «кошелёк — город — день» нет; деньги кошельков не уходят в минус;
3. шансы моделей в снимках (3 дня): сумма 1, каждый 0..1; варианты маркета покрывают шкалу без дыр;
4. дневная модель (3 дня): сумма 1 у модели и смеси, варианты ниже максимума с утра — шанс 0;
5. LLM (3 дня): сумма шансов 1, варианты = варианты маркета;
6. живые сводки METAR (tgftp, aviationweather; 14 дней) совпадают с архивом IEM по каждой сводке (≥ 99.5%);
7. быстрые замеры, по которым кошелёк obs_fast СТАВИТ, по его правилу ни разу не «убили» выигравший вариант (вся история);
8. итоги Polymarket: выигравший вариант — один из вариантов маркета, финальные цены в сумме 1.
"""

import collections
import json
import math
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
FAST_DB = DB_PATH.parent / "fastobs.sqlite3"
METAR_MIN = DB_PATH.parent / "mm_metar_minutes.json"


def _short(items, n=4):
    items = list(items)
    return "; ".join(map(str, items[:n])) + (f" … и ещё {len(items) - n}" if len(items) > n else "")


def settled_bets(c):
    out = {(a, b): (x, y) for a, b, x, y in c.execute("SELECT city, local_date, win_lo, win_hi FROM weather_poly_outcomes")}
    bad, n = [], 0
    for w, city, d, lo, hi, side, st, sh, pay in c.execute(
            "SELECT wallet, city, local_date, bucket_lo, bucket_hi, COALESCE(side, 'yes'), status, shares, payout FROM paper_trades "
            "WHERE status IN ('won', 'lost')"):
        o = out.get((city, d))
        if o is None:   # итог пишет weather_poly_resolve раз в 2 ч, расчёт — по закрытому маркету раньше
            continue
        n += 1
        hit = abs(o[0] - lo) < 1e-6 and abs(o[1] - hi) < 1e-6
        win = hit if side == "yes" else not hit
        if win != (st == "won") or abs((pay or 0) - ((sh or 0) if win else 0)) > 0.011:
            bad.append(f"{w} {city} {d}: {st}, выплата {pay}")
    for w, city, d, lo, hi, st, sh, pay in c.execute(
            "SELECT wallet, city, local_date, bucket_lo, bucket_hi, status, shares, payout FROM paper_obs_trades WHERE status IN ('won', 'lost')"):
        o = out.get((city, d))
        if o is None:
            continue
        n += 1
        win = not (abs(o[0] - lo) < 1e-6 and abs(o[1] - hi) < 1e-6)   # кошельки замеров — всегда «нет»
        if win != (st == "won") or abs((pay or 0) - ((sh or 0) if win else 0)) > 0.011:
            bad.append(f"{w} {city} {d}: {st}, выплата {pay}")
    return not bad, "закрытые ставки посчитаны по итогу Polymarket (статус и выплата)", _short(bad) or f"проверено {n}"


def bets_sane(c):
    import weather_paper as wp
    bad = []
    old = (date.today() - timedelta(days=2)).isoformat()
    for t in ("paper_trades", "paper_obs_trades"):
        n = c.execute(f"SELECT COUNT(*) FROM {t} b JOIN weather_poly_outcomes o USING (city, local_date) "
                      f"WHERE b.status = 'open' AND b.local_date < ?", (old,)).fetchone()[0]
        if n:
            bad.append(f"{t}: открытых с итогом старше 2 дней — {n}")
    bad += [f"дубль {w} {ci} {d} ×{k}" for w, ci, d, k in c.execute(
        "SELECT wallet, city, local_date, COUNT(*) FROM paper_trades WHERE status IN ('open', 'won', 'lost', 'void', 'resting') "
        "AND wallet != 'copy' GROUP BY 1, 2, 3 HAVING COUNT(*) > 1")]
    bad += [f"цена/доли {w} {ci} {d}" for w, ci, d in c.execute(
        "SELECT wallet, city, local_date FROM paper_trades WHERE status IN ('open', 'won', 'lost') AND "
        "(price <= 0 OR price >= 1 OR stake < 0 OR COALESCE(fee, 0) < 0 OR (shares > 0 AND ABS(shares * price - stake) > 0.05))")]
    for w, sp, bk in c.execute("SELECT wallet, SUM(stake + COALESCE(fee, 0)), SUM(COALESCE(payout, 0)) FROM paper_trades GROUP BY wallet"):
        if wp.start_balance(w) - sp + bk < -0.01:
            bad.append(f"{w}: деньги {wp.start_balance(w) - sp + bk:.2f}")
    return not bad, "ставки: нет зависших, дублей, неверных цен, деньги не в минусе", _short(bad) or "в порядке"


def snapshot_probs(c):
    since = (date.today() - timedelta(days=3)).isoformat()
    cols = [r[1] for r in c.execute("PRAGMA table_info(snapshots)") if r[1].endswith("_model_p") and r[1].startswith("ml")]
    bad = []
    for col in cols:
        r = c.execute(f"""SELECT COUNT(*), SUM(ABS(s - 1) > 0.01), SUM(mn < -1e-9 OR mx > 1 + 1e-9) FROM (SELECT SUM({col}) s, MIN({col}) mn,
                          MAX({col}) mx FROM snapshots WHERE local_date >= ? AND {col} IS NOT NULL GROUP BY ts_utc, city, local_date)""",
                      (since,)).fetchone()
        if r[1] or r[2]:
            bad.append(f"{col}: сумма ≠ 1 в {r[1]} из {r[0]}, вне 0..1 — {r[2]}")
    gaps = []
    for ts, city, b in c.execute("SELECT ts_utc, city, group_concat(bucket_lo || ':' || bucket_hi) FROM snapshots WHERE local_date >= ? "
                                 "GROUP BY ts_utc, city, local_date", (since,)):
        bs = sorted(tuple(map(float, x.split(":"))) for x in b.split(","))
        if not (bs[0][0] <= -998 and bs[-1][1] >= 998 and all(abs(bs[i][1] - bs[i + 1][0]) < 1e-6 for i in range(len(bs) - 1))):
            gaps.append(f"{city} {ts[:16]}")
    if gaps:
        bad.append("варианты с дырой: " + _short(gaps))
    return not bad, f"снимки 3 дня: шансы {len(cols)} моделей в сумме 1, варианты без дыр", _short(bad) or "в порядке"


def ml_day(c):
    from weather_edge import parse_bucket
    since = (date.today() - timedelta(days=3)).isoformat()
    bad, n = [], 0
    for city, d, h, u, dmax, mj, bj, kj in c.execute("SELECT city, local_date, local_hour, unit, day_max_c, model_json, blend_json, "
                                                     "market_json FROM ml_day_preds WHERE local_date >= ?", (since,)):
        n += 1
        m, b, k = json.loads(mj), json.loads(bj), json.loads(kj)
        if abs(sum(m.values()) - 1) > 0.01 or abs(sum(b.values()) - 1) > 0.01 or set(m) != set(b) or set(b) != set(k):
            bad.append(f"{city} {d} {h}ч: сумма/варианты")
            continue
        dmx = round(dmax if u == "celsius" else dmax * 9 / 5 + 32)
        for lab, p in m.items():
            lo_hi = parse_bucket(f"be {'between ' if '-' in lab[1:] else ''}{lab} on")
            if lo_hi is None:
                bad.append(f"{city} {d}: вариант «{lab}» не разобран")
            elif lo_hi[1] < dmx - 0.5 - 1e-6 and p > 0.001:
                bad.append(f"{city} {d} {h}ч: «{lab}» уже невозможен (с утра {dmx}), а шанс {p:.3f}")
    return not bad, "дневная модель 3 дня: шансы в сумме 1, у невозможных вариантов 0", _short(bad) or f"решений {n}"


def llm(c):
    since = (date.today() - timedelta(days=3)).isoformat()
    bad, n = [], 0
    for w, city, d, h, pj, kj in c.execute("SELECT wallet, city, local_date, local_hour, probs_json, market_json FROM llm_hour_preds "
                                           "WHERE local_date >= ? AND probs_json IS NOT NULL", (since,)):
        p = json.loads(pj)
        if p is None:     # "null": шансы из ответа не разобрать — ставки нет (weather_llm_hour «шансы не разобрать»), 24.10
            continue
        n += 1
        if abs(sum(p.values()) - 1) > 0.02 or any(not -1e-9 <= v <= 1 + 1e-9 for v in p.values()):
            bad.append(f"{w} {city} {d} {h}ч: сумма {sum(p.values()):.3f}")
        elif kj and set(p) != set(json.loads(kj)):
            bad.append(f"{w} {city} {d} {h}ч: варианты ≠ маркету")
    return not bad, "LLM 3 дня: шансы в сумме 1, варианты как у маркета", _short(bad) or f"ответов {n}"


def metar_vs_iem(c):
    from weather_cities import OBS_CITIES
    since = (date.today() - timedelta(days=14)).isoformat()
    iem2icao = {cfg["iem"]: cfg["icao"] for cfg in OBS_CITIES.values()}
    iem = {(iem2icao.get(st), v[:16].replace(" ", "T")): (t - 32) * 5 / 9 for st, v, t in c.execute(
        "SELECT station, valid_utc, tmpf FROM station_obs WHERE valid_utc >= ? AND tmpf IS NOT NULL", (since,))}
    same = n = 0
    diff = []
    for icao, t, src, v in c.execute("SELECT icao, obs_time_utc, source, temp_c FROM metar_seen_src WHERE source IN ('tgftp', 'awc') "
                                     "AND temp_c IS NOT NULL AND obs_time_utc >= ?", (since,)):
        k = (icao, t[:16])
        if k in iem:
            n += 1
            if abs(iem[k] - v) <= 0.6:
                same += 1
            else:
                diff.append(f"{icao} {t[5:16]} {src} {v} / архив {iem[k]:.1f}")
    ok = n > 1000 and same >= 0.995 * n
    return ok, "живые сводки METAR = архив IEM (14 дней, каждая сводка)", f"совпало {same} из {n}" + (f"; расходятся: {_short(diff)}" if diff else "")


def fast_sources(c):
    import weather_fastobs as fo
    from weather_cities import ALL_OBS_CITIES
    if not FAST_DB.exists():
        return False, "быстрые замеры не «убивают» выигравший вариант", "нет fastobs.sqlite3"
    f = sqlite3.connect(f"file:{FAST_DB}?mode=ro", uri=True, timeout=30)
    try:
        mins = json.loads(METAR_MIN.read_text())
    except (OSError, ValueError):
        mins = {}
    out = {(a, b): (x, y) for a, b, x, y in c.execute("SELECT city, local_date, win_lo, win_hi FROM weather_poly_outcomes")}
    best = {}
    for s, city, t, v in f.execute("SELECT source, city, obs_utc, temp_c FROM fast_obs WHERE source NOT IN ('synoptic', 'wethr_hf')"):
        cfg = ALL_OBS_CITIES.get(city)
        if s in fo.NO_BET_SOURCES or not cfg or cfg["unit"] == "fahrenheit":
            continue
        t = datetime.fromisoformat(t)
        if s == "hko":   # как weather_fastobs.main: запись 00:00 — максимум прошлого дня; итог — отбрасывание десятых
            day, mv = (t - timedelta(minutes=1)).astimezone(ZoneInfo(cfg["tz"])).date().isoformat(), math.floor(v)
        elif any(min((t.minute - m) % 60, (m - t.minute) % 60) <= fo.WINDOW_BY_SOURCE.get(s, fo.WINDOW) for m in mins.get(city, [0, 30])):
            day, mv = t.astimezone(ZoneInfo(cfg["tz"])).date().isoformat(), fo.metar_value(v - fo.ROUND_MARGIN.get(s, 0.0), cfg["unit"])
        else:
            continue
        k = (s, city, day)
        best[k] = max(best.get(k, -999), mv)
    f.close()
    n, bad = 0, []
    for (s, city, day), mv in best.items():
        o = out.get((city, day))
        if o:
            n += 1
            if mv > o[1]:
                bad.append(f"{s} {city} {day}: {mv} при итоге до {o[1]}")
    return not bad, "быстрые замеры (источники со ставками) по правилу obs_fast не «убивают» выигравший вариант", \
        _short(bad) or f"город-дней {n}"


def outcomes(c):
    fin = collections.defaultdict(dict)
    for city, d, lo, hi, y in c.execute("SELECT city, local_date, bucket_lo, bucket_hi, final_yes FROM poly_market_final"):
        fin[(city, d)][(lo, hi)] = y
    bad = [f"{ci} {d}: сумма {sum(v.values()):.2f}" for (ci, d), v in fin.items() if abs(sum(v.values()) - 1) > 0.01]
    n = 0
    for city, d, lo, hi in c.execute("SELECT city, local_date, win_lo, win_hi FROM weather_poly_outcomes"):
        if (city, d) in fin:
            n += 1
            if fin[(city, d)].get((lo, hi), 0) < 0.99:
                bad.append(f"{city} {d}: выигравший {lo}..{hi} не закрыт по 1")
    return not bad, "итоги: выигравший вариант есть в маркете и закрыт по 1, сумма финальных цен 1", _short(bad) or f"итогов {n}"


TESTS = (settled_bets, bets_sane, snapshot_probs, ml_day, llm, metar_vs_iem, fast_sources, outcomes)


def run():
    c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    res = []
    for fn in TESTS:
        try:
            res.append(fn(c))
        except Exception as e:   # тест не смог отработать — это тоже провал
            res.append((False, f"тест данных {fn.__name__}", f"{type(e).__name__}: {e}"))
    c.close()
    return res


if __name__ == "__main__":
    for ok, name, detail in run():
        print(f"{'✓' if ok else '✗'} {name} — {detail}")
