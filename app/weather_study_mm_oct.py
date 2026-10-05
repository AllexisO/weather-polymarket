"""
Бот-мейкер на свежих стаканах 28.09-03.10 (2026-10-05, Alex: докачать остаток кредитов Falcon и перепроверить бота до
решения 14.10). Стаканы — weather_falcon_fetch.py (OUT_DIR=book_oct), 3 самых торгуемых варианта в маркет-дне; исполнение —
weather_study_mm_book.py, правила — weather_study_mm_book2.py. Этот период при подборе правил не использовался.
Порог (записан до прогона): правило 30¢ (живой mm_ws_z30) — ≥ +5% от потраченного и ≥ 300 исполнений.
Для сравнения (без порога): все заявки, база <50¢ (15-18 ч <30¢). Только на копии:
  docker compose run --rm -e JOB_TIMEOUT=0 -e POLY_LAB_DB=/data/research/research.sqlite3 -e BOOK_DIR=/data/research/falcon/book_oct \
    --entrypoint python collector weather_study_mm_oct.py
"""
import os
import statistics
from collections import defaultdict

os.environ.setdefault("BOOK_DIR", "/data/research/falcon/book_oct")
import weather_study_mm_book as bk
import weather_study_mm_book2 as b2

data = bk.load()
print(f"вариантов со стаканом {len(data)}, их сделок {sum(len(d[2]) for d in data)}", flush=True)
for name, allow in (("все заявки", None), ("база <50¢ (15-18 <30¢)", b2.rule()), ("граница 30¢ (живой mm_ws_z30)", b2.rule(thr=0.30, late=0.30))):
    res = [(ld, bk.run_bucket(cid, b, t, c, ld, allow)) for cid, b, t, c, ld in data]
    rs = [(ld, r) for ld, r in res if r]
    sp, pn, nf = sum(r["spent"] for _, r in rs), sum(r["pnl"] for _, r in rs), sum(len(r["fills"]) for _, r in rs)
    day = defaultdict(lambda: [0.0, 0.0])
    for ld, r in rs:
        day[ld][0] += r["pnl"]; day[ld][1] += r["spent"]
    print(f"  {name:30s}: исполнений {nf:6d}, потрачено ${sp:8,.0f}, итог ${pn:+8,.0f} ({100 * pn / max(sp, 1):+6.2f}%); по дням: "
          + ", ".join(f"{d[8:10]}.{d[5:7]} {100 * v[0] / max(v[1], 1):+.0f}%" for d, v in sorted(day.items())), flush=True)
    if "30¢" in name:
        per = sorted((r["pnl"] for _, r in rs), reverse=True)
        print(f"    без 10 лучших вариантов: ${sum(per) - sum(per[:10]):+,.0f}; вариантов в плюсе {sum(p > 0 for p in per)} из {len(per)}")
        print("    порог ≥ +5% и ≥ 300 исполнений → " + ("ПРОШЛО" if pn / max(sp, 1) >= 0.05 and nf >= 300 else "не прошло"), flush=True)
