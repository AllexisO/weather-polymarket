#!/usr/bin/env python3
"""
«ИИ с дневником» (решение Alex 21.10: «сделать основной и делать деньги на ней»). Устройство — docs/DIARY.md.

ИИ сама разбирает месяц погоды одного города час за часом и пишет себе дневник уроков (без дат). Потом сдаёт экзамены
в изоляции: каждый вызов — с чистого листа, видит только дневник и данные, известные в момент ставки (накануне 18:00,
08:00, 12:00 местного). После экзамена разбирает свои ошибки на полных данных дня и правит дневник. Круги 1-3 и финал —
по учебному месяцу; честная проверка — первый экзамен следующего, ещё не виденного месяца, рядом с тем же экзаменом
без дневника (контроль). Порог — docs/RESEARCH_JOURNAL.md 21.10, записан до первого прогона.

Шаги (каждый можно запускать отдельно, всё пишется в data/research/diary/<город>/):
  build  <город> <ГГГГ-ММ>           данные месяца: замеры Iowa (все поля), прогнозы за сутки, итог, цены рынка
  study  <город> <ГГГГ-ММ>           первый проход: разбор каждого дня с ответом → уроки → дневник
  exam   <город> <ГГГГ-ММ> <метка>   экзамен (--empty — без дневника, контроль)
  review <город> <ГГГГ-ММ> <метка>   разбор ошибок экзамена → правка дневника
  score  <город> <ГГГГ-ММ> <метка…>  точность против рынка и простой формулы, деньги (по price_history — верхняя оценка)
  cycle  <город> <учебный> <проверочный>  всё подряд: study, r0 (без дневника), r1..r3 с разбором, final, проверка
--days N — только первые N дней (проба). Движок — Gemma на ПК Alex (Ollama, OLLAMA_URL / GEMMA_MODEL); не запускается,
пока Gemma занята прогоном скилла (agy_skill_bt.py). Запуск в фоне — ./diary_run.sh.
"""
import csv
import io
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from weather_cities import OBS_CITIES

ROOT = Path(__file__).resolve().parent.parent
DB = Path(os.environ.get("POLY_LAB_DB", ROOT / "data" / "db" / "polymarket_lab.sqlite3"))
OUT = ROOT / "data" / "research" / "diary"
URL = os.environ.get("OLLAMA_URL", "http://192.168.1.16:11434") + "/api/chat"
MODEL = os.environ.get("GEMMA_MODEL", "gemma4:26b")
MOMENTS = [("eve", -1, 18), ("m08", 0, 8), ("m12", 0, 12)]   # (метка, день относительно дня маркета, час местного)
DIARY_WORDS = 1500        # предел дневника: больше — ИИ теряется в своих заметках
LESSONS_MAX = 3           # уроков с одного дня
MISS_P = 0.35             # ошибка: шанс верного варианта ниже этого или промах максимума ≥ 3°
BASE_SD = 3.0             # простая формула: нормальное распределение вокруг среднего прогноза, σ = 3°F (записано до прогона)
EDGE = 0.10               # деньги: покупаем вариант, если шанс ИИ выше цены на 10 п.п. и больше
IOWA_FIELDS = ["tmpf", "dwpf", "relh", "drct", "sknt", "gust", "alti", "vsby", "skyc1", "skyl1", "skyc2", "skyl2",
               "skyc3", "skyl3", "wxcodes", "p01i"]


def db():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)


def month_days(month):
    y, m = map(int, month.split("-"))
    d = date(y, m, 1)
    while d.month == m:
        yield d
        d += timedelta(days=1)


def bucket_label(lo, hi):
    if lo < -900:
        return f"≤{int(hi - 0.5)}"
    if hi > 900:
        return f"≥{int(lo + 0.5)}"
    return f"{int(lo + 0.5)}-{int(hi - 0.5)}"


# ---------- данные ----------

