"""
Минимальная цена ставки (2026-09-28, Alex). Живые кошельки 23-28.09: ставки дешевле 15¢ — 7 угаданных из 202 при
17 ожидаемых по цене (−$348), ставки от 15¢ — в плюсе (+$106); на всём рынке варианты за 5-15¢ сбывались в 4.8%
при цене 8.9%. Похоже на переоценку «лотерейных билетов» — но это 5 дней, проверяем на другом периоде.

Порог решения (зафиксирован 28.09 до запуска):
1. июль-август — есть ли переоценка дешёвых вариантов на всём рынке (цена 08:00 против исхода);
   там же выбираем минимальную цену из 3/10/15/20¢ по деньгам (цена 08:00);
2. сентябрь, настоящие сделки — выбранный порог должен дать больше денег, чем нынешние 3¢, И у v3 (10 п.п.),
   И у смеси (3 п.п.). Прошёл — отдельным кошельком, старые не меняем.
Два способа: «лучший из разрешённых» (дешёвые просто не рассматриваем) и «лучший, дешёвый — пропуск» (как сейчас в кошельке).

Только на копии базы.
"""

import weather_study_0926 as base

MINS = (0.03, 0.10, 0.15, 0.20)


def run(days, model, thr, minp, mode):
    """mode 'allowed' — выбираем лучший среди вариантов не дешевле minp; 'skip' — лучший вообще, дешевле minp — пропуск."""
    orig = base.candidates

    def cand(d, m, t, sides=("yes",)):
        c = orig(d, m, t, sides)
        if mode == "allowed":
            return [x for x in c if x[3] >= minp]
        return c[:1] if c and c[0][3] >= minp else []
    base.candidates = cand
    try:
        return base.run_rule(days, model, thr)
    finally:
        base.candidates = orig


def calib(days):
    bands = ((0.0, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 0.25), (0.25, 0.45), (0.45, 1.01))
    out = []
    for lo, hi in bands:
        ps = [(p, b[0] == d["win"]) for d in days for b, p in zip(d["keys"], d["price"]) if lo <= p < hi]
        if ps:
            n = len(ps)
            out.append(f"{lo * 100:.0f}-{min(hi, 1) * 100:.0f}¢: {n} вар., цена {sum(p for p, _ in ps) / n * 100:.1f}%, "
                       f"сбылось {sum(w for _, w in ps) / n * 100:.1f}%")
    return out


if __name__ == "__main__":
    days = base.load_days()
    ja = [d for d in days if "2026-07-01" <= d["date"] < "2026-09-01"]
    se = [d for d in days if d["date"] >= "2026-09-01"]
    print(f"дней: июль-авг {len(ja)}, сентябрь {len(se)}; настоящие сделки с {base.FIRST_TRADE}", flush=True)
    for name, ds in (("июль-август", ja), ("сентябрь", se)):
        print(f"\n--- рынок, {name}: цена в 08:00 против исхода ---")
        for line in calib(ds):
            print("  " + line)
    for model, thr, label in (("raw", 0.10, "v3, перевес 10 п.п. (как ml3)"), ("blend", 0.03, "смесь, 3 п.п. (как ml3_cal)")):
        for mode, mname in (("skip", "лучший, дешёвый — пропуск"), ("allowed", "лучший из разрешённых")):
            print(f"\n=== {label} · {mname} ===", flush=True)
            for minp in MINS:
                a = run(ja, model, thr, minp, mode)
                s = run(se, model, thr, minp, mode)
                real = (f"{s['n_real']:3d} ставок {s['pnl_real']:+7.1f}$ ({s['pnl_real'] / s['staked_real'] * 100:+.0f}%)"
                        if s["n_real"] else "нет сделок")
                print(f"от {minp * 100:2.0f}¢ | июль-авг по цене 08:00: {a['n']:4d} ставок {a['pnl']:+7.1f}$ "
                      f"(угадано {a['won']}, рынок ждал {a['exp_mkt']:.1f}) | сентябрь, настоящие сделки: {real}", flush=True)
