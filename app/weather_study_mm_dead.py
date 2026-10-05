"""Правило 30¢ + фильтр «мёртвых» вариантов по замерам METAR (станция уже показала больше верхней границы варианта —
«да» проиграет наверняка). Замер считаем известным через 10 мин после времени наблюдения. Выбор/проверка — половины."""
import sys, bisect
sys.path.insert(0, "/app")
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import weather_study_mm_book as bk
from weather_study_mm_book2 import rule, cinfo
from weather_cities import OBS_CITIES
conn = bk.conn
obs = {}
for city, v, tf in conn.execute("SELECT city, valid_utc, tmpf FROM station_obs WHERE valid_utc >= '2026-08-17' AND tmpf IS NOT NULL ORDER BY city, valid_utc"):
    if city not in OBS_CITIES: continue
    cfg = OBS_CITIES[city]
    t = datetime.fromisoformat(v).replace(tzinfo=timezone.utc)
    d = t.astimezone(ZoneInfo(cfg["tz"])).date().isoformat()
    val = round(tf) if cfg["unit"] == "fahrenheit" else round((tf - 32) * 5 / 9)
    s = obs.setdefault((city, d), [[], []])
    s[0].append(t.timestamp() + 600); s[1].append(max(val, s[1][-1]) if s[1] else val)
base = rule(thr=0.30, late=0.30)
stats = {"dead": 0}
def dead_filter(z, price, spread, cid, ts, side):
    if not base(z, price, spread, cid, ts, side): return False
    c = cinfo.get(cid)
    if not c or side != "yes": return True
    s = obs.get((c[0], c[1]))
    if not s: return True
    i = bisect.bisect_right(s[0], ts) - 1
    if i >= 0 and c[3] < 900 and s[1][i] > c[3]:   # максимум уже выше верхней границы — «да» мёртв
        stats["dead"] += 1; return False
    return True
data = bk.load()
for name, al in (("30¢", base), ("30¢ + без мёртвых вариантов", dead_filter)):
    res = [(ld, bk.run_bucket(cid, b, t, c, ld, al)) for cid, b, t, c, ld in data]
    for half, cond in (("19.08-07.09", lambda ld: ld < bk.SPLIT), ("08.09-27.09", lambda ld: ld >= bk.SPLIT)):
        rs = [r for ld, r in res if r and cond(ld)]
        sp, pn = sum(r["spent"] for r in rs), sum(r["pnl"] for r in rs)
        print(f"{name:30s} {half}: {100 * pn / max(sp, 1):+6.2f}%  ${pn:+7,.0f}  исполнений {sum(len(r['fills']) for r in rs)}", flush=True)
print("отсечено заявок на мёртвые варианты:", stats["dead"])
