"""
Заметки с датой (2026-09-28, просьба Alex: «много текста, до 12 октября я всё забуду — пусть будут заметки,
и утренняя проверка скажет, что сегодня надо заглянуть»).

Заметка = дата + что сделать + подробности. Страница /notes (добавить, отметить «сделано»), счётчик в боковой
панели, а утренняя проверка (weather_night_check.py morning) пишет «сегодня по заметкам: …».

Отдельный маленький файл data/db/notes.sqlite3 — не рабочая база: сайт её только читает, а сюда пишет.

Из терминала:
    sudo docker compose run --rm --entrypoint python collector notes.py add 2026-10-12 "Что сделать" "Подробности"
    ... notes.py list        ... notes.py done 3
    ... notes.py add 2026-10-04 "Разбор недели" "..." --every 7   — повторяющаяся: «сделано» ставит следующую
    ... notes.py add 2026-10-06 "Что" "..." --time 21:00            — время по Кишинёву (по умолчанию 10:00)

2026-10-03 (просьба Alex: «не понимаю, когда проверять — днём, после 20:00, после 22:00?»): у заметки есть время по
Кишинёву. По умолчанию 10:00 — к этому часу отработали ночные задачи (сделки 04:30, обучение 05:20, расчёты), данные за
прошлый день полные. Нужны данные текущего дня — время позже (например 21:00).
"""

import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

NOTES_DB = Path(os.environ.get("NOTES_DB", Path(__file__).parent.parent / "data" / "db" / "notes.sqlite3"))
TZ = ZoneInfo("Europe/Chisinau")
DEFAULT_TIME = "10:00"


def today():
    return datetime.now(TZ).date().isoformat()


def connect():
    conn = sqlite3.connect(NOTES_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, due TEXT NOT NULL, title TEXT NOT NULL,
                    body TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, done_at TEXT)""")
    # 2026-09-29 (разбор недели по воскресеньям): повтор каждые N дней
    cols = [r[1] for r in conn.execute("PRAGMA table_info(notes)")]
    if "every_days" not in cols:
        conn.execute("ALTER TABLE notes ADD COLUMN every_days INTEGER")
    if "due_time" not in cols:   # 2026-10-03: время по Кишинёву
        conn.execute(f"ALTER TABLE notes ADD COLUMN due_time TEXT NOT NULL DEFAULT '{DEFAULT_TIME}'")
    return conn


def all_notes():
    """Все заметки: сначала несделанные по дате, потом сделанные (свежие сверху)."""
    if not NOTES_DB.exists():
        return []
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM notes ORDER BY done_at IS NOT NULL, CASE WHEN done_at IS NULL THEN due END, "
            "CASE WHEN done_at IS NULL THEN due_time END, done_at DESC")]
    finally:
        conn.close()


def due_notes(day=None):
    """Несделанные заметки на сегодня и просроченные."""
    day = day or today()
    return [n for n in all_notes() if n["done_at"] is None and n["due"] <= day]


def add(due, title, body="", every_days=None, due_time=None):
    try:
        datetime.strptime(due, "%Y-%m-%d")
    except ValueError:
        raise ValueError("такой даты нет — нужна дата вида 2026-10-12") from None
    due_time = (due_time or DEFAULT_TIME).strip()
    try:
        due_time = datetime.strptime(due_time, "%H:%M").strftime("%H:%M")
    except ValueError:
        raise ValueError("время — вида 10:00 или 21:30") from None
    if not title.strip():
        raise ValueError("пустая заметка")
    conn = connect()
    try:
        cur = conn.execute("INSERT INTO notes (due, title, body, created_at, every_days, due_time) VALUES (?, ?, ?, ?, ?, ?)",
                           (due, title.strip(), body.strip(), datetime.now(timezone.utc).isoformat(), every_days or None, due_time))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def set_done(note_id, done=True):
    conn = connect()
    try:
        conn.execute("UPDATE notes SET done_at = ? WHERE id = ?",
                     (datetime.now(timezone.utc).isoformat() if done else None, note_id))
        n = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        if done and n is not None and n["every_days"]:
            # повторяющаяся: следующая — через every_days от даты заметки, но не в прошлом
            nxt = date.fromisoformat(n["due"]) + timedelta(days=n["every_days"])
            while nxt <= date.fromisoformat(today()):
                nxt += timedelta(days=n["every_days"])
            if not conn.execute("SELECT 1 FROM notes WHERE title = ? AND due = ? AND done_at IS NULL",
                                (n["title"], nxt.isoformat())).fetchone():
                conn.execute("INSERT INTO notes (due, title, body, created_at, every_days, due_time) VALUES (?, ?, ?, ?, ?, ?)",
                             (nxt.isoformat(), n["title"], n["body"], datetime.now(timezone.utc).isoformat(), n["every_days"],
                              n["due_time"] if "due_time" in n.keys() else DEFAULT_TIME))
        conn.commit()
    finally:
        conn.close()


def delete(note_id):
    conn = connect()
    try:
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Заметки с датой")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("due"); a.add_argument("title"); a.add_argument("body", nargs="?", default="")
    a.add_argument("--every", type=int, default=None, help="повторять каждые N дней")
    a.add_argument("--time", default=None, help="время по Кишинёву, по умолчанию 10:00")
    sub.add_parser("list")
    d = sub.add_parser("done"); d.add_argument("id", type=int)
    x = sub.add_parser("delete"); x.add_argument("id", type=int)
    args = ap.parse_args()
    if args.cmd == "add":
        print(f"заметка {add(args.due, args.title, args.body, args.every, args.time)} на {args.due} {args.time or DEFAULT_TIME}: {args.title}"
              + (f" (каждые {args.every} дн.)" if args.every else ""))
    elif args.cmd == "done":
        set_done(args.id); print(f"заметка {args.id} — сделано")
    elif args.cmd == "delete":
        delete(args.id); print(f"заметка {args.id} удалена")
    else:
        for n in all_notes():
            print(f"{n['id']:>3} {n['due']} {n.get('due_time') or DEFAULT_TIME} {'✓' if n['done_at'] else ' '} {n['title']}")
