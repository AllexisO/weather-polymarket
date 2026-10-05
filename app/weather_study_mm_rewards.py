"""
Награды Polymarket за заявки для бота-мейкера — сколько добавили бы к итогу (2026-10-01, Alex: «давай» на «посчитать награды»).
Виртуальный бот наград не получает, а у настоящего они шли бы сверху. Считаем на тех же стаканах Falcon (19.08-27.09).

Правило наград (docs.polymarket.com, liquidity rewards): каждую минуту заявке в пределах v центов от середины начисляется
S = ((v − s) / v)² × доли (s — расстояние до середины в центах); заявка меньше min_size не считается. Одна сторона
при середине 10-90¢ — счёт делится на 3, при середине вне 10-90¢ — ноль (нужны обе стороны). Награда маркета за день
делится между мейкерами пропорционально счёту. Параметры погоды сейчас (clob /rewards/markets/current, 01.10): v = 4.5¢,
min_size 20 (у части 100), ~$30 в день на вариант (медиана; всего ~$7 200/день на ~170 вариантов дня). Историю ставок
наград API не отдаёт — берём сегодняшнюю медиану RATE для каждого варианта выборки (3 самых торгуемых — их награда обычно есть).

Бот — как живой mm_ws_zone / mm_ws_z30: одна «дешёвая» сторона (ниже 50¢, с 15 до 18 ч ниже 30¢ / всегда ниже 30¢),
на 0.1¢ лучше лучшей заявки (или на лучшей цене), паузы у сводки METAR, после 18:00 местного не стоим. Размер 20 долей
(10 — меньше min_size, наград нет). Стакан снимка держится до следующего снимка, но не дольше STALE.
Доля бота: нижняя оценка — остальные мейкеры двусторонние и стоят на большей из сторон (наш счёт/3 против их большей
стороны), верхняя — против их меньшей стороны. Итог стратегии самих сделок (20 долей) — weather_study_mm_book.run_bucket.
Порог (записан до прогона): награды по НИЖНЕЙ оценке прибавляют ≥ +1 п.п. к доходности (от потраченного) в обеих
половинах. Только на копии.
"""
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import weather_study_mm_book as bk
from weather_cities import OBS_CITIES

V, SIZE, RATE = 4.5, 20.0, float(os.environ.get("RATE", "30"))


def rewards(snaps, city, ld, thr):
    tz = ZoneInfo(OBS_CITIES[city]["tz"])
    lo = hi = 0.0
    for (ts, bids, asks), nxt in zip(snaps, snaps[1:] + [(snaps[-1][0] + bk.STALE, None, None)]):
        dt = min(nxt[0] - ts, bk.STALE)
        if dt <= 0 or not bids or not asks:
            continue
        loc = datetime.fromtimestamp(ts, tz)
        if loc.date().isoformat() > ld or (loc.date().isoformat() == ld and loc.hour >= 18):
            continue
        mn = datetime.fromtimestamp(ts, timezone.utc).minute
        if any((mn - x) % 60 <= 3 or (x - mn) % 60 <= 1 for x in bk.MINS.get(city, [])):
            continue
        bb, ba = bids[0][0], asks[0][0]
        mid = (bb + ba) / 2
        if not (0.10 <= mid <= 0.90):
            continue
        improve = ba - bb >= 3 * bk.TICK - 1e-9
        late = bk.zone(loc, ld) == "15-18"
        if mid < 0.5:   # дешёвая сторона — «да»: наша заявка на покупку «да»
            price = bb + bk.TICK if improve else bb
            side_p, s = price, (mid - price) * 100
        else:           # дешёвая сторона — «нет»: покупка «нет» = продажа «да» по ba
            price = ba - bk.TICK if improve else ba
            side_p, s = 1 - price, (price - mid) * 100
        if side_p >= (min(thr, 0.30) if late else thr) or s >= V:
            continue
        us = ((V - s) / V) ** 2 * SIZE / 3
        q = lambda lv, sign: sum(((V - d) / V) ** 2 * sz for p, sz in lv if (d := sign * (mid - p) * 100) < V)
        q1, q2 = q(bids, 1), q(asks, -1)
        per = RATE * dt / 86400
        lo += per * us / (us + max(q1, q2))
        hi += per * us / (us + min(q1, q2))
    return lo, hi


def main():
    data = bk.load()
    bk.SIZE = SIZE
    print(f"вариантов {len(data)}; награда {RATE:.0f}$/день на вариант, v {V}¢, заявка {SIZE:.0f} долей", flush=True)
    for name, thr in (("mm_ws_zone (<50¢, 15-18 <30¢)", 0.50), ("mm_ws_z30 (<30¢)", 0.30)):
        allow = lambda z, price, spread, cid, ts, side, t=thr: price < (0.30 if z == "15-18" else t)
        res = {"1": [0.0] * 5, "2": [0.0] * 5}
        for cid, snaps, trades, city, ld in data:
            r = bk.run_bucket(cid, snaps, trades, city, ld, allow)
            if not r:
                continue
            lo, hi = rewards(snaps, city, ld, thr)
            h = res["1" if ld < bk.SPLIT else "2"]
            h[0] += r["spent"]; h[1] += r["pnl"]; h[2] += lo; h[3] += hi; h[4] += 1
        for half, lab in (("1", "19.08-07.09"), ("2", "08.09-27.09")):
            sp, pn, lo, hi, n = res[half]
            print(f"  {name:30s} {lab}: вариантов {n}, потрачено ${sp:,.0f}, сделки ${pn:+,.0f} ({100 * pn / max(sp, 1):+.2f}%) | "
                  f"награды ${lo:,.0f}…${hi:,.0f} (+{100 * lo / max(sp, 1):.2f}…+{100 * hi / max(sp, 1):.2f} п.п.; "
                  f"${lo / max(n, 1):.2f}…${hi / max(n, 1):.2f} на вариант) → вместе {100 * (pn + lo) / max(sp, 1):+.2f}…"
                  f"{100 * (pn + hi) / max(sp, 1):+.2f}%", flush=True)
        ok = all(res[h][2] / max(res[h][0], 1) >= 0.01 for h in ("1", "2"))
        print(f"  {name}: порог (нижняя оценка ≥ +1 п.п. в обеих половинах) → {'ПРОШЛО' if ok else 'не прошло'}", flush=True)


if __name__ == "__main__":
    main()
