"""
Бот-мейкер на настоящих стаканах — настройка правила и связка с моделью (2026-09-30, Alex: «это максимум от этих данных?»).
Данные и исполнение — weather_study_mm_book.py (стаканы Falcon, 19.08-27.09). Базовое правило — «только дешёвая сторона»
(ниже 50¢, с 15 до 18 ч — ниже 30¢; +6.9% на проверке). Варианты: граница цены; минимальная разница заявка/предложение;
размер заявки; фильтр по модели — покупать дешёвую сторону, только если смесь v3 + рынок (на 08:00 дня маркета, проверка
вслепую ml_preds_var_mkt) не считает её переоценённой (честная цена стороны ≥ цены заявки + запас); до 08:00 фильтра нет.
Выбор — по первой половине (19.08-07.09), итог — на второй (08.09-27.09). Плюс риск: итог по дням, худший день, капитал.
Порог (записан до прогона): вариант, лучший на первой половине, лучше базового на второй на ≥ 1 п.п. доходности.
Только на копии.
"""
import json
import statistics
from collections import defaultdict

import weather_ml_check as chk
import weather_ml_q as mq
import weather_study_mm_book as bk
from weather_ml_live import blend_with_market

conn = bk.conn
cinfo = {r[0]: r[1:] for r in conn.execute("SELECT condition_id, city, local_date, bucket_lo, bucket_hi FROM poly_market_final")}


def fair_values():
    fv = {}
    for city, d, unit, qs in conn.execute("SELECT city, date, unit, qs FROM ml_preds_var_mkt WHERE date >= '2026-08-18'"):
        pr = chk.prices(conn, city, d, "A")
        if len(pr) < 3:
            continue
        keys = list(pr)
        q = json.loads(qs)
        m = [mq.bucket_prob(q, unit, b[0], b[1]) for b in keys]
        t = sum(m) or 1.0
        for b, p in zip(keys, blend_with_market([x / t for x in m], [pr[b] for b in keys])):
            fv[(city, d, b[0], b[1])] = p
    return fv


FV = fair_values()


def rule(thr=0.50, late=0.30, min_spread=0.0, fv_margin=None):
    def ok(zone, price, spread, cid, ts, side):
        if price >= (late if zone == "15-18" else thr) or spread < min_spread - 1e-9:
            return False
        if fv_margin is None or zone in ("накануне", "0-6", "6-9"):   # честная цена — с 08:00 дня маркета (с зоны 9-12)
            return True
        c = cinfo.get(cid)
        f = FV.get((c[0], c[1], c[2], c[3])) if c else None
        if f is None:
            return True
        side_fv = f if side == "yes" else 1 - f
        return side_fv >= price + fv_margin   # не покупаем сторону, которую смесь модели и рынка считает переоценённой
    return ok


def run(data, allow, size=10.0):
    bk.SIZE = size
    res = [(ld, bk.run_bucket(cid, b, t, c, ld, allow)) for cid, b, t, c, ld in data]
    bk.SIZE = 10.0
    out = {}
    for half, cond in (("1", lambda ld: ld < bk.SPLIT), ("2", lambda ld: ld >= bk.SPLIT)):
        rs = [r for ld, r in res if r and cond(ld)]
        sp, pn = sum(r["spent"] for r in rs), sum(r["pnl"] for r in rs)
        out[half] = (pn, sp, sum(len(r["fills"]) for r in rs))
    return out, res


if __name__ == "__main__":
    data = bk.load()
    print(f"вариантов {len(data)}; честных цен модели {len(FV)}", flush=True)
    variants = {"база (<50¢, 15-18 <30¢)": (rule(), 10.0)}
    for thr in (0.30, 0.40, 0.60):
        variants[f"граница {int(thr * 100)}¢"] = (rule(thr=thr, late=min(thr, 0.30)), 10.0)
    for ms in (0.01, 0.02, 0.04):
        variants[f"разница ≥ {int(ms * 100)}¢"] = (rule(min_spread=ms), 10.0)
    variants["размер 25 долей"] = (rule(), 25.0)
    variants["+ модель: не переоценена"] = (rule(fv_margin=0.0), 10.0)
    variants["+ модель: запас 2 п.п."] = (rule(fv_margin=0.02), 10.0)
    rows = {}
    for name, (al, size) in variants.items():
        o, _ = run(data, al, size)
        rows[name] = o
        print(f"  {name:26s} | 19.08-07.09: {100 * o['1'][0] / max(o['1'][1], 1):+6.2f}% (${o['1'][0]:+7,.0f}, {o['1'][2]:6d} исп.) "
              f"| 08.09-27.09: {100 * o['2'][0] / max(o['2'][1], 1):+6.2f}% (${o['2'][0]:+7,.0f}, {o['2'][2]:6d} исп.)", flush=True)
    base = rows["база (<50¢, 15-18 <30¢)"]
    best = max(rows, key=lambda k: rows[k]["1"][0] / max(rows[k]["1"][1], 1))
    r2 = lambda o: 100 * o["2"][0] / max(o["2"][1], 1)
    print(f"\nлучший на первой половине: {best}; на второй {r2(rows[best]):+.2f}% против базы {r2(base):+.2f}% → "
          + ("ПРОШЛО" if r2(rows[best]) - r2(base) >= 1 else "не прошло"), flush=True)
    # риск базового правила по дням (вторая половина)
    _, res = run(data, rule())
    day = defaultdict(lambda: [0.0, 0.0])
    for ld, r in res:
        if r and ld >= bk.SPLIT:
            day[ld][0] += r["pnl"]; day[ld][1] += r["spent"]
    pn = [v[0] for v in day.values()]
    print(f"\nриск (база, 08.09-27.09, {len(pn)} дней): в среднем ${statistics.mean(pn):+.0f}/день, разброс ±${statistics.pstdev(pn):.0f}, "
          f"худший день ${min(pn):+.0f}, лучший ${max(pn):+.0f}, дней в минусе {sum(p < 0 for p in pn)}; "
          f"тратится в среднем ${statistics.mean(v[1] for v in day.values()):,.0f}/день (деньги заняты ~1-2 дня)")
    top3 = conn.execute("""SELECT SUM(n) FROM (SELECT COUNT(*) n FROM poly_trades t JOIN poly_market_final f ON f.condition_id = t.condition_id
                           GROUP BY f.city, f.local_date, t.condition_id ORDER BY 1)""").fetchone()[0]
    covered = sum(len(t) for _, _, t, _, _ in data)
    print(f"выборка (3 самых торгуемых варианта в маркет-дне) — {100 * covered / max(top3, 1):.0f}% всех сделок погоды")
