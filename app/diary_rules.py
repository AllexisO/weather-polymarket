#!/usr/bin/env python3
"""
24.10 «дневник 2» (Alex: «дать Gemma инструмент, который посчитает за неё»; «всё должно потом работать на ставках —
завтра утром отработать и сделать ставку»). Запуск: DIARY_V2=1 DIARY_RECENT=7 ./diary_run.sh walk atlanta_rules 2026-08 2026-09,
живая ставка: DIARY_V2=1 DIARY_RECENT=7 python3 app/diary_lab.py predict atlanta_rules today [--dry].

Почему (разбор 24.10, RESEARCH_JOURNAL): уроки словами без чисел, проверить урок на других днях Gemma не могла — ложный урок
«высокая роса → выше прогнозов» записан 78 раз; на арифметику (медиана, построчная проверка) уходило 8-11 тыс. токенов.
Здесь делёж работы: Gemma придумывает условия правил и решает по ситуации дня; программа считает всё:
- числа дня — одни и те же столбцы в таблице прошлых дней, в «сегодня» и в живой ставке (features);
- правило — «IF <столбец> <знак> <число> [AND …]»; сдвиг THEN программа меряет сама по таблице: среднее
  (факт − медиана прогнозов) в днях, где условие выполнялось; правило берётся, только если таблица его подтверждает;
- «помогло / подвело» — по факту нового дня: сдвиг правила приблизил к факту или нет (сравнение с медианой прогнозов);
- на экзамене — какие правила выполнены сегодня и что они давали раньше.
Книга — diary.md (R1. IF … THEN +x [helped h, misled m]); все версии — history/.
"""
import json
import math
import re
import statistics
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import sys

# запуск через diary_lab.py: та же программа (__main__), а не вторая копия — иначе status.json для /diary теряет команду и pid
_m = sys.modules.get("__main__")
if getattr(_m, "__file__", "").endswith("diary_lab.py"):
    dl = _m
else:
    import diary_lab as dl
from weather_cities import OBS_CITIES

RULES_MAX = 30
ADD_MAX = 3                # новых правил за один разбор
MIN_DAYS = 5               # правило: условие выполнялось хотя бы в 5 днях таблицы …
MAX_SHARE = 0.85           # … но не почти во все дни (такое условие ничего не объясняет)
MIN_SEP = 0.5              # и в эти дни факт − медиана отличался от остальных дней хотя бы на 0.5°F
TABLE_ROWS = 60            # строк таблицы в запросе (подсчёт — по всем)
PROB_FLOOR = 0.02          # шанс вариантов рядом с главным (±2) не ниже
STAKE = 2.0
SKY = {"CLR": 0, "SKC": 0, "NSC": 0, "FEW": 1, "SCT": 2, "BKN": 3, "OVC": 4, "VV": 4}
COLS = [  # (имя, что это) — только то, что известно в 08:00 дня маркета
    ("med", "median of the 11 model forecasts of the daily max, °F"),
    ("spread", "max minus min of the model forecasts, °F"),
    ("cloud", "forecast cloud cover 11-17 h, %"),
    ("dew", "forecast dew point 11-17 h, °F"),
    ("rh", "forecast relative humidity 11-17 h, %"),
    ("solar", "forecast solar radiation 11-17 h, W/m²"),
    ("wind", "forecast wind 11-17 h, kt"),
    ("precip", "forecast precipitation 11-17 h, mm"),
    ("err7", "mean of (actual − median) over the 7 finished days before yesterday, °F"),
    ("prev_max", "yesterday's observed max, °F"),
    ("prev_vs_med", "yesterday's observed max minus today's forecast median, °F"),
    ("t08", "observed temperature at 08:00, °F"),
    ("dew08", "observed dew point at 08:00, °F"),
    ("sky08", "observed sky cover at 08:00: 0 clear, 1 few, 2 scattered, 3 broken, 4 overcast"),
    ("rise08", "08:00 temperature minus the lowest temperature since midnight, °F"),
]
COL_NAMES = [c for c, _ in COLS]
COND_RE = re.compile(r"^\s*([a-z0-9_]+)\s*(>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)\s*$")


# ---------- числа дня ----------

def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def features(meta, day):
    """Числа дня, известные в 08:00 (столбцы COLS) + итог, если день закончен (actual, diff)."""
    tz = meta["tz"]
    f = [v for v in day["forecasts"].values() if v is not None]
    med = statistics.median(f) if f else None
    fv = day["fcst_vars"] or {}
    t08 = dl.moment_ts(day, 0, 8, tz)
    start = day["start"]
    obs = [o for o in day["obs"] if _num(o.get("tmpf")) is not None]
    now = [o for o in obs if start <= o["t"] <= t08]
    prev = [_num(o["tmpf"]) for o in obs if start - 86400 <= o["t"] < start]
    o8 = now[-1] if now else None
    sky = None
    if o8:
        c = [SKY.get(o8.get(f"skyc{i}"), -1) for i in (1, 2, 3)]
        sky = max(c) if max(c) >= 0 else None
    re_ = day.get("recent_err") or []
    x = {"med": med, "spread": max(f) - min(f) if f else None,
         "cloud": fv.get("cloud_cover"), "dew": dl.c2f(fv["dew_point_2m"]) if "dew_point_2m" in fv else None,
         "rh": fv.get("relative_humidity_2m"), "solar": fv.get("shortwave_radiation"),
         "wind": fv["wind_speed_10m"] / 1.852 if "wind_speed_10m" in fv else None, "precip": fv.get("precipitation"),
         "err7": statistics.mean(re_) if re_ else None, "prev_max": max(prev) if prev else None,
         "t08": _num(o8["tmpf"]) if o8 else None, "dew08": _num(o8.get("dwpf")) if o8 else None, "sky08": sky,
         "rise08": (_num(o8["tmpf"]) - min(_num(o["tmpf"]) for o in now)) if o8 else None}
    x["prev_vs_med"] = x["prev_max"] - med if x["prev_max"] is not None and med is not None else None
    if day.get("actual_max") is not None and med is not None:
        x["actual"], x["diff"] = day["actual_max"], day["actual_max"] - med
    return x


