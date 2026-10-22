"""22.10 (просьба Alex: «отдельная страница для дневника — видеть и контролировать визуально, как проходит логика»):
страница /diary — ход «ИИ с дневником» (app/diary_lab.py, docs/DIARY.md). Только чтение файлов
data/research/diary/<город>/: status.json (что запущено, этап), live.jsonl (рассуждения модели построчно со временем),
calls.jsonl (все запросы), exam_*.jsonl (ответы экзаменов), diary.md и history/ (дневник и его версии).
Цифры экзаменов — diary_lab.score_rows, те же, что печатает `diary_lab.py score`.
"""
import json
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import diary_lab as dl

TZ = ZoneInfo("Europe/Chisinau")
MOMENT_NAME = {"eve": "накануне 18:00", "m08": "08:00", "m12": "12:00"}
KIND_NAME = {"info": "ход   ", "think": "думает", "answer": "ответ "}
LEARN_TAGS = ["r0", "r1", "r2", "r3", "final"]
TEST_TAGS = ["n1_empty", "n1"]
TAG_NAME = {"r0": "без дневника", "r1": "круг 1", "r2": "круг 2", "r3": "круг 3", "final": "финал",
            "n1_empty": "новый месяц без дневника", "n1": "новый месяц с дневником"}
QUIET_MIN = 20     # дольше нет новых строк — «нет признаков работы»


def _jsonl(p):
    try:
        return [json.loads(x) for x in p.read_text(errors="replace").splitlines() if x.strip()]
    except (OSError, ValueError):
        return []


def _hm(ts, fmt="%d.%m %H:%M"):
    return datetime.fromtimestamp(ts, TZ).strftime(fmt) if ts else "—"


def _dur(sec):
    sec = int(sec)
    if sec < 90:
        return f"{sec} с"
    if sec < 5400:
        return f"{sec // 60} мин"
    return f"{sec // 3600} ч {sec % 3600 // 60:02d} мин"


def cities():
    return sorted(p.name for p in dl.OUT.iterdir() if p.is_dir() and any(p.glob("data_*.json"))) if dl.OUT.exists() else []


def _months(city):
    return sorted(p.stem[5:] for p in (dl.OUT / city).glob("data_*.json"))


def _state(city, st):
    """Идёт ли сейчас: запуск не закончен и новые строки в live.jsonl были недавно."""
    live = dl.OUT / city / "live.jsonl"
    last = live.stat().st_mtime if live.exists() else None
    now = time.time()
    if not st:
        return {"code": "none", "text": "ещё не запускали", "last": last}
    if st.get("finished"):
        if st.get("error"):
            return {"code": "bad", "text": f"остановлено с ошибкой {_hm(st['finished'])}", "last": last}
        return {"code": "done", "text": f"закончено {_hm(st['finished'])}", "last": last}
    if last and now - last < QUIET_MIN * 60:
        return {"code": "on", "text": f"идёт {_dur(now - st['started'])}", "last": last}
    return {"code": "warn", "text": f"нет новых строк {_dur(now - (last or st['started']))} — процесс мог остановиться",
            "last": last}


def _stages(city, st, calls, n_days):
    """Этапы цикла: сколько сделано из скольких и где сейчас."""
    learn, test = st.get("learn"), st.get("test")
    if not learn:
        ms = _months(city)
        learn, test = (ms[0], ms[1]) if len(ms) > 1 else (ms[0] if ms else None, None)
    out = []
    started_after = False
    cur = st.get("stage") if st and not st.get("finished") else None
    for key, what, which in dl.STAGES:
        month = learn if which == "learn" else test
        nd = n_days.get(month, 0)
        if key == "study":
            done = sum(1 for c in calls if c.get("step") == f"study_{learn}")
            need = nd
        elif key.startswith("review_"):
            tag = key[7:]
            done = sum(1 for c in calls if c.get("step") == f"review_{learn}_{tag}")
            need = _miss_days(city, learn, tag, nd)
        else:
            f = dl.exam_file(city, month, key) if month else None
            done = len(_jsonl(f)) if f and f.exists() else 0
            need = nd * len(dl.MOMENTS)
        out.append({"key": key, "what": what, "month": month, "done": done, "need": need, "cur": key == cur})
    # этап закончен, если начат следующий (у разбора «нужно» известно только после экзамена)
    for i in range(len(out) - 1, -1, -1):
        s = out[i]
        if started_after or (s["need"] and s["done"] >= s["need"] and not s["cur"]):
            s["state"] = "done"
        elif s["cur"]:
            s["state"] = "cur"
        else:
            s["state"] = "wait"
        if s["done"] or s["cur"]:
            started_after = True
    return out, learn, test


