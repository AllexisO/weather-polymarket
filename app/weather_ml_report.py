"""
Отчёт о ночном обучении — для страницы /training (2026-09-26, просьба
Alex: «хочу видеть и контролировать, что конкретно делала модель»).

Вызывается из weather_ml_live.train_and_save() после обучения v1/v2/v3.
Пишет одну строку в ml_train_log (JSON с подробностями):
- данные: сколько город-дней, за какой период, сколько новых с прошлой ночи;
- что обучено: версии, число строк и признаков, разброс ошибки v1 по городам;
- экзамен: v3 заново обучается БЕЗ последних EXAM_DAYS дней и сдаёт их как
  незнакомые — какой шанс давала правильному ответу (против рынка) и на
  сколько градусов ошибалась (против среднего 16 погодных моделей и рынка);
- на что модель смотрит больше всего (важность признаков медианы v3);
- проверки: хватает ли данных, свежие ли они, в порядке ли прогнозы.
"""

import json
import math
from datetime import date, datetime, timedelta, timezone

import numpy as np

import weather_ml as ml
import weather_ml_check as chk
import weather_ml_q as mq

EXAM_DAYS = 14


def _exam(conn, dfm):
    """v3 без последних EXAM_DAYS дней -> проверка на них."""
    last = date.fromisoformat(dfm["date"].max())
    cut = (last - timedelta(days=EXAM_DAYS - 1)).isoformat()
    tr, te = dfm[dfm["date"] < cut], dfm[dfm["date"] >= cut]
    if len(tr) < 1000 or te.empty:
        return None
    models = mq.train_q(tr)
    qs_all = mq.predict_q(models, te[ml.FEATURES], te["fc_mean"].values)
    win = {(r[0], r[1]): r[2] for r in conn.execute("SELECT city, local_date, win_lo FROM weather_poly_outcomes")}
    pm = pk = 0.0
    n_p = 0
    err_model, err_fc, err_mkt = [], [], []
    i50 = mq.QUANTILES.index(0.5)
    for (_, r), qs in zip(te.iterrows(), qs_all):
        err_model.append(abs(qs[i50] - r["actual_c"]))
        err_fc.append(abs(r["fc_mean"] - r["actual_c"]))
        if not math.isnan(r.get("mkt_mean_vs_fc", float("nan"))):
            err_mkt.append(abs(r["fc_mean"] + r["mkt_mean_vs_fc"] - r["actual_c"]))
        w = win.get((r["city"], r["date"]))
        pr = chk.prices(conn, r["city"], r["date"], "A") if w is not None else {}
        wb = next((b for b in pr if b[0] == w), None)
        if len(pr) < 3 or wb is None:
            continue
        P = {b: mq.bucket_prob(list(qs), r["unit"], b[0], b[1]) for b in pr}
        pm += P[wb] / (sum(P.values()) or 1.0)
        pk += pr[wb] / (sum(pr.values()) or 1.0)
        n_p += 1
    return {"from": cut, "to": last.isoformat(), "n": len(te), "n_train": len(tr), "n_prob": n_p,
            "p_model": 100 * pm / n_p if n_p else None, "p_market": 100 * pk / n_p if n_p else None,
            "err_model": float(np.mean(err_model)), "err_fc": float(np.mean(err_fc)),
            "err_market": float(np.mean(err_mkt)) if err_mkt else None}


