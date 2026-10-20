"""17.10 (просьба Alex «видеть, как идут расчёты»): страница /skill - ход проверки скилла agy (data/research/agy_skill_bt.py).
Только чтение файлов: письма и ответы наборов (data/research/agy_skill_bt_<набор>[_claude_haiku]{_letters,}.jsonl),
бюджеты (data/agy_box/slots), идёт ли прогон (блокировка run_<набор>.lock занята), последние ответы, предварительный logloss
против рынка на письмах с известным итогом (weather_poly_outcomes). Деньги и пороги - в agy_skill_compare.py, не здесь.
"""
import fcntl
import json
import math
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

RE_LABEL = re.compile(r"^(-?\d+)(?:-(-?\d+))?°[FC]( or below| or higher)?$")


def label_range(lab):
    """Как weather_llm_hour.label_range (там импорт requests, которого нет в контейнере сайта)."""
    m = RE_LABEL.match(lab.strip())
    if not m:
        return None
    a, b, tail = float(m.group(1)), m.group(2), m.group(3)
    if tail == " or below":
        return (-999.0, a + 0.5)
    if tail == " or higher":
        return (a - 0.5, 999.0)
    return (a - 0.5, (float(b) if b else a) + 0.5)

R = Path("/data/research")
SLOTS = Path("/data/agy_box/slots")
TZ = ZoneInfo("Europe/Chisinau")
# (набор, подпись, что добавлено) - порядок как на странице
SETS = [("", "старая проверка 09-14.10", "скилл v1 на письмах 09-14.10, 8/10/12/14 ч - итоги известны"),
        ("new_v2", "v2 (база)", "v1 + ошибки прогнозов и LightGBM на станции"),
        ("new_v1", "v1", "первая версия скилла"),
        ("new_v2_mkt", "+ рынок", "насколько честны цены рынка в этот час"),
        ("new_v2_mlday", "+ ml_day", "дневная модель и её ошибки"),
        ("new_v2_nbr", "+ соседи", "соседние станции"),
        ("new_v2_hourly", "+ почасовой", "почасовой прогноз и его ошибка к пику"),
        ("new_v2_fewshot", "+ примеры", "4 разобранных прошлых письма"),
        ("new_v2_selfmem", "+ свои ответы", "свои прошлые ответы и кто был прав"),
        ("new_v2_r2", "повтор v2 №2", "для среднего трёх ответов"),
        ("new_v2_r3", "повтор v2 №3", "для среднего трёх ответов")]
ENGINES = [("_claude_sonnet", "Sonnet"), ("_claude_haiku", "Haiku"), ("", "Gemini")]
SINCE = datetime(2026, 10, 15).timestamp()


def _jsonl(p):
    try:
        return [json.loads(l) for l in open(p) if l.strip()]
    except OSError:
        return []


