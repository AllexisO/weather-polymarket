"""
Быстрая проверка народных станций (2026-09-29, 7 дней × 11 городов США — только сильная связь будет видна).
Вопрос: утром (07:00-07:50) станции вокруг аэропорта теплее/холоднее, чем обещал прогноз, — предсказывает ли это,
что прогнозы (среднее 16 моделей) и РЫНОК ошибутся в ту же сторону? И даёт ли это что-то сверх замера самого аэропорта
(он у модели уже есть)? Прогноз на 07:00 — Open-Meteo previous_day1 (кэш weather_study_factors). Только на копии.
"""
import json
import math
import statistics as st
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

import weather_ml as ml
import weather_study_0926 as base
from weather_cities import OBS_CITIES

conn = base.conn
FX = json.loads(Path("/data/research/factors.json").read_text())


def mkt_mean(city, d, unit):
    rows = conn.execute("""SELECT bucket_lo, bucket_hi, market_p FROM snapshots WHERE city = ? AND local_date = ? AND ts_utc =
                           (SELECT MIN(ts_utc) FROM snapshots WHERE city = ? AND local_date = ? AND local_hour BETWEEN 8 AND 11)""",
                        (city, d, city, d)).fetchall()
    if len(rows) < 3:
        return None
    f = ml.mkt_features({(r[0], r[1]): r[2] or 0 for r in rows}, unit, 0.0)
    return f.get("mkt_mean_vs_fc")


rows = []
for city, cfg in OBS_CITIES.items():
    p = Path(f"/data/research/pws/{city}.json")
    if not p.exists():
        continue
    tz, unit = ZoneInfo(cfg["tz"]), cfg["unit"]
    pws = json.loads(p.read_text())
    # отклонение каждой станции от прогноза на 07:00, потом — от её же среднего за неделю (у станций свой перекос)
    dev = {}
    for d, sts in pws.items():
        t07 = FX.get(city, {}).get(d, {}).get("t07")
        if t07 is None:
            continue
        for sid, obs in sts.items():
            v = [t for h, t in obs if 7 <= h < 7.84]
            if v:
                dev.setdefault(sid, {})[d] = st.mean(v) - t07
    for d in sorted(pws):
        t07 = FX.get(city, {}).get(d, {}).get("t07")
        act = conn.execute("SELECT actual_max FROM weather_station_daily WHERE city = ? AND local_date = ?", (city, d)).fetchone()
        fc = [ml.to_c(r[0], unit) for r in conn.execute(
            "SELECT fcst_max FROM mm_forecasts WHERE city = ? AND lead = 'day1' AND local_date = ?", (city, d))]
        if t07 is None or not act or len(fc) < 3:
            continue
        a = [dev[s][d] - st.mean(dev[s].values()) for s in dev if d in dev[s] and len(dev[s]) >= 4]
        raw = [dev[s][d] for s in dev if d in dev[s]]
        t0 = datetime.fromisoformat(d).replace(tzinfo=tz)
        ap = conn.execute("""SELECT tmpf FROM station_obs WHERE city = ? AND valid_utc BETWEEN ? AND ? ORDER BY valid_utc DESC LIMIT 1""",
                          (city, (t0 + timedelta(hours=6)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                           (t0 + timedelta(hours=7, minutes=55)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M"))).fetchone()
        if len(a) < 5 or not ap:
            continue
        fcm = st.mean(fc)
        actual_c = ml.to_c(act[0], unit)
        mk = mkt_mean(city, d, unit)
        rows.append({"city": city, "d": d, "pws": float(np.median(a)), "pws_raw": float(np.median(raw)),
                     "air": ml.f_to_c(ap[0]) - t07, "err_fc": actual_c - fcm,
                     "err_mkt": None if mk is None else actual_c - mk, "n": len(a)})

print(f"город-дней: {len(rows)} ({len({r['city'] for r in rows})} городов), станций на день в среднем {st.mean(r['n'] for r in rows):.0f}")


def corr(x, y):
    x, y = np.asarray(x, float), np.asarray(y, float)
    return float(np.corrcoef(x, y)[0, 1])


def partial(x, y, z):  # связь x с y сверх z
    x, y, z = (np.asarray(v, float) for v in (x, y, z))
    rx = x - np.polyval(np.polyfit(z, x, 1), z)
    ry = y - np.polyval(np.polyfit(z, y, 1), z)
    return corr(rx, ry)


se = 1 / math.sqrt(max(len(rows) - 3, 1))
print(f"(случайная связь на таком объёме — до ±{2 * se:.2f}; всё, что меньше, — неотличимо от шума)\n")
ef = [r["err_fc"] for r in rows]
print(f"ошибка прогнозов (факт − среднее 16 моделей): в среднем {st.mean(ef):+.2f}°C, разброс {st.pstdev(ef):.2f}°C")
print(f"  утро аэропорта против прогноза 07:00 → ошибка прогнозов: связь {corr([r['air'] for r in rows], ef):+.2f}  (это модель уже знает)")
print(f"  утро станций против прогноза 07:00 → ошибка прогнозов:  связь {corr([r['pws_raw'] for r in rows], ef):+.2f}")
print(f"  то же, без обычного перекоса каждой станции:             связь {corr([r['pws'] for r in rows], ef):+.2f}")
print(f"  станции СВЕРХ аэропорта:                                  связь {partial([r['pws'] for r in rows], ef, [r['air'] for r in rows]):+.2f}")
rm = [r for r in rows if r["err_mkt"] is not None]
if len(rm) >= 20:
    em = [r["err_mkt"] for r in rm]
    print(f"\nошибка РЫНКА (факт − ожидание рынка в 08:00), {len(rm)} город-дней: в среднем {st.mean(em):+.2f}°C, разброс {st.pstdev(em):.2f}°C")
    print(f"  утро аэропорта → ошибка рынка:         связь {corr([r['air'] for r in rm], em):+.2f}")
    print(f"  утро станций → ошибка рынка:           связь {corr([r['pws'] for r in rm], em):+.2f}")
    print(f"  станции СВЕРХ аэропорта → ошибка рынка: связь {partial([r['pws'] for r in rm], em, [r['air'] for r in rm]):+.2f}")
print("\nпо городам (станции без перекоса → ошибка прогнозов):")
for c in sorted({r["city"] for r in rows}):
    rc = [r for r in rows if r["city"] == c]
    if len(rc) >= 5:
        print(f"  {c:14s} {len(rc)} дн.: связь {corr([r['pws'] for r in rc], [r['err_fc'] for r in rc]):+.2f}")
