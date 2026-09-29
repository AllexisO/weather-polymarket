"""
Насколько каждая модель права по сравнению с рынком — данные для раздела
«Насколько модель права» на /paper (2026-09-25, просьба Alex: «видеть
глазами, насколько модель эффективна» — сначала для главной, потом для всех).

Для каждого города и дня: какой шанс модель и рынок давали варианту,
который в итоге выиграл (оба нормированы на сумму 1). Чей шанс выше —
тот был ближе к правде. Для дней, где по правилу кошелька была бы ставка
(перевес модели ≥10 п.п., цена 3-95¢; у ml_shift ещё сдвиг центра ≥0.5°C),
— шанс ставки по модели и по рынку и угадали ли (bet_*): кто прав именно
там, где модель спорит с рынком.

Источники:
- history — walk-forward проверка обучаемых моделей (каждую неделю модель
  обучалась только на прошлом): v3 — ml_preds_var_mkt, v2 — ml_preds_q,
  v1 — ml_preds_wf; рынок — последняя цена за час до 08:00 местного;
- live — живые прогнозы из snapshots: первый снимок дня до 12:00 местного
  с оценкой модели, рынок — цена из того же снимка. У формул (main, emos,
  mm) есть только живые дни (с 2026-08-22, до 23.09 — 6 городов).

Пишет таблицу ml_skill (перезаписывает). Крон 05:50: python weather_ml_skill.py
"""

import json
import os
import sqlite3
from pathlib import Path

import weather_ml_check as chk
import weather_ml_q as mq
from weather_edge import emos_bucket_prob

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))

# кошелёк -> (таблица истории или None, вид истории, поле живого снимка, мин. сдвиг центра °C для ставки)
MODELS = {
    "ml3": ("ml_preds_var_mkt", "q", "ml3_model_p", None),
    # смесь 35% v3 + 65% рынка (weather_ml_live.ML3_BLEND_W), ставка от 3 п.п.
    "ml3_cal": ("ml_preds_var_mkt", "q_blend", "ml3c_model_p", None),
    "ml2": ("ml_preds_q", "q", "ml2_model_p", None),
    "ml": ("ml_preds_wf", "norm", "ml_model_p", None),
    "ml_shift": ("ml_preds_wf", "norm", "ml_model_p", 0.5),
    # 2026-09-26: v4 — пока только живые дни (проверки вслепую по неделям для v4 нет)
    "ml4": (None, None, "ml4_model_p", None),
    "ml4_cal": (None, None, "ml4c_model_p", None),
    "ml4e": (None, None, "ml4e_model_p", None),
    "ml4e_cal": (None, None, "ml4ec_model_p", None),
    # 2026-09-29 (страница «Модели», просьба Alex): v5 «от рынка» и 6 ансамблей — тоже только живые дни
    "ml5": (None, None, "ml5_model_p", None),
    "ml5_cal": (None, None, "ml5c_model_p", None),
    "ens": (None, None, "ens_model_p", None),
    "main": (None, None, "model_p", None),
    "emos": (None, None, "emos_model_p", None),
    "mm": (None, None, "mm_model_p", None),
}


def center_c(probs, unit):
    tot = sum(probs.values()) or 1.0
    m = sum(((hi - 0.5) if lo <= -900 else ((lo + 0.5) if hi >= 900 else (lo + hi) / 2)) * p
            for (lo, hi), p in probs.items()) / tot
    return (m - 32) * 5 / 9 if unit == "fahrenheit" else m


MIN_EDGE = {"ml3_cal": 0.03, "ml4_cal": 0.03, "ml4e_cal": 0.03, "ml5_cal": 0.03, "ens": 0.03}


def row(key, city, date, source, unit, model, market, win_lo, min_shift):
    """model/market: {(lo, hi): шанс}. None, если выигравшего варианта нет в списке."""
    wb = next((b for b in market if b[0] == win_lo), None)
    if wb is None or wb not in model:
        return None
    tm, tk = sum(model.values()) or 1.0, sum(market.values()) or 1.0
    # ставка по правилу кошелька: максимум «модель − цена», перевес ≥10 п.п., цена 3-95¢ (цена — сырая)
    bb = max(market, key=lambda b: model.get(b, 0.0) - market[b])
    bet = model.get(bb, 0.0) - market[bb] >= MIN_EDGE.get(key, 0.10) and 0.03 <= market[bb] <= 0.95
    if bet and min_shift is not None:
        bet = abs(center_c(model, unit) - center_c(market, unit)) >= min_shift
    return (key, city, date, source, model[wb] / tm, market[wb] / tk,
            int(max(model, key=model.get) == wb), int(max(market, key=market.get) == wb),
            model.get(bb) if bet else None, market[bb] if bet else None, int(bb == wb) if bet else None)


