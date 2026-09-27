"""
Общая для weather_edge.py (коллектор) и dashboard.py логика: средний
промах модели по городу (факт минус середина топ-бакета по утренним/
дневным снимкам), посчитанный по уже резолвленным дням. Один источник
правды — чтобы поправка, которую коллектор применяет к следующему
прогнозу, и цифры, которые пользователь видит на /calibration, не могли
разойтись между собой.
"""

MIN_BIAS_N = 15  # меньше — риск подогнать поправку под шум пары дней,
# а не под реальную систематическую ошибку модели


def compute_city_bias(conn, since_ts):
    """conn.row_factory должен быть sqlite3.Row. Возвращает {city: bias
    в нативных для города единицах}, только для городов, где накопилось
    хотя бы MIN_BIAS_N резолвленных дней."""
    has_outcomes = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='weather_station_daily'"
    ).fetchone()
    if not has_outcomes:
        return {}

    rows = conn.execute(
        """
        SELECT s.ts_utc, s.city, s.local_date, s.bucket_lo, s.bucket_hi, s.model_p, o.actual_max
        FROM snapshots s
        JOIN weather_station_daily o ON s.city = o.city AND s.local_date = o.local_date
        WHERE s.ts_utc >= ? AND s.local_hour < 12
        ORDER BY s.city, s.local_date, s.ts_utc
        """,
        (since_ts,),
    ).fetchall()

    groups = {}
    for r in rows:
        key = (r["city"], r["local_date"])
        groups.setdefault(key, []).append(r)

    per_city = {}
    for (city, _local_date), grp in groups.items():
        first_ts = grp[0]["ts_utc"]
        first_grp = [r for r in grp if r["ts_utc"] == first_ts]
        actual = first_grp[0]["actual_max"]
        model_pick = max(first_grp, key=lambda r: r["model_p"])
        # открытые служебные бакеты ("35.5° и выше" и т.п.) хранятся как
        # (-999, X) / (X, 999) — середина такого "бакета" не температура,
        # а мусорное число, в промах их включать нельзя (см. dashboard.py)
        if model_pick["bucket_lo"] <= -900 or model_pick["bucket_hi"] >= 900:
            continue
        bias = actual - (model_pick["bucket_lo"] + model_pick["bucket_hi"]) / 2
        per_city.setdefault(city, []).append(bias)

    return {
        city: sum(vals) / len(vals)
        for city, vals in per_city.items()
        if len(vals) >= MIN_BIAS_N
    }


# 2026-09-16: разведка (см. CLAUDE.md) нашла независимый проект
# (anaborne/kalshi-temperature-calibration, 6.8М показаний, 21 станция),
# который честно протестировал ровно такую же поправку, как наша
# (сдвиг среднего) — и показал, что этого НЕДОСТАТОЧНО: проблема не
# только в среднем ансамбля, но и в его разбросе (dispersion). Их же
# диагностика: поправка среднего снизила худшую ошибку калибровки, но
# не убрала её. Стандартный метеорологический ответ на именно эту
# проблему — EMOS/NGR (Nonhomogeneous Gaussian Regression): регрессией
# по истории поправляем и среднее, и разброс ансамбля, затем считаем
# вероятность бакета через нормальное распределение, а не через долю
# членов ансамбля напрямую. Существенно экономнее данных, чем quantile
# mapping — то, что нужно при наших 15-20 днях на город.
MIN_EMOS_N = 12


def _bucket_mid(lo, hi):
    # Открытые служебные бакеты — грубое приближение середины, не 0/999.
    if lo <= -900:
        return hi - 1.0
    if hi >= 900:
        return lo + 1.0
    return (lo + hi) / 2


def compute_ensemble_moments(rows):
    """Среднее и дисперсия распределения модели за один снимок,
    восстановленные из уже посчитанных вероятностей по бакетам
    (rows — все строки одного (city, local_date, ts_utc)). Не нужно
    хранить сырые члены ансамбля отдельно — гистограмма по бакетам уже
    есть в snapshots, этого достаточно для приближённых momента."""
    total_p = sum(r["model_p"] for r in rows)
    if total_p <= 0:
        return None
    mean = sum(r["model_p"] * _bucket_mid(r["bucket_lo"], r["bucket_hi"]) for r in rows) / total_p
    var = sum(
        r["model_p"] * (_bucket_mid(r["bucket_lo"], r["bucket_hi"]) - mean) ** 2 for r in rows
    ) / total_p
    return mean, var


def _ols(xs, ys):
    n = len(xs)
    xbar, ybar = sum(xs) / n, sum(ys) / n
    sxx = sum((x - xbar) ** 2 for x in xs)
    if sxx == 0:
        return ybar, 0.0
    sxy = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys))
    b = sxy / sxx
    a = ybar - b * xbar
    return a, b


def compute_emos_params(conn, since_ts, min_n=MIN_EMOS_N):
    """Возвращает {city: {"a", "b", "spread_scale", "n"}} — параметры
    EMOS/NGR, посчитанные по уже резолвленным дням: скорректированное
    среднее = a + b*ensemble_mean, скорректированное стандартное
    отклонение = spread_scale*sqrt(ensemble_var). Только для городов,
    где накопилось хотя бы min_n дней — меньше того же порядка, что и
    MIN_BIAS_N, потому что EMOS оценивает всего 3 параметра (a, b,
    spread_scale), а не сложную форму распределения."""
    has_outcomes = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='weather_station_daily'"
    ).fetchone()
    if not has_outcomes:
        return {}

    rows = conn.execute(
        """
        SELECT s.ts_utc, s.city, s.local_date, s.bucket_lo, s.bucket_hi, s.model_p, o.actual_max
        FROM snapshots s
        JOIN weather_station_daily o ON s.city = o.city AND s.local_date = o.local_date
        WHERE s.ts_utc >= ? AND s.local_hour < 12
        ORDER BY s.city, s.local_date, s.ts_utc
        """,
        (since_ts,),
    ).fetchall()

    groups = {}
    for r in rows:
        key = (r["city"], r["local_date"])
        groups.setdefault(key, []).append(r)

    per_city = {}
    for (city, _local_date), grp in groups.items():
        first_ts = grp[0]["ts_utc"]
        first_grp = [r for r in grp if r["ts_utc"] == first_ts]
        moments = compute_ensemble_moments(first_grp)
        if moments is None:
            continue
        mean, var = moments
        actual = first_grp[0]["actual_max"]
        per_city.setdefault(city, []).append((mean, var, actual))

    params = {}
    for city, triples in per_city.items():
        if len(triples) < min_n:
            continue
        means = [t[0] for t in triples]
        variances = [t[1] for t in triples]
        actuals = [t[2] for t in triples]
        a, b = _ols(means, actuals)
        residual_var = sum((actual - (a + b * m)) ** 2 for m, actual in zip(means, actuals)) / len(triples)
        avg_ensemble_var = sum(variances) / len(variances)
        spread_scale = (residual_var / avg_ensemble_var) ** 0.5 if avg_ensemble_var > 0 else 1.0
        params[city] = {"a": a, "b": b, "spread_scale": spread_scale, "n": len(triples)}
    return params