def _cell(v, col):
    if v is None:
        return "-"
    if col in ("cloud", "rh", "solar", "sky08"):
        return f"{v:.0f}"
    if col in ("err7", "diff", "prev_vs_med"):
        return f"{v:+.1f}"
    return f"{v:.1f}" if col in ("med", "spread", "precip", "wind") else f"{v:.0f}"


def table_text(rows):
    head = ("PAST DAYS TABLE — computed by code, exact; one row per finished day, oldest first; # is only an order number.\n"
            "Columns: " + "; ".join(f"{c} = {w}" for c, w in COLS) + "; actual = observed max °F; diff = actual − med.\n"
            "# | " + " | ".join(COL_NAMES) + " | actual | diff")
    if not rows:
        return head + "\n(no finished days yet)"
    show = rows[-TABLE_ROWS:]
    k0 = len(rows) - len(show)
    return head + "\n" + "\n".join(f"{k0 + i} | " + " | ".join(_cell(r.get(c), c) for c in COL_NAMES) +
                                   f" | {_cell(r.get('actual'), 'actual')} | {_cell(r.get('diff'), 'diff')}"
                                   for i, r in enumerate(show, 1))


def today_text(x):
    return "TODAY'S NUMBERS (same columns, computed by code): " + ", ".join(f"{c} {_cell(x.get(c), c)}" for c in COL_NAMES)


def spread_text(rows):
    d = sorted(r["diff"] for r in rows if r.get("diff") is not None)
    if len(d) < 5:
        return "HOW FAR THE FACT LANDS FROM THE FORECAST MEDIAN: too few days yet."
    q = lambda p: d[min(len(d) - 1, int(p * len(d)))]
    ad = sorted(abs(v) for v in d)
    return (f"HOW FAR THE FACT LANDS FROM THE FORECAST MEDIAN ({len(d)} past days, computed by code): diff mean {statistics.mean(d):+.1f}°F, "
            f"middle half {q(0.25):+.1f}…{q(0.75):+.1f}, 80% of days {q(0.1):+.1f}…{q(0.9):+.1f}; |diff| ≤ 1°F on "
            f"{100 * sum(v <= 1 for v in ad) / len(ad):.0f}% of days, ≤ 2°F on {100 * sum(v <= 2 for v in ad) / len(ad):.0f}%.")


# ---------- правила ----------

RULE_RE = re.compile(r"^R(\d+)\. IF (.+?) THEN ([+-]?\d+(?:\.\d+)?)°?F? \[helped (\d+), misled (\d+)\]$")


def parse_cond(text):
    """'dew08 >= 70 AND cloud <= 30' → [(col, op, num)] или строка ошибки."""
    text = re.sub(r"^\s*IF\s+", "", str(text), flags=re.I)
    text = re.split(r"\s+THEN\b", text, flags=re.I)[0]      # 25.10: и «… THEN» без сдвига в конце
    out = []
    for part in re.split(r"\s+AND\s+", text.strip(), flags=re.I):
        m = COND_RE.match(part.strip().lower().replace("°f", "").replace("%", ""))
        if not m:
            return f"не разобрать «{part.strip()}» — нужно «столбец знак число», столбцы: {', '.join(COL_NAMES)}"
        if m.group(1) not in COL_NAMES:
            return f"нет столбца «{m.group(1)}» — есть: {', '.join(COL_NAMES)}"
        out.append((m.group(1), m.group(2), float(m.group(3))))
    return out or "пустое условие"


def cond_text(cond):
    return " AND ".join(f"{c} {op} {v:g}" for c, op, v in cond)


def holds(cond, x):
    for c, op, v in cond:
        a = x.get(c)
        if a is None:
            return False
        if not {">=": a >= v, "<=": a <= v, ">": a > v, "<": a < v}[op]:
            return False
    return True


def measure(cond, rows):
    """Подсчёт правила по таблице: в скольких днях выполнялось, среднее diff там и в остальных днях."""
    rows = [r for r in rows if r.get("diff") is not None]
    yes = [r["diff"] for r in rows if holds(cond, r)]
    no = [r["diff"] for r in rows if not holds(cond, r)]
    m = {"n": len(yes), "of": len(rows), "mean": statistics.mean(yes) if yes else None,
         "other": statistics.mean(no) if no else None, "lo": min(yes) if yes else None, "hi": max(yes) if yes else None}
    m["why"] = m["why_en"] = None
    if len(yes) < MIN_DAYS:
        m["why"] = f"выполнялось только в {len(yes)} днях (нужно ≥ {MIN_DAYS})"
        m["why_en"] = f"held on only {len(yes)} days (need ≥ {MIN_DAYS})"
    elif len(no) < MIN_DAYS:      # 24.10: сравнивать не с чем — «остальных дней» меньше 5
        m["why"] = f"не выполнялось только в {len(no)} днях — сравнить не с чем (нужно ≥ {MIN_DAYS})"
        m["why_en"] = f"did not hold on only {len(no)} days — nothing to compare with (need ≥ {MIN_DAYS})"
    elif len(yes) > MAX_SHARE * len(rows):
        m["why"] = f"выполнялось почти всегда ({len(yes)} из {len(rows)}) — ничего не объясняет"
        m["why_en"] = f"held on almost all days ({len(yes)} of {len(rows)}) — explains nothing"
    elif abs(m["mean"] - m["other"]) < MIN_SEP:
        m["why"] = f"в эти дни diff {m['mean']:+.1f}, в остальные {m['other']:+.1f} — разница меньше {MIN_SEP}°F"
        m["why_en"] = f"diff {m['mean']:+.1f} on these days vs {m['other']:+.1f} on the others — gap below {MIN_SEP}°F"
    return m


