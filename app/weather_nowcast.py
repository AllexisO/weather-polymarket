"""
Прогноз итогового максимума дня по замерам в течение дня (nowcast),
2026-09-23. Исследование (см. РЕЗУЛЬТАТ ниже).

Идея: к середине дня уже известен максимум "на сейчас" (M) по METAR
станции. Итог дня = M + добавка, и добавка сильно зависит от часа: в 10
утра температура ещё растёт на несколько градусов, после 16 — почти
никогда. Распределение добавки берём из истории замеров этой же станции
(прогнозы погоды не нужны вовсе):
  добавка = итоговый максимум дня - максимум, известный к этому часу,
отдельно для случаев "сейчас на максимуме" (температура ещё растёт) и
"уже ниже максимума" (пик, скорее всего, пройден).

Walk-forward: для дня D — только дни до D, последние HISTORY_DAYS.
Вероятность бакета = доля прошлых дней, где M + добавка попадает в
бакет. Эмпирическое распределение, без предположений о форме.

Проверка — backtest() на истории цен (price_history), запуск:
python weather_nowcast.py [-v].

РЕЗУЛЬТАТ (2026-09-23): не работает, в живой кошелёк НЕ подключён.
Бэктест на истории цен (6 городов, июнь-сентябрь, $5 на город в день):
- только замеры: 473 ставки, выиграно 53, -$598;
- замеры + прогноз микса моделей на день: 413 ставок, выиграно 74, -$320.
В течение дня рынок видит те же замеры и прогнозы и оценивает остаток
дня точнее такой простой модели. Дальше не подкручивали — на одной и
той же истории это была бы подгонка. Файл оставлен как исследование.

Все значения — в единицах маркета и с тем же округлением, что у
официального источника (°F — целые, °C — целые из METAR).
"""

import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))

HISTORY_DAYS = 60
MIN_DAYS = 20
HOURS = range(10, 18)      # решаем с 10:00 до 17:59 местного
MIN_EDGE = 0.15            # строже, чем у утренних прогнозов: сигналов в течение дня много, отбираем сильные
MIN_PRICE, MAX_PRICE = 0.05, 0.90
DELAY_MIN = 10             # сводка становится известна через 10 минут


def obs_value(tmpf, unit):
    return round(tmpf) if unit == "fahrenheit" else round((tmpf - 32) * 5 / 9)


def load_days(conn, city, cfg):
    """{local_date: [(utc_dt, value), ...]} по станции."""
    tz = ZoneInfo(cfg["tz"])
    days = {}
    for r in conn.execute("SELECT valid_utc, tmpf FROM station_obs WHERE city = ? AND tmpf IS NOT NULL", (city,)):
        t = datetime.fromisoformat(r[0]).replace(tzinfo=timezone.utc)
        days.setdefault(t.astimezone(tz).date().isoformat(), []).append((t, obs_value(r[1], cfg["unit"])))
    for v in days.values():
        v.sort()
    return days


def state_at(obs, t):
    """(максимум к моменту t, последняя сводка, на максимуме ли сейчас) по сводкам, известным к t."""
    known = [v for (ot, v) in obs if ot + timedelta(minutes=DELAY_MIN) <= t]
    if not known:
        return None
    m = max(known)
    return m, known[-1], known[-1] == m


def training_deltas(days, cfg, before, hour):
    """Добавки к максимуму за прошлые дни в тот же местный час, по двум состояниям."""
    tz = ZoneInfo(cfg["tz"])
    lo = (date.fromisoformat(before) - timedelta(days=HISTORY_DAYS)).isoformat()
    out = {True: [], False: []}
    for d, obs in days.items():
        if not (lo <= d < before) or len(obs) < 20:
            continue
        t = datetime.fromisoformat(d).replace(tzinfo=tz) + timedelta(hours=hour)
        st = state_at(obs, t)
        if st is None:
            continue
        final = max(v for _, v in obs)
        out[st[2]].append(final - st[0])
    return out


def bucket_probs(m, deltas, buckets):
    finals = [m + x for x in deltas]
    return {b: sum(1 for f in finals if b[0] < f <= b[1]) / len(finals) for b in buckets}


def decide(days, cfg, local_date, t, prices):
    """Лучший бакет для покупки в момент t или None. prices: {(lo, hi): цена Yes}."""
    st = state_at(days.get(local_date, []), t)
    if st is None:
        return None
    hour = t.astimezone(ZoneInfo(cfg["tz"])).hour
    deltas = training_deltas(days, cfg, local_date, hour)[st[2]]
    if len(deltas) < MIN_DAYS:
        return None
    probs = bucket_probs(st[0], deltas, list(prices))
    b = max(prices, key=lambda k: probs[k] - prices[k])
    if probs[b] - prices[b] >= MIN_EDGE and MIN_PRICE <= prices[b] <= MAX_PRICE:
        return b, probs[b], st[0]
    return None


def backtest(conn, cities, start="2026-06-01", end="2026-12-31", verbose=False):
    """Одна ставка $5 на город в день — первый час, где есть сигнал. Цена — история +1¢."""
    total = {}
    for city, cfg in cities.items():
        days = load_days(conn, city, cfg)
        tz = ZoneInfo(cfg["tz"])
        st = total.setdefault(city, [0, 0, 0.0])
        for d, win_lo in conn.execute(
            """SELECT d.local_date, o.win_lo FROM price_history_days d
               JOIN weather_poly_outcomes o ON o.city = d.city AND o.local_date = d.local_date
               WHERE d.city = ? AND d.local_date BETWEEN ? AND ?""", (city, start, end)).fetchall():
            series = {}
            for lo, hi, t, p in conn.execute(
                "SELECT bucket_lo, bucket_hi, t_utc, p FROM price_history WHERE city = ? AND local_date = ? ORDER BY t_utc",
                (city, d)):
                series.setdefault((lo, hi), []).append((t, p))
            for hour in HOURS:
                t = datetime.fromisoformat(d).replace(tzinfo=tz) + timedelta(hours=hour)
                ts = t.timestamp()
                prices = {}
                for k, s in series.items():
                    last = [p for pt, p in s if ts - 1800 <= pt <= ts]
                    if last:
                        prices[k] = last[-1] + 0.01
                if len(prices) < 3:
                    continue
                sig = decide(days, cfg, d, t, prices)
                if sig is None:
                    continue
                b, prob, m = sig
                won = b[0] == win_lo
                st[0] += 1
                st[1] += won
                st[2] += 5 / prices[b] - 5 if won else -5
                if verbose:
                    print(f"  {city} {d} {hour}:00 макс сейчас {m}, ставка на {b[0]}..{b[1]} по {prices[b]:.2f} "
                          f"(модель {prob:.2f}) -> {'выиграл' if won else 'проиграл'}")
                break
    return total


if __name__ == "__main__":
    from weather_cities import OBS_CITIES
    conn = sqlite3.connect(DB_PATH, timeout=60)
    res = backtest(conn, OBS_CITIES, verbose="-v" in sys.argv)
    n = sum(v[0] for v in res.values()); w = sum(v[1] for v in res.values()); pnl = sum(v[2] for v in res.values())
    for city, (cn, cw, cp) in sorted(res.items(), key=lambda kv: -kv[1][2]):
        if cn:
            print(f"{city:14} ставок {cn:3}  выиграно {cw:3}  итог {cp:+8.1f}$")
    print(f"ВСЕГО: ставок {n}, выиграно {w}, итог {pnl:+.1f}$")
