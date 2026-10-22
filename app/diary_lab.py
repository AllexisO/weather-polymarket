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
--days N — только первые N дней (проба). Движок — Gemma в виртуальной машине на NAS (Ollama, OLLAMA_URL / GEMMA_MODEL); не запускается,
пока Gemma занята прогоном скилла (agy_skill_bt.py). Запуск в фоне — ./diary_run.sh.
22.10 (Alex: «видеть и контролировать визуально»): страница /diary (app/diary_status.py). Для неё программа пишет
live.jsonl — рассуждения модели построчно со временем (ответ идёт потоком), status.json — что запущено и какой этап.
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
OUT = Path(os.environ.get("DIARY_OUT", ROOT / "data" / "research" / "diary"))   # DIARY_OUT — проверка на копии
URL = os.environ.get("OLLAMA_URL", "http://192.168.1.16:11434") + "/api/chat"
MODEL = os.environ.get("GEMMA_MODEL", "gemma4:26b")
MOMENTS = [("eve", -1, 18), ("m08", 0, 8), ("m12", 0, 12)]   # (метка, день относительно дня маркета, час местного)
DIARY_WORDS = 1500        # выше — предупреждение в журнале (дневник только дополняется, не обрезается)
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

# ---------- движок ----------

def gemma_busy():
    # 22.10: только сам прогон (python3 …agy_skill_bt.py run) — свободный шаблон ловил и оболочки с этим текстом
    return subprocess.run(["pgrep", "-f", r"^python[0-9.]* \S*agy_skill_bt\.py run"], capture_output=True).returncode == 0


def training_now():
    return subprocess.run(["pgrep", "-f", r"^python /app/job_wrap\.py weather_ml_(live|day)\.py --train"],
                          capture_output=True).returncode == 0


def night_wait(city):
    # 22.10: ночное обучение (05:20 weather_ml_live, 05:45 weather_ml_day) идёт на том же процессоре NAS, что и Gemma —
    # 21.10 оба сняты по таймауту. С 05:05 Gemma ждёт, пока оба не закончатся (не раньше 05:47, не позже 08:30);
    # 22.10 такая пауза вручную — обучение прошло за 6 и 4 мин.
    tz = ZoneInfo("Europe/Chisinau")
    now = datetime.now(tz)
    t = lambda h, m: now.replace(hour=h, minute=m, second=0, microsecond=0)
    if not t(5, 5) <= now < t(8, 30):
        return
    say(city, "⏸ ночное обучение моделей — Gemma ждёт до его конца (не позже 08:30)")
    last = time.time()
    while True:
        now = datetime.now(tz)
        if now >= t(8, 30) or (now >= t(5, 47) and not training_now()):
            break
        if time.time() - last > 600:                      # строка раз в 10 мин — /diary не считает процесс остановившимся
            say(city, "⏸ ждём конца ночного обучения")
            last = time.time()
        time.sleep(30)
    say(city, "▶ обучение закончилось — продолжаем")


LIVE_MAX = 5_000_000       # live.jsonl больше 5 МБ — старое в live.old.jsonl


def live(city, kind, text, step="", label=""):
    """Строка живого терминала /diary: время, этап, что (info / think / answer), текст."""
    f = OUT / city / "live.jsonl"
    try:
        if f.exists() and f.stat().st_size > LIVE_MAX:
            f.replace(OUT / city / "live.old.jsonl")
        with open(f, "a") as fh:
            fh.write(json.dumps({"t": round(time.time(), 1), "step": step, "label": label, "kind": kind, "text": text},
                                ensure_ascii=False) + "\n")
    except OSError:
        pass


def say(city, text):
    """Строка хода работы: в лог запуска и в живой терминал."""
    print(text, flush=True)
    live(city, "info", text, STATE.get("stage", ""))


STATE = {}


def set_status(city, **kw):
    """status.json для /diary: команда, этап, начало, закончено ли."""
    STATE.update(kw)
    try:
        (OUT / city / "status.json").write_text(json.dumps(STATE, ensure_ascii=False))
    except OSError:
        pass