def stat_text(m, en=True):
    if not m["n"]:
        return f"held on 0 of {m['of']} days"
    return (f"held on {m['n']} of {m['of']} days: diff there {m['mean']:+.1f} (from {m['lo']:+.1f} to {m['hi']:+.1f}), "
            f"other days {m['other']:+.1f}" if m["other"] is not None else
            f"held on {m['n']} of {m['of']} days: diff there {m['mean']:+.1f}")


def book_load(run):
    out = []
    for ln in dl.read_diary(run).splitlines():
        m = RULE_RE.match(ln.strip())
        if m and isinstance(c := parse_cond(m.group(2)), list):
            out.append({"id": int(m.group(1)), "cond": c, "shift": float(m.group(3)), "h": int(m.group(4)), "m": int(m.group(5))})
    return out


def book_save(run, rules, step):
    rules = sorted(rules, key=lambda r: (r["m"] - r["h"], r["id"]))
    text = "\n".join(f"R{r['id']}. IF {cond_text(r['cond'])} THEN {r['shift']:+.1f}°F [helped {r['h']}, misled {r['m']}]"
                     for r in rules)
    dl.diary_path(run).write_text(text + ("\n" if text else ""))
    hist = dl.OUT / run / "history"
    hist.mkdir(exist_ok=True)
    (hist / f"{datetime.now():%m%d_%H%M%S}_{step}.md").write_text(text + "\n")


def book_text(rules, rows, today=None):
    if not rules:
        return "(empty)"
    out = []
    for r in sorted(rules, key=lambda r: (r["m"] - r["h"], r["id"])):
        m = measure(r["cond"], rows)
        line = (f"R{r['id']}. IF {cond_text(r['cond'])} THEN {r['shift']:+.1f}°F [helped {r['h']}, misled {r['m']}] — table: "
                + stat_text(m) + (" — WEAK now: " + m["why"] if m["why"] else ""))
        if today is not None:
            line += " — TODAY: " + ("condition HOLDS" if holds(r["cond"], today) else "does not hold")
        out.append(line)
    return "\n".join(out)


def score_day(run, x, step):
    """Новый законченный день: «помогло / подвело» у правил, условие которых в этот день выполнялось."""
    rules = book_load(run)
    if x.get("diff") is None or not rules:
        return ""
    h, m, why = [], [], []
    for r in rules:
        if holds(r["cond"], x):
            ok = abs(x["diff"] - r["shift"]) < abs(x["diff"])
            (h if ok else m).append(r["id"])
            r["h" if ok else "m"] += 1
            # 25.10: объяснение по числам — Gemma гадала по сводкам, выполнялось ли правило (разбор 14.08)
            vals = ", ".join(f"{c} = {_cell(x.get(c), c)}" for c in dict.fromkeys(c for c, _, _ in r["cond"]))
            why.append(f"R{r['id']} held ({vals}; IF {cond_text(r['cond'])}): shift {r['shift']:+.1f} → error "
                       f"{abs(x['diff'] - r['shift']):.1f} vs {abs(x['diff']):.1f} with no shift → {'HELPED' if ok else 'MISLED'}")
    if h or m:
        book_save(run, rules, step)
    off = [f"R{r['id']}" for r in rules if not holds(r["cond"], x)]
    return (f"Scoring of this day by code (actual − med = {x['diff']:+.1f}): " + ("; ".join(why) if why else "no rule held")
            + (f"; did not hold: {', '.join(off)}" if off else ""))


def apply_changes(run, r, rows, step):
    """Ответ Gemma (add / edit / delete) → книга. Условия проверяет программа; сдвиг — измеренный. → текст для следующего запроса."""
    rules = book_load(run)
    by = {x["id"]: x for x in rules}
    notes = []
    for i in {int(v) for v in re.findall(r"\d+", json.dumps(r.get("delete") or []))}:
        if i in by:
            rules.remove(by.pop(i))
            notes.append(f"R{i} deleted")
    nxt = max([x["id"] for x in rules] + [_max_id(run)]) + 1

    def check(text):
        c = parse_cond(text)
        if isinstance(c, str):
            return None, c
        m = measure(c, rows)
        if m["why"]:
            return None, m["why"]
        return c, m

    for e in r.get("edit") or []:
        if not isinstance(e, dict):
            continue
        i = next((int(v) for v in re.findall(r"\d+", str(e.get("id")))), None)
        if i not in by:
            continue
        c, m = check(e.get("rule", ""))
        if c is None:
            notes.append(f"edit R{i} rejected: {m}")
            continue
        by[i].update(cond=c, shift=round(m["mean"] * 2) / 2)
        notes.append(f"R{i} edited → IF {cond_text(c)} THEN {by[i]['shift']:+.1f}°F ({stat_text(m)})")
    added = 0
    for t in (r.get("add") or [])[:ADD_MAX]:
        c, m = check(t)
        if c is None:
            notes.append(f"new rule «{str(t)[:80]}» rejected: {m}")
            continue
        if any(cond_text(x["cond"]) == cond_text(c) for x in rules):
            notes.append(f"new rule «{cond_text(c)}» rejected: already in the book")
            continue
        if len(rules) >= RULES_MAX:
            notes.append(f"new rule «{cond_text(c)}» rejected: book is full ({RULES_MAX}), delete a weak rule first")
            continue
        rules.append({"id": nxt, "cond": c, "shift": round(m["mean"] * 2) / 2, "h": 0, "m": 0})
        notes.append(f"R{nxt} added: IF {cond_text(c)} THEN {rules[-1]['shift']:+.1f}°F ({stat_text(m)})")
        nxt += 1
        added += 1
    for x in list(rules):                 # сдвиг — всегда по свежей таблице; не подтверждается — правило уходит (история — history/)
        m = measure(x["cond"], rows)
        if m["why"]:
            rules.remove(x)
            notes.append(f"R{x['id']} removed by the program: {m['why']}")
        elif m["mean"] is not None:
            x["shift"] = round(m["mean"] * 2) / 2
    book_save(run, rules, step)
    _max_id(run, nxt - 1)
    dl.say(run, f"  книга: {len(rules)} правил; " + ("; ".join(notes) if notes else "без изменений"))
    return "; ".join(notes) or "no changes"


