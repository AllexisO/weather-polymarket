"""
Разовый скрипт: досчитывает emos_model_p/emos_edge для УЖЕ РЕЗОЛВЛЕННЫХ
дней, собранных ДО того, как EMOS появился в коллекторе (2026-09-16).
Без этого проверить EMOS можно только на новых днях, которые копятся по
одному в сутки — на это ушли бы недели.

Все данные, нужные для EMOS (вероятности по бакетам на снимок), уже лежат
в snapshots — сами моменты ансамбля (среднее/дисперсия) восстанавливаются
из них (compute_ensemble_moments), как и в основном коллекторе.

ВАЖНО — walk-forward, не подгонка задним числом: параметры EMOS (a, b,
spread_scale) для дня N обучаются ТОЛЬКО на днях, которые резолвились
РАНЬШЕ дня N по тому же городу. Если фитить параметры на всех днях сразу
(включая тот, который потом же и проверяем), получится подогнанная под
шум задача — тот же класс ошибки, что уже был с крипто/золотом
("рынок не может знать цену раньше тебя", см. CLAUDE.md) и с
живым/прошедшим матчем в sports_edge.py. Здесь риск симметричный: модель
не должна "знать" свою будущую ошибку заранее.

Из-за MIN_EMOS_N=12 первые ~12 резолвленных дней каждого города walk-
forward пропускает (не из чего фитить) — это ожидаемо, честная выборка
меньше, чем могла бы быть при подгонке на всех данных.

Обновляет только строки, где emos_model_p ещё NULL — не трогает то, что
уже посчитал живой коллектор.
"""

import os
import sqlite3
import sys
from pathlib import Path

from weather_bias import compute_ensemble_moments, _ols, MIN_EMOS_N
from weather_edge import emos_bucket_prob

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))
WEATHER_COORD_FIX_TS = "2026-08-25T19:58:27+00:00"

# 2026-09-22: --recompute перезаписывает уже посчитанные emos_model_p.
# Нужен был один раз: до этой даты EMOS обучался на "факте" из
# Open-Meteo, а не на реальных показаниях станций (weather_station_daily)
# — все старые EMOS-вероятности были подогнаны под неправильный эталон.
# Пересчёт тоже walk-forward, как и исходный бэкафилл.
RECOMPUTE = "--recompute" in sys.argv


def run():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """
        SELECT s.id, s.ts_utc, s.city, s.local_date, s.bucket_lo, s.bucket_hi, s.model_p, o.actual_max
        FROM snapshots s
        JOIN weather_station_daily o ON s.city = o.city AND s.local_date = o.local_date
        WHERE s.ts_utc >= ? AND s.local_hour < 12
        ORDER BY s.city, s.local_date, s.ts_utc
        """,
        (WEATHER_COORD_FIX_TS,),
    ).fetchall()

    by_city_day = {}
    for r in rows:
        key = (r["city"], r["local_date"])
        by_city_day.setdefault(key, []).append(r)

    # один день = самый ранний снимок (та же логика, что в
    # _one_snapshot_per_day в dashboard.py) — иначе несколько снимков в
    # день считались бы отдельными точками для регрессии.
    days_by_city = {}
    for (city, local_date), grp in by_city_day.items():
        first_ts = min(r["ts_utc"] for r in grp)
        first_grp = [r for r in grp if r["ts_utc"] == first_ts]
        moments = compute_ensemble_moments(first_grp)
        if moments is None:
            continue
        mean, var = moments
        actual = first_grp[0]["actual_max"]
        days_by_city.setdefault(city, []).append(
            {
                "local_date": local_date,
                "ts_utc": first_ts,
                "rows": first_grp,
                "mean": mean,
                "var": var,
                "actual": actual,
            }
        )

    total_updated = 0
    for city, days in days_by_city.items():
        days.sort(key=lambda d: d["local_date"])
        skipped_already = 0
        for i, day in enumerate(days):
            history = days[:i]
            if len(history) < MIN_EMOS_N:
                continue
            means = [d["mean"] for d in history]
            variances = [d["var"] for d in history]
            actuals = [d["actual"] for d in history]
            a, b = _ols(means, actuals)
            residual_var = sum((act - (a + b * m)) ** 2 for m, act in zip(means, actuals)) / len(history)
            avg_ensemble_var = sum(variances) / len(variances)
            spread_scale = (residual_var / avg_ensemble_var) ** 0.5 if avg_ensemble_var > 0 else 1.0

            emos_mean = a + b * day["mean"]
            emos_std = spread_scale * (day["var"] ** 0.5)

            for r in day["rows"]:
                cur = conn.execute("SELECT emos_model_p FROM snapshots WHERE id = ?", (r["id"],)).fetchone()
                if cur["emos_model_p"] is not None and not RECOMPUTE:
                    skipped_already += 1
                    continue
                emos_mp = emos_bucket_prob(emos_mean, emos_std, r["bucket_lo"], r["bucket_hi"])
                emos_edge = emos_mp - conn.execute(
                    "SELECT market_p FROM snapshots WHERE id = ?", (r["id"],)
                ).fetchone()["market_p"]
                conn.execute(
                    "UPDATE snapshots SET emos_model_p = ?, emos_edge = ? WHERE id = ?",
                    (emos_mp, emos_edge, r["id"]),
                )
                total_updated += 1
        print(f"{city}: {len(days)} резолвленных дней, walk-forward начался после {MIN_EMOS_N}-го, "
              f"пропущено уже заполненных строк: {skipped_already}")

    conn.commit()
    conn.close()
    print(f"Итого обновлено строк: {total_updated}")


if __name__ == "__main__":
    run()
