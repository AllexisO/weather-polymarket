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
01.11 (кошелёк llm_skill, app/weather_llm_skill.py): вторая очередь data/agy/skill_queue/<имя>.json — письмо скилла и папка
скилла на день. Такое письмо идёт через agy_box.sh (agy с Python в изолированном контейнере, 3-5 мин) — только когда основная
очередь пуста, по одному, перед каждым снова проверяем основную (её письма ждут не дольше одного письма скилла).
"""
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

LAB = Path(__file__).resolve().parent
AGY_DIR = LAB / "data" / "agy"
QUEUE, WORK, DONE = AGY_DIR / "queue", AGY_DIR / "work", AGY_DIR / "done"
SKILL_Q, SKILL_WORK = AGY_DIR / "skill_queue", LAB / "data" / "agy_box" / "work"
SKILL_TIMEOUT = 300      # секунд на письмо скилла (медиана проверок 217 с, максимум 312)
# у сервера нет группы docker (TrueNAS) — agy_box.sh запускает контейнер через sudo -n
DOCKER = "docker" if os.access("/var/run/docker.sock", os.W_OK) else "sudo -n docker"
PAUSE = AGY_DIR / "paused_until"
AGY = os.path.expanduser("~/.local/bin/agy")
MODEL = "gemini-3.8-flash-medium"
TIMEOUT = 240            # секунд на одно письмо (обычно 3-20 с)
PAUSE_SEC = 3600
QUOTA_WORDS = ("RESOURCE_EXHAUSTED", "quota", "Quota", "rate limit", "Rate limit")
# 15.10: Google иногда отвечает 503 «Eligibility check failed: UNAVAILABLE» за 2-15 с, через минуту работает
# (10-15.10 — 9 писем, на /status «с пропусками») — две повторные попытки с паузой
BUSY_WORDS = ("UNAVAILABLE", "code 503")
BUSY_RETRY = (20, 40)    # секунд до повтора; вместе с ответами укладывается в AGY_WAIT (480 с) погодного скрипта


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


def ask_skill(job):
    """Письмо скилла: своя чистая копия папки скилла → agy_box.sh. → (dict ответа agy или None, текст ошибки или None)."""
    work = SKILL_WORK / ("live_" + job["name"])
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(LAB / job["folder"], work)
    (work / ".prompt.txt").write_text(job["prompt"])
    try:
        p = subprocess.run([str(LAB / "agy_box.sh"), str(work / ".prompt.txt"), str(work), str(SKILL_TIMEOUT)], capture_output=True,
                           text=True, timeout=SKILL_TIMEOUT + 90, env={**os.environ, "DOCKER": DOCKER})
    except subprocess.TimeoutExpired:
        return None, f"скилл не ответил за {SKILL_TIMEOUT} с"
    finally:
        shutil.rmtree(work, ignore_errors=True)
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
    for d in (QUEUE, WORK, DONE, SKILL_Q):
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
        try:
            paused = float(PAUSE.read_text()) if PAUSE.exists() else 0
        except (OSError, ValueError):   # 11.10 (аудит): испорченный файл паузы не должен останавливать все письма
            paused = 0
        if paused > time.time():
            write_done(name, {"status": "PAUSED", "error": f"лимит Google — пауза до {datetime.fromtimestamp(paused):%H:%M}"})
            log(f"{name}: пауза (лимит Google)")
            continue
        t0 = time.time()
        res, err = ask(text)
        # агент иногда лезет в инструменты (в режиме без человека им отказано) и отвечает пустым — один повтор
        if res and not (res.get("response") or "").strip() and res.get("status") == "SUCCESS":
            res, err = ask(text)
        for wait in BUSY_RETRY:
            if (res and (res.get("response") or "").strip()) or not any(w in json.dumps(res or {}) + (err or "") for w in BUSY_WORDS):
                break
            log(f"{name}: Google 503 — повтор через {wait} с")
            time.sleep(wait)
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
    SKILL_Q.mkdir(parents=True, exist_ok=True)
    for f in sorted(SKILL_Q.glob("*.json")):
        if any(QUEUE.glob("*.txt")):
            break   # основная очередь — первой; скилл — в следующую минуту
        name = f.stem
        try:
            job = {**json.loads(f.read_text()), "name": name}
            f.unlink()
        except (OSError, ValueError):
            continue
        try:
            paused = float(PAUSE.read_text()) if PAUSE.exists() else 0
        except (OSError, ValueError):
            paused = 0
        if paused > time.time():
            write_done(name, {"status": "PAUSED", "error": f"лимит Google — пауза до {datetime.fromtimestamp(paused):%H:%M}"})
            log(f"{name}: пауза (лимит Google)")
            continue
        t0 = time.time()
        res, err = ask_skill(job)
        blob = json.dumps(res or {}) + (err or "")
        if any(w in blob for w in QUOTA_WORDS) and not (res and (res.get("response") or "").strip()):
            PAUSE.write_text(str(time.time() + PAUSE_SEC))
            log(f"{name}: лимит Google исчерпан — пауза на час: {blob[-300:]}")
        out = res or {"status": "ERROR"}
        if err:
            out["error"] = err
        out["seconds"] = round(time.time() - t0, 1)
        write_done(name, out)
        log(f"{name}: скилл {out.get('status')} {out['seconds']} с {(out.get('response') or out.get('error') or '').strip()[:80]!r}")


if __name__ == "__main__":
    sys.exit(main())
