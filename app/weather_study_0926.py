"""
Исследования 2026-09-26 (просьба Alex: «сделай все 8»), общая база + идеи 1, 6, 7, 8.
Протокол: подбор на июле-августе, проверка на сентябре; деньги — и по цене рынка
в 08:00 (price_history, может быть устаревшей), и по НАСТОЯЩИМ сделкам (poly_trades,
с 24.08): первая сделка в течение 30 мин после решения по цене не выше допустимой.
Ставка $2, комиссия и +1¢ спреда — как в weather_ml_check.pnl.

База: проверка вслепую v3 (ml_preds_var_mkt), смесь 35% v3 + 65% рынка.
Запуск — НА КОПИИ базы (не мешать крону):
  sqlite3 data/db/polymarket_lab.sqlite3 ".backup data/research/research.sqlite3"
  docker compose run --rm -e POLY_LAB_DB=/data/research/research.sqlite3 collector weather_study_0926.py [1|6|7|8 ...]
"""

import json
import math
import sqlite3
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import weather_ml_check as chk
import weather_ml_q as mq
from weather_cities import OBS_CITIES
from weather_ml_live import blend_with_market

conn = sqlite3.connect(chk.DB_PATH, timeout=60)
WIN = {(r[0], r[1]): r[2] for r in conn.execute("SELECT city, local_date, win_lo FROM weather_poly_outcomes")}
CID = {(r[0], r[1], r[2]): r[3] for r in conn.execute("SELECT city, local_date, bucket_lo, condition_id FROM poly_market_final")}
FIRST_TRADE = conn.execute("SELECT MIN(local_date) FROM poly_trades_days").fetchone()[0]
STAKE = 2.0


def load_days():
    out = []
    # fetchall: не держать открытый запрос к базе всё время сборки (иначе блокирует запись крона)
    for city, date, unit, qs in conn.execute("SELECT city, date, unit, qs FROM ml_preds_var_mkt ORDER BY date").fetchall():
        w = WIN.get((city, date))
        if w is None:
            continue
        pr = chk.prices(conn, city, date, "A")
        if len(pr) < 3 or not any(b[0] == w for b in pr):
            continue
        keys = sorted(pr)
        q = json.loads(qs)
        P = [mq.bucket_prob(q, unit, b[0], b[1]) for b in keys]
        t = sum(P) or 1.0
        P = [x / t for x in P]
        B = blend_with_market(P, [pr[b] for b in keys])
        out.append({"city": city, "date": date, "keys": keys, "price": [pr[b] for b in keys],
                    "raw": dict(zip(keys, P)), "blend": dict(zip(keys, B)), "win": w})
    return out


def pnl(price, won, stake=STAKE):
    return chk.pnl(price, won) * stake / 5


def real_fill(city, date, lo, side, maxp, hour=8):
    """Первая настоящая сделка за 30 мин после решения, по которой можно было купить
    нужную сторону не дороже maxp. None — не купили бы (или нет данных о сделках)."""
    key = (city, date, lo)
    if date < FIRST_TRADE or key not in CID:
        return "nodata"
    ts = datetime.fromisoformat(date).replace(tzinfo=ZoneInfo(OBS_CITIES[city]["tz"])).timestamp() + hour * 3600
    for o, sd, p, t in conn.execute("""SELECT outcome, side, price, ts FROM poly_trades WHERE condition_id = ?
                                       AND ts BETWEEN ? AND ? ORDER BY ts""", (CID[key], ts, ts + 1800)):
        if side == "yes":
            px = p if o == "Yes" and sd == "BUY" else (1 - p if o == "No" and sd == "SELL" else None)
        else:
            px = p if o == "No" and sd == "BUY" else (1 - p if o == "Yes" and sd == "SELL" else None)
        if px is not None and px <= maxp:
            return px
    return None


def candidates(d, model, thr, sides=("yes",)):
    """Все варианты с перевесом ≥ thr: (перевес, сторона, бакет, цена стороны, шанс стороны по модели)."""
    out = []
    for b, p in zip(d["keys"], d["price"]):
        q = d[model][b]
        if "yes" in sides and q - p >= thr and 0.03 <= p <= 0.95:
            out.append((q - p, "yes", b, p, q))
        if "no" in sides and p - q >= thr and 0.03 <= 1 - p <= 0.95:
            out.append((p - q, "no", b, 1 - p, 1 - q))
    return sorted(out, reverse=True)


