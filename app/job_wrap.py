"""
Обёртка запуска скрипта крона (2026-09-27, страница «Здоровье системы»): запускает скрипт
как обычно (python скрипт аргументы), считает запросы к Open-Meteo и по окончании пишет
запуск в job_log (jobmark.log_run): код выхода, длительность, число запросов.
2026-09-29 (просьба Alex: «для каждого события видеть, что конкретно произошло»): вывод скрипта (stdout и stderr)
дублируется в память и хвост (до OUT_MAX символов) пишется в job_log.output — лента /events показывает его как лог запуска.
Вызывается из run_job.sh; сам скрипт ничего о ней не знает.
"""

import collections
import os
import runpy
import signal
import sys
import threading
import traceback
from datetime import datetime, timezone
from urllib.parse import urlparse

APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP)
import jobmark  # noqa: E402
from jobmark import log_run  # noqa: E402
from jobs_info import KEY_BY_SCRIPT  # noqa: E402

_calls = {"om": 0}
_lock = threading.Lock()
OUT_MAX = 12000  # символов вывода на запуск в job_log (хвост; начало обрезается)
_out = {"parts": collections.deque(), "n": 0}


class _Tee:
    """Пишет как обычно и копит хвост вывода (не больше ~2×OUT_MAX символов в памяти) для job_log."""
    def __init__(self, orig):
        self._orig = orig

    def write(self, s):
        if isinstance(s, str) and s:
            _out["parts"].append(s)
            _out["n"] += len(s)
            while _out["n"] > 2 * OUT_MAX and len(_out["parts"]) > 1:
                _out["n"] -= len(_out["parts"].popleft())
        return self._orig.write(s)

    def flush(self):
        return self._orig.flush()

    def __getattr__(self, name):
        return getattr(self._orig, name)


def _output():
    text = "".join(_out["parts"])
    return text[-OUT_MAX:] if text.strip() else None


def _count_open_meteo():
    try:
        import requests.sessions
    except ImportError:
        return
    orig = requests.sessions.Session.request

    def request(self, method, url, *a, **k):
        if "open-meteo" in (urlparse(str(url)).hostname or ""):
            with _lock:
                _calls["om"] += 1
        return orig(self, method, url, *a, **k)

    requests.sessions.Session.request = request


def main():
    script = sys.argv[1]
    name = os.path.basename(script)
    job = KEY_BY_SCRIPT.get(name, name.removesuffix(".py"))
    path = script if os.path.isabs(script) or os.path.exists(script) else os.path.join(APP, script)
    sys.argv = sys.argv[1:]
    # предел времени (run_job.sh) шлёт TERM — выходим штатно, чтобы запуск записался
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    _count_open_meteo()
    sys.stdout, sys.stderr = _Tee(sys.stdout), _Tee(sys.stderr)
    started = datetime.now(timezone.utc)
    rc = 1
    try:
        runpy.run_path(path, run_name="__main__")
        rc = 0
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        if not isinstance(e.code, (int, type(None))):
            print(e.code, file=sys.stderr)
    except BaseException:
        traceback.print_exc()
        rc = 1
    finally:
        sys.stdout.flush()
        log_run(os.environ.get("POLY_LAB_DB", "/data/db/polymarket_lab.sqlite3"), job, rc,
                started, datetime.now(timezone.utc), _calls["om"], len(jobmark.ITEM_ERRORS), _output())
        if jobmark.ITEM_ERRORS:
            print(f"ИТОГ: пропущено из-за ошибок — {len(jobmark.ITEM_ERRORS)}", flush=True)
    sys.exit(rc)


if __name__ == "__main__":
    main()
