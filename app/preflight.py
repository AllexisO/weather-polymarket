"""
Проверка перед выкладкой изменений (2026-09-27, правило Alex: «при сборке делать тесты
на все кошельки»). Повод: при вводе v4 кошельки ml4 ссылались на колонку, которой ещё
не было в snapshots, и падал ВЕСЬ запуск weather_paper (00:10-01:10, 27.09).

Что проверяем (рабочую базу НЕ меняем — открываем только на чтение):
1. все скрипты app/*.py собираются (нет синтаксических ошибок);
2. у каждого кошелька (WALLETS, MAKER_WALLETS, NO_WALLETS) его колонка есть в snapshots;
3. все колонки, которые пишут weather_edge.py и weather_ml_fast.py, есть в таблицах;
4. ПРОГОН ВСЕХ КОШЕЛЬКОВ: нужные таблицы копируются в память, за последний день ставки
   стираются, и weather_paper.place() решает заново по-настоящему — только покупка
   подменена «не купили» (без сети). Любая ошибка SQL/логики вылезет здесь, а не в кроне;
5. «насколько модель права» и повторитель — импортируются.
Страницы дашборда проверяет check.sh (снаружи контейнера).

Запуск: docker compose run --rm collector preflight.py   (код выхода 1 — есть ошибки)
Каждые 30 минут то же самое делает weather_alerts.py (красная плашка при ошибке).
"""

import glob
import os
import re
import sqlite3
import sys
import traceback
from pathlib import Path

APP = Path(__file__).parent
DB_PATH = Path(os.environ.get("POLY_LAB_DB", APP.parent / "data" / "db" / "polymarket_lab.sqlite3"))


def insert_columns(path, table):
    """Колонки из INSERT INTO <table> (...) в исходнике скрипта."""
    src = Path(path).read_text()
    out = set()
    for m in re.finditer(rf"INSERT(?: OR \w+)? INTO {table}\s*\(([^)]*)\)", src):
        out |= {c.strip() for c in m.group(1).replace("\n", " ").split(",") if c.strip()}
    return out


