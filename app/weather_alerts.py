"""
Проверки «всё ли работает» — для предупреждений на страницах и уведомлений
(2026-09-26, просьба Alex: узнавать о сбоях, а не искать их глазами).

Что проверяем:
- крон: каждый важный скрипт отработал не позже положенного (job_runs, jobmark.py);
- ночное обучение прошло все проверки (ml_train_log.ok);
- главная модель не потеряла за сутки больше LOSS_ALERT долларов (закрытые ставки).

Пишет таблицу alerts: активные предупреждения (resolved_at IS NULL) показываются
на /paper и /training; когда проблема ушла — предупреждение закрывается.
Куда ещё слать (Telegram и т.п.) — решает Alex; до этого только страница.
Крон: каждые 30 минут. Запуск: python weather_alerts.py
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("POLY_LAB_DB", Path(__file__).parent.parent / "data" / "db" / "polymarket_lab.sqlite3"))

# скрипт -> (подпись, сколько можно не обновляться); 2026-09-27: общий список с /status (jobs_info.py)
from jobs_info import JOBS as _ALL_JOBS
JOBS = {key: (label, timedelta(minutes=age)) for key, label, _s, _sch, age, _log in _ALL_JOBS
        if key != "weather_alerts"}  # сам себя не проверяет — это видно на /status
MAIN_WALLET = "ml3"
LOSS_ALERT = 50.0  # $ за сутки по закрытым ставкам главной модели (решение Alex, 2026-09-26)


def check(conn):
    now = datetime.now(timezone.utc)
    found = {}
    runs = {}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'job_runs'").fetchone():
        runs = dict(conn.execute("SELECT job, finished_at FROM job_runs"))
    for job, (label, limit) in JOBS.items():
        if job not in runs:
            continue  # ещё ни разу не отметился — не тревожим
        age = now - datetime.fromisoformat(runs[job])
        if age > limit:
            h = age.total_seconds() / 3600
            found[f"stale:{job}"] = f"«{label}» не обновлялось {h:.0f} ч — проверьте крон и логи data/logs/"
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'ml_train_log'").fetchone():
        r = conn.execute("SELECT trained_at, ok, details FROM ml_train_log ORDER BY trained_at DESC LIMIT 1").fetchone()
        if r and not r[1]:
            bad = [c["title"] for c in json.loads(r[2]).get("checks", []) if not c["ok"]]
            found["train_checks"] = "Ночное обучение: не пройдены проверки — " + "; ".join(bad)
    pnl = 0.0  # settled_at с разными поясами — сравниваем как время, не как строки
    for payout, stake, fee, at in conn.execute(
            """SELECT payout, stake, fee, settled_at FROM paper_trades
               WHERE wallet = ? AND status IN ('won', 'lost', 'void') AND settled_at IS NOT NULL""", (MAIN_WALLET,)):
        t = datetime.fromisoformat(at)
        if (t if t.tzinfo else t.replace(tzinfo=timezone.utc)) >= now - timedelta(days=1):
            pnl += (payout or 0) - stake - (fee or 0)
    if pnl < -LOSS_ALERT:
        found["loss_day"] = f"Главная модель за сутки: {pnl:+.2f}$ (порог −{LOSS_ALERT:.0f}$)"
    # 2026-09-27: полная проверка системы (weather_audit.py → audit_log, страница /audit)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'audit_log'").fetchone():
        r = conn.execute("SELECT ok, details FROM audit_log ORDER BY run_at DESC LIMIT 1").fetchone()
        if r and not r[0]:
            n = json.loads(r[1]).get("n_violations", 0)
            found["audit"] = f"Проверка системы нашла нарушений: {n} — подробности на странице «Проверка» (/audit)"
    # 2026-09-28: вечерняя проверка перед ночью (weather_night_check.py, 23:30) — проблемы видно до сна
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'night_check'").fetchone():
        # 2026-09-28: утро / день / вечер — последняя проверка каждого вида
        has_mode = "mode" in [x[1] for x in conn.execute("PRAGMA table_info(night_check)")]
        names = {"morning": "Утренняя", "midday": "Дневная", "evening": "Вечерняя"}
        for mode in (names if has_mode else ["evening"]):
            r = conn.execute("SELECT run_at, bad FROM night_check" + (" WHERE mode = ?" if has_mode else " WHERE ? = ?")
                             + " ORDER BY run_at DESC LIMIT 1", (mode,) if has_mode else (1, 1)).fetchone()
            if r and r[1] and now - datetime.fromisoformat(r[0]) < timedelta(hours=26):
                found[f"night_check:{mode}"] = f"{names[mode]} проверка: проблем {r[1]} — что делать, на странице «Проверка» (/audit)"
    # 2026-09-27: та же проверка, что preflight.py (все кошельки прогоняются в памяти) —
    # поломка видна раньше, чем упадёт настоящий запуск кошельков
    try:
        import preflight
        failed = [f"{n}: {d}" for ok, n, d in preflight.run_checks() if not ok]
        if failed:
            found["preflight"] = "Проверка кошельков не прошла — " + " | ".join(failed)[:500]
    except Exception as e:
        found["preflight"] = f"Проверка кошельков не запустилась: {type(e).__name__}: {e}"
    return found


def main():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("""CREATE TABLE IF NOT EXISTS alerts (key TEXT PRIMARY KEY, message TEXT, first_seen TEXT,
                    last_seen TEXT, resolved_at TEXT, notified INTEGER DEFAULT 0)""")
    now = datetime.now(timezone.utc).isoformat()
    found = check(conn)
    for key, msg in found.items():
        row = conn.execute("SELECT resolved_at FROM alerts WHERE key = ?", (key,)).fetchone()
        if row is None or row[0] is not None:
            conn.execute("INSERT OR REPLACE INTO alerts VALUES (?, ?, ?, ?, NULL, 0)", (key, msg, now, now))
            print(f"НОВОЕ: {msg}")
        else:
            conn.execute("UPDATE alerts SET message = ?, last_seen = ? WHERE key = ?", (msg, now, key))
    for (key,) in conn.execute("SELECT key FROM alerts WHERE resolved_at IS NULL").fetchall():
        if key not in found:
            conn.execute("UPDATE alerts SET resolved_at = ? WHERE key = ?", (now, key))
            print(f"закрыто: {key}")
    conn.commit()
    print(f"активных предупреждений: {len(found)}")
    conn.close()


if __name__ == "__main__":
    main()
