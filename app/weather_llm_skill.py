"""
Скилл agy вживую — кошелёк llm_skill (01.11, Alex «запускай»). Gemini 3.8 Flash (Medium) через Antigravity — агент с Python на
папке скилла (app/agy_skill_live: SKILL.md, tools.py; данные станции строго до сегодняшнего дня — agy_skill.build) в изолированном
контейнере (agy_box.sh). Отвечает шансами по всем вариантам. Раньше — только проверки на прошлом (PRD §9, 18-19.10): лучший вариант
(круг 2) на 13-16.10 в 08:00 наравне с рынком (0.805 против 0.806), устойчивого перевеса не было; здесь — проверка деньгами.

Раз в день, 08:10 местного, 5 городов США (THRESHOLD_CITIES): письмо скилла ~0.22% недельного лимита Google (замер 17.10),
5 писем в день ≈ 8% недели — лимит общий с живыми agy-кошельками, каждый час было бы > 100%. Письмо — то же, что у llm_agy_nomem
(короткое, без памяти; на нём шли проверки), заголовок — «прочитай SKILL.md и следуй ему».

agy_box запускает docker — это может только сервер: письмо и путь к папке — в data/agy/skill_queue/<имя>.json, обработчик
agy_runner.py (крон сервера раз в минуту) берёт их, когда основная очередь пуста, и кладёт ответ в data/agy/done/<имя>.json.
Письмо идёт 3-5 мин (медиана проверок 217 с) — ждём до SKILL_WAIT. Ставка — по ценам в момент ответа (в проверках задержка
ответа давала мнимую фору), правило bet() как у LLM-кошельков: перевес ≥ 5 п.п., 10-90¢, $2, одна ставка на город в день.
Запуск: docker compose run --rm collector weather_llm_skill.py  (крон — каждый час в :10, работает там, где сейчас 08 местного)
"""
import json
import os
import shutil
import sqlite3
import time
from datetime import date, datetime, timezone
from pathlib import Path

import agy_skill
import weather_llm_hour as L
from jobmark import item_guard

WALLET = "llm_skill"
HOUR = 8
SKILL_LIVE = Path(__file__).parent / "agy_skill_live"
LIVE = Path("/data/agy_box/live")          # папки скилла на день (на сервере — data/agy_box/live)
SKILL_Q = L.AGY_DIR / "skill_queue"
SKILL_WAIT = 1800                          # секунд ждать все ответы (5 писем по 3-5 мин подряд)
HEAD = ("You are an expert weather forecaster working in a folder that contains a skill for this task. FIRST read SKILL.md in the "
        "current folder and follow its procedure: use Python and the data files there to compute, do not guess. Then reply with the "
        "JSON object described in SKILL.md.\n\nTODAY'S LETTER (all data known right now):\n\n")


def folder(day):
    """Папка скилла на день: данные до day + SKILL.md и tools.py живой версии. Собирается один раз на день."""
    out = LIVE / day.isoformat()
    if not (out / "SKILL.md").exists():
        agy_skill.build(day, out)
        for f in ("SKILL.md", "tools.py"):
            shutil.copy(SKILL_LIVE / f, out / f)
        for p in [out, *out.rglob("*")]:   # сервер копирует папку от своего пользователя — нужно чтение
            os.chmod(p, 0o755 if p.is_dir() else 0o644)
        for old in LIVE.iterdir():         # ~3 МБ в день — старше 2 дней не нужны
            if old.is_dir() and old.name < date.fromordinal(day.toordinal() - 2).isoformat():
                shutil.rmtree(old, ignore_errors=True)
    return out


def submit(name, text, work):
    SKILL_Q.mkdir(parents=True, exist_ok=True)
    (L.AGY_DIR / "done" / f"{name}.json").unlink(missing_ok=True)
    tmp = SKILL_Q / f".{name}.tmp"
    tmp.write_text(json.dumps({"prompt": text, "folder": f"data/agy_box/live/{work.name}"}))
    tmp.rename(SKILL_Q / f"{name}.json")


