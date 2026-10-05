"""
Проверка всей системы — для страницы /audit (2026-09-27, просьба Alex: «чтобы всё это
было на сайте»). То же, что ручной аудит 27.09, но по крону:

- кошельки: каждая ставка против правил кошелька (перевес, цена 3-95¢, потолок цены,
  комиссия, доли × цена), итог против официального результата Polymarket, выплата,
  одна ставка на город в день, зависшие ставки и заявки, ни разу не поставили больше,
  чем было на счёте; copy — только накануне и не дороже их цены + 2¢;
- модели: у скольких городов за сутки есть прогноз, шансы в 0-1 и в сумме 1;
- данные: снимки по расписанию, факт и итоги за вчера/позавчера, совпадение факта станции
  с итогом Polymarket за 30 дней, последнее обучение и его проверки;
- крон: запуски и ошибки за сутки (job_log);
- база: режим WAL, целостность файла (PRAGMA quick_check — только с --deep, ~1 мин;
  между глубокими проверками показывается последний результат).

Пишет строку в audit_log (JSON); нарушения видит weather_alerts.py → красная плашка.
Крон: каждые 2 ч в :15; с --deep — раз в сутки ночью. Только чтение + одна короткая запись.
"""

import json
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import weather_paper as wp
from jobmark import mark
from weather_cities import OBS_CITIES

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
MODEL_COLS = [("model_p", "Основная формула (GFS+ICON)"), ("emos_model_p", "EMOS"), ("mm_model_p", "Микс 16 моделей"),
              ("ml_model_p", "v1"), ("ml2_model_p", "v2"), ("ml3_model_p", "v3 — главная"), ("ml3c_model_p", "v3 + рынок"),
              ("ml4_model_p", "v4"), ("ml4c_model_p", "v4 + рынок"), ("ml4e_model_p", "v4e"), ("ml4ec_model_p", "v4e + рынок")]


def _dt(s):
    if not s:
        return None
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _next_paper_run(t):
    """Старые ставки без времени покупки: покупка — в ближайший запуск weather_paper (:10 чётного часа)."""
    r = t.replace(minute=10, second=0, microsecond=0)
    while r <= t or r.hour % 2:
        r += timedelta(hours=1)
    return r


def _same(a, b):
    return abs(a - b) < 1e-6