# ---------- инструмент Gemma «проверь условие» (Alex 24.10: «дать ей инструмент, которым она только пользуется») ----------

TOOL = {"type": "function", "function": {
    "name": "test_condition",
    "description": ("Exact check of a condition on the PAST DAYS TABLE, done by the program. Returns on which days it held, "
                    "the mean diff (actual − med) on those days and on the other days, and whether the program would ACCEPT it "
                    "as a rule (and with which shift) or REJECT it (and why). Also says if it holds TODAY when today is known."),
    "parameters": {"type": "object", "properties": {"condition": {
        "type": "string", "description": "columns, signs >= <= > <, numbers, joined with AND; e.g. 't08 >= 78 AND cloud <= 30'"}},
        "required": ["condition"]}}}


def test_condition(text, rows, today=None):
    """Ответ инструмента — те же parse_cond / measure, что решают, попадёт ли правило в книгу."""
    c = parse_cond(text)
    if isinstance(c, str):
        return "cannot read the condition: " + {"пустое условие": "empty condition"}.get(c, c.replace(
            "не разобрать", "cannot parse").replace("нужно «столбец знак число», столбцы:", "need 'column sign number', columns:")
            .replace("нет столбца", "no column").replace("есть:", "columns are:"))
    m = measure(c, rows)
    if m["of"] < 2 * MIN_DAYS:      # 25.10: так Gemma не тратит вызовы, пока таблица мала
        return (f"only {m['of']} finished days in the table — no condition can be accepted before {2 * MIN_DAYS} days "
                f"(need ≥ {MIN_DAYS} days where it held and ≥ {MIN_DAYS} where it did not); " + stat_text(m))
    k0 = len(rows) - len(rows[-TABLE_ROWS:])
    num = [str(k0 + i) for i, r in enumerate(rows[-TABLE_ROWS:], 1) if r.get("diff") is not None and holds(c, r)]
    out = f"{cond_text(c)}: " + stat_text(m) + (f" (rows #{', #'.join(num)})" if num else "")
    if m["n"] and m["other"] is not None:
        out += f"; gap {m['mean'] - m['other']:+.1f}°F"
    out += ("; program would REJECT: " + m["why_en"]) if m["why"] else f"; program would ACCEPT with shift {round(m['mean'] * 2) / 2:+.1f}°F"
    if today is not None:
        out += "; TODAY: " + ("holds" if holds(c, today) else "does not hold")
    return out


def review_for(run, rows):
    """25.10 (Alex, «вариант А»): готовый ответ разбора — программа проверяет каждое add / edit тем же test_condition;
    если что-то не пройдёт, Gemma один раз видит итог и может поправить ответ."""
    def review(r):
        if sum(1 for x in rows if x.get("diff") is not None) < 2 * MIN_DAYS:
            return None        # 25.10: меньше 10 законченных дней — ни одно правило пройти не может, переспрашивать незачем
        book = {cond_text(x["cond"]) for x in book_load(run)}
        lines, bad = [], 0
        items = [("add", t) for t in (r.get("add") or [])[:ADD_MAX]] + \
                [(f"edit R{e.get('id')}", e.get("rule", "")) for e in (r.get("edit") or []) if isinstance(e, dict)]
        for kind, t in items:
            res = test_condition(str(t), rows)
            c = parse_cond(str(t))
            if isinstance(c, list) and kind == "add" and cond_text(c) in book:
                res, bad = f"{cond_text(c)}: already in the book — program would REJECT", bad + 1
            elif "ACCEPT" not in res:
                bad += 1
            lines.append(f"- {kind} «{str(t)[:100]}»: {res}")
        if not bad:
            return None
        return ("PROGRAM CHECK of your answer (exact, before anything is saved):\n" + "\n".join(lines) +
                "\nWhat the program REJECTS will not be saved. You may replace those proposals with conditions the program "
                "accepts (test them with the tool first) or drop them. Answer again with the final JSON in the same format — "
                "this is your last answer for this day.")
    return review


def tool_for(rows, today=None):
    return TOOL, lambda a: test_condition(a.get("condition", "") if isinstance(a, dict) else str(a), rows, today)


def _max_id(run, v=None):
    """Номера правил не повторяются и после удаления."""
    p = dl.OUT / run / "rules_next_id.txt"
    if v is not None:
        p.write_text(str(max(v, int(p.read_text()) if p.exists() else 0)))
        return v
    return int(p.read_text()) if p.exists() else 0


# ---------- опора программы (Alex 25.10, «делай»: пункты 1 и 2) ----------
# Проверка без Gemma (RESEARCH_JOURNAL 25.10): медиана + среднее сдвигов всех условий, которые прошли бы проверку книги и
# выполнены сегодня, — 48 городов с января 1.62°F (медиана + ошибка 7 дней 1.70, голая медиана 2.00); Gemma в сентябре
# 1.22-1.60°F при 0.94 у медианы + ошибки 7 дней. Поэтому прогноз начинается с опоры, а не с голой медианы.

