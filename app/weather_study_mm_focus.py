"""
«Сосредоточенный $100» (2026-10-07, решение Alex). mm100 (банк $100) проиграл −$27 за 02-06.10, хотя тот же бот без ограничения
банка (mm_ws_zs, строгое исполнение) +$634: с $100 пары почти не складываются — обе стороны куплены лишь в 8 из 437 маркетов
(у mm_ws_zone 31%), деньги расходятся по сотням маркетов по 5 долей, и бот по сути покупает «да» дешевле 10¢ (675 из 977
покупок) — лотерею. Пиковый «банк» прибыльного бота — ~$5 800 в позициях.

Идея: тот же бот, но сосредоточен — каждый день только K городов с самой большой торговлей в ПРОШЛЫЕ дни (решается заранее), и
в маркете без пары не больше CAP долей одной стороны (дальше берём только вторую сторону — ждём пару).

Как проверяем (без новых данных): исполнения строгого бота mm_ws_zs (data/db/mm.sqlite3, mm_ws_fills) — то, что гарантированно
исполнилось бы вживую: заявка стояла ≥ 1 с, сделка прошла строго хуже нашей цены. Сосредоточенный бот ставит те же заявки
только в выбранных маркетах, по 5 долей (минимум Polymarket), при свободных деньгах (банк $100, покупка замораживает цена × доли;
пара «да» + «нет» склеивается сразу в $1); доли без пары — по итогу маркета. Без возврата комиссии мейкеру (в жизни чуть лучше).
Задержка: SLOW — дополнительно выбрасываем исполнения, где заявка простояла бы меньше 3 с (у нас нет её возраста — грубо:
выбрасываем каждое исполнение в первые 3 с после предыдущего исполнения того же маркета, то есть в «горячие» секунды).

ПОРОГ (записан 07.10 до запуска): основной вариант K=2, CAP=5, за 02-06.10 (закрытые маркеты) — итог > 0 и склеено ≥ 50% долей.
Прошёл → бумажный кошелёк mm100f рядом с mm100 (живые правила mm100 + сосредоточение). Не прошёл → PRD §10.
Запуск: docker compose run --rm collector weather_study_mm_focus.py
"""

import sqlite3
from collections import defaultdict
from datetime import datetime

MM_DB = "/data/db/mm.sqlite3"
BANK, SIZE = 100.0, 5.0
FROM, TO = "2026-10-02", "2026-10-06"


def run(conn, fills, final, settled_ts, liq, k_cities, cap, slow=False):
    cash, merged_sh, filled_sh, pnl_merge = BANK, 0.0, 0.0, 0.0
    inv = defaultdict(lambda: [0.0, 0.0, 0.0])     # cid -> [да без пары, нет без пары, затраты без пары]
    open_ = {}                                     # cid -> время итога
    last_fill = {}
    picks = {}
    for ts, cid, side, pr, sz, city, d in fills:
        # итоги маркетов, закрывшихся до этой сделки, возвращают деньги
        for c2 in [c for c, t in open_.items() if t <= ts]:
            y, n, cost = inv.pop(c2)
            cash += y * final[c2] + n * (1 - final[c2])
            del open_[c2]
        if d not in picks:   # города дня — по торговле в прошлые дни (решается заранее)
            prev = sorted(((v, c) for (c, dd), v in liq.items() if dd < d), reverse=True)
            tot = defaultdict(float)
            for v, c in prev:
                tot[c] += v
            picks[d] = {c for c, _ in sorted(tot.items(), key=lambda x: -x[1])[:k_cities]}
        if city not in picks[d] or cid not in final:
            continue
        if slow and ts - last_fill.get(cid, -1e9) < 3.0:
            last_fill[cid] = ts
            continue
        last_fill[cid] = ts
        y, n, cost = inv[cid]
        mine = y if side == "yes" else n
        other = n if side == "yes" else y
        if mine - other >= cap:      # перекос: ждём вторую сторону
            continue
        k = min(SIZE, sz)
        if cash < pr * k:
            continue
        cash -= pr * k
        filled_sh += k
        if side == "yes":
            y += k
        else:
            n += k
        cost += pr * k
        m = min(y, n)
        if m > 0:                    # склейка «да» + «нет» → $1 за пару
            cash += m
            merged_sh += 2 * m
            pnl_merge += m - cost * (2 * m) / (y + n)
            cost -= cost * (2 * m) / (y + n)
            y, n = y - m, n - m
        inv[cid] = [y, n, cost]
        open_[cid] = settled_ts.get(cid, 1e18)
    for c2, (y, n, cost) in inv.items():
        cash += y * final[c2] + n * (1 - final[c2])
    return {"pnl": cash - BANK, "merged": merged_sh, "filled": filled_sh,
            "share": merged_sh / filled_sh if filled_sh else 0.0, "cities": sorted({c for v in picks.values() for c in v})}


if __name__ == "__main__":
    conn = sqlite3.connect(f"file:{MM_DB}?mode=ro", uri=True, timeout=60)
    final, settled_ts = {}, {}
    for cid, fy, st in conn.execute("SELECT condition_id, final_yes, settled_at FROM mm_results WHERE wallet = 'mm_ws_zs' AND final_yes IS NOT NULL"):
        final[cid] = fy
        settled_ts[cid] = datetime.fromisoformat(st).timestamp() if st else 1e18
    fills = conn.execute("""SELECT ts, condition_id, side, price, size, city, local_date FROM mm_ws_fills WHERE wallet = 'mm_ws_zs'
                            AND local_date BETWEEN ? AND ? ORDER BY ts""", (FROM, TO)).fetchall()
    # торговля по городу и дню — доли через исполнения самого широкого бота mm_ws_all (с 30.09; мера ликвидности, известна после дня)
    liq = defaultdict(float)
    for city, d, s in conn.execute("SELECT city, local_date, SUM(size) FROM mm_ws_fills WHERE wallet = 'mm_ws_all' GROUP BY 1, 2"):
        liq[(city, d)] += s
    print(f"исполнений строгого бота {FROM}..{TO}: {len(fills)}, закрытых маркетов {len(final)}")
    ref = conn.execute("SELECT ROUND(SUM(pnl), 2) FROM mm_results WHERE wallet = 'mm100' AND local_date BETWEEN ? AND ?", (FROM, TO)).fetchone()[0]
    print(f"для сравнения mm100 (все маркеты, тот же период): {ref:+.2f}$\n")
    for k in (1, 2, 3, 5):
        for cap in (5, 10):
            for slow in (False, True):
                r = run(conn, fills, final, settled_ts, liq, k, cap, slow)
                main = k == 2 and cap == 5 and not slow
                print(f"{'→ ' if main else '  '}городов {k}, перекос ≤ {cap:2.0f}, задержка {'3 с' if slow else '1 с'}: итог {r['pnl']:+7.2f}$ "
                      f"({r['pnl']:+.1f}% к банку), склеено {100 * r['share']:.0f}% из {r['filled']:.0f} долей", flush=True)
    r = run(conn, fills, final, settled_ts, liq, 2, 5)
    ok = r["pnl"] > 0 and r["share"] >= 0.5
    print(f"\nПОРОГ (K=2, перекос 5, итог > 0 и склеено ≥ 50%): {'ПРОШЁЛ' if ok else 'НЕ ПРОШЁЛ'} — итог {r['pnl']:+.2f}$, "
          f"склеено {100 * r['share']:.0f}%; города: {', '.join(r['cities'])}")
