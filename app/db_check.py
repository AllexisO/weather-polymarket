"""
Проверка «в базу можно писать» для сторожа db_watchdog.sh (2026-09-27, после блокировки
базы на 7 часов). Берём блокировку записи (BEGIN IMMEDIATE) и сразу отпускаем, ничего не
меняя. Код выхода 0 — база свободна, 1 — занята дольше WAIT_S секунд.
"""

import os
import sqlite3
import sys

DB = os.environ.get("POLY_LAB_DB", "/data/db/polymarket_lab.sqlite3")
WAIT_S = 20

try:
    conn = sqlite3.connect(DB, timeout=WAIT_S, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("ROLLBACK")
    conn.close()
except sqlite3.Error as e:
    print(f"база занята: {e}")
    sys.exit(1)