def collect(names):
    out, until = {}, time.time() + SKILL_WAIT
    while len(out) < len(names) and time.time() < until:
        for n in names:
            f = L.AGY_DIR / "done" / f"{n}.json"
            if n not in out and f.exists():
                try:
                    out[n] = json.loads(f.read_text())
                    f.unlink()
                except (OSError, ValueError):
                    pass
        time.sleep(10)
    for n in names:
        if n not in out:
            (SKILL_Q / f"{n}.json").unlink(missing_ok=True)
    return out


def run():
    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(L.DB_PATH, timeout=60)
    L.ensure_schema(conn)
    todo = []
    for city in L.THRESHOLD_CITIES:
        lnow = L.local_now(city, now)
        day = lnow.date()
        if lnow.hour == HOUR and not conn.execute("SELECT 1 FROM llm_hour_preds WHERE wallet = ? AND city = ? AND local_date = ?",
                                                  (WALLET, city, day.isoformat())).fetchone():
            todo.append((city, day))
    if not todo:
        print(f"{WALLET}: ни в одном городе сейчас не {HOUR:02d} местного (или уже спрошено) — пропуск")
        return
    obs = L.metars(now)
    pending = {}
    for city, day in todo:
        with item_guard(f"{city}/{WALLET}", conn):
            lnow = L.local_now(city, now)
            mk = L.markets(city, day)
            if not mk:
                print(f"{city}: маркета на {day} нет")
                continue
            hrs, sun = L.hourly(city)
            text, mx = L.prompt(conn, L.AGY_NOMEM_WALLET, city, day, lnow, obs, hrs, mk, sun, now, short=True, nomem=True)
            name = L.agy_name(WALLET, city, day, HOUR)
            submit(name, HEAD + text, folder(day))
            pending[name] = (city, day, HEAD + text)
    got = collect(list(pending))
    obs2 = L.metars(datetime.now(timezone.utc))
    for name, (city, day, text) in pending.items():
        with item_guard(f"{city}/{WALLET}", conn):
            a = got.get(name)
            if a is None:
                raise RuntimeError(f"скилл не ответил за {SKILL_WAIT // 60} мин — работает ли крон agy_runner.py на сервере?")
            raw = (a.get("response") or "").strip()
            try:
                ans = L._parse(raw)
            except ValueError:
                raise RuntimeError(f"скилл без ответа ({a.get('status')}: {(a.get('error') or raw)[:200]!r})") from None
            t = datetime.now(timezone.utc)
            lnow = L.local_now(city, t)
            mk = L.markets(city, day)          # цены — в момент ответа, не письма
            _txt, mx = L.prompt(conn, L.AGY_NOMEM_WALLET, city, day, lnow, obs2, [], mk, None, t, short=True, nomem=True)
            probs = L.probs_for(ans, mk, mx)
            conn.execute("""INSERT OR REPLACE INTO llm_hour_preds (wallet, city, local_date, local_hour, ts_utc, model, pred_max, probs_json,
                market_json, max_so_far, reason, cost, tokens, prompt, answer_json, probs_raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)""",
                         (WALLET, city, day.isoformat(), HOUR, t.isoformat(), f"agy_skill:{L.AGY_MODEL}", ans.get("max_f"),
                          json.dumps(probs) if probs else None, json.dumps({b["label"]: b["price"] for b in mk}), mx,
                          f"скилл: {str(ans.get('why', ''))[:250]} ({a.get('seconds', '?')} с)", (a.get("usage") or {}).get("total_tokens"),
                          text, json.dumps(ans, ensure_ascii=False), json.dumps(probs) if probs else None))
            conn.commit()
            res = L.bet(conn, WALLET, city, day, t, mk, probs) if probs else "шансы не разобрать"
            print(f"{WALLET} {city} {lnow:%H:%M}: максимум {ans.get('max_f')}° (уже {mx}), {a.get('seconds', '?')} с"
                  f"{'; ' + res if res else ''}", flush=True)
    conn.close()


if __name__ == "__main__":
    run()