def run_rule(days, model, thr, sides=("yes",), per_city=1, sizing=None):
    """Итоги правила: по цене в 08:00 и по настоящим сделкам. sizing(edge, price, q) -> $."""
    res = {"n": 0, "won": 0, "exp": 0.0, "exp_mkt": 0.0, "pnl": 0.0, "staked": 0.0,
           "n_real": 0, "pnl_real": 0.0, "staked_real": 0.0, "curve": []}
    for d in days:
        for edge, side, b, px, q in candidates(d, model, thr, sides)[:per_city]:
            won = (b[0] == d["win"]) if side == "yes" else (b[0] != d["win"])
            stake = sizing(edge, px, q) if sizing else STAKE
            res["n"] += 1
            res["won"] += won
            res["exp"] += q
            res["exp_mkt"] += px
            res["pnl"] += pnl(px + 0.0, won, stake)
            res["staked"] += stake
            fp = real_fill(d["city"], d["date"], b[0], side, min(0.95, q - thr))
            if fp not in (None, "nodata"):
                r = pnl(fp - 0.01, won, stake)  # pnl() добавляет 1¢ спреда; у сделки цена уже реальная
                res["n_real"] += 1
                res["pnl_real"] += r
                res["staked_real"] += stake
                res["curve"].append(r)
    return res


def fmt(r):
    s = (f"ставок {r['n']:4d} угадано {r['won']:4d} (обещано {r['exp']:6.1f}, рынок ждал {r['exp_mkt']:6.1f}) "
         f"| по цене 08:00 {r['pnl']:+7.1f}$")
    if r["n_real"]:
        s += f" | реальные сделки: {r['n_real']:3d} ставок {r['pnl_real']:+7.1f}$ ({r['pnl_real'] / r['staked_real'] * 100:+.0f}% от вложенного)"
    return s


def periods(days):
    return (("июль-авг", [d for d in days if d["date"] < "2026-09-01"]), ("сентябрь", [d for d in days if d["date"] >= "2026-09-01"]))


def study1(days):
    print("\n=== 1. Ставки «против» (покупка «нет» на переоценённый вариант) ===")
    for model, thr, label in (("blend", 0.03, "смесь, 3 п.п."), ("raw", 0.10, "v3, 10 п.п.")):
        for sides, name in ((("yes",), "только «да»"), (("no",), "только «нет»"), (("yes", "no"), "лучшее из обоих")):
            for per, ds in periods(days):
                print(f"{label:14s} {name:16s} {per:9s} " + fmt(run_rule(ds, model, thr, sides)))


def study6(days):
    print("\n=== 6. Размер ставки по перевесу (Келли) ===")
    def kelly(mult, lo=0.5, hi=10.0, bank=100.0):
        return lambda edge, px, q: max(lo, min(hi, bank * mult * edge / (1 - px)))
    for name, sz in (("всегда $2", None), ("Келли ×0.10", kelly(0.10)), ("Келли ×0.25", kelly(0.25))):
        for per, ds in periods(days):
            r = run_rule(ds, "blend", 0.03, ("yes",), sizing=sz)
            worst = min((sum(r["curve"][:i + 1]) for i in range(len(r["curve"]))), default=0)
            print(f"смесь 3 п.п. {name:12s} {per:9s} вложено {r['staked']:7.1f}$ | " + fmt(r) + (f" | худшая просадка {worst:+.1f}$" if r["curve"] else ""))


def study7(days):
    print("\n=== 7. Несколько вариантов в одном городе ===")
    for k in (1, 2, 3):
        for per, ds in periods(days):
            print(f"смесь 3 п.п., до {k} вариантов {per:9s} " + fmt(run_rule(ds, "blend", 0.03, ("yes",), per_city=k)))


def study8(days):
    print("\n=== 8. Выбор городов (подбор на июле-августе, проверка на сентябре) ===")
    fit = [d for d in days if d["date"] < "2026-09-01"]
    test = [d for d in days if d["date"] >= "2026-09-01"]
    by = {}
    for c in OBS_CITIES:
        r = run_rule([d for d in fit if d["city"] == c], "blend", 0.03)
        by[c] = r
    good = [c for c, r in by.items() if r["n"] >= 10 and r["pnl"] > 0]
    print(f"городов в плюсе на июле-августе (≥10 ставок): {len(good)} из {len(by)}")
    for name, cities in (("выбранные", good), ("остальные", [c for c in OBS_CITIES if c not in good])):
        print(f"сентябрь, {name:10s} " + fmt(run_rule([d for d in test if d["city"] in cities], "blend", 0.03)))
    vol = dict(conn.execute("""SELECT city, SUM(n) FROM poly_trades_days WHERE local_date < '2026-09-01' GROUP BY city"""))
    top = set(sorted(vol, key=vol.get, reverse=True)[:16])
    for name, cities in (("16 ликвидных", top), ("32 остальных", set(OBS_CITIES) - top)):
        for per, ds in periods(days):
            print(f"{name:13s} {per:9s} " + fmt(run_rule([d for d in ds if d["city"] in cities], "blend", 0.03)))


if __name__ == "__main__":
    which = sys.argv[1:] or ["1", "6", "7", "8"]
    days = load_days()
    print(f"город-дней: {len(days)} ({days[0]['date']}..{days[-1]['date']}); реальные сделки с {FIRST_TRADE}")
    for w in which:
        globals()[f"study{w}"](days)
