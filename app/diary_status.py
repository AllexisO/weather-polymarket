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
TEST_TAGS = ["n1_empty", "n1", "walk"]
TAG_NAME = {"r0": "без дневника", "r1": "круг 1", "r2": "круг 2", "r3": "круг 3", "final": "финал",
            "n1_empty": "новый месяц без дневника", "n1": "новый месяц с дневником",
            "walk": "день за днём, 08:00"}   # 24.10: diary_lab walk — схема Alex
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
    walk = (st or {}).get("step") == "walk"
    for key, what, which in (dl.WALK_STAGES if walk else dl.STAGES):
        month = learn if which == "learn" else test
        nd = n_days.get(month, 0)
        if walk and key.startswith("study"):
            done = sum(1 for c in calls if c.get("step") == f"study_{learn}_p{key[5:]}")
            need = nd
        elif walk:
            f = dl.exam_file(city, month, "walk") if month else None
            done = len(_jsonl(f)) if f and f.exists() else 0
            need = nd
        elif key == "study":
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


def _resp(c):
    """JSON из ответа модели в calls.jsonl (уроки, «подвели»)."""
    m = re.search(r"\{.*\}", c.get("response") or "", re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except ValueError:
        return {}


def _day_grid(city, st, s, calls, now_call, n_days):
    """24.10 (Alex: «живой прогресс каждого дня»): клетка на день этапа — готов / идёт сейчас (что делает, с какого
    времени) / ждёт / не нужен (разбор только дней с промахом); у готовой — коротко итог и подробности по нажатию."""
    month = s["month"]
    try:
        meta = dl.load(city, month)
    except (OSError, ValueError, TypeError):
        return []
    days = meta["days"][:n_days.get(month) or None]
    key, learn = s["key"], st.get("learn")
    walk = st.get("step") == "walk"
    if key.startswith("study"):
        step = f"study_{learn}_p{key[5:]}" if walk else f"study_{learn}"
    elif key.startswith("review_"):
        step = f"review_{learn}_{key[7:]}"
    else:
        step = None
    by_call = {c.get("label"): c for c in calls if step and c.get("step") == step}
    ans = {}
    if not step:
        f = dl.exam_file(city, month, key)
        for a in (_jsonl(f) if f.exists() else []):
            ans.setdefault(a["date"], []).append(a)
    review_calls = {c.get("label"): c for c in calls if c.get("step") == f"walk_review_{month}"} if key == "walk" else {}
    need_review = None
    if key.startswith("review_"):
        f = dl.exam_file(city, learn, key[7:])
        ex = _jsonl(f) if f.exists() else []
        need_review = {d["date"] for d in days if dl.misses(d, [a for a in ex if a["date"] == d["date"]])} if ex else None
    out = []
    for d in days:
        date = d["date"]
        cell = {"date": date, "n": date[8:], "state": "wait", "short": "", "detail": [], "fact": d["actual_max"], "win": d["win"]}
        is_now = bool(now_call and s["state"] == "cur" and str(now_call["label"]).startswith(date) and
                      (now_call["step"] == step or (not step and now_call["step"].startswith(f"exam_{month}_{key}"))
                       or (key == "walk" and now_call["step"] == f"walk_review_{month}")))
        if step:
            c = by_call.get(date)
            if c:
                r = _resp(c)
                ls = [str(x) for x in (r.get("lessons") or [])][:dl.LESSONS_MAX]
                if not ls and ("add" in r or "edit" in r):     # дневник 2: книга правил
                    ls = [str(x) for x in r.get("add") or []] + [f"правка R{e.get('id')}: {e.get('rule')}" for e in r.get("edit") or []
                                                                  if isinstance(e, dict)]
                    if r.get("check"):
                        cell["detail"].append(("Таблица", str(r["check"])))
                cell.update(state="done", short=f"{len(ls)} ур.", detail=cell["detail"] + [("Уроки", x) for x in ls] +
                            [("Подвели", str(x)) for x in (r.get("misleading") or [])] + [("Время", f"{c.get('seconds', 0):.0f} с")])
            elif need_review is not None and date not in need_review:
                cell.update(state="skip", short="не нужен")
        else:
            got = ans.get(date, [])
            for a in sorted(got, key=lambda a: [m for m, _, _ in dl.MOMENTS].index(a["moment"])):
                pr = {k: v["p"] for k, v in d["prices"].get(a["moment"], {}).items()}
                sm = sum(pr.values())
                p_ai = a["probs"].get(d["win"]) if a["ok"] else None
                p_m = pr.get(d["win"], 0) / sm if sm else None
                cell["detail"].append((MOMENT_NAME[a["moment"]],
                                       f"прогноз {a.get('expected_max')} · было {d['actual_max']:.0f} ({d['win']}) · шанс верного: ИИ "
                                       + (f"{100 * p_ai:.0f}%" if p_ai is not None else "нет ответа")
                                       + (f" / рынок {100 * p_m:.0f}%" if p_m is not None else "")))
                if a.get("factors"):
                    cell["detail"].append(("Почему", a["factors"]))
                if key == "walk":
                    b = a.get("bet")
                    cell["detail"].append(("Ставка", f"$2 на {b['bucket']} по {100 * b['price']:.0f}¢ (ИИ дала {100 * b['p_ai']:.0f}%) → "
                                           f"{'+' if b['pnl'] > 0 else '−'}${abs(b['pnl']):.2f}" if b else "не ставила: нигде нет перевеса 10 п.п."))
                    cell["pnl"] = b["pnl"] if b else None
                    if a.get("why"):
                        cell["detail"].append(("Разбор", str(a["why"])))
                    rc = review_calls.get(date)
                    rr = _resp(rc) if rc else {}
                    for x in (rr.get("lessons") or rr.get("add") or [])[:dl.LESSONS_MAX * 2]:
                        cell["detail"].append(("Урок", str(x)))
                    cell["short"] = f"{a.get('expected_max')} / {d['actual_max']:.0f}"
            if key != "walk" and got:
                ps = [a["probs"].get(d["win"], 0) for a in got if a["ok"]]
                cell["short"] = (f"{100 * sum(ps) / len(ps):.0f}%" if ps else "нет") + ("" if len(got) >= len(dl.MOMENTS) else f" · {len(got)}/3")
            if got and (key == "walk" or len(got) >= len(dl.MOMENTS)):
                cell["state"] = "done"
            elif got:
                cell["state"] = "part"
        if is_now:
            mo = str(now_call["label"]).split(" ")[-1]
            what = {"walk_review": "разбор итога и уроки", "exam": f"ответ {MOMENT_NAME.get(mo, '')}".strip(),
                    "study": "читает день и пишет уроки", "review": "разбирает ошибки"}
            k = next((x for x in what if now_call["step"].startswith(x)), None)
            cell.update(state="cur", now=what.get(k, "идёт"), since=now_call["t"])
            ec = next((c for c in calls if c.get("step") == f"exam_{month}_walk" and str(c.get("label")).startswith(date)), None)
            if key == "walk" and k == "walk_review" and ec:    # ответ 08:00 уже есть, ставка и итог — после разбора
                r = _resp(ec)
                top = sorted(((float(v), b) for b, v in (r.get("probs") or {}).items() if dl._num(v)), reverse=True)[:3]
                cell["detail"] = [("08:00", f"прогноз {r.get('expected_max')} · " + ", ".join(f"{b} {100 * v:.0f}%" for v, b in top)),
                                  ("Почему", str(r.get("factors") or ""))]
        out.append(cell)
    return out


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
        lab = {"final": "фин", "n1_empty": "нов −", "n1": "нов +", "walk": "день"}.get(t, t)
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
        for x in (r.get("lessons") or r.get("add") or [])[:dl.LESSONS_MAX]:
            out.append({"at": _hm(c["t"]), "step": "учёба" if c["step"].startswith("study_") else "разбор",
                        "day": c.get("label", ""), "text": str(x)})
        if len(out) >= last:
            break
    return out[:last]


def page_data(city=None, version=None):
    cs = cities()
    city = city if city in cs else (cs[0] if cs else None)
    if not city:
        return {"city": None, "cities": cs, "ov": overview(cs), "updated": _hm(time.time(), "%d.%m %H:%M:%S")}
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
    cur_call = now_call = None
    live = _jsonl(base / "live.jsonl")[-5000:]   # 24.10: один запрос Gemma — больше 400 строк рассуждений
    for i in range(len(live) - 1, -1, -1):
        e = live[i]
        if e["kind"] == "info" and e["text"].startswith("▶ запрос"):
            # по порядку строк, не по времени: ✓ прошлого запроса бывает в ту же десятую секунды (24.10)
            done = any(x["kind"] == "info" and x["text"].startswith("✓") for x in live[i + 1:])
            if not done:
                cur_call = {"what": e["text"].removeprefix("▶ запрос "), "since": _dur(time.time() - e["t"])}
                now_call = {"step": e.get("step", ""), "label": e.get("label", ""), "t": e["t"]}
            break
    for s in stages:      # клетки дней этапа (24.10, Alex)
        s["days"] = _day_grid(city, st, s, calls, now_call, n_days) if s["month"] else []
    # 24.10 (Alex): большой блок «Сейчас» — день, этап, что делает, полоса времени запроса (живая), общий ход
    now_box = None
    cur_s = next((x for x in stages if x.get("state") == "cur"), None)
    cell = next((c for c in cur_s["days"] if c["state"] == "cur"), None) if cur_s else None
    if cell and now_call and not st.get("finished"):
        same = [c["seconds"] for c in calls if c.get("step") == now_call["step"] and c.get("seconds")][-10:]
        card = _run_card(city)
        now_box = {"stage": cur_s["what"], "month": cur_s["month"], "stage_done": cur_s["done"], "stage_need": cur_s["need"],
                   "day_n": cur_s["days"].index(cell) + 1, "day_of": len(cur_s["days"]), "date": cell["date"], "now": cell["now"],
                   "since": now_call["t"], "started": _hm(now_call["t"], "%H:%M:%S"),
                   "avg": round(sum(same) / len(same)) if same else (round(sum(c["seconds"] for c in calls[-10:]) / len(calls[-10:]))
                                                                     if calls else None),
                   "avg_n": len(same), "detail": cell["detail"], "run_done": card["done"], "run_need": card["need"],
                   "run_pct": card["pct"], "eta": card["eta"], "left": card["left"]}

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

    return {"city": city, "cities": cs, "ov": overview(cs), "st": st, "now_box": now_box, "state": state, "stages": stages, "learn": learn, "test": test,
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
    return dict((k, w) for k, w, _ in dl.STAGES + dl.WALK_STAGES).get(step, step)


def _label(label):
    for m, name in MOMENT_NAME.items():
        label = label.replace(f" {m}", f" {name}")
    return label


def files_ver(city=None):
    """Отпечаток для живого обновления /diary: размер и время файлов дневника (кроме live.jsonl — терминал опрашивает сам)."""
    out = []
    for f in (sorted(dl.OUT.glob("*/*")) + [NEWS]) if dl.OUT.exists() else []:
        if f.name == "live.jsonl" or f.is_dir() or not f.exists():
            continue
        try:
            st = f.stat()
            out.append(f"{f.parent.name}/{f.name}:{st.st_size}:{int(st.st_mtime)}")
        except OSError:
            pass
    return "|".join(out)


# ---- «Что происходит» (22.10, Alex: «чтобы я не спрашивал каждый раз — видно, что вообще происходит») ----
NEWS = dl.OUT / "news.jsonl"      # заметки исследователя: python3 app/diary_lab.py note "текст"
# «Сейчас» — какое действие идёт (22.10, Alex: «Круг 1 - экзамен, Проверка - новый месяц без дневника, Обучение…»)
NOW_NAME = {"r0": "Контроль - экзамен без дневника", "study": "Обучение - знакомство с днями",
            "r1": "Круг 1 - экзамен", "review_r1": "Круг 1 - разбор ошибок",
            "r2": "Круг 2 - экзамен", "review_r2": "Круг 2 - разбор ошибок",
            "r3": "Круг 3 - экзамен", "review_r3": "Круг 3 - разбор ошибок",
            "final": "Финал - экзамен по учебному месяцу",
            "n1": "Проверка - новый месяц с дневником", "n1_empty": "Проверка - новый месяц без дневника"}


def _run_card(run):
    """Один запуск: модель, этап, сколько сделано из скольких, когда закончит."""
    base = dl.OUT / run
    try:
        st = json.loads((base / "status.json").read_text())
    except (OSError, ValueError):
        st = {}
    calls = [c for c in _jsonl(base / "calls.jsonl") if c.get("t", 0) >= st.get("started", 0)] if st else []
    n_days = {}
    for m in _months(run):
        try:
            k = len(dl.load(run, m)["days"])
        except (OSError, ValueError):
            k = 0
        n_days[m] = min(k, st["days"]) if st.get("days") else k
    stages, learn, test = _stages(run, st, calls, n_days)
    state = _state(run, st)
    if st.get("step") == "exam":     # отдельный экзамен (не цикл): сделано — ответы этого экзамена
        tag = (st.get("cmd") or "").split()[3:4] or [""]
        f = dl.exam_file(run, learn, tag[0])
        stages = [{"key": tag[0], "month": learn, "need": n_days.get(learn, 0) * len(dl.MOMENTS),
                   "done": len(_jsonl(f)) if f.exists() else 0, "state": "cur" if not st.get("finished") else "done",
                   "what": TAG_NAME.get(tag[0], tag[0])}]
        test = None
    # разбор, который ещё не начался: «нужно» неизвестно до экзамена — считаем все дни
    need = sum(s["need"] or (n_days.get(s["month"], 0) if s["key"].startswith("review_") else 0) for s in stages)
    done = sum(min(s["done"], s["need"] or s["done"]) for s in stages)
    cur = next((s for s in stages if s["state"] == "cur"), None)
    avg = sum(c["seconds"] for c in calls) / len(calls) if calls else None
    left = max(need - done, 0)
    eta = _hm(time.time() + left * avg, "%H:%M") if avg and state["code"] == "on" and left else None
    cost = sum(c.get("cost_usd") or 0 for c in calls)
    # «Статус»: Завершено <дата время> / В процессе <сколько идёт — таймер на странице тикает сам>
    if st.get("finished"):
        status = {"code": "bad" if st.get("error") else "done", "text": "Остановлено" if st.get("error") else "Завершено",
                  "at": _hm(st["finished"]), "since": None}
    elif st:
        status = {"code": "warn" if state["code"] == "warn" else "on", "text": "В процессе", "since": st.get("started"),
                  "elapsed": _dur(time.time() - st.get("started", time.time()))}
    else:
        status = {"code": "none", "text": "Не запускали", "since": None}
    now_key = st.get("stage") if st and not st.get("finished") else None
    # «Категория» (22.10, Alex): откуда работает модель — OpenRouter (id с «/»), Claude (Claude Code по подписке Alex,
    # в изолированном контейнере), Agy (Gemini через Antigravity), Server (своя Gemma на NAS)
    m = st.get("model") or ""
    category = ("OpenRouter" if "/" in m else "Claude" if m.startswith("claude") else "Agy" if m.startswith("gemini")
                else "Server")
    return {"run": run, "model": m, "category": category, "state": state, "status": status, "learn": learn, "test": test,
            "now": NOW_NAME.get(now_key, now_key or ""), "quiet": state["text"] if state["code"] == "warn" else "",
            "days": st.get("days"), "stage": cur["what"] if cur else "", "done": done, "need": need,
            "pct": round(100 * done / need) if need else 0, "eta": eta, "left": left,
            "avg": _dur(avg) if avg else None, "cost": round(cost, 2) if cost else None}


def _compare(runs):
    """Средний шанс верному варианту у ИИ и у рынка по этапам — по всем запускам (в процентах, больше — лучше)."""
    rows = []
    for run in runs:
        try:
            st = json.loads((dl.OUT / run / "status.json").read_text())
        except (OSError, ValueError):
            st = {}
        cells = []
        for tag in LEARN_TAGS + TEST_TAGS:
            month = st.get("test") if tag in TEST_TAGS else st.get("learn")
            f = dl.exam_file(run, month, tag) if month else None
            if not f or not f.exists():   # отдельный экзамен (шаг exam): месяц — по файлу
                fs = sorted((dl.OUT / run).glob(f"exam_*_{tag}.jsonl"))
                f, month = (fs[-1], fs[-1].stem[5:12]) if fs else (None, None)
            if not f or not f.exists():
                cells.append(None)
                continue
            try:
                days = {d["date"]: d for d in dl.load(run, month)["days"]}
            except (OSError, ValueError):
                cells.append(None)
                continue
            ai, mk, err, n = [], [], [], 0
            pnl, bets = 0.0, 0     # 23.10 (Alex): деньги по правилу score_rows — $1 на вариант, где ИИ выше цены на EDGE
            for a in _jsonl(f):
                d = days.get(a["date"])
                if not d:
                    continue
                n += 1
                ai.append(a["probs"].get(d["win"], 0) if a.get("ok") else 0)
                pr = {k: v["p"] for k, v in d["prices"].get(a["moment"], {}).items()}
                sm = sum(pr.values())
                if sm:
                    mk.append(pr.get(d["win"], 0) / sm)
                if a.get("ok") and dl._num(a.get("expected_max")):
                    err.append(abs(float(a["expected_max"]) - d["actual_max"]))
                for b, p_ai in (a["probs"].items() if a.get("ok") and sm else ()):
                    p = pr.get(b)
                    if p is None or not 0.05 <= p <= 0.95 or p_ai - p < dl.EDGE:
                        continue
                    buy = min(p + 0.01, 0.999)
                    bets += 1
                    pnl += (1 / (buy + 0.05 * buy * (1 - buy)) - 1) if b == d["win"] else -1
            cells.append({"ai": round(100 * sum(ai) / len(ai)) if ai else None, "pnl": round(pnl, 2), "bets": bets,
                          "mkt": round(100 * sum(mk) / len(mk)) if mk else None,
                          "err": round(sum(err) / len(err), 1) if err else None, "n": n})
        rows.append({"run": run, "model": st.get("model") or "", "months": f"{st.get('learn')} → {st.get('test')}",
                     "cells": cells})
    return rows


def _started(run):
    try:
        return json.loads((dl.OUT / run / "status.json").read_text()).get("started") or 0
    except (OSError, ValueError):
        return 0


def overview(runs):
    runs = sorted(runs, key=_started)      # 24.10 (Alex): по дате запуска, раньше — выше
    news = sorted(_jsonl(NEWS), key=lambda x: x.get("t", 0), reverse=True)[:30] if NEWS.exists() else []
    return {"cards": [_run_card(r) for r in runs], "compare": _compare(runs),
            "compare_tags": [TAG_NAME[t] for t in LEARN_TAGS + TEST_TAGS],
            "news": [dict(x, at=_hm(x.get("t"))) for x in news]}
