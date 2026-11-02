"""
Глубокая проверка данных и логики кошельков (02.11, Alex «данные как данные, но сама логика должна быть правильной»; каждый вечер
в weather_night_check, группа «Данные и логика»). Только чтение базы, дни маркета ≥ since (вечером — вчера и сегодня).

A. LLM — данные письма против наших архивов: местное время; каждая сводка METAR в письме — настоящая (metar_seen, та же
   температура), и все сводки, пришедшие за 10+ мин до письма, в письме есть; «максимум на сейчас» — по плановым сводкам письма,
   половина вверх; модели из строки прогнозов есть в mm_forecasts (значения live переписываются за день — сверяем только
   состав); строка LightGBM = снимок 08:00 (snapshots_fast); цены = market_json; память, систематические ошибки, таблица
   уверенности — пересчёт той же функцией.
B. LLM — логика: шансы каждого кошелька пересчитываются из его ответа (llm_gem, llm_gem_agy — probs_for + calibrate; llm_agy,
   llm_agy_nomem — нормальное распределение вокруг ответа, разброс agy_sigma; llm_agy_p, llm_skill — probs_for); производные
   (llm_mix, llm_mix5 — mix_probs; llm_cal, llm_agy_cal — market_blend) — из шансов родителя того же часа.
C. Кошельки моделей (weather_paper): повтор выбора по тому же снимку — тот же вариант, шанс и цена; пропуск — по той же причине.
D. Дневная модель (ml_day*): шанс ставки = смесь того часа (ml_day_preds), перевес ≥ EDGE, ml_day_conf — фаворит < CONF_SKIP.

Известные особенности (не ошибки, учтены): перевод часов (местное 01:00-01:59 дважды — сводка ищется в обоих вариантах);
итоги дня (weather_station_daily) пишет weather_station_obs раз в 6 ч — память, ошибки, уверенность и разброс agy у письма
сверяются с базой «на момент письма» (итог дня есть, только если после конца дня города до письма закончился запуск, job_log);
проверка 02.11 на 01.11: 15 из 15 таких писем сходятся. Цены продавца по всем вариантам не храним — «лучший ли выбор» у LLM
не проверяется; почасовой прогноз Open-Meteo не архивируется.

Запуск: docker compose run --rm collector weather_deep_check.py [ГГГГ-ММ-ДД]
"""
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import weather_llm_hour as L
import weather_ml_day as md
import weather_paper as wp
from weather_cities import OBS_CITIES, poly_round

DB_PATH = os.environ.get("POLY_LAB_DB", "/data/db/polymarket_lab.sqlite3")
RE_OBS = re.compile(r"^(\d\d):(\d\d) temp (-?[\d.]+)°([FC]) .*?(\[SPECI — does not count for settlement\])?$")
HEADS = ("YOUR ", "HOW OFTEN", "Market buckets", "Your past", "Today's METAR", "Remaining hourly", "Daily max", "NWS ", "LightGBM")
SECTIONS = {
    "A сводки METAR": "каждая сводка в письме LLM — настоящая (наш архив, та же температура), все пришедшие сводки — в письме",
    "A максимум на сейчас": "«максимум на сейчас» в письме = по плановым сводкам, половина вверх, и он же записан",
    "A время, LightGBM, цены, модели": "местное время, строка LightGBM, цены вариантов и состав моделей прогноза в письме",
    "A память и уверенность": "память, систематические ошибки и таблица уверенности в письме = пересчёт на момент письма",
    "B шансы LLM": "шансы каждого LLM-кошелька пересчитываются из его ответа / из шансов родителя того же часа",
    "C кошельки моделей": "выбор кошельков моделей повторяется по тому же снимку (вариант, шанс, цена, причина пропуска)",
    "D дневная модель": "ставки ml_day*: шанс = смесь того часа, перевес, правило «рынок уверен»",
}


def _section(text, head):
    ls = text.split("\n")
    if head not in ls:
        return None
    i, out = ls.index(head) + 1, []
    while i < len(ls) and not any(ls[i].startswith(p) for p in HEADS):
        out.append(ls[i])
        i += 1
    return "\n".join(out)