def iowa(cfg, d1, d2):
    params = {"station": cfg["iem"], "data": IOWA_FIELDS, "year1": d1.year, "month1": d1.month, "day1": d1.day,
              "year2": d2.year, "month2": d2.month, "day2": d2.day, "tz": "Etc/UTC", "format": "onlycomma",
              "latlon": "no", "missing": "M", "trace": "T", "report_type": [3, 4]}
    url = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?" + urllib.parse.urlencode(params, doseq=True)
    for attempt in range(5):
        text = urllib.request.urlopen(url, timeout=300).read().decode()
        if "Too many requests" not in text[:200]:
            return list(csv.DictReader(io.StringIO(text)))
        time.sleep(15 * (attempt + 1))
    raise RuntimeError("Iowa: too many requests")


def build(city, month):
    cfg = OBS_CITIES[city]
    tz = ZoneInfo(cfg["tz"])
    days = list(month_days(month))
    obs = iowa(cfg, days[0] - timedelta(days=2), days[-1] + timedelta(days=2))
    for o in obs:
        o["t"] = datetime.strptime(o.pop("valid"), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()
        o.pop("station", None)
    conn = db()
    out = []
    for d in days:
        ds = d.isoformat()
        win = conn.execute("SELECT win_lo, win_hi FROM weather_poly_outcomes WHERE city=? AND local_date=?", (city, ds)).fetchone()
        mx = conn.execute("SELECT actual_max FROM weather_station_daily WHERE city=? AND local_date=?", (city, ds)).fetchone()
        if not win or not mx:
            continue
        fc = dict(conn.execute("SELECT model, fcst_max FROM mm_forecasts WHERE city=? AND local_date=? AND lead='day1'", (city, ds)))
        fv = dict(conn.execute("SELECT var, value FROM ml_fcst_vars WHERE city=? AND local_date=?", (city, ds)))
        bks = conn.execute("SELECT DISTINCT bucket_lo, bucket_hi FROM price_history WHERE city=? AND local_date=? ORDER BY 1",
                           (city, ds)).fetchall()
        prices = {}
        for tag, off, hour in MOMENTS:
            ts = datetime.combine(d + timedelta(days=off), datetime.min.time(), tz).replace(hour=hour).timestamp()
            row = {}
            for lo, hi in bks:
                p = conn.execute("SELECT p, t_utc FROM price_history WHERE city=? AND local_date=? AND bucket_lo=? AND bucket_hi=? "
                                 "AND t_utc<=? ORDER BY t_utc DESC LIMIT 1", (city, ds, lo, hi, ts)).fetchone()
                if p:
                    row[bucket_label(lo, hi)] = {"p": p[0], "age_min": round((ts - p[1]) / 60)}
            prices[tag] = row
        start = datetime.combine(d, datetime.min.time(), tz).timestamp()
        out.append({"date": ds, "start": start, "buckets": [bucket_label(lo, hi) for lo, hi in bks],
                    "win": bucket_label(*win), "actual_max": mx[0], "forecasts": fc, "fcst_vars": fv, "prices": prices,
                    "obs": [o for o in obs if start - 2 * 86400 <= o["t"] < start + 86400]})
    path = OUT / city / f"data_{month}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"city": city, "unit": cfg["unit"], "tz": cfg["tz"], "station": cfg["icao"], "days": out}))
    print(f"{city} {month}: {len(out)} дней с итогом, замеров {len(obs)} → {path}")


def load(city, month):
    return json.loads((OUT / city / f"data_{month}.json").read_text())


# ---------- текст для ИИ ----------

def _f(v):
    """'47.00' → '47', '29.90' → '29.9', 'M' → '-'"""
    if v in ("M", "", None):
        return "-"
    try:
        return f"{float(v):g}"
    except ValueError:
        return v


def obs_table(rows, tz):
    head = "local time | T°F | dew°F | RH% | wind dir°/kt gust | press inHg | vis mi | clouds (cover@ft) | weather | precip in"
    lines = [head]
    for o in rows:
        clouds = " ".join(f"{o[f'skyc{i}']}@{_f(o[f'skyl{i}'])}" if o[f"skyl{i}"] != "M" else o[f"skyc{i}"]
                          for i in (1, 2, 3) if o[f"skyc{i}"] not in ("M", ""))
        wind = "calm" if _f(o["sknt"]) == "0" else f"{_f(o['drct'])}/{_f(o['sknt'])}" + (f" g{_f(o['gust'])}" if o["gust"] != "M" else "")
        t = datetime.fromtimestamp(o["t"], tz)
        lines.append(f"{t:%a %H:%M} | {_f(o['tmpf'])} | {_f(o['dwpf'])} | {_f(o['relh']) if o['relh'] == 'M' else round(float(o['relh']))} | {wind} | {_f(o['alti'])} | "
                     f"{_f(o['vsby'])} | {clouds or '-'} | {_f(o['wxcodes'])} | {_f(o['p01i'])}")
    return "\n".join(lines)


def day_text(meta, day, until=None, reveal=False):
    """Данные дня. until — момент ставки (unix): замеры только до него. reveal — с итогом (для разбора)."""
    tz = ZoneInfo(meta["tz"])
    fv = day["fcst_vars"]
    wind = ""
    if "wind_dir_sin" in fv:
        wind = f"{(math.degrees(math.atan2(fv['wind_dir_sin'], fv['wind_dir_cos'])) + 360) % 360:.0f}°"
    parts = [
        f"STATION {meta['station']}, unit °F, local time {meta['tz']}. Market: the highest temperature of the local day "
        f"(00:00-23:59), whole °F as reported by the station. Weekday of the market day: {datetime.fromtimestamp(day['start'], tz):%A}.",
        "MARKET BUCKETS: " + ", ".join(day["buckets"]),
        "MODEL FORECASTS of the daily max, issued the day before (°F): "
        + (", ".join(f"{k} {v:.1f}" for k, v in sorted(day["forecasts"].items())) or "none"),
        "FORECAST conditions 11:00-17:00 local (ECMWF average): "
        + (f"cloud {fv.get('cloud_cover', 0):.0f}%, dew point {fv.get('dew_point_2m', 0):.1f}°C, RH {fv.get('relative_humidity_2m', 0):.0f}%, "
           f"solar radiation {fv.get('shortwave_radiation', 0):.0f} W/m², wind {fv.get('wind_speed_10m', 0):.0f} km/h from {wind}, "
           f"precipitation {fv.get('precipitation', 0):.1f} mm" if fv else "none"),
    ]
    rows = [o for o in day["obs"] if (until is None or o["t"] <= until) and (reveal or o["t"] < day["start"] + 86400)]
    parts.append("OBSERVATIONS (the market day starts at 00:00; earlier rows are the previous days):\n" + obs_table(rows, tz))
    if reveal:
        parts.append(f"RESULT: the highest temperature was {day['actual_max']:.0f}°F, winning bucket {day['win']}.")
    return "\n\n".join(parts)


def moment_ts(day, off, hour, tz):
    d = date.fromisoformat(day["date"]) + timedelta(days=off)
    return datetime.combine(d, datetime.min.time(), ZoneInfo(tz)).replace(hour=hour).timestamp()


ROLE = ("You are learning to forecast the daily maximum temperature at one airport weather station. Your only memory is "
        "your DIARY: general lessons you wrote yourself about what drives the temperature here. You never see dates in "
        "the diary and must never write dates, specific days or specific results into it.")

EXAM_TASK = ("TASK: it is now {when} local time. Using the diary and the data, estimate the highest temperature of the "
             "market day. Go through the observations hour by hour and the forecasts, then answer with JSON only:\n"
             '{{"expected_max": <°F number>, "probs": {{<every bucket label>: <probability>}}, "factors": "<one sentence>"}}\n'
             "Probabilities must cover every bucket and sum to 1.")


def exam_prompt(diary, meta, day, off, hour):
    until = moment_ts(day, off, hour, meta["tz"])
    when = f"{hour:02d}:00 on the day before the market day" if off else f"{hour:02d}:00 on the market day"
    return (f"{ROLE}\n\nDIARY:\n{diary or '(empty)'}\n\nDATA:\n{day_text(meta, day, until=until)}\n\n"
            + EXAM_TASK.format(when=when))


STUDY_TASK = ("TASK: this day is shown with its RESULT. Walk through it hour by hour: why the temperature rose or fell, "
              "what set the maximum and when, what the forecasts got wrong. Then write at most {n} GENERAL lessons that "
              "would help forecast other days at this station (conditions → effect, no dates, no specific numbers of this "
              "day). Answer with JSON only:\n"
              '{{"walk": "<short hour-by-hour notes>", "lessons": ["...", "..."]}}')

REVIEW_TASK = ("TASK: this is a day from your exam, now with its RESULT. Your answers were:\n{answers}\n"
               "Find why you were wrong: which observation or forecast you misread or ignored, which diary lesson "
               "misled you. Write at most {n} GENERAL lessons (conditions → effect, no dates, no specific numbers of "
               "this day) and list diary lessons that misled you. Answer with JSON only:\n"
               '{{"why": "<short>", "lessons": ["..."], "misleading": ["<quoted diary line>", ...]}}')

CONSOLIDATE_TASK = ("TASK: rewrite your diary. Merge the NEW LESSONS into it, join duplicates, drop lessons marked as "
                    "misleading unless they still hold in general, keep only what helps forecast the daily maximum. "
                    "At most {words} words, short bullet points grouped by topic. No dates, no specific days. "
                    "Answer with the diary text only.")


# ---------- движок ----------

def gemma_busy():
    return subprocess.run(["pgrep", "-f", "agy_skill_bt.py run"], capture_output=True).returncode == 0


def ask(prompt, step, city, want_json=True):
    body = {"model": MODEL, "messages": [{"role": "user", "content": prompt}], "stream": False,
            "options": {"num_ctx": int(os.environ.get("GEMMA_CTX", "32768")), "temperature": 1.0, "top_p": 0.95,
                        "top_k": 64, "num_predict": 8000}}
    t0 = time.time()
    req = urllib.request.Request(URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=1800))
    text = r["message"].get("content") or ""
    rec = {"t": round(t0), "step": step, "seconds": round(time.time() - t0, 1), "prompt_chars": len(prompt),
           "prompt_tokens": r.get("prompt_eval_count"), "tokens": r.get("eval_count"),
           "think": (r["message"].get("thinking") or "")[:4000], "response": text}
    with open(OUT / city / "calls.jsonl", "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if not want_json:
        return text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        return None


# ---------- дневник ----------

DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}[./]\d{1,2}|(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}"
                     r"|\d{1,2}(st|nd|rd|th)?\s+(of\s+)?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
                     r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)


