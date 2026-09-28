"""
Перекосы рынка, три проверки (2026-09-28, после no_cheap; Alex: «сразу приступай»).

1. Зеркало no_cheap — «да» на недооценённого фаворита: вариант за 50-95¢ (и 65-95¢), который смесь v3 + рынок считает
   недооценённым (шанс смеси > цены), одна ставка на город-день (наибольшая недооценка); «да» не дороже цены + 1¢.
   Порог: июль-авг по цене 08:00 и с 21.08 по настоящим сделкам — оба в плюсе после комиссии.
2. Сумма цен: бывает ли, что купить «да» на ВСЕ варианты по цене продавца дешевле $1 (гарантированный выигрыш),
   по снимкам с best_ask (с 22.09); и как распределена сумма цен рынка.
3. Своя заявка для no_cheap: в 08:00 тот же сигнал, но вместо покупки по цене продавца ставим свою заявку «нет»
   на δ дешевле (0 / 1 / 2¢), без комиссии, ждём до 12:00. Исполнилась — если в окне была сделка по «нет» строго
   ниже нашей цены (с запасом на очередь). Сравнение с покупкой сразу (как в кошельке). Только настоящие сделки.
Только на копии базы.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import weather_study_0926 as base
from weather_cities import OBS_CITIES

conn = base.conn


def fav(days, lo, hi):
    n = won = nr = 0
    pnl = pr = st = 0.0
    for d in days:
        c = [(d["blend"][b] - p, b, p) for b, p in zip(d["keys"], d["price"]) if lo <= p < hi and d["blend"][b] > p]
        if not c:
            continue
        _, b, p = max(c)
        w = b[0] == d["win"]
        n += 1; won += w; pnl += base.pnl(p, w)
        fp = base.real_fill(d["city"], d["date"], b[0], "yes", min(0.97, p + 0.01))
        if fp not in (None, "nodata"):
            nr += 1; pr += base.pnl(fp - 0.01, w); st += base.STAKE
    return n, won, pnl, nr, pr, st


def overround():
    rows = conn.execute("""SELECT city, ts_utc, COUNT(*), SUM(best_ask), SUM(market_p), SUM(best_ask IS NULL)
                           FROM snapshots WHERE best_ask IS NOT NULL OR ts_utc >= '2026-09-22' GROUP BY city, ts_utc""").fetchall()
    full = [r for r in rows if r[5] == 0 and r[2] >= 5]
    asks = sorted(r[3] for r in full)
    mk = sorted(r[4] for r in full)
    under = [r for r in full if r[3] < 0.99]
    q = lambda a, p: a[int(p * (len(a) - 1))] if a else float("nan")
    print(f"\n=== 2. Сумма цен по всем вариантам (снимки с ценой продавца по каждому варианту: {len(full)}) ===")
    print(f"сумма цен продавцов «да»: мин {asks[0]:.3f}, 5% {q(asks, .05):.3f}, медиана {q(asks, .5):.3f}, 95% {q(asks, .95):.3f}")
    print(f"сумма цен рынка: мин {mk[0]:.3f}, медиана {q(mk, .5):.3f}, макс {mk[-1]:.3f}")
    print(f"снимков, где купить «да» на все варианты дешевле $0.99: {len(under)}"
          + (" — " + ", ".join(f"{r[0]} {r[1][:16]} Σ={r[3]:.3f}" for r in under[:8]) if under else ""))


def maker_fill(city, date, lo, limit_no, h0=8, h1=12):
    """Своя заявка «нет» по limit_no: исполнена, если в [h0, h1) местного была сделка по «нет» строго дешевле limit_no."""
    key = (city, date, lo)
    if date < base.FIRST_TRADE or key not in base.CID:
        return "nodata"
    tz = ZoneInfo(OBS_CITIES[city]["tz"])
    t0 = datetime.fromisoformat(date).replace(tzinfo=tz).timestamp()
    for o, sd, p, t in conn.execute("SELECT outcome, side, price, ts FROM poly_trades WHERE condition_id = ? AND ts BETWEEN ? AND ?",
                                    (base.CID[key], t0 + h0 * 3600, t0 + h1 * 3600)):
        px = p if o == "No" else 1 - p
        if px < limit_no - 1e-9:
            return True
    return False


def maker(days, delta):
    n = nf = won = 0
    pnl = 0.0
    for d in days:
        c = [(p - d["blend"][b], b, p) for b, p in zip(d["keys"], d["price"]) if 0.05 <= p < 0.15 and d["blend"][b] < p]
        if not c:
            continue
        _, b, p = max(c)
        lim = round(1 - p - delta, 3)
        f = maker_fill(d["city"], d["date"], b[0], lim)
        if f == "nodata":
            continue
        n += 1
        if f:
            w = b[0] != d["win"]
            nf += 1; won += w
            pnl += (base.STAKE / lim - base.STAKE) if w else -base.STAKE
    return n, nf, won, pnl


if __name__ == "__main__":
    days = base.load_days()
    ja = [d for d in days if "2026-07-01" <= d["date"] < "2026-09-01"]
    late = [d for d in days if d["date"] >= base.FIRST_TRADE]
    print("=== 1. «Да» на недооценённого фаворита (смесь считает недооценённым), одна ставка на город-день ===")
    for lo, hi in ((0.50, 0.95), (0.65, 0.95), (0.35, 0.65)):
        a, s = fav(ja, lo, hi), fav(late, lo, hi)
        print(f"{lo * 100:.0f}-{hi * 100:.0f}¢ | июль-авг по цене 08:00: {a[0]} ставок, угадано {a[1] / max(a[0], 1) * 100:.1f}%, "
              f"{a[2]:+.1f}$ ({a[2] / max(a[0] * base.STAKE, 1) * 100:+.1f}%) | с 21.08, настоящие сделки: {s[3]} ставок "
              f"{s[4]:+.1f}$ ({s[4] / max(s[5], 1) * 100:+.1f}%)", flush=True)
    overround()
    print("\n=== 3. no_cheap: своя заявка «нет» (без комиссии, до 12:00) против покупки сразу ===")
    import weather_study_longshot as ls
    t = ls.run_one(late, 0.05, 0.15)
    print(f"покупка сразу (как в кошельке): {t[3]} ставок {t[4]:+.1f}$ ({t[4] / max(t[5], 1) * 100:+.1f}%)")
    for delta in (0.0, 0.01, 0.02):
        n, nf, won, pnl = maker(late, delta)
        print(f"своя заявка на {delta * 100:.0f}¢ дешевле: сигналов {n}, исполнилось {nf} ({nf / max(n, 1) * 100:.0f}%), "
              f"«нет» сыграло {won / max(nf, 1) * 100:.1f}%, итог {pnl:+.1f}$ ({pnl / max(nf * base.STAKE, 1) * 100:+.1f}%)", flush=True)