def _mk(r):
    return [{"label": k, "lo": L.label_range(k)[0], "hi": L.label_range(k)[1], "price": v} for k, v in json.loads(r["market_json"]).items()]


class AsOf:
    """База «на момент письма»: weather_station_daily без итогов дней, которые к ts ещё не записал weather_station_obs."""

    def __init__(self, c):
        self.c = c
        self.runs = [datetime.fromisoformat(r[0]) for r in c.execute(
            "SELECT finished_at FROM job_log WHERE job = 'weather_station_obs' AND rc = 0 AND finished_at IS NOT NULL")]
        self.cur = None

    def at(self, ts, day):
        if self.cur == ts:
            return self.c
        c = self.c
        c.execute("DROP TABLE IF EXISTS temp.weather_station_daily")
        c.execute("CREATE TEMP TABLE weather_station_daily AS SELECT * FROM main.weather_station_daily WHERE local_date >= ?",
                  ((day - timedelta(days=200)).isoformat(),))
        for d in ((day - timedelta(days=k)) for k in range(1, 4)):
            for city, cfg in OBS_CITIES.items():
                end = datetime.combine(d + timedelta(days=1), datetime.min.time(), ZoneInfo(cfg["tz"]))
                if not any(end <= r <= ts for r in self.runs):
                    c.execute("DELETE FROM temp.weather_station_daily WHERE city = ? AND local_date = ?", (city, d.isoformat()))
        self.cur = ts
        return c

    def off(self):
        self.c.execute("DROP TABLE IF EXISTS temp.weather_station_daily")
        self.cur = None


