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


def log_run(db_path, job, rc, started, finished, om_calls=0, item_errors=0, output=None):
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
        if "item_errors" not in [r[1] for r in conn.execute("PRAGMA table_info(job_log)")]:
            conn.execute("ALTER TABLE job_log ADD COLUMN item_errors INTEGER DEFAULT 0")
        # 2026-09-29: хвост вывода запуска — «что произошло» в ленте /events
        if "output" not in [r[1] for r in conn.execute("PRAGMA table_info(job_log)")]:
            conn.execute("ALTER TABLE job_log ADD COLUMN output TEXT")
        conn.execute("INSERT INTO job_log (job, started_at, finished_at, rc, duration_s, om_calls, item_errors, output) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (job, started.isoformat(), finished.isoformat(), rc,
                      round((finished - started).total_seconds(), 1), om_calls, item_errors, output))
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


# ---- страховка на один элемент (2026-09-27, решение Alex: «даже если что-то упало — продолжаем») ----
# Цикл по городам / трейдерам / дням оборачивается так:
#     for city, cfg in CITIES.items():
#         with item_guard(city, conn):
#             ...
# Любая ошибка внутри (кроме остановки по пределу времени) пишется в лог, незавершённая запись в базу
# по этому элементу откатывается, скрипт идёт к следующему. Число пропусков обёртка job_wrap.py
# пишет в job_log.item_errors — на /status такой запуск виден как «с пропусками».
ITEM_ERRORS = []


class item_guard:
    def __init__(self, what, conn=None):
        self.what, self.conn = what, conn

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None or not issubclass(exc_type, Exception):
            return False  # KeyboardInterrupt / SystemExit (предел времени) — пусть останавливает
        import traceback
        where = traceback.extract_tb(tb)[-1] if tb else None
        msg = f"{self.what}: {exc_type.__name__}: {exc}"
        ITEM_ERRORS.append(msg)
        print(f"ОШИБКА ({msg[:300]}) — пропускаю и продолжаю"
              + (f" [{where.filename.rsplit('/', 1)[-1]}:{where.lineno}]" if where else ""), flush=True)
        if self.conn is not None:
            try:
                self.conn.rollback()
            except Exception:
                pass
        return True