ANCHOR_QS = (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8)
ANCHOR_MIN = 10            # законченных дней в таблице — раньше опора = медиана
ANCHOR_OWN = 0.5           # поправка на собственную ошибку опоры за 7 дней: 48 городов 1.57°F (без неё 1.62, полная 1.65)


def _quantile(v, q):
    """Как numpy.quantile (линейная), чтобы опора совпадала с проверкой scratchpad/shift_check.py."""
    v = sorted(v)
    k = (len(v) - 1) * q
    i = int(k)
    return v[i] + (v[min(i + 1, len(v) - 1)] - v[i]) * (k - i)


def _raw(rows, x):
    """Медиана + средний сдвиг выполненных сегодня условий, прошедших проверку книги: (°F, сколько условий)."""
    done = [r for r in rows if r.get("diff") is not None]
    if x.get("med") is None:
        return None, 0
    if len(done) < ANCHOR_MIN:
        return x["med"], 0
    shifts = []
    for c in COL_NAMES:
        if x.get(c) is None:
            continue
        vals = [r[c] for r in done if r.get(c) is not None]
        if len(vals) < ANCHOR_MIN:
            continue
        for q in ANCHOR_QS:
            th = _quantile(vals, q)
            for op in (">=", "<="):
                cond = [(c, op, th)]
                if holds(cond, x):
                    m = measure(cond, done)
                    if not m["why"]:
                        shifts.append(m["mean"])
    return x["med"] + (statistics.mean(shifts) if shifts else 0.0), len(shifts)


def _history(done):
    """Опора на каждом законченном дне (только по дням до него): [(сырая, итоговая)]; итоговая = сырая − 0.5 × средняя
    ошибка сырой за 7 предыдущих дней."""
    out = []
    for i in range(ANCHOR_MIN, len(done)):
        raw = _raw(done[:i], done[i])[0]
        last = [r - done[j]["actual"] for j, (r, _) in zip(range(i - len(out), i), out)][-7:]
        out.append((raw, raw - ANCHOR_OWN * (statistics.mean(last) if last else 0.0)))
    return out


def anchor(rows, x, hist=None):
    """Прогноз программы на день x по таблице rows (только законченные дни до x): (°F, сколько условий учтено)."""
    raw, n = _raw(rows, x)
    if raw is None:
        return None, 0
    done = [r for r in rows if r.get("diff") is not None]
    hist = _history(done) if hist is None else hist
    last = [h[0] - done[ANCHOR_MIN + k]["actual"] for k, h in enumerate(hist)][-7:]
    return raw - ANCHOR_OWN * (statistics.mean(last) if last else 0.0), n


def anchor_text(rows, x):
    """Опора на сегодня + её точность на прошлых днях таблицы (каждый день — только по дням до него)."""
    done = [r for r in rows if r.get("diff") is not None]
    hist = _history(done)
    a, n = anchor(rows, x, hist)
    if a is None:
        return "PROGRAM FORECAST: not available (no model forecasts)."
    errs = [h[1] - done[ANCHOR_MIN + k]["actual"] for k, h in enumerate(hist)]
    raw = _raw(rows, x)[0]
    out = (f"PROGRAM FORECAST (computed by code): {a:.1f}°F = med {x['med']:.1f} "
           + (f"{raw - x['med']:+.1f} (average shift of the {n} table conditions that hold today and pass the book's check)"
              if n else "(no table condition passes the check yet)")
           + (f" {a - raw:+.1f} (half of its own mean error over the last 7 days, taken back)" if abs(a - raw) >= 0.05 else ""))
    if errs:
        out += (f". Its record on {len(errs)} past days: mean |error| {statistics.mean(abs(e) for e in errs):.1f}°F, "
                f"mean (forecast − actual) {statistics.mean(errs):+.1f}°F")
    return out + "."


def record_text(rec):
    """Счёт Gemma: rec — её прошлые прогнозы [{expected, anchor, actual}] (последние 10)."""
    rec = [dict(r, expected=_num(r.get("expected"))) for r in rec]
    rec = [r for r in rec if r["expected"] is not None and r.get("actual") is not None][-10:]
    if not rec:
        return "YOUR RECORD: no finished forecasts yet."
    you = [r["expected"] - r["actual"] for r in rec]
    prog = [r["anchor"] - r["actual"] for r in rec if r.get("anchor") is not None]
    closer = sum(1 for r in rec if r.get("anchor") is not None and
                 abs(r["expected"] - r["actual"]) < abs(r["anchor"] - r["actual"]))
    same = sum(1 for r in rec if r.get("anchor") is not None and
               abs(r["expected"] - r["actual"]) == abs(r["anchor"] - r["actual"]))
    lines = [f"YOUR RECORD (computed by code, your last {len(rec)} forecasts, oldest first; your forecast / program / actual): "
             + "; ".join(f"{r['expected']:.1f} / {r['anchor']:.1f} / {r['actual']:.0f}" if r.get("anchor") is not None
                         else f"{r['expected']:.1f} / - / {r['actual']:.0f}" for r in rec)]
    lines.append(f"Your mean |error| {statistics.mean(abs(e) for e in you):.1f}°F, mean (forecast − actual) {statistics.mean(you):+.1f}°F"
                 + (f"; program's mean |error| {statistics.mean(abs(e) for e in prog):.1f}°F, mean {statistics.mean(prog):+.1f}°F; "
                    f"you were closer than the program on {closer} of {len(prog)} days" + (f", equal on {same}" if same else "")
                    if prog else ""))
    return "\n".join(lines)