def run(c, since):
    """→ {раздел: {"ok": n, "bad": [...], "warn": [...]}}."""
    c.row_factory = sqlite3.Row
    res = defaultdict(lambda: {"ok": 0, "bad": [], "warn": []})
    asof = AsOf(c)
    rows = [dict(r) for r in c.execute("SELECT * FROM llm_hour_preds WHERE local_date >= ? ORDER BY ts_utc", (since,))]
    metar = defaultdict(dict)
    for ic, t, fs, tc in c.execute("SELECT icao, obs_time_utc, first_seen_utc, temp_c FROM metar_seen WHERE obs_time_utc >= ?",
                                   ((date.fromisoformat(since) - timedelta(days=1)).isoformat(),)):
        metar[ic][t[:16]] = (tc, fs)
    models = defaultdict(set)
    for city, d, m in c.execute("SELECT city, local_date, model FROM mm_forecasts WHERE lead = 'live' AND local_date >= ?", (since,)):
        models[(city, d)].add(m)

    def calc(fn, ts, day):
        """Пересчёт по базе сейчас; не сошлось — по базе на момент письма."""
        now_v = fn(c)
        return now_v, (lambda: fn(asof.at(ts, day)))

    for r in rows:
        w, city, d, h = r["wallet"], r["city"], r["local_date"], r["local_hour"]
        tag = f"{w} {city} {d} {h:02d} ч"
        cfg = OBS_CITIES[city]
        tz = ZoneInfo(cfg["tz"])
        day = date.fromisoformat(d)
        ts = datetime.fromisoformat(r["ts_utc"])
        text = r["prompt"] or ""
        asof.off()
        if text:
            sec = res["A время, LightGBM, цены, модели"]
            m = re.search(r"Local time now: (\d{4}-\d\d-\d\d \d\d:\d\d)", text)
            if m and w != "llm_skill":   # у скилла цены и время записи — в момент ответа, письмо — раньше
                lt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=tz)
                if abs((lt - ts).total_seconds()) > 120:
                    sec["bad"].append(f"{tag}: в письме {m.group(1)}, запуск в {ts.astimezone(tz):%H:%M}")
                else:
                    sec["ok"] += 1
            # сводки
            sec = res["A сводки METAR"]
            unit_f = cfg["unit"] == "fahrenheit"
            conv = lambda tc: round(tc * 9 / 5 + 32 if unit_f else tc, 1)
            seen, routine, arch = set(), [], metar[cfg["icao"]]
            for x in text.split("\n"):
                g = RE_OBS.match(x)
                if not g:
                    continue
                v = float(g.group(3))
                if not g.group(5):
                    routine.append(v)
                lt = datetime.combine(day, datetime.min.time(), tz).replace(hour=int(g.group(1)), minute=int(g.group(2)))
                keys = sorted({lt.replace(fold=f).astimezone(timezone.utc).isoformat()[:16] for f in (0, 1)})
                key = next((k for k in keys if k in arch and k not in seen and abs(conv(arch[k][0]) - v) <= 0.051),
                           next((k for k in keys if k in arch), None))
                if key is None:
                    sec["warn"].append(f"{tag}: сводки {g.group(1)} нет в нашем архиве (metar_seen)")
                    continue
                seen.add(key)
                if abs(conv(arch[key][0]) - v) > 0.051:
                    sec["bad"].append(f"{tag}: сводка {g.group(1)} в письме {v}, в архиве {conv(arch[key][0])}")
                else:
                    sec["ok"] += 1
            if w != "llm_skill":
                for key, (tc, fs) in arch.items():
                    ot = datetime.fromisoformat(key + ":00+00:00")
                    if ot.astimezone(tz).date() == day and datetime.fromisoformat(fs) <= ts - timedelta(minutes=10) and key not in seen:
                        sec["bad"].append(f"{tag}: сводки {ot.astimezone(tz):%H:%M} нет в письме (пришла в "
                                          f"{datetime.fromisoformat(fs).astimezone(tz):%H:%M})")
            # максимум на сейчас
            sec = res["A максимум на сейчас"]
            m = re.search(r"Max observed so far today \(routine METAR only[^)]*\): (\S+)", text)
            if m:
                said = None if m.group(1) == "none" else float(m.group(1))
                calc_mx = max((poly_round(v) for v in routine), default=None)
                if said != calc_mx:
                    sec["bad"].append(f"{tag}: в письме {said}, по плановым сводкам письма {calc_mx}")
                elif w != "llm_skill" and r["max_so_far"] != said and not (r["max_so_far"] is None and said is None):
                    sec["bad"].append(f"{tag}: в письме {said}, записан {r['max_so_far']}")
                else:
                    sec["ok"] += 1
            # модели, LightGBM, цены
            sec = res["A время, LightGBM, цены, модели"]
            m = re.search(r"Daily max forecasts by weather model \([^)]*\): (.*)", text)
            if m and m.group(1) != "none":
                miss = [p.rsplit(" ", 1)[0] for p in m.group(1).split(", ") if p.rsplit(" ", 1)[0] not in models[(city, d)]]
                if miss:
                    sec["warn"].append(f"{tag}: моделей {miss} нет в mm_forecasts")
            lg = next((x for x in text.split("\n") if x.startswith("LightGBM v3 forecast")), None)
            if lg is not None:
                if lg != L.lgbm_forecast(c, city, day):
                    sec["bad"].append(f"{tag}: строка LightGBM не как утренний снимок")
                else:
                    sec["ok"] += 1
            pl = _section(text, "Market buckets and current price of YES (= market probability):")
            if pl is not None and w != "llm_skill":
                want = "\n".join(f"{lab}: {p * 100:.1f}¢" for lab, p in json.loads(r["market_json"] or "{}").items())
                if pl.strip() != want.strip():
                    sec["bad"].append(f"{tag}: цены в письме не как записанные")
                else:
                    sec["ok"] += 1
            # память, ошибки, уверенность
            sec = res["A память и уверенность"]
            mw = {L.GEM_AGY_WALLET: L.MIX_FROM, L.AGY_P_WALLET: L.AGY_WALLET, "llm_skill": L.AGY_NOMEM_WALLET}.get(w, w)
            checks = []
            mem = _section(text, "Your past daily-max forecasts for this city and the actual daily max:")
            if mem is not None:
                checks.append(("память", lambda cc: mem.strip() == L.memory(cc, mw, city, day).strip()))
            head = "YOUR SYSTEMATIC ERRORS (computed by code over your whole history):\n"
            if head in text:
                tail = text[text.index(head) + len(head):]
                checks.append(("систематические ошибки", lambda cc: tail.startswith(L.bias_stats(cc, mw, city, day))))
            if "HOW OFTEN YOUR CHANCES CAME TRUE" in text:
                checks.append(("таблица уверенности", lambda cc: L.calibration_text(L.calibration(cc, mw, day)) in text))
            for name, fn in checks:
                if fn(c) or fn(asof.at(ts, day)):
                    sec["ok"] += 1
                else:
                    sec["bad"].append(f"{tag}: {name} не как пересчёт (и на момент письма)")
                asof.off()
        # B шансы
        if not r["probs_json"] or w in ("llm_agy_bet", "llm_ds"):
            continue
        sec = res["B шансы LLM"]
        got, mk, mx = json.loads(r["probs_json"]), _mk(r), r["max_so_far"]
        try:
            ans = json.loads(r["answer_json"]) if (r["answer_json"] or "").lstrip().startswith("{") else None
        except ValueError:
            ans = None
        par = lambda pw: next((x for x in rows if x["wallet"] == pw and x["city"] == city and x["local_date"] == d
                               and x["local_hour"] == h and x["probs_json"]), None)

        def want(cc):
            if w in ("llm_gem", L.GEM_AGY_WALLET):
                return L.calibrate(L.probs_for(ans, mk, mx), L.calibration(cc, L.MIX_FROM, day), mk, mx) if ans else None
            if w in (L.AGY_WALLET, L.AGY_NOMEM_WALLET):
                return L.agy_probs(r["pred_max"], L.agy_sigma(cc, city, day, h)[0], mk, mx)
            if w in (L.AGY_P_WALLET, "llm_skill"):
                return L.probs_for(ans, mk, mx) if ans else None
            if w in (L.MIX_WALLET, L.MIX5_WALLET, L.CAL_WALLET, L.AGY_CAL_WALLET):
                p = par(L.AGY_WALLET if w == L.AGY_CAL_WALLET else L.MIX_FROM)
                if p is None:
                    return None
                pp = json.loads(p["probs_json"])
                if w == L.MIX_WALLET:
                    return L.mix_probs(cc, city, day, mk, mx, pp)
                if w == L.MIX5_WALLET:
                    return L.mix_probs(cc, city, day, mk, mx, pp, "ml5_model_p")
                return L.market_blend(pp, mk, mx)
            return "skip"

        same = lambda v: v not in (None, "skip") and all(abs(v.get(k, 0) - x) < 1e-6 for k, x in got.items())
        v = want(c)
        if v == "skip":
            continue
        if same(v) or same(want(asof.at(ts, day))):
            sec["ok"] += 1
        elif v is None:
            sec["warn"].append(f"{tag}: не из чего пересчитать (ответ не разобрать или нет родителя)")
        else:
            sec["bad"].append(f"{tag}: шансы не пересчитываются (и на момент письма)")
        asof.off()

    # C кошельки моделей
    sec = res["C кошельки моделей"]
    fields = {**wp.WALLETS, **wp.NO_WALLETS}
    skip_w = set(wp.SHIFT_WALLETS) | set(wp.BIAS_REQUIRED)    # все выключены
    snaps = {}
    for t in c.execute("SELECT * FROM paper_trades WHERE local_date >= ? ORDER BY id", (since,)):
        t = dict(t)
        w = t["wallet"]
        if w not in fields or w in wp.RETIRED or w in skip_w:
            continue
        k = (t["snapshot_ts"], t["city"], t["local_date"])
        if k not in snaps:
            snaps[k] = [dict(x) for x in c.execute("SELECT * FROM snapshots_fast WHERE ts_utc = ? AND city = ? AND local_date = ?", k)] or \
                       [dict(x) for x in c.execute("SELECT * FROM snapshots WHERE ts_utc = ? AND city = ? AND local_date = ?", k)]
        tag = f"{w} {t['city']} {t['local_date']}"
        if not snaps[k]:
            sec["bad"].append(f"{tag}: снимка {t['snapshot_ts']} нет")
            continue
        kind, x = _replay(w, fields[w], snaps[k])
        if w in wp.CITY_PICK and t["city"] not in wp.city_pick(c, w, t["local_date"])[0]:
            kind, x = "skip", "город сейчас не в лучшей половине"
            if t["local_date"] < wp.CITY_PICK_SAT_FROM and t["status"] != "skip":
                # 03.11 разобрано: до недели 09.11 список недели досчитывался в понедельник 07:50 (воскресенье в ml_skill),
                # ранние города решались без него — ставка верна по данным на момент решения
                sec["ok"] += 1
                continue
        if t["status"] == "skip":
            why = t["reason"] or ""
            if w in wp.CITY_PICK and t["local_date"] < wp.CITY_PICK_SAT_FROM and why.startswith("город сейчас не в лучшей половине"):
                sec["ok"] += 1     # 03.11 разобрано — обратный случай (Циндао 02.11), см. выше
                continue
            if kind == "bet":
                sec["bad"].append(f"{tag}: записан пропуск («{why[:60]}»), а по снимку — ставка")
            elif x not in why:
                sec["warn"].append(f"{tag}: пропуск по другой причине: «{why[:50]}», повтор — «{x}»")
            else:
                sec["ok"] += 1
        elif kind != "bet":
            if t["status"] != "nofill":
                sec["bad"].append(f"{tag}: записана ставка ({t['status']}), а по снимку — пропуск «{x}»")
        elif abs(x["bucket_lo"] - t["bucket_lo"]) > 1e-6 or abs(x["bucket_hi"] - t["bucket_hi"]) > 1e-6:
            sec["bad"].append(f"{tag}: вариант {t['bucket_lo']}-{t['bucket_hi']}, по снимку {x['bucket_lo']}-{x['bucket_hi']}")
        elif abs(x[fields[w]] - t["model_p"]) > 1e-9 or abs(x["market_p"] - t["market_p"]) > 1e-9:
            sec["bad"].append(f"{tag}: шанс/цена {t['model_p']:.4f}/{t['market_p']:.4f}, в снимке {x[fields[w]]:.4f}/{x['market_p']:.4f}")
        else:
            sec["ok"] += 1

    # D дневная модель
    sec = res["D дневная модель"]
    dp = defaultdict(list)
    for r in c.execute("SELECT * FROM ml_day_preds WHERE local_date >= ?", (since,)):
        dp[(r["city"], r["local_date"])].append(dict(r))
    for t in c.execute("SELECT * FROM paper_trades WHERE wallet LIKE 'ml_day%' AND local_date >= ? AND stake > 0", (since,)):
        t = dict(t)
        w, tag = t["wallet"], f"{t['wallet']} {t['city']} {t['local_date']}"
        cand = [r for r in dp[(t["city"], t["local_date"])] if r["ts_utc"] <= t["placed_at"]]
        if not cand:
            sec["bad"].append(f"{tag}: нет решения модели до ставки")
            continue
        r = max(cand, key=lambda r: r["ts_utc"])
        blend, mkt = json.loads(r["blend_json"]), json.loads(r["market_json"])
        lab = next((k for k in blend if L.label_range(k) and abs(L.label_range(k)[0] - t["bucket_lo"]) < 1e-6
                    and abs(L.label_range(k)[1] - t["bucket_hi"]) < 1e-6), None)
        q = None if lab is None else (blend[lab] if (t["side"] or "yes") == "yes" else 1 - blend[lab])
        if lab is None:
            sec["bad"].append(f"{tag}: варианта ставки нет в решении")
        elif w != md.WALLET_NOHIGH and abs(q - t["model_p"]) > 1e-6:
            sec["bad"].append(f"{tag}: шанс ставки {t['model_p']:.4f}, смесь {q:.4f}")
        elif w not in (md.WALLET_FAV, md.WALLET_NOHIGH) and t["model_p"] - t["market_p"] < md.EDGE - 1e-9:
            sec["bad"].append(f"{tag}: перевес {100 * (t['model_p'] - t['market_p']):.1f} п.п. меньше {100 * md.EDGE:.0f}")
        elif w == md.WALLET_CONF and max(mkt.values()) / (sum(mkt.values()) or 1) >= md.CONF_SKIP:
            sec["bad"].append(f"{tag}: фаворит ≥ {100 * md.CONF_SKIP:.0f}% — ставки быть не должно")
        else:
            sec["ok"] += 1
    asof.off()
    return dict(res)