def diary_path(city):
    return OUT / city / "diary.md"


def read_diary(city):
    p = diary_path(city)
    return p.read_text() if p.exists() else ""


def save_diary(city, text, step):
    # даты не должны попасть в дневник (Alex 21.10): строки с датой или днём недели выкидываем
    lines = [ln for ln in text.splitlines() if not DATE_RE.search(ln)]
    text = "\n".join(lines).strip()
    words = text.split()
    if len(words) > DIARY_WORDS * 1.2:
        text = " ".join(words[:int(DIARY_WORDS * 1.2)])
    diary_path(city).write_text(text + "\n")
    hist = OUT / city / "history"
    hist.mkdir(exist_ok=True)
    (hist / f"{datetime.now():%m%d_%H%M}_{step}.md").write_text(text + "\n")
    print(f"  дневник: {len(text.split())} слов ({step})", flush=True)


def consolidate(city, lessons, misleading, step):
    if not lessons and not misleading:
        return
    prompt = (f"{ROLE}\n\nDIARY:\n{read_diary(city) or '(empty)'}\n\nNEW LESSONS:\n" + "\n".join(f"- {x}" for x in lessons)
              + ("\n\nMISLEADING LESSONS (from your mistakes):\n" + "\n".join(f"- {x}" for x in misleading) if misleading else "")
              + "\n\n" + CONSOLIDATE_TASK.format(words=DIARY_WORDS))
    text = ask(prompt, f"consolidate_{step}", city, want_json=False)
    text = re.sub(r"^```\w*\n|\n```$", "", text.strip())
    if text:
        save_diary(city, text, step)


