"""
Свежая копия базы для исследователя (2026-09-29, решение Alex: «что-то, что само помогает обучать модель»).
Запускается вручную в начале недельного разбора (docs/RESEARCHER.md), не по крону.
VACUUM INTO — один согласованный снимок; база в WAL, поэтому запись крона во время копии не ждёт.
Копия пишется во временный файл и подменяет старую только целиком.
Запуск: docker compose run --rm -e JOB_TIMEOUT=0 --entrypoint python collector weather_research.py
"""
import os
import sqlite3
import time
from pathlib import Path

SRC = os.environ.get("POLY_LAB_DB", "/data/db/polymarket_lab.sqlite3")
DST = Path("/data/research/research.sqlite3")


def main():
    tmp = DST.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    t0 = time.time()
    conn = sqlite3.connect(SRC, timeout=120)
    conn.execute("VACUUM INTO ?", (str(tmp),))
    conn.close()
    chk = sqlite3.connect(str(tmp)).execute("PRAGMA quick_check").fetchone()[0]
    if chk != "ok":
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"копия повреждена: {chk}")
    for ext in ("-wal", "-shm"):
        Path(str(DST) + ext).unlink(missing_ok=True)
    os.replace(tmp, DST)
    print(f"копия базы для исследований готова: {DST.stat().st_size / 1e9:.1f} ГБ за {time.time() - t0:.0f} с", flush=True)


if __name__ == "__main__":
    main()