def _miss_days(city, month, tag, n):
    """Сколько дней экзамена попадёт в разбор (те же правила, что diary_lab.misses)."""
    f = dl.exam_file(city, month, tag)
    if not f.exists():
        return 0
    try:
        days = dl.load(city, month)["days"][:n or None]
    except (OSError, ValueError):
        return 0
    ans = _jsonl(f)
    return sum(1 for d in days if dl.misses(d, [a for a in ans if a["date"] == d["date"]]))


def _chart(rows, moment, tags):
    """SVG: logloss верного варианта по кругам — ИИ, рынок, простая формула (меньше — лучше)."""
    pts = [(t, r) for t in tags for r in rows if r["tag"] == t and r["moment"] == moment and r["n"]]
    if not pts:
        return None
    W, H, L, R, T, B = 360, 190, 34, 12, 14, 42
    vals = [r[k] for _, r in pts for k in ("ai", "mkt", "base") if r[k] is not None]
    top = max(1.0, max(vals) * 1.1)
    xs = {t: L + (W - L - R) * (i / max(1, len(pts) - 1) if len(pts) > 1 else 0.5) for i, (t, _) in enumerate(pts)}
    y = lambda v: T + (H - T - B) * (1 - v / top)
    parts = [f'<svg viewBox="0 0 {W} {H}" class="dchart" role="img" aria-label="logloss по кругам, {MOMENT_NAME[moment]}">']
    for k in range(0, 5):
        v = top * k / 4
        parts.append(f'<line x1="{L}" x2="{W - R}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="g"/>'
                     f'<text x="{L - 6}" y="{y(v) + 4:.1f}" class="ax" text-anchor="end">{v:.1f}</text>')
    for t, _ in pts:
        lab = {"final": "фин", "n1_empty": "янв −", "n1": "янв +"}.get(t, t)
        parts.append(f'<text x="{xs[t]:.1f}" y="{H - B + 18}" class="ax" text-anchor="middle">{lab}</text>')
    for key, cls in (("base", "s-base"), ("mkt", "s-mkt"), ("ai", "s-ai")):
        line = [(xs[t], y(r[key])) for t, r in pts if r[key] is not None]
        if len(line) > 1:
            parts.append(f'<polyline class="{cls}" points="' + " ".join(f"{a:.1f},{b:.1f}" for a, b in line) + '"/>')
        for (t, r), (a, b) in zip([(t, r) for t, r in pts if r[key] is not None], line):
            name = {"ai": "ИИ", "mkt": "рынок", "base": "формула"}[key]
            parts.append(f'<circle class="{cls}" cx="{a:.1f}" cy="{b:.1f}" r="3.5"><title>{TAG_NAME.get(t, t)} · {name}: '
                         f'{r[key]:.3f} ({r["n"]} дн.)</title></circle>')
    parts.append("</svg>")
    return "".join(parts)


def _lessons(calls, last=12):
    """Последние уроки из учёбы и разборов (то, что ИИ решила записать)."""
    out = []
    for c in reversed(calls):
        if not str(c.get("step", "")).startswith(("study_", "review_")):
            continue
        m = re.search(r"\{.*\}", c.get("response") or "", re.S)
        try:
            r = json.loads(m.group(0)) if m else {}
        except ValueError:
            r = {}
        for x in (r.get("lessons") or [])[:dl.LESSONS_MAX]:
            out.append({"at": _hm(c["t"]), "step": "учёба" if c["step"].startswith("study_") else "разбор",
                        "day": c.get("label", ""), "text": str(x)})
        if len(out) >= last:
            break
    return out[:last]