# ---------- шаги ----------

def days_of(city, month, n):
    data = load(city, month)
    return data, data["days"][:n] if n else data["days"]


def study(city, month, n=None):
    meta, days = days_of(city, month, n)
    lessons = []
    for i, day in enumerate(days, 1):
        prompt = (f"{ROLE}\n\nDIARY:\n{read_diary(city) or '(empty)'}\n\nDATA:\n{day_text(meta, day, reveal=True)}\n\n"
                  + STUDY_TASK.format(n=LESSONS_MAX))
        r = ask(prompt, f"study_{month}", city)
        new = [str(x) for x in (r or {}).get("lessons", [])][:LESSONS_MAX]
        lessons += new
        print(f"study {day['date']}: уроков {len(new)}", flush=True)
        if i % 7 == 0 or i == len(days):          # раз в неделю — сведение в дневник
            consolidate(city, lessons, [], f"study_{month}_{i}")
            lessons = []


def exam_file(city, month, tag):
    return OUT / city / f"exam_{month}_{tag}.jsonl"


def exam(city, month, tag, empty=False, n=None):
    meta, days = days_of(city, month, n)
    diary = "" if empty else read_diary(city)
    f = exam_file(city, month, tag)
    done = {(x["date"], x["moment"]) for x in map(json.loads, f.open())} if f.exists() else set()
    for day in days:
        for m, off, hour in MOMENTS:
            if (day["date"], m) in done:
                continue
            t0 = time.time()
            r = ask(exam_prompt(diary, meta, day, off, hour), f"exam_{month}_{tag}", city) or {}
            probs = {k: float(v) for k, v in (r.get("probs") or {}).items() if k in day["buckets"] and _num(v)}
            s = sum(probs.values())
            probs = {k: v / s for k, v in probs.items()} if s > 0 else {}
            rec = {"date": day["date"], "moment": m, "expected_max": r.get("expected_max"), "probs": probs,
                   "factors": r.get("factors"), "ok": bool(probs), "seconds": round(time.time() - t0, 1),
                   "diary_words": len(diary.split())}
            with f.open("a") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"exam {tag} {day['date']} {m}: max {rec['expected_max']} p(верный)={probs.get(day['win'], 0):.2f}", flush=True)


