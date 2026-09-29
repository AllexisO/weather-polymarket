"""
Поток сильных трейдеров до 08:00 (2026-09-29, очередь недельного разбора, п. 1). Вопрос: знают ли сильные трейдеры
больше рынка, и видно ли это по их покупкам ДО нашего решения в 08:00?

Честно по времени: сильные на неделю W — по сделкам 14 дней до W, маркеты которых уже закрылись до W (как
weather_sharp_rank: ≥ MIN_N сделок, в плюсе, ≥ 5% от оборота, топ-30 по итогу). Их чистая покупка «да» по каждому
варианту — от открытия маркета до 08:00 местного в день маркета (купил «да» / продал «нет» = +, наоборот = −).
Проверка:
  1) калибровка: варианты, которые сильные чисто купили, сбываются чаще своей цены в 08:00? (сбылось − Σ цен)
  2) деньги: «да» на вариант с наибольшей чистой покупкой сильных, если она ≥ порога, — по цене 08:00 и по настоящим
     сделкам после 08:00 (real_fill, цена + 1¢); то же «нет» на вариант, который они чисто продали.
Порог (записан до прогона): плюс по настоящим сделкам и сбылось больше Σ цен минимум на 2 ст. ошибки. Только на копии.
"""
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import weather_study_0926 as base
from weather_cities import OBS_CITIES

conn = base.conn
MIN_SHARES = 20


def load():
    mk = {r[0]: r[1:] for r in conn.execute("SELECT condition_id, city, local_date, bucket_lo, bucket_hi, final_yes FROM poly_market_final")}
    wal = {r[:6]: r[6] for r in conn.execute("SELECT tx, asset, ts, price, size, side, wallet FROM poly_trade_wallets")}
    tr = []
    for tx, asset, ts, price, size, side, cid, outc, w in conn.execute(
            "SELECT tx, asset, ts, price, size, side, condition_id, outcome, wallet FROM poly_trades"):
        if cid not in mk:
            continue
        w = w or wal.get((tx, asset, ts, price, size, side))
        tr.append((ts, cid, outc, side, price, size, w))
    tr.sort()
    return mk, tr


def sharp_set(tr, mk, week_start_ts, min_n, top):
    lo = week_start_ts - 14 * 86400
    st = defaultdict(lambda: [0, 0.0, 0.0])
    for ts, cid, outc, side, price, size, w in tr:
        if not w or ts < lo or ts >= week_start_ts:
            continue
        city, d, blo, bhi, fy = mk[cid]
        tz = ZoneInfo(OBS_CITIES[city]["tz"])
        if datetime.fromisoformat(d).replace(tzinfo=tz).timestamp() + 86400 > week_start_ts:
            continue  # итог ещё не был известен к началу недели
        fin = fy if outc == "Yes" else 1 - fy
        s = st[w]
        s[0] += 1
        s[1] += size * (fin - price) if side == "BUY" else size * (price - fin)
        s[2] += size * price
    good = [(s[1], w) for w, s in st.items() if s[0] >= min_n and s[1] > 0 and s[1] / max(s[2], 1) >= 0.05]
    return {w for _, w in sorted(good, reverse=True)[:top]}


def main():
    mk, tr = load()
    by_cid = defaultdict(list)
    for t in tr:
        by_cid[t[1]].append(t)
    days = defaultdict(list)
    for cid, (city, d, blo, bhi, fy) in mk.items():
        days[(city, d)].append((blo, bhi, cid, fy))
    first = datetime.fromtimestamp(tr[0][0], timezone.utc).date()
    for min_n, top in ((100, 30), (30, 60)):
        stats = {"yes": [0, 0.0, 0, 0.0, 0.0, 0, 0.0, 0.0], "no": [0, 0.0, 0, 0.0, 0.0, 0, 0.0, 0.0]}
        cal = [0, 0.0, 0.0, 0]  # вариантов, сбылось, Σ цен, Σ p(1-p)
        w = first + timedelta(days=14 - first.weekday() + 7)
        weeks = 0
        while True:
            ws = datetime(w.year, w.month, w.day, tzinfo=timezone.utc).timestamp()
            if ws > tr[-1][0]:
                break
            sharp = sharp_set(tr, mk, ws, min_n, top)
            weeks += 1
            for (city, d), bs in days.items():
                if not (w.isoformat() <= d < (w + timedelta(days=7)).isoformat()):
                    continue
                tz = ZoneInfo(OBS_CITIES[city]["tz"])
                t8 = datetime.fromisoformat(d).replace(tzinfo=tz).timestamp() + 8 * 3600
                rows = []
                for blo, bhi, cid, fy in bs:
                    net, last = 0.0, None
                    for ts, _, outc, side, price, size, wl in by_cid[cid]:
                        if ts >= t8:
                            break
                        last = price if outc == "Yes" else 1 - price
                        if wl in sharp:
                            sg = (1 if side == "BUY" else -1) * (1 if outc == "Yes" else -1)
                            net += sg * size
                    if last is not None and fy is not None:
                        rows.append((net, blo, last, fy))
                if len(rows) < 3:
                    continue
                for net, blo, p, fy in rows:
                    if net >= MIN_SHARES and 0.02 <= p <= 0.98:
                        cal[0] += 1; cal[1] += fy; cal[2] += p; cal[3] += p * (1 - p)
                for side in ("yes", "no"):
                    net, blo, p, fy = max(rows) if side == "yes" else min(rows)
                    if (side == "yes" and net < MIN_SHARES) or (side == "no" and net > -MIN_SHARES):
                        continue
                    px = p if side == "yes" else 1 - p
                    if not 0.03 <= px <= 0.95:
                        continue
                    won = (fy > 0.5) if side == "yes" else (fy < 0.5)
                    s = stats[side]
                    s[0] += 1; s[1] += base.pnl(px, won); s[2] += won; s[3] += 2.0
                    fill = base.real_fill(city, d, blo, side, min(0.97, px + 0.01))
                    if fill not in (None, "nodata"):
                        s[5] += 1; s[6] += base.pnl(fill, won); s[7] += 2.0
            w += timedelta(days=7)
        print(f"\n=== сильные: ≥{min_n} сделок, топ-{top}; недель проверки {weeks} ===")
        if cal[0]:
            se = math.sqrt(cal[3])
            print(f"калибровка: вариантов, которые сильные чисто купили до 08:00 (≥{MIN_SHARES} шт.): {cal[0]}; сбылось {cal[1]:.0f}, "
                  f"рынок ожидал {cal[2]:.1f} → разница {cal[1] - cal[2]:+.1f} ({(cal[1] - cal[2]) / se:+.1f} ст. ошибки)")
        for side, s in stats.items():
            print(f"«{'да' if side == 'yes' else 'нет'}» на вариант, который сильные больше всех {'купили' if side == 'yes' else 'продали'}: "
                  f"по цене 08:00 {s[0]} ставок {s[1]:+.1f}$ ({s[1] / max(s[3], 1) * 100:+.1f}%), угадано {s[2]} | "
                  f"по настоящим сделкам {s[5]} ставок {s[6]:+.1f}$ ({s[6] / max(s[7], 1) * 100:+.1f}%)", flush=True)


if __name__ == "__main__":
    main()