def run_checks():
    res = []
    # 1. сборка скриптов
    bad = []
    for f in sorted(glob.glob(str(APP / "*.py"))):
        try:
            compile(Path(f).read_text(), f, "exec")
        except SyntaxError as e:
            bad.append(f"{Path(f).name}: строка {e.lineno}: {e.msg}")
    res.append((not bad, "все скрипты собираются", "; ".join(bad) or f"{len(glob.glob(str(APP / '*.py')))} файлов"))

    ro = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    # 2026-09-27: база в режиме WAL — чтение (сайт) не ждёт записи; после блокировки на 7 часов
    jm = ro.execute("PRAGMA journal_mode").fetchone()[0]
    res.append((jm == "wal", "база в режиме WAL (чтение не ждёт записи)", f"сейчас: {jm}"))
    cols = lambda t: {r[1] for r in ro.execute(f"PRAGMA table_info({t})")}
    snap, fast = cols("snapshots"), cols("snapshots_fast")

    # 2. колонки кошельков
    import weather_paper as wp
    wallets = {**wp.WALLETS, **wp.MAKER_WALLETS, **wp.NO_WALLETS}
    miss = [f"{w} → {f}" for w, f in wallets.items() if f not in snap]
    res.append((not miss, "у каждого кошелька есть его колонка в snapshots", ", ".join(miss) or f"{len(wallets)} кошельков"))

    # 2b. стартовый баланс кошельков одинаков в ставках и на сайте
    import ast
    m = re.search(r"^WALLET_START = (\{.*\})", (APP / "dashboard.py").read_text(), re.M)
    dash = ast.literal_eval(m.group(1)) if m else None
    res.append((dash == wp.START_BY_WALLET, "стартовый баланс кошельков одинаков в ставках и на сайте",
                f"ставки {wp.START_BY_WALLET}, сайт {dash}"))

    # 2в. у каждого кошелька есть полное описание логики (wallet_docs.py, страница кошелька)
    from wallet_docs import WALLET_DOCS
    nodoc = sorted((set(wallets) | {"copy", "obs", "obs_fmi"}) - set(WALLET_DOCS))
    res.append((not nodoc, "у каждого кошелька есть описание логики", ", ".join(nodoc) or f"{len(WALLET_DOCS)} описаний"))

    # 3. колонки, которые пишут снимки
    for script, table, have in (("weather_edge.py", "snapshots", snap), ("weather_ml_fast.py", "snapshots_fast", fast)):
        need = insert_columns(APP / script, table)
        miss = sorted(need - have)
        res.append((not miss, f"{script} пишет только существующие колонки {table}", ", ".join(miss) or f"{len(need)} колонок"))

    # 4. прогон всех кошельков в памяти
    try:
        mem = sqlite3.connect(":memory:")
        mem.row_factory = sqlite3.Row
        mem.execute(f"ATTACH DATABASE 'file:{DB_PATH}?mode=ro' AS src")
        for t in ("snapshots", "snapshots_fast", "paper_trades", "weather_station_daily", "weather_poly_outcomes", "ml_skill"):
            if ro.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (t,)).fetchone():
                mem.execute(f"CREATE TABLE {t} AS SELECT * FROM src.{t}")
        mem.execute("DETACH DATABASE src")
        last = mem.execute("SELECT MAX(local_date) FROM snapshots").fetchone()[0]
        mem.execute("DELETE FROM paper_trades WHERE local_date >= ?", (last,))
        mem.commit()
        nofill = {"shares": 0.0, "cost": 0.0, "fee": 0.0, "avg": None, "min_ask": None, "book": None,
                  "reason": "проверка preflight: покупка не выполнялась"}
        orig = (wp.buy_yes, wp.find_market, wp.trading_stopped)
        wp.buy_yes = lambda *a, **k: dict(nofill)
        wp.find_market = lambda *a, **k: None
        wp.trading_stopped = lambda: False
        import contextlib
        import io
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                wp.place(mem, "preflight")
        finally:
            wp.buy_yes, wp.find_market, wp.trading_stopped = orig
        decided = {r[0]: r[1] for r in mem.execute(
            "SELECT wallet, COUNT(*) FROM paper_trades WHERE local_date >= ? GROUP BY wallet", (last,))}
        silent = [w for w in wallets if w not in decided]
        res.append((True, "прогон всех кошельков без ошибок (в памяти, без покупок)",
                    f"день {last}: решений {sum(decided.values())}; без решений: {', '.join(silent) or 'нет'}"))
    except Exception as e:
        res.append((False, "прогон всех кошельков без ошибок (в памяти, без покупок)",
                    f"{type(e).__name__}: {e} | {traceback.format_exc().strip().splitlines()[-3].strip()}"))

    # 5. остальные части
    for mod in ("weather_ml_skill", "weather_copy", "weather_copy_live", "weather_ml_fast", "weather_alerts", "dashboard"):
        try:
            __import__(mod)
            res.append((True, f"{mod} загружается", ""))
        except ModuleNotFoundError as e:
            # у контейнера collector нет fastapi (дашборд — свой контейнер): это не ошибка
            res.append((True, f"{mod} загружается", f"пропущено: {e.name} нет в этом контейнере"))
        except Exception as e:
            res.append((False, f"{mod} загружается", f"{type(e).__name__}: {e}"))
    ro.close()
    return res


if __name__ == "__main__":
    results = run_checks()
    for ok, name, detail in results:
        print(f"{'✓' if ok else '✗'} {name}" + (f" — {detail}" if detail else ""))
    failed = [r for r in results if not r[0]]
    print(f"\nИТОГ: {'ВСЁ В ПОРЯДКЕ' if not failed else f'ОШИБОК: {len(failed)}'}")
    sys.exit(1 if failed else 0)