def _replay(wallet, field, buckets):
    """Выбор weather_paper.place по снимку — без стакана, денег и размера ставки → ("bet", вариант) / ("skip", начало причины)."""
    no_side = wallet in wp.NO_WALLETS
    buckets = [b for b in buckets if b.get(field) is not None]
    if no_side:
        buckets = [dict(b, **{field: 1 - b[field], "market_p": 1 - b["market_p"]}) for b in buckets]
        if wallet in wp.NO_BAND and buckets:
            lo_b, hi_b = wp.NO_BAND[wallet]
            buckets = [b for b in buckets if lo_b <= 1 - b["market_p"] < hi_b and b[field] > b["market_p"]]
            if not buckets:
                return "skip", "нет вариантов за"
    if wallet in wp.TOP_RANK and buckets:
        buckets = sorted(buckets, key=lambda r: -(r["market_p"] or 0))[:wp.TOP_RANK[wallet]]
    if wallet in wp.YES_BAND and buckets:
        lo_b, hi_b = wp.YES_BAND[wallet]
        buckets = [b for b in buckets if lo_b <= b["market_p"] < hi_b and b[field] > b["market_p"]]
        if not buckets:
            return "skip", "нет вариантов за"
    if wallet in wp.YES_FLOOR and buckets:
        buckets = [b for b in buckets if wp.YES_FLOOR[wallet] <= (b["market_p"] or 0) <= wp.YES_CEIL.get(wallet, 1.0)]
        if not buckets:
            return "skip", "нет вариантов не дешевле"
    if wallet in wp.CONF_SKIP and buckets:
        tot = sum(b["market_p"] or 0 for b in buckets) or 1.0
        if max((b["market_p"] or 0) / tot for b in buckets) >= wp.CONF_SKIP[wallet]:
            return "skip", "рынок уверен"
    if not buckets:
        return "skip", "в утреннем снимке"
    if wallet in wp.PICK_ASK:
        ab = [r for r in buckets if r["best_ask"] is not None and wp.YES_MIN <= r["best_ask"] <= wp.MAX_PRICE]
        if not ab:
            return "skip", "в снимке нет цены продажи"
        b = max(ab, key=lambda r: r[field] - r["best_ask"] - wp.ask_fee(r["best_ask"]))
    else:
        b = max(buckets, key=(lambda r: r[field] / max(r["market_p"], 1e-6)) if wallet in wp.PICK_RATIO else (lambda r: r[field] - r["market_p"]))
    edge = b[field] - (b["best_ask"] + wp.ask_fee(b["best_ask"]) if wallet in wp.PICK_ASK else b["market_p"])
    if edge < wp.EDGE_BY_WALLET.get(wallet, wp.MIN_EDGE):
        return "skip", "нет перевеса"
    min_price = wp.MIN_PRICE_BY_WALLET.get(wallet, wp.MIN_PRICE)
    if not no_side and wallet not in wp.YES_BAND:
        min_price = max(min_price, wp.YES_MIN)
    if wallet not in wp.PICK_ASK and not (min_price <= b["market_p"] <= wp.MAX_PRICE):
        return "skip", "лучший вариант"
    return "bet", b


def main():
    since = sys.argv[1] if len(sys.argv) > 1 else (date.today() - timedelta(days=1)).isoformat()
    c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    res = run(c, since)
    for name in SECTIONS:
        s = res.get(name, {"ok": 0, "bad": [], "warn": []})
        print(f"[{name}] верно {s['ok']}, ошибок {len(s['bad'])}, внимание {len(s['warn'])}")
        for x in s["bad"][:20] + s["warn"][:10]:
            print("   ", x)
    c.close()


if __name__ == "__main__":
    main()