def check_wallets(c, out, now):
    rows = [dict(r) for r in c.execute("SELECT * FROM paper_trades")]
    taker = set(wp.WALLETS) | set(wp.NO_WALLETS)
    maker = set(wp.MAKER_WALLETS)
    by_w = defaultdict(list)
    for r in rows:
        by_w[r["wallet"]].append(r)
    wallets, violations, n_bets = [], [], 0
    for w, rs in sorted(by_w.items()):
        bets = [r for r in rs if r["status"] in ("open", "resting", "won", "lost", "void")]
        closed = [r for r in bets if r["status"] in ("won", "lost", "void")]
        n_bets += len(bets)
        v = []
        for (city, d), n in Counter((r["city"], r["local_date"]) for r in rs).items():
            if n > 1:
                v.append(f"две записи на {city} {d}")
        thr = wp.EDGE_BY_WALLET.get(w, wp.MIN_EDGE)
        for r in bets:
            tag = f"{r['city']} {r['local_date']}"
            if w in taker and r["status"] != "resting" and r["model_p"] is not None and r["market_p"] is not None:
                if r["model_p"] - r["market_p"] < thr - 1e-9:
                    v.append(f"{tag}: перевес {100 * (r['model_p'] - r['market_p']):.1f} п.п. меньше порога {100 * thr:.0f}")
                if not (wp.MIN_PRICE_BY_WALLET.get(w, wp.MIN_PRICE) - 1e-9 <= r["market_p"] <= wp.MAX_PRICE + 1e-9):
                    v.append(f"{tag}: цена рынка {100 * r['market_p']:.0f}¢ вне 3-95¢")
                # 03.10: «да» дешевле 10¢ кошельки моделей не покупают (ставки с этого момента)
                if (w not in wp.NO_WALLETS and w not in wp.YES_BAND and _dt(r["placed_at"] or r["snapshot_ts"]) >= _dt(wp.YES_MIN_FROM)
                        and r["market_p"] < wp.YES_MIN - 1e-9):
                    v.append(f"{tag}: «да» за {100 * r['market_p']:.0f}¢ — дешевле {100 * wp.YES_MIN:.0f}¢ (правило с 03.10)")
                if w in wp.YES_BAND and not (wp.YES_BAND[w][0] - 1e-9 <= r["market_p"] < wp.YES_BAND[w][1] + 1e-9):
                    v.append(f"{tag}: вариант стоил {100 * r['market_p']:.0f}¢ — вне полосы кошелька")
                if w in wp.NO_BAND and not (wp.NO_BAND[w][0] - 1e-9 <= 1 - r["market_p"] < wp.NO_BAND[w][1] + 1e-9):
                    v.append(f"{tag}: вариант стоил {100 * (1 - r['market_p']):.0f}¢ — вне полосы кошелька")
                if r["price"] is not None and r["price"] > min(wp.MAX_PRICE, r["model_p"] - thr) + 0.005:
                    v.append(f"{tag}: купили по {100 * r['price']:.1f}¢ дороже потолка {100 * (r['model_p'] - thr):.1f}¢")
            if w == "copy" and r["market_p"] is not None and r["price"] > r["market_p"] + 0.02 + 1e-6:
                v.append(f"{tag}: дороже цены трейдера + 2¢")
            if w == "copy":
                cfg = OBS_CITIES.get(r["city"])
                if cfg and _dt(r["snapshot_ts"]).astimezone(ZoneInfo(cfg["tz"])).date().isoformat() >= r["local_date"]:
                    v.append(f"{tag}: куплено не накануне дня маркета")
            if r["status"] != "resting" and w not in maker and r["shares"] and r["price"]:
                exp = r["shares"] * 0.05 * r["price"] * (1 - r["price"])
                if abs((r["fee"] or 0) - exp) > 0.02 + 0.05 * exp:
                    v.append(f"{tag}: комиссия ${r['fee'] or 0:.3f} вместо ${exp:.3f}")
            if r["status"] in ("open", "won", "lost", "void") and r["shares"] and r["price"] \
                    and abs(r["shares"] * r["price"] - r["stake"]) > 0.03:
                v.append(f"{tag}: доли × цена ≠ ставка")
            if r["status"] in ("open", "resting"):
                cfg = OBS_CITIES.get(r["city"])
                tz = ZoneInfo(cfg["tz"]) if cfg else timezone.utc
                day0 = datetime.combine(date.fromisoformat(r["local_date"]), datetime.min.time(), tz)
                if r["status"] == "open" and now - (day0 + timedelta(days=1)) > timedelta(hours=12):
                    v.append(f"{tag}: ставка открыта {(now - day0 - timedelta(days=1)).total_seconds() / 3600:.0f} ч после конца дня")
                if r["status"] == "resting" and now - (day0 + timedelta(hours=wp.MAKER_CUTOFF_HOUR)) > timedelta(hours=3):
                    v.append(f"{tag}: заявка висит после полудня")
        for r in closed:
            tag = f"{r['city']} {r['local_date']}"
            if r["status"] == "void":
                if abs((r["payout"] or 0) - 0.5 * (r["shares"] or 0)) > 0.02:
                    v.append(f"{tag}: при отмене выплата ${r['payout'] or 0:.2f}")
                continue
            o = out.get((r["city"], r["local_date"]))
            if o is None:
                # ставки закрываются по финальной цене сразу, а итоги пишет weather_poly_resolve раз в 2 ч —
                # нарушение, только если итога нет дольше 6 ч
                if now - _dt(r["settled_at"]) > timedelta(hours=6):
                    v.append(f"{tag}: закрыта больше 6 ч назад, а официального итога нет")
                continue
            hit = _same(r["bucket_lo"], o[0]) and _same(r["bucket_hi"], o[1])
            if (r["status"] == "won") != (hit if (r.get("side") or "yes") == "yes" else not hit):
                v.append(f"{tag}: записано «{r['status']}», а по итогу Polymarket наоборот")
            exp = (r["shares"] or 0) if r["status"] == "won" else 0.0
            if abs((r["payout"] or 0) - exp) > 0.02:
                v.append(f"{tag}: выплата ${r['payout'] or 0:.2f} вместо ${exp:.2f}")
        start = wp.start_balance(w)
        ev = []
        for r in bets:
            ev.append((_dt(r["placed_at"]) if r["placed_at"] else _next_paper_run(_dt(r["snapshot_ts"])), 1, r["stake"], r["stake"] + (r["fee"] or 0)))
            if r["settled_at"]:
                ev.append((_dt(r["settled_at"]), 0, 0, -(r["payout"] or 0)))
        bal, low = start, start
        for t, is_buy, stake, cost in sorted(ev, key=lambda x: (x[0], x[1])):
            if is_buy and bal < stake - 1e-9:
                v.append(f"поставили ${stake:.2f} при остатке ${bal:.2f} ({t:%d.%m %H:%M})")
            bal -= cost
            low = min(low, bal)
        staked = sum(r["stake"] + (r["fee"] or 0) for r in closed)
        pnl = sum((r["payout"] or 0) - r["stake"] - (r["fee"] or 0) for r in closed)
        in_play = sum(r["stake"] + (r["fee"] or 0) for r in bets if r["status"] in ("open", "resting"))
        wallets.append({"key": w, "bets": len(bets), "open": len(bets) - len(closed), "closed": len(closed),
                        "won": sum(r["status"] == "won" for r in closed), "pnl": pnl, "roi": 100 * pnl / staked if staked else None,
                        "cash": bal, "in_play": in_play, "low": low, "start": start,
                        "nofill": sum(r["status"] == "nofill" for r in rs), "violations": v[:20], "n_viol": len(v)})
        violations += [f"{w}: {x}" for x in v]
    # кошельки по замерам
    orow = [dict(r) for r in c.execute("SELECT * FROM paper_obs_trades")] if \
        c.execute("SELECT 1 FROM sqlite_master WHERE name = 'paper_obs_trades'").fetchone() else []
    for w in ("obs", "obs_fmi", "obs_fast", "obs_rt", "obs_wethr"):
        rs = [r for r in orow if r.get("wallet", "obs") == w]
        bets = [r for r in rs if r["status"] in ("open", "won", "lost", "void")]
        closed = [r for r in bets if r["status"] != "open"]
        n_bets += len(bets)
        v = []
        for r in bets:
            if r["bucket_hi"] >= r["obs_max"]:
                v.append(f"{r['city']} {r['local_date']}: ставка против ещё возможного варианта")
        for r in closed:
            o = out.get((r["city"], r["local_date"]))
            if r["status"] != "void" and o is not None:
                hit = _same(r["bucket_lo"], o[0]) and _same(r["bucket_hi"], o[1])
                if (r["status"] == "won") == hit:
                    v.append(f"{r['city']} {r['local_date']}: записано «{r['status']}», а по итогу наоборот")
        staked = sum(r["stake"] + (r["fee"] or 0) for r in closed)
        pnl = sum((r["payout"] or 0) - r["stake"] - (r["fee"] or 0) for r in closed)
        spent = sum(r["stake"] + (r["fee"] or 0) for r in bets)
        wallets.append({"key": w, "bets": len(bets), "open": len(bets) - len(closed), "closed": len(closed),
                        "won": sum(r["status"] == "won" for r in closed), "pnl": pnl, "roi": 100 * pnl / staked if staked else None,
                        "cash": 100 - spent + sum(r["payout"] or 0 for r in closed),
                        "in_play": sum(r["stake"] + (r["fee"] or 0) for r in bets if r["status"] == "open"), "low": None, "start": 100.0,
                        "nofill": sum(r["status"] == "nofill" for r in rs), "violations": v[:20], "n_viol": len(v)})
        violations += [f"{w}: {x}" for x in v]
    return wallets, violations, n_bets


