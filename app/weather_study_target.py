"""
Идея 6 (2026-09-26, Alex): учить v3 итогу Polymarket, а не замеру станции.
Итог Polymarket совпадает с нашим фактом станции в ~97-98% дней; в остальных
(округления, в прошлом другой источник — Шэньчжэнь, Париж, Сеул) модель учится
«не тому». Цель: если факт станции внутри выигравшего варианта — не меняем; иначе
сдвигаем к ближайшей точке внутри выигравшего варианта. Дни без итога — как было.
Проверка — как в weather_study_tune.py (август и сентябрь, учим на всём до месяца).
Запуск — на копии базы: POLY_LAB_DB=/data/research/research.sqlite3
"""

import weather_ml as ml
import weather_study_tune as tune
from weather_study_0926 import WIN

WIN_HI = {(r[0], r[1]): (r[2], r[3]) for r in tune.conn.execute(
    "SELECT city, local_date, win_lo, win_hi FROM weather_poly_outcomes").fetchall()}


def to_poly_target(df):
    df = df.copy()
    moved = 0
    for idx, r in df.iterrows():
        w = WIN_HI.get((r["city"], r["date"]))
        if not w:
            continue
        k, off = (1.8, 32) if r["unit"] == "fahrenheit" else (1.0, 0)
        a = r["actual_c"] * k + off  # в единицах города
        lo, hi = w
        eps = 0.05
        new = a
        if lo > -900 and a <= lo:
            new = lo + eps + 0.45  # середина первой половины варианта
        elif hi < 900 and a >= hi:
            new = hi - eps - 0.45
        if new != a:
            df.at[idx, "actual_c"] = (new - off) / k
            moved += 1
    return df, moved


if __name__ == "__main__":
    ml.USE_MKT = True
    df = ml.build(tune.conn)
    df = df[df["actual_c"].notna()]
    ml.FEATURES = ml.features(df)
    df_poly, moved = to_poly_target(df)
    print(f"данных {len(df)}; цель поправлена по итогу Polymarket в {moved} днях")
    for name, data in (("цель = замер станции (как сейчас)", df), ("цель = итог Polymarket", df_poly)):
        line = f"{name:36s}"
        for label, start, end in (("август", "2026-08-01", "2026-09-01"), ("сентябрь", "2026-09-01", "2026-10-01")):
            tr = data[data["date"] < start]
            te = df[(df["date"] >= start) & (df["date"] < end)]  # проверяем всегда по одинаковым дням и ответам
            s = tune.score(tune.train(tr, {}, 300, None), te)
            line += f" | {label}: модель {s['ll']:.3f} смесь {s['blend']:.3f} (рынок {s['mkt']:.3f})"
            if label == "сентябрь":
                r = tune.base.run_rule(s["days"], "blend", 0.03)
                line += f" | смесь 3 п.п., реальные сделки {r['n_real']} ставок {r['pnl_real']:+.1f}$"
        print(line, flush=True)
