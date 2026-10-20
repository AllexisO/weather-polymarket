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
5. «насколько модель права» и повторитель — импортируются;
6. (с 15.10) правила на известных примерах и тесты данных (data_tests.py) — расчёт ставок, шансы моделей, сводки, итоги.
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

    # 2б2. выключенные кошельки (14.10) одинаковы в ставках и на сайте
    m = re.search(r"^(RETIRED = .*?)  #", (APP / "dashboard.py").read_text(), re.M)
    _ns = {}
    exec(m.group(1), {}, _ns) if m else None
    dash_r = _ns.get("RETIRED")
    res.append((dash_r == wp.RETIRED, "выключенные кошельки одинаковы в ставках и на сайте", f"ставки {wp.RETIRED}, сайт {dash_r}"))

    # 2в. у каждого кошелька есть полное описание логики (wallet_docs.py, страница кошелька)
    from wallet_docs import WALLET_DOCS
    nodoc = sorted((set(wallets) | {"copy", "obs", "obs_fmi", "obs_fast", "obs_rt", "obs_wethr", "llm_gem", "llm_ds", "llm_mix", "llm_cal", "llm_agy", "llm_agy_bet", "llm_agy_nomem", "ml_day", "ml_day_fav"}) - set(WALLET_DOCS))
    res.append((not nodoc, "у каждого кошелька есть описание логики", ", ".join(nodoc) or f"{len(WALLET_DOCS)} описаний"))

    # 2г. 03.10: группа каждого кошелька есть в списке групп /paper — иначе кошелёк молча не виден на странице
    m = re.search(r"^WALLET_GROUPS = (\[.*?\])", (APP / "dashboard.py").read_text(), re.M | re.S)
    grp = set(ast.literal_eval(m.group(1))) | {"Главная модель", "LLM каждый час"} if m else set()   # LLM — своя страница /llm
    src = (APP / "dashboard.py").read_text()
    info = dict(re.findall(r'^    "([a-z0-9_]+)": \("([^"]+)",', src[src.index("WALLET_INFO = {"):src.index("OBS_WALLETS =")], re.M))
    import weather_paper as _wp   # 14.10: выключенные кошельки в списке /paper не показываются — это нормально
    lost = sorted(f"{w} ({g})" for w, g in info.items() if g not in grp and w not in _wp.RETIRED)
    res.append((not lost, "у каждого кошелька группа видна на /paper", ", ".join(lost) or f"{len(info)} кошельков"))

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
        # 15.10 (проверка проекта): день — последний, где уже есть снимок 08:00 (решения кошельков). Раньше — MAX(local_date): с ~14:00
        # по Кишинёву там уже завтрашний день (снимки на завтра без 08:00), и прогон шёл с 0 решений — ничего не проверял.
        last = mem.execute("SELECT MAX(local_date) FROM snapshots WHERE local_hour = 8").fetchone()[0]
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
        res.append((sum(decided.values()) > 0, "прогон всех кошельков без ошибок (в памяти, без покупок)",
                    f"день {last}: решений {sum(decided.values())}; без решений: {', '.join(silent) or 'нет'}"))
    except Exception as e:
        res.append((False, "прогон всех кошельков без ошибок (в памяти, без покупок)",
                    f"{type(e).__name__}: {e} | {traceback.format_exc().strip().splitlines()[-3].strip()}"))

    # 4б. 15.10 (проверка проекта, Alex: «сделай тесты»): правила, на которых уже ошибались, — на известных примерах
    tests = []

    def t(name, got, want):
        if got != want:
            tests.append(f"{name}: получилось {got!r}, нужно {want!r}")
    try:
        from weather_station_obs import whole_f
        for tmpf, want in ((65.5, 65), (81.5, 82), (75.4, 75), (83.7, 84), (62.8, 63), (64.0, 64), (90.0, 90)):
            t(f"итог °F из {tmpf}", whole_f(tmpf), want)   # KBKF 13.10: 18.6 °C = 65.48 → 65 (Polymarket «64-65»)
        from weather_obs_live import safe_value, FMI_BETS
        t("целые 32 °C в °F-городе — нижняя граница", safe_value(32.0, "fahrenheit"), 89)
        t("десятые 31.7 °C в °F-городе", safe_value(31.7, "fahrenheit"), 89)
        t("obs_fmi ставок не делает (FMI против итога: 15 ошибок из 75 дней)", FMI_BETS, False)
        import weather_fastobs as fo
        t("KNMI и FMI только пишем", {"knmi", "fmi"} <= fo.NO_BET_SOURCES, True)
        t("DWD: 19.7 с запасом 0.3 — ещё не 20", fo.metar_value(19.7 - fo.ROUND_MARGIN["dwd"], "celsius"), 19)
        t("DWD: 19.8 с запасом 0.3 — уже 20", fo.metar_value(19.8 - fo.ROUND_MARGIN["dwd"], "celsius"), 20)
        t("JMA без запаса: 19.5 → 20", fo.metar_value(19.5 - fo.ROUND_MARGIN.get("jma", 0.0), "celsius"), 20)
        from weather_edge import parse_bucket
        for q, want in (("Will the highest temperature in Chicago be between 66-67°F on October 15?", (65.5, 67.5)),
                        ("Will the highest temperature in Chicago be 69°F or below on October 15?", (-999.0, 69.5)),
                        ("Will the highest temperature in London be 24°C or higher on October 15?", (23.5, 999.0)),
                        ("Will the highest temperature in London be 19°C on October 15?", (18.5, 19.5))):
            t(f"вариант из «{q[37:60]}…»", parse_bucket(q), want)
        from weather_cities import EXTRA_OBS_CITIES, OBS_CITIES
        import weather_poly_resolve  # noqa: F401 — итоги доп. городов (Гонконг, Тайбэй) собираются с 15.10
        t("доп. города не входят в 48 городов ставок моделей", set(EXTRA_OBS_CITIES) & set(OBS_CITIES), set())
        import weather_ml_day as md
        t("ml_day_fav: полоса «да»", md.FAV_BAND, (0.50, 0.95))
        import weather_mm_ml as mml   # 15.10: mm_ws_ml не покупает «да» на вариант, уже убитый сводкой, даже при прогнозе > 0
        from datetime import datetime as _dt, timezone as _tz
        ts15 = _dt(2026, 10, 15, 14, 0, tzinfo=_tz.utc).timestamp()   # 15:00 по Лондону, день маркета
        dead = mml.features("yes", 0.05, ts15, "london", "2026-10-15", 17.5, 18.5, 10, 22)
        alive = mml.features("yes", 0.40, ts15, "london", "2026-10-15", 21.5, 22.5, 10, 22)
        t("mm_ws_ml: «да» на убитый сводкой вариант", mml.take(dead, 0.12), False)
        t("mm_ws_ml: «да» на живой вариант при прогнозе > 0", mml.take(alive, 0.12), True)
        t("mm_ws_ml: прогноз ≤ 0 — не брать", mml.take(alive, -0.01), False)
    except Exception as e:
        tests.append(f"{type(e).__name__}: {e}")
    res.append((not tests, "правила на известных примерах (округление, источники замеров, варианты)",
                "; ".join(tests) or "все примеры сходятся"))

    # 4в. 15.10 (третья проверка проекта): тесты данных — ставки посчитаны по итогу, шансы моделей и LLM, сводки = архив,
    # быстрые замеры не «убивают» победителя (data_tests.py)
    import data_tests
    res += data_tests.run()

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