def floor_probs(probs, buckets):
    if not probs:
        return probs
    top = max(probs, key=probs.get)
    out = dict(probs)
    if top in buckets:
        i = buckets.index(top)
        for j in range(max(0, i - 2), min(len(buckets), i + 3)):
            out[buckets[j]] = max(out.get(buckets[j], 0.0), PROB_FLOOR)
    s = sum(out.values())
    return {k: v / s for k, v in out.items()}


# ---------- запросы ----------

ROLE = ("You forecast the daily maximum temperature at one airport weather station, every morning at 08:00, to bet on it. "
        "Your memory is a RULE BOOK: rules 'IF <conditions on numeric columns> THEN <shift °F versus the forecast median>'. "
        "A program works for you: it computes all numbers, checks every rule against the table of past days and sets each "
        "rule's shift to what really happened on the days its condition held. You have a TOOL from the program: "
        "test_condition('<condition>') — it checks any condition on the table exactly and tells if the program would accept it "
        "as a rule. You cannot change the tool, only use it (up to " + str(dl.TOOL_MAX) + " calls per answer). Never count or "
        "recompute table rows yourself — call the tool instead; test a condition before you propose it. Your job: notice what "
        "matters on this day and in the table, test your ideas with the tool, and make the forecast. Think briefly and to the "
        "point, then answer with JSON.")
EXAM_TASK = ("TASK: it is now 08:00 on the market day. Forecast the highest temperature of the day: start from the PROGRAM "
             "FORECAST (it already includes med and every table condition that holds today). Move away from it only for a reason you "
             "can see in today's data, in the rules that HOLD today or in YOUR RECORD (e.g. if you keep over- or under-forecasting, "
             "correct it); say that reason in factors. Keep the uncertainty the program measured above. Answer with JSON only:\n"
             '{"expected_max": <°F number>, "probs": {<every bucket label>: <probability>}, "rules_used": [<rule numbers>], '
             '"factors": "<one sentence>"}\nProbabilities must cover every bucket and sum to 1. Buckets: ')
CHANGE_TASK = ("Now improve the rule book for future days, using the WHOLE table. You may: add up to " + str(ADD_MAX) +
               " new conditions worth testing (the program accepts a rule only if the table supports it: held on at least "
               + str(MIN_DAYS) + " days, not on almost all days, and diff on those days differs from other days by at least "
               + str(MIN_SEP) + "°F; it then sets the shift itself), edit the condition of a rule, delete a weak rule. "
               "Write conditions only with the column names, e.g. 'IF dew08 >= 70 AND cloud <= 30'. Answer with JSON only:\n"
               '{"check": "<short: what you see in this day and the table>", "add": ["IF ..."], '
               '"edit": [{"id": <n>, "rule": "IF ..."}], "delete": [<rule numbers>]}')


def prompt(run, rows, data, task, today=None, last="", extra=""):
    rules = book_load(run)
    return (f"{ROLE}\n\nRULE BOOK (checked by the program on the table):\n{book_text(rules, rows, today)}\n\n"
            + (f"LAST CHANGES (program's verdict on your previous proposals): {last}\n\n" if last else "")
            + f"{table_text(rows)}\n\n{spread_text(rows)}\n\n"
            + (today_text(today) + "\n\n" if today else "") + (extra + "\n\n" if extra else "") + f"DATA:\n{data}\n\n{task}")


def answer_probs(r, buckets):
    probs = {k: float(v) for k, v in (r.get("probs") or {}).items() if k in buckets and dl._num(v)}
    s = sum(probs.values())
    return floor_probs({k: v / s for k, v in probs.items()}, buckets) if s > 0 else {}


def ask_forecast(run, meta, day, rows, step, label, until=None, record=None):
    """→ (ответ, шансы, опора). record — прошлые прогнозы Gemma для «YOUR RECORD» (None — блока нет)."""
    x = features(meta, day)
    x_today = {k: v for k, v in x.items() if k not in ("actual", "diff")}
    until = until or dl.moment_ts(day, 0, 8, meta["tz"])
    data = dl.day_text(meta, day, until=until)
    a = anchor(rows, x_today)[0]
    extra = anchor_text(rows, x_today) + ("\n\n" + record_text(record) if record is not None else "")
    r = dl.ask(prompt(run, rows, data, EXAM_TASK + ", ".join(day["buckets"]), today=x_today, extra=extra), step, run,
               label=label, tool=tool_for(rows, x_today)) or {}
    return r, answer_probs(r, day["buckets"]), a


def ask_changes(run, meta, day, rows, step, label, head, last):
    data = dl.day_text(meta, day, reveal=True)
    task = f"TASK: this day is shown with its RESULT; it is the last row of the table.\n{head}\n{CHANGE_TASK}"
    return dl.ask(prompt(run, rows, data, task, last=last), step, run, label=label, tool=tool_for(rows),
                  review=review_for(run, rows)) or {}


# ---------- схема «день за днём» ----------

def study(run, month, n, k, last):
    meta, days = dl.days_of(run, month, n)
    rows_all = [dict(features(meta, d), date=d["date"]) for d in days]
    for i, day in enumerate(days):
        rows = rows_all[:i + 1] if k == 1 else rows_all
        head = score_day(run, rows_all[i], f"study_p{k}_{day['date'][8:]}") if k == 1 else \
            "Second pass over the same month: the table is complete."
        r = ask_changes(run, meta, day, rows, f"study_{month}_p{k}", day["date"], head, last)
        last = apply_changes(run, r, rows, f"study_p{k}_{day['date'][8:]}")
        dl.say(run, f"study {day['date']}: {str(r.get('check', ''))[:200]}")
    return last