def _num(v):
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def misses(day, answers):
    out = []
    for a in answers:
        p = a["probs"].get(day["win"], 0)
        em = a["expected_max"] if _num(a["expected_max"]) else None
        if not a["ok"] or p < MISS_P or (em is not None and abs(float(em) - day["actual_max"]) >= 3):
            out.append(a)
    return out


def review(city, month, tag, n=None):
    meta, days = days_of(city, month, n)
    ans = [json.loads(x) for x in exam_file(city, month, tag).open()]
    lessons, misleading = [], []
    for day in days:
        bad = misses(day, [a for a in ans if a["date"] == day["date"]])
        if not bad:
            continue
        answers = "\n".join(f"- at {m}: expected max {a['expected_max']}, probabilities {json.dumps(a['probs'])}, "
                            f"factors: {a['factors']}" for a in bad for m in [a["moment"]])
        prompt = (f"{ROLE}\n\nDIARY:\n{read_diary(city) or '(empty)'}\n\nDATA:\n{day_text(meta, day, reveal=True)}\n\n"
                  + REVIEW_TASK.format(answers=answers, n=LESSONS_MAX))
        r = ask(prompt, f"review_{month}_{tag}", city) or {}
        lessons += [str(x) for x in r.get("lessons", [])][:LESSONS_MAX]
        misleading += [str(x) for x in r.get("misleading", [])]
        print(f"review {tag} {day['date']}: ошибок {len(bad)}, уроков {len(r.get('lessons', []))}", flush=True)
    # сведение по частям, чтобы запрос влез в окно Gemma
    for i in range(0, len(lessons), 20):
        consolidate(city, lessons[i:i + 20], misleading[i:i + 20] if i == 0 else [], f"review_{month}_{tag}_{i}")


def baseline_probs(day, buckets, until, moment):
    fc = list(day["forecasts"].values())
    if not fc:
        return {}
    mu = sum(fc) / len(fc)
    if moment == "m12":                               # днём: уже набранный максимум — нижняя граница
        seen = [float(o["tmpf"]) for o in day["obs"] if day["start"] <= o["t"] <= until and _num(o["tmpf"])]
        if seen:
            mu = max(mu, max(seen))
    cdf = lambda x: 0.5 * (1 + math.erf((x - mu) / (BASE_SD * math.sqrt(2))))
    out = {}
    for b in buckets:
        if b.startswith("≤"):
            lo, hi = -1e9, int(b[1:]) + 0.5
        elif b.startswith("≥"):
            lo, hi = int(b[1:]) - 0.5, 1e9
        else:
            a, c = b.split("-")
            lo, hi = int(a) - 0.5, int(c) + 0.5
        out[b] = cdf(hi) - cdf(lo)
    return out


