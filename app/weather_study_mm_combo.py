"""
Сумма маленьких улучшений для бота-мейкера (2026-10-01, Alex: «если брать по кусочку с каждой стороны — это какой-никакой
процент»). На стаканах Falcon (weather_study_mm_book.py): правило 30¢ + разница заявка/предложение ≥ 2¢ + фильтр смесью v3 +
рынок (сторона не переоценена, с 08:00 дня маркета). Все кусочки выбраны раньше по отдельности; здесь — один честный прогон
связки. Порог (записан до прогона): связка лучше правила 30¢ на ≥ 1 п.п. доходности на 08.09-27.09 и не хуже на 19.08-07.09.
Только на копии.
"""
import weather_study_mm_book as bk
from weather_study_mm_book2 import rule

if __name__ == "__main__":
    data = bk.load()
    res = {}
    for name, al in (("30¢", rule(thr=0.30, late=0.30)),
                     ("30¢ + разница ≥ 2¢", rule(thr=0.30, late=0.30, min_spread=0.02)),
                     ("30¢ + модель", rule(thr=0.30, late=0.30, fv_margin=0.0)),
                     ("30¢ + разница ≥ 2¢ + модель", rule(thr=0.30, late=0.30, min_spread=0.02, fv_margin=0.0))):
        rs = [(ld, bk.run_bucket(cid, b, t, c, ld, al)) for cid, b, t, c, ld in data]
        res[name] = {}
        for half, cond in (("1", lambda ld: ld < bk.SPLIT), ("2", lambda ld: ld >= bk.SPLIT)):
            x = [r for ld, r in rs if r and cond(ld)]
            sp, pn = sum(r["spent"] for r in x), sum(r["pnl"] for r in x)
            res[name][half] = (100 * pn / max(sp, 1), pn, sum(len(r["fills"]) for r in x))
        o = res[name]
        print(f"{name:30s} 19.08-07.09: {o['1'][0]:+6.2f}% (${o['1'][1]:+,.0f}, {o['1'][2]} исп.) | 08.09-27.09: {o['2'][0]:+6.2f}% "
              f"(${o['2'][1]:+,.0f}, {o['2'][2]} исп.)", flush=True)
    b, c = res["30¢"], res["30¢ + разница ≥ 2¢ + модель"]
    ok = c["2"][0] - b["2"][0] >= 1 and c["1"][0] >= b["1"][0]
    print(f"\nсвязка против 30¢: {c['2'][0] - b['2'][0]:+.2f} п.п. на проверке → {'ПРОШЛО' if ok else 'не прошло'}")