def page_data(city=None, version=None):
    cs = cities()
    city = city if city in cs else (cs[0] if cs else None)
    if not city:
        return {"city": None, "cities": cs, "updated": _hm(time.time(), "%d.%m %H:%M:%S")}
    base = dl.OUT / city
    try:
        st = json.loads((base / "status.json").read_text())
    except (OSError, ValueError):
        st = {}
    calls = _jsonl(base / "calls.jsonl")
    n_days = {}
    for m in _months(city):
        try:
            k = len(dl.load(city, m)["days"])
        except (OSError, ValueError):
            k = 0
        n_days[m] = min(k, st["days"]) if st.get("days") else k
    stages, learn, test = _stages(city, st, calls, n_days)
    state = _state(city, st)

    # запросы этого запуска: время, токены, ответы в нужном виде
    run_calls = [c for c in calls if c.get("t", 0) >= st.get("started", 0)] if st else []
    exams = []
    for f in sorted(base.glob("exam_*.jsonl")):
        month, tag = f.stem[5:12], f.stem[13:]
        for a in _jsonl(f):
            a.update(month=month, tag=tag)
            exams.append(a)
    run_ex = [a for a in exams if a.get("t", 0) >= st.get("started", 0)] if st else []
    ok_pct = 100 * sum(a["ok"] for a in run_ex) / len(run_ex) if run_ex else None
    avg_s = sum(c["seconds"] for c in run_calls) / len(run_calls) if run_calls else None
    cur_call = None
    live = _jsonl(base / "live.jsonl")[-400:]
    for e in reversed(live):
        if e["kind"] == "info" and e["text"].startswith("▶ запрос"):
            done = any(x["kind"] == "info" and x["text"].startswith("✓") and x["t"] >= e["t"] for x in live)
            if not done:
                cur_call = {"what": e["text"][10:], "since": _dur(time.time() - e["t"])}
            break

    # итоги экзаменов: учебный месяц по кругам и проверочный
    rows = []
    for month, tags in ((learn, LEARN_TAGS), (test, TEST_TAGS)):
        if not month:
            continue
        for t in tags:
            if dl.exam_file(city, month, t).exists():
                rows += [dict(r, month=month) for r in dl.score_rows(city, month, t)]
    charts = [{"moment": m, "name": MOMENT_NAME[m], "svg": _chart(rows, m, LEARN_TAGS + TEST_TAGS)} for m, _, _ in dl.MOMENTS]

    # последние ответы экзаменов: прогноз против факта, шанс верного варианта у ИИ и у рынка
    facts = {}
    for m in {a["month"] for a in exams}:
        try:
            facts.update({(m, d["date"]): d for d in dl.load(city, m)["days"]})
        except (OSError, ValueError):
            pass
    recent = {t: [] for t in LEARN_TAGS + TEST_TAGS}
    for a in sorted(exams, key=lambda a: a.get("t", 0))[::-1]:
        d = facts.get((a["month"], a["date"]))
        if not d:
            continue
        pr = {k: v["p"] for k, v in d["prices"].get(a["moment"], {}).items()}
        s = sum(pr.values())
        recent.setdefault(a["tag"], []).append({"t": a.get("t", 0), "at": _hm(a.get("t")), "tag": TAG_NAME.get(a["tag"], a["tag"]), "date": a["date"],
                       "moment": MOMENT_NAME[a["moment"]], "exp": a.get("expected_max"), "fact": d["actual_max"],
                       "win": d["win"], "p_ai": a["probs"].get(d["win"]) if a["ok"] else None,
                       "p_mkt": pr.get(d["win"], 0) / s if s else None, "ok": a["ok"], "factors": a.get("factors") or ""})

    # по экзаменам: без дневника, круги 1-3, финал, новый месяц (22.10, Alex)
    recent = [{"tag": t, "name": TAG_NAME.get(t, t), "rows": rs} for t, rs in recent.items() if rs or t in LEARN_TAGS]
    recent_on = max(recent, key=lambda g: g["rows"][0]["t"] if g["rows"] else 0)["tag"] if recent else None

    # дневник и его версии
    hist = sorted((base / "history").glob("*.md")) if (base / "history").exists() else []
    versions = []
    for h in hist:
        txt = h.read_text(errors="replace")
        versions.append({"file": h.name, "words": len(txt.split()), "step": h.stem.split("_", 2)[-1],
                         "at": _hm(h.stat().st_mtime)})
    diary_file = base / "diary.md"
    sel = next((v for v in versions if v["file"] == version), None)
    diary = (base / "history" / sel["file"]).read_text(errors="replace") if sel else \
        (diary_file.read_text(errors="replace") if diary_file.exists() else "")
    mx = max([v["words"] for v in versions] + [dl.DIARY_WORDS])
    for v in versions:
        v["h"] = round(100 * v["words"] / mx)

    return {"city": city, "cities": cs, "st": st, "state": state, "stages": stages, "learn": learn, "test": test,
            "model": st.get("model") or (calls[-1].get("model") if calls else dl.MODEL),
            "n_calls": len(run_calls), "avg_s": avg_s, "ok_pct": ok_pct, "n_ex": len(run_ex), "cur_call": cur_call,
            "tokens": sum((c.get("tokens") or 0) for c in run_calls), "rows": rows, "charts": charts,
            "recent": recent, "recent_on": recent_on, "lessons": _lessons(calls), "versions": versions[::-1], "version": sel,
            "diary": diary, "diary_words": len(diary.split()), "diary_max": dl.DIARY_WORDS,
            "moment_name": MOMENT_NAME, "tag_name": TAG_NAME, "updated": _hm(time.time(), "%d.%m %H:%M:%S")}