def score(city, month, tags):
    meta = load(city, month)
    by_date = {d["date"]: d for d in meta["days"]}
    print(f"{city} {month}: logloss верного варианта (меньше — лучше), промах максимума °F, деньги — верхняя оценка (price_history)")
    for tag in tags:
        ans = [json.loads(x) for x in exam_file(city, month, tag).open()]
        for m, off, hour in MOMENTS:
            ll = {"ai": [], "mkt": [], "base": []}
            err, pnl, n_bet = [], 0.0, 0
            for a in (x for x in ans if x["moment"] == m and x["ok"]):
                day = by_date[a["date"]]
                pr = {k: v["p"] for k, v in day["prices"].get(m, {}).items()}
                s = sum(pr.values())
                if s <= 0:
                    continue
                until = moment_ts(day, off, hour, meta["tz"])
                base = baseline_probs(day, day["buckets"], until, m)
                ll["ai"].append(-math.log(max(a["probs"].get(day["win"], 0), 0.01)))
                ll["mkt"].append(-math.log(max(pr.get(day["win"], 0) / s, 0.01)))
                ll["base"].append(-math.log(max(base.get(day["win"], 0), 0.01)))
                if _num(a["expected_max"]):
                    err.append(abs(float(a["expected_max"]) - day["actual_max"]))
                for b, p_ai in a["probs"].items():   # $1 на вариант, где ИИ выше цены на EDGE; комиссия тех дней 0.05·p(1−p)
                    p = pr.get(b)
                    if p is None or not 0.05 <= p <= 0.95 or p_ai - p < EDGE:
                        continue
                    buy = min(p + 0.01, 0.999)
                    n_bet += 1
                    pnl += (1 / (buy + 0.05 * buy * (1 - buy)) - 1) if b == day["win"] else -1
            k = len(ll["ai"])
            if not k:
                continue
            avg = lambda xs: sum(xs) / len(xs)
            print(f"  {tag:>8} {m}: дней {k:2d}  ИИ {avg(ll['ai']):.3f}  рынок {avg(ll['mkt']):.3f}  формула {avg(ll['base']):.3f}"
                  f"  промах {avg(err) if err else float('nan'):.1f}°  ставок {n_bet} доходность "
                  f"{(pnl / n_bet * 100 if n_bet else 0):+.1f}%")


def cycle(city, learn, test, n=None):
    if not (OUT / city / f"data_{learn}.json").exists():
        build(city, learn)
    if not (OUT / city / f"data_{test}.json").exists():
        build(city, test)
    exam(city, learn, "r0", empty=True, n=n)                   # контроль: без дневника
    study(city, learn, n)
    for r in ("r1", "r2", "r3"):
        exam(city, learn, r, n=n)
        review(city, learn, r, n=n)
    exam(city, learn, "final", n=n)
    exam(city, test, "n1", n=n)                                 # честная проверка: новый месяц, без разбора
    exam(city, test, "n1_empty", empty=True, n=n)               # тот же месяц без дневника
    score(city, learn, ["r0", "r1", "r2", "r3", "final"])
    score(city, test, ["n1_empty", "n1"])


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    n = next((int(a.split("=")[1]) for a in sys.argv if a.startswith("--days=")), None)
    empty = "--empty" in sys.argv
    if len(args) < 3:
        print(__doc__)
        sys.exit(1)
    step, city, month, *rest = args
    (OUT / city).mkdir(parents=True, exist_ok=True)
    if step not in ("build", "score") and gemma_busy():
        sys.exit("Gemma занята прогоном скилла (agy_skill_bt.py) — не запускаю, чтобы не тормозить его")
    if step == "build":
        build(city, month)
    elif step == "study":
        study(city, month, n)
    elif step == "exam":
        exam(city, month, rest[0], empty=empty, n=n)
    elif step == "review":
        review(city, month, rest[0], n=n)
    elif step == "score":
        score(city, month, rest)
    elif step == "cycle":
        cycle(city, month, rest[0], n=n)


if __name__ == "__main__":
    main()
