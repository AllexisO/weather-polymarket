"""
Учёт запросов к Open-Meteo у запусков мимо обёртки крона (06.11, Alex: «хочу видеть кол-во запросов»).

Скрипты крона считает job_wrap.py (job_log.om_calls). Ручные запуски — исследования (`--entrypoint python ...`,
`JOB_TIMEOUT=0`, `python -c`) — шли мимо неё, и карточка /status показывала 2.7 тыс. при настоящих ~7.4 тыс. за сутки
(06.11: загрузки проверки вечерней модели). Теперь sitecustomize.py в каждом python контейнера collector ставит этот счётчик,
кроме запусков через job_wrap (run_job.sh ставит OM_WRAPPED=1 — не считать дважды).

Пишет data/db/om_usage.sqlite3: om_usage (ts_utc, script, calls) — порциями каждые FLUSH_EVERY запросов и при выходе.
Любая ошибка учёта молча пропускается: счётчик не должен ломать запуск.
"""

import atexit
import os
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

DB = Path(os.environ.get("OM_USAGE_DB", "/data/db/om_usage.sqlite3"))
FLUSH_EVERY = 50
_n = {"pending": 0}
_lock = threading.Lock()


def _script():
    a = sys.argv[0] if sys.argv and sys.argv[0] else ""
    if a in ("-c", ""):
        return "python -c"
    return os.path.basename(a)


def flush():
    with _lock:
        n, _n["pending"] = _n["pending"], 0
    if not n:
        return
    try:
        conn = sqlite3.connect(DB, timeout=10)
        conn.execute("CREATE TABLE IF NOT EXISTS om_usage (ts_utc TEXT, script TEXT, calls INTEGER)")
        conn.execute("INSERT INTO om_usage VALUES (?, ?, ?)", (datetime.now(timezone.utc).isoformat(), _script(), n))
        conn.commit()
        conn.close()
    except Exception:
        pass


def install():
    try:
        import requests.sessions
    except ImportError:
        return
    orig = requests.sessions.Session.request

    def request(self, method, url, *a, **k):
        if "open-meteo" in (urlparse(str(url)).hostname or ""):
            with _lock:
                _n["pending"] += 1
                full = _n["pending"] >= FLUSH_EVERY
            if full:
                flush()
        return orig(self, method, url, *a, **k)

    requests.sessions.Session.request = request
    atexit.register(flush)


def last_24h(db=DB):
    """[(скрипт, запросов)] за последние сутки, по убыванию; нет файла — пусто."""
    if not Path(db).exists():
        return []
    since = datetime.now(timezone.utc).timestamp() - 86400
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        rows = conn.execute("SELECT script, SUM(calls) FROM om_usage WHERE ts_utc >= ? GROUP BY script ORDER BY 2 DESC",
                            (datetime.fromtimestamp(since, timezone.utc).isoformat(),)).fetchall()
        conn.close()
        return [(s, int(n)) for s, n in rows if n]
    except sqlite3.Error:
        return []