def terminal(city=None, last=300):
    """Живой терминал: каждая строка — дата и время, этап и день, что это (ход / думает / ответ), текст."""
    cs = cities()
    city = city if city in cs else (cs[0] if cs else None)
    if not city:
        return "дневник ещё не запускали"
    ev = _jsonl(dl.OUT / city / "live.jsonl")[-last:]
    if not ev:
        return "строк пока нет — программа дневника ещё не запускалась с живым терминалом"
    out = []
    for e in ev:
        where = " · ".join(x for x in (_step_name(e.get("step", "")), _label(e.get("label", ""))) if x)
        txt = " ".join(str(e.get("text", "")).split())
        out.append(f"{_hm(e['t'], '%d.%m %H:%M:%S')} │ {KIND_NAME.get(e['kind'], e['kind'])} │ {where[:52]:52s} │ {txt[:400]}")
    return "\n".join(out)


def _step_name(step):
    """exam_2025-12_r1 → «экзамен круг 1», study_2025-12 → «учёба», consolidate_… → «запись в дневник»."""
    if step.startswith("exam_"):
        return "экзамен " + TAG_NAME.get(step[13:], step[13:])
    if step.startswith("review_"):
        return "разбор " + TAG_NAME.get(step[15:], step[15:])
    if step.startswith("study_"):
        return "учёба"
    if step.startswith("consolidate_"):
        return "запись в дневник"
    return dict((k, w) for k, w, _ in dl.STAGES).get(step, step)


def _label(label):
    for m, name in MOMENT_NAME.items():
        label = label.replace(f" {m}", f" {name}")
    return label


def files_ver(city=None):
    """Отпечаток для живого обновления /diary: размер и время файлов дневника (кроме live.jsonl — терминал опрашивает сам)."""
    out = []
    for f in sorted(dl.OUT.glob("*/*")) if dl.OUT.exists() else []:
        if f.name == "live.jsonl" or f.is_dir():
            continue
        try:
            st = f.stat()
            out.append(f"{f.parent.name}/{f.name}:{st.st_size}:{int(st.st_mtime)}")
        except OSError:
            pass
    return "|".join(out)
