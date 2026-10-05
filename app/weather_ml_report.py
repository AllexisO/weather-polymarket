"""
Отчёт о ночном обучении — для страницы /training (2026-09-26, просьба
Alex: «хочу видеть и контролировать, что конкретно делала модель»).

Вызывается из weather_ml_live.train_and_save() после обучения v1/v2/v3.
Пишет одну строку в ml_train_log (JSON с подробностями):
- данные: сколько город-дней, за какой период, сколько новых с прошлой ночи;
- что обучено: версии, число строк и признаков, разброс ошибки v1 по городам;
- экзамен: v3 заново обучается БЕЗ последних EXAM_DAYS дней и сдаёт их как
  незнакомые (с 30.09 — ещё экзамен ВСЕХ версий v1-v5 на тех же днях, exam_all) — какой шанс давала правильному ответу (против рынка) и на
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
    # 2026-09-27 (просьба Alex): и смесь 35% модели + 65% рынка — то, на что ставят кошельки *_cal
    from weather_ml_live import blend_with_market
    pm = pk = pb = 0.0
    lm = lk = lb = 0.0  # логошибка: −ln(шанс, данный тому, что случилось) — честное мерило, штрафует самоуверенность
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
        keys = list(pr)
        mix = blend_with_market([P[b] / (sum(P.values()) or 1.0) for b in keys], [pr[b] for b in keys])
        pb += mix[keys.index(wb)]
        lm -= math.log(max(P[wb] / (sum(P.values()) or 1.0), 1e-4))
        lk -= math.log(max(pr[wb] / (sum(pr.values()) or 1.0), 1e-4))
        lb -= math.log(max(mix[keys.index(wb)], 1e-4))
        n_p += 1
    return {"from": cut, "to": last.isoformat(), "n": len(te), "n_train": len(tr), "n_prob": n_p,
            "p_model": 100 * pm / n_p if n_p else None, "p_market": 100 * pk / n_p if n_p else None,
            "p_blend": 100 * pb / n_p if n_p else None,
            "ll_model": lm / n_p if n_p else None, "ll_market": lk / n_p if n_p else None,
            "ll_blend": lb / n_p if n_p else None,
            "err_model": float(np.mean(err_model)), "err_fc": float(np.mean(err_fc)),
            "err_market": float(np.mean(err_mkt)) if err_mkt else None}


EXAM_VERSIONS = [("ml", "v1"), ("ml2", "v2"), ("ml3", "v3"), ("ml4", "v4"), ("ml4e", "v4e"), ("ml5", "v5")]


def _exam_all(conn, dfm):
    """2026-09-30 (решение Alex): экзамен ВСЕХ версий на одних и тех же днях — каждая версия учится заново без последних
    EXAM_DAYS дней ровно так же, как в рабочем обучении (weather_ml_live), и сдаёт их против рынка. Сравнение версий сразу,
    а не через недели живых дней. Разница меньше 0.015 по логошибке — «наравне» (обычный разброс между обучениями)."""
    import lightgbm as lgb
    from weather_edge import emos_bucket_prob
    from weather_ml_live import V4_EXTRA, V4E_SEEDS, V5_SEEDS, blend_with_market
    last = date.fromisoformat(dfm["date"].max())
    cut = (last - timedelta(days=EXAM_DAYS - 1)).isoformat()
    tr, te = dfm[dfm["date"] < cut], dfm[dfm["date"] >= cut]
    if len(tr) < 1000 or te.empty:
        return None
    f3 = list(ml.FEATURES)
    f2 = [f for f in f3 if not f.startswith("mkt_")]

    def quant(feats, base_tr, base_te, extra_list):
        runs = []
        for extra in extra_list:
            ms = {q: lgb.train({**mq.Q_PARAMS, **extra, "alpha": q},
                               lgb.Dataset(tr[feats], tr["actual_c"] - base_tr, categorical_feature=["city_id"]), mq.Q_ROUNDS)
                  for q in mq.QUANTILES}
            raw = np.column_stack([ms[q].predict(te[feats]) for q in mq.QUANTILES])
            raw.sort(axis=1)
            runs.append(raw)
        return np.mean(runs, axis=0) + np.asarray(base_te)[:, None]

    seeds = lambda ss, ex=None: [{**(ex or {}), "seed": s, "bagging_seed": s, "feature_fraction_seed": s} for s in ss]
    b5_tr = tr["fc_mean"] + tr["mkt_mean_vs_fc"].fillna(0.0)
    b5_te = te["fc_mean"] + te["mkt_mean_vs_fc"].fillna(0.0)
    preds = {
        "ml2": quant(f2, tr["fc_mean"], te["fc_mean"], [{}]),
        "ml3": quant(f3, tr["fc_mean"], te["fc_mean"], [{}]),
        "ml4": quant(f3, tr["fc_mean"], te["fc_mean"], [V4_EXTRA]),
        "ml4e": quant(f3, tr["fc_mean"], te["fc_mean"], seeds(V4E_SEEDS, V4_EXTRA)),
        "ml5": quant(f3, b5_tr, b5_te, seeds(V5_SEEDS)),
    }
    # v1: одно число + разброс по городу (честный, по кросс-валидации внутри обучения) — как в рабочем обучении
    saved = ml.FEATURES
    try:
        ml.FEATURES = f2
        m1 = ml.train(tr)
        sig1, _ = ml.city_sigmas(tr)
        mu1 = m1.predict(te[f2]) + te["fc_mean"].values
    finally:
        ml.FEATURES = saved
    win = {(r[0], r[1]): r[2] for r in conn.execute("SELECT city, local_date, win_lo FROM weather_poly_outcomes")}
    acc = {k: {"lm": 0.0, "lb": 0.0, "err": []} for k, _ in EXAM_VERSIONS}
    lk, n_p = 0.0, 0
    i50 = mq.QUANTILES.index(0.5)
    for j, (_, r) in enumerate(te.iterrows()):
        k1 = (9 / 5, 32) if r["unit"] == "fahrenheit" else (1, 0)
        s1 = sig1.get(r["city"], np.median(list(sig1.values())) if sig1 else 1.0)
        acc["ml"]["err"].append(abs(mu1[j] - r["actual_c"]))
        for k in preds:
            acc[k]["err"].append(abs(preds[k][j][i50] - r["actual_c"]))
        w = win.get((r["city"], r["date"]))
        pr = chk.prices(conn, r["city"], r["date"], "A") if w is not None else {}
        wb = next((b for b in pr if b[0] == w), None)
        if len(pr) < 3 or wb is None:
            continue
        keys = list(pr)
        mk = [pr[b] / (sum(pr.values()) or 1.0) for b in keys]
        lk -= math.log(max(mk[keys.index(wb)], 1e-4))
        n_p += 1
        for k, _ in EXAM_VERSIONS:
            if k == "ml":
                P = [emos_bucket_prob(mu1[j] * k1[0] + k1[1], s1 * k1[0], b[0], b[1]) for b in keys]
            else:
                P = [mq.bucket_prob(list(preds[k][j]), r["unit"], b[0], b[1]) for b in keys]
            t = sum(P) or 1.0
            P = [p / t for p in P]
            mix = blend_with_market(P, [pr[b] for b in keys])
            acc[k]["lm"] -= math.log(max(P[keys.index(wb)], 1e-4))
            acc[k]["lb"] -= math.log(max(mix[keys.index(wb)], 1e-4))
    if not n_p:
        return None
    return {"from": cut, "to": last.isoformat(), "n": len(te), "n_prob": n_p, "ll_market": lk / n_p,
            "versions": {k: {"name": nm, "ll_model": a["lm"] / n_p, "ll_blend": a["lb"] / n_p, "err": float(np.mean(a["err"]))}
                         for (k, nm), a in ((kv, acc[kv[0]]) for kv in EXAM_VERSIONS)}}


def version_detail(timing, rows1, rows_m, feat1, feat_m, sigmas, sig_glob):
    """30.09 (просьба Alex: «видеть, как училась каждая модель»): по каждой версии — что учит, настройки, сколько моделей,
    сколько длилось, данные и на что смотрит больше всего (важность признаков медианы её собственных моделей, среднее по зёрнам)."""
    import lightgbm as lgb
    from weather_ml_live import ML_DIR, V4E_SEEDS, V5_SEEDS
    base = f"{mq.Q_PARAMS['num_leaves']} листьев, шаг {mq.Q_PARAMS['learning_rate']}, {mq.Q_ROUNDS} деревьев, мин. {mq.Q_PARAMS['min_data_in_leaf']} дней в листе"
    spec = {
        "ml": ("поправку к среднему 16 моделей — одно число; разброс — постоянный по городу", f"регрессия, {ml.PARAMS['num_leaves']} листьев, {ml.N_ROUNDS} деревьев",
               1, rows1, feat1, [ML_DIR / "model.txt"]),
        "ml2": ("поправку к среднему 16 моделей — всё распределение (13 уровней), без цены рынка", base, 13, rows1, feat1, [ML_DIR / "q" / "q50.txt"]),
        "ml3": ("поправку к среднему 16 моделей — 13 уровней, с мнением рынка в 08:00", base, 13, rows_m, feat_m, [ML_DIR / "q_mkt" / "q50.txt"]),
        "ml4": ("то же, что v3", base.replace(f"{mq.Q_PARAMS['num_leaves']} листьев", "31 лист"), 13, rows_m, feat_m, [ML_DIR / "q_mkt31" / "q50.txt"]),
        "ml4e": ("то же, что v4, три обучения с зёрнами " + ", ".join(map(str, V4E_SEEDS)) + " — прогноз среднее",
                 base.replace(f"{mq.Q_PARAMS['num_leaves']} листьев", "31 лист"), 39, rows_m, feat_m, [ML_DIR / f"q_mkt31_s{s}" / "q50.txt" for s in V4E_SEEDS]),
        "ml5": ("поправку к ожидаемому максимуму по ценам рынка (не к среднему 16 моделей), три обучения с зёрнами "
                + ", ".join(map(str, V5_SEEDS)), base, 39, rows_m, feat_m, [ML_DIR / f"q_fm_s{s}" / "q50.txt" for s in V5_SEEDS]),
    }
    out = {}
    for k, (what, params, n_models, rows, feats, files) in spec.items():
        imp = {}
        for f in files:
            if not f.exists():
                continue
            m = lgb.Booster(model_file=str(f))
            g = m.feature_importance("gain")
            tot = float(g.sum()) or 1.0
            for n, v in zip(m.feature_name(), g):
                imp[n] = imp.get(n, 0.0) + 100 * v / tot / len(files)
        out[k] = {"what": what, "params": params, "n_models": n_models, "rows": rows, "features": feats,
                  "seconds": round(timing.get(k, 0.0), 1),
                  "importance": [{"name": n, "pct": p} for n, p in sorted(imp.items(), key=lambda x: -x[1])[:12]]}
    sv = sorted(sigmas.values())
    if sv:
        out["ml"]["sigmas"] = {"min": sv[0], "max": sv[-1], "global": sig_glob,
                               "cities": sorted(({"city": c, "s": s} for c, s in sigmas.items()), key=lambda x: -x["s"])}
    return out


def report(conn, started, df1, dfm, sigmas, qmodels_mkt):
    finished = datetime.now(timezone.utc)
    prev = None
    try:
        r = conn.execute("SELECT details FROM ml_train_log ORDER BY trained_at DESC LIMIT 1").fetchone()
        prev = json.loads(r[0]) if r else None
    except Exception:
        prev = None
    exam = _exam(conn, dfm)
    try:
        exam_all = _exam_all(conn, dfm)
    except Exception as e:  # noqa: BLE001 — экзамен всех версий не должен ломать отчёт и ночное обучение
        print(f"экзамен всех версий не прошёл: {type(e).__name__}: {e}", flush=True)
        exam_all = None
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
            {"key": "ml5", "name": "v5 — «от рынка», среднее 3 обучений", "rows": len(dfm), "features": len(ml.FEATURES),
             "note": "с 29.09: учит поправку к ожидаемому максимуму по ценам рынка, а не к среднему 16 моделей"},
        ],
        "exam": exam,
        "exam_all": exam_all,
        "importance": [{"name": n, "pct": 100 * g / tot} for n, g in top],
        "checks": [{"ok": bool(ok), "title": t, "detail": d} for ok, t, d in checks],
    }
    conn.execute("""CREATE TABLE IF NOT EXISTS ml_train_log (trained_at TEXT PRIMARY KEY, ok INTEGER, details TEXT)""")
    conn.execute("INSERT OR REPLACE INTO ml_train_log VALUES (?, ?, ?)",
                 (finished.isoformat(), int(all(c["ok"] for c in details["checks"])), json.dumps(details, ensure_ascii=False)))
    conn.commit()
    if exam:
        print(f"экзамен {exam['from']}..{exam['to']}: шанс правильному ответу модель "
              f"{exam['p_model'] or 0:.1f}% / рынок {exam['p_market'] or 0:.1f}% / смесь {exam.get('p_blend') or 0:.1f}%; ошибка {exam['err_model']:.2f}°C "
              f"(среднее моделей {exam['err_fc']:.2f}°C)")
    if exam_all:
        print(f"экзамен всех версий {exam_all['from']}..{exam_all['to']} ({exam_all['n_prob']} город-дней), логошибка "
              f"(рынок {exam_all['ll_market']:.3f}): " + ", ".join(f"{v['name']} {v['ll_model']:.3f} / смесь {v['ll_blend']:.3f}"
                                                              for v in exam_all["versions"].values()), flush=True)
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
