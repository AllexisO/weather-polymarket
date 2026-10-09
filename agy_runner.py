#!/usr/bin/env python3
"""
Обработчик писем кошелька llm_agy (09.10, решение Alex): Gemini 3.8 Flash (Medium) через Antigravity CLI (agy).

agy стоит на сервере (~/.local/bin/agy, вход по аккаунту Google Alex), а не в контейнере. Поэтому:
  weather_llm_hour.py (контейнер) кладёт письмо в data/agy/queue/<город>_<дата>_<час>.txt →
  этот скрипт (крон сервера раз в минуту) отдаёт его agy и пишет ответ в data/agy/done/<то же>.json →
  weather_llm_hour.py забирает ответ, считает шансы и ставит.
Только стандартная библиотека Python сервера. Работает в пустой папке data/agy/work — агенту нечего трогать,
и GEMINI.md проекта (правила вида сайта) в письмо не попадает.
Исчерпан лимит Google — пауза на час (data/agy/paused_until): письма сразу получают ответ PAUSED, Google не долбим.
Лог — data/logs/agy_runner.log, пишется только когда есть письма.
"""
import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

LAB = Path(__file__).resolve().parent
AGY_DIR = LAB / "data" / "agy"
QUEUE, WORK, DONE = AGY_DIR / "queue", AGY_DIR / "work", AGY_DIR / "done"
PAUSE = AGY_DIR / "paused_until"
AGY = os.path.expanduser("~/.local/bin/agy")
MODEL = "gemini-3.8-flash-medium"
TIMEOUT = 240            # секунд на одно письмо (обычно 3-20 с)
PAUSE_SEC = 3600
QUOTA_WORDS = ("RESOURCE_EXHAUSTED", "quota", "Quota", "rate limit", "Rate limit")


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def ask(text):
    """→ (dict ответа agy или None, текст ошибки или None)."""
    try:
        p = subprocess.run([AGY, "-p", text, "--model", MODEL, "--output-format", "json", "--disable-slash-commands",
                            "--print-timeout", f"{TIMEOUT}s"], capture_output=True, text=True, timeout=TIMEOUT + 30, cwd=WORK)
    except subprocess.TimeoutExpired:
        return None, f"agy не ответил за {TIMEOUT} с"
    try:
        return json.loads(p.stdout), None
    except ValueError:
        return None, (p.stderr or p.stdout or f"код {p.returncode}").strip()[-400:]


def write_done(name, obj):
    tmp = DONE / f".{name}.tmp"
    tmp.write_text(json.dumps(obj, ensure_ascii=False))
    os.chmod(tmp, 0o666)
    tmp.rename(DONE / f"{name}.json")


def main():
    for d in (QUEUE, WORK, DONE):
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o777)   # контейнер пишет от root, сервер — от allexiso: обоим нужно создавать и удалять файлы
        except PermissionError:
            pass
    lock = open(AGY_DIR / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return   # прошлый запуск ещё работает
    for f in sorted(QUEUE.glob("*.txt")):
        name = f.stem
        try:
            text = f.read_text()
            f.unlink()
        except OSError:
            continue
        paused = float(PAUSE.read_text()) if PAUSE.exists() else 0
        if paused > time.time():
            write_done(name, {"status": "PAUSED", "error": f"лимит Google — пауза до {datetime.fromtimestamp(paused):%H:%M}"})
            log(f"{name}: пауза (лимит Google)")
            continue
        t0 = time.time()
        res, err = ask(text)
        # агент иногда лезет в инструменты (в режиме без человека им отказано) и отвечает пустым — один повтор
        if res and not (res.get("response") or "").strip() and res.get("status") == "SUCCESS":
            res, err = ask(text)
        blob = json.dumps(res or {}) + (err or "")
        if any(w in blob for w in QUOTA_WORDS) and not (res and (res.get("response") or "").strip()):
            PAUSE.write_text(str(time.time() + PAUSE_SEC))
            log(f"{name}: лимит Google исчерпан — пауза на час: {blob[-300:]}")
        out = res or {"status": "ERROR"}
        if err:
            out["error"] = err
        out["seconds"] = round(time.time() - t0, 1)
        write_done(name, out)
        log(f"{name}: {out.get('status')} {out['seconds']} с {(out.get('response') or out.get('error') or '').strip()[:80]!r}")


if __name__ == "__main__":
    sys.exit(main())