def walk(run, learn, test, n=None):
    for m in (learn, test):
        if not (dl.OUT / run / f"data_{m}.json").exists():
            dl.build(run, m)
    st = lambda stage: (dl.set_status(run, stage=stage),
                        dl.say(run, f"=== этап {stage}: {dict((k, w) for k, w, _ in dl.WALK_STAGES)[stage]}"))
    lmeta, ldays = dl.days_of(run, learn, n)
    last = ""
    for k in range(1, dl.WALK_STUDY + 1):
        if sum(1 for c in dl._calls(run) if c.get("step") == f"study_{learn}_p{k}") >= len(ldays):
            continue
        st(f"study{k}")
        last = study(run, learn, n, k, last)
    st("walk")
    meta, days = dl.days_of(run, test, n)
    f = dl.exam_file(run, test, "walk")
    old = [json.loads(x) for x in f.open()] if f.exists() else []
    done = {x["date"] for x in old}
    acts = {d["date"]: d["actual_max"] for d in days}
    record = [{"expected": x.get("expected_max"), "anchor": x.get("anchor"), "actual": acts.get(x["date"])} for x in old]
    total = sum(json.loads(x).get("bet", {}).get("pnl", 0) or 0 for x in f.open()) if f.exists() else 0.0
    past = [dict(features(lmeta, d), date=d["date"]) for d in ldays]
    for i, day in enumerate(days):
        x = dict(features(meta, day), date=day["date"])
        if day["date"] in done:
            past.append(x)
            continue
        t0 = time.time()
        r, probs, anc = ask_forecast(run, meta, day, past, f"exam_{test}_walk", f"{day['date']} m08", record=record)
        bet = dl.walk_bet(day, probs) if probs else None
        total += bet["pnl"] if bet else 0
        past.append(x)
        record.append({"expected": r.get("expected_max"), "anchor": anc, "actual": day["actual_max"]})
        e_ = _num(r.get("expected_max"))
        ok_e = e_ is not None
        head = (f"Your answer at 08:00 was: expected max {r.get('expected_max')}, rules used {r.get('rules_used')}, "
                f"factors: {r.get('factors')}. "
                + (f"Program forecast was {anc:.1f}°F, actual {day['actual_max']:.0f}°F: your error "
                   f"{abs(e_ - day['actual_max']):.1f}°F, program's {abs(anc - day['actual_max']):.1f}°F → "
                   + ("you were closer. " if abs(e_ - day['actual_max']) < abs(anc - day['actual_max'])
                      else "same. " if abs(e_ - day['actual_max']) == abs(anc - day['actual_max'])
                      else "the program was closer. ") if ok_e and anc is not None else "")
                + (f"You bet ${STAKE:.0f} on {bet['bucket']} at {bet['price'] * 100:.0f}¢ and "
                   f"{'WON' if bet['pnl'] > 0 else 'LOST'} {abs(bet['pnl']):.2f}$. " if bet else "No bet (no 10-point edge). ")
                + score_day(run, x, f"walk_{day['date']}"))
        rv = ask_changes(run, meta, day, past, f"walk_review_{test}", day["date"], head, last)
        last = apply_changes(run, rv, past, f"walk_{day['date']}")
        rec = {"t": round(t0), "date": day["date"], "moment": "m08", "expected_max": r.get("expected_max"), "anchor": anc, "probs": probs,
               "factors": r.get("factors"), "rules_used": r.get("rules_used"), "ok": bool(probs),
               "seconds": round(time.time() - t0, 1), "diary_words": len(dl.read_diary(run).split()), "bet": bet,
               "why": rv.get("check")}
        with f.open("a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        dl.say(run, f"walk {day['date']}: max {rec['expected_max']}, опора {anc:.1f} (было {day['actual_max']:.0f}) "
                    f"p(верный {day['win']})={probs.get(day['win'], 0):.2f}; правила {r.get('rules_used')}; "
                    + (f"ставка {bet['bucket']} по {bet['price'] * 100:.0f}¢ → {bet['pnl']:+.2f}$" if bet else "без ставки")
                    + f"; итого {total:+.2f}$")
    dl.score(run, test, ["walk"])


# ---------- живая ставка утром ----------

LIVE_FILE = "live_bets.jsonl"


def past_rows(run, city, upto):
    """Все законченные дни до upto: месяцы прогона (data_*.json) + недостающие дни из базы и Iowa (кэш data_live.json)."""
    rows, have = [], set()
    for p in sorted((dl.OUT / run).glob("data_20??-??.json")):
        meta = json.loads(p.read_text())
        for d in meta["days"]:
            if d["date"] < upto.isoformat():
                rows.append(dict(features(meta, d), date=d["date"]))
                have.add(d["date"])
    cfg = OBS_CITIES[city]
    cache = dl.OUT / run / "data_live.json"
    extra = json.loads(cache.read_text()) if cache.exists() else {"city": city, "unit": cfg["unit"], "tz": cfg["tz"],
                                                                   "station": cfg["icao"], "days": []}
    got = {d["date"] for d in extra["days"]}
    start = max([date.fromisoformat(x) for x in have] or [upto - timedelta(days=60)]) + timedelta(days=1)
    need = [start + timedelta(days=i) for i in range((upto - start).days) if (start + timedelta(days=i)).isoformat() not in got]
    if need:
        obs = dl.iowa_obs(cfg, need[0] - timedelta(days=2), need[-1] + timedelta(days=1))
        conn = dl.db()
        for d in need:
            x = dl.build_day(conn, city, d, obs, prices=False)
            if x["win"] is not None:
                extra["days"].append(x)
        extra["days"].sort(key=lambda d: d["date"])
        cache.write_text(json.dumps(extra))
    for d in extra["days"]:
        if d["date"] not in have and d["date"] < upto.isoformat():
            rows.append(dict(features(extra, d), date=d["date"]))
    return sorted(rows, key=lambda r: r["date"])