def history(conn, key, table, kind, min_shift, win):
    out = []
    cols = "city, date, unit, qs" if kind in ("q", "q_blend") else "city, date, unit, ml_mu_c, ml_sigma_c"
    for r in conn.execute(f"SELECT {cols} FROM {table}").fetchall():
        city, date, unit = r[0], r[1], r[2]
        w = win.get((city, date))
        if w is None:
            continue
        pr = chk.prices(conn, city, date, "A")
        if len(pr) < 3:
            continue
        if kind in ("q", "q_blend"):
            q = json.loads(r[3])
            model = {b: mq.bucket_prob(q, unit, b[0], b[1]) for b in pr}
            if kind == "q_blend":
                from weather_ml_live import blend_with_market
                keys = list(pr)
                tm = sum(model.values()) or 1.0
                model = dict(zip(keys, blend_with_market([model[b] / tm for b in keys], [pr[b] for b in keys])))
        else:
            k, off = (9 / 5, 32) if unit == "fahrenheit" else (1, 0)
            model = {b: emos_bucket_prob(r[3] * k + off, r[4] * k, b[0], b[1]) for b in pr}
        x = row(key, city, date, "history", unit, model, pr, w, min_shift)
        if x:
            out.append(x)
    return out


FAST_FIELDS = {"ml_model_p", "ml2_model_p", "ml3_model_p", "ml3c_model_p", "ml4_model_p", "ml4c_model_p", "ml4e_model_p", "ml4ec_model_p", "ml5_model_p", "ml5c_model_p"}


def live(conn, key, field, after, min_shift, win):
    """Как решают кошельки: самый ранний снимок дня с оценкой модели — обычный или быстрый
    (snapshots_fast, weather_ml_fast.py, с 2026-09-26)."""
    out = []
    first = {}
    tables = ["snapshots"]
    if field in FAST_FIELDS and conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'snapshots_fast'").fetchone():
        tables.append("snapshots_fast")
    for tbl in tables:
        for city, date, ts in conn.execute(
                f"""SELECT city, local_date, MIN(ts_utc) FROM {tbl} WHERE {field} IS NOT NULL AND local_hour < 12
                    AND local_date > ? GROUP BY city, local_date""", (after,)).fetchall():
            if (city, date) not in first or ts < first[(city, date)][0]:
                first[(city, date)] = (ts, tbl)
    for (city, date), (ts, tbl) in sorted(first.items()):
        w = win.get((city, date))
        if w is None:
            continue
        bs = conn.execute(f"SELECT bucket_lo, bucket_hi, {field}, market_p, unit FROM {tbl} WHERE city = ? AND ts_utc = ?",
                          (city, ts)).fetchall()
        x = row(key, city, date, "live", bs[0][4], {(b[0], b[1]): b[2] or 0.0 for b in bs},
                {(b[0], b[1]): b[3] or 0.0 for b in bs}, w, min_shift)
        if x:
            out.append(x)
    return out


def main():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    win = {(r[0], r[1]): r[2] for r in conn.execute("SELECT city, local_date, win_lo FROM weather_poly_outcomes")}
    out = []
    for key, (table, kind, field, min_shift) in MODELS.items():
        rows = history(conn, key, table, kind, min_shift, win) if table else []
        rows += live(conn, key, field, max((r[2] for r in rows), default=""), min_shift, win)
        out += rows
        n_live = sum(r[3] == "live" for r in rows)
        pm = sum(r[4] for r in rows) / max(len(rows), 1)
        pk = sum(r[5] for r in rows) / max(len(rows), 1)
        bets = [r for r in rows if r[10] is not None]
        print(f"{key}: {len(rows)} город-дней (история {len(rows) - n_live}, вживую {n_live}); "
              f"шанс правильному ответу: модель {pm:.1%}, рынок {pk:.1%}; ставок {len(bets)}, угадано "
              f"{sum(r[10] for r in bets)}, рынок ожидал {sum(r[9] for r in bets):.0f}, модель {sum(r[8] for r in bets):.0f}")
    conn.execute("DROP TABLE IF EXISTS ml_skill")
    conn.execute("""CREATE TABLE ml_skill (model TEXT, city TEXT, date TEXT, source TEXT, p_model REAL, p_market REAL,
                    hit_model INTEGER, hit_market INTEGER, bet_p_model REAL, bet_p_market REAL, bet_won INTEGER)""")
    conn.executemany("INSERT INTO ml_skill VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", out)
    conn.commit()
    from jobmark import mark
    mark(conn, "weather_ml_skill")
    conn.close()


if __name__ == "__main__":
    main()