def report(conn, started, df1, dfm, sigmas, qmodels_mkt):
    finished = datetime.now(timezone.utc)
    prev = None
    try:
        r = conn.execute("SELECT details FROM ml_train_log ORDER BY trained_at DESC LIMIT 1").fetchone()
        prev = json.loads(r[0]) if r else None
    except Exception:
        prev = None
    exam = _exam(conn, dfm)
    q50 = qmodels_mkt[0.5]
    gain = q50.feature_importance(importance_type="gain")
    names = q50.feature_name()
    tot = float(gain.sum()) or 1.0
    top = sorted(zip(names, gain), key=lambda x: -x[1])[:12]
    last_day = df1["date"].max()
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    sig = sorted(sigmas.values())
    data = {"rows": len(df1), "first": df1["date"].min(), "last": last_day, "cities": int(df1["city"].nunique()),
            "rows_mkt": int(dfm["mkt_mean_vs_fc"].notna().sum()) if "mkt_mean_vs_fc" in dfm else 0,
            "first_mkt": dfm.loc[dfm["mkt_mean_vs_fc"].notna(), "date"].min() if "mkt_mean_vs_fc" in dfm else None,
            "new_rows": len(df1) - prev["data"]["rows"] if prev else None,
            "prev_last": prev["data"]["last"] if prev else None}
    X = dfm[ml.FEATURES].tail(200)
    sample = mq.predict_q(qmodels_mkt, X, dfm["fc_mean"].tail(200).values)
    checks = [
        (data["rows"] >= (prev["data"]["rows"] if prev else 0), "данных не меньше, чем прошлой ночью",
         f"{data['rows']} город-дней" + (f" (было {prev['data']['rows']})" if prev else "")),
        (last_day >= (datetime.now(timezone.utc).date() - timedelta(days=3)).isoformat(), "в данных есть свежие дни",
         f"последний день с фактом — {last_day}" + ("" if last_day >= yesterday else " (факт приходит с задержкой до 1-2 дней)")),
        (data["cities"] >= 40, "в обучении все города", f"{data['cities']} из 48"),
        (bool(np.isfinite(sample).all()), "прогнозы без пустых значений", "проверено на 200 последних днях"),
        (exam is None or exam["err_model"] <= exam["err_fc"] + 0.05, "на экзамене модель не хуже простого среднего 16 моделей",
         f"ошибка {exam['err_model']:.2f}°C против {exam['err_fc']:.2f}°C" if exam else "экзамен не проведён"),
    ]
    details = {
        "started": started.isoformat(), "finished": finished.isoformat(),
        "duration_s": (finished - started).total_seconds(), "data": data,
        "versions": [
            {"key": "ml", "name": "v1 — одно число + разброс", "rows": len(df1), "features": len(ml.FEATURES) - 3,
             "note": f"разброс ошибки по городам {sig[0]:.2f}–{sig[-1]:.2f}°C" if sig else ""},
            {"key": "ml2", "name": "v2 — распределение (13 уровней)", "rows": len(df1), "features": len(ml.FEATURES) - 3,
             "note": "те же данные, учит весь разброс температуры"},
            {"key": "ml3", "name": "v3 — v2 + мнение рынка (главная)", "rows": len(dfm), "features": len(ml.FEATURES),
             "note": "все дни; цена рынка в 08:00 есть у {} из них (с {}), в остальные модель учится без неё".format(
                 data["rows_mkt"], data["first_mkt"])},
            {"key": "ml4", "name": "v4 — v3 с крупными деревьями (31 лист)", "rows": len(dfm), "features": len(ml.FEATURES),
             "note": "с 26.09: на проверке по месяцам точнее v3 (август 1.273 против 1.279, сентябрь 1.172 против 1.183)"},
            {"key": "ml4e", "name": "v4e — v4, среднее 3 обучений", "rows": len(dfm), "features": len(ml.FEATURES),
             "note": "с 27.09: три обучения с разными зёрнами, прогноз — среднее; на проверке точнее одного обучения везде"},
        ],
        "exam": exam,
        "importance": [{"name": n, "pct": 100 * g / tot} for n, g in top],
        "checks": [{"ok": bool(ok), "title": t, "detail": d} for ok, t, d in checks],
    }
    conn.execute("""CREATE TABLE IF NOT EXISTS ml_train_log (trained_at TEXT PRIMARY KEY, ok INTEGER, details TEXT)""")
    conn.execute("INSERT OR REPLACE INTO ml_train_log VALUES (?, ?, ?)",
                 (finished.isoformat(), int(all(c["ok"] for c in details["checks"])), json.dumps(details, ensure_ascii=False)))
    conn.commit()
    if exam:
        print(f"экзамен {exam['from']}..{exam['to']}: шанс правильному ответу модель "
              f"{exam['p_model'] or 0:.1f}% / рынок {exam['p_market'] or 0:.1f}%; ошибка {exam['err_model']:.2f}°C "
              f"(среднее моделей {exam['err_fc']:.2f}°C)")
    return details


if __name__ == "__main__":
    # вхолостую: обучить в памяти и записать отчёт, НЕ заменяя рабочие модели в data/ml
    import sqlite3
    started = datetime.now(timezone.utc)
    conn = sqlite3.connect(ml.DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    df = ml.build(conn)
    df = df[df["actual_c"].notna()]
    ml.FEATURES = ml.features(df)
    n1 = len(ml.FEATURES)
    sig, _ = ml.city_sigmas(df)
    ml.USE_MKT = True
    try:
        dfm = ml.build(conn)
        dfm = dfm[dfm["actual_c"].notna()]
        ml.FEATURES = ml.features(dfm)
        qm = mq.train_q(dfm)
        rep = report(conn, started, df, dfm, sig, qm)
        rep["versions"][0]["features"] = rep["versions"][1]["features"] = n1
        rep["dry_run"] = True
        conn.execute("UPDATE ml_train_log SET details = ? WHERE trained_at = (SELECT MAX(trained_at) FROM ml_train_log)",
                     (json.dumps(rep, ensure_ascii=False),))
        conn.commit()
    finally:
        ml.USE_MKT = False
    print(json.dumps({k: rep[k] for k in ("data", "exam", "checks")}, ensure_ascii=False, indent=1)[:3000])