def predict(run, day_s=None, dry=False):
    """Утро дня маркета: данные дня как в прогонах (build_day), книга правил, Gemma → шансы → одна ставка $2 по настоящему
    стакану (polyexec.simulate_buy: задержка, комиссия, минимум 5 долей). Пока бумажная — в live_bets.jsonl; кошелёк — решение Alex."""
    import weather_llm_hour as lh
    from polyexec import simulate_buy
    city = dl.city_of(run)
    cfg = OBS_CITIES[city]
    tz = ZoneInfo(cfg["tz"])
    now = datetime.now(timezone.utc)
    d = date.fromisoformat(day_s) if day_s else now.astimezone(tz).date()
    out = dl.OUT / run / LIVE_FILE
    if not dry and out.exists() and any(json.loads(x)["date"] == d.isoformat() for x in out.open()):
        print(f"{run} {d}: прогноз уже сделан")
        return
    mk = lh.markets(city, d)
    if not mk:
        print(f"{run} {d}: маркета нет")
        return
    for b in mk:
        b["bucket"] = dl.bucket_label(b["lo"], b["hi"])
    obs = dl.iowa_obs(cfg, d - timedelta(days=2), d + timedelta(days=1))
    day = dl.build_day(dl.db(), city, d, obs, prices=False)
    day["buckets"] = [b["bucket"] for b in mk]
    meta = {"city": city, "unit": cfg["unit"], "tz": cfg["tz"], "station": cfg["icao"]}
    rows = past_rows(run, city, d)
    until = min(now.timestamp(), dl.moment_ts(day, 0, 8, cfg["tz"]) + 3600)
    t0 = time.time()
    acts = {x["date"]: x["actual"] for x in rows if x.get("actual") is not None}
    record = [{"expected": x.get("expected_max"), "anchor": x.get("anchor"), "actual": acts.get(x["date"])}
              for x in (map(json.loads, out.open()) if out.exists() else [])]
    r, probs, anc = ask_forecast(run, meta, day, rows, "live_predict", f"{d} m08", until=until, record=record)
    rec = {"t": round(t0), "date": d.isoformat(), "asked_at": now.isoformat(), "seconds": round(time.time() - t0, 1),
           "expected_max": r.get("expected_max"), "anchor": anc, "probs": probs, "rules_used": r.get("rules_used"), "factors": r.get("factors"),
           "rules": dl.read_diary(run), "past_days": len(rows),
           "market": {b["bucket"]: {"ask": b["ask"], "bid": b["bid"]} for b in mk}, "bet": None}
    cands = [(probs.get(b["bucket"], 0) - b["ask"], b) for b in mk
             if 0.05 <= b["ask"] <= 0.95 and probs.get(b["bucket"], 0) - b["ask"] >= dl.EDGE]
    stopped = (dl.ROOT / "data" / "STOP").exists()
    if cands and not stopped:
        gap, b = max(cands, key=lambda c: c[0])
        q = probs[b["bucket"]]
        tok = json.loads(b["m"]["clobTokenIds"])[0]
        f = {"shares": 0, "reason": "проба (--dry)"} if dry else simulate_buy(b["m"], tok, STAKE, q - dl.EDGE / 2)
        rec["bet"] = {"bucket": b["bucket"], "p_ai": round(q, 3), "ask": b["ask"], "shares": f["shares"],
                      "cost": f.get("cost"), "fee": f.get("fee"), "avg": f.get("avg"), "book": f.get("book"),
                      "reason": f.get("reason"), "token": tok, "condition_id": b["m"].get("conditionId")}
    elif stopped:
        rec["no_bet"] = "data/STOP — ставки остановлены"
    else:
        rec["no_bet"] = "ни у одного варианта шанс не выше цены на 10 п.п."
    if not dry:
        with out.open("a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    b = rec["bet"]
    txt = (f"live {d}: max {rec['expected_max']}, " + ", ".join(f"{k} {100 * v:.0f}%" for k, v in sorted(probs.items(), key=lambda t: -t[1])[:3])
           + "; " + (f"купили {b['bucket']} {b['shares']:.1f} долей по {100 * b['avg']:.1f}¢ (ИИ {100 * b['p_ai']:.0f}%)" if b and b["shares"]
                     else f"ставки нет: {b['reason'] if b else rec.get('no_bet')}"))
    dl.say(run, txt)
    if not dry:
        _news(f"Дневник 2, живая ставка Атланта {d} (бумажная, $2): {txt}")
    print(txt)


def _news(text):
    with open(dl.OUT / "news.jsonl", "a") as f:
        f.write(json.dumps({"t": round(time.time()), "text": text}, ensure_ascii=False) + "\n")


def settle(run):
    """Итог живых ставок: выигравший вариант Polymarket (weather_poly_outcomes) → выплата 1 за долю."""
    p = dl.OUT / run / LIVE_FILE
    if not p.exists():
        return []
    conn = dl.db()
    city = dl.city_of(run)
    out = []
    for x in map(json.loads, p.open()):
        w = conn.execute("SELECT win_lo, win_hi FROM weather_poly_outcomes WHERE city=? AND local_date=?", (city, x["date"])).fetchone()
        x["win"] = dl.bucket_label(*w) if w else None
        b = x.get("bet")
        if b and b.get("shares") and x["win"]:
            x["pnl"] = (b["shares"] if b["bucket"] == x["win"] else 0) - b["cost"] - b["fee"]
        out.append(x)
    return out
