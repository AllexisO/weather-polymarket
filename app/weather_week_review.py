"""
Разбор ошибок модели за неделю (2026-09-29, план Alex: раз в неделю, в воскресенье вечером, собрать неделю,
найти, где модель ошиблась, и дать ей это учесть при следующем обучении).

Модель и так переобучается каждую ночь на всей истории, включая прошлую неделю, — отдельные ошибки она уже «видит».
Разбор ищет ПОВТОРЯЮЩИЕСЯ ошибки (город, тип дня, уверенность рынка, сторона промаха), которые модель сама
не исправляет, — из них делаем признак или поправку, проверяем по правилам (docs/RESEARCHER.md) и, если прошло,
добавляем в обучение.

Данные: решение в 08:00 — v3 (ml3_model_p), смесь 35/65 с рынком (ml3c_model_p), цена рынка, итог Polymarket.
До 24.09 — честный прогон по истории (ml_preds_var_mkt), с 25.09 — живые утренние снимки.
Запуск — только на копии (сначала свежая копия: weather_research.py):
  docker compose run --rm -e JOB_TIMEOUT=0 -e POLY_LAB_DB=/data/research/research.sqlite3 collector weather_week_review.py [конец_недели]
"""
import math
import statistics as st
import sys
from collections import defaultdict
from datetime import date, timedelta

import weather_study_0926 as base
from weather_cities import OBS_CITIES
from weather_ml_live import blend_with_market

conn = base.conn


def live_days(since):
    """Живые утренние решения: самый ранний снимок до полудня с прогнозом v3 (быстрый, потом обычный)."""
    out = {}
    for table in ("snapshots", "snapshots_fast"):
        try:
            rows = conn.execute(f"""
                SELECT s.city, s.local_date, s.unit, s.bucket_lo, s.market_p, s.ml3_model_p, s.ml3c_model_p
                FROM {table} s JOIN (SELECT city, local_date, MIN(ts_utc) AS ts FROM {table}
                                     WHERE local_date >= ? AND local_hour < 12 AND ml3_model_p IS NOT NULL
                                     GROUP BY city, local_date) f
                  ON f.city = s.city AND f.local_date = s.local_date AND f.ts = s.ts_utc""", (since,)).fetchall()
        except Exception:
            continue
        g = defaultdict(list)
        for r in rows:
            g[(r[0], r[1])].append(r)
        for (city, d), rs in g.items():  # быстрый снимок (08:00) важнее обычного — он идёт вторым и перезаписывает
            out[(city, d)] = rs
    days = []
    for (city, d), rs in out.items():
        w = base.WIN.get((city, d))
        rs = sorted(rs, key=lambda r: r[3])
        if w is None or not any(r[3] == w for r in rs) or len(rs) < 3:
            continue
        keys = [(r[3],) for r in rs]
        price = [r[4] or 0.0 for r in rs]
        raw = [r[5] or 0.0 for r in rs]
        t = sum(raw) or 1.0
        raw = [x / t for x in raw]
        bl = [r[6] for r in rs]
        if any(x is None for x in bl):
            bl = blend_with_market(raw, price)
        days.append({"city": city, "date": d, "keys": keys, "price": price, "raw": dict(zip(keys, raw)),
                     "blend": dict(zip(keys, bl)), "win": w})
    return days


def med(ps):
    t, c = sum(ps) or 1.0, 0.0
    for i, p in enumerate(ps):
        c += p / t
        if c >= 0.5:
            return i
    return len(ps) - 1


def score(d):
    ks = d["keys"]
    i = [k[0] for k in ks].index(d["win"])
    t = sum(d["price"]) or 1.0
    pm = d["price"][i] / t
    pb, pr = d["blend"][ks[i]], d["raw"][ks[i]]
    return {"ll_b": -math.log(max(pb, 1e-4)), "ll_r": -math.log(max(pr, 1e-4)), "ll_m": -math.log(max(pm, 1e-4)),
            "pb": pb, "pm": pm, "fav": max(d["price"]) / t,
            # промах середины в вариантах: + модель теплее факта, − холоднее
            "miss_r": med([d["raw"][k] for k in ks]) - i, "miss_m": med(d["price"]) - i}


def line(name, ds):
    if not ds:
        return f"  {name:28s} —"
    s = [score(d) for d in ds]
    gap = st.mean(x["ll_m"] - x["ll_b"] for x in s)
    return (f"  {name:28s} {len(s):4d} дн. | смесь лучше рынка на {gap:+.3f} | v3 {st.mean(x['ll_r'] for x in s):.3f} "
            f"смесь {st.mean(x['ll_b'] for x in s):.3f} рынок {st.mean(x['ll_m'] for x in s):.3f} | "
            f"сдвиг v3 {st.mean(x['miss_r'] for x in s):+.2f} вар., рынка {st.mean(x['miss_m'] for x in s):+.2f}")