def ask(prompt, step, city, want_json=True, label=""):
    body = {"model": MODEL, "messages": [{"role": "user", "content": prompt}], "stream": True,
            "options": {"num_ctx": int(os.environ.get("GEMMA_CTX", "32768")), "temperature": 1.0, "top_p": 0.95,
                        "top_k": 64, "num_predict": 8000}}
    night_wait(city)
    t0 = time.time()
    live(city, "info", f"▶ запрос {step} {label}: {len(prompt)} символов", step, label)
    req = urllib.request.Request(URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
    think, text, buf, r = [], [], {"think": "", "answer": ""}, {}
    with urllib.request.urlopen(req, timeout=1800) as resp:      # потоком: каждая строка рассуждения — в терминал сразу
        for raw in resp:
            if not raw.strip():
                continue
            r = json.loads(raw)
            msg = r.get("message") or {}
            for kind, piece, acc in (("think", msg.get("thinking") or "", think), ("answer", msg.get("content") or "", text)):
                if not piece:
                    continue
                acc.append(piece)
                buf[kind] += piece
                while "\n" in buf[kind] or len(buf[kind]) > 300:
                    cut = buf[kind].find("\n")
                    cut = cut if 0 <= cut <= 300 else 300
                    line, buf[kind] = buf[kind][:cut], buf[kind][cut + (1 if buf[kind][cut:cut + 1] == "\n" else 0):]
                    if line.strip():
                        live(city, kind, line, step, label)
            if r.get("done"):
                break
    for kind in ("think", "answer"):
        if buf[kind].strip():
            live(city, kind, buf[kind], step, label)
    text = "".join(text)
    rec = {"t": round(t0), "step": step, "label": label, "model": MODEL, "seconds": round(time.time() - t0, 1),
           "prompt_chars": len(prompt), "prompt_tokens": r.get("prompt_eval_count"), "tokens": r.get("eval_count"),
           "think": "".join(think)[:4000], "response": text}
    with open(OUT / city / "calls.jsonl", "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    live(city, "info", f"✓ ответ за {rec['seconds']:.0f} с, {rec['tokens'] or 0} токенов", step, label)
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
    diary_path(city).write_text(text + "\n")
    hist = OUT / city / "history"
    hist.mkdir(exist_ok=True)
    (hist / f"{datetime.now():%m%d_%H%M%S}_{step}.md").write_text(text + "\n")
    n = len(text.split())
    say(city, f"  дневник: {n} слов ({step})" + (f" — больше {DIARY_WORDS}, проверить окно Gemma" if n > DIARY_WORDS else ""))


MISLED_RE = re.compile(r" \(misled me (\d+)×\)$")


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", MISLED_RE.sub("", s).lstrip("-* ").lower()).strip()


def consolidate(city, lessons, misleading, step):
    # 22.10 (Alex): дневник только дополняется — новые уроки в конец, старые не стираются.
    # Урок, который подвёл на разборе, остаётся с пометкой «misled me N×» — видно, что ему меньше веры.
    if not lessons and not misleading:
        return
    lines = [ln for ln in read_diary(city).splitlines() if ln.strip()]
    marked = 0
    for m in {_norm(x) for x in misleading if _norm(x)}:
        for i, ln in enumerate(lines):
            if m in _norm(ln) or (_norm(ln) and _norm(ln) in m):
                k = MISLED_RE.search(ln)
                lines[i] = MISLED_RE.sub("", ln) + f" (misled me {int(k.group(1)) + 1 if k else 1}×)"
                marked += 1
                break
    have = {_norm(ln) for ln in lines}
    added = 0
    for x in lessons:
        x = " ".join(str(x).split())
        if x and _norm(x) not in have and not DATE_RE.search(x):
            lines.append(f"- {x}")
            have.add(_norm(x))
            added += 1
    say(city, f"  в дневник: +{added} уроков, подвели {marked}")
    save_diary(city, "\n".join(lines), step)


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
        r = ask(prompt, f"study_{month}", city, label=day["date"])
        new = [str(x) for x in (r or {}).get("lessons", [])][:LESSONS_MAX]
        lessons += new
        say(city, f"study {day['date']}: уроков {len(new)}")
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
            r = ask(exam_prompt(diary, meta, day, off, hour), f"exam_{month}_{tag}", city, label=f"{day['date']} {m}") or {}
            probs = {k: float(v) for k, v in (r.get("probs") or {}).items() if k in day["buckets"] and _num(v)}
            s = sum(probs.values())
            probs = {k: v / s for k, v in probs.items()} if s > 0 else {}
            rec = {"t": round(t0), "date": day["date"], "moment": m, "expected_max": r.get("expected_max"), "probs": probs,
                   "factors": r.get("factors"), "ok": bool(probs), "seconds": round(time.time() - t0, 1),
                   "diary_words": len(diary.split())}
            with f.open("a") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            say(city, f"exam {tag} {day['date']} {m}: max {rec['expected_max']} (было {day['actual_max']:.0f}) "
                      f"p(верный {day['win']})={probs.get(day['win'], 0):.2f}")


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
        r = ask(prompt, f"review_{month}_{tag}", city, label=day["date"]) or {}
        lessons += [str(x) for x in r.get("lessons", [])][:LESSONS_MAX]
        misleading += [str(x) for x in r.get("misleading", [])]
        say(city, f"review {tag} {day['date']}: ошибок {len(bad)}, уроков {len(r.get('lessons', []))}")
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


def score_rows(city, month, tag):
    """Итог экзамена по моментам: logloss ИИ / рынка / формулы, промах, ставки и доходность (верхняя оценка). И для /diary."""
    meta = load(city, month)
    by_date = {d["date"]: d for d in meta["days"]}
    f = exam_file(city, month, tag)
    ans = [json.loads(x) for x in f.open()] if f.exists() else []
    out = []
    for m, off, hour in MOMENTS:
        ll = {"ai": [], "mkt": [], "base": []}
        err, pnl, n_bet = [], 0.0, 0
        n_all = sum(1 for x in ans if x["moment"] == m)
        n_ok = sum(1 for x in ans if x["moment"] == m and x["ok"])
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
        avg = lambda xs: sum(xs) / len(xs) if xs else None
        out.append({"tag": tag, "moment": m, "n": len(ll["ai"]), "n_all": n_all, "n_ok": n_ok, "ai": avg(ll["ai"]),
                    "mkt": avg(ll["mkt"]), "base": avg(ll["base"]), "mae": avg(err), "bets": n_bet,
                    "roi": pnl / n_bet * 100 if n_bet else None})
    return out


def score(city, month, tags):
    print(f"{city} {month}: logloss верного варианта (меньше — лучше), промах максимума °F, деньги — верхняя оценка (price_history)")
    for tag in tags:
        for r in score_rows(city, month, tag):
            if not r["n"]:
                continue
            print(f"  {tag:>8} {r['moment']}: дней {r['n']:2d}  ИИ {r['ai']:.3f}  рынок {r['mkt']:.3f}  формула {r['base']:.3f}"
                  f"  промах {r['mae'] if r['mae'] is not None else float('nan'):.1f}°  ставок {r['bets']} доходность "
                  f"{(r['roi'] or 0):+.1f}%")


# этапы полного цикла — для status.json и /diary (метка, что делает, месяц: learn / test)
STAGES = [("r0", "экзамен без дневника (контроль)", "learn"), ("study", "знакомство: дни с ответом → дневник", "learn"),
          ("r1", "круг 1: экзамен", "learn"), ("review_r1", "круг 1: разбор ошибок", "learn"),
          ("r2", "круг 2: экзамен", "learn"), ("review_r2", "круг 2: разбор ошибок", "learn"),
          ("r3", "круг 3: экзамен", "learn"), ("review_r3", "круг 3: разбор ошибок", "learn"),
          ("final", "финал по учебному месяцу", "learn"), ("n1", "проверка: новый месяц с дневником", "test"),
          ("n1_empty", "проверка: новый месяц без дневника", "test")]


def cycle(city, learn, test, n=None):
    if not (OUT / city / f"data_{learn}.json").exists():
        build(city, learn)
    if not (OUT / city / f"data_{test}.json").exists():
        build(city, test)
    st = lambda stage: (set_status(city, stage=stage), say(city, f"=== этап {stage}: {dict((k, w) for k, w, _ in STAGES)[stage]}"))
    st("r0")
    exam(city, learn, "r0", empty=True, n=n)                   # контроль: без дневника
    st("study")
    study(city, learn, n)
    for r in ("r1", "r2", "r3"):
        st(r)
        exam(city, learn, r, n=n)
        st(f"review_{r}")
        review(city, learn, r, n=n)
    st("final")
    exam(city, learn, "final", n=n)
    st("n1")
    exam(city, test, "n1", n=n)                                 # честная проверка: новый месяц, без разбора
    st("n1_empty")
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
    if step not in ("build", "score"):
        set_status(city, cmd=" ".join(sys.argv[1:]), step=step, learn=month, test=rest[0] if step == "cycle" else None,
                   days=n, model=MODEL, pid=os.getpid(), started=round(time.time()), finished=None, error=None,
                   stage=step if step != "cycle" else "")
        say(city, f"=== старт: {' '.join(sys.argv[1:])}, модель {MODEL}")
    try:
        _step(step, city, month, rest, n, empty)
    except BaseException as e:
        if step not in ("build", "score"):
            set_status(city, finished=round(time.time()), error=f"{type(e).__name__}: {e}"[:300])
            say(city, f"!!! остановлено: {type(e).__name__}: {e}")
        raise
    if step not in ("build", "score"):
        set_status(city, finished=round(time.time()))
        say(city, "=== готово")


def _step(step, city, month, rest, n, empty):
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