def check_models(c, now):
    cols = {r[1] for r in c.execute("PRAGMA table_info(snapshots)")}
    mcols = [(k, n) for k, n in MODEL_COLS if k in cols]
    since = (now - timedelta(hours=26)).isoformat()
    groups = defaultdict(list)
    for r in c.execute(f"SELECT ts_utc, city, local_date, {', '.join(k for k, _ in mcols)} FROM snapshots WHERE ts_utc >= ?", (since,)):
        groups[(r["ts_utc"][:16], r["city"], r["local_date"])].append(dict(r))
    out = []
    for k, name in mcols:
        cities, bad_sum, bad_rng, n = set(), 0, 0, 0
        for g, rs in groups.items():
            vals = [r[k] for r in rs if r[k] is not None]
            if not vals:
                continue
            n += 1
            cities.add(g[1])
            bad_rng += any(not (0 <= x <= 1) for x in vals)
            bad_sum += abs(sum(vals) - 1) > 0.02
        out.append({"key": k, "name": name, "cities": len(cities), "snaps": n, "bad_sum": bad_sum, "bad_range": bad_rng})
    return out


def check_data(c, out, now):
    since = (now - timedelta(hours=26)).isoformat()
    hours = sorted({r[0][:13] for r in c.execute("SELECT ts_utc FROM snapshots WHERE ts_utc >= ?", (since,))})
    gaps = []
    for a, b in zip(hours, hours[1:]):
        h = (datetime.fromisoformat(b + ":00") - datetime.fromisoformat(a + ":00")).total_seconds() / 3600
        if h > 2.5:
            gaps.append(f"{a[8:10]}.{a[5:7]} {a[11:13]}:00 → {b[8:10]}.{b[5:7]} {b[11:13]}:00 UTC")
    last = c.execute("SELECT MAX(ts_utc) FROM snapshots").fetchone()[0]
    last_cities = c.execute("SELECT COUNT(DISTINCT city) FROM snapshots WHERE ts_utc = ?", (last,)).fetchone()[0] if last else 0
    days = [(now - timedelta(days=i)).date().isoformat() for i in (2, 1)]
    fact_n = {d: c.execute("SELECT COUNT(*) FROM weather_station_daily WHERE local_date = ?", (d,)).fetchone()[0] for d in days}
    out_n = {d: c.execute("SELECT COUNT(*) FROM weather_poly_outcomes WHERE local_date = ?", (d,)).fetchone()[0] for d in days}
    # 02.10: сколько городов в этот день вообще имели маркет (Polymarket перестал выставлять Чжэнчжоу после 01.10) — не 48 жёстко
    mkt_n = {d: c.execute("SELECT COUNT(DISTINCT city) FROM snapshots WHERE local_date = ?", (d,)).fetchone()[0] for d in days}
    fact = {(r[0], r[1]): r[2] for r in c.execute("SELECT city, local_date, actual_max FROM weather_station_daily WHERE local_date >= ?",
                                                    ((now - timedelta(days=30)).date().isoformat(),))}
    agree = tot = 0
    for k, f in fact.items():
        if f is None or k not in out:
            continue
        lo, hi = out[k]
        tot += 1
        agree += (lo < f <= hi) or (lo < round(f) <= hi) or (lo <= -900 and f <= hi) or (hi >= 900 and f > lo)
    tr = c.execute("SELECT MAX(local_date) FROM poly_trades_days").fetchone()[0]
    train = None
    r = c.execute("SELECT trained_at, ok, details FROM ml_train_log WHERE details NOT LIKE '%\"dry_run\": true%' "
                  "ORDER BY trained_at DESC LIMIT 1").fetchone()
    if r:
        d = json.loads(r["details"])
        train = {"at": r["trained_at"], "ok": bool(r["ok"]), "checks": d.get("checks", []),
                 "exam": d.get("exam"), "rows": d.get("data", {}).get("rows")}
    return {"snap_hours": len(hours), "gaps": gaps, "last_snap": last, "last_snap_cities": last_cities,
            "fact_n": fact_n, "out_n": out_n, "mkt_n": mkt_n, "agree": [agree, tot], "trades_last_day": tr, "train": train}


