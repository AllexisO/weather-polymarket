"""
Почему модель ошиблась (2026-09-29, Alex: «мало просто учесть ошибку — надо понять, почему модель ошиблась»).
Часть недельного разбора (weather_week_review.py), можно запускать отдельно.

1. Честная модель на неделю: середина распределения (квантиль 0.5, как у v3) обучается только на днях ДО недели —
   она не знает ответов этой недели.
2. Для каждого дня недели — разложение прогноза по признакам (LightGBM pred_contrib): какой признак сколько градусов
   добавил или убавил. Промах (≥ 1°C) — смотрим, какие признаки толкнули прогноз не в ту сторону.
3. Что случилось на самом деле — по METAR дня: облака после обеда, смена ветра, когда был максимум, утро против
   прогноза. Сравниваем с обычными днями: если у промахов облака после обеда втрое чаще — вот причина.
4. Итог: какие признаки чаще всего вводят модель в заблуждение и какие события модель не видит → идеи признаков.

Запуск (только на копии): docker compose run --rm -e JOB_TIMEOUT=0 -e POLY_LAB_DB=/data/research/research.sqlite3 collector weather_why.py 2026-10-04
"""
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import lightgbm as lgb
import numpy as np

import weather_ml as ml
import weather_ml_q as mq
from weather_cities import OBS_CITIES

MISS_C = 1.0  # промах — середина прогноза мимо итога на 1°C и больше
NAMES = {
    "fc_mean": "среднее 16 прогнозов", "fc_std": "разброс прогнозов", "fc_min": "самый холодный прогноз",
    "fc_max": "самый тёплый прогноз", "obs_t": "утренняя температура", "obs_vs_fc": "утро против прогноза максимума",
    "obs_dew": "утренняя точка росы", "obs_spread": "утренняя сухость воздуха", "obs_tmin": "ночной минимум",
    "obs_alti": "давление утром", "obs_dalti3h": "давление за 3 ч", "obs_dt3h": "прирост температуры за 3 ч",
    "obs_wind": "ветер утром", "obs_wind_sin": "направление ветра утром", "obs_wind_cos": "направление ветра утром",
    "obs_cloud": "облачность утром", "fv_cloud_cover": "прогноз облачности", "fv_dew_point_2m": "прогноз точки росы",
    "fv_precipitation": "прогноз осадков", "fv_relative_humidity_2m": "прогноз влажности",
    "fv_shortwave_radiation": "прогноз солнца", "fv_wind_speed_10m": "прогноз ветра",
    "fv_wind_dir_sin": "прогноз направления ветра", "fv_wind_dir_cos": "прогноз направления ветра",
    "prev_actual_vs_fc": "вчера: итог против прогноза", "prev_err": "вчерашняя ошибка прогнозов",
    "mix_vs_fc": "микс моделей", "mkt_mean_vs_fc": "мнение рынка", "mkt_std": "неуверенность рынка",
    "mkt_top_p": "цена фаворита", "city_id": "город", "doy_sin": "сезон", "doy_cos": "сезон", "lat": "город", "lon": "город",
}
SKY = {"CLR": 0, "SKC": 0, "NSC": 0, "CAVOK": 0, "FEW": 1, "SCT": 2, "BKN": 3, "OVC": 4, "VV": 4}


def name(f):
    if f.startswith("fc_"):
        return NAMES.get(f, "прогноз " + f[3:].split("_")[0].upper())
    return NAMES.get(f, f)


