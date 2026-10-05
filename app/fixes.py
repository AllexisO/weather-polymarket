"""
Отметки «ошибка исправлена» (2026-09-28, просьба Alex: «если мы уже пофиксили ошибку — помечать, а то
самообман: ошибка висит, хотя исправлена»).

Когда ошибка скрипта починена, записываем: какой скрипт, когда исправлено, что сделали. Все падения
этого скрипта ДО отметки считаются исправленными — вечерняя проверка, «Проверка», «Здоровье системы»
и «События» показывают их как «исправлено», а не как проблему. Упал снова после отметки — снова проблема.

Отметить (с сервера):
    sudo docker compose run --rm --entrypoint python collector fixes.py weather_copy "что исправлено"
    ... fixes.py weather_copy "что исправлено" --at 2026-09-27T20:53:00+00:00   # если исправлено раньше
"""

import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))


def _dt(s):
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def last_fixes(conn):
    """{скрипт: (когда исправлено, что сделали)} — последняя отметка по каждому скрипту."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'error_fixes'").fetchone():
        return {}
    out = {}
    for job, at, note in conn.execute("SELECT job, fixed_at, note FROM error_fixes ORDER BY fixed_at"):
        out[job.removesuffix(".py")] = (_dt(at), note)   # 02.10: «weather_x.py» и «weather_x» — один скрипт
    return out


def is_fixed(fixes, job, when):
    """Падение скрипта job в момент when исправлено, если после него есть отметка."""
    f = fixes.get(job)
    return bool(f and when is not None and _dt(when if isinstance(when, str) else when.isoformat()) <= f[0])


def _when(t):
    """«сегодня 23:15» / «вчера 23:15» / «26.09 23:15» по Кишинёву."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Chisinau")
    d, now = t.astimezone(tz), datetime.now(tz)
    day = "сегодня" if d.date() == now.date() else ("вчера" if d.date() == now.date() - timedelta(days=1) else f"{d:%d.%m}")
    return f"{day} {d:%H:%M}"


def _times(n):
    return "раз" if n % 10 == 1 and n % 100 != 11 or n % 10 not in (2, 3, 4) or n % 100 in (12, 13, 14) else "раза"


def failures(conn, since, labels):
    """Падения и пропуски скриптов после since, по-человечески: (не исправленные, исправленные) —
    списки строк вида «Повтор за сильными трейдерами (запасной опрос) падал 2 раза за сутки, последний раз вчера 23:15»."""
    has_ie = "item_errors" in [r[1] for r in conn.execute("PRAGMA table_info(job_log)")]
    rows = conn.execute(f"SELECT job, finished_at, rc{', item_errors' if has_ie else ', 0'} FROM job_log "
                        f"WHERE finished_at >= ? AND (rc != 0{' OR item_errors > 0' if has_ie else ''}) ORDER BY finished_at",
                        (since.isoformat(),)).fetchall()
    fx = last_fixes(conn)
    by = {}
    for job, at, rc, ie in rows:
        if job not in labels:
            continue  # ручные запуски и проверки — не скрипты крона
        by.setdefault((job, is_fixed(fx, job, at)), []).append((_dt(at), rc, ie))
    open_, fixed = [], []
    for (job, done), ev in by.items():
        n_fail = sum(1 for _t, rc, _ie in ev if rc != 0)
        n_part = sum(1 for _t, rc, ie in ev if rc == 0 and ie)
        what = []
        if n_fail:
            what.append(f"падал {n_fail} {_times(n_fail)}")
        if n_part:
            what.append(f"{n_part} {_times(n_part)} отработал с пропусками")
        text = f"«{labels[job]}» — {' и '.join(what)} за сутки, последний раз {_when(ev[-1][0])}"
        if done:
            fixed.append(f"{text}. Исправлено {_when(fx[job][0])}: {fx[job][1]}")
        else:
            open_.append(text)
    return open_, fixed


def mark_fixed(job, note, at=None):
    at = at or datetime.now(timezone.utc)
    conn = sqlite3.connect(DB_PATH, timeout=60)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS error_fixes (job TEXT, fixed_at TEXT, note TEXT)")
        conn.execute("INSERT INTO error_fixes VALUES (?, ?, ?)", (job.removesuffix(".py"), at.isoformat(), note))
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Отметить ошибку скрипта исправленной")
    ap.add_argument("job", help="скрипт, как в job_log: weather_copy, weather_edge ...")
    ap.add_argument("note", nargs="+", help="что исправлено")
    ap.add_argument("--at", help="когда исправлено (ISO), по умолчанию — сейчас")
    a = ap.parse_args()
    at = _dt(a.at) if a.at else None
    mark_fixed(a.job, " ".join(a.note), at)
    print(f"отмечено: {a.job} исправлен{' на ' + at.isoformat() if at else ''} — {' '.join(a.note)}")