def main():
    end = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today() - timedelta(days=1)
    wk0, base0 = end - timedelta(days=6), end - timedelta(days=34)
    hist = [d for d in base.load_days() if d["date"] < "2026-09-25"]
    days = [d for d in hist + live_days("2026-09-25") if base0.isoformat() <= d["date"] <= end.isoformat()]
    week = [d for d in days if d["date"] >= wk0.isoformat()]
    prev = [d for d in days if d["date"] < wk0.isoformat()]
    print(f"=== Разбор недели {wk0:%d.%m}–{end:%d.%m} (для сравнения — 4 недели до неё) ===")
    print("логошибка — меньше лучше; «сдвиг» — насколько середина прогноза выше (+) или ниже (−) итога, в вариантах\n")
    print(line("неделя", week))
    print(line("4 недели до", prev))

    print("\n--- по уверенности рынка (цена фаворита) ---")
    for lbl, lo, hi in (("рынок не уверен (<35¢)", 0, .35), ("средне (35-60¢)", .35, .60), ("уверен (≥60¢)", .60, 2)):
        f = lambda ds: [d for d in ds if lo <= score(d)["fav"] < hi]
        print(line(lbl + ", неделя", f(week)))
        print(line(lbl + ", 4 нед.", f(prev)))

    print("\n--- по единицам ---")
    for u in ("°C", "°F"):
        is_f = lambda d: OBS_CITIES[d["city"]]["unit"].upper().startswith("F")
        f = lambda ds: [d for d in ds if is_f(d) == (u == "°F")]
        print(line(u + ", 5 недель", f(days)))

    print("\n--- города: где смесь хуже рынка и куда промахивается v3 (5 недель, ≥ 10 дней) ---")
    by = defaultdict(list)
    for d in days:
        by[d["city"]].append(d)
    rows = []
    for c, ds in by.items():
        if len(ds) < 10:
            continue
        s = [score(d) for d in ds]
        wk = [score(d) for d in ds if d["date"] >= wk0.isoformat()]
        miss = [x["miss_r"] for x in s]
        same = max(sum(m > 0 for m in miss), sum(m < 0 for m in miss)) / len(miss)
        rows.append((st.mean(x["ll_m"] - x["ll_b"] for x in s), c, len(s), st.mean(miss), same,
                     st.mean(x["ll_m"] - x["ll_b"] for x in wk) if wk else None))
    rows.sort()
    for gap, c, n, mb, same, gw in rows[:10]:
        flag = "  ← систематически " + ("теплее" if mb > 0 else "холоднее") if abs(mb) >= 0.4 and same >= 0.65 else ""
        print(f"  {c:14s} {n:3d} дн. | смесь vs рынок {gap:+.3f} (неделя {'—' if gw is None else f'{gw:+.3f}'}) | "
              f"сдвиг v3 {mb:+.2f} вар., в одну сторону {same * 100:.0f}%{flag}")
    sys_rows = [r for r in rows if abs(r[3]) >= 0.4 and r[4] >= 0.65]
    print(f"  систематический сдвиг (|сдвиг| ≥ 0.4 варианта и ≥ 65% дней в одну сторону): "
          + (", ".join(f"{r[1]} {r[3]:+.2f}" for r in sys_rows) or "нет"))

    print("\n--- самые дорогие ошибки недели (смесь дала итогу меньше всего по сравнению с рынком) ---")
    worst = sorted(week, key=lambda d: score(d)["ll_b"] - score(d)["ll_m"], reverse=True)[:10]
    for d in worst:
        s = score(d)
        print(f"  {d['date']} {d['city']:14s} итог {d['win']:g}: смесь {s['pb'] * 100:4.1f}%, рынок {s['pm'] * 100:4.1f}%, "
              f"середина v3 {'выше' if s['miss_r'] > 0 else 'ниже' if s['miss_r'] < 0 else 'в точку'}"
              + (f" на {abs(s['miss_r'])} вар." if s["miss_r"] else ""))
    v5_section(wk0.isoformat(), end.isoformat())
    conn.row_factory = __import__("sqlite3").Row  # ml.build читает строки по именам
    import weather_why
    weather_why.explain(conn, wk0, end)


def v5_section(d0, d1):
    """2026-09-29: v5 «от рынка» против v3 и рынка на живых утренних снимках (где у v5 есть оценка)."""
    acc = defaultdict(list)
    for table in ("snapshots", "snapshots_fast"):
        try:
            rows = conn.execute(f"""
                SELECT s.city, s.local_date, s.bucket_lo, s.market_p, s.ml3_model_p, s.ml5_model_p, s.ml5c_model_p, s.ml3c_model_p
                FROM {table} s JOIN (SELECT city, local_date, MIN(ts_utc) AS ts FROM {table}
                                     WHERE local_date BETWEEN ? AND ? AND local_hour < 12 AND ml5_model_p IS NOT NULL
                                     GROUP BY city, local_date) f
                  ON f.city = s.city AND f.local_date = s.local_date AND f.ts = s.ts_utc""", (d0, d1)).fetchall()
        except Exception:
            continue
        g = defaultdict(list)
        for r in rows:
            g[(r[0], r[1])].append(r)
        for k, rs in g.items():
            acc[k] = rs
    out = defaultdict(list)
    for (city, d), rs in acc.items():
        w = base.WIN.get((city, d))
        if w is None or not any(r[2] == w for r in rs):
            continue
        for j, name in ((3, "рынок"), (4, "v3"), (5, "v5"), (7, "смесь v3"), (6, "смесь v5")):
            t = sum(r[j] or 0 for r in rs) or 1.0
            p = [(r[j] or 0) / t for r in rs if r[2] == w][0]
            out[name].append(-math.log(max(p, 1e-4)))
    if not out:
        print("\n--- v5 «от рынка»: живых дней с итогом пока нет ---")
        return
    n = len(out["рынок"])
    print(f"\n--- v5 «от рынка» против v3 и рынка, живые дни ({n} город-дней) ---")
    print("  " + " | ".join(f"{k} {st.mean(v):.3f}" for k, v in out.items())
          + f"  → отставание от рынка: v3 {st.mean(out['v3']) - st.mean(out['рынок']):+.3f}, v5 {st.mean(out['v5']) - st.mean(out['рынок']):+.3f}")


if __name__ == "__main__":
    main()