def _running(lock):
    try:
        f = open(lock, "r")
    except OSError:
        return False
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(f, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        f.close()


def _probs(resp, market):
    try:
        a, b = resp.find("{"), resp.rfind("}")
        p = {k: max(0.0, float(v)) for k, v in json.loads(resp[a:b + 1], strict=False)["probs"].items()}
        p = {k: p.get(k, 0.0) for k in market}
        t = sum(p.values())
        return {k: v / t for k, v in p.items()} if t > 0 else None
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def _ll(p, win):
    lo, hi = win
    mid = (lo + hi) / 2
    s = sum(v for k, v in p.items() if (r := label_range(k)) and r[0] <= mid <= r[1])
    return -math.log(max(1e-3, s))


def _cnt(log, since=0.0):
    try:
        return sum(float(t) >= since for t in log.read_text().split())
    except (OSError, ValueError):
        return 0


# 20.10 (Alex: «отдельный блок для Gemma - сколько писем, время на письмо, доходность»): прогоны вето на Gemma
# (data/research/veto, заметки #92/#95/#102). (тег, подпись, что считается)
# 20.10 (Alex: «только цифры, без воды»): только прогон no_cheap; отладка fav (8 писем) - не показываем.
VETO_RUNS = [("_nc", "вето no_cheap", "")]


def _veto_pnl(price, won):
    """На $1 ставки - как weather_ml_check.pnl: покупка на 1¢ дороже, комиссия 0.05·p·(1−p)."""
    buy = min(price + 0.01, 0.999)
    return 1 / (buy + 0.05 * buy * (1 - buy)) - 1 if won else -1.0


def veto_rows():
    """Сколько писем вето обработано, среднее время на письмо, сколько осталось и доходность: все ставки / оставленные."""
    L = {x["id"]: x for x in _jsonl(R / "agy_skill_bt_veto_letters.jsonl")}
    out = []
    for tag, name, what in VETO_RUNS:
        ids = (R / "veto" / ("no_cheap_ids.txt" if tag == "_nc" else "debug_ids.txt"))
        try:
            need = set(ids.read_text().split())
        except OSError:
            continue
        A = {a["id"]: a for a in _jsonl(R / f"agy_skill_bt_veto_gemma{tag}.jsonl") if a["id"] in need}
        ok, err, secs = [], 0, []
        for a in A.values():
            secs.append(a.get("seconds") or 0)
            r = a.get("response") or ""
            try:
                c = json.loads(r[r.find("{"):r.rfind("}") + 1], strict=False)["cancel"]
                if not isinstance(c, bool):
                    raise ValueError
                ok.append((L[a["id"]]["bet"], c))
            except (ValueError, KeyError, TypeError):
                err += 1
        allp = [_veto_pnl(b["price"], b["won"]) for b, _ in ok]
        kept = [_veto_pnl(b["price"], b["won"]) for b, c in ok if not c]
        wins = sum(b["won"] for b, _ in ok)
        running = _running(SLOTS / f"run_veto_gemma{tag}.lock")
        avg = sum(secs) / len(secs) if secs else None
        left = len(need) - len(A)
        out.append({"name": name, "what": what, "done": len(A), "need": len(need), "err": err, "running": running,
                    "avg_min": avg / 60 if avg else None,
                    "eta_h": left * avg / 3600 if avg and left and running else None,
                    "n": len(ok), "cancel": sum(c for _, c in ok),
                    "cancel_won": sum(c and b["won"] for b, c in ok), "cancel_lost": sum(c and not b["won"] for b, c in ok),
                    "won_cut": 100 * sum(c and b["won"] for b, c in ok) / wins if wins else None,
                    "roi_all": 100 * sum(allp) / len(allp) if allp else None,
                    "roi_kept": 100 * sum(kept) / len(kept) if kept else None, "n_kept": len(kept),
                    "gain": 100 * (sum(kept) / len(kept) - sum(allp) / len(allp)) if kept and allp else None})
    return out


def skill_page_data(conn):
    win = {(c, d): (lo, hi) for c, d, lo, hi in conn.execute(
        "SELECT city, local_date, win_lo, win_hi FROM weather_poly_outcomes WHERE local_date >= '2026-10-09'")}
    now = time.time()
    rows, recent = [], []
    for s, name, what in SETS:
        _S = f"_{s}" if s else ""
        L = {x["id"]: x for x in _jsonl(R / f"agy_skill_bt{_S}_letters.jsonl")}
        hours = {8, 10, 12, 14} if not s else {10, 14}
        need = [i for i, x in L.items() if x["hour"] in hours]
        for e, ename in ENGINES:
            ap = R / f"agy_skill_bt{_S}{e}.jsonl"
            A = _jsonl(ap)
            if not A and not (e and _running(SLOTS / f"run_{s or 'old'}{e}.lock")):
                continue
            ok, err, sk, mk, n_ll = set(), 0, 0.0, 0.0, 0
            for a in A:
                x = L.get(a["id"])
                p = _probs(a.get("response") or "", x["market"]) if x else None
                if p is None:
                    err += 1
                    continue
                ok.add(a["id"])
                w = win.get((x["city"], x["day"]))
                if w:
                    t = sum(v or 0 for v in x["market"].values()) or 1
                    sk += _ll(p, w)
                    mk += _ll({k: (v or 0) / t for k, v in x["market"].items()}, w)
                    n_ll += 1
            mt = ap.stat().st_mtime if ap.exists() else None
            rows.append({"name": name, "what": what, "engine": ename, "ok": len(ok & set(need)), "need": len(need),
                         "err": err, "running": _running(SLOTS / f"run_{s or 'old'}{e}.lock"),
                         "last": datetime.fromtimestamp(mt, TZ).strftime("%d.%m %H:%M") if mt else "-",
                         "n_ll": n_ll, "ll": sk / n_ll if n_ll else None, "ll_m": mk / n_ll if n_ll else None})
            if e and mt and now - mt < 6 * 3600:
                for a in A[-6:]:
                    why = ""
                    try:
                        r = a.get("response") or ""
                        why = json.loads(r[r.find("{"):r.rfind("}") + 1], strict=False).get("why", "")
                    except (ValueError, AttributeError):
                        why = (a.get("error") or r)[-120:]
                    recent.append({"at": a.get("at") or mt, "set": name, "id": a["id"], "sec": a.get("seconds"),
                                   "ok": a["id"] in ok, "why": why[:160]})
    recent.sort(key=lambda r: r["at"], reverse=True)
    for r in recent:
        r["at_s"] = datetime.fromtimestamp(r["at"], TZ).strftime("%H:%M")

    def week(fname):
        try:
            w0, wmax = (SLOTS / fname).read_text().split()[:2]
            return float(w0), int(wmax)
        except (OSError, ValueError):
            return None, 0
    c0, cmax = week("claude_week.txt")
    g0, gmax = week("gemini_week.txt")
    budget = {"c_used": _cnt(SLOTS / "claude_calls.log", c0 or now), "c_max": cmax,
              "c_5h": _cnt(SLOTS / "claude_calls.log", now - 5 * 3600),
              "g_used": _cnt(SLOTS / "gemini_calls.log", g0) if g0 else 0, "g_max": gmax,
              "g_paused": not gmax}
    return {"rows": rows, "recent": recent[:25], "budget": budget, "veto": veto_rows(),
            "updated": datetime.now(TZ).strftime("%d.%m %H:%M:%S")}


TRACE_KIND = {"start": "старт", "think": "думает", "text": "пишет", "call": "python", "result": "вывод", "empty": "пусто",
              "fix": "поправка", "parse_fix": "поправка", "final": "готово", "error": "ошибка"}


def live_traces(now, fresh=900, last=30):
    """20.10 (Alex: «хочу видеть вживую, как это работает»): шаги писем Gemma / DeepSeek / Qwen (gemma_box.py, GEMMA_TRACE) -
    файлы data/research/agy_trace/<набор><движок><тег>/<письмо>.jsonl, изменённые за последние 15 мин: последние шаги каждого."""
    files = []
    for f in (R / "agy_trace").glob("*/*.jsonl"):
        try:
            mt = f.stat().st_mtime
        except OSError:
            continue
        if now - mt < fresh:
            files.append((mt, f))
    out = []
    for mt, f in sorted(files):
        try:
            ev = [json.loads(x) for x in f.read_text(errors="replace").splitlines() if x.strip()]
        except (OSError, ValueError):
            continue
        if not ev:
            continue
        model = next((e["text"] for e in ev if e["kind"] == "start"), "?")
        done = ev[-1]["kind"] in ("final", "error")
        state = "закончено" if done else "считает"
        out.append(f"▶ {state}  {model:40s} {f.parent.name:28s} {f.stem:24s} уже {int(ev[-1]['t'] - ev[0]['t'])} с, "
                   f"последний шаг {int(now - ev[-1]['t'])} с назад")
        for e in ev[-last:]:
            hh = datetime.fromtimestamp(e["t"], TZ).strftime("%H:%M:%S")
            txt = " ".join(str(e.get("text", "")).split())
            out.append(f"    {hh} ход {e.get('turn', 0):2d} {TRACE_KIND.get(e['kind'], e['kind']):9s} {txt[:300]}")
        out.append("")
    return out


def skill_terminal():
    """17.10 (просьба Alex «как в терминале»): что считается прямо сейчас (рабочие папки писем) + хвост логов прогонов Haiku."""
    now = time.time()
    names = {s: name for s, name, _ in SETS}
    out = []
    started = []
    for w in Path("/data/agy_box/work").glob("*_claude_*"):
        try:
            t0 = (w / ".prompt.txt").stat().st_mtime     # пишется в начале письма (папка - копия, её время старое)
        except OSError:
            continue
        if now - t0 < 600:                               # дольше - остаток прерванного прогона
            s, _, rest = w.name.partition("_claude_")
            m = next((m for m in ("haiku", "sonnet") if rest.startswith(m)), "")   # папка: набор + _claude_<модель> + письмо
            started.append((t0, (s, m, rest[len(m):])))
    for t0, (s, m, lid) in sorted(started):
        out.append(f"▶ считает  {m:7s} {names.get(s, s or 'старая проверка'):24s} {lid:24s} уже {int(now - t0)} с")
    out += live_traces(now)
    if not out:
        out.append("▶ сейчас ничего не считается")
    out.append("")
    lines = []
    for (s, name, _), m in ((x, m) for x in SETS for m in ("haiku", "sonnet")):
        p = R / f"agy_skill_bt{'_' + s if s else ''}_claude_{m}.log"
        try:
            mt = p.stat().st_mtime
            tail = p.read_text(errors="replace").splitlines()[-8:]
        except OSError:
            continue
        for i, l in enumerate(tail):
            lines.append((mt - (len(tail) - i) * 1e-3, f"[{m} · {name}] {l[:220]}"))
    lines.sort()
    out += [l for _, l in lines[-60:]]
    return "\n".join(out)