def check_cron(c, now):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name = 'job_log'").fetchone():
        return {"runs": 0, "fails": 0, "since": None, "failed": []}
    day = (now - timedelta(days=1)).isoformat()
    n = c.execute("SELECT COUNT(*) FROM job_log WHERE finished_at >= ?", (day,)).fetchone()[0]
    # 2026-09-28: исправленные падения (fixes.py) не считаем; текст — по-человечески
    from fixes import failures, is_fixed, last_fixes
    from jobs_info import JOBS
    fx = last_fixes(c)
    f = sum(1 for job, t in c.execute("SELECT job, finished_at FROM job_log WHERE finished_at >= ? AND rc != 0", (day,))
            if not is_fixed(fx, job, t))
    failed, _fixed = failures(c, now - timedelta(days=1), {k: label for k, label, *_ in JOBS})
    first = c.execute("SELECT MIN(started_at) FROM job_log").fetchone()[0]
    alerts = c.execute("SELECT COUNT(*) FROM alerts WHERE resolved_at IS NULL").fetchone()[0] if \
        c.execute("SELECT 1 FROM sqlite_master WHERE name = 'alerts'").fetchone() else 0
    return {"runs": n, "fails": f, "since": first, "failed": failed, "alerts": alerts}


def run(deep=False):
    t0 = time.time()
    now = datetime.now(timezone.utc)
    c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    out = {(r["city"], r["local_date"]): (r["win_lo"], r["win_hi"]) for r in c.execute("SELECT * FROM weather_poly_outcomes")}
    wallets, violations, n_bets = check_wallets(c, out, now)
    models = check_models(c, now)
    data = check_data(c, out, now)
    cron = check_cron(c, now)
    db = {"journal": c.execute("PRAGMA journal_mode").fetchone()[0], "size": DB_PATH.stat().st_size}
    if deep:
        t1 = time.time()
        db["quick_check"] = c.execute("PRAGMA quick_check").fetchone()[0]
        db["quick_check_at"] = now.isoformat()
        db["quick_check_s"] = round(time.time() - t1)
    c.close()
    violations += [f"модель {m['name']}: шансы не в сумме 1 в {m['bad_sum']} снимках" for m in models if m["bad_sum"]]
    violations += [f"модель {m['name']}: шансы вне 0-1 в {m['bad_range']} снимках" for m in models if m["bad_range"]]
    if deep and db["quick_check"] != "ok":
        violations.append(f"целостность базы: {db['quick_check']}")
    # внимание (не ошибки кода, но требуют решения)
    attention = []
    for w in wallets:
        if w["key"] not in ("obs", "obs_fmi", "obs_fast", "obs_rt", "obs_wethr") and w["cash"] < wp.STAKE and w["in_play"] < 1e-6:
            attention.append(f"{w['key']}: денег ${w['cash']:.2f} и нет открытых ставок — кошелёк остановился")
        elif w["key"] not in ("obs", "obs_fmi", "obs_fast", "obs_rt", "obs_wethr") and w["cash"] < wp.STAKE:
            attention.append(f"{w['key']}: свободно ${w['cash']:.2f} — меньше ставки, новых ставок не делает")
    if data["gaps"]:
        attention.append("пропуски снимков: " + "; ".join(data["gaps"]))
    # 2026-09-28: только не исправленные ошибки и по-человечески (fixes.py)
    try:
        from fixes import failures
        from jobs_info import JOBS
        cc = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
        open_, fixed = failures(cc, now - timedelta(days=1), {k: label for k, label, *_ in JOBS})
        cc.close()
    except sqlite3.Error:
        open_, fixed = [], []
    attention += open_
    cron["fixed"] = fixed
    res = {"run_at": now.isoformat(), "deep": deep, "took_s": round(time.time() - t0, 1), "n_bets": n_bets,
           "violations": violations[:100], "n_violations": len(violations), "attention": attention,
           "wallets": wallets, "models": models, "data": data, "cron": cron, "db": db}
    w = sqlite3.connect(DB_PATH, timeout=60)
    try:
        w.execute("CREATE TABLE IF NOT EXISTS audit_log (run_at TEXT PRIMARY KEY, ok INTEGER, deep INTEGER, details TEXT)")
        if not deep:  # целостность — из последней глубокой проверки
            prev = w.execute("SELECT details FROM audit_log WHERE deep = 1 ORDER BY run_at DESC LIMIT 1").fetchone()
            if prev:
                pdb = json.loads(prev[0])["db"]
                for k in ("quick_check", "quick_check_at", "quick_check_s"):
                    if k in pdb:
                        db[k] = pdb[k]
        w.execute("INSERT OR REPLACE INTO audit_log VALUES (?, ?, ?, ?)",
                  (now.isoformat(), int(not violations), int(deep), json.dumps(res, ensure_ascii=False, default=str)))
        w.execute("DELETE FROM audit_log WHERE run_at < ?", ((now - timedelta(days=30)).isoformat(),))
        mark(w, "weather_audit")
        w.commit()
    except Exception:
        w.rollback()
        raise
    finally:
        w.close()
    print(f"проверка: ставок {n_bets}, нарушений {len(violations)}, внимание {len(attention)}, за {res['took_s']} с"
          + (f", целостность базы: {db.get('quick_check')}" if deep else ""))
    for x in violations[:20]:
        print("  НАРУШЕНИЕ:", x)
    for x in attention:
        print("  внимание:", x)


if __name__ == "__main__":
    run(deep="--deep" in sys.argv)
