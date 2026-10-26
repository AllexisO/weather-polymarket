#!/usr/bin/env python3
"""
24.10 проверка инструмента Gemma «проверь условие» (Alex: «мы должны убедиться, что питоновский скрипт правильно работает»).
Всё пересчитывается здесь заново, своим кодом, прямо из сырых данных прогона, и сравнивается с diary_rules:
1. числа дня (features) — медиана прогнозов, замер 08:00, роса, небо, рост с полуночи, вчерашний максимум, diff;
2. подсчёт условия (measure) — 3000 случайных условий: сколько дней, среднее там и в остальных, принять / отклонить;
3. разбор условий — правильные, кривые, чужие столбцы;
4. ответ инструмента = решение книги: что инструмент «ACCEPT», то apply_changes добавляет (и с тем же сдвигом), остальное — нет;
5. утренний прогноз не видит сегодняшний итог.
Запуск: python3 app/diary_rules_test.py [запуск, по умолчанию atlanta_rules]   (выход 1 — есть расхождения)
"""
import contextlib
import io
import json
import random
import shutil
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
import diary_lab as dl
import diary_rules as R

RUN = sys.argv[1] if len(sys.argv) > 1 else "atlanta_rules"
bad = []


def same(a, b, what):
    if a is None or b is None:
        ok = a is None and b is None
    else:
        ok = abs(a - b) < 1e-9
    if not ok:
        bad.append(f"{what}: программа {a}, пересчёт {b}")


