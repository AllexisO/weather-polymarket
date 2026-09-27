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


def log_run(db_path, job, rc, started, finished, om_calls=0):
    """2026-09-27 (страница «Здоровье системы»): каждый запуск скрипта крона — в job_log
    (итог, длительность, запросы к Open-Meteo). Пишет обёртка job_wrap.py / run_job.sh.
    Короткое соединение; база занята — запись пропускается, скрипт от этого не падает."""
    import sqlite3
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=15)
        conn.execute("""CREATE TABLE IF NOT EXISTS job_log (job TEXT, started_at TEXT, finished_at TEXT,
                        rc INTEGER, duration_s REAL, om_calls INTEGER)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_job_log_job ON job_log(job, finished_at)")
        conn.execute("INSERT INTO job_log VALUES (?, ?, ?, ?, ?, ?)",
                     (job, started.isoformat(), finished.isoformat(), rc,
                      round((finished - started).total_seconds(), 1), om_calls))
        if rc == 0:
            mark(conn, job)
        conn.execute("DELETE FROM job_log WHERE finished_at < datetime('now', '-14 days')")
        conn.commit()
    except Exception as e:
        print(f"job_log: не записано ({type(e).__name__}: {e})", flush=True)
        if conn is not None:
            conn.rollback()
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    # run_job.sh: python jobmark.py <скрипт> <код> <старт, секунды с 1970> — когда скрипт убит по пределу
    import os
    import sys
    from jobs_info import KEY_BY_SCRIPT
    script, rc, t0 = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
    name = os.path.basename(script)
    log_run(os.environ.get("POLY_LAB_DB", "/data/db/polymarket_lab.sqlite3"),
            KEY_BY_SCRIPT.get(name, name.removesuffix(".py")), rc,
            datetime.fromtimestamp(t0, timezone.utc), datetime.now(timezone.utc))