def day_events(conn, city, d):
    """Что случилось днём по METAR: облака после обеда, смена ветра, час максимума."""
    tz = ZoneInfo(OBS_CITIES[city]["tz"])
    t0 = datetime.fromisoformat(d).replace(tzinfo=tz)
    rows = conn.execute("SELECT valid_utc, tmpf, drct, skyc1 FROM station_obs WHERE city = ? AND valid_utc BETWEEN ? AND ?",
                        (city, t0.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                         (t0 + timedelta(days=1)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M"))).fetchall()
    if len(rows) < 8:
        return None
    pts = []
    for v, t, dr, sk in rows:
        dt = datetime.fromisoformat(v.replace(" ", "T")).replace(tzinfo=timezone.utc).astimezone(tz)
        pts.append((dt.hour + dt.minute / 60, t, dr, SKY.get((sk or "").strip().upper())))
    morn = [p for p in pts if 6 <= p[0] < 9]
    aft = [p for p in pts if 12 <= p[0] < 17]
    sky_m = [p[3] for p in morn if p[3] is not None]
    sky_a = [p[3] for p in aft if p[3] is not None]
    wd = lambda ps: [p[2] for p in ps if p[2] not in (None, 0)]
    shift = None
    if wd(morn) and wd(aft):
        a, b = np.radians(np.mean(wd(morn))), np.radians(np.mean(wd(aft)))
        shift = abs((np.degrees(b - a) + 180) % 360 - 180)
    tmax = max((p for p in pts if p[1] is not None), key=lambda p: p[1], default=None)
    return {"clouds_after": bool(sky_a) and np.mean(sky_a) >= 3 and (not sky_m or np.mean(sky_m) < 3),
            "clear_after": bool(sky_a) and np.mean(sky_a) <= 1 and bool(sky_m) and np.mean(sky_m) >= 3,
            "wind_shift": shift is not None and shift >= 90,
            "max_early": tmax is not None and tmax[0] < 11, "max_late": tmax is not None and tmax[0] >= 17}


EV_NAMES = {"clouds_after": "после обеда затянуло (утром было ясно)", "clear_after": "после обеда прояснилось (утром облака)",
            "wind_shift": "ветер после обеда сменился на 90°+", "max_early": "максимум рано, до 11:00",
            "max_late": "максимум поздно, после 17:00"}


def explain(conn, wk0, end, out=print):
    ml.USE_MKT = True
    df = ml.build(conn)
    df = df[df["actual_c"].notna() & (df["date"] >= "2025-06-01")]
    feats = ml.features(df)
    tr = df[df["date"] < wk0.isoformat()]
    te = df[(df["date"] >= wk0.isoformat()) & (df["date"] <= end.isoformat())].copy()
    if te.empty:
        out("\n--- почему ошиблась: за неделю нет дней с итогом ---")
        return
    m = lgb.train({**mq.Q_PARAMS, "alpha": 0.5, "num_threads": 4, "seed": 11},
                  lgb.Dataset(tr[feats], tr["actual_c"] - tr["fc_mean"], categorical_feature=["city_id"]), mq.Q_ROUNDS)
    contrib = m.predict(te[feats], pred_contrib=True)  # последний столбец — база
    te["pred_c"] = te["fc_mean"] + contrib.sum(axis=1)
    te["err"] = te["pred_c"] - te["actual_c"]  # + модель теплее факта
    misses = te[te["err"].abs() >= MISS_C]
    out(f"\n--- почему ошиблась: {len(te)} город-дней, промахов ≥ {MISS_C:g}°C — {len(misses)} "
        f"(теплее {int((misses['err'] > 0).sum())}, холоднее {int((misses['err'] < 0).sum())}); "
        f"средняя ошибка {te['err'].abs().mean():.2f}°C ---")
    if len(misses):
        fc_err = misses["fc_mean"] - misses["actual_c"]
        corr = misses["pred_c"] - misses["fc_mean"]
        right = int(((corr * -fc_err) > 0).sum())
        out(f"  откуда ошибка в промахах: сами погодные прогнозы ошиблись в среднем на {fc_err.abs().mean():.1f}°C "
            f"(в ту же сторону — в {int((np.sign(fc_err) == np.sign(misses['err'])).sum())} из {len(misses)}); поправка модели "
            f"в среднем {corr.abs().mean():.1f}°C, в верную сторону — в {right} из {len(misses)}")
    idx = {i: k for k, i in enumerate(te.index)}
    harm = defaultdict(float)
    harm_n = Counter()
    ev_m, ev_ok = Counter(), Counter()
    n_ev_m = n_ev_ok = 0
    ev_by = {}
    for i, r in te.iterrows():
        ev = day_events(conn, r["city"], r["date"])
        ev_by[i] = ev
        if ev is None:
            continue
        if abs(r["err"]) >= MISS_C:
            n_ev_m += 1
            ev_m.update(k for k, v in ev.items() if v)
        else:
            n_ev_ok += 1
            ev_ok.update(k for k, v in ev.items() if v)
    for i, r in misses.iterrows():
        c = contrib[idx[i]][:-1]
        s = np.sign(r["err"])
        for f, v in zip(feats, c):
            if v * s > 0.05:  # признак толкнул прогноз в сторону ошибки
                harm[name(f)] += abs(v)
                harm_n[name(f)] += 1
    out("  что сбивало модель в промахах (сумма градусов «не в ту сторону», в скольких промахах):")
    for f, v in sorted(harm.items(), key=lambda x: -x[1])[:8]:
        out(f"    {f:34s} {v:5.1f}°C, в {harm_n[f]} из {len(misses)}")
    if n_ev_m and n_ev_ok:
        out("  что случалось днём (промахи против обычных дней) — модель этого утром не видит:")
        for k in EV_NAMES:
            a, b = ev_m[k] / n_ev_m, ev_ok[k] / n_ev_ok
            flag = "  ← чаще в промахах" if a >= 1.5 * b and ev_m[k] >= 3 else ""
            out(f"    {EV_NAMES[k]:42s} промахи {a * 100:3.0f}% · обычные дни {b * 100:3.0f}%{flag}")
    out("  крупнейшие промахи недели:")
    for i, r in misses.reindex(misses["err"].abs().sort_values(ascending=False).index).head(8).iterrows():
        c = contrib[idx[i]][:-1]
        s = np.sign(r["err"])
        top = sorted(((v, f) for f, v in zip(feats, c) if v * s > 0), key=lambda x: -abs(x[0]))[:3]
        ev = ev_by.get(i) or {}
        what = ", ".join(EV_NAMES[k] for k, v in ev.items() if v) or "ничего особенного по METAR"
        out(f"    {r['date']} {r['city']:13s} прогнозы {r['fc_mean']:.1f}° → модель {r['pred_c']:.1f}°, итог {r['actual_c']:.1f}° "
            f"({'теплее' if s > 0 else 'холоднее'} на {abs(r['err']):.1f}°): толкнули "
            + ", ".join(f"{name(f)} {v:+.1f}°" for v, f in top) + f" | днём: {what}")


if __name__ == "__main__":
    end = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today() - timedelta(days=1)
    c = sqlite3.connect(f"file:{os.environ.get('POLY_LAB_DB', '/data/research/research.sqlite3')}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    explain(c, end - timedelta(days=6), end)