def f_(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------- 1. числа дня — заново из сырых данных ----------

def mine(meta, day):
    tz = ZoneInfo(meta["tz"])
    fc = sorted(v for v in day["forecasts"].values() if v is not None)
    n = len(fc)
    med = (fc[n // 2] if n % 2 else (fc[n // 2 - 1] + fc[n // 2]) / 2) if n else None
    local = lambda t: datetime.fromtimestamp(t, tz)
    start = day["start"]
    d0 = local(start).date()
    obs = [o for o in day["obs"] if f_(o.get("tmpf")) is not None]
    morning = [o for o in obs if o["t"] >= start and local(o["t"]).date() == d0 and
               (local(o["t"]).hour, local(o["t"]).minute, local(o["t"]).second) <= (8, 0, 0)]
    yday = [f_(o["tmpf"]) for o in obs if start - 86400 <= o["t"] < start]
    x = {"med": med, "spread": fc[-1] - fc[0] if fc else None}
    fv = day["fcst_vars"] or {}
    x["dew"] = fv["dew_point_2m"] * 9 / 5 + 32 if "dew_point_2m" in fv else None
    x["wind"] = fv["wind_speed_10m"] / 1.852 if "wind_speed_10m" in fv else None
    for c, k in (("cloud", "cloud_cover"), ("rh", "relative_humidity_2m"), ("solar", "shortwave_radiation"), ("precip", "precipitation")):
        x[c] = fv.get(k)
    e = day.get("recent_err") or []
    x["err7"] = sum(e) / len(e) if e else None
    x["prev_max"] = max(yday) if yday else None
    x["prev_vs_med"] = x["prev_max"] - med if yday and med is not None else None
    last = max(morning, key=lambda o: o["t"]) if morning else None
    x["t08"] = f_(last["tmpf"]) if last else None
    x["dew08"] = f_(last.get("dwpf")) if last else None
    if last:
        cov = {"CLR": 0, "SKC": 0, "NSC": 0, "FEW": 1, "SCT": 2, "BKN": 3, "OVC": 4, "VV": 4}
        s = [cov[last.get(k)] for k in ("skyc1", "skyc2", "skyc3") if last.get(k) in cov]
        x["sky08"] = max(s) if s else None
        x["rise08"] = f_(last["tmpf"]) - min(f_(o["tmpf"]) for o in morning)
    else:
        x["sky08"] = x["rise08"] = None
    if day.get("actual_max") is not None and med is not None:
        x["diff"] = day["actual_max"] - med
    return x


rows = []
for p in sorted((dl.OUT / RUN).glob("data_20??-??.json")):
    meta = json.loads(p.read_text())
    for d in meta["days"]:
        a, b = R.features(meta, d), mine(meta, d)
        for c in R.COL_NAMES + ["diff"]:
            same(a.get(c), b.get(c), f"{d['date']} {c}")
        rows.append(dict(a, date=d["date"]))
print(f"1. числа дня: {len(rows)} дней × {len(R.COL_NAMES) + 1} чисел — расхождений {len(bad)}")
n1 = len(bad)

# ---------- 2. подсчёт условия — своим кодом ----------

def mine_measure(cond, rows):
    ok = lambda r: all(r.get(c) is not None and (r[c] >= v if op == ">=" else r[c] <= v if op == "<=" else
                                                   r[c] > v if op == ">" else r[c] < v) for c, op, v in cond)
    done = [r for r in rows if r.get("diff") is not None]
    yes = [r["diff"] for r in done if ok(r)]
    no = [r["diff"] for r in done if not ok(r)]
    mean = sum(yes) / len(yes) if yes else None
    other = sum(no) / len(no) if no else None
    accept = len(yes) >= 5 and len(no) >= 5 and len(yes) <= 0.85 * len(done) and abs(mean - other) >= 0.5
    return len(yes), mean, other, accept


random.seed(24)
acc = 0
for k in range(3000):
    sub = rows[:random.randint(1, len(rows))]
    cond = []
    for _ in range(random.choice((1, 1, 2, 3))):
        c = random.choice(R.COL_NAMES)
        vals = [r[c] for r in sub if r.get(c) is not None] or [0]
        cond.append((c, random.choice((">=", "<=", ">", "<")), round(random.choice(vals), 1)))
    m, (n, mean, other, accept) = R.measure(cond, sub), mine_measure(cond, sub)
    same(m["n"], n, f"#{k} {R.cond_text(cond)} n")
    same(m["mean"], mean, f"#{k} mean")
    same(m["other"], other, f"#{k} other")
    if (m["why"] is None) != accept:
        bad.append(f"#{k} {R.cond_text(cond)}: программа {'принимает' if m['why'] is None else 'отклоняет'}, пересчёт — нет")
    acc += accept
print(f"2. подсчёт условий: 3000 случайных (из них приняты бы {acc}) — расхождений {len(bad) - n1}")
n2 = len(bad)

# ---------- 3. разбор условий ----------

cases = {"t08 >= 78": [("t08", ">=", 78.0)], "IF dew08 >= 70 AND cloud <= 30": [("dew08", ">=", 70.0), ("cloud", "<=", 30.0)],
         "if t08>=78°F and rh < 60% THEN +2°F": [("t08", ">=", 78.0), ("rh", "<", 60.0)], "err7 > -1.5": [("err7", ">", -1.5)],
         "IF sky08 <= 2 AND rise08 <= 1 THEN": [("sky08", "<=", 2.0), ("rise08", "<=", 1.0)],
         "temp >= 80": str, "t08 = 78": str, "": str, "t08 >= high": str}
for t, want in cases.items():
    got = R.parse_cond(t)
    if (want is str and not isinstance(got, str)) or (want is not str and got != want):
        bad.append(f"разбор «{t}»: {got}")
for t in ("temp >= 80", "t08 = 78"):
    if "cannot" not in R.test_condition(t, rows):
        bad.append(f"инструмент на кривое «{t}»: {R.test_condition(t, rows)}")
print(f"3. разбор: {len(cases)} случаев — расхождений {len(bad) - n2}")
n3 = len(bad)

# ---------- 4. инструмент = решение книги ----------

tmp = "_rules_selftest"
d = dl.OUT / tmp
shutil.rmtree(d, ignore_errors=True)
d.mkdir()
try:
    dl.diary_path(tmp).write_text("")
    tried = 0
    for k in range(200):
        c = random.choice(R.COL_NAMES)
        vals = sorted(r[c] for r in rows if r.get(c) is not None)
        t = f"{c} {random.choice(('>=', '<='))} {vals[len(vals) * random.randint(2, 8) // 10]:g}"
        res = R.test_condition(t, rows)
        dl.diary_path(tmp).write_text("")
        with contextlib.redirect_stdout(io.StringIO()):
            R.apply_changes(tmp, {"add": [t]}, rows, "selftest")
        book = R.book_load(tmp)
        if ("ACCEPT" in res) != bool(book):
            bad.append(f"«{t}»: инструмент {res!r}, книга {book}")
        elif book and f"shift {book[0]['shift']:+.1f}" not in res:
            bad.append(f"«{t}»: сдвиг в книге {book[0]['shift']}, инструмент {res!r}")
        tried += 1
finally:
    shutil.rmtree(d, ignore_errors=True)
print(f"4. инструмент = книга: {tried} условий — расхождений {len(bad) - n3}")
n4 = len(bad)

# ---------- 5. утро не видит итог ----------

meta = json.loads(sorted((dl.OUT / RUN).glob("data_20??-??.json"))[-1].read_text())
day = meta["days"][10]
x = {k: v for k, v in R.features(meta, day).items() if k not in ("actual", "diff")}
txt = R.today_text(x) + R.test_condition("t08 >= 0", rows[:10], x)
if "actual" in txt or "diff" in R.today_text(x):
    bad.append("в утренних числах есть итог дня")
print(f"5. утро без итога — расхождений {len(bad) - n4}")

n5 = len(bad)

# ---------- 6. проверка готового ответа (25.10, «вариант А») ----------

d.mkdir()
try:
    dl.diary_path(tmp).write_text("")
    rv = R.review_for(tmp, rows)
    ok_c = next(t for t in (f"{c} >= {v:g}" for c in R.COL_NAMES for v in sorted({r[c] for r in rows if r.get(c) is not None}))
                if "ACCEPT" in R.test_condition(t, rows))
    no_c = "t08 >= 999"
    if rv({"add": [ok_c]}) is not None:
        bad.append(f"проверка ответа: всё принимается ({ok_c}), а Gemma всё равно переспрашивают")
    fb = rv({"add": [ok_c, no_c], "edit": [{"id": 1, "rule": "temp >= 1"}]}) or ""
    if not ("REJECT" in fb and ok_c in fb and "temp >= 1" in fb and fb.count("\n- ") == 3):
        bad.append(f"проверка ответа: неполный итог {fb!r}")
    with contextlib.redirect_stdout(io.StringIO()):
        R.apply_changes(tmp, {"add": [ok_c]}, rows, "selftest")
    if "already in the book" not in (rv({"add": [ok_c]}) or ""):
        bad.append("проверка ответа: правило уже в книге, а итог не говорит")
finally:
    shutil.rmtree(d, ignore_errors=True)
print(f"6. проверка ответа перед записью — расхождений {len(bad) - n5}")

n6 = len(bad)

# ---------- 7. разбор дня объясняет по числам (25.10) ----------

d.mkdir()
try:
    import re as _re
    for x in rows[10:40]:
        dl.diary_path(tmp).write_text("R1. IF sky08 >= 3 THEN +3.0°F [helped 0, misled 0]\nR2. IF t08 <= 75 THEN -1.0°F [helped 0, misled 0]\n")
        txt = R.score_day(tmp, x, "selftest")
        for rid, cond, sh in ((1, [("sky08", ">=", 3.0)], 3.0), (2, [("t08", "<=", 75.0)], -1.0)):
            if R.holds(cond, x):
                want = "HELPED" if abs(x["diff"] - sh) < abs(x["diff"]) else "MISLED"
                c = cond[0][0]
                if not _re.search(rf"R{rid} held \({c} = {_re.escape(R._cell(x[c], c))};.*?→ {want}", txt):
                    bad.append(f"{x['date']} R{rid}: ждали {want}, разбор: {txt}")
            elif f"R{rid}" not in txt.split("did not hold:")[-1]:
                bad.append(f"{x['date']} R{rid} не выполнялось, а в разборе нет: {txt}")
        b = {r["id"]: r for r in R.book_load(tmp)}
        for rid, cond, sh in ((1, [("sky08", ">=", 3.0)], 3.0), (2, [("t08", "<=", 75.0)], -1.0)):
            want = (1, 0) if R.holds(cond, x) and abs(x["diff"] - sh) < abs(x["diff"]) else (0, 1) if R.holds(cond, x) else (0, 0)
            if (b[rid]["h"], b[rid]["m"]) != want:
                bad.append(f"{x['date']} R{rid}: счёт {b[rid]['h']}/{b[rid]['m']}, ждали {want}")
finally:
    shutil.rmtree(d, ignore_errors=True)
print(f"7. разбор дня по числам: 30 дней × 2 правила — расхождений {len(bad) - n6}")
print("   пример:", R.score_day.__name__, txt[:300])

n7 = len(bad)

# ---------- 8. опора программы и счёт Gemma (25.10) — своим кодом, numpy ----------

import numpy as np


def my_raw(done, x):
    sh = []
    for c in R.COL_NAMES:
        v = [r[c] for r in done if r.get(c) is not None]
        if x.get(c) is None or len(v) < 10:
            continue
        for q in (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
            th = float(np.quantile(v, q))
            for ge in (True, False):
                if not (x[c] >= th if ge else x[c] <= th):
                    continue
                ok = lambda r: r.get(c) is not None and (r[c] >= th if ge else r[c] <= th)
                (n, mean, other, accept) = mine_measure([(c, ">=" if ge else "<=", th)], done)
                if accept:
                    sh.append(mean)
    return x["med"] + (sum(sh) / len(sh) if sh else 0.0) if len(done) >= 10 else x["med"]


days = rows[:]
raws, finals = [], []
for i in range(len(days)):
    x = {k: v for k, v in days[i].items() if k not in ("actual", "diff")}
    raw = my_raw(days[:i], x)
    errs = [raws[j] - days[j]["actual"] for j in range(max(10, i - 7), i)] if i >= 10 else []
    fin = raw - 0.5 * (sum(errs) / len(errs) if errs else 0.0)
    raws.append(raw)
    finals.append(fin)
    same(R.anchor(days[:i], x)[0], fin, f"опора {days[i]['date']}")
print(f"8. опора программы: {len(days)} дней — расхождений {len(bad) - n7}")
n8 = len(bad)
rec = [{"expected": "95.0", "anchor": 93.2, "actual": 93}, {"expected": None, "anchor": 90, "actual": 91},
       {"expected": 90, "anchor": 91.0, "actual": 90}, {"expected": 88, "anchor": 88.0, "actual": 87}]
t = R.record_text(rec)
for want in ("your last 3 forecasts", "Your mean |error| 1.0°F", "mean (forecast − actual) +1.0°F",
             "program's mean |error| 0.7°F", "equal on 1", "closer than the program on 1 of 3 days"):
    if want not in t:
        bad.append(f"счёт Gemma: нет «{want}» в {t!r}")
print(f"   счёт Gemma — расхождений {len(bad) - n8}")

# ---------- 9. подсказка v5 вместо опоры (25.10, DIARY_HINT=v5) — своим кодом из файла честной v5 ----------

n9 = len(bad)
V5RUN = "atlanta_v5"
wf_p = dl.ROOT / "data" / "research" / "v5_wf_atlanta.json"
if (dl.OUT / V5RUN / "v5.json").exists() and wf_p.exists():
    wf = json.loads(wf_p.read_text())
    hint = json.loads((dl.OUT / V5RUN / "v5.json").read_text())
    vdays, vmeta = [], {}
    for p_ in sorted((dl.OUT / V5RUN).glob("data_20??-??.json")):
        m_ = json.loads(p_.read_text())
        for d_ in m_["days"]:
            vdays.append((dict(R.features(m_, d_), date=d_["date"]), d_))
    R.HINT, R.WHO = "v5", "model"
    for i, (x, d_) in enumerate(vdays):
        dd = x["date"]
        h = hint.get(dd)
        if not h:
            bad.append(f"v5: нет дня {dd}")
            continue
        if not h["trained_to"] < dd:   # честность: v5 этого дня учена только на днях до него
            bad.append(f"v5 {dd}: обучена до {h['trained_to']}")
        same(h["expected"], round(wf[dd]["qs_c"][6] * 1.8 + 32, 1), f"v5 {dd}: ожидаемый максимум")
        if abs(sum(h["probs"].values()) - 1) > 0.01 or set(h["probs"]) != set(d_["buckets"]):
            bad.append(f"v5 {dd}: шансы не по вариантам дня или сумма {sum(h['probs'].values())}")
        # прошлое: счёт v5 только по дням раньше dd; будущие строки таблицы не должны ничего менять
        e = [hint[r["date"]]["expected"] - r["actual"] for r, _ in vdays[:i]]
        a1, t1 = R.v5_text(V5RUN, [r for r, _ in vdays[:i]], dd)
        a2, t2 = R.v5_text(V5RUN, [r for r, _ in vdays], dd)
        same(a1, h["expected"], f"v5 {dd}: значение подсказки")
        if t1 != t2:
            bad.append(f"v5 {dd}: подсказка видит дни после {dd}")
        if e:
            want = (f"record on {len(e)} past days of the table: mean |error| {sum(abs(v) for v in e) / len(e):.1f}°F, "
                    f"mean (forecast − actual) {sum(e) / len(e):+.1f}°F; last {min(7, len(e))} days: mean (forecast − actual) "
                    f"{sum(e[-7:]) / len(e[-7:]):+.1f}°F")
            if want not in t1:
                bad.append(f"v5 {dd}: нет «{want}» в {t1!r}")
        line = R.v5_day_line(V5RUN, dict(x, win=d_["win"]))
        want = (f"forecast for this day was {h['expected']:.1f}°F (chance of the winning bucket {h['probs'][d_['win']]:.0%}), "
                f"actual {x['actual']:.0f}°F: error {h['expected'] - x['actual']:+.1f}°F")
        if want not in line:
            bad.append(f"v5 {dd}: учёба — нет «{want}» в {line!r}")
    # утренний запрос: подсказка v5 вместо опоры, слова «program» в счёте нет
    got = {}
    real_ask, dl.ask = dl.ask, lambda prompt, *a, **k: got.setdefault("p", prompt) and {}
    try:
        x, d_ = vdays[45]
        R.ask_forecast(V5RUN, json.loads(sorted((dl.OUT / V5RUN).glob("data_*.json"))[-1].read_text()), d_,
                       [r for r, _ in vdays[:45]], "test", "test", record=[{"expected": 90, "anchor": 91, "actual": 92}])
    finally:
        dl.ask = real_ask
    pr = got.get("p", "")
    for want in ("MODEL FORECAST (our LightGBM model v5", "start from the MODEL FORECAST", f"{hint[x['date']]['expected']:.1f}°F",
                 "your forecast / model / actual", "closer than the model"):
        if want not in pr:
            bad.append(f"v5: в утреннем запросе нет «{want}»")
    if "PROGRAM FORECAST" in pr:
        bad.append("v5: в утреннем запросе осталась опора программы")
    R.HINT, R.WHO = "anchor", "program"
    print(f"9. подсказка v5: {len(vdays)} дней — расхождений {len(bad) - n9}")
else:
    print("9. подсказка v5: нет файлов atlanta_v5 — пропущено")

print(f"\nпример ответа инструмента: {R.test_condition('t08 >= 78', rows[:31])}")
if bad:
    print("\nРАСХОЖДЕНИЯ:\n" + "\n".join(bad[:30]))
    sys.exit(1)
print("ИНСТРУМЕНТ ПРОВЕРЕН — всё сходится")
