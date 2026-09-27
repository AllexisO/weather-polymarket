"""
Время последнего успешного запуска скриптов — для строки «Обновлено» на
/paper (2026-09-26, просьба Alex: видеть, когда обновлялись данные).
Скрипт вызывает mark(conn, "имя") в самом конце работы, перед conn.close().
"""

from datetime import datetime, timezone


def mark(conn, job):
    conn.execute("CREATE TABLE IF NOT EXISTS job_runs (job TEXT PRIMARY KEY, finished_at TEXT NOT NULL)")
    conn.execute("INSERT OR REPLACE INTO job_runs (job, finished_at) VALUES (?, ?)",
                 (job, datetime.now(timezone.utc).isoformat()))
    conn.commit()
